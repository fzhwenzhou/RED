# The knowledge graph (Neo4j)

RED records every run in a persistent Neo4j graph, and the agents read it back.
Everything else RED writes is per-run JSON under `output/`, which you are told to
delete between test batches; the graph is what survives, and it is how run *N+1*
learns what run *N* already tried.

Without it the loop still runs — every write is best-effort and every read
degrades to a sentence the agent can act on — but each run starts from zero.

## Install (once)

```bash
bash scripts/setup_neo4j.sh install
bash scripts/setup_neo4j.sh start
```

This is a **userland** install: a Temurin JDK 21 and Neo4j Community 5.26 into
`~/.local/share/`, matching how `scripts/setup_spike.sh` installs Spike. Nothing
needs root — this host has neither passwordless sudo nor a docker group, so apt
and containers are both unavailable. The JDK is chosen for the machine's
architecture (`aarch64` and `x64` are both handled).

`scripts/red_env.sh up` starts the graph for you if it is installed, so in normal
use the command above is the only time you run it by hand.

| command | effect |
|---|---|
| `setup_neo4j.sh install` | JDK + Neo4j into the prefix, configure, set the password |
| `setup_neo4j.sh start` | start the server and apply the schema (idempotent) |
| `setup_neo4j.sh stop` | stop the server — **the data is kept** |
| `setup_neo4j.sh status` | up or down, plus a census of what is in the graph |
| `setup_neo4j.sh wipe` | delete every recorded run (asks for confirmation) |

## Where it runs, and why there

**On the head, not on a cluster worker.** `red_env.sh up` and `down` create and
destroy the GCP nodes, so a graph living on one would be erased on every
teardown — the opposite of persistent. The head's disk outlives any cluster.

**Data lives in `~/.local/share/red-neo4j/data`**, deliberately outside the
repo's `output/`. `down` leaves the server running on purpose: stopping it there
would make teardown look as though it had deleted your history.

Configuration is by environment variable, all with working defaults:
`RED_NEO4J_URI` (default `bolt://127.0.0.1:7687`), `RED_NEO4J_USER`,
`RED_NEO4J_PASSWORD`, `RED_NEO4J_DATABASE`, `RED_NEO4J_HOME`,
`RED_NEO4J_ENABLED=0` to turn recording off entirely.

## The model

```
(:Project)<-[:IN_PROJECT]-(:Workload)-[:HAS_LOOP]->(:HotLoop)
                               ^                        ^
                        [:OF_WORKLOAD]             [:REPLACES]
                               |                        |
     (:Core)<-[:TARGETS]-----(:Run)-[:PRODUCED]->(:Spec)-[:HAS_INSTRUCTION]->(:Instruction)
                               |  \                                          ^        ^
                     [:MINED]  |   \[:EVALUATED]->(:PerfEstimate)-[:SCORES]--'        |
                    (loop_id,  |    \[:REVIEWED]->(:Finding)-[:ABOUT]-----------------'
                cycle_share,   |     \[:CHECKED]->(:Gate)                             |
                     calls)    |      \[:SECURITY_CHECKED]->(:SecurityFinding)--------'
                               v           (check, confidence, severity, round)
                           (:HotLoop)
```

`SecurityFinding` is deliberately not a `Finding`. A review finding is somebody's
judgement; a security finding carries a `confidence` — `confirmed` (a tool
observed it), `derived` (decoded from the spec's own fields), `heuristic`, or
`agent` — and the useful query across runs is "what has actually been *proven*
unsafe on this workload", which needs that property to exist.

**Hot loops are keyed by function, not by the agent's `loop_id`.** A1 names the
same kernel `uECC_vli_mult::inner_loop` in one run, `::inner` in the next and
`::region` in a third. Keying on those would scatter one kernel's history across
three nodes and defeat the point of having a graph at all. The function name
comes from the profiler, so it is stable; the run's own `loop_id` is kept as a
property of the `:MINED` edge. (A function containing two genuinely distinct hot
loops would merge them — an accepted trade for a mechanical key.)

## Where the loop writes

| position in `red/loop.py` | what is recorded |
|---|---|
| run start | `Project`, `Core`, `Workload`, `Run` |
| after A0 | the core's RTL-derived properties on `Core` |
| after Gate 1 | `Gate`, the mined `HotLoop`s and this run's `:MINED` numbers |
| each Gate 3 round | the `Spec` at that stage and its `PerfEstimate` |
| each Gate 4 round | every `SecurityFinding`, with how it was established |
| each review round | the `Spec` at that stage and every `Finding` |
| final | the final `Spec`, remaining gates, and the run's verdict |
| `finally` | a run that died is marked `aborted`, never left `running` |

## What the agents do with it

The A2 charter tells the designer to consult the graph **before** designing, via
seven tools on its existing server:

- `graph_winning_shape(function)` — **the one to read first.** Only the
  instructions that actually *shipped* in a converged run, with their
  `work_per_word` and the speedup the gate credited. `prior_designs` lists
  everything ever tried, failures included, and leaves the designer to infer
  which rows are the lesson; this one *is* the lesson.
- `graph_prior_designs(function)` — every instruction earlier runs built for that
  kernel: `words`, `mac_ops`, `invocations`, `work_per_word`, the speedup the
  performance gate predicted, and whether that run converged. Excludes the
  asking run's own drafts — it used to return them, which is a designer's guess
  handed back as evidence in a table labelled "what earlier runs learned".
- `graph_prior_findings(function)` — the blocking and major objections reviewers
  already raised about instructions for it, **each with what became of the run
  that heard it**. `run_converged = true` means that design shipped anyway; a
  finding whose run died with it outstanding is the one to design around.
- `graph_prior_security(function)` — security defects a machine already
  established against designs for this workload. Cheap to avoid at design time,
  expensive at Gate 4.
- `graph_best_design()` — the best extension recorded for this workload.
- `graph_loops()` — every hot loop seen for this workload.
- `graph_query(cypher)` — read-only Cypher, with the schema in the tool's own
  description.

This matters because the transcripts show successive runs proposing the same
per-iteration instruction and being told again that its operand traffic is
charged on every invocation. `work_per_word` makes that visible directly: the
design that lost scored 0.25, the one that converged scored 2.00.

**The graph is read-only to agents.** Writes are refused twice over — a clause
check for a clear error message, and a genuinely read-only transaction, which is
the actual enforcement. An agent that could edit the graph could rewrite its own
history, and the reviewers' objections would stop meaning anything.

History is evidence, not instruction: the charter tells the designer that this
run's profile and core profile win if they disagree with it.

## Inspecting it yourself

```bash
bash scripts/setup_neo4j.sh status
./.venv/bin/python -m red.graph --stats
./.venv/bin/python -m red.graph --query \
  "MATCH (r:Run) RETURN r.run_id, r.converged, r.predicted_speedup ORDER BY r.started DESC"
```

The browser UI is at <http://localhost:7474> (user `neo4j`, password
`redgraph1` unless you set `RED_NEO4J_PASSWORD`).

## Tests

`tests/test_env.py` covers it two ways:

- `knowledge graph` writes two synthetic runs that name one kernel differently,
  then asserts they share a single `HotLoop` node and that every agent-facing
  query returns the right thing — including that writes are refused. It uses an
  isolated workload name and deletes it afterwards, so it never pollutes your
  real graph. It skips cleanly when Neo4j is not running.
- `graph degrades safely` runs every write and read path with the server marked
  unreachable and asserts nothing raises.
