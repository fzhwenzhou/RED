# Evaluation results for all RED projects

Evaluated 2026-09-23 from the three pinned specs under [`spec/`](spec/). All
current projects now have C-model/RTL differential tests and self-checking
end-to-end PicoRV32 simulations.

| Project | Workload measured | Baseline cycles | Extended cycles | Speedup | Correct |
|---|---|---:|---:|---:|---:|
| micro-ecc | one secp256r1 ECDH exchange | 298,502,758 | 178,231,186 | **1.6748x** | yes |
| matrixmul | one 10x10 float matrix multiply | 1,040,318 | 71,392 | **14.5719x** | yes |
| libcrc | CRC-16/32/64 over one 4 KiB buffer | 852,200 | 405,322 | **2.1025x** | yes |

The matrixmul and libcrc inputs match their generated RED workloads, with the
outer 20,000/2,000 repetition loops removed. Those loops repeat the same kernel
and would only multiply RTL simulation time; initialization and reference checks
are outside the timed region.

The previous micro-ecc `mac96`/`add256`/`sub256` evaluation is preserved in
[`history/2026-09-07.md`](history/2026-09-07.md).

## Verification summary

| Project | Instructions | C-model/RTL vectors | Mismatches | End-to-end signature |
|---|---:|---:|---:|---|
| micro-ecc | 1 | 412 | 0 | shared secret `a941684d…696d08e0` |
| matrixmul | 1 | 412 | 0 | `1f377458` |
| libcrc | 3 | 1,236 | 0 | `a3891e9f8925af24d81f5f8c0000975d` |

Every vector set runs once with distinct input/output blocks and once with them
aliased. The application runs also require `RESULT: PASS`, status zero, and an
identical baseline/extended signature.

## micro-ecc

The current spec replaces one eight-limb `uECC_vli_mult` call with a complete
256x256-to-512-bit multiply. Its iterative RTL reuses one 32x32 multiplier.

| Metric | Baseline | Extended |
|---|---:|---:|
| whole ECDH cycles | 298,502,758 | 178,231,186 |
| `uECC_vli_mult` cycles/call | 9,009 | 656 |
| firmware image | 8,548 B | 8,624 B |
| iCE40 LUT4 | 5,590 | 9,873 |
| iCE40 flip-flops | 3,816 | 6,604 |
| generic cells | 18,089 | 28,682 |

The instruction itself takes 139 cycles in the differential harness and 164
cycles including issue/loop overhead. Explicit operand marshalling raises that
to 577 cycles; the patched function including call/return takes 656.

The multiplication call is 13.73x faster, but multiplication is about 43.5% of
the application, giving the measured 1.6748x whole-program speedup. Gate 3
predicted 11.60x after attributing each of two loops the full function cost and
summing them to 95% coverage.

## matrixmul

`fdot2_custom` performs two IEEE-754 binary32 multiplies followed by two adds in
the exact order specified, canonicalizing NaNs after every operation. The
application groups each pair of terms in the original 10x10 triple loop; 500
custom instructions replace 1,000 software float MAC iterations.

| Metric | Baseline | Extended |
|---|---:|---:|
| 10x10 multiply cycles | 1,040,318 | 71,392 |
| instruction latency | — | 28 cycles |
| result signature | `1f377458` | `1f377458` |

This is an **architectural simulation result**, not a completed area result. The
model uses SystemVerilog `shortreal` for Icarus differential testing and a DPI C
binary32 implementation under Verilator. It gives an explicit four-operation
latency and validates the ABI and application transformation, but it is not
synthesizable. A real floating-point datapath may have different latency and
area. Gate 3 predicted 92.10x; the measured model gives 14.5719x.

## libcrc

The three custom-3 instructions consume 32 bytes per call. Their synthesizable
RTL uses one bit step per cycle, so the measured instruction latencies are 301
cycles for CRC-64 and 297 cycles for CRC-16/32. The baseline uses libcrc's real
lookup-table implementations; the one-time CRC-16 table initialization is
warmed before timing.

| Metric | Baseline | Extended | Change |
|---|---:|---:|---:|
| three CRCs over 4 KiB | 852,200 | 405,322 | **2.1025x** |
| iCE40 LUT4 | 5,582 | 6,585 | +18.0% |
| iCE40 flip-flops | 3,816 | 7,052 | +84.8% |
| generic cells | 18,089 | 20,382 | +12.7% |
| accelerator alone | — | 1,002 LUT4 / 2,206 cells | — |

Gate 3 predicted 14.71x. The RTL result is lower because the spec's C models
perform 256 bit steps per 32-byte block while the cost model materially
underprices those operations, and because operand packing is paid at every call.

## Does the performance gate predict what the RTL measures?

This is the question `eval/` exists to answer about RED itself, and for a long
time nobody asked it. Gate 3 over-predicted all three shipped extensions by
**6.3x to 7.0x** — a consistency that invites a single explanation and does not
have one. `scripts/reprice.py` re-prices each shipped `ISASpec` with `red.cost`
on that run's own frozen `HotLoopReport` and `CoreProfile` and prints the
prediction beside the measurement above:

```bash
./.venv/bin/python eval/scripts/reprice.py
```

| workload | RTL measured | Gate 3, as it shipped | Gate 3, after the fixes |
|---|---:|---:|---:|
| micro-ecc | **1.67x** | 11.60x (6.9x over) | **1.81x** (1.08x) |
| libcrc | **2.10x** | 14.71x (7.0x over) | 1.09x (0.52x) |
| matrixmul | **14.57x** | 92.10x (6.3x over) | 1.02x (0.07x) |

Provenance, because it matters for what can be reproduced. The micro-ecc row is
live: `eval/spec/micro-ecc.report.json` and `.core.json` are pinned beside the
spec, so `reprice.py` regenerates **1.81x** on demand, and a later run that
re-derived the same design (16 words, 64 MACs, one invocation per call — the
same machine, spelled `secp256r1_mult`) reported the same 1.81x from its own
freshly mined profile. The libcrc and matrixmul figures were measured on the
runs that shipped those pinned specifications, whose run directories have since
been cleared; later runs of those two projects produced *different* instruction
shapes, which the RTL here does not implement, so `reprice.py` marks them "not
comparable" rather than quietly pricing a different design against these
measurements.

Four causes were separated, three of them defects in the model:

1. **Gate 3 never re-ran on what shipped.** Every other gate is re-run when the
   artifact changes under it — link 1 after every repair, Gate 4 on the final
   spec — but Gate 3 ran before the review and was re-priced only after a
   *security* revision. All three runs were revised after it last ran. It is now
   re-priced on the shipped specification, which is the whole of micro-ecc's
   error: 11.60x was a stale number for a design the review had already merged.
2. **Coverage was counted twice.** A1 mines several regions of one kernel and
   gives each the function's whole cycle share; micro-ecc's summed to 143% and
   the gate credited 94.6% of the application to a design replacing two halves
   of one function. Shares are now counted once per function.
3. **Bit-serial work was priced as multiplies.** libcrc's models perform 256
   sequential bit steps per block and the RTL runs one per cycle, measuring
   297–301 cycles where the multiply-rate model predicted ~112. Work the
   declared `mac_ops` cannot account for is now charged as sequential logic
   steps, calibrated on this machine like the multiply rate already was.
4. **`invocations` is a declaration nothing can verify.** libcrc shipped 2 where
   a 4 KiB buffer needs 128; matrixmul shipped 4 where a 10x10 multiply needs
   500. The obvious estimator — kernel instructions per call over model
   instructions per invocation — is *biased*, because the two sides are
   different implementations of the same work: it reads 31 for libcrc's true
   128 and 147 for matrixmul's 4. Substituting it moved the prediction by an
   order of magnitude in both directions, so the gate now **reports the
   inconsistency** instead of guessing, and the redesign edge asks the designer
   to fix the field.

What that leaves: when the declarations are right, the model is accurate to
**8%** (micro-ecc). When `invocations` is wrong the prediction is wrong, and the
gate now says so in its own verdict rather than silently compensating. The two
remaining errors are both traceable to that one field — and correcting it by
hand does not rescue them either (libcrc 5.39x, matrixmul 0.87x), because
`marshal_words` is a second unverifiable declaration whose weight grows once
marshalling is charged at the rate the hardware showed. **Gate 3's accuracy is
bounded by two fields the designer declares and RED cannot measure**, which is a
sharper statement than "the model is optimistic" and a more useful one.

The direction of the error also changed, and that matters for a gate: it was
uniformly optimistic, shipping designs believed to be ~7x better than they are.

## Reproduction

```bash
./.venv/bin/python eval/scripts/reprice.py   # gate 3 prediction vs the RTL below
bash eval/scripts/run_all.sh quick  # all link tests; micro-ecc skips full ECDH
bash eval/scripts/run_all.sh        # all projects including full micro-ecc ECDH
bash eval/scripts/area_all.sh       # micro-ecc + libcrc synthesis
```

Individual runs:

```bash
bash eval/scripts/run_eval.sh                    # micro-ecc
bash eval/scripts/run_project_eval.sh matrixmul
bash eval/scripts/run_project_eval.sh libcrc
```

Generated binaries and complete logs are under ignored `eval/build/`. Tool
versions: Verilator 5.051-devel, Icarus Verilog 14.0-devel, Yosys 0.68+136, and
riscv64-unknown-elf-gcc 13.2.0.
