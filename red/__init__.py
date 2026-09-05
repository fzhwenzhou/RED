"""RED: An Agentic RISC-V ISA Extension Designer.

Given one application project and one processor core, RED profiles the project,
mines its hot loops, and designs a custom RISC-V instruction extension for them,
as a bounded CHIA graph of ChiaFunction workers.

    A1 Kernel Mining  -> HotLoopReport  (Gate 1: ±5% re-profile)
    A2 ISA Synthesis  -> ISASpec        (link 1: C models built + stress-run)

The ISASpec is the deliverable. The downstream evaluation nodes of the full
design (A3 RTL, A4 LLVM pass, A5 end-to-end speedup) are out of scope here, and
Gate 2 (C model <=> patched Spike ISS) is not implemented — see LIMITATIONS in
README.md.
"""

__all__ = ["state_def", "constants", "db_node", "tools", "nodes", "loop"]
