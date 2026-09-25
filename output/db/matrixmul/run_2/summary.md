# RED run_2 — matrixmul

- model: `gemini:gemini-3.1-pro-preview` (project `project-160a0199-6b4a-464c-86a`)
- result: **NOT converged**
- A2 rounds: 2
- Gate 0 (workload): PASS 95,700,724 instructions, 99.9% in the project's own code, 0.00% drift
    - matrixmul_workload.c: 95,700,724 instructions, 99.9% in the project's own code
    - drives: multiply, main
- Gate 1 (re-profile ±5%): PASS runs 95700771 vs 95700771 cycles (0.00% vs ±5%)
- Gate 2 (C model vs Spike): — 
- Gate 3 (predicted speedup): — 
- Gate 4 (security): — 
- coverage: 99.75% (80% target)
- spec review: not run
- verification edges:

- ISASpec: none (0 instructions)

