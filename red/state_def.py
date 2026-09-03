"""Typed artifacts for the RED loop.

RED's agents communicate through *typed JSON artifacts* that downstream
tool nodes consume mechanically. Everything the loop produces that crosses a
node boundary is a dataclass here, with ``to_json``/``from_json`` round-trips
and a ``validate_*`` function that enforces the falsifiability contract of the
design:

* ``HotLoopReport`` — A1's output. A mined, ranked set of hot loops with a
  cycle-share budget and Gate-1 re-profile reproducibility.
* ``ISASpec`` — A2's output. A closed, executable instruction spec
  (encodings + semantics + tests) that A3/A4/A5 consume mechanically.
* ``GateResult`` — the verdict of one programmatic evaluation edge (gates and
  the four-link verification chain); a failure carries a counterexample.
* ``EvalResult`` / ``SpeedupResult`` — A3/A4/A5 outcomes.

These are persisted verbatim in CHIA's store (``red.db_node``) for audit and
reuse, mirroring the ``HotLoopReport``/``ISASpec`` contract in the RED design.
"""

from __future__ import annotations

import dataclasses
import json
from dataclasses import dataclass, field, asdict
from typing import Any, Optional, Union, get_args, get_origin, get_type_hints


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
    operand_stats: dict[str, int] = field(default_factory=dict)
    rank: int = 0                    # 1 = hottest


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
    """One designed custom instruction in the custom-0/custom-3 space."""

    name: str                        # e.g. "secp256r1.modadd"
    mnemonic: str                    # assembler mnemonic
    encoding: str                    # 32-bit encoding, custom-0/3 opcode space
    semantics: str                   # prose semantics
    pseudocode: str                  # single-assignment pseudo-code (the model)
    operands: list[str] = field(default_factory=list)   # rd, rs1, rs2
    funct3: str = ""
    funct7: str = ""
    opcode: str = ""                 # e.g. "custom-0"


@dataclass
class ISASpec:
    """A2's typed output — an executable ISA spec consumed mechanically by
    A3 (RTL/PCPI), A4 (LLVM pass), and the Spike patch. Gate 2 passes when the
    C model and a patched Spike agree on ``gate2_vectors`` vectors."""

    name: str                        # extension name, e.g. "RED-secp256r1"
    version: str = "0.1.0"
    description: str = ""
    instructions: list[InstructionSpec] = field(default_factory=list)
    encodings: list[str] = field(default_factory=list)   # machine-encoded lines
    tests: list[str] = field(default_factory=list)       # emitted test vectors/asm
    gate2_vectors: int = 0           # 10^5 target
    passes_gate2: bool = False
    critic_rounds: int = 0           # S2c reject/revise rounds (<= 8)


# ---------------------------------------------------------------------------
# Gates / verification chain
# ---------------------------------------------------------------------------

@dataclass
class GateResult:
    """The verdict of one programmatic evaluation edge. A failure carries a
    counterexample that rejects the instruction back to S2b/S2c."""

    gate: str                        # "gate1" | "gate2" | "link1" .. "link4"
    passed: bool
    detail: str = ""
    counterexample: Optional[str] = None   # e.g. failing vector or divergence


@dataclass
class EvalResult:
    """A bounded tool-node outcome (A3 RTL, A4 compiler, A5 e2e)."""

    node: str                        # "A3" | "A4" | "A5"
    passed: bool
    detail: str = ""
    metrics: dict[str, Any] = field(default_factory=dict)


@dataclass
class SpeedupResult(EvalResult):
    """A5's speedup measurement, specializing EvalResult."""

    baseline_cycles: int = 0
    extended_cycles: int = 0
    speedup: float = 1.0             # baseline / extended
    instr_reduction_pct: float = 0.0  # dynamic instruction-count reduction


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


def validate_isa_spec(s: ISASpec) -> None:
    """An ISASpec is valid only if every instruction names an encoding in the
    custom opcode space and has prose + pseudo-code semantics (the executable
    model). The loop additionally enforces Gate 2 before A3/A4 consume it."""
    if not s.name:
        raise ValueError("ISASpec.name is required")
    if not s.instructions:
        raise ValueError("ISASpec must define at least one instruction")
    for ins in s.instructions:
        if not ins.mnemonic or not ins.encoding:
            raise ValueError(f"instruction {ins.name!r} missing mnemonic/encoding")
        if not ins.semantics or not ins.pseudocode:
            raise ValueError(f"instruction {ins.name!r} missing semantics/pseudocode")
