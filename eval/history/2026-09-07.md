# Outcome: does RED's extension actually accelerate micro-ecc?

RED's A1+A2 produce a *semantically verified* ISASpec — Gate 1, link 1 and Gate 2
all pass. None of those gates asks whether the instructions can be **built** or
whether using them makes the application **faster**. This is that measurement,
end to end, on real hardware RTL.

**Headline: no. The extension as designed is 2.0% slower than the baseline
RISC-V binary and costs 74% more core area.** The reason is measured, specific,
and fixable, and it is a property of RED's operand ABI rather than of the
instructions' arithmetic. Details below.

---

## 1. What was built

| Step | Artifact | Status |
|---|---|---|
| A3-lite: RTL for the extension | [rtl/red_accel.v](rtl/red_accel.v) — PCPI coprocessor, bus master | verified against the ISASpec C models |
| Core integration | [rtl/red_core.v](rtl/red_core.v) — PicoRV32 ± accelerator | same CPU config both ways |
| SoC + measurement | [rtl/red_soc_tb.v](rtl/red_soc_tb.v) — 256 KiB SRAM, MMIO, cycle counter | — |
| A4-lite: compiler support | [patches/micro-ecc-red-ext.patch](patches/micro-ecc-red-ext.patch) — hand-applied, not an LLVM pass | applied to a copy; submodule untouched |
| A5-lite: end-to-end eval | [firmware/bench.c](firmware/bench.c) — one full ECDH exchange | correctness checked, not just timed |

The C-model ⇔ RTL link (link 3 of the design's chain) is closed by
[rtl/tb_accel.v](rtl/tb_accel.v): **1236 vectors, 0 mismatches**, run twice — once
with distinct input/output blocks and once with them aliased, which is how
`uECC_vli_mult` uses `mac96`.

## 2. End-to-end result

One complete ECDH key exchange over secp256r1 (2 × `uECC_make_key` +
2 × `uECC_shared_secret`), micro-ecc compiled `-Os` for rv32im, deterministic RNG
so both binaries do identical work:

| | baseline | extended | ratio |
|---|---|---|---|
| PicoRV32 config | FAST_MUL, DIV, counters | identical + `red_accel` | — |
| **cycles (whole application)** | **298,502,758** | **304,410,162** | **0.98× (2.0% slower)** |
| shared secrets agree | yes | yes | — |
| image size | 8,548 B | 8,512 B | — |

Both runs print the same shared secret
(`a941684d…696d08e0`), so the extended binary is *correct* — this is a real
slowdown, not a broken run being timed.

## 3. Area

yosys, flattened, no PDK needed. iCE40 is PicoRV32's own published area target,
so these numbers are comparable to upstream's.

| metric | baseline | extended | overhead |
|---|---|---|---|
| iCE40 LUT4 | 5,582 | 9,696 | **+73.7%** |
| iCE40 flip-flops | 3,816 | 6,308 | +65.3% |
| generic cells (tech-independent) | 18,089 | 29,122 | +61.0% |
| `red_accel` alone | — | 4,001 LUT4 | — |

The accelerator is large for what it computes because RED's ABI forces it to be
a **bus master with a 16-word operand buffer**: 512 bits of registers, a 256-bit
adder/subtractor, a 32×32 multiplier (duplicating the one already in the CPU's
`ENABLE_FAST_MUL` unit), and a 16:1 32-bit output mux.

**Cost-efficiency: 0.98× performance for 1.74× area.** The extension is not
cost-effective on this platform.

## 4. Why — measured per operation

Every number below is measured on the RTL by
[firmware/microbench.c](firmware/microbench.c) (mean of 256 runs):

| operation | cycles | vs software |
|---|---|---|
| software `uECC_vli_add` (8 words) | 769 | — |
| `add256`, operands already adjacent | 92 | **8.4× faster** |
| `add256` + marshalling operands into RED's block | 774 | 0.99× (break-even) |
| software `uECC_vli_sub` | 769 | — |
| `sub256`, operands already adjacent | 92 | **8.4× faster** |
| software `uECC_vli_mult` (64 MACs) | 9,009 | — |
| `uECC_vli_mult` patched to use `mac96` | 9,399 | **0.96× (slower)** |
| `mac96`, operands already in place | 48 | — |
| software MAC, accumulator in registers | 79 | 1.65× faster than software, in isolation |

Three things follow, and together they fully explain the end-to-end number.

**(a) The instruction latency is data movement, not arithmetic.** Fitting the two
measured latencies (`mac96` 10 bus beats → 48 cycles; `add256` 32 beats → 92
cycles) gives **2.0 cycles per bus beat and 28 cycles of fixed PCPI/FSM
overhead**. `mac96`'s actual arithmetic is one cycle. RED's ABI — `rs1` = address
of the input block, `rs2` = address of the output block — means every instruction
pays for moving its operands through memory.

**(b) `mac96` wins in isolation and loses in context.** 48 cycles beats the
79-cycle software MAC sequence, but micro-ecc's software inner loop keeps the
96-bit accumulator in **three registers across all 64 MACs**, while the ABI
requires it to live in memory. In context: 147 cycles per MAC patched versus 141
software. `uECC_vli_mult` is 42.3% of the ECDH instruction count (callgrind, same
32-bit word size), so 4.3% worse on 43% of the work ⇒ 1.9% worse overall — which
is what the end-to-end run measured (2.0%). The model and the measurement agree.

**(c) `add256`/`sub256` are 8.4× faster but unusable.** Two independent blockers:

- **No carry-out.** micro-ecc's `uECC_vli_add`/`uECC_vli_sub` *return* the
  carry/borrow, and their callers need it (`uECC_vli_modAdd` uses it to decide
  whether to subtract the modulus). The ISASpec's versions discard it. Recovering
  it costs a full 256-bit comparison — more than the instruction saves.
- **Operands must be adjacent.** Where the carry *is* dead (the correction
  subtract in `modAdd`/`modSub`), the operands are separate arrays, and
  marshalling them costs 682 cycles against an instruction that takes 92 — which
  is exactly why the marshalled row above is break-even.

So the patch uses `mac96` only, and deliberately leaves `add256`/`sub256` unused.
That is recorded in the patch header.

## 5. Where the performance actually is

From the measured components, the ceiling for these instructions with a
**register-operand ABI** instead of memory blocks:

- `mac96` would cost the 28-cycle fixed overhead and no bus beats. Per MAC:
  ~2 operand loads + 28 + loop ≈ 58 cycles against 141 software ⇒ `uECC_vli_mult`
  ~2.4× faster.
- Amdahl over the measured 43.5% cycle share: **≈1.34× whole-application
  speedup**, at a much smaller area than 4,001 LUT4 (no 16-word buffer, no bus
  master, no second multiplier).

This is an estimate from measured parts, not a measured result — it is what the
next iteration should be built and measured against.

The single highest-value change to the ISASpec is not a faster datapath but a
**modular** add: micro-ecc's `uECC_vli_modAdd` is "add, then conditionally
subtract the modulus", and its carry-out is only needed *for that decision*. An
instruction that does the whole modular add internally needs no carry-out, keeps
its operands adjacent by construction, and would replace ~1,540 cycles of
software with ~92-150. `uECC_vli_modSub` alone is 6.2% of the workload.

## 6. What this says about RED

The loop verified these instructions thoroughly and they are still the wrong
instructions. Gate 1 (profile reproducibility), link 1 (C models build and
survive 100k vectors under sanitizers) and Gate 2 (C model ⇔ patched Spike over
100k vectors) all passed — and all of them only ever ask *"does this instruction
mean what the spec says?"*. Nothing in A1+A2 asks:

1. can it be implemented within the target's coprocessor interface (PCPI cannot
   touch memory — `red_accel` had to become a bus master),
2. does the application's data layout let it be called without marshalling,
3. does it preserve the values callers need (the dropped carry-out), or
4. is it faster.

Those four questions are exactly what A3-A5 exist for in the design, and this
evaluation is the first time the loop has been told the answer. The proposal's
`speedup < target → re-mine (budgeted)` edge is the mechanism for feeding it
back; the concrete feedback is section 5. **A verified spec is not a good spec**,
and A1+A2 alone cannot tell the difference.

## 7. Reproducing

See [eval/README.md](README.md). Short version:

```bash
bash eval/scripts/area.sh                      # ~1 min   -> area table
./.venv/bin/python eval/scripts/gen_vectors.py # vectors from the ISASpec C models
iverilog -g2005-sv -o eval/build/tb_accel.vvp eval/rtl/tb_accel.v eval/rtl/red_accel.v
vvp eval/build/tb_accel.vvp +vectors=eval/build/vectors.txt   # C model <=> RTL
bash eval/scripts/run_eval.sh                  # ~7 min   -> the end-to-end table
```

Every number in this document comes from those commands on this machine
(Verilator 5.051, yosys 0.68, riscv64-unknown-elf-gcc 13.2.0).
