"""RED's design-knowledge graph, in Neo4j.

Everything else RED writes is *per run*: a directory of JSON under
``output/db/<workload>/run_<n>/`` that describes one pass of the loop and is
deleted whenever the operator clears ``output/``. That is fine for auditing a
run and useless for learning across runs — nothing connects the instruction
this run is designing to the four earlier attempts at the same hot loop, what
the performance gate predicted for each, or which reviewer objection sank them.

This module is that connection. One persistent graph accumulates every run:

    (:Project)<-[:IN_PROJECT]-(:Workload)<-[:OF_WORKLOAD]-(:Run)-[:TARGETS]->(:Core)
                                   |                        |
                              [:HAS_LOOP]              [:PRODUCED]
                                   v                        v
                               (:HotLoop)<--[:REPLACES]--(:Instruction)<-[:HAS_INSTRUCTION]-(:Spec)
                                   ^                        ^      ^
                              [:MINED]                 [:ABOUT] [:SCORES]
                                   |                        |      |
                                (:Run)               (:Finding)  (:PerfEstimate)

Two decisions worth stating, because they are what make the graph useful rather
than merely present:

**Hot loops are keyed by function, not by the agent's ``loop_id``.** A1 names
the same kernel ``uECC_vli_mult::inner_loop`` in one run, ``::inner`` in the
next and ``::loops`` in a third. Keying on those would scatter one kernel's
history across three nodes and defeat the whole point. The function name comes
from the profiler, so it is stable; the run's own ``loop_id`` is kept as a
property of the ``:MINED`` edge. (A function holding two genuinely distinct hot
loops would merge them — an acceptable trade for a key that is mechanical.)

**Writes never fail the loop.** A design run that dies because a database is
down would be a bad trade for a nice-to-have. Every write is wrapped: if Neo4j
is unreachable the module says so once and turns itself into a no-op.
"""

from __future__ import annotations

import os
import re
import threading
from typing import Any

from red.constants import (
    NEO4J_DATABASE,
    NEO4J_ENABLED,
    NEO4J_PASSWORD,
    NEO4J_URI,
    NEO4J_USER,
)

# ---------------------------------------------------------------------------
# Connection
# ---------------------------------------------------------------------------

_lock = threading.Lock()
_driver: Any = None
_state = "unopened"          # unopened | ok | unavailable | disabled
_warned = False


def _warn_once(msg: str) -> None:
    global _warned
    if not _warned:
        print(f"[red] graph: {msg}")
        _warned = True


def driver():
    """The shared Neo4j driver, or None when the graph is unavailable.

    Opened lazily and once. A failure here is remembered, so a run without a
    server pays one connection timeout rather than one per write.
    """
    global _driver, _state
    with _lock:
        if _state == "ok":
            return _driver
        if _state in ("unavailable", "disabled"):
            return None
        if not NEO4J_ENABLED:
            _state = "disabled"
            return None
        try:
            import neo4j
        except ImportError:
            _state = "unavailable"
            _warn_once("the neo4j driver is not installed "
                       "(pip install neo4j) — not recording this run")
            return None
        try:
            # Notifications off: the server emits advisories (an OPTIONAL
            # MATCH that found nothing feeding an aggregate, say) that are
            # correct and expected here, and printing them mid-run buries the
            # loop's own output.
            d = neo4j.GraphDatabase.driver(
                NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASSWORD),
                connection_timeout=10, max_connection_lifetime=3600,
                notifications_min_severity="OFF")
            d.verify_connectivity()
        except Exception as exc:                          # noqa: BLE001
            _state = "unavailable"
            _warn_once(f"no server at {NEO4J_URI} ({type(exc).__name__}) — "
                       "not recording this run. Start it with "
                       "`bash scripts/setup_neo4j.sh start`.")
            return None
        _driver, _state = d, "ok"
        return _driver


def available() -> bool:
    return driver() is not None


def close() -> None:
    global _driver, _state
    with _lock:
        if _driver is not None:
            try:
                _driver.close()
            except Exception:                             # noqa: BLE001
                pass
        _driver, _state = None, "unopened"


def _write(cypher: str, **params) -> list:
    """Run one write query, swallowing every failure.

    The graph is a record of the run, not a participant in it.
    """
    d = driver()
    if d is None:
        return []
    try:
        with d.session(database=NEO4J_DATABASE) as s:
            return [r.data() for r in s.run(cypher, **params)]
    except Exception as exc:                              # noqa: BLE001
        _warn_once(f"write failed ({type(exc).__name__}: {exc}) — "
                   "continuing without the graph")
        return []


def _read(cypher: str, **params) -> list:
    """Run one read query in a genuinely read-only transaction."""
    d = driver()
    if d is None:
        return []
    with d.session(database=NEO4J_DATABASE,
                   default_access_mode="READ") as s:
        return s.execute_read(
            lambda tx: [r.data() for r in tx.run(cypher, **params)])


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

_SCHEMA = [
    "CREATE CONSTRAINT red_project IF NOT EXISTS "
    "FOR (n:Project) REQUIRE n.name IS UNIQUE",
    "CREATE CONSTRAINT red_core IF NOT EXISTS "
    "FOR (n:Core) REQUIRE n.name IS UNIQUE",
    "CREATE CONSTRAINT red_workload IF NOT EXISTS "
    "FOR (n:Workload) REQUIRE n.name IS UNIQUE",
    "CREATE CONSTRAINT red_run IF NOT EXISTS "
    "FOR (n:Run) REQUIRE n.uid IS UNIQUE",
    "CREATE CONSTRAINT red_loop IF NOT EXISTS "
    "FOR (n:HotLoop) REQUIRE n.uid IS UNIQUE",
    "CREATE CONSTRAINT red_spec IF NOT EXISTS "
    "FOR (n:Spec) REQUIRE n.uid IS UNIQUE",
    "CREATE CONSTRAINT red_instruction IF NOT EXISTS "
    "FOR (n:Instruction) REQUIRE n.uid IS UNIQUE",
    "CREATE CONSTRAINT red_perf IF NOT EXISTS "
    "FOR (n:PerfEstimate) REQUIRE n.uid IS UNIQUE",
    "CREATE CONSTRAINT red_finding IF NOT EXISTS "
    "FOR (n:Finding) REQUIRE n.uid IS UNIQUE",
    "CREATE CONSTRAINT red_gate IF NOT EXISTS "
    "FOR (n:Gate) REQUIRE n.uid IS UNIQUE",
    "CREATE CONSTRAINT red_security IF NOT EXISTS "
    "FOR (n:SecurityFinding) REQUIRE n.uid IS UNIQUE",
    "CREATE INDEX red_loop_function IF NOT EXISTS "
    "FOR (n:HotLoop) ON (n.function)",
    "CREATE INDEX red_run_converged IF NOT EXISTS "
    "FOR (n:Run) ON (n.converged)",
    "CREATE INDEX red_instruction_mnemonic IF NOT EXISTS "
    "FOR (n:Instruction) ON (n.mnemonic)",
]


def ensure_schema() -> bool:
    """Create the constraints and indexes. Idempotent."""
    d = driver()
    if d is None:
        return False
    with d.session(database=NEO4J_DATABASE) as s:
        for stmt in _SCHEMA:
            s.run(stmt)
    return True


# ---------------------------------------------------------------------------
# Keys
# ---------------------------------------------------------------------------

def _run_uid(workload: str, run_id: str) -> str:
    return f"{workload}::{run_id}"


def _loop_uid(workload: str, function: str) -> str:
    return f"{workload}::{function}"


# ---------------------------------------------------------------------------
# Writes — one per position in the loop
# ---------------------------------------------------------------------------

def start_run(workload: str, run_id: str, project: str, core: str,
              model: str = "", harness: str = "", cflags: str = "") -> None:
    """Called as the run begins: the project, core, workload and run itself."""
    _write(
        """
        MERGE (p:Project {name: $project})
        MERGE (c:Core {name: $core})
        MERGE (w:Workload {name: $workload})
          ON CREATE SET w.harness = $harness, w.cflags = $cflags
        MERGE (w)-[:IN_PROJECT]->(p)
        MERGE (r:Run {uid: $uid})
          SET r.run_id = $run_id, r.model = $model, r.converged = false,
              r.started = datetime(), r.status = 'running'
        MERGE (r)-[:OF_WORKLOAD]->(w)
        MERGE (r)-[:TARGETS]->(c)
        """,
        uid=_run_uid(workload, run_id), run_id=run_id, workload=workload,
        project=os.path.basename(project.rstrip("/")) or project,
        core=os.path.basename(core.rstrip("/")) or core,
        model=model, harness=harness, cflags=cflags)


def record_core_profile(core: str, profile) -> None:
    """A0's reading of the target core's RTL."""
    if profile is None:
        return
    _write(
        """
        MERGE (c:Core {name: $core})
        SET c.isa = $isa, c.microarchitecture = $uarch,
            c.coprocessor = $coproc, c.coprocessor_memory_access = $mem,
            c.cycles_per_word_moved = $beat,
            c.coprocessor_issue_overhead = $issue,
            c.area_note = $area
        """,
        core=os.path.basename(core.rstrip("/")) or core,
        isa=getattr(profile, "isa", "") or "",
        uarch=getattr(profile, "microarchitecture", "") or "",
        coproc=getattr(profile, "coprocessor", "") or "",
        mem=bool(getattr(profile, "coprocessor_memory_access", False)),
        beat=float(getattr(profile, "cycles_per_word_moved", 0) or 0),
        issue=float(getattr(profile, "coprocessor_issue_overhead", 0) or 0),
        area=getattr(profile, "area_note", "") or "")


def record_hot_loops(workload: str, run_id: str, report) -> None:
    """A1's mined loops, after Gate 1 has accepted them.

    The loop node is shared across runs (keyed by function); the numbers that
    belong to *this* run's profile go on the :MINED edge.
    """
    if report is None:
        return
    for loop in getattr(report, "loops", []) or []:
        fn = loop.function or (loop.loop_id or "").split("::")[0]
        if not fn:
            continue
        _write(
            """
            MATCH (r:Run {uid: $run})
            MERGE (w:Workload {name: $workload})
            MERGE (l:HotLoop {uid: $uid})
              SET l.function = $function, l.workload = $workload,
                  l.source_file = $source_file,
                  l.body = CASE WHEN $body <> '' THEN $body ELSE l.body END
            MERGE (w)-[:HAS_LOOP]->(l)
            MERGE (r)-[m:MINED]->(l)
              SET m.loop_id = $loop_id, m.rank = $rank,
                  m.cycle_share = $share, m.calls = $calls,
                  m.dynamic_cycles = $cycles, m.trip_count = $trip,
                  m.operand_stats = $operands
            """,
            run=_run_uid(workload, run_id), workload=workload,
            uid=_loop_uid(workload, fn), function=fn,
            loop_id=loop.loop_id or "", rank=int(loop.rank or 0),
            share=float(loop.cycle_share or 0.0), calls=int(loop.calls or 0),
            cycles=int(loop.dynamic_cycles or 0),
            trip=int(loop.trip_count or 0),
            source_file=loop.source_file or "",
            body=(loop.body or "")[:8000],
            operands=str(loop.operand_stats or "")[:2000])
    _write("MATCH (r:Run {uid: $run}) SET r.coverage = $coverage",
           run=_run_uid(workload, run_id),
           coverage=float(getattr(report, "coverage", 0.0) or 0.0))


def record_spec(workload: str, run_id: str, spec, stage: str = "draft") -> None:
    """The ISASpec and its instructions, linked to the loops they replace.

    Called at each stage the spec meaningfully changes (draft, after Gate 3,
    after review, final) so the graph shows how a design moved, not just where
    it landed.
    """
    if spec is None:
        return
    run = _run_uid(workload, run_id)
    _write(
        """
        MATCH (r:Run {uid: $run})
        MERGE (s:Spec {uid: $uid})
          SET s.name = $name, s.version = $version,
              s.description = $description, s.stage = $stage,
              s.instruction_count = $n
        MERGE (r)-[:PRODUCED {stage: $stage}]->(s)
        """,
        run=run, uid=f"{run}::{stage}", name=spec.name or "",
        version=getattr(spec, "version", "") or "",
        description=(getattr(spec, "description", "") or "")[:2000],
        stage=stage, n=len(spec.instructions or []))
    for ins in spec.instructions or []:
        fn = (ins.replaces or "").split("::")[0]
        _write(
            """
            MATCH (s:Spec {uid: $spec})
            MERGE (i:Instruction {uid: $uid})
              SET i.mnemonic = $mnemonic, i.name = $name, i.stage = $stage,
                  i.words = $words, i.mac_ops = $mac_ops,
                  i.invocations = $invocations,
                  i.marshal_words = $marshal_words,
                  i.encoding = $encoding, i.semantics = $semantics,
                  i.replaces = $replaces, i.c_model = $c_model,
                  i.work_per_word = $wpw
            MERGE (s)-[:HAS_INSTRUCTION]->(i)
            WITH i
            OPTIONAL MATCH (l:HotLoop {uid: $loop})
            FOREACH (_ IN CASE WHEN l IS NULL THEN [] ELSE [1] END |
                     MERGE (i)-[:REPLACES]->(l))
            """,
            spec=f"{run}::{stage}", uid=f"{run}::{stage}::{ins.mnemonic}",
            mnemonic=ins.mnemonic or "", name=ins.name or "", stage=stage,
            words=int(ins.words or 0), mac_ops=int(ins.mac_ops or 0),
            invocations=int(ins.invocations or 0),
            marshal_words=int(getattr(ins, "marshal_words", 0) or 0),
            encoding=(ins.encoding or "")[:500],
            semantics=(ins.semantics or "")[:2000],
            replaces=ins.replaces or "",
            c_model=(ins.c_model or "")[:8000],
            wpw=(float(ins.mac_ops or 0) / max(2 * int(ins.words or 1), 1)),
            loop=_loop_uid(workload, fn))


def record_perf(workload: str, run_id: str, est, round_: int,
                stage: str = "gate3") -> None:
    """One Gate 3 verdict, with its per-instruction arithmetic."""
    if est is None:
        return
    run = _run_uid(workload, run_id)
    uid = f"{run}::perf::{stage}::{round_}"
    _write(
        """
        MATCH (r:Run {uid: $run})
        MERGE (p:PerfEstimate {uid: $uid})
          SET p.round = $round, p.app_speedup = $speedup,
              p.covered_share = $covered, p.detail = $detail,
              p.measurement_error = $err
        MERGE (r)-[:EVALUATED]->(p)
        """,
        run=run, uid=uid, round=round_,
        speedup=float(est.app_speedup or 0.0),
        covered=float(est.covered_share or 0.0),
        detail=(est.detail or "")[:4000],
        err=(getattr(est, "measurement_error", "") or "")[:1000])
    for c in getattr(est, "per_instruction", []) or []:
        _write(
            """
            MATCH (p:PerfEstimate {uid: $perf})
            OPTIONAL MATCH (i:Instruction)
              WHERE i.uid STARTS WITH $prefix AND i.mnemonic = $mnemonic
            FOREACH (_ IN CASE WHEN i IS NULL THEN [] ELSE [1] END |
              MERGE (p)-[sc:SCORES]->(i)
                SET sc.cycles = $cycles, sc.replaced_cycles = $replaced,
                    sc.speedup = $speedup, sc.beats = $beats,
                    sc.mac_ops = $mac_ops, sc.note = $note)
            """,
            perf=uid, prefix=f"{run}::", mnemonic=c.mnemonic or "",
            cycles=float(c.cycles or 0.0),
            replaced=float(c.replaced_cycles or 0.0),
            speedup=float(c.speedup or 0.0), beats=int(c.beats or 0),
            mac_ops=int(c.mac_ops or 0), note=(c.note or "")[:1000])


def record_review(workload: str, run_id: str, report, round_: int) -> None:
    """One review round's findings, attached to the instructions they concern."""
    if report is None:
        return
    run = _run_uid(workload, run_id)
    for n, f in enumerate(getattr(report, "findings", []) or []):
        _write(
            """
            MATCH (r:Run {uid: $run})
            MERGE (f:Finding {uid: $uid})
              SET f.reviewer = $reviewer, f.severity = $severity,
                  f.round = $round, f.instruction = $instruction,
                  f.finding = $finding, f.evidence = $evidence, f.fix = $fix
            MERGE (r)-[:REVIEWED {round: $round}]->(f)
            WITH f
            OPTIONAL MATCH (i:Instruction)
              WHERE i.uid STARTS WITH $prefix AND i.mnemonic = $instruction
            FOREACH (_ IN CASE WHEN i IS NULL THEN [] ELSE [1] END |
                     MERGE (f)-[:ABOUT]->(i))
            """,
            run=run, uid=f"{run}::finding::{round_}::{f.reviewer}::{n}",
            reviewer=f.reviewer or "", severity=f.severity or "",
            round=round_, instruction=f.instruction or "",
            finding=(f.finding or "")[:2000],
            evidence=(getattr(f, "evidence", "") or "")[:2000],
            fix=(getattr(f, "fix", "") or "")[:2000],
            prefix=f"{run}::")


def record_security(workload: str, run_id: str, report, round_: int) -> None:
    """One security round's findings.

    Kept as their own label rather than folded into :class:`Finding`, because
    the question a later run asks about them is different: not "what did a
    reviewer dislike" but "what has actually been *proven* unsafe on this
    workload before". The ``confidence`` property is what makes that query
    possible, so it is stored verbatim.
    """
    if report is None:
        return
    run = _run_uid(workload, run_id)
    for n, f in enumerate(getattr(report, "findings", []) or []):
        _write(
            """
            MATCH (r:Run {uid: $run})
            MERGE (s:SecurityFinding {uid: $uid})
              SET s.check = $check, s.severity = $severity,
                  s.confidence = $confidence, s.round = $round,
                  s.instruction = $instruction, s.finding = $finding,
                  s.evidence = $evidence, s.fix = $fix
            MERGE (r)-[:SECURITY_CHECKED {round: $round}]->(s)
            WITH s
            OPTIONAL MATCH (i:Instruction)
              WHERE i.uid STARTS WITH $prefix AND i.mnemonic = $instruction
            FOREACH (_ IN CASE WHEN i IS NULL THEN [] ELSE [1] END |
                     MERGE (s)-[:ABOUT]->(i))
            """,
            run=run,
            uid=f"{run}::security::{round_}::{f.check}::{f.instruction}::{n}",
            check=f.check or "", severity=f.severity or "",
            confidence=getattr(f, "confidence", "") or "", round=round_,
            instruction=f.instruction or "",
            finding=(f.finding or "")[:2000],
            evidence=(getattr(f, "evidence", "") or "")[:2000],
            fix=(getattr(f, "fix", "") or "")[:2000],
            prefix=f"{run}::")


def record_gate(workload: str, run_id: str, gate) -> None:
    """One gate or link verdict."""
    if gate is None:
        return
    run = _run_uid(workload, run_id)
    _write(
        """
        MATCH (r:Run {uid: $run})
        MERGE (g:Gate {uid: $uid})
          SET g.gate = $gate, g.passed = $passed, g.detail = $detail,
              g.counterexample = $cex
        MERGE (r)-[:CHECKED]->(g)
        """,
        run=run, uid=f"{run}::gate::{gate.gate}", gate=gate.gate,
        passed=bool(gate.passed), detail=(gate.detail or "")[:2000],
        cex=(getattr(gate, "counterexample", None) or "")[:2000])


def finish_run(workload: str, run_id: str, converged: bool,
               wallclock: float = 0.0, blocking: int = 0,
               speedup: float = 0.0, summary: str = "") -> None:
    """Close the run out. This is what makes a run comparable to its peers."""
    _write(
        """
        MATCH (r:Run {uid: $run})
        SET r.converged = $converged, r.finished = datetime(),
            r.wallclock_seconds = $wallclock, r.blocking_findings = $blocking,
            r.predicted_speedup = $speedup, r.status = 'finished',
            r.summary = $summary
        """,
        run=_run_uid(workload, run_id), converged=bool(converged),
        wallclock=float(wallclock or 0.0), blocking=int(blocking or 0),
        speedup=float(speedup or 0.0), summary=(summary or "")[:8000])


def abort_run(workload: str, run_id: str, reason: str = "") -> None:
    """Close out a run that ended without a verdict.

    Called from the driver's ``finally``, so a crash, a killed batch or a stage
    that bailed early leaves an honestly-labelled record instead of a run stuck
    at ``status = 'running'`` that later queries would count as live.
    """
    _write(
        """
        MATCH (r:Run {uid: $run}) WHERE r.status = 'running'
        SET r.status = 'aborted', r.finished = datetime(),
            r.summary = $reason, r.converged = false
        """,
        run=_run_uid(workload, run_id), reason=(reason or "")[:2000])


# ---------------------------------------------------------------------------
# Reads — what the agents ask
# ---------------------------------------------------------------------------

def prior_designs(workload: str, function: str, limit: int = 12) -> list:
    """Every instruction earlier runs designed for this kernel, with its shape,
    what the performance gate predicted for it, and how that run ended.

    This is the query the whole graph exists for: without it each run
    rediscovers from scratch that (say) replacing a loop *body* cannot pay,
    because the operand traffic is charged per invocation.
    """
    return _read(
        """
        MATCH (l:HotLoop {uid: $loop})<-[:REPLACES]-(i:Instruction)
              <-[:HAS_INSTRUCTION]-(s:Spec)<-[:PRODUCED]-(r:Run)
        OPTIONAL MATCH (p:PerfEstimate)-[sc:SCORES]->(i)
        WITH r, i, s, max(p.app_speedup) AS app, max(sc.speedup) AS ins_speedup,
             head(collect(sc.note)) AS note
        RETURN r.run_id AS run, r.converged AS converged,
               i.mnemonic AS mnemonic, i.words AS words, i.mac_ops AS mac_ops,
               i.invocations AS invocations, i.work_per_word AS work_per_word,
               i.stage AS stage, app AS app_speedup,
               ins_speedup AS instruction_speedup, note AS note
        ORDER BY converged DESC, app_speedup DESC
        LIMIT $limit
        """,
        loop=_loop_uid(workload, function), limit=limit)


def prior_findings(workload: str, function: str, limit: int = 15) -> list:
    """Blocking and major findings earlier reviewers raised about instructions
    that replaced this kernel — the mistakes not worth making twice."""
    return _read(
        """
        MATCH (l:HotLoop {uid: $loop})<-[:REPLACES]-(i:Instruction)<-[:ABOUT]-(f:Finding)
        WHERE f.severity IN ['blocking', 'major']
        RETURN DISTINCT f.severity AS severity, f.reviewer AS reviewer,
               i.mnemonic AS mnemonic, i.words AS words, i.mac_ops AS mac_ops,
               f.finding AS finding, f.fix AS fix
        ORDER BY severity, reviewer
        LIMIT $limit
        """,
        loop=_loop_uid(workload, function), limit=limit)


def prior_security(workload: str, function: str = "", limit: int = 15) -> list:
    """Security defects earlier runs had *proven* against instructions for this
    loop — the ones a machine confirmed, first.

    A designer that reads this before drafting does not have to rediscover that
    a conditional subtraction in a reduction leaks its operand through timing;
    it was caught, recorded, and fixed once already.
    """
    rows = _read(
        """
        MATCH (r:Run)-[:OF_WORKLOAD]->(w:Workload {name: $workload})
        MATCH (r)-[:SECURITY_CHECKED]->(s:SecurityFinding)
        WHERE s.severity IN ['blocking', 'major']
          AND ($function = '' OR
               EXISTS { MATCH (s)-[:ABOUT]->(:Instruction)-[:REPLACES]->
                              (l:HotLoop {function: $function}) })
        OPTIONAL MATCH (s)-[:ABOUT]->(i:Instruction)
        RETURN DISTINCT s.severity AS severity, s.confidence AS confidence,
               s.check AS check, coalesce(i.mnemonic, s.instruction) AS mnemonic,
               i.words AS words, s.finding AS finding, s.fix AS fix
        ORDER BY confidence, severity
        LIMIT $limit
        """,
        workload=workload, function=function or "", limit=limit)
    return rows


def best_design(workload: str) -> list:
    """The best design recorded for this workload: converged runs first, then
    by predicted speedup."""
    return _read(
        """
        MATCH (r:Run)-[:OF_WORKLOAD]->(:Workload {name: $workload})
        WHERE r.status = 'finished'
        MATCH (r)-[:PRODUCED]->(s:Spec)-[:HAS_INSTRUCTION]->(i:Instruction)
        WITH r, s, collect(i.mnemonic + ' (words=' + toString(i.words) +
             ', mac_ops=' + toString(i.mac_ops) + ', replaces=' +
             i.replaces + ')') AS instructions
        RETURN r.run_id AS run, r.converged AS converged,
               r.predicted_speedup AS predicted_speedup,
               r.blocking_findings AS blocking, s.name AS spec,
               instructions
        ORDER BY converged DESC, predicted_speedup DESC
        LIMIT 5
        """,
        workload=workload)


def loop_history(workload: str) -> list:
    """Every hot loop recorded for this workload and how often it was mined."""
    return _read(
        """
        MATCH (:Workload {name: $workload})-[:HAS_LOOP]->(l:HotLoop)
        OPTIONAL MATCH (r:Run)-[m:MINED]->(l)
        OPTIONAL MATCH (l)<-[:REPLACES]-(i:Instruction)
        RETURN l.function AS function, count(DISTINCT r) AS runs_mined,
               avg(m.cycle_share) AS mean_cycle_share,
               max(m.calls) AS calls,
               count(DISTINCT i) AS instructions_designed
        ORDER BY mean_cycle_share DESC
        """,
        workload=workload)


def stats() -> dict:
    """A one-line census, for the setup script's `status`."""
    rows = _read(
        """
        CALL () { MATCH (r:Run) RETURN count(r) AS runs }
        CALL () { MATCH (r:Run) WHERE r.converged RETURN count(r) AS converged }
        CALL () { MATCH (l:HotLoop) RETURN count(l) AS loops }
        CALL () { MATCH (i:Instruction) RETURN count(i) AS instructions }
        CALL () { MATCH (f:Finding) RETURN count(f) AS findings }
        CALL () { MATCH (s:SecurityFinding) RETURN count(s) AS security }
        RETURN runs, converged, loops, instructions, findings, security
        """)
    return rows[0] if rows else {}


# ---------------------------------------------------------------------------
# Free-form queries, read-only
# ---------------------------------------------------------------------------

_WRITE_CLAUSE = re.compile(
    r"\b(create|merge|delete|detach|set|remove|drop|foreach|load\s+csv|"
    r"call\s*\{[^}]*\b(create|merge|delete|set|remove)\b)\b", re.I)


def query(cypher: str, limit: int = 50) -> list:
    """Run an agent-supplied read-only Cypher query.

    Two independent guards, because one of them is advisory: the regex gives a
    clear refusal for an obvious write, and the READ transaction is the actual
    enforcement — Neo4j itself rejects a write inside it whatever the text says.
    """
    if _WRITE_CLAUSE.search(cypher or ""):
        raise ValueError(
            "this query would modify the graph; only read queries are allowed "
            "(the graph is the record of what happened, not a scratchpad)")
    if not re.search(r"\breturn\b", cypher or "", re.I):
        raise ValueError("a query must RETURN something")
    if not re.search(r"\blimit\b", cypher, re.I):
        cypher = cypher.rstrip().rstrip(";") + f"\nLIMIT {int(limit)}"
    return _read(cypher)


# ---------------------------------------------------------------------------
# CLI (used by scripts/setup_neo4j.sh)
# ---------------------------------------------------------------------------

def _main(argv=None) -> int:
    import argparse
    import json

    ap = argparse.ArgumentParser(description="RED's Neo4j knowledge graph")
    ap.add_argument("--ensure-schema", action="store_true")
    ap.add_argument("--stats", action="store_true")
    ap.add_argument("--wipe", action="store_true")
    ap.add_argument("--query", metavar="CYPHER")
    args = ap.parse_args(argv)

    if driver() is None:
        print(f"graph unavailable at {NEO4J_URI}")
        return 1
    if args.ensure_schema:
        ensure_schema()
        print(f"schema applied at {NEO4J_URI}")
    if args.wipe:
        _write("MATCH (n) DETACH DELETE n")
        print("graph emptied")
    if args.stats or not (args.ensure_schema or args.wipe or args.query):
        s = stats()
        print("  graph: {runs} run(s), {converged} converged, {loops} hot "
              "loop(s), {instructions} instruction(s), {findings} review "
              "finding(s), {security} security finding(s)"
              .format(**{**{"runs": 0, "converged": 0, "loops": 0,
                            "instructions": 0, "findings": 0, "security": 0},
                         **s}))
    if args.query:
        print(json.dumps(query(args.query), indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
