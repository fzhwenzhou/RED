"""Sealed MCP tool servers, hosted in this process.

RED's agents reach their artifacts only through MCP tools whose destination
paths are baked in at construction, so an agent sees the payload and never a
path it could redirect. That design is unchanged; what changed is where the
server lives.

It used to be a Ray actor on a cluster node, which meant a tool call travelled
head -> Ray -> actor -> uvicorn, and on a tunnelled cloud worker the agent could
not dial the head's address at all without a relay rewrite. None of that bought
anything: the tools are a few hundred lines of Python reading and writing local
files. Here each one is a uvicorn server on a loopback port in a daemon thread
of the driver process, and the agent reaches it at
``http://127.0.0.1:<port>/<name>/mcp``.

The surface :mod:`red.tools` builds on is deliberately the same as the old
base class — ``self.mcp.add_tool(...)`` in ``__init__``, then
``__post_init__()`` to serve, ``stop()`` to shut down — so the sealed tool
classes themselves did not have to change.
"""

from __future__ import annotations

import logging
import socket
import threading
import time

import uvicorn
from mcp.server.fastmcp import FastMCP


def free_port() -> int:
    """A loopback port nothing is listening on.

    Asked of the kernel rather than scanned: the old implementation walked a
    fixed range from a base port and every tool server rescanned it as it
    bound, which cost minutes of start-up before the first agent turn when
    several tools came up together.
    """
    s = socket.socket()
    try:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]
    finally:
        s.close()


class LocalTool:
    """One sealed MCP tool server, served from this process.

    Subclasses register their methods in ``__init__`` and then call
    ``super().__post_init__()``::

        class MyTool(LocalTool):
            METHODS = ("read_thing",)
            def __init__(self, name, path):
                super().__init__(name)
                self.path = path
                for m in self.METHODS:
                    self.mcp.add_tool(getattr(self, m), name=f"{name}_{m}")
                super().__post_init__()
    """

    #: how long to wait for the server to accept connections before giving up
    START_TIMEOUT = 15.0

    def __init__(self, name: str, task_options=None):
        # task_options is accepted and ignored: it carried Ray placement
        # ("put this actor on the head"), and there is no longer anywhere else
        # to put it. Keeping the parameter means the sealed tool classes keep
        # their signatures.
        del task_options
        self.name = name
        self.hostname = "127.0.0.1"
        self.port = free_port()
        self.mcp = FastMCP(name=name, host=self.hostname, port=self.port,
                           streamable_http_path=f"/{name}/mcp")
        self._server: uvicorn.Server | None = None
        self._thread: threading.Thread | None = None

    @property
    def url(self) -> str:
        """Where an agent reaches this tool."""
        return f"http://{self.hostname}:{self.port}/{self.name}/mcp"

    def __post_init__(self) -> None:
        """Serve, and do not return until the port actually accepts.

        Returning early is the failure that is hard to debug: the agent's first
        tool call hits a closed port, the MCP client reports a transport error
        rather than a missing tool, and the turn fails for a reason that looks
        nothing like "the server had not finished binding".
        """
        config = uvicorn.Config(self.mcp.streamable_http_app(),
                                host=self.hostname, port=self.port,
                                log_level="warning", access_log=False)
        self._server = uvicorn.Server(config)
        # uvicorn installs signal handlers on the main thread only; in a worker
        # thread that raises, so tell it not to.
        self._server.install_signal_handlers = lambda: None
        self._thread = threading.Thread(target=self._server.run, daemon=True,
                                        name=f"mcp-{self.name}")
        self._thread.start()

        deadline = time.monotonic() + self.START_TIMEOUT
        while time.monotonic() < deadline:
            if getattr(self._server, "started", False):
                return
            with socket.socket() as probe:
                probe.settimeout(0.2)
                if probe.connect_ex((self.hostname, self.port)) == 0:
                    return
            time.sleep(0.05)
        raise RuntimeError(
            f"tool server {self.name!r} did not start listening on "
            f"{self.hostname}:{self.port} within {self.START_TIMEOUT:.0f}s")

    def stop(self) -> None:
        """Shut the server down and release the port. Safe to call twice."""
        if self._server is not None:
            self._server.should_exit = True
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=5.0)
        self._server, self._thread = None, None


def quiet_servers() -> None:
    """Keep uvicorn's per-request chatter out of the run log.

    A long agent turn makes hundreds of MCP calls and each one logged a line,
    which buried the driver's own progress output. Silence the whole `mcp` and
    `uvicorn` trees rather than the loggers observed to be noisy: the names
    move between releases, and a run log the operator cannot read is how a
    failing stage goes unnoticed for an hour.
    """
    for name in ("uvicorn", "mcp", "httpx", "sse_starlette"):
        logging.getLogger(name).setLevel(logging.WARNING)
