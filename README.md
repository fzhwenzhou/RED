# RED — an Agentic RISC-V ISA Extension Designer

RED is an agent loop that, given **one application project** and **one processor
core**, profiles the project, mines its hot loops, and designs a custom RISC-V
instruction extension for them. Its deliverable is a typed, executable
**`ISASpec`**.

If the project ships no representative workload — most do not; they ship unit
tests — an agent reads the repository and writes one, and a gate decides by
running it whether what it wrote is a workload at all. Three projects are
bundled (`micro-ecc`, `libcrc`, `matrixmul`) and
`bash scripts/run_projects.sh` iterates over all of them.

The loop's agents, gates and feedback edges are documented with flow charts in
**[ARCHITECTURE.md](ARCHITECTURE.md)**.

RED runs **on one machine**. Nothing is provisioned, nothing bills while it is
idle, and the only long-lived process is its knowledge graph. Each agentic
stage is a headless [Claude Code](https://claude.com/claude-code) turn
(`claude -p`) over MCP tool servers RED runs in-process on loopback; each gate
is a program that measures something. There is no cloud account to set up —
if `claude` is on your PATH and authenticated, RED runs.

## Scope

The RED proposal places A1 + A2 inside the "research contribution" enclosure and
A3–A5 (RTL, compiler, end-to-end) outside it as fixed evaluation tool nodes on
one platform. **This repository implements A1 + A2 only**; the ISASpec is the
artifact those downstream nodes would consume.

| Stage | What it does | Status |
|-------|--------------|--------|
| **AW — Workload synthesis** | When no harness is supplied, a sub-agent reads the repository and writes one: a C file driving the library's real work. | **Implemented** |
| **Gate 0 — is it a workload?** | The harness is built and run twice under callgrind. It must execute ≥50 M instructions, ≥60% of them in functions *the binary itself defines* (from the symbol table, not from the agent's description), with <2% run-to-run drift. A repository's own unit test executes ~10⁵ — profiling that mines the dynamic loader. | **Implemented** |
| **A1 — Kernel mining** | Build + profile the project's own workload harness (callgrind), rank hot loops to ≥80% cycle coverage. Agentic ranking over a mechanical profile. | **Implemented** |
| **Gate 1** | Two independent profile runs must agree within ±5%. | **Implemented** |
| **A0 — Core analysis** | A separate agent reads the target core's RTL and writes a typed `CoreProfile`: ISA, microarchitecture, what the coprocessor interface can reach, units that already exist, per-core cycle costs. Runs concurrently with A1. | **Implemented** |
| **A2 — ISA synthesis** | S2a pattern analyzer → S2b instruction designer → S2c spec critic, emitting the `ISASpec` ([format reference](ISASPEC_FORMAT.md)). Designs against the `CoreProfile`, not an abstract RISC-V. | **Implemented** |
| **Gate 3 — performance** | Costs every instruction against the target's *measured* platform model, handing the designer the arithmetic when it falls short (≤4 redesign rounds). The bar is **not fixed at 2×**: Amdahl caps an extension at `1/(1-coverage)`, so a design covering 47% of an application is held to a reachable bar rather than an impossible one, and the bar relaxes each redesign round to a 1.15× floor. Re-priced on the spec that actually ships, because the review can revise a design after the gate last ran. | **Implemented** |
| **Gate 4 — security** | Runs each C model under ASan+UBSan on adversarial operand values in exactly-sized allocations, against two differently poisoned output blocks (an unwritten output word is a residue leak), and counts the instructions the model itself executes for several operand values (a difference is a timing side channel). Decodes the encoding bits, and taint-scans the model for secret-dependent control flow. A **security sub-agent** probes each model with operand values chosen from what the arithmetic *means*. Only mechanically confirmed findings can fail the gate (≤2 revision rounds). | **Implemented** |
| **Spec review** | Four sub-agents examine the spec *concurrently* — implementability, callability, benefit, legality. Each sees its own findings from the previous round and must adjudicate them, so the review converges instead of redrawing; blocking findings go back to the designer (≤4 rounds). | **Implemented** |
| **link 1** | Coverage holds, and every instruction's C reference model compiles and survives a 100,000-vector stress run under ASan+UBSan. | **Implemented (partial — see LIMITATIONS)** |
| **ISS implementation** | A *separate* agent implements each instruction for Spike from the spec's prose + encoding alone — it never sees the C model. | **Implemented** |
| **Gate 2 / link 2** | The two models must agree on 10⁵ random + corner vectors, with the instruction really executed by a patched Spike. | **Implemented** |
| A3 / A4 / A5 | RTL (PCPI), LLVM pass, end-to-end speedup. | **Out of scope as loop nodes** — but see `eval/` for a hand-built evaluation of every current project ISASpec |

## LIMITATIONS

Read this before quoting any result.

1. **Gate 2 proves agreement, not correctness.** Two implementations agreeing
   means the spec was unambiguous enough that two independent readers built the
   same machine — it does not mean that machine is the one the workload needs.
   The independence is enforced mechanically, not by asking nicely: the ISS
   agent's sealed tool serves the spec with every `c_model` stripped out and
   accepts only `spike_model` back, so it cannot copy the implementation it is
   supposed to cross-check.
2. **…and when the algorithm is canonical, the two implementations converge.**
   Observed on a real run: for a 256x256 multiply specified as "in[0..7] = rs1,
   in[8..15] = rs2, 512-bit product to out[0..15]", the ISS agent — which never
   saw the C model — wrote schoolbook long multiplication *character for
   character* the same as the designer had. Agreement then says the spec is
   unambiguous (which is what the design claims the gate is for) but provides
   little implementation diversity, so do not read a Gate 2 pass as two
   genuinely different machines cross-checking each other. What Gate 2 adds over
   link 1 regardless, and this part is solid: the instruction is **decoded from
   the rv32 instruction stream and executed by the simulator** — operands arrive
   in architectural registers, results go through the MMU — so a wrong
   funct3/funct7, an encoding collision, or a broken operand convention fails
   here and *cannot* fail in link 1.
3. **link 1 is partial.** The design's link 1 proves the instruction's C model
   *equivalent to the original kernel* by differential testing over the kernel's
   input space. What runs here builds each `c_model` and stress-runs it on
   100,000 corner + pseudo-random vectors under AddressSanitizer and
   UndefinedBehaviorSanitizer, checking it is a pure, total, memory-safe
   function. That genuinely falsifies broken models (the test suite proves it
   catches an out-of-bounds write), but it does **not** establish equivalence to
   the mined kernel — that needs a per-kernel extraction harness.
4. **Gate 1 has no repair edge.** A failing Gate 1 is recorded and the run is
   marked not-converged; the design's "re-instrument the harness" repair loop is
   not wired up. In practice callgrind on a fixed binary is deterministic, so
   Gate 1 reproduces at 0.00% drift. (link 1 and Gate 2 *do* have bounded
   agentic repair edges.)
5. **`perf` cycle counts are corroboration only** and are usually 0 (no PMU
   access inside WSL/VMs). Ranking comes from callgrind instruction counts.
6. **Gate 4's constant-time evidence needs a host that can run memcheck.**
   The ctgrind check marks the operand block undefined and asks memcheck which
   branches depend on it. memcheck refuses to start without glibc debuginfo
   (`sudo apt-get install -y libc6-dbg`), and on a host without it the gate
   reports `NOT CHECKED — timing.secret_branch` rather than passing. That is
   the designed behaviour — a skipped check is not a passed check — but it
   means the taint signal is absent from those runs, and the instruction-count
   check is then the only constant-time evidence. `bash scripts/red_env.sh
   check` says up front whether it will run.
7. **Gate 4 audits the reference model, not the silicon.** Its evidence is what
   the C model does when executed: an out-of-bounds access, an unwritten output
   word, an instruction count that varies with the operands. That is the right
   level for a *spec* — a model whose execution time depends on its data
   describes an algorithm whose time depends on its data — but an RTL
   implementation can still leak through a channel the model cannot express
   (power, a shared multiplier's early-out, a cache the model does not have).
   And the constant-time evidence is a property of *this compilation*: a source
   branch a compiler turns into a conditional move counts as constant time here
   and is still a branch in the pseudo-code the hardware would be built from,
   which is why the taint scan runs alongside as an advisory signal. Gate 4
   passing is a floor, not a certificate.
8. **Gate 4 audits the instruction against its own declared operand block**, not
   against the caller. A model that writes all 16 words it declares is clean
   here even if the application's buffer is 8 words long — that overflow lives
   at the call site, and it is the `callability` reviewer's question (it was
   caught there, as a blocking finding, on a real run). The two are
   complementary, and neither subsumes the other.
9. **The security sub-agent cannot fail a design on its own word.** By
   construction: what it merely asserts is recorded as `major` and reaches the
   designer as advice. It earns a blocking finding only by handing over an
   operand value that actually breaks the model when executed. That keeps
   hallucinated vulnerabilities out of the gate — and it means a real
   vulnerability it can describe but not trigger will not stop the spec either.
10. **Gate 3's accuracy is bounded by two declarations it cannot measure.**
   Where they are sound the model is good: on the one design that has been both
   predicted and built, it predicts **1.81× against a measured 1.67×** (8%
   error). Where they are not, it is wrong by an order of magnitude. `mac_ops`
   is profiled from the C model and `replaces` is matched against the frozen
   report, but `invocations` (how many times the instruction runs per kernel
   call) and `marshal_words` (what the caller pays to lay operands out) are
   taken on the designer's word. The obvious estimator for `invocations` is
   *biased* — the model and the kernel are different implementations of the same
   work, so the ratio of their instruction counts reads 31 for a true 128 and
   147 for a true 4 — so the gate reports the inconsistency, refuses a
   declaration more than 10× out, and does not substitute a number it cannot
   stand behind.
11. **A workload AW wrote is a workload AW chose.** Gate 0 establishes that a
   synthesized harness does enough work, in the project's own code,
   reproducibly. It cannot establish that the work is *representative* of how
   anyone actually uses the library. Results on a synthesized workload
   characterise RED on that kernel, not on the repository's own tests.
12. **The instruction ABI is fixed by RED**, not chosen per instruction: an
   R-type in a custom opcode slot with `rs1` = input block address, `rs2` =
   output block address, `rd` = 0. That is what lets any designed instruction be
   harnessed mechanically, but it means RED currently designs memory-operand
   coprocessor instructions rather than register-to-register ones.

## Layout

```
red/
  constants.py    config: example inputs, gate thresholds, agent backend, paths
  state_def.py    typed artifacts: HotLoopReport, ISASpec, GateResult
  db_node.py      durable artifact store (outlives the run directory)
  tools.py        sealed MCP tools the agents use (source/profile/report/spec/…)
  localtool.py    runs one of those tool servers in-process on a loopback port
  backend.py      one agent turn = one agent-CLI subprocess over those tools
                  (Claude Code and Codex; both sealed to their own tools)
  modelcfg.py     which backend and model run which stage, from models.toml
  nodes.py        mechanical edges: A1 profiling, Gate 1, link 1, Gate 2, link 2
  iss.py          Gate 2: Spike extension codegen, rv32 harness, the diff
  iss_selftest.py proves Gate 2 both passes agreeing models and fails others
  cost.py         Gate 3: the platform cost model and the speedup prediction
  security.py     Gate 4: the mechanical security checks (sanitizers, ctgrind…)
  structure.py    the structural check: is this an ISA extension, or one
                  opcode with a kernel behind it?
  rsi.py          recursive self-improvement: run telemetry, proposals against
                  RED's own prompts, and the guard that keeps the improver
                  away from everything that scores a run
  workload.py     Gate 0: does a candidate harness measure anything?
  review.py       the four review sub-agents and their adjudication
  graph.py        the persistent Neo4j knowledge graph (writes + agent queries)
  callgrind.py    reads callgrind's own output format (costs and call counts)
  loop.py         the orchestration driver (AW → A1 → A2 → Gate 3/4 → review → Gate 2)
  prompts/        agent charters (workload.md, mine.md, core.md, system.md,
                  redesign.md, secure.md, security.md, revise.md, iss.md,
                  debug.md, improve.md, review/)
target_project/   the projects to design for (micro-ecc, libcrc, matrixmul)
                  — INPUTS, not hard-coded
models.toml       per-stage model routing — an INPUT, not hard-coded
target_cpu/       one example core (PicoRV32)      — an INPUT, not hard-coded
scripts/          red_env.sh (set this machine up, check it), run_projects.sh
                  (every project in turn), setup_spike.sh (Gate 2 toolchain),
                  setup_neo4j.sh (knowledge graph)
rsi/              tunables.toml (loop knobs the improver may retune) and
                  proposals/ (every self-improvement proposal and its fate)
ARCHITECTURE.md   the agent loop: agents, gates, feedback edges, flow charts
ISASPEC_FORMAT.md the ISASpec format: operand forms, architectural state,
                  the executable models, and what makes a spec an extension
RSI.md            recursive self-improvement: what may change, what may not,
                  and how "better" is decided
ISASPEC_FORMAT.md the deliverable's format: every field, every validation
                  rule, the fixed operand ABI, and a worked example
SPIKE_SETUP.md    installing Spike for Gate 2 (what, where, why, verifying)
NEO4J_SETUP.md    the knowledge graph: install, model, what the agents ask it
eval/             does the extension actually pay off? RTL, area, end-to-end
                  cycles on PicoRV32 -- see eval/RESULTS.md
tests/test_env.py environment verification
output/           all run artifacts (git-ignored): output/work + output/db
                  — the graph deliberately lives OUTSIDE this, so clearing
                  output/ between batches does not erase RED's memory
```

RED keeps a **persistent knowledge graph** of every run it has done (Neo4j, a
userland install on this machine). The designer consults it before designing:
what earlier runs built
for the same hot loop, what the performance gate predicted for each, and which
reviewer objections sank them. See `NEO4J_SETUP.md`.

RED is **general-purpose**: no workload, core, or hot function is hard-coded.
The bundled `micro-ecc` / `PicoRV32` pair is a worked example — defaults you
override:

```bash
python -m red.loop --project target_project/micro-ecc \
                   --core target_cpu/picorv32 \
                   --workload ECDH-secp256r1 \
                   --harness test/test_ecdh.c \
                   --cflags "-DuECC_WORD_SIZE=4 -DuECC_SUPPORTS_secp160r1=0 ..."
```

`--harness` (the `.c` file holding the workload's `main`) and `--cflags`
together *define* the workload: micro-ecc compiles every curve into each test
binary, so pinning the run to ECDH-over-secp256r1 means selecting that test and
switching the other curves off. `uECC_WORD_SIZE=4` makes the profiled limb
arithmetic 32-bit, matching the rv32 target the extension is designed for.

## Run

See **[run_instructions.md](run_instructions.md)** for the full guide.

```bash
bash scripts/red_env.sh setup      # one-time: venv, backend check, graph, self-test
bash scripts/red_env.sh check      # is this machine ready? (re-runnable, cheap)
python tests/test_env.py           # verify the environment (92 checks)
python -m red.loop --mechanical    # smoke test: profile + Gate 1, no agent spend
python -m red.loop                 # the full loop on one project
bash scripts/run_projects.sh       # every project under target_project/, in turn
bash scripts/run_projects.sh libcrc    # just this one
bash scripts/red_env.sh status     # graph, toolchain, backend, runs on disk
```

There is no bring-up and no teardown. `--mechanical` is the free path: it runs
the profile, Gate 1 and the typed artifacts with no agent turns at all, which
is what to run after changing anything mechanical.

## One model per stage

RED's agent turns are not equally important, so they do not have to run on the
same model. `models.toml` maps each stage to a model and an effort level, and
`red/modelcfg.py` resolves it. No model id appears anywhere in the code; a test
enforces that.

The split follows the same principle as the gates. **A stage whose output a
program checks can run cheaper, because a weak answer costs a round rather
than shipping.** AW writes a harness and Gate 0 decides by running it; the
security sub-agent proposes operand values and only the ones that actually
break a model under the sanitizers become findings; a repair turn gets the
compiler diagnostic back and link 1 re-runs on the result. Those three are
routed to a cheaper model. The designer, whose output *is* the deliverable,
runs at the highest effort, as does the ISS implementer, since Gate 2 only
means something if the independent side was written competently.

```toml
[default]
model  = "<strong-model-id>"
effort = "high"

[roles.security]           # proposals a sanitizer adjudicates
model  = "<cheaper-model-id>"
effort = "medium"

[roles.design]             # writes the deliverable
effort = "xhigh"
```

An entry may also name a **`backend`**, which is how a stage runs on a
different vendor's agent entirely: `claude` drives Claude Code, `codex` drives
OpenAI's Codex CLI. The natural use is the ISS implementer, since Gate 2's
independence argument gets stronger the less the two implementations share.
Both backends seal the turn to its own tools, by different means (Claude:
`--strict-mcp-config` plus `--allowedTools`; Codex: `--ignore-user-config`
plus per-server `enabled_tools`), and a test asserts both halves are present
in each. One asymmetry is worth knowing: Codex only permits MCP calls
headlessly under `--dangerously-bypass-approvals-and-sandbox`, which also
disables its shell sandbox, so a Codex-routed stage is a larger trust surface
than a Claude-routed one. RED gives it a throwaway working directory;
`red/backend.py` spells out the rest.

Resolution per stage: `[roles.<name>]`, then `[default]`, then `--model` /
`--effort` / `--backend`, then whatever the CLI is set to. Every run prints
the table it resolved, so a routing surprise shows up in the log rather than
in a result. `--models-config PATH` picks a different file, and
`--models-config ''` turns routing off so one `--model` applies everywhere,
which is what makes a model sweep a fair comparison. The eleven stage names
are listed in `red/modelcfg.py`; an unknown name in the file is reported
rather than silently ignored.

`run_projects.sh` holds the per-project inputs that cannot be guessed from the
sources — the harness (leave it empty and AW writes one), the cflags that define
the workload's configuration, and `--exclude` for directories belonging to a
*different* program in the same repository, like libcrc's `precalc/` table
generator. A project with no entry runs fully automatically.

Artifacts land in `output/db/<workload>/run_<N>/`: `profile.yaml`,
`HotLoopReport.yaml`, `ISASpec.draft.yaml`, `ISASpec.yaml` (the deliverable),
`gates/`, and `summary.md`.

## Designing for speed, not just correctness

The first extension RED produced was verified by every gate and **2% slower**
than the baseline at +74% area. The gates all asked *"does this instruction mean
what its spec says?"*; none asked what it costs. `red/cost.py` is that question,
and its constants are measured on the RTL in `eval/`, not assumed:

    instruction cycles = 28 + 2 x (2 x words) + 1 x mac_ops + 1 x logic_steps
    caller pays          12.9 x marshal_words, each way
    (28 = PCPI + FSM overhead and 2 = cycles per 32-bit word moved, fitted to
     the measured 48-cycle mac96 and 92-cycle add256; 12.9 fitted to the 413 of
     656 cycles the measured micro-ecc call spends marshalling a 16-word block)

`logic_steps` is work the declared `mac_ops` cannot account for, charged at the
rate hardware runs it. Pricing everything as multiplies was a real defect:
libcrc's models perform 256 sequential bit steps per block and the synthesizable
RTL takes one cycle each, measuring 297–301 cycles where the multiply-rate model
predicted ~112.

Operand traffic is fixed by `words` and paid on every invocation, so the
quantity that decides everything is **arithmetic per operand word moved**:

| design | words | MACs | cycles | MAC/word | app speedup |
|---|---|---|---|---|---|
| one 32×32 MAC, 64× per call | 5 | 1 | 49 | 0.10 | 1.32× — rejected |
| a whole 256×256 multiply | 16 | 64 | 156 | 2.00 | 1.71× — rejected (covers only 42%) |
| + a whole modular reduction | 16 | 0 | 92 | 0.00 | **9.14× — accepted** |

`mac_ops` is **measured, not trusted**: each `c_model` is compiled and run under
callgrind, and the instruction is priced on what it executes. In a live run a
spec declaring `mac_ops = 0` for a 512-iteration bit-serial reduction was
repriced 92 → 7,590 cycles, turning a claimed win into a slowdown. An
instruction is also charged for the part of its kernel it does *not* replace,
and `replaces` must name a mined loop exactly — both were loopholes that paid an
instruction for work it never did.

Gate 3 rejects the first two and tells the designer why: replace whole kernels,
count `invocations` from the data volume, minimize `marshal_words`, and cover
enough of the profile for Amdahl to matter.

The RTL evaluation then scored the gate itself, and it was over-predicting all
three shipped extensions by 6.3×–7.0×. Four causes were separated and three
fixed — coverage counted twice (two regions of one function each claimed its
full share, summing to 143%), bit-serial work priced as multiplies, and a gate
that never re-ran on what shipped. The fourth, an unverifiable `invocations`, is
now reported rather than guessed. On the one design that has been both predicted
and built the model is now accurate to **8%** (1.81× predicted, 1.67× measured),
where it previously said 11.60×. Run
`./.venv/bin/python eval/scripts/reprice.py` after any change to `red/cost.py`;
the evidence is in `eval/RESULTS.md`.

## Loop performance

All three bundled projects converge, on one machine, in **42 minutes for the
whole batch**: libcrc 9.4 min (1 synthesis round), matrixmul 13.3 min (2),
micro-ecc 19.3 min (2). The same three took 16–41 minutes *each* on the cluster
this loop used to run on — the agent turns are the same work, but a turn is now
a subprocess rather than a scheduled task on a VM reached through a tunnel.

The mechanical front end is 1–3 minutes of that; agent turns dominate, and
which edge a run exercises decides the rest. Where the time went:

| change | effect |
|---|---|
| ten tool servers collapsed to four, split by *what an agent may see* rather than by artifact | every turn re-reads the MCP config and every server is a thread and a port; a server per artifact cost startup time for a boundary that did no work |
| the two Gate-1 profile runs dispatch before either is resolved | the second profile is free in wall-clock terms |
| four reviewers dispatch concurrently, with no tool servers | one turn's wall-clock instead of four. An agent turn is a subprocess that mostly waits, so `RED_AGENT_PARALLEL` (default 4) bounds how many run at once rather than how much CPU they get |
| a throttled turn is retried with backoff, not counted as a failure | two consecutive `RateLimitError`s used to trip the "backend is down" guard and abandon synthesis; three runs in one batch died that way |
| `measure_ops` caches on a hash of the C model | re-pricing every review round compiles and runs each model under callgrind; on a 12-instruction spec that was 48 passes where 12 suffice, and it ran the machine out of memory and lost a tool server |
| Gate 2's per-instruction differential tests run on a thread pool | subprocess-bound work overlaps; `RED_GATE2_PARALLEL` (default 4) |
| self-critique rounds cut from 8 to 3 | the dedicated reviewers do that job better than the designer re-reading itself |

## Do the extensions actually pay off?

The evaluation now covers every current target project on PicoRV32:

| project | baseline cycles | extended cycles | speedup | area result |
|---|---:|---:|---:|---|
| micro-ecc | 298,502,758 | 178,231,186 | **1.6748x** | +76.6% LUT4 |
| matrixmul | 1,040,318 | 71,392 | **14.5719x** | behavioral model; unavailable |
| libcrc | 852,200 | 405,322 | **2.1025x** | +18.0% LUT4 |

All baseline and extended outputs match. Micro-ecc and libcrc use synthesizable
RTL; matrixmul uses an explicit four-operation behavioral floating-point model,
so its cycle result is useful for architectural evaluation but has no valid area
claim yet.

Correctness is checked, not assumed: **2,060 differential vectors with 0
mismatches** (412 / 412 / 1,236), each set run twice — once with distinct input
and output blocks, once with them aliased, because that is how the application
calls them — and every end-to-end run must print `RESULT: PASS`, exit zero, and
produce a signature identical to the baseline binary.

These measurements are also what scored RED's own performance gate, which had
been over-predicting all three by 6.3×–7.0× (11.60×, 14.71×, 92.10×). That is
fixed as far as the evidence reaches; see *Designing for speed* above and
**[eval/RESULTS.md](eval/RESULTS.md)** for the full argument, the reproduction
commands, and what remains unverifiable.

This is the answer the design's A3-A5 nodes exist to produce, and the reason a
`speedup < target -> re-mine` edge is in the proposal.
