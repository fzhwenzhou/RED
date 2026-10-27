"""Which model runs which stage.

RED is a dozen agent turns of very unequal weight. The A2 designer writes the
deliverable; the security sub-agent suggests operand values that a sanitizer
then accepts or rejects. Running both on the same model is a choice, and until
now it was a choice nothing in the repository let you make differently.

**The rule this module exists to express:** a stage whose output is checked by
a program can run on a cheaper model than a stage whose output *is* the
result. A weaker answer from a checked stage costs a round; a weaker answer
from an unchecked stage ships. That is the same principle the gates are built
on, applied to the budget.

Nothing here names a model. The mapping lives in a config file (``models.toml``
by default, or ``RED_MODELS_CONFIG`` / ``--models-config``), so changing the
routing never means editing code, and a run that was produced under one
routing can be compared against another by reading the file beside it.

File format, all fields optional::

    [default]                       # every role not named below
    backend = "claude"              # which CLI runs the turn
    model   = "<strong-model-id>"
    effort  = "high"

    [roles.review]                  # the four review sub-agents
    model   = "<cheaper-model-id>"
    effort  = "medium"

    [roles.iss]                     # a different vendor's agent entirely
    backend = "codex"
    model   = "<their-model-id>"

``backend`` names one of :data:`BACKENDS`. Routing a stage to a different
vendor is the same mechanism as routing it to a different model, which is the
point: Gate 2's independence argument gets stronger the less the two sides
share, and "a different company's agent wrote it" is as independent as RED can
currently make it.

Resolution order for one role, first hit wins:

1. ``[roles.<role>]`` in the config file
2. ``[default]`` in the config file
3. what the caller passed (``--model`` / ``--effort``, or ``RED_AGENT_*``)
4. nothing, meaning whatever the Claude Code CLI is configured for

An unknown role name in the file is a typo that would otherwise route silently
to the default, so loading reports it rather than ignoring it.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass

from red.constants import MODELS_CONFIG

# Every agent stage in the loop, with what checks its output. Keep this in
# step with the ``role=`` arguments in red/loop.py; tests/test_env.py fails if
# they drift apart, because a role the loop passes but this does not list
# would route to the default without anyone noticing.
ROLES: dict[str, str] = {
    "workload":        "AW writes a harness; Gate 0 runs it and decides",
    "mine":            "A1 ranks hot loops over a mechanical profile",
    "core":            "A0 reads the target core's RTL into a typed profile",
    "design":          "A2 synthesis — writes the deliverable",
    "redesign":        "Gate 3 handed back the cost arithmetic",
    "revise":          "the designer answering blocking review findings",
    "review":          "the four review sub-agents, concurrent",
    "security":        "proposes operand probes; only executed ones count",
    "security_repair": "rewriting a model a confirmed exploit broke",
    "iss":             "the independent Spike implementation Gate 2 checks",
    "repair":          "fixing a C model that failed to compile or run",
    # Not part of a design run: red.rsi dispatches this after one finishes,
    # to propose changes to RED's own prompts and loop knobs. It is routed
    # here so an improvement turn can be priced and compared like any other.
    "improve":         "red.rsi reflecting on a finished run",
}


# Agent CLIs RED knows how to drive. The strings are what a config file
# writes; red.backend maps them to classes. Listed here rather than there so
# that loading a config can reject a typo without importing a subprocess
# driver.
BACKENDS: tuple[str, ...] = ("claude", "codex")
DEFAULT_BACKEND = "claude"


@dataclass(frozen=True)
class Routing:
    """What one role resolves to. Empty strings mean "the CLI's own default"."""

    backend: str = DEFAULT_BACKEND
    model: str = ""
    effort: str = ""

    def label(self) -> str:
        inner = self.model or ""
        if self.effort:
            inner = f"{inner} (effort={self.effort})" if inner else f"effort={self.effort}"
        return f"{self.backend}: {inner}" if inner else f"{self.backend}: default"


class ModelConfig:
    """Resolved role -> Routing, with the fallbacks already applied."""

    def __init__(self, table: dict[str, Routing], fallback: Routing,
                 source: str = "", unknown: tuple[str, ...] = ()):
        self._table = table
        self.fallback = fallback
        self.source = source            # path the routing came from, or ""
        self.unknown = unknown          # role names in the file we do not have

    def for_role(self, role: str) -> Routing:
        return self._table.get(role, self.fallback)

    @property
    def backends(self) -> tuple[str, ...]:
        """Every backend this routing will actually start, fallback included."""
        names = {self.fallback.backend} | {r.backend for r in self._table.values()}
        return tuple(sorted(names))

    @property
    def routed(self) -> dict[str, Routing]:
        """Only the roles the config file named, for printing the difference."""
        return dict(self._table)

    def render(self) -> str:
        """One block for the run log, so the routing is never a mystery."""
        if not self.source:
            return f"[red] models: {self.fallback.label()} for every stage"
        lines = [f"[red] models: {self.source}",
                 f"[red]   (default)        {self.fallback.label()}"]
        for role in ROLES:
            r = self._table.get(role)
            if r is not None and r != self.fallback:
                lines.append(f"[red]   {role:<16} {r.label()}")
        for name in self.unknown:
            lines.append(f"[red]   WARNING: {name!r} is not a RED role, ignored")
        return "\n".join(lines)


def _routing(section: dict, fallback: Routing) -> Routing:
    backend = str(section.get("backend", fallback.backend) or DEFAULT_BACKEND)
    if backend not in BACKENDS:
        raise ValueError(
            f"unknown backend {backend!r}; RED drives {', '.join(BACKENDS)}")
    return Routing(
        backend=backend,
        model=str(section.get("model", fallback.model) or ""),
        effort=str(section.get("effort", fallback.effort) or ""),
    )


def load(path: str = "", model: str = "", effort: str = "",
         backend: str = DEFAULT_BACKEND) -> ModelConfig:
    """Read the routing file, falling back to *model* / *effort* for the rest.

    A missing file is not an error: it means every stage runs on what the
    caller asked for, which is what RED did before this existed. An
    unparseable file *is* an error, because silently reverting to one model
    for everything would make a run's record wrong.
    """
    caller = Routing(backend=backend or DEFAULT_BACKEND,
                     model=model or "", effort=effort or "")
    path = path if path is not None else ""
    if path == "":
        path = MODELS_CONFIG
    if not path or not os.path.isfile(path):
        return ModelConfig({}, caller, "")

    with open(path, "rb") as f:
        doc = tomllib.load(f)

    fallback = _routing(doc.get("default", {}) or {}, caller)
    roles_doc = doc.get("roles", {}) or {}
    table = {name: _routing(section or {}, fallback)
             for name, section in roles_doc.items() if name in ROLES}
    unknown = tuple(sorted(n for n in roles_doc if n not in ROLES))
    return ModelConfig(table, fallback, os.path.relpath(path), unknown)
