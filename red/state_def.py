"""Typed artifacts for the RED loop.

RED's agents communicate through *typed JSON artifacts* that downstream
tool nodes consume mechanically. Everything the loop produces that crosses a
node boundary is a dataclass here, with ``to_json``/``from_json`` round-trips
and a ``validate_*`` function that enforces the falsifiability contract of the
design:

* ``HotLoopReport`` — A1's output. A mined, ranked set of hot loops with a
  cycle-share budget and Gate-1 re-profile reproducibility.
* ``ISASpec`` — A2's output and RED's final deliverable. A closed, executable
  instruction spec (encodings + semantics + C reference models + tests).
* ``GateResult`` — the verdict of one programmatic evaluation edge (Gate 1,
  Gate 2, link 1, link 2); a failure carries a counterexample.

These are persisted verbatim in CHIA's store (``red.db_node``) for audit and
reuse, mirroring the ``HotLoopReport``/``ISASpec`` contract in the RED design.
"""

from __future__ import annotations

import dataclasses
import json
from dataclasses import dataclass, field, asdict
from typing import Optional, Union, get_args, get_origin, get_type_hints


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
# A2 — ISA synthesis
# ---------------------------------------------------------------------------

@dataclass
class InstructionSpec:
    """One designed custom instruction in the custom-0/custom-3 space.

    The instruction carries *two independently written* executable models, and
    Gate 2 passes only when they agree on 10^5 vectors:

    * ``c_model``     — C99, written by the designer (S2b). Signature fixed by
      RED so any instruction can be harnessed mechanically::

          void <ident>_model(const uint32_t in[<words>], uint32_t out[<words>])

    * ``spike_model`` — C++, written by a *different* agent from this spec's
      prose, pseudo-code and encoding alone, and compiled into a Spike
      extension::

          void <ident>_iss(const uint32_t in[<words>], uint32_t out[<words>])

    ``<ident>`` is ``mnemonic`` with non-identifier characters replaced by
    ``_``; ``words`` is the operand width the designer chose, in 32-bit words.
    See ``red/iss.py`` for the instruction's register/memory ABI."""

    name: str                        # e.g. "secp256r1.modadd"
    mnemonic: str                    # assembler mnemonic
    encoding: str                    # 32-bit encoding, custom-0/3 opcode space
    semantics: str                   # prose semantics
    pseudocode: str                  # single-assignment pseudo-code (the model)
    c_model: str = ""                # compilable C99 reference model (link 1)
    spike_model: str = ""            # C++ model for the Spike ISS (Gate 2)
    words: int = 8                   # operand width in 32-bit words
    operands: list[str] = field(default_factory=list)   # rd, rs1, rs2
    funct3: str = ""
    funct7: str = ""
    opcode: str = ""                 # e.g. "custom-0"


@dataclass
class ISASpec:
    """A2's typed output and RED's final deliverable — an executable ISA spec:
    encodings, prose + pseudo-code semantics, and a compilable C reference model
    per instruction, plus the vectors link 1 exercised them on."""

    name: str                        # extension name, e.g. "RED-secp256r1"
    version: str = "0.1.0"
    description: str = ""
    instructions: list[InstructionSpec] = field(default_factory=list)
    encodings: list[str] = field(default_factory=list)   # machine-encoded lines
    tests: list[str] = field(default_factory=list)       # link-1 vector summary
    link1_vectors: int = 0           # vectors each C model was run on
    gate2_vectors: int = 0           # vectors the C model and Spike agreed on
    passes_link1: bool = False       # set by the loop from link 1, never by the agent
    passes_gate2: bool = False       # set by the loop from Gate 2, never by the agent
    critic_rounds: int = 0           # S2c reject/revise rounds (<= 8)


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
# JSON round-trip (typed-artifact persistence)
# ---------------------------------------------------------------------------

def to_json(obj) -> str:
    """Serialize a typed artifact (or a list of them, or a plain dict) to JSON.

    The loop also persists non-dataclass payloads (e.g. the A1 profile dict and
    the list-of-GateResult chain), so this falls back to plain json.dumps for
    anything that is not a dataclass instance."""
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return json.dumps(asdict(obj), indent=2, sort_keys=True)
    if isinstance(obj, list):
        return json.dumps(
            [asdict(o) if dataclasses.is_dataclass(o) else o for o in obj],
            indent=2, sort_keys=True)
    return json.dumps(obj, indent=2, sort_keys=True, default=str)


def _convert(typ, val):
    """Recursively coerce *val* to *typ* (a dataclass, list[...], dict, or scalar).

    ``asdict`` flattens nested dataclasses into dicts, so ``from_json`` must
    rebuild them — a shallow ``cls(**json.loads(data))`` would leave
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


def from_json(data: str, cls):
    """Rebuild a typed artifact from JSON produced by :func:`to_json` (nested
    dataclasses included)."""
    return _from_dict(cls, json.loads(data))


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


def validate_isa_spec(s: ISASpec) -> None:
    """An ISASpec is valid only if every instruction names an encoding in the
    custom opcode space, has prose + pseudo-code semantics, and carries a C
    reference model that declares the fixed harness signature. The loop
    additionally enforces link 1 (build + stress-run) before the spec ships."""
    if not s.name:
        raise ValueError("ISASpec.name is required")
    if not s.instructions:
        raise ValueError("ISASpec must define at least one instruction")
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
        key = (normalize_opcode(ins.opcode), ins.funct3, ins.funct7)
        if key in seen:
            raise ValueError(
                f"instruction {ins.name!r} collides with {seen[key]!r}: both encode "
                f"opcode={key[0]} funct3={key[1]!r} funct7={key[2]!r}")
        seen[key] = ins.name
        if not ins.semantics or not ins.pseudocode:
            raise ValueError(f"instruction {ins.name!r} missing semantics/pseudocode")
        if not ins.c_model:
            raise ValueError(f"instruction {ins.name!r} missing c_model "
                             "(the compilable C reference model link 1 runs)")
        if not isinstance(ins.words, int) or not (1 <= ins.words <= 256):
            raise ValueError(f"instruction {ins.name!r} words={ins.words!r} "
                             "must be an int in [1, 256] (operand width in 32-bit words)")
        expected = f"{c_identifier(ins.mnemonic)}_model"
        if expected not in ins.c_model:
            raise ValueError(
                f"instruction {ins.name!r} c_model must define "
                f"void {expected}(const uint32_t in[{ins.words}], uint32_t out[{ins.words}])")
