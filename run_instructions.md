# Running RED

RED mines an application's hot loops (A1) and designs a custom RISC-V
instruction extension for them (A2). The output is `ISASpec.json`.

Everything runs on **one machine**. There is nothing to provision, nothing to
tear down, and nothing that bills while RED is idle. Two ways to run it:

| Mode | Command | Agent turns | Use it for |
|------|---------|-------------|------------|
| Mechanical | `python -m red.loop --mechanical` | none | Checking the profile + Gate 1 work at all, for free |
| Full loop | `python -m red.loop` | `claude -p`, up to 4 at a time | The real thing: AW → A1/A0 → A2 → Gate 3/4 → review → Gate 2 |

---

## 0. Prerequisites (one time)

You need: this repo, Python 3.10+, a C toolchain with valgrind, and the
[Claude Code](https://claude.com/claude-code) CLI on your PATH, already
authenticated. No cloud account, no API key in the environment — RED shells out
to `claude`, which carries its own credentials.

```bash
sudo apt-get install -y build-essential valgrind        # A1 profiling
python3 -m venv .venv && source .venv/bin/activate
pip install -e .               # RED (two dependencies: mcp, uvicorn)
claude --version               # the agent backend; must already be logged in
```

One command does all of it and checks the result:

```bash
bash scripts/red_env.sh setup
```

Pin the model if you want a specific one; otherwise RED uses whatever your
Claude Code install is configured for:

```bash
python -m red.loop --model <model-id>     # or: export RED_AGENT_MODEL=<model-id>
```

Install the two native dependencies RED brings its own copies of. Both are
userland installs under `~/.local` and need no root:

```bash
bash scripts/setup_spike.sh              # Gate 2's patched ISS
bash scripts/setup_neo4j.sh install      # the persistent knowledge graph
bash scripts/setup_neo4j.sh start
```

The graph is where RED remembers earlier runs — what was designed for each hot
loop, what it was predicted to be worth, and what the reviewers refused — and
the designer reads it before designing. Its data directory is outside
`output/`, so clearing run artifacts does not erase it, and `red_env.sh`
deliberately never stops it. A run without it still works; it just starts from
zero. See `NEO4J_SETUP.md`.

Verify the environment — 92 checks covering imports, the toolchain, artifact
round-trips, every mechanical gate (including that link 1 *rejects* a bad C
model), the durable store, the sealed MCP tools, and the knowledge graph:

```bash
python tests/test_env.py
```

`cc` and `valgrind` must be present, or A1 silently falls back to a much weaker
static estimate — the test fails loudly if so.

---

## 1. Mechanical smoke test (free, ~90 s)

```bash
python -m red.loop --mechanical
```

Builds the workload harness, profiles it twice under callgrind, runs Gate 1,
and writes a `HotLoopReport`. No LLM, so A2 stops with "no ISASpec produced" —
that is the expected outcome for this mode. Expect Gate 1 to pass at ~0.00%
drift and coverage around 99%. Run this after changing anything mechanical:
it exercises the profile, Gate 1 and the typed artifacts with no agent spend
at all.

---

## 2. The full loop

```bash
python -m red.loop
```

Options (all optional — the defaults are the bundled micro-ecc example):

```
--project PATH     application project directory
--core PATH        processor core directory (context for the encoding space)
--workload NAME    workload label
--harness FILE     project-relative .c holding the workload's main().
                   Pass "" and AW writes one (§3.6).
--cflags "..."     compile flags that pin the workload
--exclude "a,b"    path fragments whose .c files belong to a DIFFERENT program
--model ID         model for the agent turns
--mechanical       no agent turns at all (§1)
```

**Note the `--harness` default is the bundled micro-ecc test.** Running a
*different* project without overriding it hands that project a harness path it
does not have: the build fails, A1 falls back to a static estimate, and AW
never runs because a harness was nominally supplied. Pass `--harness ""`, or
use `scripts/run_projects.sh` (§3.5), which does it for you.

### How a turn actually runs

Each agentic stage is one `claude -p` subprocess (`red/backend.py`). Before it
starts, RED writes a temporary MCP config naming only the tool servers that
stage may use, and passes `--strict-mcp-config` with an explicit
`--allowedTools` list. The servers themselves are uvicorn apps on loopback
ports inside the driver process (`red/localtool.py`), each exposing only the
methods its agent is allowed to call.

That boundary is enforced twice on purpose — the server has no method for what
the agent must not do, *and* the turn is not permitted to call it. It is what
makes Gate 2 mean anything: the ISS implementer's config does not contain a
server that would show it the reference C models.

Knobs: `RED_AGENT_PARALLEL` (4) concurrent turns, `RED_AGENT_MAX_TURNS` (40)
tool-use turns inside one stage, `RED_AGENT_TIMEOUT` (1800 s) before a wedged
turn is cut off and reported as a failed turn.

A turn that fails is a *result*, never an exception: the loop's repair edges
are written to act on it, and a backend that is down should not look like a
design that is wrong.

---

## 3. Running every project

`scripts/run_projects.sh` iterates over every directory under
`target_project/`:

```bash
bash scripts/run_projects.sh                 # all of them, once each
bash scripts/run_projects.sh libcrc          # just these
RED_RUNS=3 bash scripts/run_projects.sh      # three runs of each
```

It holds the three per-project inputs that cannot be guessed from the sources:

| input | what it is |
|---|---|
| `harness` | the TU holding the workload's `main`. **Leave it empty** and AW writes one (below). Set it only when the repository already ships a representative workload, as micro-ecc does. |
| `cflags` | flags that define the workload's configuration |
| `exclude` | path fragments whose `.c` files belong to a *different program* in the same repository — libcrc's `precalc/` is a table generator with its own `main`, and linking it fails. They stay readable by the agents; they are just not linked. |

**Projects that generate part of their own source need that done first.**
libcrc's `src/crc32.c` includes `../tab/gentab32.inc`, produced by the
project's own build and gitignored as build output. RED compiles the sources
itself — that is how it instruments them — so for RED those generated files are
*sources*. The script runs the generator before profiling and says so if it
could not.

## 3.1 AW and Gate 0 — manufacturing a workload

RED's stated input is "a project + representative workloads" and repositories
routinely ship only the first half. The difference is not a matter of degree:

```
micro-ecc  test/test_ecdh.c   10,302,000,000 instructions   43% in one kernel
libcrc     test/testall.c            272,664 instructions   hottest: 0x10db4
matrixmul  test.c                    125,089 instructions   one 2x2 multiply
```

The bottom two are unit tests; profiling them mines the dynamic loader. So when
no harness is supplied, an agent reads the repository and writes one, and Gate 0
decides by **running it**: ≥`RED_WORKLOAD_MIN_IR` (50 M) instructions,
≥`RED_WORKLOAD_MIN_SHARE` (60%) of them in functions the binary itself defines,
<`RED_WORKLOAD_DETERMINISM` (2%) run-to-run drift, exits 0 twice.

The project-share condition is the one an agent cannot argue with: it comes from
`nm --defined-only` on the built binary intersected with what callgrind saw
execute. A harness looping around `printf` measured 14,193,444 instructions at
**9.0%** project share and was rejected; the same library over a 64 KiB buffer
measured 255,822,904 at **99.9%** and passed.

Failures come back as the measurement, including *by what factor* the harness
fell short and which project functions it actually reached — the most useful
line on the page when the agent thinks it is stressing something it is not.
`RED_WORKLOAD_ROUNDS` (4) rounds, then the run stops rather than mining libc.

Artifacts: `output/db/<workload>/run_<N>/workload/round<N>.{json,md}`.

## 4. Reading the results

Everything lands in `output/db/<workload>/run_<N>/` (git-ignored), written as
each artifact is produced rather than at the end, so a run you kill halfway
still leaves the record of what it had decided. Point `RED_DB_ROOT` somewhere
outside the repo to keep a batch's record when you clear `output/`.

| File | What it is |
|------|-----------|
| `profile.json` | A1's mechanical profile: per-function instruction counts, method, harness used |
| `HotLoopReport.json` | A1's deliverable: ranked hot loops, cycle share, coverage, Gate-1 status |
| `ISASpec.draft.json` | A2's spec as the agent wrote it, before verification |
| `ISASpec.json` | **RED's deliverable**: the verified spec (encodings, semantics, pseudocode, both executable models, link-1 + Gate-2 verdicts) |
| `gate2/` | Everything Gate 2 built: the generated Spike extension, the rv32 test program, the signature dumps |
| `gates/gate2.json`, `gates/chain.json` | The verification edges' verdicts |
| `summary.md` | Human-readable run summary |

`output/work/<workload>/<run-id>/` holds the driver-side scratch: the agent
transcripts under `llm_logs/`, link 1's generated C harnesses under `link1/`,
the built workload harness, and `callgrind.out`. This is what you read while a
run is in flight; it is also what gets deleted between batches, which is why
everything that matters is mirrored into the durable store as it is produced.

The loop exits 0 only when Gate 1, link 1 **and** Gate 2 all pass. Gate 2
passing means two independently written models of each instruction agreed on
100,000 vectors with the instruction really executed by Spike — it does *not*
mean the instruction is the right one for the workload. See LIMITATIONS in
`README.md` before quoting an ISASpec as verified.

---

## 4-graph. What the run left in the knowledge graph

Every run is also recorded in Neo4j, and unlike `output/` that record persists.

```bash
bash scripts/setup_neo4j.sh status
./.venv/bin/python -m red.graph --query \
  "MATCH (r:Run) RETURN r.run_id, r.converged, r.predicted_speedup, r.coverage
   ORDER BY r.started DESC"
```

The query worth knowing is the one the designer itself runs — what has been
tried for a hot loop, and what it was worth:

```bash
./.venv/bin/python -m red.graph --query \
  "MATCH (l:HotLoop {function:'uECC_vli_mult'})<-[:REPLACES]-(i:Instruction)
         <-[:HAS_INSTRUCTION]-(:Spec)<-[:PRODUCED]-(r:Run)
   RETURN r.run_id, r.converged, i.mnemonic, i.words, i.mac_ops,
          i.work_per_word, r.predicted_speedup
   ORDER BY r.predicted_speedup DESC"
```

`work_per_word` is the column that explains outcomes: the per-iteration designs
that lost score ~0.25, the whole-kernel ones that pass Gate 3 score ~2.0. Full
model and agent-facing queries in `NEO4J_SETUP.md`.

---

## 4a. What a good run looks like

Run end to end on the micro-ecc ECDH-secp256r1 workload:

```
- result: **converged (A1+A2)**
- A2 rounds: 1
- Gate 1 (re-profile ±5%): PASS runs 8882695006 vs 8882810979 cycles (0.00% vs ±5%)
- Gate 2 (C model vs Spike): PASS 2/2 instructions agree on 100,000 random + corner vectors
- coverage: 91.58% (80% target)
- PASS link1: 2/2 C models built and survived 100,000 vectors under ASan+UBSan
- PASS link2: 2/2 instructions: C model and patched Spike agree
- ISASpec: RED_ECC_SECP256R1 (2 instructions)
    vli256_mul      custom-0, 16x32b — 256x256 -> 512-bit multiply
    secp256r1_mmod  custom-0, 16x32b — reduce 512 bits mod the secp256r1 prime
```

Three agent turns total (`llm_logs/transcript.log` records each): **A1 mining**,
**A2 round 0**, **ISS implementer**. A1 mined the two kernels that actually
dominate ECDH — `uECC_vli_mult` (~43%) and `vli_mmod_fast_secp256r1` (~49%) —
and A2 designed one instruction for each. Wall clock ~25 min, nearly all of it
model latency; the mechanical gates are seconds (Gate 2 over 10⁵ vectors takes
about 3 s per instruction).

You can confirm Gate 2 really used the simulator by disassembling the harness it
built and left behind:

```bash
riscv64-unknown-elf-objdump -d output/work/*/*/gate2/*/rv32_blocks.elf | grep insn
#   800000bc:  01cf878b  .insn 4, 0x01cf878b
```

`0x01cf878b` is opcode `0x0b` (custom-0), funct3 `000`, funct7 `0000000` — the
encoding from the ISASpec. objdump prints `.insn` rather than a mnemonic because
the instruction does not exist in the base ISA; Spike ran it through the
extension RED generated.

One honest observation from that run: the ISS agent, which never saw the C
model, wrote schoolbook long multiplication identical to the designer's. See
LIMITATIONS point 2 in `README.md` for what that does and does not buy you.

---

## 4d. A0 — the core analyst

Before A2 designs anything, a separate agent reads the **target core's own RTL**
and writes a typed `CoreProfile`: the ISA this configuration implements, whether
the coprocessor interface can address memory, what functional units already
exist, how many cycles a load costs, which opcodes are free, and how big the core
is. It runs concurrently with A1 mining (one reads the project, the other the
core), and its output goes to A2's charter, the reviewers, and Gate 3's cost
constants.

On the bundled PicoRV32 example it independently derives the constraint that the
RTL evaluation in `eval/` discovered the hard way:

```
CONSTRAINT: The coprocessor interface (PCPI) cannot access memory.
CONSTRAINT: Operands must be provided entirely via the two 32-bit register reads.
CONSTRAINT: Results must fit in a single 32-bit register write (rd).
units already present: multiplier, divider, barrel shifter   <- do not duplicate
area: extremely small core (~750-1000 LUTs); a 512-bit FF buffer would be massive
```

Its cycle numbers are the *interface* latency (PCPI handshake = 1 cycle). Gate 3
composes them with the measured implementation overhead — a coprocessor also
pays for its own state machine and bus hand-over — so `fixed = 1 + 27` and
`beat = 1 + 1` reproduce the 28/2 the RTL actually showed. Taking A0's numbers
raw would make the gate optimistic by an order of magnitude.

Written to `output/db/<workload>/run_<N>/CoreProfile.json`.

## 4e. Gate 3 — the performance gate

Gate 3 costs every instruction against the target's measured platform model and
hands the designer the arithmetic when it falls short, for up to
`RED_PERF_ROUNDS` (default 4) redesign rounds.

**The bar is not a fixed 2×.** Amdahl caps any extension at `1/(1-coverage)`, so
a design covering 47.3% of an application cannot exceed 1.90× however perfect
the silicon — and demanding `RED_SPEEDUP_TARGET` of it demands something
arithmetic forbids. The bar in force is

    max(RED_SPEEDUP_FLOOR, min(RED_SPEEDUP_TARGET, ceiling x RED_SPEEDUP_ATTAINMENT)
        x RED_SPEEDUP_RELAX^(round-1))

which is 2.00 → 1.32 → 1.15 for a workload with headroom, and 1.42 → 1.15 for
one capped at 1.90×. Every verdict states the bar it applied and why it moved.
The floor (1.15×) exists because below it a prediction is inside the model's own
error, measured at ~8% against RTL.

Gate 3 is also **re-priced on the spec that actually ships**. It runs before
Gate 4 and the review, and a review revision can change the design after it last
ran: one run reported 11.60× for a design whose shipped form priced at 1.85× and
measured 1.67× on RTL.

`mac_ops` sets an instruction's price, so Gate 3 **measures it rather than
trusting it**: each `c_model` is compiled and run once under callgrind, and the
instruction is costed on what it executes. From a live run:

```
covers 97.9% of profiled cycles; predicted whole-application speedup 0.91x
  mul256:       189 cyc (32 beats, 64 MAC, 2.00 MAC/word) vs 10216 cyc  = 29.27x
  modreduce256: 10536 cyc (32 beats, 10411 MAC) vs 4925 cyc = 0.47x
                — declares mac_ops=0 but its C model executes 83,295
                  instructions (~10412 ops); costed on the measurement
```

Priced on the declaration that spec would have scored ~30× and shipped; priced
on what it does, it is a **slowdown**, and it went back to the designer.

`invocations` — how many times the instruction runs per kernel call — is the one
cost field RED cannot derive, and it was wrong by one to two orders of magnitude
in two of three shipped specs. The ratio of kernel to model instruction counts
is a *biased* estimator (the two sides are different implementations), so the
gate reports the inconsistency instead of substituting it, and `write_spec`
refuses a declaration more than 10× out.

Verdicts land in `output/db/<workload>/run_<N>/perf/round<N>.{json,md}`, and the
shipped re-price in `perf/shipped.{json,md}`.

To score the gate against cycle-accurate RTL:
`./.venv/bin/python eval/scripts/reprice.py`.

## 4f. Gate 4 — the security gate

Between Gate 3 and the review, RED asks whether the extension is safe to build
into the machine. Every other gate can pass on an instruction that leaks the key
it multiplies: a conditional subtraction in a modular reduction is correct, is
fast, and is a working timing attack.

The gate is decided **mechanically**, on the designer's own C model:

| check | how |
|---|---|
| memory safety | ASan + UBSan, operand blocks in exactly-sized allocations, adversarial vectors (all-ones, single bits at every word boundary, carry ripples) then random |
| output residue | each vector run twice against two differently poisoned output blocks — a word the model never writes is a leak of the caller's previous data |
| purity | the same two runs catch state carried between invocations |
| input integrity | the input block is shadowed and compared after every call |
| constant time | callgrind counts the instructions **the model itself** executes for several operand values; a difference is a timing side channel |
| secret branches | the ctgrind technique — the operand block is marked undefined and memcheck reports any branch or address depending on it |
| decode legality | the low 7 bits decoded against the four custom opcodes; field widths; encoding-slot collisions |
| no libc | a comment-stripped scan for allocation, I/O, the clock, non-local jumps |
| interrupt latency | the cost model's cycle count against `RED_SECURITY_MAX_STALL` — a coprocessor instruction cannot be interrupted |

A **security sub-agent** runs alongside (`red/prompts/security.md`). It reads the
spec and the mechanical report and probes each model with operand values chosen
from what the arithmetic *means* — the modulus, the reduction threshold, a pair
whose product is exactly 2³². Those probes are **executed** under the same
sanitizers, so a vector that breaks a model becomes a confirmed blocking finding.
What the agent merely asserts is capped at `major`: it reaches the designer as
advice and cannot fail the gate. A checker a model can talk into rejecting a
sound design is not a checker; one that passes an unsafe design because the model
said it looked fine is worse.

Blocking findings go back to the designer (`red/prompts/secure.md`) for up to
`RED_SECURITY_ROUNDS` rounds (default 2), with the branch-free rewrite spelled
out — masked select keeps `words` and `mac_ops`, and therefore the Gate 3 result,
intact. If a revision does change the model, Gate 3 is re-priced on it. The gate
is then re-run, without the agent, on whatever spec finally ships.

Knobs: `RED_SECURITY_VECTORS` (512), `RED_SECURITY_CT_VECTORS` (6),
`RED_SECURITY_ROUNDS` (2), `RED_SECURITY_PROBES` (24),
`RED_SECURITY_MAX_STALL` (4096), and `RED_SECURITY_CT=major` for a workload
whose operands are genuinely not secret.

**A skipped check is not a passed check.** Anything the host could not run is
named in `security/round<N>.md`, in the gate's one-line detail, to the reviewers,
and in the run summary as `NOT CHECKED —`. A host without a C compiler fails the
gate outright rather than passing on the static checks alone.

Verdicts land in `output/db/<workload>/run_<N>/security/`.

## 4c. The review stage and its feedback edge

After link 1, four **review sub-agents** examine the spec concurrently, each
answering a question the gates cannot:

| reviewer | question |
|---|---|
| `implementability` | can it be built as a coprocessor for this core, and at what latency/area? |
| `callability` | can the application call it on its own data without marshalling, and does it return every value callers need? |
| `benefit` | is it faster than the software it replaces, and does that move the whole application (Amdahl)? |
| `legality` | encodings, collisions, totality, agreement between prose, pseudo-code and operand packing |

They take their whole context in the prompt and answer with JSON, so they carry
no tool servers and all four dispatch in one round trip. Every **blocking**
finding goes back to *the designer that wrote the spec* (`red/prompts/revise.md`)
through the sealed status tool; the designer revises, link 1 is re-established,
and the reviewers run again — `RED_REVIEW_ROUNDS` rounds (default 2).

A spec with unresolved blocking findings is reported **NOT converged** even when
Gate 1, link 1, Gate 4 and Gate 2 all pass. That is deliberate: the gates verify that an
instruction means what it says, and `eval/` showed that is not the same as it
being worth building.

Findings land in `output/db/<workload>/run_<N>/review/round<N>.{json,md}`.

### What the reviewers actually catch

From three consecutive runs on the bundled example, round 1 raised 12, 9 and 9
findings (10, 5 and 7 blocking). They are substantive, and they independently
reproduce what the hand-built RTL evaluation in `eval/` measured:

- *"add256/sub256 are slower than the software they replace — marshalling
  separate 8-word operands into a contiguous block costs more than the
  instruction saves"* — `eval/` measured 774 cycles marshalled against 769 for
  the software routine.
- *"the latency of all instructions is dominated by data movement"* — `eval/`
  measured 2.0 cycles per operand word plus 28 cycles of fixed overhead.
- *"mulu256 discards the existing accumulator, forcing a 512-bit software
  addition that negates the saving"* — the same class of defect as the dropped
  carry-out that made `add256` unusable.

The designer acts on them. In run 1 it replaced a one-MAC-per-call `mac96` with
`mac256_32` — a 32x256 multiply-accumulate that does eight MACs per operand
block, i.e. eight times the arithmetic for the same data movement — and replaced
`add256`/`sub256` with `mmod_fast256`, a full modular reduction whose caller
needs no carry-out. Both survived Gate 2 against an independently written Spike
model. Blocking findings fell from 10 to 4.

### Five consecutive runs — *historical, before Gate 3 and Gate 4 existed*

These five predate the performance gate, the security gate, review adjudication
and the knowledge graph. They are kept because they are the evidence that the
review's feedback edge works at all; **they are not the current convergence
rate.** For that, see [eval/RESULTS.md](eval/RESULTS.md) and § 4a — the three
bundled projects currently converge in 16–41 minutes each with zero blocking
findings.

| run | wall clock | blocking findings, round 1 -> 2 | Gate 2 | outcome |
|---|---|---|---|---|
| 1 | 15.6 min | 10 -> 4 | PASS 2/2 | not converged |
| 2 | 21.4 min | 5 -> 3 | PASS 2/2 | not converged |
| 3 | — | 7 -> 3 | — | **crashed** in the ISS turn (see below) |
| 4 | 13.2 min | 7 -> 5 | PASS 3/3 | not converged |
| 5 | 19.6 min | 8 -> 1 | PASS 2/2 | not converged |

The feedback edge works in every run: blocking findings fall each time the
designer is given them (37 -> 16 across the five, a 57% reduction), and Gate 2
passed on the *revised* spec in every completed run. What the designer produces
varies between runs — one produced `mulu32x256` + `modred256`, another went back
to `add256`/`sub256`/`mul256` and was told so again — which is why the reviewers
are a loop rather than a one-shot check.

Run 3 exposed a real robustness bug and is left in the table rather than
re-rolled: the backend of the day *raised* on a reply that hit the output-token
limit instead of reporting it, and that exception tore down a 17-minute run.
Dispatch now converts a raised turn into a failed turn, which every caller
already handles — the rule the current backend keeps by construction
(`red/backend.py` returns a `QueryResult` with `success=False` and never raises
for an agent failure). Run 4 proved the fix: it hit a rate limit mid-revision,
logged it, and carried on to a clean Gate 2.

That the runs still report **NOT converged** is the honest verdict: the
reviewers keep finding real problems, because RED's fixed memory-block operand
ABI makes it hard for *any* instruction on this core to win. See
[eval/RESULTS.md](eval/RESULTS.md) §5.

## 4b. Does the extension pay off? (`eval/`)

The loop stops at a verified ISASpec. To find out whether that spec actually
accelerates the application and what it costs in silicon:

```bash
bash eval/scripts/run_all.sh quick   # all three projects, skipping the full ECDH
bash eval/scripts/run_all.sh         # everything, including the ~3 min ECDH runs
bash eval/scripts/area_all.sh        # synthesis area, where the datapath allows
./.venv/bin/python eval/scripts/reprice.py   # score Gate 3 against the RTL
```

Each project's ISASpec is built as PicoRV32 RTL, the application is patched to
use it, and the workload runs both ways on cycle-accurate RTL with the result
signature checked against the baseline binary. The measured outcome:

| project | baseline | extended | speedup | area |
|---|---:|---:|---:|---|
| micro-ecc | 298,502,758 | 178,231,186 | **1.6748×** | +76.6% LUT4 |
| matrixmul | 1,040,318 | 71,392 | **14.5719×** | behavioural model |
| libcrc | 852,200 | 405,322 | **2.1025×** | +18.0% LUT4 |

2,060 differential vectors, 0 mismatches. An earlier micro-ecc spec measured a
2% *slowdown* at +74% area, and Gate 3 exists because of that measurement;
`eval/scripts/reprice.py` exists because the gate then over-predicted these
three by 6.3×–7.0×. See [eval/README.md](eval/README.md) for the harness and
[eval/RESULTS.md](eval/RESULTS.md) for the evidence.

---

## 5. Troubleshooting

| Symptom | Cause / fix |
|---------|-------------|
| `test_a1_profile_workload` fails with `profile_method='static-asm'` | `valgrind`/`callgrind_annotate` missing → `sudo apt-get install valgrind` |
| Gate 1 fails with "no dynamic cycle counts" | The harness did not build; check `build_log` in `profile.json` and your `--cflags` |
| Every agent turn fails instantly | `claude` is not on PATH or not authenticated. Check with `bash scripts/red_env.sh check`; the transcripts in `output/work/.../llm_logs/` carry the CLI's own stderr |
| `A2: no ISASpec produced` | Expected with `--mechanical`. Otherwise read `output/work/.../llm_logs/transcript.log` — the turn either failed or wrote a spec the schema rejected, and both are recorded there |
| A turn is cut off after 30 min | `RED_AGENT_TIMEOUT`. The run continues: a wedged turn is reported as a failed turn, not a hang |
| Agent turns are slow and serialized | `RED_AGENT_PARALLEL` (default 4) bounds concurrent turns. A turn is a subprocess that mostly waits, so raising it costs little CPU |
| Tool servers fail to bind | A previous run left a driver process alive holding its ports: `pkill -f 'python -m red.loop'`. Ports are asked of the kernel, not scanned, so a clean machine never collides |
| The run says the knowledge graph is unavailable | Harmless — RED runs without memory of earlier runs. `bash scripts/setup_neo4j.sh start` |
| Gate 4 reports `NOT CHECKED — timing.secret_branch` | memcheck cannot start without glibc debuginfo: `sudo apt-get install -y libc6-dbg`. The gate is behaving correctly — a check it could not run is never reported as passed — but that run has no taint evidence |
| link 1 fails with a sanitizer report | Working as intended — the agent's C model has a real bug; the counterexample is in `gates/chain.json` |
| Gate 2 says "needs a patched-Spike toolchain" | `bash scripts/setup_spike.sh`; check with its `check` subcommand |
| Gate 2 fails with "spike extension does not compile" | The ISS agent's C++ is bad; the compiler diagnostic is in the counterexample, and the repair edge feeds it back automatically |
| Gate 2 reports an encoding collision | Two instructions claim the same (opcode, funct3, funct7); the counterexample names them |
