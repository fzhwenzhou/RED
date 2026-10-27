"""Typed artifacts for the RED loop.

RED's agents communicate through *typed YAML artifacts* that downstream tool
nodes consume mechanically. Everything the loop produces that crosses a node
boundary is a dataclass here, with ``to_yaml``/``from_yaml`` round-trips and a
``validate_*`` function that enforces the falsifiability contract of the
design:

* ``HotLoopReport`` — A1's output. A mined, ranked set of hot loops with a
  cycle-share budget and Gate-1 re-profile reproducibility.
* ``ISASpec`` — A2's output and RED's final deliverable. A closed, executable
  instruction spec (encodings + semantics + C reference models + tests).
* ``GateResult`` — the verdict of one programmatic evaluation edge (Gate 1,
  Gate 2, link 1, link 2); a failure carries a counterexample.

These are persisted verbatim in the durable store (``red.db_node``) for audit
and reuse: every one is written the moment it is produced, so a run killed
halfway still leaves the record of what it had decided.

The format is YAML 1.1, which has one sharp edge worth stating where the
schema meets it: a bare ``0000011`` is an *octal integer*, and every encoding
field in an ``InstructionSpec`` looks exactly like that. :func:`to_yaml`
quotes them. A hand-edited file that leaves them bare loads ``funct7`` as the
integer 9, and :func:`validate_isa_spec` rejects it for not being a string,
which is the outcome to want: a mistyped encoding field should be loud.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field, asdict
from typing import Optional, Union, get_args, get_origin, get_type_hints

import yaml


# ---------------------------------------------------------------------------
# AW — Workload synthesis (what A1 profiles)
# ---------------------------------------------------------------------------

@dataclass
class WorkloadSpec:
    """A representative workload for a project, and the measurements that say
    whether it is one.

    RED's input is "a source repo + representative workloads", and repositories
    routinely ship only the first half. What they do ship is a *unit test*: a
    few short inputs, a lot of printf, and a total dynamic cost thousands of
    times too small to profile. Mining that produces hot "loops" in the dynamic
    loader.

    So the workload is written by an agent that reads the repository, and the
    fields below the line are filled in by Gate 0 from measurement, never by the
    agent: how much work it does, how much of that work is in the project's own
    code, and whether two runs agree.
    """

    project: str                     # the project directory it drives
    name: str = ""                   # e.g. "libcrc/crc32-64KiB"
    rationale: str = ""              # why this is what the library is *for*
    entry: str = ""                  # path of the generated harness .c
    api: list[str] = field(default_factory=list)   # functions it drives
    # --- measured by Gate 0, not claimed by the agent ---------------------
    dynamic_ir: int = 0              # instructions one run executes
    project_ir: int = 0              # ...of which are in the project's own code
    project_share: float = 0.0       # project_ir / dynamic_ir
    runs: list[int] = field(default_factory=list)  # per-run totals
    drift: float = 0.0               # |run1 - run2| / run1
    exit_code: int = 0
    passes_gate0: bool = False
    detail: str = ""


def validate_workload_spec(w: WorkloadSpec) -> None:
    """A WorkloadSpec is only meaningful if it names a project and an entry
    file. Everything that decides whether it is *representative* is measured by
    Gate 0, so there is nothing here to check about it."""
    if not w.project:
        raise ValueError("WorkloadSpec.project is required")
    if not w.entry:
        raise ValueError("WorkloadSpec.entry is required (the harness source)")


# ---------------------------------------------------------------------------
# A1 — Kernel mining
# ---------------------------------------------------------------------------

@dataclass
class HotLoop:
    """One mined hot loop: the loop body, its IR, trip count, cycle share, and
    operand statistics (what S2a's pattern analyzer generalizes from)."""

    loop_id: str                     # stable id, e.g. "uECC_vli_mult::inner"
    source_file: str                 # path within the target project
    function: str                    # enclosing function
    body: str                        # the hot-loop source text
    ir: str                          # LLVM-IR / gimple snippet if available
    trip_count: int = 0              # static trip count (0 = unknown)
    calls: int = 0                   # times called per workload run (profiler)
    dynamic_cycles: int = 0          # measured dynamic cycles (Gate-1 input)
    cycle_share: float = 0.0         # fraction of total dynamic cycles [0, 1]
    # Free-form observations that feed S2a: operand widths, memory access
    # pattern, recurrences. Prose, because that is what the analysis needs —
    # a dict written by an agent is normalized to text at the tool boundary.
    operand_stats: str = ""
    rank: int = 0                    # 1 = hottest, assigned by the loop


@dataclass
class HotLoopReport:
    """A1's typed output. ``coverage`` is the sum of the ranked loops' cycle
    shares — RED requires it to reach ``COVERAGE_TARGET`` (0.80). Gate 1
    passes when independent re-profiles agree within ``GATE1_TOLERANCE``."""

    workload: str                    # e.g. "micro-ecc/ECDH-secp256r1"
    profile_method: str              # "perf+callgrind+IR" | "static" | ...
    loops: list[HotLoop] = field(default_factory=list)
    coverage: float = 0.0
    profile_runs: list[int] = field(default_factory=list)  # dynamic cycle counts
    passes_gate1: bool = False
    profile_command: str = ""

    def ranked(self) -> list[HotLoop]:
        return sorted(self.loops, key=lambda l: (-l.cycle_share, l.rank))


# ---------------------------------------------------------------------------
# A0 — Core analysis (what the extension has to live inside)
# ---------------------------------------------------------------------------

@dataclass
class CoreProfile:
    """The target core as the designer must understand it.

    A2 was designing instructions for an abstract RISC-V and getting them
    architecturally wrong: it specified memory-operand instructions for a core
    whose coprocessor interface cannot address memory, and duplicated a
    multiplier the core already had (eval/RESULTS.md). This artifact is a
    separate agent's reading of the core's own RTL, and it is what makes an
    ISASpec specific to the machine that has to run it.

    The cost fields feed :mod:`red.cost` directly, so a different core changes
    the numbers Gate 3 gates on rather than requiring new code."""

    name: str                        # e.g. "picorv32"
    isa: str = ""                    # e.g. "rv32im"
    microarchitecture: str = ""      # prose: pipeline, issue, hazards
    coprocessor: str = ""            # e.g. "PCPI"
    coprocessor_memory_access: bool = False   # can it address memory itself?
    coprocessor_result: str = ""     # how a result gets back (e.g. "one 32-bit rd")
    memory_interface: str = ""       # e.g. "native valid/ready, 2-cycle SRAM"
    existing_units: list[str] = field(default_factory=list)   # don't duplicate these
    custom_opcode_space: list[str] = field(default_factory=list)
    register_file: str = ""
    # --- cost parameters Gate 3 uses (per core, measured or estimated) -----
    cycles_per_load: float = 0.0
    cycles_per_alu_op: float = 0.0
    coprocessor_issue_overhead: float = 0.0   # cycles before the first operand moves
    cycles_per_word_moved: float = 0.0
    area_note: str = ""              # what "large" means on this core
    constraints: list[str] = field(default_factory=list)   # hard facts to respect
    evidence: list[str] = field(default_factory=list)      # where each claim came from


def validate_core_profile(c: CoreProfile) -> None:
    """A CoreProfile is useful only if it names the core and says how a
    coprocessor attaches — those are the two facts that decide what an
    instruction may even look like."""
    if not c.name:
        raise ValueError("CoreProfile.name is required")
    if not c.coprocessor:
        raise ValueError("CoreProfile must say how a coprocessor attaches "
                         "(`coprocessor`), e.g. the interface's name")
    if not c.isa:
        raise ValueError("CoreProfile.isa is required (e.g. 'rv32im')")
    for fname in ("cycles_per_load", "cycles_per_alu_op",
                  "coprocessor_issue_overhead", "cycles_per_word_moved"):
        v = getattr(c, fname)
        if not isinstance(v, (int, float)) or v < 0:
            raise ValueError(f"CoreProfile.{fname}={v!r} must be a non-negative number")


# ---------------------------------------------------------------------------
# A2 — ISA synthesis
# ---------------------------------------------------------------------------

# How an instruction's operands reach it. Every form is harnessed through the
# same C model signature -- ``void f(const uint32_t in[], uint32_t out[])`` --
# so link 1, the vector generator and the Gate-2 differential test are shared.
# Only the *binding* of that array to machine state differs:
#
#   reg      R-type.  in[0] = x[rs1], in[1] = x[rs2] (0 when src_regs == 1),
#                     out[0] -> x[rd].  No memory traffic.
#   reg_imm  I-type.  in[0] = x[rs1], in[1] = zero-extended imm,
#                     out[0] -> x[rd].  No memory traffic.
#   block    R-type.  rs1 = &in[words], rs2 = &out[words], rd <- 0.
#                     The unit masters the bus for 2*words transfers.
#
# ``reg`` and ``reg_imm`` are what a RISC primitive looks like: a couple of
# registers in, one register out, composed by the surrounding software loop.
# ``block`` is the accelerator form -- legitimate when a kernel really does
# stream a tile, and the wrong answer when it is reached for by default.
FORMS = ("reg", "reg_imm", "block")
REGISTER_FORMS = ("reg", "reg_imm")
DEFAULT_FORM = "block"

# What the forms that pass operands in registers can carry, in `words` of the
# model array: in[0], in[1] -> out[0].
REGISTER_FORM_WORDS = 2

# How wide a `reg_imm` immediate may be. The Gate-2 harness issues one
# instruction per immediate value, so the field is bounded by what can be
# enumerated rather than by the 12 bits the I-type format provides.
MAX_IMM_BITS = 5

# Instruction categories, borrowed from how a real extension is laid out (the
# XuanTie matrix extension separates configuration / load-store / compute /
# element-wise; the scalar crypto extensions are almost entirely `compute`).
# An extension whose instructions all land in one group, and especially one
# that has a single `compute` instruction doing everything, is the monolith
# this field exists to make visible. :mod:`red.structure` reads it.
GROUPS = ("configuration", "load-store", "move", "compute", "reduction", "other")
DEFAULT_GROUP = "compute"


@dataclass
class RegisterFile:
    """An architectural register file the extension adds.

    Real extensions that need working state say so: the XuanTie matrix
    extension names its tile and accumulator register files and gives their
    geometry, because that state is what the OS has to context-switch and what
    the implementer has to build. A spec that keeps 3,200 bits of accumulator
    live inside one instruction and never declares it has hidden its own cost.
    """

    name: str                        # architectural name used in assembly, e.g. "mt"
    count: int                       # how many registers in the file
    width_bits: int                  # bits per register
    purpose: str = ""                # what it holds and why the extension needs it
    reset: str = ""                  # value at reset, or "undefined"
    context: str = ""                # how it is saved/restored across a context switch

    def state_bits(self) -> int:
        return max(self.count, 0) * max(self.width_bits, 0)


# CSR address ranges RISC-V reserves for non-standard use. An extension that
# puts its control register anywhere else aliases an architectural CSR.
CUSTOM_CSR_RANGES = (
    (0x5C0, 0x5FF),   # supervisor, custom read/write
    (0x800, 0x8FF),   # unprivileged, custom read/write
    (0x9C0, 0x9FF),   # supervisor, custom read/write
    (0xBC0, 0xBFF),   # machine, custom read/write
    (0xCC0, 0xCFF),   # unprivileged, custom read-only
    (0xDC0, 0xDFF),   # supervisor, custom read-only
    (0xFC0, 0xFFF),   # machine, custom read-only
)


def in_custom_csr_space(address: int) -> bool:
    return any(lo <= address <= hi for lo, hi in CUSTOM_CSR_RANGES)


@dataclass
class CSRSpec:
    """A control/status register the extension adds.

    Configuration state belongs in a CSR rather than in every instruction's
    operands: it is written once and read by a whole loop, so encoding it into
    each instruction pays for it on every invocation.
    """

    name: str                        # e.g. "xmcfg"
    address: str                     # "0x800" -- must be in CUSTOM_CSR_RANGES
    purpose: str = ""
    width_bits: int = 32
    access: str = "RW"               # RW | RO | WARL | WLRL
    fields: str = ""                 # bitfield layout, as a table or prose
    reset: str = ""

    def address_int(self) -> int:
        """The numeric address, or -1 if it is not parseable."""
        try:
            return int(str(self.address).strip(), 0)
        except (TypeError, ValueError):
            return -1


@dataclass
class InstructionSpec:
    """One designed custom instruction in the custom-0/custom-3 space.

    Four fields describe what the instruction does, and they are *not* four
    drafts of the same text. Each has one job, and writing any of them as a
    restatement of another wastes the only thing it was there to provide:

    * ``semantics``   — prose. What the instruction is for, what the caller has
      to arrange, what is architecturally visible: traps, alignment, ordering,
      state it touches. It is read by a human and by the reviewers. It should
      not re-derive the arithmetic line by line; that is the next field's job.

    * ``pseudocode``  — **the normative operation.** Single-assignment, no
      control flow beyond bounded loops, in the style of the operation blocks
      in the RISC-V specifications. This is the definition of the instruction:
      where the prose and the pseudo-code disagree, the pseudo-code wins, and
      everything below is an *implementation* of it.

    * ``c_model``     — C99, written by the designer (S2b). A verification
      artifact, not part of the published spec. Signature fixed by RED so any
      instruction can be harnessed mechanically::

          void <ident>_model(const uint32_t in[<words>], uint32_t out[<words>])

    * ``spike_model`` — C++, written by a *different* agent (``red/tools.py``
      refuses it from the designer) from the prose, the pseudo-code and the
      encoding alone, and compiled into a Spike extension::

          void <ident>_iss(const uint32_t in[<words>], uint32_t out[<words>])

    The last two look like duplicates of each other and that is deliberate:
    they are a differential pair. Two agents implement the same pseudo-code
    without seeing each other's code, and Gate 2 passes only when they agree
    on 10^5 vectors. Agreement is evidence that ``pseudocode`` is unambiguous;
    a single model, however carefully written, is evidence of nothing. Collapse
    them into one and Gate 2 becomes a test of a function against itself.

    ``<ident>`` is ``mnemonic`` with non-identifier characters replaced by
    ``_``; ``words`` is the length of the model array. How that array binds to
    machine state is the instruction's ``form`` -- see :data:`FORMS` above and
    ``red/iss.py``."""

    name: str                        # e.g. "secp256r1.modadd"
    mnemonic: str                    # assembler mnemonic
    encoding: str                    # 32-bit encoding, custom-0/3 opcode space
    semantics: str                   # prose: purpose, visible effects, traps
    pseudocode: str                  # the normative operation (single-assignment)
    c_model: str = ""                # compilable C99 reference model (link 1)
    spike_model: str = ""            # C++ model for the Spike ISS (Gate 2)
    words: int = 8                   # operand width in 32-bit words
    # --- what it costs and what it buys (Gate 3, see red/cost.py) --------
    # The operand traffic is fixed by `words`, so an instruction only pays for
    # itself when it does enough arithmetic per word moved. These make that
    # claim explicit and checkable instead of leaving it to hope.
    mac_ops: int = 0                 # 32x32 multiplies performed per invocation
    replaces: str = ""               # loop_id (or function) from the HotLoopReport
    invocations: int = 1             # times this runs per call of that loop
    marshal_words: int = 0           # words the caller must copy per invocation
    operands: list[str] = field(default_factory=list)   # rd, rs1, rs2
    funct3: str = ""
    funct7: str = ""
    opcode: str = ""                 # e.g. "custom-0"
    # --- shape (see FORMS) ------------------------------------------------
    form: str = DEFAULT_FORM         # "reg" | "reg_imm" | "block"
    src_regs: int = 2                # source registers the form reads (reg: 1 or 2)
    imm_bits: int = 0                # reg_imm only: width of the immediate field
    group: str = DEFAULT_GROUP       # see GROUPS -- what role it plays in the family
    # --- architectural state it touches (names from ISASpec) --------------
    # A `block` instruction may carry live register-file contents in the first
    # `state_words` of in[] and the updated contents in the first `state_words`
    # of out[]. That keeps every model a pure function of its input array --
    # which is what link 1 and Gate 2 can actually test -- while still letting
    # the spec describe a persistent register file. Register-form instructions
    # must be pure: they have nowhere to thread state through.
    reads_state: list[str] = field(default_factory=list)
    writes_state: list[str] = field(default_factory=list)
    state_words: int = 0


@dataclass
class ISASpec:
    """A2's typed output and RED's final deliverable — an executable ISA spec:
    encodings, prose + pseudo-code semantics, and a compilable C reference model
    per instruction, plus the vectors link 1 exercised them on."""

    name: str                        # extension name, e.g. "RED-secp256r1"
    version: str = "0.1.0"
    description: str = ""
    base_isa: str = ""               # what it extends, e.g. "rv32im"
    rationale: str = ""              # why the work is split across these instructions
    # --- architectural state the extension adds ---------------------------
    # Empty is a legitimate answer, and the best one when the instructions are
    # pure register-to-register primitives. What is not legitimate is holding
    # state and not saying so.
    register_files: list[RegisterFile] = field(default_factory=list)
    csrs: list[CSRSpec] = field(default_factory=list)
    state_note: str = ""             # context switch, traps, interaction with the base ISA
    instructions: list[InstructionSpec] = field(default_factory=list)
    encodings: list[str] = field(default_factory=list)   # machine-encoded lines
    tests: list[str] = field(default_factory=list)       # link-1 vector summary
    link1_vectors: int = 0           # vectors each C model was run on
    gate2_vectors: int = 0           # vectors the C model and Spike agreed on
    passes_link1: bool = False       # set by the loop from link 1, never by the agent
    passes_gate2: bool = False       # set by the loop from Gate 2, never by the agent
    critic_rounds: int = 0           # S2c reject/revise rounds (<= 8)

    def state_bits(self) -> int:
        """Bits of architectural state the extension declares. Compared
        against the core's own state by :mod:`red.structure`."""
        return (sum(rf.state_bits() for rf in self.register_files)
                + sum(max(c.width_bits, 0) for c in self.csrs))

    def state_words(self) -> int:
        """The declared architectural state as a count of 32-bit words.

        This is the length of the ``xs[]`` array both executable models
        receive when the extension holds state. It is derived from the
        declaration rather than chosen, so the two models cannot disagree
        about how big the register file is -- which would otherwise be a
        Gate-2 failure that says nothing about the instruction."""
        return (self.state_bits() + 31) // 32

    def holds_state(self) -> bool:
        """Whether the models take the persistent-state parameter."""
        return self.state_words() > 0

    def groups(self) -> dict[str, int]:
        """How many instructions fall in each group, worst-first by count."""
        out: dict[str, int] = {}
        for ins in self.instructions:
            out[ins.group or DEFAULT_GROUP] = out.get(ins.group or DEFAULT_GROUP, 0) + 1
        return out


# ---------------------------------------------------------------------------
# Gates / verification chain
# ---------------------------------------------------------------------------

@dataclass
class GateResult:
    """The verdict of one programmatic evaluation edge. A failure carries a
    counterexample that rejects the instruction back to S2b/S2c."""

    gate: str                        # "gate1" | "gate2" | "link1" | "link2"
    passed: bool
    detail: str = ""
    counterexample: Optional[str] = None   # e.g. failing vector or divergence


# ---------------------------------------------------------------------------
# Spec review (the sub-agents in red.review)
# ---------------------------------------------------------------------------

@dataclass
class ReviewFinding:
    """One reviewer's objection to the spec.

    ``blocking`` means the instruction cannot serve its purpose as written —
    unbuildable on the target, uncallable by the application, or slower than
    what it replaces. Those are the ones fed back to the designer."""

    reviewer: str                    # implementability | callability | benefit | legality
    severity: str                    # blocking | major | minor
    finding: str
    instruction: str = ""            # mnemonic, or "" for a spec-wide finding
    evidence: str = ""
    fix: str = ""


@dataclass
class ReviewReport:
    """What the reviewers collectively found, ordered worst-first."""

    reviewers: list[str] = field(default_factory=list)
    findings: list[ReviewFinding] = field(default_factory=list)

    def blocking(self) -> int:
        return sum(1 for f in self.findings if f.severity == "blocking")

    def actionable(self) -> list[ReviewFinding]:
        return [f for f in self.findings if f.severity in ("blocking", "major")]


# ---------------------------------------------------------------------------
# Security (Gate 4 — red.security)
# ---------------------------------------------------------------------------

@dataclass
class SecurityFinding:
    """One security defect in the spec, and *how it is known*.

    ``confidence`` is the field that matters, because it decides whether the
    finding can block:

    * ``confirmed`` — a tool observed the failure. AddressSanitizer trapped an
      out-of-bounds write, the model returned a different answer for the same
      input, callgrind counted different instruction counts for two operand
      values, memcheck saw a branch on a secret. There is a reproducible
      artifact behind it.
    * ``derived``   — arithmetic or decoding over the spec's own declared
      fields, with no judgement in between: an encoding whose opcode bits are
      not in the custom space, two instructions in one encoding slot, a stall
      longer than the interrupt-latency bound.
    * ``heuristic`` — a pattern match over the model's source text (the taint
      scan for secret-dependent control flow). Real signal, but it can be
      wrong, so it advises rather than blocks.
    * ``agent``     — the security sub-agent's own reading. It sees what no
      checker can, and it can also hallucinate; it is never trusted on its own
      word. When it wants a finding to count, it proposes a concrete input
      vector, the loop *executes* it, and the resulting evidence comes back as
      ``confirmed`` through the mechanical path instead.
    """

    check: str                       # check id, e.g. "memory.bounds"
    severity: str                    # blocking | major | minor
    confidence: str                  # confirmed | derived | heuristic | agent
    finding: str
    instruction: str = ""            # mnemonic, or "" for a spec-wide finding
    evidence: str = ""               # the tool output, verbatim where short
    fix: str = ""

    def is_mechanical(self) -> bool:
        return self.confidence in ("confirmed", "derived")


@dataclass
class SecurityReport:
    """What Gate 4 found, plus what it could not check and why.

    ``checks_skipped`` is not bookkeeping: a check that did not run is not a
    check that passed, and a security report that quietly omits the checks its
    host could not perform is worse than no report. The loop prints them and
    the gate's detail names them.
    """

    checks_run: list[str] = field(default_factory=list)
    checks_skipped: list[str] = field(default_factory=list)   # "id: reason"
    findings: list[SecurityFinding] = field(default_factory=list)
    vectors: int = 0                 # vectors the batteries executed per model

    def blocking(self) -> int:
        return sum(1 for f in self.findings if f.severity == "blocking")

    def mechanical_blocking(self) -> list[SecurityFinding]:
        """The blocking findings a machine stands behind — the ones that decide
        the gate. An agent's unsupported assertion never appears here."""
        return [f for f in self.findings
                if f.severity == "blocking" and f.is_mechanical()]

    def actionable(self) -> list[SecurityFinding]:
        return [f for f in self.findings if f.severity in ("blocking", "major")]


# ---------------------------------------------------------------------------
# YAML round-trip (typed-artifact persistence)
# ---------------------------------------------------------------------------

class _Dumper(yaml.SafeDumper):
    """RED's YAML 1.1 emitter: block scalars for source, quotes where 1.1 bites."""


def _represent_str(dumper, data):
    """Multi-line strings as block scalars, everything else as PyYAML sees fit.

    The block style is what makes an artifact readable: a ``c_model`` comes
    out as C, indented under ``c_model: |``. PyYAML declines block style on
    its own for the strings it cannot represent that way (a line with trailing
    whitespace, an embedded tab or CR) and falls back to a quoted scalar, so
    this never loses a byte. The round-trip is asserted in
    tests/test_env.py rather than assumed.
    """
    style = "|" if "\n" in data else None
    return dumper.represent_scalar("tag:yaml.org,2002:str", data, style=style)


def _represent_unknown(dumper, data):
    """Last resort for a type YAML has no tag for.

    The loop persists a few plain dicts it assembles itself, and one
    surprising value in one of them should not lose the whole artifact.
    """
    return dumper.represent_scalar("tag:yaml.org,2002:str", str(data))


_Dumper.add_representer(str, _represent_str)
_Dumper.add_multi_representer(object, _represent_unknown)


def to_yaml(obj) -> str:
    """Serialize a typed artifact (or a list of them, or a plain dict) to YAML.

    The loop also persists non-dataclass payloads (e.g. the A1 profile dict and
    the list-of-GateResult chain), so this falls back to dumping the object as
    given for anything that is not a dataclass instance.

    ``width`` is set absurdly high on purpose. PyYAML line-wraps long plain
    scalars by default, and a reloaded folded scalar is not byte-identical to
    the original when it contained runs of spaces. The artifacts carry whole
    paragraphs of prose semantics, so wrapping is off rather than risked.
    """
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        payload = asdict(obj)
    elif isinstance(obj, list):
        payload = [asdict(o) if dataclasses.is_dataclass(o) and not isinstance(o, type)
                   else o for o in obj]
    else:
        payload = obj
    return yaml.dump(payload, Dumper=_Dumper, sort_keys=True,
                     allow_unicode=True, default_flow_style=False,
                     width=1 << 20)


def _convert(typ, val):
    """Recursively coerce *val* to *typ* (a dataclass, list[...], dict, or scalar).

    ``asdict`` flattens nested dataclasses into dicts, so ``from_yaml`` must
    rebuild them — a shallow ``cls(**loads(data))`` would leave
    ``loops`` / ``instructions`` as plain dicts and break attribute access
    downstream."""
    if val is None:
        return None
    origin = get_origin(typ)
    if origin is list:
        (item_t,) = get_args(typ)
        return [_convert(item_t, v) for v in val]
    if origin is Union:
        # Optional[X] == Union[X, NoneType]
        for arg in get_args(typ):
            if arg is type(None):
                continue
            return _convert(arg, val)
    if dataclasses.is_dataclass(typ) and isinstance(val, dict):
        return _from_dict(typ, val)
    return val  # dict, int, str, float, bool ...


def _from_dict(cls, d: dict):
    hints = get_type_hints(cls)
    kwargs = {}
    for name, val in d.items():
        kwargs[name] = _convert(hints[name], val) if name in hints else val
    return cls(**kwargs)


def _parse(data: str):
    """Parse YAML, reporting a malformed document as a ValueError.

    Callers all the way up handle ``ValueError`` as "the artifact was not
    well formed" and have a repair edge for it. Letting the parser's own
    exception type out would route a typo in a draft to the traceback handler
    instead.
    """
    try:
        return yaml.safe_load(data)
    except yaml.YAMLError as exc:
        raise ValueError(f"malformed YAML: {exc}") from exc


def from_yaml(data: str, cls):
    """Rebuild a typed artifact from YAML produced by :func:`to_yaml` (nested
    dataclasses included)."""
    return _from_dict(cls, _parse(data))


def loads(data: str):
    """Parse a YAML document to plain Python, with no schema attached."""
    return _parse(data)


# ---------------------------------------------------------------------------
# Validation — the falsifiability contract
# ---------------------------------------------------------------------------

def validate_hot_loop_report(r: HotLoopReport) -> None:
    """A HotLoopReport is only valid if it names a workload and its coverage
    is a well-formed fraction. Gate-1 fields are checked by the loop, not here."""
    if not r.workload:
        raise ValueError("HotLoopReport.workload is required")
    if not (0.0 <= r.coverage <= 1.0):
        raise ValueError(f"coverage {r.coverage!r} out of [0, 1]")
    for loop in r.loops:
        if not loop.loop_id or not loop.function:
            raise ValueError(f"loop {loop!r} missing loop_id/function")
        if not (0.0 <= loop.cycle_share <= 1.0):
            raise ValueError(f"loop {loop.loop_id} cycle_share out of [0, 1]")


def c_identifier(mnemonic: str) -> str:
    """The C identifier link 1's harness calls for *mnemonic*: every character
    that cannot appear in a C identifier becomes ``_`` (``secp256r1.modadd`` ->
    ``secp256r1_modadd``). Both the harness generator and the prompt that tells
    the designer what to name its function go through this, so they cannot
    drift apart."""
    ident = "".join(c if c.isalnum() or c == "_" else "_" for c in mnemonic)
    return ident if not ident[:1].isdigit() else f"_{ident}"


# The custom opcode space RED designs into (RISC-V reserves custom-0..custom-3).
CUSTOM_OPCODES = ("custom-0", "custom-1", "custom-2", "custom-3")

# The actual 7-bit opcode each name stands for. RISC-V reserves exactly these
# four for non-standard extensions; an encoding outside them aliases a real
# instruction (red/security.py decodes this at Gate 4).
CUSTOM_OPCODE_BITS = {"custom-0": "0001011", "custom-1": "0101011",
                      "custom-2": "1011011", "custom-3": "1111011"}


def normalize_opcode(value: str) -> str:
    """Canonicalize an opcode spelling to ``custom-N``, or "" if it is not one.

    Agents write ``custom0`` / ``CUSTOM_1`` / ``custom-2`` interchangeably; the
    spec is only useful downstream if the field is one canonical form."""
    token = str(value).strip().lower().replace("_", "-").replace(" ", "")
    if token in CUSTOM_OPCODES:
        return token
    if token.startswith("custom") and token[6:].lstrip("-").isdigit():
        n = token[6:].lstrip("-")
        return f"custom-{n}" if f"custom-{n}" in CUSTOM_OPCODES else ""
    return ""


def normalize_form(value: str) -> str:
    """Canonicalize an operand-form spelling, or "" if it is not one.

    Agents write ``reg-imm`` / ``REG_IMM`` / ``register`` interchangeably."""
    token = str(value).strip().lower().replace("-", "_").replace(" ", "")
    if token in ("register", "r", "rtype", "r_type"):
        token = "reg"
    if token in ("regimm", "itype", "i_type", "immediate"):
        token = "reg_imm"
    if token in ("memory", "mem", "tile"):
        token = "block"
    return token if token in FORMS else ""


def normalize_group(value: str) -> str:
    """Canonicalize an instruction group, defaulting to "compute"."""
    token = str(value).strip().lower().replace("_", "-").replace(" ", "")
    if token in ("load", "store", "loadstore", "load-store", "memory"):
        token = "load-store"
    if token in ("config", "configure", "configuration", "setup"):
        token = "configuration"
    return token if token in GROUPS else DEFAULT_GROUP


def validate_architectural_state(s: ISASpec) -> None:
    """Register files and CSRs must be nameable, sized, and not alias
    anything architectural.

    An extension is free to add no state at all -- a family of pure
    register-to-register primitives is the cleanest thing it can be. The
    requirement is only that state it *does* hold is declared, because
    undeclared state is still built, still context-switched, and still paid
    for; it just is not reviewed."""
    seen_rf: set[str] = set()
    for rf in s.register_files:
        if not rf.name:
            raise ValueError("every register file needs a name")
        if rf.name in seen_rf:
            raise ValueError(f"register file {rf.name!r} is declared twice")
        seen_rf.add(rf.name)
        if not isinstance(rf.count, int) or rf.count < 1:
            raise ValueError(f"register file {rf.name!r} count={rf.count!r} "
                             "must be a positive int")
        if not isinstance(rf.width_bits, int) or rf.width_bits < 1:
            raise ValueError(f"register file {rf.name!r} width_bits="
                             f"{rf.width_bits!r} must be a positive int")

    seen_csr: dict[int, str] = {}
    for csr in s.csrs:
        if not csr.name:
            raise ValueError("every CSR needs a name")
        addr = csr.address_int()
        if addr < 0:
            raise ValueError(f"CSR {csr.name!r} address {csr.address!r} is not a "
                             "number (write it as e.g. '0x800')")
        if not in_custom_csr_space(addr):
            ranges = ", ".join(f"0x{lo:03X}-0x{hi:03X}" for lo, hi in CUSTOM_CSR_RANGES)
            raise ValueError(
                f"CSR {csr.name!r} at {csr.address} is outside the custom CSR "
                f"space and aliases an architectural CSR — use one of {ranges}")
        if addr in seen_csr:
            raise ValueError(f"CSR {csr.name!r} collides with {seen_csr[addr]!r} "
                             f"at address {csr.address}")
        seen_csr[addr] = csr.name
        if str(csr.access).upper() not in ("RW", "RO", "WARL", "WLRL"):
            raise ValueError(f"CSR {csr.name!r} access={csr.access!r} must be one "
                             "of RW, RO, WARL, WLRL")

    declared = seen_rf | {c.name for c in s.csrs}
    for ins in s.instructions:
        for fname in ("reads_state", "writes_state"):
            for ref in getattr(ins, fname) or []:
                if str(ref) not in declared:
                    raise ValueError(
                        f"instruction {ins.name!r} {fname} names {ref!r}, which the "
                        "spec does not declare in register_files or csrs")


def validate_isa_spec(s: ISASpec) -> None:
    """An ISASpec is valid only if every instruction names an encoding in the
    custom opcode space, has prose + pseudo-code semantics, and carries a C
    reference model that declares the fixed harness signature. The loop
    additionally enforces link 1 (build + stress-run) before the spec ships."""
    if not s.name:
        raise ValueError("ISASpec.name is required")
    if not s.instructions:
        raise ValueError("ISASpec must define at least one instruction")
    validate_architectural_state(s)
    seen: dict[tuple[str, str, str], str] = {}
    for ins in s.instructions:
        if not ins.mnemonic or not ins.encoding:
            raise ValueError(f"instruction {ins.name!r} missing mnemonic/encoding")
        # The fields are typed as strings and consumed as strings downstream; a
        # nested object here means the draft was not written to the schema.
        for field_name in ("encoding", "opcode", "funct3", "funct7", "mnemonic"):
            if not isinstance(getattr(ins, field_name), str):
                raise ValueError(
                    f"instruction {ins.name!r} field {field_name!r} must be a string, "
                    f"got {type(getattr(ins, field_name)).__name__}")
        if normalize_opcode(ins.opcode) == "":
            raise ValueError(
                f"instruction {ins.name!r} opcode {ins.opcode!r} is not in the custom "
                f"space — use one of {', '.join(CUSTOM_OPCODES)}")
        # An encoding that collides with another instruction's is unimplementable;
        # S2c is supposed to catch this, so fail the draft loudly if it did not.
        # An I-type form has no funct7 to decode on -- bits 31:20 are the
        # immediate -- so it claims the whole funct3 slot, and anything else
        # sharing that funct3 is unreachable.
        f7_key = "*" if normalize_form(ins.form) == "reg_imm" else ins.funct7
        key = (normalize_opcode(ins.opcode), ins.funct3, f7_key)
        wild = (normalize_opcode(ins.opcode), ins.funct3, "*")
        clash = seen.get(key) or (seen.get(wild) if f7_key != "*" else None)
        if clash is None and f7_key == "*":
            clash = next((n for (o, f3, _), n in seen.items()
                          if (o, f3) == (normalize_opcode(ins.opcode), ins.funct3)), None)
        if clash is not None:
            raise ValueError(
                f"instruction {ins.name!r} collides with {clash!r}: both encode "
                f"opcode={key[0]} funct3={key[1]!r} funct7={f7_key!r}"
                + (" (an immediate form claims every funct7 of its funct3)"
                   if "*" in (f7_key, "*") and f7_key == "*" else ""))
        seen[key] = ins.name
        if not ins.semantics or not ins.pseudocode:
            raise ValueError(f"instruction {ins.name!r} missing semantics/pseudocode")
        if not ins.c_model:
            raise ValueError(f"instruction {ins.name!r} missing c_model "
                             "(the compilable C reference model link 1 runs)")
        if not isinstance(ins.words, int) or not (1 <= ins.words <= 256):
            raise ValueError(f"instruction {ins.name!r} words={ins.words!r} "
                             "must be an int in [1, 256] (operand width in 32-bit words)")
        for fname, val in (("mac_ops", ins.mac_ops),
                           ("invocations", ins.invocations),
                           ("marshal_words", ins.marshal_words)):
            if not isinstance(val, int) or val < 0:
                raise ValueError(f"instruction {ins.name!r} {fname}={val!r} must be "
                                 "a non-negative int (see red/cost.py)")
        if ins.invocations < 1:
            raise ValueError(f"instruction {ins.name!r} invocations must be >= 1")
        if not ins.replaces:
            raise ValueError(f"instruction {ins.name!r} must name the mined loop it "
                             "replaces (`replaces`), so its benefit can be computed")
        # RED's operand ABI is fixed for every instruction it designs:
        #   <mnemonic> rd, rs1, rs2   rs1 = input block address,
        #                             rs2 = output block address, rd <- 0
        # A draft that omits rs2 has no way to say where its result goes, and
        # the harness, the Spike model and the caller all assume it is there.
        # This was the single most common blocking review finding in the
        # project's history -- all four reviewers independently spend a finding
        # on it and the loop pays a full fan-out plus a revision round for
        # something decidable from the spec's own fields. Decide it here.
        form = normalize_form(ins.form)
        if form == "":
            raise ValueError(
                f"instruction {ins.name!r} form={ins.form!r} is not one of "
                f"{', '.join(FORMS)}")
        named = {str(o).strip().lower() for o in (ins.operands or [])}
        if form == "block":
            required = ("rd", "rs1", "rs2")
            how = ("rs1 holds the address of the input block, rs2 holds the "
                   f"address of the output block ({ins.words} words), and rd "
                   "receives 0")
        elif form == "reg":
            required = ("rd", "rs1") + (("rs2",) if ins.src_regs == 2 else ())
            how = ("in[0] comes from x[rs1], in[1] from x[rs2], and out[0] is "
                   "written to x[rd]")
        else:  # reg_imm
            required = ("rd", "rs1")
            how = ("in[0] comes from x[rs1], in[1] is the zero-extended "
                   "immediate, and out[0] is written to x[rd]")
        missing = [r for r in required if r not in named]
        if missing:
            raise ValueError(
                f"instruction {ins.name!r} operands={ins.operands!r} is missing "
                f"{', '.join(missing)}. For form {form!r} the ABI is "
                f"`{ins.mnemonic} {', '.join(required)}"
                + (", imm" if form == "reg_imm" else "")
                + f"`, where {how}. State them all: "
                f'"operands": {list(required)}.')

        if form in REGISTER_FORMS:
            if ins.words != REGISTER_FORM_WORDS:
                raise ValueError(
                    f"instruction {ins.name!r} form={form!r} must set "
                    f"words={REGISTER_FORM_WORDS} (in[0], in[1] -> out[0]); "
                    f"got words={ins.words}. An operand that does not fit in "
                    "registers needs form='block'.")
            if not isinstance(ins.src_regs, int) or ins.src_regs not in (1, 2):
                raise ValueError(f"instruction {ins.name!r} src_regs="
                                 f"{ins.src_regs!r} must be 1 or 2")
            # `state_words` threads state through the operand block, and a
            # register form has no operand block to thread it through.
            #
            # Naming an architectural register file in reads_state /
            # writes_state is a different thing and is allowed -- it is the
            # whole point of the load/store shape the structural check now
            # requires. `vmul.vv vt2, vt0, vt1` is a register-form compute
            # that reads and writes the extension's own file and never names
            # an address; forbidding it here would make that rule
            # unsatisfiable. The file lives in the model's own file-scope
            # state (see ISASPEC_FORMAT.md 6b), not in in[]/out[], so the
            # model stays a pure function of its input array and Gate 2 can
            # still drive it.
            if ins.state_words:
                raise ValueError(
                    f"instruction {ins.name!r} form={form!r} cannot set "
                    "state_words: that threads state through the operand "
                    "block and a register form has none. An extension "
                    "register file is named in reads_state/writes_state "
                    "instead, and held in the model's own file-scope state.")
        if form == "reg_imm":
            # The I-type immediate field is 12 bits, but RED's differential
            # test enumerates it: the firmware has to name the immediate at
            # assembly time, so it emits one `.insn i` per value. 5 bits is 32
            # of them, which covers what immediates are actually used for in
            # this space -- a byte or round selector, a rotate amount -- and
            # keeps the harness a readable switch instead of 4,096 cases.
            if not isinstance(ins.imm_bits, int) or not (1 <= ins.imm_bits <= MAX_IMM_BITS):
                raise ValueError(
                    f"instruction {ins.name!r} form='reg_imm' needs imm_bits in "
                    f"[1, {MAX_IMM_BITS}]; got {ins.imm_bits!r}. The immediate is "
                    "tested exhaustively, so it has to be small enough to "
                    "enumerate; a wider operand belongs in a register.")
        if not isinstance(ins.state_words, int) or ins.state_words < 0:
            raise ValueError(f"instruction {ins.name!r} state_words="
                             f"{ins.state_words!r} must be a non-negative int")
        if ins.state_words > ins.words:
            raise ValueError(
                f"instruction {ins.name!r} state_words={ins.state_words} exceeds "
                f"words={ins.words}: the live state is carried in the first "
                "state_words of in[] and out[], so it has to fit in them")
        expected = f"{c_identifier(ins.mnemonic)}_model"
        if expected not in ins.c_model:
            raise ValueError(
                f"instruction {ins.name!r} c_model must define "
                f"void {expected}(const uint32_t in[{ins.words}], uint32_t out[{ins.words}])")
