# RED run_3 — libcrc

- model: `gemini:gemini-3.1-pro-preview` (project `project-160a0199-6b4a-464c-86a`)
- result: **NOT converged**
- A2 rounds: 2
- Gate 0 (workload): PASS 564,295,859 instructions, 100.0% in the project's own code, 0.00% drift
    - libcrc_workload.c: 564,295,859 instructions, 100.0% in the project's own code
    - drives: crc_64_we, crc_16, crc_ccitt_1d0f, crc_32, crc_8, main
- Gate 1 (re-profile ±5%): PASS runs 564295899 vs 564295899 cycles (0.00% vs ±5%)
- Gate 2 (C model vs Spike): — 
- Gate 3 (predicted speedup): — 
- Gate 4 (security): — 
- coverage: 99.88% (80% target)
- spec review: not run
- verification edges:

- ISASpec: none (0 instructions)

