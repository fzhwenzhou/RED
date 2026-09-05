# RED — an Agentic RISC-V ISA Extension Designer

RED is an agent loop that, given **one application project** and **one processor
core**, profiles the project, mines its hot loops, and designs a custom RISC-V
instruction extension for them. Its deliverable is a typed, executable
**`ISASpec`**.

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
| **A2 — ISA synthesis** | S2a pattern analyzer → S2b instruction designer → S2c spec critic (≤8 rounds), emitting the `ISASpec`. | **Implemented** |
| **link 1** | Coverage holds, and every instruction's C reference model compiles and survives a 100,000-vector stress run under ASan+UBSan. | **Implemented (partial — see LIMITATIONS)** |
| **ISS implementation** | A *separate* agent implements each instruction for Spike from the spec's prose + encoding alone — it never sees the C model. | **Implemented** |
| **Gate 2 / link 2** | The two models must agree on 10⁵ random + corner vectors, with the instruction really executed by a patched Spike. | **Implemented** |
| A3 / A4 / A5 | RTL (PCPI), LLVM pass, end-to-end speedup. | **Out of scope** |

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
6. **The instruction ABI is fixed by RED**, not chosen per instruction: an
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
scripts/          red_env.sh (cluster lifecycle), setup_spike.sh (Gate 2
                  toolchain), gcp_util.py (GCP helper)
SPIKE_SETUP.md    installing Spike for Gate 2 (what, where, why, verifying)
tests/test_env.py environment verification
output/           all run artifacts (git-ignored): output/work + output/db
```

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
