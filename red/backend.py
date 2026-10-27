"""The agent backends: coding-agent CLIs, driven headlessly.

Every agent turn in RED is the same shape — a charter, a set of sealed MCP
tools, one reply — so a backend is small: build an argv, run it with a
deadline, pull the final message out. What differs between CLIs is only how
each of those three is spelled, which is why :class:`AgentBackend` owns the
turn and the subclasses own the spelling.

Two properties the loop depends on, and which every backend must preserve:

* **The tool set is the seal.** A turn is given only the servers it was handed
  and is isolated from whatever MCP configuration the operator has for their
  own use. An agent cannot reach another agent's surface by guessing, which is
  what makes Gate 2's independence and the review's read-only access mean
  something.
* **A turn is a subprocess.** It cannot corrupt the driver, it can be given a
  hard timeout, and several can run at once, which is how the four review
  sub-agents stay concurrent.

Backends available, selected per stage in ``models.toml`` (:mod:`red.modelcfg`):

``claude``  Anthropic's Claude Code. ``claude -p`` with the prompt in argv, an
            MCP config file, ``--strict-mcp-config`` and an explicit
            ``--allowedTools`` allow-list.
``codex``   OpenAI's Codex CLI. ``codex exec`` with the prompt on stdin, MCP
            servers as ``-c mcp_servers.<name>.url=...`` overrides, and
            ``--ignore-user-config`` for the isolation. Shape learned from
            CHIA's ``chia/models/codex.py``.

RED is model- and vendor-agnostic by construction: the loop only ever calls
``prompt(message, tools, role=...)`` on whatever :func:`make_router` returns.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass

from red.constants import (
    AGENT_EFFORT,
    AGENT_MAX_TURNS,
    AGENT_MODEL,
    AGENT_PERMISSION_MODE,
    AGENT_TIMEOUT_SECONDS,
    CLAUDE_BIN,
    CODEX_BIN,
)
from red.modelcfg import (
    DEFAULT_BACKEND,
    ModelConfig,
    Routing,
    load as load_models,
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
    model: str = ""          # what actually ran this turn, for the audit trail


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


def server_tool_names(tool) -> list:
    """The MCP tool names one server exposes, as the server registers them.

    Claude Code namespaces these (``mcp__<server>__<name>``); Codex's
    per-server ``enabled_tools`` wants them bare.
    """
    return [f"{tool.name}_{m}" for m in getattr(tool, "METHODS", ())]


def mcp_config(tools) -> dict:
    """The ``--mcp-config`` payload pointing at the live tool servers."""
    return {"mcpServers": {t.name: {"type": "http", "url": t.url}
                           for t in tools}}


# ---------------------------------------------------------------------------
# the shared turn
# ---------------------------------------------------------------------------

class AgentBackend(ABC):
    """One coding-agent CLI, driven headlessly.

    Subclasses supply :meth:`_argv` (and may override :meth:`_parse`); this
    class owns everything that must behave the same whichever CLI runs:
    the deadline, the temp files, and the rule that an agent failure comes
    back as a value rather than an exception.

    ``system_message`` is assigned by the loop between stages — the designer's
    charter for a design turn, cleared for a reviewer so it does not believe
    it is reviewing its own work.
    """

    name = "agent"

    def __init__(self, model: str = "", effort: str = "",
                 timeout_seconds: int = AGENT_TIMEOUT_SECONDS,
                 max_turns: int = AGENT_MAX_TURNS):
        self.model = model
        self.effort = effort
        self.timeout_seconds = timeout_seconds
        self.max_turns = max_turns
        self.system_message = ""

    # -- what a subclass must say -------------------------------------------

    @abstractmethod
    def _argv(self, message: str, tools: list, route: Routing,
              scratch: str) -> tuple[list[str], str | None]:
        """Return (argv, stdin_text). *scratch* is a temp dir for this turn,
        removed afterwards, for config files and last-message sinks."""

    @abstractmethod
    def _parse(self, proc, route: Routing, scratch: str,
               message: str) -> QueryResult:
        """Turn a finished subprocess into a QueryResult."""

    # -- the turn ------------------------------------------------------------

    def prompt(self, message: str, tools=(), role: str = "") -> QueryResult:
        """One turn. Returns a QueryResult; never raises for an agent failure.

        *route* carries the model and effort for this stage; the caller
        resolved it, because the same backend object serves several roles.
        """
        return self.run(message, tools, Routing(backend=self.name,
                                                model=self.model,
                                                effort=self.effort))

    def run(self, message: str, tools, route: Routing) -> QueryResult:
        started = time.monotonic()
        tools = list(tools or ())
        scratch = tempfile.mkdtemp(prefix=f"red-{self.name}-")
        try:
            argv, stdin_text = self._argv(message, tools, route, scratch)
            try:
                proc = subprocess.run(argv, input=stdin_text,
                                      capture_output=True, text=True,
                                      timeout=self.timeout_seconds)
            except subprocess.TimeoutExpired:
                return QueryResult(
                    returncode=-1, success=False,
                    stderr=f"the turn did not finish within "
                           f"{self.timeout_seconds}s",
                    model=route.label(),
                    seconds=time.monotonic() - started)
            except OSError as exc:
                # The CLI is not installed or not executable. That is a setup
                # failure, not a design failure, and it must not read as one.
                return QueryResult(
                    returncode=-1, success=False,
                    stderr=f"could not start {argv[0]!r}: {exc}",
                    model=route.label(),
                    seconds=time.monotonic() - started)
            res = self._parse(proc, route, scratch, message)
            res.seconds = time.monotonic() - started
            res.model = route.label()
            return res
        finally:
            _rmtree(scratch)


def _rmtree(path: str) -> None:
    import shutil
    shutil.rmtree(path, ignore_errors=True)


def _transcript(message: str, text: str, turns, cost) -> str:
    """What goes in the run's own log, so the audit trail is the artifacts
    *plus* how the agents got there."""
    head = message if len(message) < 4000 else message[:4000] + "\n[...]"
    cost_s = f"${cost:.4f}" if isinstance(cost, (int, float)) and cost else "cost n/a"
    return (f"--- prompt ---\n{head}\n--- reply "
            f"({turns} turns, {cost_s}) ---\n{text}\n")


# ---------------------------------------------------------------------------
# Claude Code
# ---------------------------------------------------------------------------

class ClaudeCodeLLM(AgentBackend):
    """``claude -p``: prompt in argv, one JSON envelope back.

    The allow-list is the strong form of the seal here: ``--strict-mcp-config``
    drops the operator's own servers and ``--allowedTools`` names every method
    this stage may call, so the turn is confined twice over.
    """

    name = "claude"

    def _argv(self, message, tools, route, scratch):
        cmd = [CLAUDE_BIN, "-p", message,
               "--output-format", "json",
               "--permission-mode", AGENT_PERMISSION_MODE,
               "--max-turns", str(self.max_turns)]
        if route.model:
            cmd += ["--model", route.model]
        if route.effort:
            cmd += ["--effort", route.effort]
        if self.system_message:
            cmd += ["--append-system-prompt", self.system_message]
        if tools:
            cfg_path = os.path.join(scratch, "mcp.json")
            with open(cfg_path, "w") as f:
                json.dump(mcp_config(tools), f)
            cmd += ["--mcp-config", cfg_path,
                    # Only the servers named here, and only their tools: no
                    # inherited project MCP config, no file system, no shell.
                    "--strict-mcp-config",
                    "--allowedTools", ",".join(tool_names(tools))]
        return cmd, None

    @staticmethod
    def _failure_detail(proc) -> str:
        """What actually went wrong, from a turn that exited non-zero.

        The CLI frequently exits non-zero *and* prints a complete result
        envelope, so the useful diagnosis -- a usage limit, a denied tool, an
        API error -- is a field inside that JSON. Keeping only the tail of
        stdout truncated it mid-key and produced log lines that began
        `on_denials":[{"tool_name":"Bash"`, which says what the agent tried
        but not why the turn died. It also hid the text the throttle check
        matches on, so a rate-limited turn could read as a hard failure and
        skip its retry.

        So: parse the envelope when it is there and report its own words;
        otherwise keep the head as well as the tail."""
        out = (proc.stdout or "").strip()
        try:
            payload = json.loads(out)
        except (json.JSONDecodeError, ValueError):
            payload = None
        if isinstance(payload, dict):
            bits = []
            for key in ("subtype", "stop_reason"):
                if payload.get(key):
                    bits.append(f"{key}={payload[key]}")
            denied = payload.get("permission_denials") or []
            if denied:
                names = sorted({str(d.get("tool_name", "?")) for d in denied
                                if isinstance(d, dict)})
                bits.append(f"denied tools: {', '.join(names)} "
                            "(not in this turn's allow-list)")
            text = payload.get("result") or payload.get("error") or ""
            if isinstance(text, str) and text.strip():
                bits.append(text.strip()[:600])
            if bits:
                return " | ".join(bits)
        err = (proc.stderr or "").strip()
        if err:
            return err[-2000:]
        return (out[:900] + " ... " + out[-900:]) if len(out) > 1900 else out

    def _parse(self, proc, route, scratch, message):
        if proc.returncode != 0:
            return QueryResult(returncode=proc.returncode, success=False,
                               stderr=self._failure_detail(proc))
        try:
            payload = json.loads(proc.stdout)
        except json.JSONDecodeError:
            # A reply that is not JSON means the CLI printed something other
            # than a result envelope; keep the text so the log shows what.
            return QueryResult(result=proc.stdout, returncode=0,
                               success=bool(proc.stdout.strip()),
                               stderr=proc.stderr[-2000:])
        text = payload.get("result") or ""
        turns = int(payload.get("num_turns") or 0)
        cost = float(payload.get("total_cost_usd") or 0.0)
        return QueryResult(
            result=text,
            returncode=0,
            success=not payload.get("is_error", False),
            stderr=payload.get("error", "") or "",
            stream_result=_transcript(message, text, turns, cost),
            cost_usd=cost,
            turns=turns)


# ---------------------------------------------------------------------------
# Codex
# ---------------------------------------------------------------------------

class CodexLLM(AgentBackend):
    """``codex exec``: prompt on stdin, JSONL events out, final message to a file.

    Four things differ from Claude Code and each one is handled here rather
    than leaking into the loop:

    1. **The prompt goes on stdin** (the trailing ``-``), not in argv.
    2. **There is no system-prompt flag**, so the charter is folded into the
       message under a header, the way CHIA's codex backend does it.
    3. **MCP servers are config overrides**, ``-c mcp_servers.<n>.url=<url>``,
       and the per-method allow-list is ``mcp_servers.<n>.enabled_tools``.
       Together with ``--ignore-user-config`` that reproduces both halves of
       what ``--strict-mcp-config`` plus ``--allowedTools`` does on the Claude
       side: the operator's own servers are not loaded, and a method RED did
       not list is not callable. Measured, not assumed: with the list in place
       a withheld tool is unreachable, and without it the same prompt reads it
       straight out.
    4. **Effort is a config key**, ``-c model_reasoning_effort=...``.

    **On approvals.** Codex refuses an MCP call under
    ``--ask-for-approval never`` ("MCP tool call requires approval, but
    approval policy is never") and has no per-server trust setting, so a
    headless turn that must use its tools has to pass
    ``--dangerously-bypass-approvals-and-sandbox``. CHIA's backend reaches the
    same conclusion and defaults to it. Two things are worth being plain
    about, because the flag's name is doing a lot of work:

    * What RED relies on is unaffected. The tool surface is still sealed, by
      ``enabled_tools`` and by the server having no method for what the agent
      must not do.
    * What is genuinely weaker than the Claude backend: that flag also turns
      off Codex's shell sandbox, and RED cannot turn it back on
      independently. A Claude turn is given an allow-list containing only MCP
      methods, so it has no shell at all; a Codex turn could in principle run
      a command. RED gives the turn a throwaway working directory to limit
      what a stray command would find, but a Codex-routed stage is a larger
      trust surface than a Claude-routed one. Route accordingly.
    """

    name = "codex"

    def _argv(self, message, tools, route, scratch):
        last = os.path.join(scratch, "last-message.txt")
        workdir = os.path.join(scratch, "cwd")
        os.makedirs(workdir, exist_ok=True)
        cmd = [CODEX_BIN, "exec", "--json", "--color", "never",
               # RED runs from a checkout but points at arbitrary project
               # directories; neither needs to be a git repo for a turn.
               "--skip-git-repo-check",
               # No session files: RED's audit trail is its own artifacts, and
               # a resumable session on disk is state the loop does not model.
               "--ephemeral",
               # Half the seal: the operator's own MCP servers are not loaded.
               "--ignore-user-config",
               # Required for the turn to call its tools at all; see the class
               # docstring for what this does and does not cost.
               "--dangerously-bypass-approvals-and-sandbox",
               # An empty throwaway root, so the sandbox the line above
               # disabled is not pointed at the repository.
               "--cd", workdir,
               "-o", last]
        if route.model:
            cmd += ["--model", route.model]
        if route.effort:
            cmd += ["-c", f"model_reasoning_effort={json.dumps(route.effort)}"]
        for t in tools:
            key = t.name if _bare_key(t.name) else json.dumps(t.name)
            cmd += ["-c", f"mcp_servers.{key}.url={json.dumps(t.url)}"]
            # The other half of the seal: only these methods exist for this
            # turn. A server RED hands over still carries only the methods its
            # agent may use, so this is the second strap, not the only one.
            allowed = server_tool_names(t)
            if allowed:
                cmd += ["-c", f"mcp_servers.{key}.enabled_tools="
                              f"{json.dumps(allowed)}"]
        cmd.append("-")
        return cmd, self._with_system(message)

    def _with_system(self, message: str) -> str:
        if not self.system_message:
            return message
        return (f"[System Instructions]\n{self.system_message}\n\n"
                f"[User Request]\n{message}")

    def _parse(self, proc, route, scratch, message):
        last = os.path.join(scratch, "last-message.txt")
        text = ""
        try:
            with open(last) as f:
                text = f.read().strip()
        except OSError:
            pass
        stream, turns, cost, final = _codex_events(proc.stdout)
        text = text or final
        if proc.returncode != 0:
            return QueryResult(
                result=text, returncode=proc.returncode, success=False,
                stderr=(proc.stderr or stream or proc.stdout)[-2000:],
                stream_result=stream, turns=turns, cost_usd=cost)
        return QueryResult(
            result=text,
            returncode=0,
            # Codex can exit 0 having said nothing useful; the loop's callers
            # all check for an empty reply, but say so here too.
            success=bool(text.strip()),
            stderr=(proc.stderr or "")[-2000:],
            stream_result=_transcript(message, text, turns, cost) + stream,
            cost_usd=cost,
            turns=turns)


def _bare_key(name: str) -> bool:
    """Can *name* go into a TOML dotted path unquoted?"""
    return bool(name) and all(c.isalnum() or c in "_-" for c in name)


def _codex_events(stdout: str) -> tuple[str, int, float, str]:
    """Fold Codex's JSONL event stream into (transcript, turns, cost, final).

    Written against the event shapes codex 0.163 emits (``item.started`` /
    ``item.completed`` carrying an ``item`` with a ``type``), but tolerant:
    any line that will not parse is kept verbatim rather than dropped, because
    a transcript with a hole in it is worse than a noisy one.
    """
    parts: list[str] = []
    turns = 0
    cost = 0.0
    final = ""
    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            parts.append(f"[unparsed] {line[:200]}\n")
            continue
        item = event.get("item") if isinstance(event.get("item"), dict) else event
        itype = str(item.get("type") or event.get("type") or "").lower()
        if itype == "mcp_tool_call" and str(item.get("status")) != "in_progress":
            parts.append(f"[tool] {item.get('server')}.{item.get('tool')}\n")
            turns += 1
        elif itype in ("agent_message", "assistant_message"):
            text = _codex_text(item)
            if text:
                final = text
                parts.append(f"[reply] {text[:2000]}\n")
        elif itype == "error":
            parts.append(f"[error] {_codex_text(item) or line[:300]}\n")
        usage = event.get("usage") or item.get("usage")
        if isinstance(usage, dict):
            for key in ("total_cost_usd", "cost_usd"):
                if isinstance(usage.get(key), (int, float)):
                    cost += float(usage[key])
    return "".join(parts), turns, cost, final


def _codex_text(value) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "\n".join(filter(None, (_codex_text(v) for v in value)))
    if isinstance(value, dict):
        for key in ("text", "content", "message", "output", "result"):
            got = _codex_text(value.get(key))
            if got:
                return got
    return ""


# ---------------------------------------------------------------------------
# routing one loop across several backends
# ---------------------------------------------------------------------------

BACKEND_CLASSES = {
    ClaudeCodeLLM.name: ClaudeCodeLLM,
    CodexLLM.name: CodexLLM,
}


class Router:
    """What the loop holds: one object that sends each stage to its backend.

    The loop sets ``system_message`` between stages and calls
    ``prompt(msg, tools, role=...)``. Which CLI, model and effort serve that
    role is the routing file's business, resolved here. Backends are built on
    first use, so a config that never mentions Codex never looks for it.
    """

    def __init__(self, models: ModelConfig,
                 timeout_seconds: int = AGENT_TIMEOUT_SECONDS,
                 max_turns: int = AGENT_MAX_TURNS):
        self.models = models
        self.timeout_seconds = timeout_seconds
        self.max_turns = max_turns
        self.system_message = ""
        self._built: dict[str, AgentBackend] = {}

    # Kept so existing callers that read .model/.effort still work; they
    # describe the fallback, which is what those names meant before routing.
    @property
    def model(self) -> str:
        return self.models.fallback.model

    @property
    def effort(self) -> str:
        return self.models.fallback.effort

    def routing_for(self, role: str) -> Routing:
        return self.models.for_role(role)

    def backend_for(self, role: str) -> AgentBackend:
        route = self.routing_for(role)
        be = self._built.get(route.backend)
        if be is None:
            cls = BACKEND_CLASSES[route.backend]
            be = cls(model=route.model, effort=route.effort,
                     timeout_seconds=self.timeout_seconds,
                     max_turns=self.max_turns)
            self._built[route.backend] = be
        return be

    def prompt(self, message: str, tools=(), role: str = "") -> QueryResult:
        route = self.routing_for(role)
        be = self.backend_for(role)
        be.system_message = self.system_message
        return be.run(message, tools, route)


def make_router(models_config: str = "", model: str = AGENT_MODEL,
                effort: str = AGENT_EFFORT, backend: str = DEFAULT_BACKEND,
                **kwargs) -> Router:
    """Build the loop's backend from a routing file plus CLI fallbacks."""
    return Router(load_models(models_config, model=model, effort=effort,
                              backend=backend), **kwargs)
