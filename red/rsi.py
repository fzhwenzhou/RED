"""Recursive self-improvement for RED.

Recursive self-improvement, in the sense this module means it, is narrower
than the term usually is. It is not an intelligence explosion. It is the
property Phil Schmid's framing picks out: a system makes a *persistent* change
to itself that improves both its future performance and its ability to make
later improvements. The distinction that matters is against plain iteration —
an agent that edits a file, runs a test and fixes the bug has not
self-improved, because the next task starts with exactly the same agent. RED
improves when the *next run* is better because of what this run learned, and
when that learning is written into the repository rather than into a context
window.

RED already had the weak form of this. The Neo4j knowledge graph carries what
earlier runs designed, what the reviewers objected to and what the gates
measured, and feeding it back took convergence from 1/3 to 4/4. But the graph
only informs the agents; it never changes them. The prompts, the loop and the
code are the same on run 100 as on run 1. This module is the part that
changes them.

What the improver may change
----------------------------

The literature's standing objection to self-improving systems is reward
hacking: a system scored on a proxy optimises the proxy. RED is unusually
exposed to this, because everything that scores a run is code in this same
repository. An improver that could edit ``red/cost.py`` would discover very
quickly that the cheapest way to raise the predicted speedup is to change how
speedup is predicted, and every downstream number would agree with it.

So the rule is structural, not advisory:

**The improver may not modify anything that judges a run.**

Three classes of file, decided by :func:`classify` and enforced by
:func:`validate_proposal`:

``mutable``
    Changed automatically once the test suite passes and the metrics do not
    regress. The prompts, and a small table of bounded loop knobs. These are
    how the agents *think*, and they are not how the agents are *scored*.

``frozen``
    Never changed by this module, through any path. The gates
    (``red/nodes.py``), the cost model (``red/cost.py``), the security checks
    (``red/security.py``), the structural check (``red/structure.py``), the
    schema and its validators (``red/state_def.py``), the differential test
    (``red/iss.py``), the review machinery (``red/review.py``), the thresholds
    (``red/constants.py``), the tests, and this file. A proposal naming one is
    rejected outright, and :func:`frozen_fingerprint` is recorded in every
    run's metrics so that tampering is detectable after the fact and so that
    two runs are only ever compared when they were judged by the same code.

``proposal-only``
    Everything else, which is mostly ``red/loop.py``. The improver may write a
    patch and argue for it; a human applies it. The loop is where a subtle
    change does the most damage and where a reviewer is cheapest.

That split is also why the acceptance test is honest. A proposal is accepted
only if the full suite still passes *and* the frozen fingerprint is unchanged
afterwards — so the thing that said "this is better" is provably the same
thing that said the previous run was worse.

What "better" means
-------------------

:class:`RunMetrics` records what a run cost and what it produced. Quality is
lexicographic and comes first: did it converge, how many gates passed, how
much of the application did it cover, what speedup did it predict, how many
blocking findings survived. Efficiency is second: wall clock, agent turns, and
the rounds spent inside the loop (review, redesign, repair, rejected drafts).

Efficiency alone is trivially gamed by giving up sooner, which is why quality
is compared first and a regression in it rejects the proposal whatever
happened to the clock.
"""

from __future__ import annotations

import dataclasses
import datetime
import hashlib
import os
import re
import subprocess
import threading
from dataclasses import dataclass, field

from . import state_def


# ---------------------------------------------------------------------------
# What may change
# ---------------------------------------------------------------------------

# Changed automatically, subject to the test suite and a metrics comparison.
MUTABLE: tuple[str, ...] = (
    "red/prompts/",
    "rsi/tunables.toml",
)

# Never changed by this module. These are the things that decide whether a run
# was any good; an improver that can edit them is not measuring itself.
FROZEN: tuple[str, ...] = (
    "red/nodes.py",        # the gates
    "red/cost.py",         # Gate 3's pricing
    "red/security.py",     # Gate 4's checks
    "red/structure.py",    # the structural check
    "red/state_def.py",    # the schema and its validators
    "red/iss.py",          # Gate 2's differential test
    "red/review.py",       # how findings are parsed, merged and counted
    "red/constants.py",    # every threshold
    "red/rsi.py",          # this file: the improver cannot rewrite its own guard
    "tests/",
)

MUTABLE_CLASS = "mutable"
FROZEN_CLASS = "frozen"
PROPOSAL_CLASS = "proposal-only"

RSI_DIR = "rsi"
PROPOSALS_DIR = os.path.join(RSI_DIR, "proposals")
TUNABLES_FILE = os.path.join(RSI_DIR, "tunables.toml")


def _norm(path: str) -> str:
    return path.replace(os.sep, "/").lstrip("./")


def classify(path: str) -> str:
    """Which class a repo-relative *path* falls in.

    Frozen wins over mutable: if a path somehow matched both, the safe answer
    is the one that refuses."""
    p = _norm(path)
    for f in FROZEN:
        if p == f or (f.endswith("/") and p.startswith(f)):
            return FROZEN_CLASS
    for m in MUTABLE:
        if p == m or (m.endswith("/") and p.startswith(m)):
            return MUTABLE_CLASS
    return PROPOSAL_CLASS


def _files_under(root: str, spec: str) -> list[str]:
    base = os.path.join(root, spec)
    if spec.endswith("/"):
        out = []
        for dirpath, _dirs, names in os.walk(base):
            for n in sorted(names):
                if n.endswith((".pyc",)) or "__pycache__" in dirpath:
                    continue
                out.append(os.path.relpath(os.path.join(dirpath, n), root))
        return sorted(_norm(p) for p in out)
    return [_norm(spec)] if os.path.isfile(base) else []


def frozen_fingerprint(root: str) -> str:
    """A hash over every frozen file.

    Recorded in each run's metrics. Two runs whose fingerprints differ were
    judged by different code and their numbers are not comparable; a proposal
    that changes it has escaped the guard and is rejected."""
    h = hashlib.sha256()
    for spec in FROZEN:
        for rel in _files_under(root, spec):
            h.update(rel.encode())
            with open(os.path.join(root, rel), "rb") as f:
                h.update(hashlib.sha256(f.read()).digest())
    return h.hexdigest()[:16]


# ---------------------------------------------------------------------------
# Bounded loop knobs
# ---------------------------------------------------------------------------

# The part of "the loop" the improver may retune on its own. Each is a cap on
# effort, so moving one trades compute against quality -- and quality is
# measured, so a knob turned too far shows up as a regression and the proposal
# is rejected. Nothing that defines the *bar* is here: coverage target, speedup
# target, gate tolerances and vector counts all live in the frozen constants.
TUNABLES: dict[str, tuple[int, int, str]] = {
    # name: (min, max, what it does)
    "REVIEW_MAX_ROUNDS": (1, 8, "review/revise rounds before the loop ships "
                                "the best-scoring spec it has"),
    "CRITIC_MAX_ROUNDS": (1, 6, "A2 self-critique rounds before the spec goes "
                                "to the reviewers"),
    "REPAIR_MAX_ROUNDS": (1, 6, "agentic repair attempts after a link-1 failure"),
    "PERF_MAX_ROUNDS": (1, 8, "Gate 3 redesign rounds before the bar relaxes"),
}


def load_tunables(root: str) -> dict[str, int]:
    """Read ``rsi/tunables.toml``, clamped to the declared bounds.

    Out-of-range and unknown keys are dropped rather than raising: this is
    read at import time by :mod:`red.constants`, and a malformed file written
    by a bad proposal must not make the whole tool unimportable."""
    path = os.path.join(root, TUNABLES_FILE)
    out: dict[str, int] = {}
    if not os.path.isfile(path):
        return out
    try:
        import tomllib
        with open(path, "rb") as f:
            raw = tomllib.load(f)
    except Exception:                                        # noqa: BLE001
        return out
    for key, val in (raw.get("loop") or {}).items():
        if key not in TUNABLES or not isinstance(val, int) or isinstance(val, bool):
            continue
        lo, hi = TUNABLES[key][0], TUNABLES[key][1]
        if lo <= val <= hi:
            out[key] = val
    return out


def render_tunables(values: dict[str, int]) -> str:
    lines = ["# Loop knobs the self-improver may retune (red/rsi.py).",
             "# Each is clamped to its declared bounds on read; anything that",
             "# defines the bar a run is judged against is frozen, not here.",
             "", "[loop]"]
    for key in sorted(values):
        lo, hi, what = TUNABLES[key]
        lines.append(f"# {what} ({lo}..{hi})")
        lines.append(f"{key} = {values[key]}")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Telemetry
# ---------------------------------------------------------------------------

@dataclass
class Turn:
    """One agent turn, as the improver sees it."""

    tag: str = ""
    role: str = ""
    model: str = ""
    seconds: float = 0.0
    ok: bool = True
    reply_chars: int = 0


_TURNS: list[Turn] = []
_TURNS_LOCK = threading.Lock()


def record_turn(tag: str, role: str, model: str, seconds: float,
                ok: bool, reply_chars: int) -> None:
    """Called from the loop's one dispatch chokepoint, for every turn."""
    with _TURNS_LOCK:
        _TURNS.append(Turn(tag=tag, role=role, model=model,
                           seconds=round(seconds, 2), ok=ok,
                           reply_chars=reply_chars))


def reset_turns() -> None:
    with _TURNS_LOCK:
        _TURNS.clear()


def turns() -> list[Turn]:
    with _TURNS_LOCK:
        return list(_TURNS)


@dataclass
class RunMetrics:
    """What one run cost and what it produced.

    Written to ``metrics.yaml`` in the run directory. This is the improver's
    evidence and the acceptance test's measuring stick, so everything in it is
    recorded by the loop from what happened, never claimed by an agent."""

    workload: str = ""
    run_id: str = ""
    started: str = ""
    model_label: str = ""
    frozen_fingerprint: str = ""
    # --- quality, compared first ----------------------------------------
    converged: bool = False
    gates_passed: int = 0
    gates_total: int = 0
    coverage: float = 0.0
    predicted_speedup: float = 0.0
    speedup_bar: float = 0.0
    blocking_findings: int = 0
    structure_blocking: int = 0
    instructions: int = 0
    # --- efficiency, compared second ------------------------------------
    wallclock_seconds: float = 0.0
    turns: int = 0
    failed_turns: int = 0
    turn_seconds: float = 0.0
    turns_by_role: dict = field(default_factory=dict)
    seconds_by_role: dict = field(default_factory=dict)
    review_rounds: int = 0
    redesign_rounds: int = 0
    repair_rounds: int = 0
    critic_rounds: int = 0
    rejected_drafts: int = 0

    def quality(self) -> tuple:
        """Compared lexicographically, higher is better.

        Convergence first, because a faster run that does not produce a spec
        has not improved anything. Blocking findings are negated so that fewer
        is better in the same ordering."""
        return (int(self.converged), self.gates_passed,
                -self.blocking_findings, -self.structure_blocking,
                round(self.coverage, 4), round(self.predicted_speedup, 3))

    def effort(self) -> tuple:
        """Compared lexicographically, *lower* is better."""
        return (round(self.turn_seconds, 1), self.turns,
                self.review_rounds + self.redesign_rounds + self.repair_rounds,
                self.rejected_drafts)

    def summary(self) -> str:
        return (f"{self.workload}/{self.run_id}: "
                f"{'converged' if self.converged else 'did not converge'}, "
                f"{self.gates_passed}/{self.gates_total} gates, "
                f"{self.predicted_speedup:.2f}x over {self.coverage:.1%}, "
                f"{self.blocking_findings} blocking — "
                f"{self.turns} turns / {self.turn_seconds / 60:.0f} min agent, "
                f"{self.wallclock_seconds / 60:.0f} min wall")


def collect_turn_metrics(m: RunMetrics) -> RunMetrics:
    """Fold the recorded turns into *m*."""
    ts = turns()
    m.turns = len(ts)
    m.failed_turns = sum(1 for t in ts if not t.ok)
    m.turn_seconds = round(sum(t.seconds for t in ts), 2)
    by_n: dict[str, int] = {}
    by_s: dict[str, float] = {}
    for t in ts:
        key = t.role or t.tag.split()[0] or "other"
        by_n[key] = by_n.get(key, 0) + 1
        by_s[key] = round(by_s.get(key, 0.0) + t.seconds, 1)
    m.turns_by_role, m.seconds_by_role = by_n, by_s
    return m


def compare(before: RunMetrics, after: RunMetrics) -> tuple[bool, str]:
    """Did *after* improve on *before*?

    Quality must not regress; given that, less effort is an improvement. The
    two runs must have been judged by the same frozen code, or the comparison
    means nothing and is refused."""
    if before.frozen_fingerprint and after.frozen_fingerprint and \
            before.frozen_fingerprint != after.frozen_fingerprint:
        return False, ("the two runs were judged by different code "
                       f"({before.frozen_fingerprint} vs "
                       f"{after.frozen_fingerprint}); the comparison is void")
    qb, qa = before.quality(), after.quality()
    if qa < qb:
        return False, f"quality regressed: {qb} -> {qa}"
    eb, ea = before.effort(), after.effort()
    if qa > qb:
        return True, f"quality improved: {qb} -> {qa}"
    if ea < eb:
        return True, f"same quality, less effort: {eb} -> {ea}"
    return False, f"no improvement: quality {qa}, effort {eb} -> {ea}"


# ---------------------------------------------------------------------------
# Proposals
# ---------------------------------------------------------------------------

@dataclass
class Proposal:
    """One persistent change the improver wants to make to RED itself."""

    id: str = ""
    created: str = ""
    from_run: str = ""               # the run directory it was argued from
    target: str = ""                 # repo-relative path
    kind: str = ""                   # prompt | tunable | code
    rationale: str = ""              # what in the run says this is worth doing
    expected_effect: str = ""        # what should change in the next run
    metric: str = ""                 # the RunMetrics field it claims to move
    evidence: str = ""               # the numbers it is arguing from
    old_text: str = ""               # the file as it was (exact)
    new_text: str = ""               # the file as proposed
    status: str = "proposed"         # proposed | applied | rejected | reverted
    detail: str = ""                 # why rejected, or the test result
    fingerprint: str = ""            # frozen fingerprint when it was written

    def klass(self) -> str:
        return classify(self.target)


class ProposalRejected(Exception):
    """A proposal that must not be applied, with the reason."""


def validate_proposal(p: Proposal, root: str) -> None:
    """Everything that must hold before a proposal is even considered.

    Raises :class:`ProposalRejected`. The checks are ordered so the most
    serious objection is the one reported: a proposal against a frozen file is
    an attempt to move the goalposts, and that is worth saying plainly even if
    the proposal is also malformed in three other ways."""
    if not p.target:
        raise ProposalRejected("the proposal names no target file")
    target = _norm(p.target)
    if os.path.isabs(p.target) or ".." in target.split("/"):
        raise ProposalRejected(
            f"target {p.target!r} escapes the repository")

    klass = classify(target)
    if klass == FROZEN_CLASS:
        raise ProposalRejected(
            f"{target} is frozen: it is part of what judges a run, and the "
            "improver does not get to change how it is scored. Propose a "
            "change to how RED designs, not to how RED grades.")
    if klass == PROPOSAL_CLASS:
        raise ProposalRejected(
            f"{target} is proposal-only: it can be written up and reviewed by "
            "a human, but it is not applied automatically.")

    full = os.path.join(root, target)
    if not os.path.isfile(full):
        raise ProposalRejected(f"{target} does not exist")
    if not p.new_text.strip():
        raise ProposalRejected("the proposal has no replacement text")

    current = open(full, encoding="utf-8").read()
    if p.old_text and p.old_text != current:
        raise ProposalRejected(
            f"{target} has changed since the proposal was written; it was "
            "argued against a version that is no longer on disk")
    if p.new_text == current:
        raise ProposalRejected("the proposal changes nothing")

    if target == _norm(TUNABLES_FILE):
        _validate_tunable_text(p.new_text)


def _validate_tunable_text(text: str) -> None:
    """A tunables file is only allowed to set known keys, within bounds."""
    try:
        import tomllib
        raw = tomllib.loads(text)
    except Exception as exc:                                 # noqa: BLE001
        raise ProposalRejected(f"tunables.toml does not parse: {exc}") from exc
    extra = set(raw) - {"loop"}
    if extra:
        raise ProposalRejected(
            f"tunables.toml may only contain a [loop] table; got {sorted(extra)}")
    for key, val in (raw.get("loop") or {}).items():
        if key not in TUNABLES:
            raise ProposalRejected(
                f"{key} is not a tunable. The tunables are: "
                + ", ".join(sorted(TUNABLES)) + ". Everything else that "
                "affects how a run is judged is frozen.")
        lo, hi, _ = TUNABLES[key]
        if not isinstance(val, int) or isinstance(val, bool) or not (lo <= val <= hi):
            raise ProposalRejected(
                f"{key} = {val!r} is outside its bounds [{lo}, {hi}]")


def _snapshot_frozen(root: str) -> dict[str, bytes]:
    out: dict[str, bytes] = {}
    for spec in FROZEN:
        for rel in _files_under(root, spec):
            with open(os.path.join(root, rel), "rb") as f:
                out[rel] = f.read()
    return out


def _restore_frozen(root: str, snap: dict[str, bytes]) -> list[str]:
    """Put the frozen files back, returning the ones that had moved."""
    moved = []
    for rel, body in snap.items():
        full = os.path.join(root, rel)
        try:
            with open(full, "rb") as f:
                if f.read() == body:
                    continue
        except OSError:
            pass
        moved.append(rel)
        with open(full, "wb") as f:
            f.write(body)
    return moved


def apply_proposal(p: Proposal, root: str, test_cmd: list[str] | None = None,
                   timeout: int = 3600) -> tuple[bool, str]:
    """Apply *p*, run the test suite, and keep it only if the suite passes.

    The frozen set is snapshotted first and checked twice: after the file is
    written, and again after the suite has run. The second check is the one
    that matters and the one that is easy to leave out. A test command is
    arbitrary code — it is the suite today, and a proposal cannot change the
    suite, but it can change what the suite imports — so "the tests passed"
    is only evidence if the tests that passed were the frozen ones. A suite
    that still passes is precisely what a successful goalpost-move looks
    like.

    Anything that moved is restored, including frozen files, and the
    proposal is rejected whatever the suite said."""
    try:
        validate_proposal(p, root)
    except ProposalRejected as exc:
        p.status, p.detail = "rejected", str(exc)
        return False, str(exc)

    full = os.path.join(root, _norm(p.target))
    snap = _snapshot_frozen(root)
    before_fp = frozen_fingerprint(root)
    original = open(full, encoding="utf-8").read()
    with open(full, "w", encoding="utf-8") as f:
        f.write(p.new_text)

    def _restore(reason: str) -> tuple[bool, str]:
        with open(full, "w", encoding="utf-8") as fh:
            fh.write(original)
        moved = _restore_frozen(root, snap)
        if moved:
            reason += ("\nrestored frozen file(s) that had been modified: "
                       + ", ".join(moved))
        p.status, p.detail = "rejected", reason
        return False, reason

    def _tampered() -> str:
        now = frozen_fingerprint(root)
        return "" if now == before_fp else (
            f"the frozen fingerprint moved ({before_fp} -> {now}): something "
            "reached a file that decides whether a run is any good")

    bad = _tampered()
    if bad:
        return _restore(bad)

    cmd = test_cmd or ["python3", os.path.join(root, "tests", "test_env.py")]
    try:
        proc = subprocess.run(cmd, cwd=root, capture_output=True, text=True,
                              timeout=timeout)
    except subprocess.TimeoutExpired:
        return _restore(f"the test suite did not finish within {timeout}s")
    except OSError as exc:
        return _restore(f"could not run the test suite: {exc}")

    # Checked again, and before the exit status is even looked at.
    bad = _tampered()
    if bad:
        return _restore(bad)

    if proc.returncode != 0:
        tail = "\n".join((proc.stdout or "").strip().splitlines()[-12:])
        return _restore(f"the test suite failed (exit {proc.returncode}):\n{tail}")

    p.status = "applied"
    p.detail = _tests_line(proc.stdout) or "the test suite passed"
    p.fingerprint = before_fp
    return True, p.detail


def _tests_line(out: str) -> str:
    m = re.search(r"(\d+)/(\d+) checks passed", out or "")
    return f"{m.group(0)}" if m else ""


def revert_proposal(p: Proposal, root: str) -> tuple[bool, str]:
    """Put a previously applied proposal's file back as it was."""
    if p.status != "applied":
        return False, f"proposal {p.id} is {p.status}, not applied"
    full = os.path.join(root, _norm(p.target))
    if not p.old_text:
        return False, (f"proposal {p.id} did not record the previous text, "
                       "so it cannot be reverted automatically")
    with open(full, "w", encoding="utf-8") as f:
        f.write(p.old_text)
    p.status, p.detail = "reverted", "reverted on request"
    return True, f"{p.target} restored"


# ---------------------------------------------------------------------------
# The proposal store
# ---------------------------------------------------------------------------

def store_dir(root: str) -> str:
    d = os.path.join(root, PROPOSALS_DIR)
    os.makedirs(d, exist_ok=True)
    return d


def next_id(root: str) -> str:
    used = [int(n[:4]) for n in os.listdir(store_dir(root))
            if n[:4].isdigit() and n.endswith(".yaml")]
    return f"{(max(used) + 1) if used else 1:04d}"


def save(p: Proposal, root: str) -> str:
    if not p.id:
        p.id = next_id(root)
    if not p.created:
        p.created = datetime.datetime.now().astimezone().isoformat(timespec="seconds")
    if not p.fingerprint:
        p.fingerprint = frozen_fingerprint(root)
    path = os.path.join(store_dir(root), f"{p.id}.yaml")
    with open(path, "w", encoding="utf-8") as f:
        f.write(state_def.to_yaml(p))
    return path


def load(pid: str, root: str) -> Proposal:
    path = os.path.join(store_dir(root), f"{pid}.yaml")
    if not os.path.isfile(path):
        raise FileNotFoundError(f"no proposal {pid}")
    return state_def.from_yaml(open(path, encoding="utf-8").read(), Proposal)


def history(root: str) -> list[Proposal]:
    out = []
    for name in sorted(os.listdir(store_dir(root))):
        if not name.endswith(".yaml"):
            continue
        try:
            out.append(state_def.from_yaml(
                open(os.path.join(store_dir(root), name), encoding="utf-8").read(),
                Proposal))
        except (TypeError, ValueError):
            continue
    return out


# ---------------------------------------------------------------------------
# The improver turn
# ---------------------------------------------------------------------------

MAX_PROPOSALS = 4            # per improvement turn; keeps a runaway reply bounded
_PROMPT = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       "prompts", "improve.md")


def mutable_files(root: str) -> list[str]:
    out: list[str] = []
    for spec in MUTABLE:
        out += _files_under(root, spec)
    return sorted(out)


def render_metrics(m: RunMetrics) -> str:
    """The run's numbers, as the improver reads them."""
    lines = [f"- result: {m.summary()}",
             f"- gates: {m.gates_passed}/{m.gates_total} passed",
             f"- coverage: {m.coverage:.2%}   predicted speedup: "
             f"{m.predicted_speedup:.2f}x (bar {m.speedup_bar:.2f}x)",
             f"- review: {m.blocking_findings} blocking, "
             f"{m.structure_blocking} of them structural",
             f"- instructions shipped: {m.instructions}",
             f"- wall clock: {m.wallclock_seconds / 60:.1f} min; "
             f"agent time: {m.turn_seconds / 60:.1f} min over {m.turns} turns "
             f"({m.failed_turns} failed)",
             f"- rounds: review {m.review_rounds}, redesign {m.redesign_rounds}, "
             f"repair {m.repair_rounds}, self-critique {m.critic_rounds}, "
             f"rejected drafts {m.rejected_drafts}"]
    if m.seconds_by_role:
        worst = sorted(m.seconds_by_role.items(), key=lambda kv: -kv[1])
        lines.append("- agent time by stage (minutes): " + ", ".join(
            f"{k} {v / 60:.1f} ({m.turns_by_role.get(k, 0)} turns)"
            for k, v in worst))
    return "\n".join(lines)


def render_history(past: list[Proposal]) -> str:
    """What has already been tried.

    This is the part that makes the improvement recursive rather than
    repetitive: without it the improver re-proposes the same rejected idea
    every run, and nothing accumulates."""
    if not past:
        return "(nothing has been proposed yet — this is the first round.)"
    lines = []
    for p in past[-24:]:
        lines.append(f"- [{p.id}] {p.status.upper()} {p.target} — "
                     f"{p.rationale[:160]}")
        if p.status == "rejected" and p.detail:
            lines.append(f"      rejected because: {p.detail[:200]}")
    return "\n".join(lines)


def render_findings(run_dir: str) -> str:
    """What the reviewers and the gates actually objected to, last round.

    The metrics say a run did not converge and how long it spent doing so.
    They do not say *why*, and an improver handed only the totals can locate
    a bottleneck but not diagnose it -- which is exactly what one improver
    said when it declined to propose anything: "that identifies the
    bottleneck, but not its cause ... without those, a prompt change would
    guess at the failure mechanism". It was right, and this is the fix."""
    if not run_dir or not os.path.isdir(run_dir):
        return "(no run directory was given, so the findings are not available.)"
    lines: list[str] = []
    rounds = sorted(f for f in os.listdir(os.path.join(run_dir, "review"))
                    if f.startswith("round") and f.endswith(".yaml")) \
        if os.path.isdir(os.path.join(run_dir, "review")) else []
    if rounds:
        last = os.path.join(run_dir, "review", rounds[-1])
        try:
            rep = state_def.from_yaml(open(last, encoding="utf-8").read(),
                                      state_def.ReviewReport)
            lines.append(f"Review, final round ({rounds[-1]}), "
                         f"{rep.blocking()} blocking of {len(rep.findings)}:")
            for f in rep.findings:
                if f.severity not in ("blocking", "major"):
                    continue
                lines.append(f"- [{f.severity}] {f.reviewer}/"
                             f"{f.instruction or 'spec'}: {f.finding[:400]}")
                if f.fix:
                    lines.append(f"      proposed fix: {f.fix[:250]}")
        except (TypeError, ValueError):
            lines.append(f"(review/{rounds[-1]} did not parse)")
    gdir = os.path.join(run_dir, "gates")
    if os.path.isdir(gdir):
        verdicts = []
        for name in sorted(os.listdir(gdir)):
            if not name.endswith(".yaml"):
                continue
            # chain.yaml holds a *list* of gate results, the rest hold one.
            # Load generically and flatten, rather than assuming a shape and
            # letting an AttributeError escape past the parse guard.
            try:
                raw = state_def.loads(open(os.path.join(gdir, name),
                                           encoding="utf-8").read())
            except ValueError:
                continue
            for item in (raw if isinstance(raw, list) else [raw]):
                if not isinstance(item, dict) or "gate" not in item:
                    continue
                verdicts.append(
                    f"- {item.get('gate')}: "
                    f"{'PASS' if item.get('passed') else 'FAIL'} "
                    f"— {str(item.get('detail') or '')[:220]}")
        if verdicts:
            lines += ["", "Gate verdicts:"] + verdicts
    return "\n".join(lines) or "(no review or gate artifacts in the run.)"


def brief(m: RunMetrics, root: str, past: list[Proposal] | None = None,
          extra: str = "", run_dir: str = "") -> str:
    """The improver's whole context.

    It is given no tools. The files it may change are small enough to embed,
    and embedding them keeps the turn sealed the same way a reviewer's is."""
    from string import Template
    files = []
    for rel in mutable_files(root):
        body = open(os.path.join(root, rel), encoding="utf-8").read()
        files.append(f"### `{rel}`\n\n```\n{body.rstrip()}\n```")
    tun = "\n".join(f"- `{k}` — {v[2]} (allowed {v[0]}..{v[1]})"
                    for k, v in sorted(TUNABLES.items()))
    return Template(open(_PROMPT, encoding="utf-8").read()).safe_substitute(
        METRICS=render_metrics(m),
        FINDINGS=render_findings(run_dir),
        HISTORY=render_history(past or []),
        MUTABLE="\n".join(f"- `{f}`" for f in mutable_files(root)),
        FROZEN="\n".join(f"- `{f}`" for f in FROZEN),
        TUNABLES=tun,
        MAX_PROPOSALS=str(MAX_PROPOSALS),
        FILES="\n\n".join(files),
        EXTRA=extra)


def parse_reply(text: str, root: str, from_run: str = "") -> list[Proposal]:
    """Pull proposals out of an improver reply.

    A reply that contains nothing usable yields no proposals rather than
    raising: an improvement round that produces nothing is a normal outcome,
    and the run it was reflecting on has already finished."""
    if not text:
        return []
    out: list[Proposal] = []
    for block in re.findall(r"```(?:yaml)?\s*\n(.*?)```", text, re.S):
        try:
            items = state_def.loads(block)
        except ValueError:
            continue
        if isinstance(items, dict):
            items = [items]
        if not isinstance(items, list):
            continue
        for it in items:
            if not isinstance(it, dict) or "target" not in it:
                continue
            target = _norm(str(it.get("target", "")))
            full = os.path.join(root, target)
            old = ""
            if os.path.isfile(full):
                old = open(full, encoding="utf-8").read()
            new = str(it.get("new_text", "") or "")
            # Agents reliably drop the trailing newline a text file ends with;
            # that is not a change anyone meant to propose.
            if old.endswith("\n") and new and not new.endswith("\n"):
                new += "\n"
            out.append(Proposal(
                from_run=from_run, target=target,
                kind=str(it.get("kind", "") or "prompt"),
                rationale=str(it.get("rationale", "") or "")[:2000],
                expected_effect=str(it.get("expected_effect", "") or "")[:1000],
                metric=str(it.get("metric", "") or "")[:80],
                evidence=str(it.get("evidence", "") or "")[:2000],
                old_text=old, new_text=new))
            if len(out) >= MAX_PROPOSALS:
                return out
    return out


def propose(metrics: RunMetrics, root: str, llm, extra: str = "",
            run_dir: str = "") -> list[Proposal]:
    """Run one improvement turn and return the proposals it produced.

    The turn is toolless, like a reviewer's: everything it may change is small
    enough to put in the prompt, and keeping it sealed means an improver
    cannot read the gates it is forbidden to edit."""
    text = brief(metrics, root, past=history(root), extra=extra,
                 run_dir=run_dir)
    res = llm.prompt(text, (), role="improve")
    # Keep the reply whatever happens. "No proposals" is a legitimate and
    # common outcome, but it has three very different causes -- the turn
    # failed, the reply was prose declining to propose anything, or the reply
    # argued for something and the YAML did not parse -- and without the text
    # an operator cannot tell which. Writing it next to the proposals makes
    # the difference one `cat` away.
    last = os.path.join(store_dir(root), "last-reply.md")
    try:
        with open(last, "w", encoding="utf-8") as f:
            f.write(f"# improver reply — {metrics.workload}/{metrics.run_id}\n\n"
                    f"- success: {getattr(res, 'success', False)}\n"
                    f"- model: {getattr(res, 'model', '') or '(unreported)'}\n\n"
                    + ((getattr(res, "result", "") or "") if res else "")
                    + (f"\n\n## stderr\n\n{res.stderr}"
                       if res is not None and getattr(res, "stderr", "") else ""))
    except OSError:
        pass
    if res is None or not getattr(res, "success", False):
        return []
    out = []
    for p in parse_reply(res.result or "", root, from_run=metrics.run_id):
        p.from_run = f"{metrics.workload}/{metrics.run_id}"
        save(p, root)
        out.append(p)
    return out


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _load_metrics(path: str) -> RunMetrics:
    if os.path.isdir(path):
        path = os.path.join(path, "metrics.yaml")
    return state_def.from_yaml(open(path, encoding="utf-8").read(), RunMetrics)


def _cmd_status(args, root: str) -> int:
    past = history(root)
    by = {}
    for p in past:
        by[p.status] = by.get(p.status, 0) + 1
    print(f"frozen fingerprint : {frozen_fingerprint(root)}")
    print(f"frozen files       : {sum(len(_files_under(root, f)) for f in FROZEN)} "
          f"across {len(FROZEN)} paths")
    print(f"mutable files      : {len(mutable_files(root))}")
    print(f"proposals          : {len(past)}"
          + (f"  ({', '.join(f'{v} {k}' for k, v in sorted(by.items()))})" if by else ""))
    tuned = load_tunables(root)
    print("tunables           :")
    for k, (lo, hi, what) in sorted(TUNABLES.items()):
        print(f"  {k:<20} {tuned.get(k, '(default)')!s:<10} [{lo}..{hi}]  {what}")
    return 0


def _cmd_log(args, root: str) -> int:
    past = history(root)
    if not past:
        print("no proposals yet")
        return 0
    for p in past:
        print(f"[{p.id}] {p.status:<9} {p.target:<28} {p.metric:<18} "
              f"{p.from_run}")
        print(f"       {p.rationale.strip().splitlines()[0][:100] if p.rationale else ''}")
        if p.detail:
            print(f"       -> {p.detail.splitlines()[0][:100]}")
    return 0


def _cmd_show(args, root: str) -> int:
    p = load(args.id, root)
    print(state_def.to_yaml(p))
    return 0


def _cmd_apply(args, root: str) -> int:
    p = load(args.id, root)
    ok, detail = apply_proposal(p, root, timeout=args.timeout)
    save(p, root)
    print(f"[{p.id}] {'APPLIED' if ok else 'REJECTED'} {p.target}\n  {detail}")
    return 0 if ok else 1


def _cmd_revert(args, root: str) -> int:
    p = load(args.id, root)
    ok, detail = revert_proposal(p, root)
    save(p, root)
    print(f"[{p.id}] {'REVERTED' if ok else 'NOT REVERTED'}\n  {detail}")
    return 0 if ok else 1


def _cmd_compare(args, root: str) -> int:
    before, after = _load_metrics(args.before), _load_metrics(args.after)
    better, why = compare(before, after)
    print(f"before: {before.summary()}")
    print(f"after : {after.summary()}")
    print(f"verdict: {'IMPROVED' if better else 'NOT AN IMPROVEMENT'} — {why}")
    return 0 if better else 1


def _cmd_propose(args, root: str) -> int:
    from red.backend import make_router
    metrics = _load_metrics(args.run)
    fp = frozen_fingerprint(root)
    if metrics.frozen_fingerprint and metrics.frozen_fingerprint != fp:
        print(f"[rsi] note: this run was judged by {metrics.frozen_fingerprint}, "
              f"the tree is now {fp} — its numbers are not comparable with "
              "runs made since.")
    router = make_router(args.models_config or "", model=args.model,
                         effort=args.effort, backend=args.backend)
    print(f"[rsi] improving from {metrics.summary()}")
    made = propose(metrics, root, router,
                   run_dir=args.run if os.path.isdir(args.run)
                   else os.path.dirname(args.run))
    if not made:
        print("[rsi] no proposals — the improver argued for no change, or its "
              "reply carried none this tool could read.")
        print(f"[rsi] its reply is at {os.path.join(PROPOSALS_DIR, 'last-reply.md')}"
              " — read it before concluding there was nothing to find")
        return 0
    for p in made:
        print(f"[{p.id}] {p.klass():<13} {p.target:<30} metric={p.metric}")
        print(f"       {p.rationale.strip().splitlines()[0][:110]}")
    print(f"\n{len(made)} proposal(s) written to {PROPOSALS_DIR}/. "
          f"Review with `show`, then `apply <id>`.")
    return 0


def main(argv=None) -> int:
    import argparse
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    ap = argparse.ArgumentParser(
        prog="python -m red.rsi",
        description="RED's self-improvement loop: propose, test and apply "
                    "changes to RED's own prompts and loop knobs.")
    ap.add_argument("--root", default=root, help="repository root")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("status", help="what may change, what may not, and what has")
    sub.add_parser("log", help="every proposal and what became of it")

    s = sub.add_parser("show", help="one proposal in full")
    s.add_argument("id")

    s = sub.add_parser("apply", help="apply a proposal if the test suite passes")
    s.add_argument("id")
    s.add_argument("--timeout", type=int, default=3600)

    s = sub.add_parser("revert", help="undo an applied proposal")
    s.add_argument("id")

    s = sub.add_parser("compare", help="did the second run improve on the first?")
    s.add_argument("before", help="a run directory or metrics.yaml")
    s.add_argument("after", help="a run directory or metrics.yaml")

    s = sub.add_parser("propose", help="run the improver on a finished run")
    s.add_argument("run", help="a run directory or its metrics.yaml")
    s.add_argument("--model", default="")
    s.add_argument("--effort", default="")
    s.add_argument("--backend", default="claude")
    s.add_argument("--models-config", default="")

    args = ap.parse_args(argv)
    fn = {"status": _cmd_status, "log": _cmd_log, "show": _cmd_show,
          "apply": _cmd_apply, "revert": _cmd_revert, "compare": _cmd_compare,
          "propose": _cmd_propose}[args.cmd]
    return fn(args, args.root)


if __name__ == "__main__":
    raise SystemExit(main())
