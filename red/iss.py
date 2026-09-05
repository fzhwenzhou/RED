"""Gate 2 — the C model <=> patched Spike ISS differential test.

The RED design gates an ISASpec on two *independently written* executable
models of the same instruction agreeing on 10^5 random + corner vectors:

  * ``InstructionSpec.c_model``     — S2b's C reference model (link 1 builds
    and stress-runs it; here it is the differential test's reference side).
  * ``InstructionSpec.spike_model`` — written by a separate agent from the
    spec's prose, pseudo-code and encoding ALONE (never from ``c_model``), and
    compiled into a Spike extension so the real ISS executes it.

Two implementations agreeing does not prove either correct, but a disagreement
is a hard counterexample: the spec was ambiguous enough that two readers built
different machines from it.

What makes this a genuine *ISS* test rather than two C functions compared: the
Spike side goes through the whole simulator — the encoding is decoded from the
rv32 instruction stream, operands arrive in architectural registers, and the
model reads and writes simulated memory through the MMU. An encoding collision,
a bad funct3/funct7, or a wrong operand convention fails here and cannot fail in
link 1.

Instruction ABI (fixed by RED so any instruction can be harnessed mechanically):

    <mnemonic> rd, rs1, rs2
        rs1  address of the input block  (`words` x uint32, little-endian)
        rs2  address of the output block (`words` x uint32)
        rd   written 0, so rd is a real destination and the R-type is well formed

which lines up 1:1 with the C model's ``void f(const uint32_t in[], uint32_t out[])``.

The comparison runs in two phases so a failure yields an exact counterexample:

    phase 1  both sides hash every output vector and emit one FNV-1a checksum
             per 1024-vector block -> agree, or the first divergent block
    phase 2  both sides dump the full output words for that block -> the first
             divergent vector, with its inputs and both models' outputs

The target program reports through Spike's ``+signature`` memory dump rather
than a console, so nothing depends on HTIF plumbing beyond the exit word.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from string import Template

from red import state_def
from red.constants import (
    GATE2_BLOCK,
    RISCV_GCC,
    SPIKE_BIN,
    SPIKE_BUILD,
    SPIKE_PREFIX,
    SPIKE_SRC,
)

# The four RISC-V opcode slots reserved for non-standard extensions.
CUSTOM_OPCODES = {"custom-0": 0x0B, "custom-1": 0x2B, "custom-2": 0x5B, "custom-3": 0x7B}

# Corner vectors every run starts with, before the pseudo-random ones. Both
# sides must generate the identical sequence, so this list and the xorshift
# seed below are duplicated verbatim in the C and the RISC-V harness.
CORNER_WORDS = (0x00000000, 0xFFFFFFFF, 0xAAAAAAAA, 0x55555555)
N_CORNERS = len(CORNER_WORDS)
PRNG_SEED = 0xC0FFEE


# ---------------------------------------------------------------------------
# Encoding
# ---------------------------------------------------------------------------

def iss_function(mnemonic: str) -> str:
    """Name the Spike-side model must define, e.g. ``modadd_iss``."""
    return f"{state_def.c_identifier(mnemonic)}_iss"


def _bitfield(value: str, width: int, what: str, mnemonic: str) -> int:
    s = (value or "").strip()
    if not re.fullmatch(rf"[01]{{{width}}}", s):
        raise ValueError(f"instruction {mnemonic!r}: {what} must be {width} binary "
                         f"digits, got {value!r}")
    return int(s, 2)


def match_mask(ins: state_def.InstructionSpec) -> tuple[int, int]:
    """The (match, mask) pair Spike decodes this instruction with.

    R-type: funct7 in 31:25, rs2 24:20, rs1 19:15, funct3 14:12, rd 11:7,
    opcode 6:0. The mask pins funct7 + funct3 + opcode and leaves the three
    register fields free."""
    if ins.opcode not in CUSTOM_OPCODES:
        raise ValueError(f"instruction {ins.mnemonic!r}: opcode must be one of "
                         f"{sorted(CUSTOM_OPCODES)}, got {ins.opcode!r}")
    op = CUSTOM_OPCODES[ins.opcode]
    f3 = _bitfield(ins.funct3, 3, "funct3", ins.mnemonic)
    f7 = _bitfield(ins.funct7, 7, "funct7", ins.mnemonic)
    return (f7 << 25) | (f3 << 12) | op, 0xFE00707F


def encoding_slots(spec: state_def.ISASpec) -> dict[tuple[str, str, str], list[str]]:
    """Map each (opcode, funct3, funct7) slot to the mnemonics claiming it —
    the raw material for the collision check."""
    slots: dict[tuple[str, str, str], list[str]] = {}
    for ins in spec.instructions:
        slots.setdefault((ins.opcode, ins.funct3, ins.funct7), []).append(ins.mnemonic)
    return slots


# ---------------------------------------------------------------------------
# Toolchain
# ---------------------------------------------------------------------------

def _spike_bin() -> str | None:
    return SPIKE_BIN if os.path.isfile(SPIKE_BIN) else shutil.which("spike")


def _riscv_gcc() -> str | None:
    return shutil.which(RISCV_GCC)


def spike_env() -> dict:
    """Environment Spike runs under: the prefix's ``bin`` on PATH (Spike shells
    out to ``dtc`` when it builds the device tree) and its libraries on
    LD_LIBRARY_PATH. Harmless when the prefix is not the install location."""
    env = dict(os.environ)
    extra_path = [os.path.join(SPIKE_PREFIX, "bin"), os.path.join(SPIKE_PREFIX, "usr", "bin")]
    env["PATH"] = os.pathsep.join([p for p in extra_path if os.path.isdir(p)] +
                                  [env.get("PATH", "")])
    libs = [os.path.join(SPIKE_PREFIX, "lib")]
    usr_lib = os.path.join(SPIKE_PREFIX, "usr", "lib")
    if os.path.isdir(usr_lib):
        libs += [os.path.join(usr_lib, d) for d in sorted(os.listdir(usr_lib))
                 if os.path.isdir(os.path.join(usr_lib, d))]
    libs = [p for p in libs if os.path.isdir(p)]
    if libs:
        env["LD_LIBRARY_PATH"] = os.pathsep.join(libs + [env.get("LD_LIBRARY_PATH", "")])
    return env


def toolchain_status() -> tuple[bool, str]:
    """(ready, detail) — whether Gate 2 can run here at all."""
    missing = []
    if _spike_bin() is None:
        missing.append(f"spike (looked at {SPIKE_BIN} and $PATH)")
    if _riscv_gcc() is None:
        missing.append(f"{RISCV_GCC}")
    if not os.path.isdir(SPIKE_SRC):
        missing.append(f"spike headers ({SPIKE_SRC})")
    if not os.path.isfile(os.path.join(SPIKE_BUILD, "config.h")):
        missing.append(f"spike config.h ({SPIKE_BUILD})")
    if missing:
        return False, ("Gate 2 needs a patched-Spike toolchain; missing: "
                       + ", ".join(missing) + " — run scripts/setup_spike.sh")
    return True, "spike + riscv gcc present"


# ---------------------------------------------------------------------------
# Spike extension: the ISS side of the differential test
# ---------------------------------------------------------------------------

_EXTENSION_CC = Template(r'''
/* Generated by RED (red/iss.py) from ISASpec "$spec_name" — do not edit.
 *
 * One Spike extension holding every designed instruction. Each is a plain
 * R-type in a custom opcode slot: rs1 = &in[words], rs2 = &out[words], rd = 0.
 */
/* libstdc++'s <atomic_wait> reaches for SYS_futex, which spike's own build gets
   transitively; include it up front so this stands alone. */
#include <sys/syscall.h>

/* Every file that uses spike's register macros must first say whether register
   writes go into the commit log. Spike's own instruction bodies define it (0 in
   insn_template.cc, 1 in rocc.cc); 1 keeps --log-commits honest for our
   instructions too. */
#define DECODE_MACRO_USAGE_LOGGED 1

#include "extension.h"
#include "decode_macros.h"
#include "mmu.h"

#include <cstdint>
#include <cstring>
#include <vector>

$models

$execs

class red_ext_t : public extension_t
{
 public:
  const char* name() const override { return "red"; }

  std::vector<insn_desc_t> get_instructions(const processor_t &) override {
    return {
$descs
    };
  }

  std::vector<disasm_insn_t*> get_disasms(const processor_t *) override {
    return {
$disasms
    };
  }
};

REGISTER_EXTENSION(red, []() { static red_ext_t ext; return &ext; })
''')

# Each designer-written model goes in its own namespace: two instructions may
# both define a helper called `add_carry`, and without this they collide at
# link time and the whole extension fails to build for a reason that has
# nothing to do with either instruction's correctness.
_EXT_MODEL = Template(r'''
/* ---- ISS model for $mnemonic ($words x 32-bit words) ---- */
namespace red_$ident {
$model
}
''')

_EXT_EXEC = Template(r'''
static reg_t red_exec_$ident(processor_t* p, insn_t insn, reg_t pc)
{
  const reg_t src = RS1, dst = RS2;
  uint32_t in[$words], out[$words];

  for (int i = 0; i < $words; i++)
    in[i] = (uint32_t)MMU.load<uint32_t>(src + 4 * i);
  memset(out, 0, sizeof(out));

  red_$ident::$fn(in, out);

  for (int i = 0; i < $words; i++)
    MMU.store<uint32_t>(dst + 4 * i, out[i]);

  WRITE_RD(0);
  return pc + 4;
}
''')


def render_extension(spec: state_def.ISASpec) -> str:
    """The C++ source of the Spike extension implementing every instruction."""
    models, execs, descs, disasms = [], [], [], []
    for ins in spec.instructions:
        ident = state_def.c_identifier(ins.mnemonic)
        fn = iss_function(ins.mnemonic)
        match, mask = match_mask(ins)
        models.append(_EXT_MODEL.substitute(mnemonic=ins.mnemonic, words=ins.words,
                                            ident=ident, model=ins.spike_model))
        execs.append(_EXT_EXEC.substitute(ident=ident, words=ins.words, fn=fn))
        f = f"red_exec_{ident}"
        descs.append(f"      {{0x{match:08x}, 0x{mask:08x}, "
                     + ", ".join([f] * 8) + "},")
        disasms.append(f'      new disasm_insn_t("{ins.mnemonic}", '
                       f"0x{match:08x}, 0x{mask:08x}, {{}}),")
    return _EXTENSION_CC.substitute(
        spec_name=spec.name, models="\n".join(models), execs="\n".join(execs),
        descs="\n".join(descs), disasms="\n".join(disasms))


def build_extension(spec: state_def.ISASpec, work_dir: str) -> tuple[str | None, str]:
    """Compile the extension to a shared object Spike loads with --extlib.

    Building a .so beats patching and rebuilding Spike itself: it takes seconds
    per candidate spec instead of a quarter of an hour, so the critic can afford
    to iterate."""
    os.makedirs(work_dir, exist_ok=True)
    src = os.path.join(work_dir, "red_ext.cc")
    so = os.path.join(work_dir, "libred_ext.so")
    with open(src, "w") as f:
        f.write(render_extension(spec))

    includes = [SPIKE_BUILD, SPIKE_SRC]
    includes += [os.path.join(SPIKE_SRC, d)
                 for d in ("riscv", "fesvr", "softfloat", "disasm", "customext", "fdt")]
    cmd = (["g++", "-std=c++2a", "-O1", "-fPIC", "-shared", "-o", so, src]
           + [f"-I{d}" for d in includes if os.path.isdir(d) or d == SPIKE_BUILD])
    p = subprocess.run(cmd, capture_output=True, text=True)
    if p.returncode != 0 or not os.path.exists(so):
        return None, f"spike extension does not compile: {p.stderr.strip()[-1500:]}"
    return so, "built"


# ---------------------------------------------------------------------------
# The two harnesses. Both walk the identical vector sequence — the corner
# vectors first, then xorshift32 from a fixed seed — so the only thing that can
# make their outputs differ is the models disagreeing.
# ---------------------------------------------------------------------------

_VECTOR_GEN = r'''
static uint32_t red_xs32(uint32_t *s) {
  uint32_t x = *s;
  x ^= x << 13; x ^= x >> 17; x ^= x << 5;
  return *s = x;
}

static const uint32_t RED_CORNERS[RED_NCORNER] = { $corners };

/* Vector i: the first RED_NCORNER are uniform corner patterns, the rest are
   drawn from the PRNG. The PRNG is advanced only by the random vectors, so
   both harnesses stay in lockstep. */
static void red_fill(long i, uint32_t *seed, uint32_t *in) {
  int w;
  if (i < RED_NCORNER) {
    for (w = 0; w < RED_WORDS; w++) in[w] = RED_CORNERS[i];
  } else {
    for (w = 0; w < RED_WORDS; w++) in[w] = red_xs32(seed);
  }
}

static uint32_t red_hash(uint32_t h, const uint32_t *v, int n) {
  int w, b;
  for (w = 0; w < n; w++)
    for (b = 0; b < 4; b++) {
      h ^= (v[w] >> (8 * b)) & 0xffu;
      h *= 16777619u;
    }
  return h;
}
'''

_NATIVE_C = Template(r'''
/* Generated by RED (red/iss.py): native reference run of $mnemonic's c_model.
 * Prints one 32-bit word per line, lowercase hex — the exact format Spike's
 * +signature dump uses, so the two are compared line by line. */
#include <stdint.h>
#include <stdio.h>
#include <string.h>

#define RED_WORDS   $words
#define RED_NVEC    $nvec
#define RED_NCORNER $ncorner
#define RED_BLOCK   $block

/* ---- designer-supplied C reference model ---- */
$model
/* ---- end model ---- */

$vector_gen

int main(void) {
  uint32_t in[RED_WORDS], out[RED_WORDS];
  uint32_t seed = ${seed}u, h = 2166136261u;
  long i, w, blk = 0;
  (void)blk;

  for (i = 0; i < RED_NVEC; i++) {
    red_fill(i, &seed, in);
    memset(out, 0, sizeof out);
    $fn(in, out);
    h = red_hash(h, out, RED_WORDS);
$emit
  }
  return 0;
}
''')

_RV32_C = Template(r'''
/* Generated by RED (red/iss.py): bare-metal rv32 run of $mnemonic on Spike.
 *
 * Executes the custom instruction itself — encoding, register operands and
 * memory traffic all go through the simulator — and leaves its results in the
 * .signature region, which Spike writes out with +signature. Freestanding: no
 * libc, no crt0, no HTIF console; the only host interaction is the exit word. */
#include <stdint.h>

#define RED_WORDS   $words
#define RED_NVEC    $nvec
#define RED_NCORNER $ncorner
#define RED_BLOCK   $block

/* fesvr finds these by symbol name; writing 1 to tohost exits the simulation.
   On rv32 that is two 32-bit stores, and both orderings are safe here: the
   intermediate value is either 0 (no request) or 1 (the exit request). */
volatile uint64_t tohost   __attribute__((section(".htif"), aligned(8)));
volatile uint64_t fromhost __attribute__((section(".htif"), aligned(8)));

/* Bracketed by begin_signature/end_signature in the linker script. Initialised
   to a sentinel so the section is PROGBITS and an entry the program never wrote
   is obvious in the dump rather than reading as a plausible zero. */
volatile uint32_t red_sig[$sig_words] __attribute__((section(".signature"), used))
  = { [0 ... $sig_last] = 0xdeadbeefu };

static uint32_t red_in[RED_WORDS], red_out[RED_WORDS];

$vector_gen

int main(void) {
  uint32_t seed = ${seed}u, h = 2166136261u;
  long i, w;
  long blk = 0;

  for (i = 0; i < RED_NVEC; i++) {
    red_fill(i, &seed, red_in);
    for (w = 0; w < RED_WORDS; w++) red_out[w] = 0;
    {
      uint32_t rd_val;
      const uint32_t *src = red_in;
      uint32_t *dst = red_out;
      /* <mnemonic> rd, rs1, rs2  with rs1 = &in, rs2 = &out */
      __asm__ __volatile__(".insn r $opcode_hex, $funct3_hex, $funct7_hex, %0, %1, %2"
                           : "=r"(rd_val) : "r"(src), "r"(dst) : "memory");
      (void)rd_val;
    }
    h = red_hash(h, (const uint32_t *)red_out, RED_WORDS);
$emit
  }

  tohost = 1;          /* exit(0) */
  for (;;) { }
}

void _start(void) __attribute__((naked, section(".text.init")));
void _start(void) {
  __asm__ __volatile__(
    "la sp, _stack_top\n"
    "call main\n"
    "la t0, tohost\n"
    "li t1, 1\n"
    "sw t1, 0(t0)\n"
    "1: j 1b\n");
}
''')

_LINKER_LD = r'''
OUTPUT_ARCH("riscv")
ENTRY(_start)
SECTIONS
{
  . = 0x80000000;
  .text.init : { *(.text.init) }
  .text      : { *(.text) *(.text.*) }
  .rodata    : { *(.rodata) *(.rodata.*) *(.srodata) *(.srodata.*) }
  .data      : { *(.data) *(.data.*) *(.sdata) *(.sdata.*) }
  .bss       : { *(.bss) *(.bss.*) *(.sbss) *(.sbss.*) *(COMMON) }
  . = ALIGN(8);
  .htif      : { *(.htif) }
  . = ALIGN(16);
  begin_signature = .;
  .signature : { *(.signature) }
  end_signature = .;
  . = ALIGN(16);
  . = . + 0x10000;
  _stack_top = .;
}
'''


def _vector_gen() -> str:
    return Template(_VECTOR_GEN).substitute(
        corners=", ".join(f"0x{c:08x}u" for c in CORNER_WORDS))


def _emit_blocks(target: str) -> str:
    """Phase-1 emitter: one checksum per finished block.

    The emitted text is substituted into the harness template as a *value*, so
    it must contain no ``$`` placeholders of its own — sizes are already fixed
    by the caller."""
    store = ("      red_sig[blk] = h; blk++;" if target == "rv32"
             else '      printf("%08x\\n", h);')
    return ("    if ((i + 1) % RED_BLOCK == 0 || i + 1 == RED_NVEC) {\n"
            + store + "\n    }")


def _emit_detail(target: str, dfrom: int, dto: int) -> str:
    """Phase-2 emitter: every output word for the vectors in [dfrom, dto)."""
    if target == "rv32":
        body = (f"      for (w = 0; w < RED_WORDS; w++)\n"
                f"        red_sig[(i - {dfrom}) * RED_WORDS + w] = red_out[w];")
    else:
        body = ('      for (w = 0; w < RED_WORDS; w++)\n'
                '        printf("%08x\\n", out[w]);')
    return (f"    (void)h; (void)blk;\n"
            f"    if (i >= {dfrom} && i < {dto}) {{\n{body}\n    }}")


def render_native(ins: state_def.InstructionSpec, nvec: int, emit: str) -> str:
    return _NATIVE_C.substitute(
        mnemonic=ins.mnemonic, words=ins.words, nvec=nvec, ncorner=N_CORNERS,
        block=GATE2_BLOCK, model=ins.c_model,
        fn=f"{state_def.c_identifier(ins.mnemonic)}_model",
        vector_gen=_vector_gen(), seed=f"0x{PRNG_SEED:x}", emit=emit)


def render_rv32(ins: state_def.InstructionSpec, nvec: int, emit: str,
                sig_words: int) -> str:
    op, f3, f7 = (CUSTOM_OPCODES[ins.opcode],
                  _bitfield(ins.funct3, 3, "funct3", ins.mnemonic),
                  _bitfield(ins.funct7, 7, "funct7", ins.mnemonic))
    return _RV32_C.substitute(
        mnemonic=ins.mnemonic, words=ins.words, nvec=nvec, ncorner=N_CORNERS,
        block=GATE2_BLOCK, vector_gen=_vector_gen(), seed=f"0x{PRNG_SEED:x}",
        emit=emit, sig_words=sig_words, sig_last=sig_words - 1,
        opcode_hex=f"0x{op:02x}", funct3_hex=f"0x{f3:x}", funct7_hex=f"0x{f7:02x}")


# ---------------------------------------------------------------------------
# Build + run
# ---------------------------------------------------------------------------

def _run_native(ins, work_dir, nvec, emit, tag) -> tuple[list[str] | None, str]:
    src = os.path.join(work_dir, f"native_{tag}.c")
    exe = os.path.join(work_dir, f"native_{tag}")
    with open(src, "w") as f:
        f.write(render_native(ins, nvec, emit))
    p = subprocess.run(["cc", "-std=c99", "-O2", "-o", exe, src],
                       capture_output=True, text=True)
    if p.returncode != 0:
        return None, f"native reference does not compile: {p.stderr.strip()[-800:]}"
    try:
        r = subprocess.run([exe], capture_output=True, text=True, timeout=600)
    except subprocess.TimeoutExpired:
        return None, "native reference did not terminate within 600s"
    if r.returncode != 0:
        return None, f"native reference exited {r.returncode}: {r.stderr.strip()[-400:]}"
    return r.stdout.split(), "ok"


def _run_spike(ins, work_dir, nvec, emit, sig_words, so, tag) -> tuple[list[str] | None, str]:
    src = os.path.join(work_dir, f"rv32_{tag}.c")
    elf = os.path.join(work_dir, f"rv32_{tag}.elf")
    lds = os.path.join(work_dir, "red.ld")
    sig = os.path.join(work_dir, f"rv32_{tag}.sig")
    with open(src, "w") as f:
        f.write(render_rv32(ins, nvec, emit, sig_words))
    with open(lds, "w") as f:
        f.write(_LINKER_LD)

    cc = subprocess.run(
        [RISCV_GCC, "-march=rv32im", "-mabi=ilp32", "-O2", "-std=gnu99",
         "-nostdlib", "-nostartfiles", "-ffreestanding", "-T", lds, "-o", elf, src],
        capture_output=True, text=True)
    if cc.returncode != 0:
        return None, f"rv32 harness does not build: {cc.stderr.strip()[-800:]}"

    spike = _spike_bin()
    cmd = [spike, "--isa=rv32im", f"--extlib={so}", "--extension=red",
           f"+signature={sig}", "+signature-granularity=4", elf]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=1800,
                           env=spike_env(), cwd=work_dir)
    except subprocess.TimeoutExpired:
        return None, "spike did not terminate within 1800s"
    if r.returncode != 0:
        return None, (f"spike exited {r.returncode}: "
                      f"{(r.stderr or r.stdout).strip()[-800:]}")
    if not os.path.exists(sig):
        return None, f"spike produced no signature dump: {(r.stderr or '').strip()[-400:]}"
    with open(sig) as f:
        return [ln.strip() for ln in f if ln.strip()], "ok"


# ---------------------------------------------------------------------------
# The differential test
# ---------------------------------------------------------------------------

def _first_difference(a: list[str], b: list[str]) -> int:
    for i, (x, y) in enumerate(zip(a, b)):
        if x != y:
            return i
    return len(a) if len(a) != len(b) else -1


def differential_test(ins: state_def.InstructionSpec, work_dir: str, so: str,
                      vectors: int) -> tuple[bool, str, str | None]:
    """Run one instruction's C model and its Spike implementation over the same
    *vectors* and compare. Returns (passed, detail, counterexample)."""
    os.makedirs(work_dir, exist_ok=True)
    nblocks = (vectors + GATE2_BLOCK - 1) // GATE2_BLOCK

    # -- phase 1: per-block checksums ---------------------------------------
    native, err = _run_native(ins, work_dir, vectors, _emit_blocks("native"), "blocks")
    if native is None:
        return False, err, None
    iss, err = _run_spike(ins, work_dir, vectors, _emit_blocks("rv32"), nblocks,
                          so, "blocks")
    if iss is None:
        return False, err, None

    iss = [w.lstrip("0").rjust(1, "0") for w in iss[:len(native)]]
    ref = [w.lstrip("0").rjust(1, "0") for w in native]
    bad = _first_difference(ref, iss)
    if bad < 0:
        return True, (f"C model and Spike agree on {vectors:,} vectors "
                      f"({nblocks} block checksums)"), None

    # -- phase 2: dump the divergent block and name the exact vector ---------
    lo = bad * GATE2_BLOCK
    hi = min(lo + GATE2_BLOCK, vectors)
    detail_native, err = _run_native(ins, work_dir, hi,
                                     _emit_detail("native", lo, hi), "detail")
    if detail_native is None:
        return False, f"divergence in block {bad} but detail run failed: {err}", None
    detail_iss, err = _run_spike(ins, work_dir, hi, _emit_detail("rv32", lo, hi),
                                 (hi - lo) * ins.words, so, "detail")
    if detail_iss is None:
        return False, f"divergence in block {bad} but detail run failed: {err}", None

    dn = [w.lstrip("0") or "0" for w in detail_native]
    di = [w.lstrip("0") or "0" for w in detail_iss[:len(dn)]]
    word = _first_difference(dn, di)
    if word < 0:
        return False, (f"block {bad} checksums differ but every output word in it "
                       "matches — the harnesses disagree, not the models"), None

    vec = lo + word // ins.words
    off = word % ins.words
    lo_v, hi_v = vec * ins.words, (vec + 1) * ins.words
    base = (vec - lo) * ins.words
    c_out = " ".join(f"{int(w, 16):08x}" for w in dn[base:base + ins.words])
    i_out = " ".join(f"{int(w, 16):08x}" for w in di[base:base + ins.words])
    counter = (f"vector #{vec} (of {vectors:,}), output word {off}:\n"
               f"  C model : {c_out}\n"
               f"  Spike   : {i_out}\n"
               f"  input   : {_describe_input(ins, vec)}")
    return False, (f"C model and Spike DISAGREE on vector #{vec} "
                   f"(first divergent block {bad})"), counter


def _describe_input(ins: state_def.InstructionSpec, index: int) -> str:
    """Reproduce vector *index* in Python — the same corner-then-xorshift
    sequence both harnesses walk — so the counterexample carries the actual
    input the two models disagreed on, not just its index."""
    if index < N_CORNERS:
        return " ".join(f"{CORNER_WORDS[index]:08x}" for _ in range(ins.words))
    seed = PRNG_SEED
    words = []
    for i in range(N_CORNERS, index + 1):
        words = []
        for _ in range(ins.words):
            seed ^= (seed << 13) & 0xFFFFFFFF
            seed ^= seed >> 17
            seed ^= (seed << 5) & 0xFFFFFFFF
            words.append(seed)
    return " ".join(f"{w:08x}" for w in words)


def run_gate2(spec: state_def.ISASpec, work_dir: str,
              vectors: int) -> tuple[bool, str, str | None]:
    """Gate 2 over a whole ISASpec: build the extension once, then differential
    test every instruction. Returns (passed, detail, counterexample)."""
    ready, why = toolchain_status()
    if not ready:
        return False, why, None

    slots = encoding_slots(spec)
    collisions = {k: v for k, v in slots.items() if len(v) > 1}
    if collisions:
        return False, "encoding collision", "\n".join(
            f"{op}/funct3={f3}/funct7={f7} claimed by: {', '.join(ms)}"
            for (op, f3, f7), ms in collisions.items())

    for ins in spec.instructions:
        if not ins.spike_model:
            return False, f"instruction {ins.mnemonic!r} has no spike_model", None
        if iss_function(ins.mnemonic) not in ins.spike_model:
            return False, (f"instruction {ins.mnemonic!r}: spike_model must define "
                           f"{iss_function(ins.mnemonic)}"), None

    so, log = build_extension(spec, work_dir)
    if so is None:
        return False, log, log

    failures = []
    for ins in spec.instructions:
        ok, detail, counter = differential_test(
            ins, os.path.join(work_dir, state_def.c_identifier(ins.mnemonic)),
            so, vectors)
        if not ok:
            failures.append(f"{ins.mnemonic}: {detail}"
                            + (f"\n{counter}" if counter else ""))
    n = len(spec.instructions)
    if failures:
        return False, (f"{n - len(failures)}/{n} instructions agree with Spike over "
                       f"{vectors:,} vectors"), "\n\n".join(failures)
    return True, (f"{n}/{n} instructions: C model and patched Spike agree on "
                  f"{vectors:,} random + corner vectors"), None
