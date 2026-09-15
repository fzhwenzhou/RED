# `eval/` — measuring whether a RED extension pays off

RED's A1+A2 emit a verified `ISASpec`. This directory answers the two questions
the ISASpec cannot answer about itself:

1. **Is the whole application faster** with the extension than with the plain
   RISC-V binary — measured on cycle-accurate RTL, not estimated?
2. **What does it cost in area?**

Results and analysis: **[RESULTS.md](RESULTS.md)**.

This is a deliberately narrow, hand-built stand-in for the design's A3/A4/A5
nodes — enough to get trustworthy numbers for one ISASpec on one platform, not
a general co-design flow. What is *not* automated is called out in
[§ Scope](#scope) below.

---

## Prerequisites

Everything comes from tools already used elsewhere in this repo, plus
[oss-cad-suite](https://github.com/YosysHQ/oss-cad-suite-build) for the RTL
tooling:

| tool | used for | note |
|---|---|---|
| `verilator` ≥ 5 | whole-application RTL runs | `--binary`; ~150 s per ECDH run |
| `iverilog` / `vvp` | the C-model ⇔ RTL differential test | seconds |
| `yosys` | area | no PDK needed |
| `riscv64-unknown-elf-gcc` | rv32im firmware | same compiler Gate 2 uses |
| `valgrind` | cycle-share attribution | host build, optional |

`target_cpu/picorv32` and `target_project/micro-ecc` are used **read-only**: the
firmware build copies micro-ecc's sources and patches the copy, and the PicoRV32
core is instantiated unmodified.

## Layout

```
rtl/red_accel.v      PCPI coprocessor implementing the ISASpec (the A3 step)
rtl/red_core.v       PicoRV32 ± the accelerator; identical CPU config either way
rtl/red_soc_tb.v     SoC: core + 256 KiB SRAM + MMIO (putchar/exit/cycles)
rtl/tb_accel.v       differential test: red_accel vs the ISASpec's C models
firmware/bench.c     the workload — one full ECDH exchange, self-checking
firmware/microbench.c per-operation cost, software vs instruction
firmware/check.c      is the patched micro-ecc still correct?
firmware/red_ext.h    .insn intrinsics for the designed instructions
patches/              micro-ecc changes, applied to a build copy only
scripts/              gen_vectors.py, build_firmware.sh, run_eval.sh, area.sh
build/                everything generated (git-ignored)
```

## Running it

```bash
bash eval/scripts/run_eval.sh          # everything: verify, per-op, end-to-end (~8 min)
bash eval/scripts/run_eval.sh quick    # skip the two 3-minute ECDH runs (~1 min)
bash eval/scripts/area.sh              # area comparison (~1 min, no simulation)
```

Individually:

```bash
# 1. vectors from the ISASpec's C models, then C model <=> RTL
./.venv/bin/python eval/scripts/gen_vectors.py [path/to/ISASpec.json] [--n 200]
iverilog -g2005-sv -o eval/build/tb_accel.vvp eval/rtl/tb_accel.v eval/rtl/red_accel.v
vvp eval/build/tb_accel.vvp +vectors=eval/build/vectors.txt

# 2. firmware (base = unmodified micro-ecc, ext = patched to use the extension)
bash eval/scripts/build_firmware.sh base|ext|micro|micro-ext|check

# 3. a core (ENABLE_ACCEL=0 baseline, =1 extended)
verilator --binary -j 4 --top-module red_soc_tb -GENABLE_ACCEL=1 -Wno-fatal \
  --Mdir eval/build/obj_ext -o red_sim_ext \
  eval/rtl/red_soc_tb.v eval/rtl/red_core.v eval/rtl/red_accel.v \
  target_cpu/picorv32/picorv32.v

# 4. run; +trace_accel logs every bus beat the coprocessor performs
./eval/build/obj_ext/red_sim_ext +firmware=eval/build/ext.hex [+trace_accel]
```

Both binaries use a **deterministic RNG**, so they perform identical work and
their cycle counts are directly comparable. `bench.c` checks that both parties
derive the same shared secret and prints `RESULT: PASS`/`FAIL` — a wrong answer
can never be reported as a speedup.

## Applying this to a different ISASpec

The harness reads the spec for the parts it can (test vectors come straight from
the `c_model` fields, so `gen_vectors.py` needs no edits), but **the RTL is
hand-written per instruction**. For a new spec:

1. `./.venv/bin/python eval/scripts/gen_vectors.py path/to/new/ISASpec.json`
2. Implement each instruction's datapath in `rtl/red_accel.v` — add a `funct3`
   case to `store_word`, and set `nwords` for it. The load/compute/store FSM and
   the bus mastering are generic and need no changes.
3. `vvp …/tb_accel.vvp` until it reports 0 mismatches. Do not skip this: it
   found two real bugs in `red_accel` (a one-word store shift, and a bus-handover
   race where the unit accepted the `mem_ready` left over from the CPU's
   instruction fetch and latched an instruction word as operand data).
4. Write a patch that makes the application call the instructions (below), then
   `run_eval.sh`.

## The patch workflow

`patches/micro-ecc-red-ext.patch` is applied by `build_firmware.sh` to a **copy**
of micro-ecc in `eval/build/src-ext/`; the submodule is never modified. To
change or extend it:

```bash
cp -r target_project/micro-ecc /tmp/work-a && cp -r /tmp/work-a /tmp/work-b
# edit /tmp/work-b/uECC.c ...
cd /tmp && diff -u work-a/uECC.c work-b/uECC.c > \
    $OLDPWD/eval/patches/micro-ecc-red-ext.patch   # keep the header comment
bash eval/scripts/build_firmware.sh ext            # verifies it still applies
./eval/build/obj_ext/red_sim_ext +firmware=eval/build/check.hex   # still correct?
```

Use the intrinsics in `firmware/red_ext.h` rather than writing `.insn` by hand;
they encode RED's operand ABI (`rs1` = &input block, `rs2` = &output block) and
carry the `"memory"` clobber the memory operands require.

## Scope

Honest about what this is and is not:

- **The RTL is written by hand, per ISASpec.** Nothing generates a datapath from
  a C model. Adding an instruction means editing `red_accel.v`.
- **The compiler support is a source patch, not the design's LLVM pass.** It
  shows what a pass *would* have to do (including that the data layout, not the
  instruction selection, is the hard part) but it does not do it automatically.
- **One workload, one core, one memory system.** 256 KiB of 2-cycle SRAM, no
  cache. A core with a different memory hierarchy would shift the balance between
  the software routines and the accelerator's bus traffic.
- **Area has no PDK behind it.** iCE40 LUT4 counts and technology-independent
  cell counts are real synthesis results and are comparable *between* the two
  cores, which is what the overhead figure needs; they are not µm² and say
  nothing about timing or power.
- **`red_accel` is not the only possible implementation.** It follows RED's ABI
  literally (load the whole operand block, compute, store the whole output
  block). A different implementation could overlap or stream; the measured
  2 cycles/beat and 28-cycle fixed overhead are properties of this one.
