# Recursive self-improvement in RED

## What this means here

Recursive self-improvement, in the narrow sense this repository uses it, is
the property that a system makes a **persistent change to itself** which
improves both its future performance and its ability to make later
improvements. The distinction that does the work is against ordinary
iteration: an agent that edits a file, runs a test and fixes the bug has not
self-improved, because the next task starts with exactly the same agent. RED
improves when the *next run* is better because of what this run learned, and
when that learning is written into the repository rather than into a context
window.

RED already had the weak form. The Neo4j knowledge graph (`NEO4J_SETUP.md`)
carries what earlier runs designed, what the reviewers objected to and what
the gates measured, and feeding that back took convergence from 1/3 to 4/4.
But the graph only ever informs the agents. It never changes them: the
prompts, the loop and the code are the same on run 100 as on run 1.

`red/rsi.py` is the part that changes them. After a run finishes, an improver
agent reads what that run cost and what it produced, and writes proposals
against RED's own prompts and loop knobs. A proposal is applied only if the
full test suite still passes, and it is kept only if the next run does not
come out worse.

## The problem this has to solve first

Everything that scores a RED run is code in this same repository. The gates
are `red/nodes.py`, the cost model is `red/cost.py`, the security checks are
`red/security.py`, the structural check is `red/structure.py`, the thresholds
are `red/constants.py`. An improver able to edit any of them would discover
very quickly that the cheapest way to raise the predicted speedup is to change
how speedup is predicted — and every number downstream would agree with it.
This is the standing objection to self-improving systems scored on proxy
metrics, and it is not a hypothetical here; it is the shortest path to a
better score.

So the rule is structural rather than advisory, and it is deliberately not
stated only in the improver's prompt — the improver is allowed to rewrite that
prompt.

**The improver may not modify anything that judges a run.**

| Class | What | What happens |
|---|---|---|
| `mutable` | `red/prompts/**`, `rsi/tunables.toml` | Applied automatically if the suite passes |
| `frozen` | `nodes.py`, `cost.py`, `security.py`, `structure.py`, `state_def.py`, `iss.py`, `review.py`, `constants.py`, `rsi.py`, `tests/` | Refused outright |
| `proposal-only` | everything else, mostly `red/loop.py` | Written up for a human to apply |

`red/rsi.py` freezes itself, so the improver cannot rewrite its own guard.
`red/prompts/improve.md` is *not* frozen — the improver may rewrite its own
brief, which is the "improve its ability to improve" half of the definition.
That is safe precisely because the enforcement does not live in the brief.

### How it is enforced

`frozen_fingerprint()` hashes every frozen file. It is recorded in each run's
`metrics.yaml` and checked three times:

1. **Before applying** a proposal, as the baseline.
2. **After writing** the proposed file — a change here means the proposal
   reached something it should not have.
3. **After the test suite has run**, before its exit status is even looked at.

The third is the one that matters and the one that is easy to leave out. A
test command is arbitrary code, and while a proposal cannot change the suite,
it can change what the suite imports. "The tests passed" is only evidence if
the tests that passed were the frozen ones — a suite that still passes is
exactly what a successful goalpost-move looks like. If the fingerprint has
moved at any of the three points, every frozen file is restored from a
snapshot taken before the apply, the target file is restored, and the proposal
is rejected whatever the suite said.

The fingerprint also makes run-to-run comparison honest: two runs judged by
different code are not compared at all, because the thing that scored them is
not the same thing.

## What "better" means

`RunMetrics`, written to `metrics.yaml` in every run directory, records what a
run cost and what it produced. Nothing in it is claimed by an agent; it is all
measured by the loop.

**Quality**, compared first and lexicographically: converged, gates passed,
blocking findings (negated), structural blocking findings (negated), coverage,
predicted speedup.

**Effort**, compared second: agent seconds, turns, review + redesign + repair
rounds, rejected drafts.

Effort alone is trivially gained by giving up sooner — a run that abandons
synthesis after one turn is fast, cheap and worthless, and every effort metric
prefers it. So a regression in quality rejects a change whatever happened to
the clock.

Per-turn telemetry is recorded at the loop's single dispatch chokepoint
(`_dispatch_llm`), which is also where retries and failures are handled, so
every agent turn in a run is counted exactly once and attributed to its stage.
That is what lets the improver say "the designer spent 20 of its 50 minutes in
redesign rounds" instead of "the prompts feel verbose".

## The tunable loop knobs

Four values in `rsi/tunables.toml`, each clamped to a declared range on read:

| Knob | Range | What it caps |
|---|---|---|
| `REVIEW_MAX_ROUNDS` | 1–8 | review/revise rounds |
| `CRITIC_MAX_ROUNDS` | 1–6 | A2 self-critique rounds |
| `REPAIR_MAX_ROUNDS` | 1–6 | link-1 repair attempts |
| `PERF_MAX_ROUNDS` | 1–8 | Gate 3 redesign rounds |

All four cap *effort*, so turning one down trades compute against quality —
and quality is measured per run, so a knob turned too far shows up as a
regression. Nothing that defines the bar is reachable this way: the coverage
target, the speedup target, the gate tolerances and the vector counts are all
in the frozen constants. A tunables file that names one is rejected rather
than clamped, and a damaged file leaves RED importable with the defaults.

Precedence is environment variable, then `tunables.toml`, then the default.

## Using it

```sh
# what may change, what may not, and what has
python -m red.rsi status

# reflect on a finished run and write proposals
python -m red.rsi propose output/db/matrixmul/run_1 --model claude-opus-5-5 --effort high

# read one, apply it (runs the full suite), or undo it
python -m red.rsi show 0001
python -m red.rsi apply 0001
python -m red.rsi revert 0001

# every proposal and what became of it
python -m red.rsi log

# did the next run actually come out better?
python -m red.rsi compare output/db/matrixmul/run_1 output/db/matrixmul/run_2
```

Proposals live in `rsi/proposals/NNNN.yaml`, each carrying the run it was
argued from, the metric it claims to move, the evidence, the complete old and
new text, and what became of it. That record is also the improver's input:
re-proposing something already rejected wastes a round, so the history goes
into the next brief. Without it the improver is repetitive rather than
recursive.

## The loop, end to end

```
run ──> metrics.yaml ──> improver turn ──> proposals
                                              │
                              validate (frozen? stale? in bounds?)
                                              │
                               apply + full test suite + fingerprint
                                              │
                                        next run ──> compare()
                                              │
                                   kept, or reverted
```

## What it deliberately does not do

* **It does not edit code automatically.** `red/loop.py` and everything else
  outside the prompts is proposal-only. The loop is where a subtle change does
  the most damage and where a human reviewer is cheapest.
* **It does not raise its own bar.** Schmid's framing argues that real
  self-improvement has to raise the evaluation standard, not just score better
  against a fixed one. RED does not do that, and cannot while the evaluation
  is frozen — which is the right trade for now, because an improver that can
  move the bar is an improver that can lower it. Raising the bar stays a human
  decision, and `frozen_fingerprint` records which bar each run faced.
* **It does not run itself.** `propose` is invoked deliberately after a run,
  not wired into the loop's exit path. An improvement round costs a turn and
  should be a decision.

## Tests

`tests/test_env.py` covers the guard specifically:

* `test_the_self_improver_cannot_edit_what_judges_it` — every judging file is
  frozen; frozen, proposal-only, relative-escape and absolute targets are all
  refused; the fingerprint notices a frozen file changing; an apply whose test
  command tampers with a frozen file is abandoned and the tree restored.
* `test_a_proposal_is_kept_only_if_the_suite_still_passes` — stale and
  no-op proposals refused; a failing suite restores the file exactly; revert
  restores it.
* `test_an_improvement_has_to_be_an_improvement` — quality is compared before
  effort; a faster run that stopped converging is not an improvement; a
  changed fingerprint voids the comparison.
* `test_the_tunable_loop_knobs_stay_inside_their_bounds` — the bar is not
  tunable; out-of-range and unknown keys are dropped on read; a damaged file
  leaves RED importable.
* `test_a_run_records_what_it_cost` — telemetry is taken at the dispatch
  chokepoint, cleared per run, and round-trips as YAML.
