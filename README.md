# RED — an Agentic RISC-V ISA Extension Designer

RED is an agent loop that, given **one application project** and **one processor
core**, profiles the project, mines its hot loops, and designs a custom RISC-V
instruction extension for them. Its deliverable is a typed, executable
**`ISASpec`**.

The loop's agents, gates and feedback edges are documented with flow charts in
**[ARCHITECTURE.md](ARCHITECTURE.md)**.

RED runs on [CHIA](https://github.com/ucb-bar/chia) (UC Berkeley's agentic
hardware design framework). Its **LLM backend is Google Gemini on Vertex AI**,
billed to GCP credit.

## Scope

The RED proposal places A1 + A2 inside the "research contribution" enclosure and
A3–A5 (RTL, compiler, end-to-end) outside it as fixed evaluation tool nodes on
one platform. **This repository implements A1 + A2 only**; the ISASpec is the
artifact those downstream nodes would consume.

| Stage | What it does | Status |
|-------|--------------|--------|
| **A1 — Kernel mining** | Build + profile the project's own workload harness (callgrind), rank hot loops to ≥80% cycle coverage. Agentic ranking over a mechanical profile. | **Implemented** |
| **Gate 1** | Two independent profile runs must agree within ±5%. | **Implemented** |
| **A0 — Core analysis** | A separate agent reads the target core's RTL and writes a typed `CoreProfile`: ISA, microarchitecture, what the coprocessor interface can reach, units that already exist, per-core cycle costs. Runs concurrently with A1. | **Implemented** |
| **A2 — ISA synthesis** | S2a pattern analyzer → S2b instruction designer → S2c spec critic, emitting the `ISASpec`. Designs against the `CoreProfile`, not an abstract RISC-V. | **Implemented** |
| **Gate 3 — performance** | Costs every instruction against the target's *measured* platform model and rejects an extension predicted to deliver less than 2× whole-application speedup, handing the designer the arithmetic (≤3 redesign rounds). | **Implemented** |
| **Gate 4 — security** | Runs each C model under ASan+UBSan on adversarial operand values in exactly-sized allocations, against two differently poisoned output blocks (an unwritten output word is a residue leak), and counts the instructions the model itself executes for several operand values (a difference is a timing side channel). Decodes the encoding bits, and taint-scans the model for secret-dependent control flow. A **security sub-agent** probes each model with operand values chosen from what the arithmetic *means*. Only mechanically confirmed findings can fail the gate (≤2 revision rounds). | **Implemented** |
| **Spec review** | Four sub-agents examine the spec *concurrently* — implementability, callability, benefit, legality. Each sees its own findings from the previous round and must adjudicate them, so the review converges instead of redrawing; blocking findings go back to the designer (≤4 rounds). | **Implemented** |
| **link 1** | Coverage holds, and every instruction's C reference model compiles and survives a 100,000-vector stress run under ASan+UBSan. | **Implemented (partial — see LIMITATIONS)** |
| **ISS implementation** | A *separate* agent implements each instruction for Spike from the spec's prose + encoding alone — it never sees the C model. | **Implemented** |
| **Gate 2 / link 2** | The two models must agree on 10⁵ random + corner vectors, with the instruction really executed by a patched Spike. | **Implemented** |
| A3 / A4 / A5 | RTL (PCPI), LLVM pass, end-to-end speedup. | **Out of scope as loop nodes** — but see `eval/` for a hand-built evaluation of one ISASpec |

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
6. **Gate 4 audits the reference model, not the silicon.** Its evidence is what
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
7. **Gate 4 audits the instruction against its own declared operand block**, not
   against the caller. A model that writes all 16 words it declares is clean
   here even if the application's buffer is 8 words long — that overflow lives
   at the call site, and it is the `callability` reviewer's question (it was
   caught there, as a blocking finding, on a real run). The two are
   complementary, and neither subsumes the other.
8. **The security sub-agent cannot fail a design on its own word.** By
   construction: what it merely asserts is recorded as `major` and reaches the
   designer as advice. It earns a blocking finding only by handing over an
   operand value that actually breaks the model when executed. That keeps
   hallucinated vulnerabilities out of the gate — and it means a real
   vulnerability it can describe but not trigger will not stop the spec either.
9. **The instruction ABI is fixed by RED**, not chosen per instruction: an
   R-type in a custom opcode slot with `rs1` = input block address, `rs2` =
   output block address, `rd` = 0. That is what lets any designed instruction be
   harnessed mechanically, but it means RED currently designs memory-operand
   coprocessor instructions rather than register-to-register ones.

## Layout

```
red/
  constants.py    config: example inputs, gates, Gemini/GCP backend, output paths
  state_def.py    typed artifacts: HotLoopReport, ISASpec, GateResult
  db_node.py      durable artifact store (database node)
  tools.py        sealed MCP tools the agents use (source/profile/report/spec/…)
  nodes.py        mechanical edges: A1 profiling, Gate 1, link 1, Gate 2, link 2
  iss.py          Gate 2: Spike extension codegen, rv32 harness, the diff
  iss_selftest.py proves Gate 2 both passes agreeing models and fails others
  loop.py         the orchestration driver (A1 + Gate 1 → A2 + link 1 → Gate 2)
  prompts/        agent charters (mine.md, system.md, iss.md, debug.md)
target_project/   one example project (micro-ecc)  — an INPUT, not hard-coded
target_cpu/       one example core (PicoRV32)      — an INPUT, not hard-coded
cluster.yaml      GCP cluster definition (llm, database, profile + local head)
  graph.py        the persistent Neo4j knowledge graph (writes + agent queries)
  callgrind.py    reads callgrind's own output format (costs and call counts)
scripts/          red_env.sh (cluster lifecycle), setup_spike.sh (Gate 2
                  toolchain), setup_neo4j.sh (knowledge graph), gcp_util.py
ARCHITECTURE.md   the agent loop: agents, gates, feedback edges, flow charts
SPIKE_SETUP.md    installing Spike for Gate 2 (what, where, why, verifying)
NEO4J_SETUP.md    the knowledge graph: install, model, what the agents ask it
eval/             does the extension actually pay off? RTL, area, end-to-end
                  cycles on PicoRV32 -- see eval/RESULTS.md
tests/test_env.py environment verification
output/           all run artifacts (git-ignored): output/work + output/db
                  — the graph deliberately lives OUTSIDE this, so clearing
                  output/ between batches does not erase RED's memory
```

RED keeps a **persistent knowledge graph** of every run it has done (Neo4j, on
the head). The designer consults it before designing: what earlier runs built
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
python tests/test_env.py          # verify the environment (19 checks)
python -m red.loop --local        # mechanical smoke test: A1 + Gate 1, no LLM
python -m red.loop --local-llm    # full A1+A2 on this machine, real Gemini
bash scripts/red_env.sh up        # bring the GCP cluster up
python -m red.loop                # full A1+A2 on the CHIA cluster
bash scripts/red_env.sh down      # tear down, verify nothing is billing
```

Artifacts land in `output/db/<workload>/run_<N>/`: `profile.json`,
`HotLoopReport.json`, `ISASpec.draft.json`, `ISASpec.json` (the deliverable),
`gates/`, and `summary.md`.

## Designing for speed, not just correctness

The first extension RED produced was verified by every gate and **2% slower**
than the baseline at +74% area. The gates all asked *"does this instruction mean
what its spec says?"*; none asked what it costs. `red/cost.py` is that question,
and its constants are measured on the RTL in `eval/`, not assumed:

    instruction cycles = 28 + 2 x (2 x words) + 1 x mac_ops
    (28 = PCPI + FSM overhead, 2 = cycles per 32-bit word moved — both fitted
     to the measured 48-cycle mac96 and 92-cycle add256)

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

Gate 3 rejects the first two and tells the designer why, in those terms:
replace *whole kernels, not loop bodies*, drive `invocations` to 1, drive
`marshal_words` to 0, and cover enough of the profile for Amdahl to matter. Each
instruction must now declare `mac_ops`, `replaces`, `invocations` and
`marshal_words`, so its performance claim is explicit and checkable.

## Loop performance

Measured on one machine (`--local-llm`, `gemini-3.1-pro-preview`), a full run
with the review stage takes **~15.6 min**, against ~25 min for the previous
pipeline that did strictly less. Where it went:

| change | effect |
|---|---|
| ten tool servers collapsed to two (`DesignerTool`, `IssTool`) | each ChiaTool is its own Ray actor that rescans the port range as it binds; a server per artifact cost minutes before the first agent turn |
| the two Gate-1 profile runs dispatch before either is resolved | the second profile is free in wall-clock terms |
| four reviewers dispatch concurrently, with no tool servers | one model round trip instead of four |
| Gate 2's per-instruction differential tests run on a thread pool | subprocess-bound work overlaps; `RED_GATE2_PARALLEL` (default 4) |
| self-critique rounds cut from 8 to 3 | the dedicated reviewers do that job better than the designer re-reading itself |

## Does the extension actually pay off?

A1+A2 verify that an instruction *means what its spec says*. They never ask
whether it can be built, whether the application's data layout lets it be
called, or whether it is faster. `eval/` answers those for one ISASpec, on
cycle-accurate PicoRV32 RTL:

| | baseline | with the extension |
|---|---|---|
| whole ECDH exchange | 298,502,758 cycles | 304,410,162 cycles (**0.98x**) |
| iCE40 LUT4 | 5,582 | 9,696 (**+74%**) |
| shared secret correct | yes | yes |

The extension is verified, correct, and **2% slower for 74% more area**. The
cause is measured rather than guessed — RED's memory-block operand ABI makes
each instruction's latency data movement rather than arithmetic (2.0 cycles per
bus beat, 28 cycles fixed overhead), and `add256`/`sub256` drop the carry-out
their callers need. Full analysis, per-operation measurements, and the concrete
feedback for the next A2 round: **[eval/RESULTS.md](eval/RESULTS.md)**.

This is the answer the design's A3-A5 nodes exist to produce, and the reason a
`speedup < target -> re-mine` edge is in the proposal.
