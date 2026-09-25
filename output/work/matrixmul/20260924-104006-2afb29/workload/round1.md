# Gate 0 — workload measurement

- instructions executed: **38,356,433** (minimum 50,000,000)
- of which in the project's own code: **99.6%** (minimum 60%)
- run-to-run drift: **0.00%** (limit 2%)
- exit code: 0

Hottest functions of the project that your harness reached:

- `multiply`
- `main`

**Gate 0 fails.**

it executes only 38,356,433 instructions, and a workload needs at least 50,000,000 — you are 1.3x short. **Multiply the work by at least 3x**: raise the iteration count, the buffer size or the problem dimension, whichever scales the kernel rather than the setup. Overshooting is free; the profile just needs to be dominated by the kernel.