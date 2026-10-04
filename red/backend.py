"""The agent backend: Claude Code, driven headlessly.

Every agent turn in RED is the same shape — a charter, a set of sealed MCP
tools, one reply — so the backend is small. It writes an MCP config naming the
tool servers :mod:`red.localtool` is already hosting, runs ``claude -p`` with
exactly those tools allowed, and hands back the reply.

Two properties of this arrangement are worth stating because the loop depends
on them:

* **The tool allow-list is the seal.** Each turn is given only the tools of the
  servers it was handed, named explicitly. An agent cannot reach another
  agent's surface by guessing, which is what makes Gate 2's independence and
  the review's read-only access mean something.
* **A turn is a subprocess.** It cannot corrupt the driver, it can be given a
  hard timeout, and several can run at once — which is how the four review
  sub-agents stay concurrent now that there is no cluster to fan them out
  across.

Replaces the Vertex AI backend. RED is model-agnostic by construction: the loop
only ever calls :meth:`ClaudeCodeLLM.prompt`.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import time
from dataclasses import dataclass, field

from red.constants import (
    AGENT_MAX_TURNS,
    AGENT_MODEL,
    AGENT_PERMISSION_MODE,
    AGENT_TIMEOUT_SECONDS,
    CLAUDE_BIN,
)


@dataclass
class QueryResult:
    """One agent turn's outcome.

    ``success`` is what the loop branches on; everything else is for the audit
    trail. A failed turn is reported, never raised — the loop has bounded
    repair edges for every stage and they only work if a failure arrives as a
    value.
    """

    result: str = ""
    returncode: int = 0
    stderr: str = ""
    stream_result: str = ""          # the transcript, for the run's log
    success: bool = True
    cost_usd: float = 0.0
    turns: int = 0
    seconds: float = 0.0


def tool_names(tools) -> list:
    """The fully-qualified MCP names of every method on *tools*.

    Claude Code addresses a tool as ``mcp__<server>__<tool>``; the servers in
    :mod:`red.tools` register each method as ``<server>_<method>``, so the
    allow-list entry is ``mcp__<server>__<server>_<method>``.
    """
    names = []
    for t in tools:
        for method in getattr(t, "METHODS", ()):
            names.append(f"mcp__{t.name}__{t.name}_{method}")
    return names


def mcp_config(tools) -> dict:
    """The ``--mcp-config`` payload pointing at the live tool servers."""
    return {"mcpServers": {t.name: {"type": "http", "url": t.url}
                           for t in tools}}


class ClaudeCodeLLM:
    """Runs agent turns through the Claude Code CLI.

    ``system_message`` is assigned by the loop between stages — the designer's
    charter for a design turn, cleared for a reviewer so it does not believe it
    is reviewing its own work — and is passed as an appended system prompt.
    """

    def __init__(self, model: str = AGENT_MODEL,
                 timeout_seconds: int = AGENT_TIMEOUT_SECONDS,
                 max_turns: int = AGENT_MAX_TURNS):
        self.model = model
        self.timeout_seconds = timeout_seconds
        self.max_turns = max_turns
        self.system_message = ""

    def prompt(self, message: str, tools=()) -> QueryResult:
        """One turn. Returns a QueryResult; never raises for an agent failure."""
        started = time.monotonic()
        tools = list(tools or ())
        cmd = [CLAUDE_BIN, "-p", message,
               "--output-format", "json",
               "--permission-mode", AGENT_PERMISSION_MODE,
               "--max-turns", str(self.max_turns)]
        if self.model:
            cmd += ["--model", self.model]
        if self.system_message:
            cmd += ["--append-system-prompt", self.system_message]

        cfg_path = None
        try:
            if tools:
                fd, cfg_path = tempfile.mkstemp(prefix="red-mcp-", suffix=".json")
                with os.fdopen(fd, "w") as f:
                    json.dump(mcp_config(tools), f)
                cmd += ["--mcp-config", cfg_path,
                        # Only the servers named here, and only their tools:
                        # no inherited project MCP config, no file system, no
                        # shell. The agents reach their artifacts through the
                        # sealed surface or not at all.
                        "--strict-mcp-config",
                        "--allowedTools", ",".join(tool_names(tools))]
            try:
                proc = subprocess.run(cmd, capture_output=True, text=True,
                                      timeout=self.timeout_seconds)
            except subprocess.TimeoutExpired:
                return QueryResult(
                    returncode=-1, success=False,
                    stderr=f"the turn did not finish within "
                           f"{self.timeout_seconds}s",
                    seconds=time.monotonic() - started)
        finally:
            if cfg_path and os.path.exists(cfg_path):
                os.unlink(cfg_path)

        elapsed = time.monotonic() - started
        if proc.returncode != 0:
            return QueryResult(returncode=proc.returncode, success=False,
                               stderr=(proc.stderr or proc.stdout)[-2000:],
                               seconds=elapsed)
        try:
            payload = json.loads(proc.stdout)
        except json.JSONDecodeError:
            # A reply that is not JSON means the CLI printed something other
            # than a result envelope; keep the text so the log shows what.
            return QueryResult(result=proc.stdout, returncode=0,
                               success=bool(proc.stdout.strip()),
                               stderr=proc.stderr[-2000:], seconds=elapsed)
        text = payload.get("result") or ""
        return QueryResult(
            result=text,
            returncode=0,
            success=not payload.get("is_error", False),
            stderr=payload.get("error", "") or "",
            stream_result=_transcript(message, payload, text),
            cost_usd=float(payload.get("total_cost_usd") or 0.0),
            turns=int(payload.get("num_turns") or 0),
            seconds=elapsed)


def _transcript(message: str, payload: dict, text: str) -> str:
    """What goes in the run's own log, so the audit trail is the artifacts
    *plus* how the agents got there."""
    head = message if len(message) < 4000 else message[:4000] + "\n[...]"
    return (f"--- prompt ---\n{head}\n--- reply "
            f"({payload.get('num_turns', '?')} turns, "
            f"${payload.get('total_cost_usd', 0):.4f}) ---\n{text}\n")
