"""RED: An Agentic RISC-V ISA Extension Designer.

A full-stack DSA co-design loop that discovers custom RISC-V instructions for
the micro-ecc ECDH/ECDSA secp256r1 workload, targeting PicoRV32 + PCPI + LLVM,
built as a bounded cyclic CHIA graph of ChiaFunction workers.

    A1 Kernel Mining  -> HotLoopReport  (Gate 1: ±5% re-profile)
    A2 ISA Synthesis  -> ISASpec        (Gate 2: C <=> Spike, 10^5 vectors)
    A3 RTL eval (PicoRV32 + PCPI)
    A4 compiler eval (LLVM pass)
    A5 e2e eval (speedup)
    verification chain: kernel<=>C<=>Spike<=>RTL<=>e2e
"""

__all__ = ["state_def", "constants", "db_node", "tools", "nodes", "loop"]
