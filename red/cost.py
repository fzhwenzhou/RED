"""What an instruction costs, and whether the extension is worth building.

RED verified its way to an extension that was 2% *slower* than the baseline at
+74% area (eval/RESULTS.md). Nothing in the loop had ever asked what an
instruction costs, so the designer chose ``mac96`` — one 32x32 multiply for ten
memory beats — when the same operand traffic could have carried sixty-four.

This module is the missing question, and the numbers in it are measured, not
assumed. Every constant comes from the RTL harness in ``eval/``:

    FIXED_CYCLES      28   PCPI handshake + coprocessor FSM, from fitting the
                           two measured latencies (mac96 10 beats -> 48 cycles,
                           add256 32 beats -> 92 cycles)
    CYCLES_PER_BEAT    2   the same fit; one 32-bit word in or out
    MAC_CYCLES         1   a 32x32 multiply-accumulate in a sequential engine
    CPU_LOAD_STORE     5   one CPU lw/sw on PicoRV32 with 2-cycle SRAM
    CYCLES_PER_IR   8.51   host instruction -> PicoRV32 cycle, from the
                           measured 298,502,758 cycles / 35,063,482 Ir for one
                           full ECDH exchange

The instruction cost is therefore

    cost = FIXED + CYCLES_PER_BEAT * 2 * words + MAC_CYCLES * mac_ops

and the decisive quantity is **arithmetic per operand word moved**: the data
movement is fixed by ``words``, so an instruction only wins when it does enough
work per invocation to bury it. ``mac96`` moves 10 words to do 1 multiply;
a 256x256 multiply moves 32 to do 64.

The model is a model. It predicts a kernel speedup within ~15% of what the RTL
measured on the cases eval/ covers, which is enough to separate a 5x design from
a 0.98x one — which is all the gate needs to do.
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field

from red import callgrind, state_def
from red.constants import (
    SPEEDUP_ATTAINMENT,
    SPEEDUP_FLOOR,
    SPEEDUP_RELAX,
    CPU_LOAD_STORE,
    CYCLES_PER_MARSHAL_WORD,
    CYCLES_PER_BEAT,
    CYCLES_PER_IR,
    FIXED_CYCLES,
    MAC_CYCLES,
    SPEEDUP_TARGET,
)


# What the core alone contributes, as read from PicoRV32's RTL: a 1-cycle PCPI
# handshake and a 1-cycle memory word. The difference between these and the
# measured FIXED_CYCLES / CYCLES_PER_BEAT is what a coprocessor implementation
# costs on top, and that part travels to other cores.
_CORE_ISSUE_BASELINE = 1.0
_CORE_BEAT_BASELINE = 1.0


@dataclass
class InstructionCost:
    """What one instruction costs, and what it buys."""

    mnemonic: str
    words: int
    mac_ops: int
    cycles: float                    # one invocation, including its operand traffic
    beats: int                       # 32-bit words moved in + out
    marshal_cycles: float            # what the caller pays to lay operands out
    replaces: str = ""               # loop_id from the HotLoopReport
    invocations: float = 1.0         # per kernel call — the designer's, unverified
    unverified: str = ""             # why this instruction's cost is not settled
    replaced_cycles: float = 0.0     # software cost of the work one invocation replaces
    speedup: float = 0.0             # replaced_cycles / (cycles + marshal_cycles)
    note: str = ""

    def total(self) -> float:
        return self.cycles + self.marshal_cycles


@dataclass
class SpeedupEstimate:
    """The extension's predicted effect on the whole application."""

    per_instruction: list[InstructionCost] = field(default_factory=list)
    covered_share: float = 0.0       # fraction of app cycles the extension touches
    app_speedup: float = 1.0         # Amdahl over the covered kernels
    detail: str = ""
    # Set when the *profile* is unusable, not when the *design* is bad. Without
    # it a missing call count reads as a worthless extension, and the loop
    # spends its whole redesign budget asking the designer to fix a profiler
    # bug it cannot see. Three full cluster runs were lost that way.
    measurement_error: str = ""
    # Declarations the prediction depends on that RED could not verify. Not an
    # error — the number may well be right — but the gate must not present a
    # prediction resting on one as though it were measured throughout.
    unverified: list = field(default_factory=list)

    def meets_target(self, target: float = SPEEDUP_TARGET) -> bool:
        return self.app_speedup >= target


@dataclass
class Platform:
    """The cost constants Gate 3 uses. Defaults are the measured PicoRV32
    numbers; :meth:`from_core` takes whatever A0 read out of the target core's
    RTL instead, so retargeting changes the profile rather than this module."""

    fixed_cycles: float = FIXED_CYCLES
    cycles_per_beat: float = CYCLES_PER_BEAT
    mac_cycles: float = MAC_CYCLES
    cpu_load_store: float = CPU_LOAD_STORE
    cycles_per_ir: float = CYCLES_PER_IR

    @classmethod
    def from_core(cls, profile) -> "Platform":
        """Compose A0's reading of the core with what the RTL actually costs.

        A0 reads the *interface* out of the RTL — PicoRV32's PCPI handshake is
        1 cycle, a memory word is 1 cycle. The measured end-to-end cost is 28
        and 2, because a real coprocessor also pays for its own state machine,
        its bus hand-over and the arbitration around it. Those overheads belong
        to the implementation, not to the core, so they carry across to another
        core while A0's interface numbers change with it:

            fixed = A0 issue latency + measured implementation overhead
            beat  = A0 word latency  + measured memory overhead

        Taking A0's numbers alone would make Gate 3 optimistic by an order of
        magnitude and let through exactly the designs the gate exists to stop.
        """
        if profile is None:
            return cls()
        impl_overhead = FIXED_CYCLES - _CORE_ISSUE_BASELINE
        mem_overhead = CYCLES_PER_BEAT - _CORE_BEAT_BASELINE
        return cls(
            fixed_cycles=(profile.coprocessor_issue_overhead or _CORE_ISSUE_BASELINE)
                         + max(0.0, impl_overhead),
            cycles_per_beat=(profile.cycles_per_word_moved or _CORE_BEAT_BASELINE)
                            + max(0.0, mem_overhead),
            mac_cycles=MAC_CYCLES,
            cpu_load_store=profile.cycles_per_load or CPU_LOAD_STORE,
            cycles_per_ir=CYCLES_PER_IR)

    def instruction_cycles(self, words: int, mac_ops: int) -> float:
        return (self.fixed_cycles
                + self.cycles_per_beat * 2 * max(words, 1)
                + self.mac_cycles * max(mac_ops, 0))


def instruction_cycles(words: int, mac_ops: int, platform: "Platform | None" = None) -> float:
    """Cycles for one invocation, operand traffic included."""
    return (platform or Platform()).instruction_cycles(words, mac_ops)


def work_per_beat(words: int, mac_ops: int) -> float:
    """Multiplies performed per 32-bit word moved — the number that decides
    whether an instruction can win at all. Below ~1 the instruction is a data
    movement engine with an ALU attached."""
    return mac_ops / float(2 * max(words, 1))


# Host instructions per 32x32 multiply-accumulate, used to turn a measured
# instruction count into a comparable "op" count. This is a property of the
# machine doing the measuring, not of the design, so it is CALIBRATED there
# rather than assumed: `_ir_per_op` compiles a reference schoolbook multiply
# with a known MAC count through the same path as the models and measures it.
# The old fixed 8.0 was fitted on one host; on the GCP profiling node the same
# honest 64-MAC model measures ~14 Ir per MAC, so a truthful `mac_ops=64` was
# rejected as an understatement and the instruction was overpriced.
_IR_PER_OP_FALLBACK = 8.0
# Host instructions per sequential logic step when the calibration cannot run.
_IR_PER_LOGIC_FALLBACK = 5.0
# A reference kernel whose MAC count is known exactly: 8x8 limbs, schoolbook.
_CALIB_MACS = 64
_CALIB_MODEL = r"""
void calib_model(const uint32_t in[16], uint32_t out[16]) {
    uint32_t a[8], b[8];
    for (int i = 0; i < 8; i++) { a[i] = in[i]; b[i] = in[i + 8]; }
    for (int i = 0; i < 16; i++) out[i] = 0;
    for (int i = 0; i < 8; i++) {
        uint32_t carry = 0;
        for (int j = 0; j < 8; j++) {
            uint64_t t = (uint64_t)a[i] * b[j] + out[i + j] + carry;
            out[i + j] = (uint32_t)t;
            carry = (uint32_t)(t >> 32);
        }
        out[i + 8] = carry;
    }
}
"""
# Below this, a model is too small for the measurement to say anything.
# A reference kernel whose *sequential* step count is known exactly and which
# contains no multiplies at all: 32 rounds of a CRC-style shift/xor over 8
# words, 256 steps. The multiply calibration above cannot price this work --
# dividing a bit-serial model's instruction count by ~15 Ir-per-MAC turns 256
# hardware steps into ~80 "ops" and charges a third of the real latency. libcrc
# is exactly that case: its models perform 256 bit steps per block, the
# synthesizable RTL runs one step per cycle and measures 297-301 cycles, and
# (\ref{eq:cost}) predicted ~112.
_CALIB_STEPS = 256
_CALIB_LOGIC_MODEL = r"""
void caliblogic_model(const uint32_t in[8], uint32_t out[8]) {
    uint32_t crc = in[0];
    for (int i = 0; i < 8; i++) {
        uint32_t byte = in[i];
        for (int b = 0; b < 32; b++) {
            uint32_t mask = (uint32_t)0 - ((crc ^ byte) & 1u);
            crc = (crc >> 1) ^ (0xEDB88320u & mask);
            byte >>= 1;
        }
        out[i] = crc;
    }
}
"""
# Cycles the datapath takes per sequential logic step. One step per cycle is
# what the measured RTL does.
LOGIC_CYCLES = float(os.environ.get("RED_LOGIC_CYCLES", "1.0"))

_IR_FLOOR = 40.0
# How far an instruction's measured work may fall short of the work it claims to
# replace before the claim, not the measurement, is treated as the error.
_WORK_MISMATCH = 4.0
# How far `invocations` may understate before it is replaced by the measured
# ratio. Loose enough to tolerate a model that is a little cheaper than the
# kernel iteration it replaces, tight enough to catch 2-where-128-is-needed.
_INVOCATION_MISMATCH = float(os.environ.get("RED_INVOCATION_MISMATCH", "2.0"))

_OPS_HARNESS = r"""
#include <stdint.h>
#include <string.h>
%(model)s
int main(void) {
    uint32_t in[%(words)d], out[%(words)d];
    for (int i = 0; i < %(words)d; i++) in[i] = 0x9e3779b9u * (uint32_t)(i + 1);
    memset(out, 0, sizeof out);
    %(ident)s_model(in, out);
    return (int)(out[0] & 1);
}
"""


_OPS_CACHE: dict = {}


def measure_ops(spec: state_def.ISASpec, work_dir: str) -> dict:
    """Measure how much arithmetic each C model actually performs.

    `mac_ops` is declared by the designer and Gate 3 costs the instruction from
    it, which makes it exactly the field worth misreporting — a spec declaring
    ``mac_ops = 0`` for a 512-iteration bit-serial reduction prices a very
    expensive instruction at the cost of a memcpy. So measure it: compile the
    model, run it once under callgrind, and count the instructions actually
    executed. Returns {mnemonic: executed_ir}; missing entries mean the
    measurement could not run and the declaration stands.
    """
    if not (shutil.which("cc") and shutil.which("valgrind")):
        return {}
    os.makedirs(work_dir, exist_ok=True)
    _ir_per_op(work_dir)          # calibrate this machine once, before costing
    _ir_per_logic_step(work_dir)  # ...and the sequential-step rate with it
    out: dict = {}
    for ins in spec.instructions:
        # A model's instruction count is a property of the model, so measuring
        # it twice is wasted work -- and not cheap waste: every measurement
        # compiles the model and runs it under callgrind. Re-pricing each review
        # round (which is what keeps a revision from giving the design away)
        # multiplied that by the round count, and on a twelve-instruction libcrc
        # spec it ran 48 callgrind passes on the head where 12 would do. The
        # head has 5.7 GB and Ray's memory monitor killed a tool server mid-run.
        key = hashlib.sha1(
            (ins.mnemonic + "\0" + (ins.c_model or "")).encode()).hexdigest()
        if key in _OPS_CACHE:
            if _OPS_CACHE[key] is not None:
                out[ins.mnemonic] = _OPS_CACHE[key]
            continue
        ident = state_def.c_identifier(ins.mnemonic)
        src = os.path.join(work_dir, f"{ident}_ops.c")
        exe = os.path.join(work_dir, f"{ident}_ops")
        cg = os.path.join(work_dir, f"{ident}_ops.callgrind")
        try:
            with open(src, "w") as f:
                f.write(_OPS_HARNESS % {"model": ins.c_model, "words": max(ins.words, 1),
                                        "ident": ident})
            if subprocess.run(["cc", "-std=c99", "-O1", "-o", exe, src],
                              capture_output=True, text=True).returncode != 0:
                continue
            subprocess.run(["valgrind", "--tool=callgrind",
                            f"--callgrind-out-file={cg}", "--quiet", exe],
                           capture_output=True, text=True, timeout=120)
            if not os.path.exists(cg):
                continue
            self_ir, _calls = callgrind.parse_raw(cg)
            if f"{ident}_model" in self_ir:
                out[ins.mnemonic] = self_ir[f"{ident}_model"]
                _OPS_CACHE[key] = self_ir[f"{ident}_model"]
            else:
                _OPS_CACHE[key] = None
        except (OSError, subprocess.TimeoutExpired):
            continue
    return out


_IR_PER_OP_CACHE: dict = {}
_IR_PER_STEP_CACHE: dict = {}


def _ir_per_logic_step(work_dir: str) -> float:
    """Host instructions per sequential logic step, measured on THIS machine.

    The multiply calibration answers "how many host instructions is one 32x32
    MAC"; this answers the same question for a shift/xor step, and they are not
    the same number. Pricing bit-serial work with the multiply ratio is what
    made Gate 3 charge a third of libcrc's real instruction latency.
    """
    if "v" in _IR_PER_STEP_CACHE:
        return _IR_PER_STEP_CACHE["v"]
    _IR_PER_STEP_CACHE["v"] = _IR_PER_LOGIC_FALLBACK
    probe = state_def.ISASpec(name="caliblogic", instructions=[
        state_def.InstructionSpec(
            name="caliblogic", mnemonic="caliblogic", encoding="-",
            semantics="-", pseudocode="-", c_model=_CALIB_LOGIC_MODEL, words=8,
            mac_ops=0, replaces="", invocations=1,
            operands=["rd", "rs1", "rs2"])])
    measured = measure_ops(probe, os.path.join(work_dir, "caliblogic"))
    ir = measured.get("caliblogic")
    if ir and ir > _IR_FLOOR:
        _IR_PER_STEP_CACHE["v"] = ir / float(_CALIB_STEPS)
    return _IR_PER_STEP_CACHE["v"]


def _ir_per_op(work_dir: str) -> float:
    """Host instructions per 32x32 MAC, measured on THIS machine.

    Compiles a reference schoolbook multiply of known MAC count through exactly
    the path ``measure_ops`` uses and divides. Cached per work root, since it
    depends on the local compiler rather than on anything in the run.
    """
    if "v" in _IR_PER_OP_CACHE:
        return _IR_PER_OP_CACHE["v"]
    # Seed the cache before measuring: the calibration goes through
    # `measure_ops`, which calibrates, and this is what stops the recursion.
    _IR_PER_OP_CACHE["v"] = _IR_PER_OP_FALLBACK
    ratio = _IR_PER_OP_FALLBACK
    probe = state_def.ISASpec(name="calib", instructions=[
        state_def.InstructionSpec(
            name="calib", mnemonic="calib", encoding="-", semantics="-",
            pseudocode="-", c_model=_CALIB_MODEL, words=16,
            mac_ops=_CALIB_MACS, replaces="", invocations=1)])
    measured = measure_ops(probe, os.path.join(work_dir, "calib"))
    ir = measured.get("calib")
    if ir and ir > _IR_FLOOR:
        ratio = ir / float(_CALIB_MACS)
    _IR_PER_OP_CACHE["v"] = ratio
    return ratio


def amdahl_ceiling(covered_share: float) -> float:
    """The most any extension can deliver on a workload it covers this much of.

    Everything the extension does not touch runs at its original speed, so the
    whole-application speedup is bounded by 1/(1-covered) no matter how fast the
    instructions are. This is the number that makes a fixed target incoherent:
    at 47.3% coverage the ceiling is 1.90x, and asking for 2.0x asks for
    something arithmetic forbids.
    """
    if covered_share >= 1.0:
        return float("inf")
    return 1.0 / max(1e-9, 1.0 - covered_share)


def target_for(round_: int, covered_share: float,
               base: float = SPEEDUP_TARGET) -> tuple:
    """The bar in force this round, and why.

    Returns ``(target, reason)``. The bar is the ambition capped by what the
    workload permits, relaxed geometrically per redesign round and never taken
    below the floor. Below the floor a "speedup" is inside the cost model's own
    error -- measured at ~8% against RTL where the declarations are sound -- and
    passing a design there would be reporting noise as a result.
    """
    ceiling = amdahl_ceiling(covered_share)
    attainable = ceiling * SPEEDUP_ATTAINMENT
    reasons = []
    bar = base
    if attainable < base:
        bar = attainable
        reasons.append(
            f"Amdahl caps this workload at {ceiling:.2f}x for the "
            f"{covered_share:.1%} it covers, so the bar is "
            f"{SPEEDUP_ATTAINMENT:.0%} of that rather than the {base:.2f}x "
            "default")
    if round_ > 1:
        bar *= SPEEDUP_RELAX ** (round_ - 1)
        reasons.append(f"relaxed for redesign round {round_}")
    if bar < SPEEDUP_FLOOR:
        bar = SPEEDUP_FLOOR
        reasons.append(f"held at the {SPEEDUP_FLOOR:.2f}x floor, below which a "
                       "prediction is inside the model's own error")
    return bar, "; ".join(reasons)


def estimate(spec: state_def.ISASpec,
             report: state_def.HotLoopReport,
             core_profile=None,
             measured_ops: dict | None = None) -> SpeedupEstimate:
    """Predict the extension's whole-application speedup.

    Each instruction declares which mined loop it accelerates (``replaces``) and
    how many times it runs per call of that loop (``invocations``). The loop's
    measured cost and call count come from A1's profile, so the software side is
    measurement and only the hardware side is modelled.
    """
    plat = Platform.from_core(core_profile)
    loops = {l.loop_id: l for l in report.loops}
    # Exact loop ids only. Matching `uECC_vli_mult::inner_loop` to the whole
    # `uECC_vli_mult` function credited an instruction that replaces one
    # iteration with the cost of all sixty-four — an overcredit a designer can
    # reach for by naming a loop that was never mined.
    by_function: dict[str, state_def.HotLoop] = {}
    for l in report.loops:
        by_function.setdefault(l.function, l)

    # Shares must be relative to the WHOLE application, not to the mined
    # subset: A1 typically covers ~90% of cycles, and dividing by the mined
    # total instead would silently delete the unmined remainder from Amdahl's
    # denominator and overstate the speedup. `cycle_share` is already
    # app-relative (red.loop._merge_mechanical divides by every profiled
    # function); the recomputation is only a fallback for a report that has
    # none.
    total_ir = sum(l.dynamic_cycles for l in report.loops) or 0
    # Gate 3 prices software against measured cost *per call*, so a report whose
    # loops all lack call counts cannot score any design. That is a profiling
    # failure; say so instead of returning 1.00x and blaming the designer.
    if report.loops and not any(l.calls > 0 for l in report.loops):
        return SpeedupEstimate(
            covered_share=0.0, app_speedup=1.0,
            detail=("no mined loop carries a call count, so no design can be "
                    "priced"),
            measurement_error=(
                "The profile has no call counts for any mined loop "
                f"({', '.join(l.loop_id for l in report.loops[:4])}). Gate 3 "
                "compares an instruction against the measured cost of one call "
                "of the kernel it replaces, which is unobtainable without them. "
                "This is a profiling failure, not a design failure — check "
                "red.nodes._parse_callgrind_raw against the callgrind output on "
                "the profiling node."))
    costs: list[InstructionCost] = []
    # kernel -> (share of app cycles, best speedup claimed on it)
    accelerated: dict[str, tuple[float, float]] = {}

    measured_ops = measured_ops or {}
    ir_per_op = _IR_PER_OP_CACHE.get("v", _IR_PER_OP_FALLBACK)
    ir_per_step = _IR_PER_STEP_CACHE.get("v", _IR_PER_LOGIC_FALLBACK)
    for ins in spec.instructions:
        beats = 2 * max(ins.words, 1)
        # Split the measured work into the part the declared multiplies can
        # account for and the part they cannot, and charge each at the rate the
        # datapath runs it. Pricing everything as multiplies was the defect
        # behind libcrc: its models perform 256 sequential bit steps per block
        # and the synthesizable RTL takes one cycle each, measuring 297-301
        # cycles where the multiply-rate model predicted ~112.
        #
        # This also subsumes the older "declares mac_ops=0 but executes 60,000
        # instructions" override, and more accurately: that work is now charged
        # as the sequential steps it is, rather than as a number of multiplies
        # inferred through the wrong ratio.
        ops = ins.mac_ops
        logic_steps = 0.0
        note = ""
        ir = measured_ops.get(ins.mnemonic)
        if ir is not None and ir > _IR_FLOOR:
            mac_ir = min(float(ir), max(ins.mac_ops, 0) * ir_per_op)
            logic_steps = max(0.0, (ir - mac_ir) / ir_per_step)
            if logic_steps > max(ins.mac_ops, 1):
                note = (f"its C model executes {ir:,} instructions, of which "
                        f"{ir - mac_ir:,.0f} are not accounted for by "
                        f"mac_ops={ins.mac_ops}; charged as "
                        f"{logic_steps:.0f} sequential logic steps")
        cycles = plat.instruction_cycles(ins.words, ops) + logic_steps * LOGIC_CYCLES
        # Copy in and back out, at the measured marshalling rate rather than the
        # bare load/store rate. On the one call anyone has measured end to end,
        # this is 413 of 656 cycles -- the largest single component, and a
        # direct consequence of the memory-block ABI.
        marshal = ins.marshal_words * CYCLES_PER_MARSHAL_WORD * 2
        cost = InstructionCost(mnemonic=ins.mnemonic, words=ins.words,
                               mac_ops=ops, cycles=cycles, beats=beats,
                               marshal_cycles=marshal, replaces=ins.replaces,
                               note=note)

        loop = loops.get(ins.replaces)
        if loop is None and ins.replaces in by_function:
            # The function is mined but this exact region is not. Credit it only
            # against the whole function, and only if the instruction plausibly
            # does the whole function's work (checked below).
            loop = by_function[ins.replaces]
        if loop is None:
            cost.note = ((cost.note + "; " if cost.note else "")
                         + f"replaces={ins.replaces!r} matches no mined loop — "
                         "no benefit can be credited")
            costs.append(cost)
            continue
        if loop.calls <= 0:
            cost.note = ((cost.note + "; " if cost.note else "")
                         + f"{loop.loop_id} has no call count in the profile — "
                         "no benefit can be credited")
            costs.append(cost)
            continue

        # What one call of the kernel costs today, in target cycles.
        kernel_ir_per_call = loop.dynamic_cycles / loop.calls
        kernel_cycles = kernel_ir_per_call * plat.cycles_per_ir

        # `invocations` — how many times the instruction runs per call of the
        # kernel — was the one cost declaration nothing ever measured. mac_ops
        # is profiled from the reference model and `replaces` is matched against
        # the frozen report, but this was taken on the designer's word, and it
        # was wrong by one to two orders of magnitude in two of three shipped
        # specifications: libcrc declared 2 where a 4 KiB buffer needs 128, and
        # matrixmul declared 4 where a 10x10 multiply needs 500. Understating it
        # understates the hardware side of the comparison by the same factor,
        # because the extension is charged for n invocations and credited with
        # the whole kernel.
        #
        # It is derivable: the kernel executes `kernel_ir_per_call` host
        # instructions per call and the model executes `ir` per invocation, so
        # their ratio is how many invocations that call needs. Measure it.
        n = max(ins.invocations, 1)
        # `invocations` is the one cost declaration RED cannot derive, and it
        # was wrong by one to two orders of magnitude in two of three shipped
        # specifications (libcrc declared 2 where a 4 KiB buffer needs 128,
        # matrixmul 4 where a 10x10 multiply needs 500).
        #
        # The tempting estimator -- kernel instructions per call divided by the
        # model's instructions per invocation -- is *biased*, because the two
        # sides are different implementations of the same work: it reads 31 for
        # libcrc's 128 because the model spends more instructions per byte than
        # the table-driven software does, and 147 for matrixmul's 4 because the
        # model spends fewer. Substituting it silently moved the prediction by
        # an order of magnitude in both directions. So it is a *check*, not a
        # measurement: the discrepancy is reported to the designer and carried
        # into the gate's own detail, and the number stays the designer's until
        # RED can measure it.
        if ir is not None and ir > _IR_FLOOR and kernel_ir_per_call > 0:
            implied = kernel_ir_per_call / ir
            if implied > n * _INVOCATION_MISMATCH or n > implied * _INVOCATION_MISMATCH:
                cost.unverified = (
                    f"invocations={ins.invocations} is the designer's "
                    f"declaration and RED cannot verify it: one call of "
                    f"{loop.function} executes {kernel_ir_per_call:,.0f} host "
                    f"instructions against this model's {ir:,}, which is "
                    f"consistent with roughly {implied:,.0f} invocations, not "
                    f"{ins.invocations}. That ratio is itself only indicative — "
                    "the two sides are different implementations — but the gap "
                    "is too large to ignore. Check it against the data volume: "
                    "how many bytes does one call process, and how many does "
                    "one invocation?")
                cost.note = ((cost.note + "; " if cost.note else "")
                             + f"invocations UNVERIFIED (~{implied:,.0f} implied)")
        cost.invocations = n
        cost.replaced_cycles = kernel_cycles / n

        # Does the instruction actually do the work it is being paid for? The
        # kernel performs `kernel_ir / n` host instructions per invocation; the
        # C model was measured. An instruction credited far more than it
        # computes is claiming someone else's cycles — usually by naming a
        # whole function while implementing one iteration of it, or the reverse.
        replaced_per_invocation = cost.replaced_cycles
        if ir is not None and ir > _IR_FLOOR:
            expected_ir = kernel_ir_per_call / n
            if expected_ir > 0 and ir < expected_ir / _WORK_MISMATCH:
                credited = ir * plat.cycles_per_ir
                cost.note = ((cost.note + "; " if cost.note else "") +
                             f"claims to replace {expected_ir:.0f} host "
                             f"instructions per invocation but its C model "
                             f"executes only {ir:,} — credited on what it "
                             f"computes, not on what it names")
                cost.replaced_cycles = credited
                replaced_per_invocation = credited
        cost.speedup = cost.replaced_cycles / cost.total() if cost.total() else 0.0

        share = loop.cycle_share or (
            (loop.dynamic_cycles / total_ir) * report.coverage if total_ir else 0.0)
        # Whatever the instruction does not replace still runs in software. An
        # instruction that covers a sixty-fourth of its kernel leaves the other
        # sixty-three sixty-fourths at full price, and the kernel barely moves —
        # which is exactly why replacing a loop body instead of a loop loses.
        unreplaced = max(0.0, kernel_cycles - replaced_per_invocation * n)
        new_kernel = unreplaced + n * cost.total()
        # A kernel accelerated by several instructions keeps the best claim
        # rather than compounding them — they are alternatives, not a pipeline.
        kernel_speedup = (kernel_cycles / new_kernel) if new_kernel > 0 else 0.0
        # Keyed by FUNCTION, not by loop id. A1 routinely mines two regions of
        # one kernel and gives each the function's whole cycle share: micro-ecc
        # reported two loops inside uECC_vli_mult at 47.3% each, plus the add
        # and sub loops those kernels call, and the shares summed to 143%
        # because the regions nest. Gate 3 was arithmetically correct on the
        # report it was given and credited a two-instruction design with 94.6%
        # of the application for replacing two halves of one function. One
        # function's cycles can only be spent once.
        key = loop.function or loop.loop_id
        prev = accelerated.get(key)
        if prev is None or kernel_speedup > prev[1]:
            accelerated[key] = (max(share, prev[0]) if prev else share,
                                kernel_speedup)
        costs.append(cost)

    # ...and the total still cannot exceed the application. Nested mined regions
    # can make even the per-function shares sum past 1.0; scale them back rather
    # than handing Amdahl a negative remainder.
    covered = sum(sh for sh, _ in accelerated.values())
    if covered > 1.0:
        scale = 1.0 / covered
        accelerated = {k: (sh * scale, sp) for k, (sh, sp) in accelerated.items()}
        covered = 1.0
    # Amdahl over the whole application: everything the extension does not
    # touch — unaccelerated mined loops *and* the cycles A1 never mined — runs
    # at its original speed and bounds the result.
    remainder = max(0.0, 1.0 - covered)
    new_time = remainder + sum(sh / sp for sh, sp in accelerated.values() if sp > 0)
    app_speedup = (1.0 / new_time) if new_time > 0 else 1.0

    lines = [f"covers {covered:.1%} of profiled cycles; "
             f"predicted whole-application speedup {app_speedup:.2f}x"]
    for c in costs:
        wpb = work_per_beat(c.words, c.mac_ops)
        lines.append(
            f"  {c.mnemonic}: {c.cycles:.0f} cyc "
            f"({c.beats} beats, {c.mac_ops} MAC, {wpb:.2f} MAC/word"
            + (f", x{c.invocations:,.0f} per call" if c.invocations > 1 else "")
            + ")"
            + (f" + {c.marshal_cycles:.0f} marshalling" if c.marshal_cycles else "")
            + (f" vs {c.replaced_cycles:.0f} cyc of software = {c.speedup:.2f}x"
               if c.replaced_cycles else "")
            + (f" — {c.note}" if c.note else ""))
    unverified = [c.unverified for c in costs if c.unverified]
    if unverified:
        lines.append("")
        lines.append("**This prediction rests on an unverified declaration.** "
                     "Gate 3 measures everything else it uses — the software "
                     "cost from the profile, the instruction's work from its own "
                     "C model — but not this:")
        for u in unverified:
            lines.append(f"  - {u}")
    return SpeedupEstimate(per_instruction=costs, covered_share=covered,
                           app_speedup=app_speedup, detail="\n".join(lines),
                           unverified=unverified)


def render_feedback(est: SpeedupEstimate, target: float = SPEEDUP_TARGET,
                    platform: "Platform | None" = None) -> str:
    """The performance verdict as the designer reads it."""
    out = ["# Performance gate", "",
           f"Target: {target:.2f}x whole-application speedup.",
           f"Predicted: **{est.app_speedup:.2f}x** over {est.covered_share:.1%} "
           f"of profiled cycles.", ""]
    if est.meets_target(target):
        out.append("The extension meets the target.")
        return "\n".join(out) + "\n\n```\n" + est.detail + "\n```\n"

    out += ["The extension does **not** meet the target. Per instruction:", "",
            "```", est.detail, "```", "",
            "## How the cost is computed", "",
            f"    cycles = {(platform or Platform()).fixed_cycles:g} + "
            f"{(platform or Platform()).cycles_per_beat:g} x (2 x words) "
            f"+ {(platform or Platform()).mac_cycles:g} x mac_ops",
            "",
            "The operand traffic is fixed by `words`, so the only way to make an "
            "instruction pay is to do **more arithmetic per operand word**. Ways "
            "to raise it, in order of effect:", "",
            "1. **Fuse the loop, not the loop body.** An instruction that "
            "replaces one iteration moves its operands every iteration; one that "
            "replaces the whole loop moves them once. A 32x32 multiply-accumulate "
            "over a 5-word block does 0.1 MAC per word moved; a 256x256 multiply "
            "over a 16-word block does 2.0 — twenty times the work for the same "
            "traffic.",
            "2. **Raise `invocations` coverage.** Check how many times your "
            "instruction must run per call of the kernel it replaces: if that "
            "number is in the tens, each invocation is paying the fixed "
            f"{FIXED_CYCLES}-cycle overhead again.",
            "3. **Drive `marshal_words` to zero.** Operands the caller has to "
            "copy into your block cost "
            f"{(platform or Platform()).cpu_load_store * 2:g} cycles per word, "
            "on top of the instruction. "
            "Choose an operand layout the application already has.",
            "4. **Cover a hotter kernel.** Amdahl caps you at the cycle share "
            "you touch; accelerating 6% of the profile perfectly yields 1.06x."]
    return "\n".join(out) + "\n"
