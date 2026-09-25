# RED run_1 — micro-ecc

- model: `gemini:gemini-3.1-pro-preview` (project `project-160a0199-6b4a-464c-86a`)
- result: **converged (A1+A2)**
- A2 rounds: 3
- Gate 0 (workload): — (operator-supplied harness)
- Gate 1 (re-profile ±5%): PASS runs 10302589354 vs 10302406997 cycles (0.00% vs ±5%)
- Gate 2 (C model vs Spike): PASS 1/1 instructions: C model and patched Spike agree on 100,000 random + corner vectors
- Gate 3 (predicted speedup): PASS predicted 1.79x over 47% of cycles (bar 1.42x; Amdahl caps this workload at 1.90x for the 47.3% it covers, so the bar is 75% of that rather than the 2.00x default, priced on the shipped spec)
- Gate 4 (security): PASS 13 checks over 512 adversarial vectors per model: 0 mechanically confirmed blocking, 0 advisory
- coverage: 100.00% (80% target)
- spec review: clean (callability, benefit, legality)
- verification edges:
- PASS link1: coverage 100.00% >= 80%; 1/1 C models built and survived 100,000 vectors under ASan+UBSan
- PASS link2: 1/1 instructions: C model and patched Spike agree on 100,000 random + corner vectors
- ISASpec: MicroECC secp256r1 Extension (1 instructions)
  - `secp256r1.mult` (custom-0, 16x32b): Performs a 256x256-bit multiplication of two 8-word integers. in[0..7] is the left operand and in[8..15] is the right operand. The 512-bit result is written to out[0..15].
