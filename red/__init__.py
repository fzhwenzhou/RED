"""RED: An Agentic RISC-V ISA Extension Designer.

Given one application project and one processor core, RED profiles the project,
mines its hot loops, and designs a custom RISC-V instruction extension for
them, as a bounded graph of agentic stages and mechanical gates that runs on
one machine.

    AW Workload       -> a representative harness  (Gate 0: is it a workload?)
    A1 Kernel Mining  -> HotLoopReport             (Gate 1: ±5% re-profile)
    A2 ISA Synthesis  -> ISASpec                   (link 1: C models stress-run)
                                                   (Gate 2: C model <=> Spike)
                                                   (Gate 3: measured speedup)
                                                   (Gate 4: security)

The ISASpec is the deliverable. The downstream nodes of the full design (A3
RTL, A4 LLVM pass, A5 end-to-end speedup) are out of scope here — see
LIMITATIONS in README.md; `eval/` scores RED's predictions against hand-written
RTL after the fact.

Every agentic stage is a `claude -p` subprocess over sealed MCP tools
(red.backend, red.localtool); every gate is a program that measures something
(red.iss, red.cost, red.security, red.workload). Nothing in the loop takes an
agent's word for a result a machine can check.
"""

__all__ = [
    "backend", "callgrind", "constants", "cost", "db_node", "graph", "iss",
    "localtool", "loop", "nodes", "review", "security", "state_def", "tools",
    "workload",
]
