"""Mechanical structural review of an ISASpec — is this an ISA extension?

RED's agents reliably produce an instruction that *works*: it passes the
differential test against Spike, it survives the security vectors, and it
predicts a large speedup. What they also reliably produce is a single
instruction that reads a block of memory, performs an entire kernel, and
writes a block back. The matrixmul run that prompted this module shipped one
``fmmac.s`` taking a 200-word input block and a 200-word output block: a
5x10x10 matrix multiply, start to finish, behind one opcode.

That design is not wrong so much as it is not an *instruction set*. An
extension is a small set of composable operations plus whatever architectural
state they need, and the software loop keeps the structure of the computation.
The two specs worth imitating both look like that:

* The RISC-V scalar crypto extensions are almost entirely two-register
  primitives — ``sha256sig0 rd, rs1``, ``xperm8 rd, rs1, rs2``,
  ``aes32esmi rd, rs1, rs2, bs``. No instruction performs a whole hash or a
  whole block cipher; each performs one step that the base ISA happens to be
  bad at, and the round loop stays in software.
* The XuanTie matrix extension does take on a whole tile multiply, but it
  earns it by declaring what it costs: named tile and accumulator register
  files, a geometry held in CSRs, and separate configuration, load/store and
  compute instructions. The state is architectural, so it can be reviewed,
  context-switched and reasoned about.

What makes the generated monolith different from the second of those is not
its size — it is that its state is invisible. The 3,200 bits of accumulator
and operand tile that ``fmmac.s`` has to hold appear nowhere in the spec. One
reviewer noticed and filed it as ``major``; nothing blocked, and the spec
shipped.

So the checks here are mechanical and the thresholds are stated in bits, which
is the unit the question is actually about. They produce ordinary
``ReviewFinding``s, merged into the review report alongside the four agent
reviewers, so the existing revise loop acts on them without knowing where they
came from. The agent reviewers are free to disagree with a *style* judgement;
they cannot talk a measured 6,400-bit operand block down to something smaller.
"""

from __future__ import annotations

import re

from . import state_def


REVIEWER = "structure"

# Operand-block thresholds, in 32-bit words.
#
# A 32-bit RISC-V core's entire architectural register file is 32 x 32 = 1,024
# bits, and that is the soft bound: an operand block up to the size of the
# register file is something the caller could plausibly have had in registers,
# and an implementation can stream it without becoming a memory of its own.
# The hard bound is twice that -- past it the unit holds more state than the
# processor it attaches to, and "instruction" has stopped being the right word.
#
# Calibrated against what RED has actually shipped: micro-ecc's 16-word
# multiply and libcrc's 17-word CRC step (16 words of message plus one word of
# running CRC) sit under the soft bound and are not flagged; matrixmul's
# 200-word tile multiply is three times the hard bound.
BLOCK_WORDS_SOFT = 32            # 1,024 bits — the core's whole register file
BLOCK_WORDS_HARD = 64            # 2,048 bits

# An extension is a set. One instruction can be the right answer -- a single
# well-chosen primitive is a fine extension -- but one instruction in `block`
# form carrying a large operand block is the accelerator shape.
MONOLITH_WORDS = BLOCK_WORDS_SOFT

# Groups whose instructions are allowed to address memory at all.
#
# RISC-V is a load/store architecture: memory is touched by loads and stores,
# and every arithmetic instruction reads and writes registers. An instruction
# that takes a memory address, computes, and writes the result back to memory
# is a memory-to-memory ALU operation -- the VAX shape the architecture exists
# to avoid -- however small its operand block is.
#
# This is the rule that was missing while `block` form was the only way to
# carry a wide operand. micro-ecc's shipped design is three such instructions:
# a 16-word multiply, a reduction and a subtract, each reading two memory
# blocks and writing one. The operands genuinely do not fit in two 32-bit
# registers, which is the real constraint -- and the architectural answer to
# it is an extension register file with loads and stores, which is what the
# vector and matrix extensions both do, not a memory-operand multiply.
MEMORY_GROUPS = ("load-store", "move")


def _bits(words: int) -> str:
    return f"{words} words ({words * 32:,} bits)"


# Stable ids for the rules below. They go in each finding's evidence so a
# consumer can tell which rule fired without matching on prose -- the tests
# need that to separate "an operand block larger than the machine" from "a
# memory-operand ALU instruction", which are different objections with
# different fixes.
RULE_BLOCK_TOO_WIDE = "block-too-wide"
RULE_UNDECLARED_BUFFER = "undeclared-buffer"
RULE_LOAD_STORE = "load-store"
RULE_MONOLITH = "monolith"
RULE_UNREACHABLE_STATE = "unreachable-state"
RULE_UNREACHABLE_CSR = "unreachable-csr"
RULE_STATE_TOO_LARGE = "state-too-large"
RULE_NO_RATIONALE = "no-rationale"


def _finding(severity: str, text: str, instruction: str = "",
             evidence: str = "", fix: str = "", rule: str = "") -> state_def.ReviewFinding:
    return state_def.ReviewFinding(
        reviewer=REVIEWER, severity=severity, finding=text,
        instruction=instruction,
        evidence=(f"[{rule}] " if rule else "") + evidence, fix=fix)


def rule_of(finding) -> str:
    """The rule id a structural finding carries, or "" if it is not one."""
    m = re.match(r"\[([a-z-]+)\]", finding.evidence or "")
    return m.group(1) if m else ""


def check(spec: state_def.ISASpec,
          core: state_def.CoreProfile | None = None) -> list[state_def.ReviewFinding]:
    """Every structural objection to *spec*, worst first.

    An empty list means the extension is shaped like an extension: its
    operands fit the machine, the state it holds is declared, and the work is
    split across instructions the caller composes."""
    out: list[state_def.ReviewFinding] = []
    blocks = [i for i in spec.instructions
              if state_def.normalize_form(i.form) == "block"]
    declared_state = spec.state_bits()
    # CoreProfile.name is sometimes written as a whole sentence; the finding
    # wants the core, not its description.
    core_name = ((core.name if core else "").split("(")[0].strip() or "target")[:40]

    # --- 1. operand blocks larger than the machine ----------------------
    for ins in blocks:
        if ins.words > BLOCK_WORDS_HARD:
            out.append(_finding(
                "blocking",
                f"moves {_bits(ins.words)} in and the same out per invocation. "
                f"That is more state than the whole {core_name} "
                "register file, and an implementation has to hold or buffer it. "
                "This is a DMA engine with an ALU attached, not an instruction.",
                ins.mnemonic,
                evidence=(f"form=block, words={ins.words}; the hard bound is "
                          f"{BLOCK_WORDS_HARD} words ({BLOCK_WORDS_HARD * 32:,} bits)"),
                rule=RULE_BLOCK_TOO_WIDE,
                fix=("Split it. Find the operation inside this kernel that the "
                     "base ISA is actually bad at and give that its own "
                     "instruction, in `reg` form if its operands fit in two "
                     "registers. Let the loop that walks the tile stay in "
                     "software — it is not what is costing the cycles. If the "
                     "block really is irreducible, declare the state it needs "
                     "in `register_files` and give the extension the "
                     "configuration and load/store instructions that make that "
                     "state usable.")))
        elif ins.words > BLOCK_WORDS_SOFT and ins.state_words == 0 and not spec.register_files:
            out.append(_finding(
                "major",
                f"carries {_bits(ins.words)} of operands but the spec declares no "
                "architectural state. An implementation still has to hold them "
                "somewhere; the spec simply does not say so, which puts that "
                "area outside review.",
                ins.mnemonic,
                evidence=(f"form=block, words={ins.words}, state_words=0, "
                          f"register_files=[] — declared state is {declared_state} bits"),
                rule=RULE_UNDECLARED_BUFFER,
                fix=("Either declare the buffer as a register file (with its "
                     "`count`, `width_bits` and how it is context-switched), or "
                     "narrow the operand block so it does not need one.")))

    # --- 1b. load/store discipline --------------------------------------
    for ins in blocks:
        group = state_def.normalize_group(ins.group)
        # `group` is the designer's own label, so the exemption it buys has to
        # be checked against something the designer does not control.
        # `mac_ops` is measured from the C model by red.cost, and an
        # instruction that moves data does not multiply: a `load-store` that
        # declares arithmetic is a compute instruction wearing a load's label,
        # and gets the same finding.
        if group in MEMORY_GROUPS and ins.mac_ops <= 0:
            continue
        mislabelled = group in MEMORY_GROUPS
        out.append(_finding(
            "blocking",
            (f"is labelled {group} but performs {ins.mac_ops} multiplies per "
             "invocation, so it is arithmetic addressing memory, not data "
             "movement. " if mislabelled else
             f"is a {group} instruction that addresses memory: ")
            + "it takes the "
            f"address of a {ins.words}-word input block, computes, and writes "
            "the result back to memory. RISC-V is a load/store architecture — "
            "only loads and stores touch memory, and arithmetic reads and "
            "writes registers. A memory-operand ALU instruction is the shape "
            "the architecture exists to avoid, whatever its operand block "
            "costs.",
            ins.mnemonic,
            evidence=(f"form=block, group={group}, words={ins.words}, "
                      f"mac_ops={ins.mac_ops}; memory is addressable only from "
                      f"{' and '.join(MEMORY_GROUPS)} instructions"),
            rule=RULE_LOAD_STORE,
            fix=("If one invocation's operands fit in two 32-bit registers, "
                 "use form='reg' and the question disappears. If they do not "
                 "— a 256-bit field element does not — then the extension "
                 "needs somewhere to put them, and that is an architectural "
                 "register file: declare it in `register_files`, add "
                 "`load-store` instructions that move operands between memory "
                 "and it, and make this instruction a `compute` that reads and "
                 "writes the file and never names an address. That is what the "
                 "vector and matrix extensions do, and it is the only shape in "
                 "which a wide operand is a RISC operand.")))

    # --- 2. the shape of the extension as a whole -----------------------
    if len(spec.instructions) == 1 and blocks and blocks[0].words >= MONOLITH_WORDS:
        ins = blocks[0]
        out.append(_finding(
            "major",
            f"the extension is one instruction, and that instruction takes a "
            f"{_bits(ins.words)} operand block and returns the finished result. "
            "Nothing about the computation is left for the caller to compose, "
            "so this is an accelerator reached through the opcode space rather "
            "than an instruction set.",
            ins.mnemonic,
            evidence=(f"1 instruction, groups={spec.groups()}, "
                      f"form=block, words={ins.words}"),
            rule=RULE_MONOLITH,
            fix=("Decompose it. Two or three primitives the hot loop calls in "
                 "sequence are worth more than one call that replaces the loop: "
                 "they are reusable by the rest of the application, they are "
                 "far cheaper to build, and each one is separately testable.")))

    # --- 3. state that is declared but no instruction can reach -------
    touched: set[str] = set()
    for ins in spec.instructions:
        touched |= {str(x) for x in (ins.reads_state or [])}
        touched |= {str(x) for x in (ins.writes_state or [])}
    for rf in spec.register_files:
        if rf.name not in touched:
            out.append(_finding(
                "major",
                f"register file {rf.name!r} ({rf.count} x {rf.width_bits} bits) is "
                "declared but no instruction names it in `reads_state` or "
                "`writes_state`, so nothing can put a value into it or get one "
                "out. Architectural state with no instruction that reaches it is "
                "area the design cannot use.",
                evidence=f"register_files[{rf.name}], referenced by 0 instructions",
                rule=RULE_UNREACHABLE_STATE,
                fix=("Either give the extension the load/store and configuration "
                     "instructions that make the file usable — this is what the "
                     "XuanTie matrix extension spends most of its opcodes on — "
                     "or drop the declaration.")))
    for csr in spec.csrs:
        if csr.name not in touched:
            out.append(_finding(
                "minor",
                f"CSR {csr.name!r} at {csr.address} is declared but no instruction "
                "reads or writes it. A CSR only earns its encoding when it is "
                "read by a whole loop rather than carried in each instruction.",
                evidence=f"csrs[{csr.name}], referenced by 0 instructions",
                rule=RULE_UNREACHABLE_CSR))

    # --- 4. state the core cannot afford --------------------------------
    if core is not None and declared_state:
        # 32 architectural registers is the only state figure every RV32 core
        # has, so it is the one comparison that does not need the RTL.
        base = 32 * 32
        if declared_state > 4 * base:
            out.append(_finding(
                "major",
                f"the extension declares {declared_state:,} bits of architectural "
                f"state, {declared_state / base:.1f}x the core's own 1,024-bit "
                "register file. On a core this size that is the dominant cost of "
                "the design, and it is paid on every context switch.",
                evidence=(f"register_files={[(r.name, r.count, r.width_bits) for r in spec.register_files]}, "
                          f"csrs={[c.name for c in spec.csrs]}"),
                rule=RULE_STATE_TOO_LARGE,
                fix=("Reduce the geometry, or say in `state_note` why this core "
                     "can carry it.")))

    # --- 5. an extension that cannot say why it is shaped this way -------
    if len(spec.instructions) > 1 and not spec.rationale:
        out.append(_finding(
            "minor",
            "the spec does not say why the work is split across these "
            "instructions rather than any other way. That rationale is what a "
            "reader needs to judge the decomposition.",
            fix="Fill in `rationale`.", rule=RULE_NO_RATIONALE))

    order = {"blocking": 0, "major": 1, "minor": 2}
    out.sort(key=lambda f: order.get(f.severity, 3))
    return out


def summary(spec: state_def.ISASpec) -> str:
    """One line describing the extension's shape, for the run log."""
    forms: dict[str, int] = {}
    for ins in spec.instructions:
        f = state_def.normalize_form(ins.form) or "block"
        forms[f] = forms.get(f, 0) + 1
    shape = ", ".join(f"{n} {f}" for f, n in sorted(forms.items()))
    widest = max((i.words for i in spec.instructions), default=0)
    bits = spec.state_bits()
    return (f"{len(spec.instructions)} instruction(s) [{shape}], "
            f"widest operand block {widest} words, "
            f"declared state {bits:,} bits")
