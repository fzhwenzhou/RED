"""Gate 4 — the security bound on a designed ISA extension.

An ISA extension is a permanent widening of the machine's attack surface, and
nothing else in the RED loop asks about it. Gate 1 asks whether the profile is
reproducible; link 1 and Gate 2 ask whether an instruction *means what its spec
says*; Gate 3 asks whether it is *faster*. All four can pass on an instruction
that leaks the private key it was designed to multiply:

* a custom instruction executes at the privilege of whoever issues it, and a
  coprocessor that masters the bus for its own operands reads memory that the
  issuing code may not own;
* the core is **stalled for the instruction's whole latency**, so a long
  operation is an interrupt-latency bound, i.e. a denial-of-service surface for
  anything real-time sharing the core;
* on the kernels RED actually mines — big-integer modular arithmetic — the
  operands *are* the secret, so any data-dependent latency or memory address is
  a side channel;
* and an instruction that fails to write part of its output block hands the
  caller back whatever was in that memory, which on the previous call was
  somebody's scalar.

**The rule of this module: a machine decides.** Large-model judgement is
valuable here and is used (``red/prompts/security.md`` drives a sub-agent that
reads the spec for what no checker can see) but it is not *trusted*: every
finding carries a ``confidence``, only ``confirmed`` (a tool observed it) and
``derived`` (arithmetic over the spec's own declared fields) can block the gate,
and the agent's own findings are advisory by construction. What the agent can
do is aim the machinery: it proposes concrete input vectors, the loop executes
them under the sanitizers, and a vector that actually breaks a model comes back
as ``confirmed`` evidence through the same path as everything else.

The checks, and how each one is answered:

==========================  ==========  =========================================
check                       confidence  how it is decided
==========================  ==========  =========================================
encoding.opcode             derived     the 7 opcode bits, decoded
encoding.fields             derived     funct3/funct7 field widths
encoding.collision          derived     two instructions in one encoding slot
model.forbidden_call        derived     tokenised scan for libc/allocation calls
model.includes              derived     headers outside the C99 contract
latency.interrupt_bound     derived     the platform cost model's cycle count
memory.bounds               confirmed   ASan + UBSan over adversarial vectors,
                                        operand blocks in exactly-sized heap
                                        allocations so a one-word overrun traps
memory.input_mutation       confirmed   a shadow copy of the input block
memory.output_residue       confirmed   two differently poisoned output blocks
model.impure                confirmed   same input, two calls, two answers
timing.data_dependent       confirmed   callgrind Ir of the model itself, per
                                        operand value, collection toggled inside
                                        the model
timing.secret_branch        confirmed   memcheck with the operand block marked
                                        undefined (the ctgrind technique)
timing.taint_scan           heuristic   taint propagation from ``in[]`` to
                                        control flow and subscripts
agent.*                     agent       the security sub-agent's reading
==========================  ==========  =========================================

Every dynamic check degrades to an entry in ``checks_skipped`` when its tool is
missing. A check that did not run is never reported as a check that passed.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess

from red import callgrind, state_def
from red.constants import (
    SECURITY_CT_SEVERITY,
    SECURITY_CT_VECTORS,
    SECURITY_MAX_FINDINGS,
    SECURITY_MAX_STALL_CYCLES,
    SECURITY_VECTORS,
)
from red.state_def import SecurityFinding, SecurityReport

# ---------------------------------------------------------------------------
# Static: the encoding
# ---------------------------------------------------------------------------

# RISC-V reserves these four 7-bit opcodes for non-standard extensions. An
# encoding whose opcode field is anything else is not "a custom instruction with
# an unusual opcode" — it is an alias of an existing instruction, and the core
# will execute whichever the decoder matches first.
_CUSTOM_BITS = {
    "custom-0": "0001011",
    "custom-1": "0101011",
    "custom-2": "1011011",
    "custom-3": "1111011",
}

# What a mis-stated opcode field would actually decode as, so the finding names
# the consequence rather than just the mismatch.
_STANDARD_OPCODES = {
    "0110011": "OP (the base integer register-register instructions)",
    "0010011": "OP-IMM (ADDI and friends)",
    "0000011": "LOAD",
    "0100011": "STORE",
    "1100011": "BRANCH",
    "1101111": "JAL",
    "1100111": "JALR",
    "0110111": "LUI",
    "0010111": "AUIPC",
    "1110011": "SYSTEM — ECALL/EBREAK and every CSR access",
    "0001111": "MISC-MEM (FENCE)",
    "1010011": "OP-FP",
    "0000111": "LOAD-FP",
    "0100111": "STORE-FP",
    "0111011": "OP-32 (RV64 only)",
    "0011011": "OP-IMM-32 (RV64 only)",
}


def _bits(token: str) -> bool:
    return bool(token) and all(c in "01" for c in token)


def _check_encoding(spec: state_def.ISASpec) -> list[SecurityFinding]:
    """Decode every instruction's encoding the way a decoder would."""
    out: list[SecurityFinding] = []
    slots: dict[tuple, str] = {}
    for ins in spec.instructions:
        opcode = state_def.normalize_opcode(ins.opcode)
        want = _CUSTOM_BITS.get(opcode, "")
        tokens = str(ins.encoding).replace("|", " ").split()
        tail = tokens[-1] if tokens else ""
        if _bits(tail) and len(tail) == 7:
            if want and tail != want:
                named = _STANDARD_OPCODES.get(tail)
                out.append(SecurityFinding(
                    check="encoding.opcode", severity="blocking",
                    confidence="derived", instruction=ins.mnemonic,
                    finding=(f"the encoding's opcode field is {tail}, which is "
                             + (f"{named}" if named
                                else "not one of the four custom opcodes")
                             + f", but the instruction declares {opcode}"),
                    evidence=f"encoding = {ins.encoding!r}; {opcode} is {want}",
                    fix=(f"write the low 7 bits as {want} so the instruction "
                         f"decodes in the custom space and cannot alias an "
                         f"existing instruction")))
        elif want and want not in str(ins.encoding):
            out.append(SecurityFinding(
                check="encoding.opcode", severity="major", confidence="derived",
                instruction=ins.mnemonic,
                finding="the encoding string never states the 7-bit opcode "
                        "field, so what this instruction decodes as is unstated",
                evidence=f"encoding = {ins.encoding!r}",
                fix=f"end the encoding with the literal opcode bits {want}"))

        for name, width in (("funct3", 3), ("funct7", 7)):
            val = str(getattr(ins, name) or "")
            if not _bits(val) or len(val) != width:
                out.append(SecurityFinding(
                    check="encoding.fields", severity="major",
                    confidence="derived", instruction=ins.mnemonic,
                    finding=(f"{name} is {val!r}, not {width} bits — every bit "
                             f"left unfixed makes this instruction decode at "
                             f"2^{width - len(val) if _bits(val) else width} "
                             f"encodings instead of one"),
                    evidence=f"{name} = {val!r}",
                    fix=f"give {name} exactly {width} binary digits"))

        key = (opcode, str(ins.funct3), str(ins.funct7))
        if key in slots:
            out.append(SecurityFinding(
                check="encoding.collision", severity="blocking",
                confidence="derived", instruction=ins.mnemonic,
                finding=f"shares the encoding slot {key} with {slots[key]!r}; "
                        "which one a decoder picks is undefined",
                evidence=f"opcode={key[0]} funct3={key[1]!r} funct7={key[2]!r}",
                fix="move one of the two to a free (funct3, funct7) slot"))
        else:
            slots[key] = ins.mnemonic
    return out


# ---------------------------------------------------------------------------
# Static: the C model's text
# ---------------------------------------------------------------------------

# Calls that cannot exist inside an instruction. Allocation, I/O, the clock, the
# environment and non-local jumps are not things a coprocessor can do; a model
# that reaches for one is not modelling the instruction it claims to, and its
# behaviour on real hardware is undefined.
_FORBIDDEN = (
    "malloc", "calloc", "realloc", "free", "alloca", "mmap", "munmap",
    "printf", "fprintf", "sprintf", "snprintf", "puts", "putchar", "scanf",
    "fopen", "fclose", "fread", "fwrite", "fgets", "gets", "open", "read",
    "write", "system", "popen", "execl", "execv", "fork", "pthread_create",
    "rand", "srand", "random", "arc4random", "getrandom",
    "time", "clock", "gettimeofday", "getenv", "setenv",
    "exit", "_exit", "abort", "raise", "signal", "setjmp", "longjmp",
    "strcpy", "strcat", "atoi", "atol", "strtol",
)
# The C99 contract in the designer's charter: these and nothing else.
_ALLOWED_INCLUDES = ("stdint.h", "string.h", "stddef.h", "limits.h")


def _strip_c(text: str) -> str:
    """Comments and string/char literals removed, so a scan matches code.

    Without this, the word ``free`` in a comment is a blocking finding and the
    checker is worse than useless.
    """
    out, i, n = [], 0, len(text)
    while i < n:
        c = text[i]
        if c == "/" and i + 1 < n and text[i + 1] == "*":
            j = text.find("*/", i + 2)
            i = n if j < 0 else j + 2
            out.append(" ")
        elif c == "/" and i + 1 < n and text[i + 1] == "/":
            j = text.find("\n", i)
            i = n if j < 0 else j
            out.append(" ")
        elif c in "\"'":
            quote, j = c, i + 1
            while j < n and text[j] != quote:
                j += 2 if text[j] == "\\" else 1
            i = j + 1
            out.append(" ")
        else:
            out.append(c)
            i += 1
    return "".join(out)


def _check_model_text(ins: state_def.InstructionSpec) -> list[SecurityFinding]:
    out: list[SecurityFinding] = []
    src = _strip_c(ins.c_model or "")
    for name in _FORBIDDEN:
        if re.search(rf"\b{name}\s*\(", src):
            out.append(SecurityFinding(
                check="model.forbidden_call", severity="blocking",
                confidence="derived", instruction=ins.mnemonic,
                finding=f"the C model calls {name}() — an instruction cannot "
                        "allocate, do I/O, read the clock or the environment, "
                        "or jump non-locally, so this model does not describe "
                        "the hardware it claims to",
                evidence=f"{name}( appears in c_model",
                fix=f"remove the call to {name}() and express the instruction's "
                    "effect as arithmetic on in[] and out[] alone"))
    for m in re.finditer(r"#\s*include\s*[<\"]([^>\"]+)[>\"]", src):
        header = m.group(1).strip()
        if header not in _ALLOWED_INCLUDES:
            out.append(SecurityFinding(
                check="model.includes", severity="blocking",
                confidence="derived", instruction=ins.mnemonic,
                finding=f"the C model includes <{header}>, which is outside the "
                        "C99 contract the instruction is specified under",
                evidence=f"#include <{header}>",
                fix="use only <stdint.h> and <string.h>"))
    return out


# ---------------------------------------------------------------------------
# Static: taint from the operand block to control flow
# ---------------------------------------------------------------------------

_ASSIGN = re.compile(
    r"(?:^|[;{}()])\s*(?:[A-Za-z_][\w\s\*]*?\s)?([A-Za-z_]\w*)\s*"
    r"(?:\[[^\]]*\])?\s*(?:=|\+=|-=|\*=|\|=|&=|\^=|<<=|>>=)\s*([^;]*);")
_KEYWORDS = {"if", "while", "for", "switch", "return", "sizeof", "do", "else",
             "int", "long", "char", "void", "const", "static", "unsigned",
             "uint32_t", "uint64_t", "int32_t", "int64_t", "size_t"}


def _condition_at(src: str, start: int) -> str:
    """The parenthesised text beginning at *start* (which indexes the '(')."""
    depth, i, n = 0, start, len(src)
    while i < n:
        if src[i] == "(":
            depth += 1
        elif src[i] == ")":
            depth -= 1
            if depth == 0:
                return src[start + 1:i]
        i += 1
    return ""


def _taint_scan(ins: state_def.InstructionSpec) -> list[SecurityFinding]:
    """Does the model branch, or index memory, on a value derived from ``in``?

    A small forward taint propagation over the model's text: ``in`` is the
    secret, anything assigned from a tainted expression becomes tainted, and a
    tainted value reaching an ``if``/``while``/``for``/``switch`` condition or a
    subscript expression is a data-dependent branch or a data-dependent address.

    This is a text scan and it is *heuristic* on purpose — it is the cheap,
    always-available signal that runs even where valgrind does not, and it is
    never allowed to block on its own. ``timing.data_dependent`` and
    ``timing.secret_branch`` are the checks that decide.
    """
    src = _strip_c(ins.c_model or "")
    tainted = {"in"}
    for _ in range(4):                       # fixpoint in a few passes
        for m in _ASSIGN.finditer(src):
            name, expr = m.group(1), m.group(2)
            if name in _KEYWORDS or name in tainted:
                continue
            if any(re.search(rf"\b{re.escape(t)}\b", expr) for t in tainted):
                tainted.add(name)
    pattern = "|".join(re.escape(t) for t in sorted(tainted))
    hot = re.compile(rf"\b({pattern})\b")

    out: list[SecurityFinding] = []
    seen: set = set()
    for m in re.finditer(r"\b(if|while|switch|for)\s*\(", src):
        cond = _condition_at(src, m.end() - 1)
        if m.group(1) == "for":              # only the middle clause is the test
            parts = cond.split(";")
            cond = parts[1] if len(parts) >= 2 else ""
        hit = hot.search(cond)
        if not hit:
            continue
        text = " ".join(cond.split())[:120]
        if text in seen:
            continue
        seen.add(text)
        out.append(SecurityFinding(
            check="timing.taint_scan", severity="major", confidence="heuristic",
            instruction=ins.mnemonic,
            finding=f"`{m.group(1)} ({text})` tests a value derived from the "
                    "operand block, so the instruction's control flow depends "
                    "on its data",
            evidence=f"`{hit.group(1)}` is derived from in[] by assignment",
            fix="make the path branch-free: compute both results and select "
                "with a mask derived from the condition "
                "(`mask = -(uint32_t)(cond); r = (a & mask) | (b & ~mask)`)"))
    return out[:4]


# ---------------------------------------------------------------------------
# Static: the interrupt-latency bound
# ---------------------------------------------------------------------------

def _check_latency(spec: state_def.ISASpec,
                   core_profile=None) -> list[SecurityFinding]:
    """A coprocessor instruction is not interruptible. Its latency is therefore
    a floor on the machine's worst-case interrupt latency, which is a real
    availability property on any core that also has to service something."""
    from red import cost                     # local: only this check needs it
    plat = cost.Platform.from_core(core_profile)
    out: list[SecurityFinding] = []
    for ins in spec.instructions:
        cycles = cost.instruction_cycles(ins.words, ins.mac_ops, plat)
        if cycles > SECURITY_MAX_STALL_CYCLES:
            out.append(SecurityFinding(
                check="latency.interrupt_bound", severity="major",
                confidence="derived", instruction=ins.mnemonic,
                finding=(f"stalls the core for ~{cycles:,.0f} cycles per "
                         f"invocation, above the {SECURITY_MAX_STALL_CYCLES:,.0f}"
                         "-cycle bound — while it runs, the core cannot take an "
                         "interrupt"),
                evidence=(f"words={ins.words}, mac_ops={ins.mac_ops} -> "
                          f"{cycles:,.0f} cycles on this core's cost model"),
                fix="split it into resumable pieces, or accept and document the "
                    "interrupt-latency cost for this deployment"))
    return out


# ---------------------------------------------------------------------------
# Dynamic: the memory-safety and determinism battery
# ---------------------------------------------------------------------------

# Operand blocks are exactly-sized heap allocations, so AddressSanitizer's
# redzones make a single word of overrun a trap rather than a silent write into
# the next variable. The input block is shadowed and compared after every call
# (an instruction may not modify its source operands), and each vector is run
# against two DIFFERENTLY poisoned output blocks: a word the model never writes
# then shows up as a difference instead of hiding behind a zeroed buffer. That
# unwritten word is a residue leak — the caller gets back whatever the previous
# instruction left at that address.
_BATTERY = r"""
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

/* ---- designer-supplied C reference model ---- */
%(model)s
/* ---- end model ---- */

#define WORDS %(words)d
#define NVEC  %(vectors)d
#define POISON_A 0xA5A5A5A5u
#define POISON_B 0x5C5C5C5Cu

static uint32_t xs32(uint32_t *s) {
    uint32_t x = *s;
    x ^= x << 13; x ^= x >> 17; x ^= x << 5;
    return *s = x;
}

/* Adversarial patterns first: saturating values, one bit at every word
   boundary, and carry ripples that reach exactly as far as each word. Then
   pseudo-random fill for the rest. */
static void fill(uint32_t *in, long idx, uint32_t *seed) {
    static const uint32_t pat[] = { 0u, 0xFFFFFFFFu, 1u, 0x80000000u,
                                    0xAAAAAAAAu, 0x55555555u };
    const long npat = (long)(sizeof pat / sizeof pat[0]);
    int w;
    if (idx < npat) {
        for (w = 0; w < WORDS; w++) in[w] = pat[idx];
        return;
    }
    idx -= npat;
    if (idx < (long)WORDS * 3) {
        long w0 = idx / 3; int k = (int)(idx %% 3);
        for (w = 0; w < WORDS; w++) in[w] = 0u;
        in[w0] = (k == 0) ? 1u : (k == 1) ? 0x80000000u : 0xFFFFFFFFu;
        return;
    }
    idx -= (long)WORDS * 3;
    if (idx < (long)WORDS) {
        for (w = 0; w < WORDS; w++) in[w] = ((long)w <= idx) ? 0xFFFFFFFFu : 0u;
        return;
    }
    for (w = 0; w < WORDS; w++) in[w] = xs32(seed);
}

static void show(const char *label, const uint32_t *v) {
    int w;
    printf("%%s ", label);
    for (w = 0; w < WORDS; w++) printf("%%08x", v[w]);
    printf("\n");
}

/* One vector, twice, against two poisons. Returns 0 when clean. */
static int trial(const uint32_t *src, int report) {
    uint32_t *in     = (uint32_t *)malloc(WORDS * 4);
    uint32_t *shadow = (uint32_t *)malloc(WORDS * 4);
    uint32_t *a      = (uint32_t *)malloc(WORDS * 4);
    uint32_t *b      = (uint32_t *)malloc(WORDS * 4);
    int rc = 0, w, unwritten = -1, impure = -1;
    if (!in || !shadow || !a || !b) return 9;
    memcpy(in, src, WORDS * 4);
    memcpy(shadow, src, WORDS * 4);
    for (w = 0; w < WORDS; w++) a[w] = POISON_A;
    %(ident)s_model(in, a);
    if (memcmp(in, shadow, WORDS * 4) != 0) {
        printf("FAIL mutates-input\n"); rc = 2; goto done;
    }
    for (w = 0; w < WORDS; w++) b[w] = POISON_B;
    %(ident)s_model(in, b);
    if (memcmp(in, shadow, WORDS * 4) != 0) {
        printf("FAIL mutates-input\n"); rc = 2; goto done;
    }
    for (w = 0; w < WORDS; w++) {
        if (a[w] == b[w]) continue;
        if (a[w] == POISON_A && b[w] == POISON_B) {
            if (unwritten < 0) unwritten = w;
        } else if (impure < 0) {
            impure = w;
        }
    }
    if (impure >= 0) {
        printf("FAIL impure word=%%d %%08x vs %%08x\n", impure,
               a[impure], b[impure]);
        rc = 3; goto done;
    }
    if (unwritten >= 0) {
        printf("FAIL unwritten-output word=%%d of %%d\n", unwritten, WORDS);
        rc = 4; goto done;
    }
    if (report) show("out", a);
done:
    free(in); free(shadow); free(a); free(b);
    return rc;
}

int main(int argc, char **argv) {
    uint32_t in[WORDS];
    uint32_t seed = 0xC0FFEEu;
    long i;
    int w, rc;
    if (argc > 1) {                      /* probe: one caller-supplied vector */
        if (strlen(argv[1]) != (size_t)(8 * WORDS)) {
            printf("FAIL bad-vector expected %%d hex digits\n", 8 * WORDS);
            return 5;
        }
        for (w = 0; w < WORDS; w++) {
            char buf[9]; char *end;
            memcpy(buf, argv[1] + 8 * w, 8); buf[8] = 0;
            in[w] = (uint32_t)strtoul(buf, &end, 16);
            if (*end) { printf("FAIL bad-vector\n"); return 5; }
        }
        if (argc > 2) {                  /* timing mode: exactly one call */
            uint32_t out[WORDS];
            for (w = 0; w < WORDS; w++) out[w] = 0u;
            %(ident)s_model(in, out);
            return (int)(out[0] & 1u);
        }
        rc = trial(in, 1);
        if (rc) show("vector", in);
        return rc;
    }
    for (i = 0; i < NVEC; i++) {
        fill(in, i, &seed);
        rc = trial(in, 0);
        if (rc) { show("vector", in); return rc; }
    }
    printf("PASS %%d vectors\n", NVEC);
    return 0;
}
"""

_FAULTS = {
    2: ("memory.input_mutation", "blocking",
        "the model writes to its input block — an instruction may not modify "
        "the operands it was given",
        "leave in[] untouched; copy into a local if you need to mutate"),
    3: ("model.impure", "blocking",
        "the model returned different results for the same input, so it "
        "carries state between invocations",
        "remove every static/global; the instruction must be a pure function "
        "of in[]"),
    4: ("memory.output_residue", "blocking",
        "the model leaves part of its output block unwritten, so the caller "
        "reads back whatever the previous user of that memory left there",
        "write all `words` words of out[] on every path, padding with zeros"),
}


# Exit codes the harness uses for its own failures, as opposed to the model's.
# They must never be read as a defect in the design.
_HARNESS_ERRORS = {
    5: "the operand vector was malformed",
    9: "the harness could not allocate the operand blocks",
}


def _battery_binary(ins: state_def.InstructionSpec, work_dir: str,
                    vectors: int, sanitize: bool = True):
    """Compile the battery for one instruction. Returns (path, error)."""
    ident = state_def.c_identifier(ins.mnemonic)
    suffix = "san" if sanitize else "ct"
    src = os.path.join(work_dir, f"{ident}_sec_{suffix}.c")
    exe = os.path.join(work_dir, f"{ident}_sec_{suffix}")
    with open(src, "w") as f:
        f.write(_BATTERY % {"model": ins.c_model, "words": max(ins.words, 1),
                            "vectors": vectors, "ident": ident})
    cmd = ["cc", "-std=c99", "-O1", "-g", "-o", exe, src]
    if sanitize:
        cmd[4:4] = ["-fsanitize=address,undefined", "-fno-sanitize-recover=all"]
    else:
        # The timing measurement must count the model's own instructions, so it
        # must still be a callable function when callgrind toggles on it.
        cmd[4:4] = ["-fno-inline", "-fno-inline-functions"]
    p = subprocess.run(cmd, capture_output=True, text=True)
    if p.returncode != 0:
        return None, p.stderr.strip()[-800:]
    return exe, ""


def _battery(ins: state_def.InstructionSpec, work_dir: str,
             vectors: int) -> tuple[list[SecurityFinding], str]:
    """Run the memory-safety / determinism battery. Returns (findings, skip)."""
    exe, err = _battery_binary(ins, work_dir, vectors, sanitize=True)
    if exe is None:
        return [SecurityFinding(
            check="memory.bounds", severity="blocking", confidence="confirmed",
            instruction=ins.mnemonic,
            finding="the C model does not compile under the security build "
                    "(ASan + UBSan, no recovery)",
            evidence=err,
            fix="fix the compile error")], ""
    try:
        run = subprocess.run([exe], capture_output=True, text=True, timeout=600)
    except subprocess.TimeoutExpired:
        return [SecurityFinding(
            check="latency.termination", severity="blocking",
            confidence="confirmed", instruction=ins.mnemonic,
            finding="the model did not terminate within 600s on the adversarial "
                    "vectors — an instruction that can fail to terminate hangs "
                    "the core it is stalled in",
            evidence=f"{vectors} vectors, 600s timeout",
            fix="bound every loop by a constant derived from `words`")], ""
    if run.returncode == 0:
        return [], ""
    text = (run.stdout + "\n" + run.stderr).strip()
    if run.returncode in _HARNESS_ERRORS:
        # The battery itself could not run the vector (allocation failed, the
        # vector was malformed). That is not a finding about the design, and
        # reporting it as one would be a false accusation.
        return [], (f"memory.bounds for {ins.mnemonic}: the battery could not "
                    f"run — {_HARNESS_ERRORS[run.returncode]}")
    check, severity, finding, fix = _FAULTS.get(
        run.returncode,
        ("memory.bounds", "blocking",
         "the model trapped under AddressSanitizer/UndefinedBehaviorSanitizer "
         "on an adversarial operand value — it reads or writes outside its "
         "operand blocks, or executes undefined behaviour",
         "read only in[0..words-1] and write only out[0..words-1], and keep "
         "every shift, index and conversion in range"))
    return [SecurityFinding(
        check=check, severity=severity, confidence="confirmed",
        instruction=ins.mnemonic, finding=finding,
        evidence=text[-1200:], fix=fix)], ""


# ---------------------------------------------------------------------------
# Dynamic: does the instruction's latency depend on its data?
# ---------------------------------------------------------------------------

def _ct_vectors(words: int, n: int) -> list[str]:
    """Operand values that differ only in data, as hex digit strings."""
    out = []
    seed = 0x1234567
    for i in range(n):
        vals = []
        for w in range(words):
            if i == 0:
                v = 0
            elif i == 1:
                v = 0xFFFFFFFF
            elif i == 2:
                v = 1 if w == 0 else 0
            else:
                seed = (seed * 1103515245 + 12345 + w * 2654435761) & 0xFFFFFFFF
                v = seed
            vals.append(f"{v:08x}")
        out.append("".join(vals))
    return out


def _ir_variance(ins: state_def.InstructionSpec,
                 work_dir: str) -> tuple[list[SecurityFinding], str]:
    """Count the model's OWN instructions for several operand values.

    callgrind collects nothing until it enters ``<ident>_model``, so the number
    is the instruction's work and not the harness's. Two operand values that
    produce different counts are a timing side channel: the hardware's latency
    would carry the same dependence, and on the kernels RED mines the operands
    are the secret.
    """
    ident = state_def.c_identifier(ins.mnemonic)
    exe, err = _battery_binary(ins, work_dir, 1, sanitize=False)
    if exe is None:
        return [], f"timing.data_dependent: model does not compile ({err[:120]})"
    counts: dict[str, int] = {}
    for n, vec in enumerate(_ct_vectors(max(ins.words, 1), SECURITY_CT_VECTORS)):
        cg = os.path.join(work_dir, f"{ident}_ct{n}.callgrind")
        try:
            subprocess.run(
                ["valgrind", "--tool=callgrind", f"--callgrind-out-file={cg}",
                 "--collect-atstart=no", f"--toggle-collect={ident}_model",
                 "--quiet", exe, vec, "ct"],
                capture_output=True, text=True, timeout=300)
        except (OSError, subprocess.TimeoutExpired):
            return [], "timing.data_dependent: callgrind did not run"
        if not os.path.exists(cg):
            return [], "timing.data_dependent: callgrind produced no output"
        self_ir, _calls = callgrind.parse_raw(cg)
        counts[vec] = sum(self_ir.values())
    if not counts or max(counts.values()) == 0:
        return [], ("timing.data_dependent: collection never toggled on "
                    f"{ident}_model (inlined away?) — no timing evidence")
    lo_vec = min(counts, key=lambda v: counts[v])
    hi_vec = max(counts, key=lambda v: counts[v])
    lo, hi = counts[lo_vec], counts[hi_vec]
    if lo == hi:
        return [], ""
    return [SecurityFinding(
        check="timing.data_dependent", severity=SECURITY_CT_SEVERITY,
        confidence="confirmed", instruction=ins.mnemonic,
        finding=(f"the model executes {hi:,} instructions for one operand value "
                 f"and {lo:,} for another ({(hi - lo) / hi:.1%} spread), so the "
                 "instruction's latency leaks its operands through timing"),
        evidence=(f"callgrind, collection toggled inside {ident}_model:\n"
                  f"  {hi:>10,} Ir for in = {hi_vec[:64]}...\n"
                  f"  {lo:>10,} Ir for in = {lo_vec[:64]}..."),
        fix="make the model branch-free and fixed-trip: compute every branch's "
            "result and select with a mask "
            "(`m = -(uint32_t)cond; r = (x & m) | (y & ~m)`), and give every "
            "loop a trip count that depends only on `words`")], ""


# The ctgrind technique (Adam Langley, 2010): mark the operand block as
# *undefined* memory and let memcheck's existing uninitialised-value tracking
# report any branch or address computation that depends on it. It catches what
# the instruction-count test cannot — a data-dependent path both of whose arms
# happen to cost the same — and needs nothing but valgrind's own header.
_CT_HARNESS = r"""
#include <stdint.h>
#include <string.h>
#include <valgrind/memcheck.h>

/* ---- designer-supplied C reference model ---- */
%(model)s
/* ---- end model ---- */

#define WORDS %(words)d

/* The result is consumed through a volatile sink rather than through the exit
   status, so the process's exit code carries memcheck's verdict and nothing
   else: 0 is clean, 97 is "memcheck found errors", anything else means
   memcheck itself could not run. */
static volatile uint32_t sink;

int main(void) {
    uint32_t in[WORDS], out[WORDS];
    int w;
    for (w = 0; w < WORDS; w++) in[w] = 0x9e3779b9u * (uint32_t)(w + 1);
    memset(out, 0, sizeof out);
    VALGRIND_MAKE_MEM_UNDEFINED(in, sizeof in);
    %(ident)s_model(in, out);
    VALGRIND_MAKE_MEM_DEFINED(out, sizeof out);
    for (w = 0; w < WORDS; w++) sink = out[w];
    return 0;
}
"""

_VG_INCLUDE_CACHE: dict = {}


def _valgrind_include() -> str:
    """Where ``valgrind/memcheck.h`` lives on this host, as a ``-I`` flag.

    The header is what lets the constant-time check mark the operand block as
    secret, and it is not always on the default include path: a snap or a
    userland valgrind keeps its headers next to its own binary. Deriving the
    path from the binary we are actually going to run finds those, and an empty
    string (header already on the default path, or nowhere) is harmless — the
    compile then decides.
    """
    if "v" in _VG_INCLUDE_CACHE:
        return _VG_INCLUDE_CACHE["v"]
    found = ""
    candidates = ["/usr/include", "/usr/local/include",
                  os.path.join(os.path.expanduser("~"), ".local", "include"),
                  # A snap's headers are not on any include path, and its
                  # launcher resolves to /usr/bin/snap rather than to valgrind,
                  # so the prefix cannot be derived from the binary at all.
                  "/snap/valgrind/current/usr/include"]
    vg = shutil.which("valgrind")
    if vg:
        # .../<prefix>/bin/valgrind -> <prefix>/include, following the symlink
        # a snap or a package manager may have left.
        for path in {vg, os.path.realpath(vg)}:
            prefix = os.path.dirname(os.path.dirname(path))
            candidates.append(os.path.join(prefix, "include"))
            candidates.append(os.path.join(prefix, "usr", "include"))
    for d in candidates:
        if os.path.exists(os.path.join(d, "valgrind", "memcheck.h")):
            found = "" if d == "/usr/include" else f"-I{d}"
            break
    _VG_INCLUDE_CACHE["v"] = found
    return found


_CT_ERRORS = re.compile(
    r"(Conditional jump or move depends on uninitialised value|"
    r"Use of uninitialised value|"
    r"depends on uninitialised value)")


def _secret_branch(ins: state_def.InstructionSpec,
                   work_dir: str) -> tuple[list[SecurityFinding], str]:
    ident = state_def.c_identifier(ins.mnemonic)
    src = os.path.join(work_dir, f"{ident}_ctgrind.c")
    exe = os.path.join(work_dir, f"{ident}_ctgrind")
    with open(src, "w") as f:
        f.write(_CT_HARNESS % {"model": ins.c_model,
                               "words": max(ins.words, 1), "ident": ident})
    cmd = ["cc", "-std=c99", "-O1", "-g", "-fno-inline", "-o", exe, src]
    inc = _valgrind_include()
    if inc:
        cmd.insert(1, inc)
    p = subprocess.run(cmd, capture_output=True, text=True)
    if p.returncode != 0:
        why = ("valgrind/memcheck.h is not installed (it ships with valgrind's "
               "development headers)"
               if "memcheck.h" in p.stderr else p.stderr.strip()[-200:])
        return [], f"timing.secret_branch: {why}"
    try:
        run = subprocess.run(["valgrind", "--tool=memcheck", "-q",
                              "--error-exitcode=97", exe],
                             capture_output=True, text=True, timeout=300)
    except (OSError, subprocess.TimeoutExpired):
        return [], "timing.secret_branch: memcheck did not run"
    err = run.stderr or ""
    lines = [l for l in err.splitlines() if l.strip()]
    if _CT_ERRORS.search(err):
        return [SecurityFinding(
            check="timing.secret_branch", severity=SECURITY_CT_SEVERITY,
            confidence="confirmed", instruction=ins.mnemonic,
            finding="with the operand block marked as secret, the model branches "
                    "on it or uses it to compute an address — a timing / cache "
                    "side channel on the very values the instruction exists to "
                    "protect",
            evidence="\n".join(lines[:12])[:1200],
            fix="replace the data-dependent branch or table index with masked "
                "arithmetic that touches the same addresses for every input")], ""
    if run.returncode == 97:
        return [SecurityFinding(
            check="memory.bounds", severity="major", confidence="confirmed",
            instruction=ins.mnemonic,
            finding="memcheck reported an error in the model that is not a "
                    "constant-time violation — an invalid access or a leak",
            evidence="\n".join(lines[:12])[:1200],
            fix="read the memcheck output and fix the access it names")], ""
    if run.returncode != 0:
        # memcheck could not start (a stripped ld.so without debuginfo is the
        # usual cause, and is exactly the situation where a silent "no errors"
        # would be a lie). Say so instead of passing the check.
        why = next((l for l in lines if "Fatal error" in l or "Cannot continue"
                    in l), lines[-1] if lines else f"exit {run.returncode}")
        return [], f"timing.secret_branch: memcheck could not run — {why.strip()}"
    return [], ""


# ---------------------------------------------------------------------------
# The gate
# ---------------------------------------------------------------------------

def analyze(spec: state_def.ISASpec, core_profile=None, work_dir: str = "",
            vectors: int = SECURITY_VECTORS) -> SecurityReport:
    """Run every mechanical security check that this host can run.

    Findings are ordered worst-first and the report says plainly which checks
    could not run, because a skipped check is not a passed one.
    """
    work_dir = work_dir or os.path.join(".", "security")
    os.makedirs(work_dir, exist_ok=True)
    findings: list[SecurityFinding] = []
    run: list[str] = ["encoding.opcode", "encoding.fields",
                      "encoding.collision", "model.forbidden_call",
                      "model.includes", "latency.interrupt_bound",
                      "timing.taint_scan"]
    skipped: list[str] = []

    findings += _check_encoding(spec)
    findings += _check_latency(spec, core_profile)
    for ins in spec.instructions:
        findings += _check_model_text(ins)
        findings += _taint_scan(ins)

    have_cc = shutil.which("cc") is not None
    have_vg = shutil.which("valgrind") is not None
    if not have_cc:
        skipped.append("memory.bounds, memory.input_mutation, "
                       "memory.output_residue, model.impure: no C compiler on "
                       "this host")
    else:
        run += ["memory.bounds", "memory.input_mutation",
                "memory.output_residue", "model.impure"]
        for ins in spec.instructions:
            got, skip = _battery(ins, work_dir, vectors)
            findings += got
            if skip:
                skipped.append(skip)
    if not (have_cc and have_vg):
        skipped.append("timing.data_dependent, timing.secret_branch: "
                       "valgrind is not available on this host")
    else:
        run += ["timing.data_dependent", "timing.secret_branch"]
        for ins in spec.instructions:
            for probe_fn in (_ir_variance, _secret_branch):
                got, skip = probe_fn(ins, work_dir)
                findings += got
                if skip:
                    skipped.append(skip)

    order = {"blocking": 0, "major": 1, "minor": 2}
    conf = {"confirmed": 0, "derived": 1, "heuristic": 2, "agent": 3}
    findings.sort(key=lambda f: (order.get(f.severity, 3),
                                 conf.get(f.confidence, 4)))
    return SecurityReport(checks_run=sorted(set(run)), checks_skipped=skipped,
                          findings=findings, vectors=vectors)


def verdict(report: SecurityReport) -> state_def.GateResult:
    """Gate 4's verdict. **Only mechanically established findings can fail it.**

    That asymmetry is deliberate. A security checker that a language model can
    talk into failing a sound design is not a security checker, and one that
    passes a design because the model said it looked fine is worse. So the gate
    is decided by sanitizers, instruction counts and decoded bits; the agent's
    judgement travels with the report as advice to the designer, and earns a
    blocking finding only by proposing a vector that actually breaks something.
    """
    hard = report.mechanical_blocking()
    advisory = [f for f in report.findings
                if f.severity in ("blocking", "major") and not f.is_mechanical()]
    n_checks = len(report.checks_run)
    if not report.checks_run:
        return state_def.GateResult(
            gate="gate4", passed=False,
            detail="no security check could run on this host")
    # The static checks run anywhere, because they are arithmetic over the
    # spec's own text — and on their own they establish nothing about what the
    # models DO. Passing a spec whose memory safety was never executed would be
    # the exact failure this module exists to prevent, so the battery is
    # required rather than merely reported as missing.
    if "memory.bounds" not in report.checks_run:
        return state_def.GateResult(
            gate="gate4", passed=False,
            detail=("the memory-safety battery could not run on this host (no C "
                    "compiler), so nothing about what these models do is "
                    "established — only their text was checked"),
            counterexample="\n".join(report.checks_skipped) or None)
    detail = (f"{n_checks} checks over {report.vectors:,} adversarial vectors "
              f"per model: {len(hard)} mechanically confirmed blocking, "
              f"{len(advisory)} advisory")
    if report.checks_skipped:
        detail += f"; {len(report.checks_skipped)} check(s) could not run"
    return state_def.GateResult(
        gate="gate4", passed=not hard, detail=detail,
        counterexample="\n\n".join(
            f"[{f.check}] {f.instruction or 'spec-wide'}: {f.finding}\n"
            f"{f.evidence}" for f in hard) or None)


# ---------------------------------------------------------------------------
# The agent's side: probes and findings
# ---------------------------------------------------------------------------

def probe(ins: state_def.InstructionSpec, vector_hex: str,
          work_dir: str) -> tuple[str, SecurityFinding | None]:
    """Execute one caller-supplied operand value against an instruction's model.

    This is how the security agent's hypotheses become evidence. It names a
    vector it believes is dangerous; the model runs on exactly that vector under
    ASan + UBSan, with the input shadowed and the output block poisoned, and
    what comes back is either the output words or a fault that is now a
    ``confirmed`` finding — the same status a finding from the batteries has,
    because it was established the same way.
    """
    words = max(ins.words, 1)
    digits = re.sub(r"[^0-9a-fA-F]", "", vector_hex or "").lower()
    if len(digits) != words * 8:
        return (f"Rejected: this instruction's operand block is {words} words, "
                f"so the vector must be {words * 8} hex digits (word 0 first, "
                f"8 digits each); got {len(digits)}."), None
    os.makedirs(work_dir, exist_ok=True)
    exe, err = _battery_binary(ins, work_dir, 1, sanitize=True)
    if exe is None:
        return f"The model does not compile under ASan+UBSan:\n{err}", None
    try:
        run = subprocess.run([exe, digits], capture_output=True, text=True,
                             timeout=300)
    except subprocess.TimeoutExpired:
        return ("The model did not terminate within 300s on that vector.",
                SecurityFinding(
                    check="latency.termination", severity="blocking",
                    confidence="confirmed", instruction=ins.mnemonic,
                    finding="the model does not terminate on the operand value "
                            f"{digits[:64]}...",
                    evidence="300s timeout", fix="bound every loop by `words`"))
    text = (run.stdout + "\n" + run.stderr).strip()
    if run.returncode == 0:
        out = next((l for l in run.stdout.splitlines() if l.startswith("out ")),
                   "")
        return (f"Clean. in  = {digits}\n{out or '(no output captured)'}\n"
                "No sanitizer fault, the input block was unmodified, every "
                "output word was written, and two calls agreed."), None
    if run.returncode in _HARNESS_ERRORS:
        return (f"The probe could not run: {_HARNESS_ERRORS[run.returncode]}.\n"
                f"{text[-400:]}"), None
    check, severity, finding, fix = _FAULTS.get(
        run.returncode,
        ("memory.bounds", "blocking",
         "the model trapped under AddressSanitizer/UndefinedBehaviorSanitizer "
         "on this operand value", "keep every access inside in[]/out[] and "
         "every shift and index in range"))
    got = SecurityFinding(
        check=f"probe.{check}", severity=severity, confidence="confirmed",
        instruction=ins.mnemonic,
        finding=f"{finding} (found by a probe on in = {digits[:64]}...)",
        evidence=text[-1200:], fix=fix)
    return (f"FAULT on in = {digits}\n\n{text[-1500:]}\n\n"
            "This is now a confirmed blocking finding on the spec."), got


def parse_agent_findings(text: str) -> list[SecurityFinding]:
    """Pull the security agent's JSON array of findings out of its reply.

    Everything it says arrives as ``confidence='agent'`` and is capped at
    ``major``: the agent does not get to fail the gate on its own authority. If
    it wants a blocking finding it has to earn one with a probe, and that
    arrives through :func:`probe` as ``confirmed`` instead.
    """
    import json
    if not text:
        return []
    blob = text.strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", blob, re.S)
    if fence:
        blob = fence.group(1).strip()
    start = blob.find("[")
    if start < 0:
        return []
    for end in range(len(blob), start, -1):
        if blob[end - 1] != "]":
            continue
        try:
            items = json.loads(blob[start:end])
        except json.JSONDecodeError:
            continue
        if not isinstance(items, list):
            return []
        out = []
        for it in items[:SECURITY_MAX_FINDINGS]:
            if not isinstance(it, dict):
                continue
            sev = str(it.get("severity", "minor")).lower()
            out.append(SecurityFinding(
                check="agent." + re.sub(r"[^a-z_]", "_",
                                        str(it.get("class", "review")).lower())[:40],
                severity="major" if sev in ("blocking", "major") else "minor",
                confidence="agent",
                instruction=str(it.get("instruction", "") or ""),
                finding=str(it.get("finding", "") or "")[:600],
                evidence=str(it.get("evidence", "") or "")[:800],
                fix=str(it.get("fix", "") or "")[:600]))
        return out
    return []


def merge(report: SecurityReport, extra: list[SecurityFinding]) -> SecurityReport:
    """Fold agent findings and probe results into a mechanical report."""
    seen = {(f.check, f.instruction, f.finding) for f in report.findings}
    for f in extra or []:
        if (f.check, f.instruction, f.finding) not in seen:
            report.findings.append(f)
            seen.add((f.check, f.instruction, f.finding))
    order = {"blocking": 0, "major": 1, "minor": 2}
    conf = {"confirmed": 0, "derived": 1, "heuristic": 2, "agent": 3}
    report.findings.sort(key=lambda f: (order.get(f.severity, 3),
                                        conf.get(f.confidence, 4)))
    return report


_CONFIDENCE_NOTE = {
    "confirmed": "a tool observed this",
    "derived": "decoded from the spec's own fields",
    "heuristic": "pattern scan — verify before acting",
    "agent": "the security reviewer's reading — advisory",
}


def render(report: SecurityReport) -> str:
    """The security report as the designer and the reviewers read it."""
    hard = report.mechanical_blocking()
    lines = ["# Security review (Gate 4)", "",
             f"Checks run: {', '.join(report.checks_run) or 'none'}",
             f"Vectors per model: {report.vectors:,}"]
    if report.checks_skipped:
        lines += ["", "**Checks that could NOT run** (not the same as passing):"]
        lines += [f"- {s}" for s in report.checks_skipped]
    lines += ["",
              f"**{len(hard)} mechanically confirmed blocking finding(s)** — "
              "these fail the gate." if hard else
              "**No mechanically confirmed blocking findings** — the gate "
              "passes.", ""]
    if not report.findings:
        lines.append("Nothing was found by any check.")
        return "\n".join(lines)
    for f in report.findings:
        who = f"`{f.instruction}`" if f.instruction else "spec-wide"
        lines += [f"## [{f.severity.upper()}] {who} — {f.check}",
                  f"*{_CONFIDENCE_NOTE.get(f.confidence, f.confidence)}*", "",
                  f"**Finding.** {f.finding}"]
        if f.evidence:
            lines += ["", "```", f.evidence.strip()[:1200], "```"]
        if f.fix:
            lines.append(f"**Required change.** {f.fix}")
        lines.append("")
    return "\n".join(lines)
