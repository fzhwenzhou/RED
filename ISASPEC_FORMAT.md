# The ISASpec format

**Version 1 · normative reference**

An `ISASpec` is RED's deliverable: a RISC-V instruction set extension described
completely enough that a program can check it. It is plain YAML (version 1.1), so a human
reviewer and a downstream RTL or compiler tool consume the same file.

The schema lives in [`red/state_def.py`](red/state_def.py)
(`ISASpec`, `InstructionSpec`), its validator is `validate_isa_spec`, and the
tool boundary that accepts drafts is `DesignerTool.write_spec` in
[`red/tools.py`](red/tools.py). Where this document and the code disagree, the
code is right and this document is a bug.

---

## 1. What the format is for

Most ISA extension descriptions are prose. Prose is readable and not
checkable: you cannot compile a paragraph, and you cannot tell from one
whether the instruction is faster than the code it replaces.

An ISASpec adds four things to the prose, and each exists so that a specific
claim becomes falsifiable:

| Claim | Made checkable by |
|---|---|
| "this instruction computes *X*" | `c_model`, a compilable C11 function stress-run on 10⁵ vectors |
| "the spec is unambiguous" | `spike_model`, written independently from the prose alone, required to agree with `c_model` on 10⁵ vectors in a real simulator |
| "this instruction is worth building" | `words`, `form`, `mac_ops`, `replaces`, `invocations`, `marshal_words`, which a cost model turns into a predicted speedup |
| "this is an ISA extension, not an accelerator behind an opcode" | `form`, `group`, `register_files`, `csrs`, measured against the core's own state by the structural check in §9 |

Everything else in the format exists to support those four.

The fourth is the newest and the least obvious. A spec can satisfy the first
three completely — a correct, unambiguous, provably fast instruction — and
still not be an extension: one opcode that reads a block of memory, runs an
entire kernel and writes the result back is an accelerator invocation. The
format makes the difference measurable rather than a matter of taste.

---

## 2. The file

* **Encoding**: UTF-8 YAML 1.1.
* **Name on disk**: `ISASpec.yaml` for a spec that has passed the verification
  chain, `ISASpec.draft.yaml` for the designer's output before it.
* **Location**: `output/db/<workload>/<run>/`, alongside the `HotLoopReport`,
  the `CoreProfile` and the gate verdicts. Real examples ship in this
  repository after any run.
* **Unknown keys are rejected.** The reader builds dataclasses directly, so
  a key the schema does not define raises
  `TypeError: ISASpec.__init__() got an unexpected keyword argument 'x'`, at
  the top level and inside an instruction alike. The format is closed: there
  is no vendor-extension space, and a producer that adds one makes a file RED
  cannot read.

---

## 3. Top-level object

| Field | Type | Req. | Written by | Meaning |
|---|---|:--:|---|---|
| `name` | string | **yes** | designer | Extension name, e.g. `CRC_Accel`. Must be non-empty. |
| `instructions` | array | **yes** | designer | One or more `InstructionSpec`. Must be non-empty. |
| `version` | string | no | designer | Spec version. Free-form; defaults to `"1.0.0"`. |
| `description` | string | no | designer | Why this extension looks the way it does. In practice the designer records its reasoning here, which is the most useful field in the file for a human reviewer. |
| `base_isa` | string | no | designer | What the extension extends, e.g. `rv32im`. |
| `rationale` | string | no | designer | Why the work is split across *these* instructions rather than any other way. This is the field a reader needs to judge the decomposition, and §9 is the standard it is judged against. |
| `register_files` | array | no | designer | Architectural register files the extension adds. See §6. Empty is a good answer. |
| `csrs` | array | no | designer | Control/status registers the extension adds. See §6. |
| `state_note` | string | no | designer | How the added state behaves across traps and context switches, and how it interacts with the base ISA. |
| `encodings` | string[] | no | designer | Machine-encoded encoding lines. Informational; the authoritative encoding is per instruction. |
| `tests` | string[] | no | **the loop** | One line per verification edge that ran, e.g. `"gate2: 3/3 instructions ... agree on 100,000 ... vectors"`. |
| `link1_vectors` | int | no | **the loop** | Vectors each `c_model` was stress-run on. |
| `gate2_vectors` | int | no | **the loop** | Vectors the two models were compared on. |
| `passes_link1` | bool | no | **the loop** | Set from link 1's verdict. |
| `passes_gate2` | bool | no | **the loop** | Set from Gate 2's verdict. |
| `critic_rounds` | int | no | the loop | Self-critique rounds the designer spent. |

### Fields a producer must not assert

`passes_link1`, `passes_gate2`, `link1_vectors`, `gate2_vectors` and `tests`
are **results**, not claims. RED's loop overwrites them from what the gates
measured. A spec arriving with `passes_gate2: true` has not thereby passed
Gate 2, and a consumer reading a spec from an untrusted source should treat
these as unverified until it has run the comparison itself.

---

## 4. The instruction object

### 4.1 Identity

| Field | Type | Req. | Meaning |
|---|---|:--:|---|
| `name` | string | **yes** | Human-readable name, e.g. `"CRC-32 (reflected, poly 0xEDB88320) step over 64 bytes"`. Used in diagnostics. |
| `mnemonic` | string | **yes** | Assembler mnemonic, e.g. `crc32.step64b`. Also determines the C function names (§7). |

### 4.2 Encoding

| Field | Type | Req. | Meaning |
|---|---|:--:|---|
| `opcode` | string | **yes** | One of `custom-0`, `custom-1`, `custom-2`, `custom-3`. |
| `funct3` | string | **yes¹** | Exactly 3 binary digits, e.g. `"000"`. |
| `funct7` | string | **yes¹** | Exactly 7 binary digits, e.g. `"0000000"`. |
| `encoding` | string | **yes** | The full 32-bit R-type layout as text, low bits last: `"0000000 rs2 rs1 000 rd 0101011"`. |

¹ `validate_isa_spec` requires them to be strings and requires the
`(opcode, funct3, funct7)` triple to be unique, but does not itself require
the right bit widths. Gate 4 does: a `funct3` that is not exactly 3 binary
digits is a `major` finding, because every unfixed bit doubles the number of
encodings the instruction answers to.

**The opcode field is the one that can alias a real instruction.** The four
custom opcodes are the only 7-bit values RISC-V reserves for non-standard
extensions:

| `opcode` | low 7 bits |
|---|---|
| `custom-0` | `0001011` |
| `custom-1` | `0101011` |
| `custom-2` | `1011011` |
| `custom-3` | `1111011` |

Gate 4 decodes the last whitespace-separated token of `encoding` the way a
decoder would. If it is 7 binary digits and does not match the declared
`opcode`, that is a **blocking** finding naming what the instruction would
actually decode as. If `encoding` never states the opcode bits at all, that is
a `major` finding, because what the instruction decodes as is then unstated.

Two instructions sharing an `(opcode, funct3, funct7)` triple is rejected by
the validator and, independently, by Gate 4: which one a decoder picks is
undefined.

### 4.3 Semantics

| Field | Type | Req. | Meaning |
|---|---|:--:|---|
| `semantics` | string | **yes** | Prose. Must state the operand packing, the result, and totality. This is what the independent implementer reads, so an omission here surfaces as a Gate 2 disagreement. |
| `pseudocode` | string | **yes** | Single-assignment pseudo-code. The executable intent, free of a particular language's conventions. |

Both must be non-empty. The bar for `semantics` is not "describes the
instruction" but **"a competent reader who has never seen the C model could
reimplement it"** — that is literally what Gate 2 tests.

### 4.4 Executable models

| Field | Type | Req. | Written by | Meaning |
|---|---|:--:|---|---|
| `c_model` | string | **yes** | the designer | C99 reference implementation. |
| `spike_model` | string | no | **the ISS agent only** | C++ implementation for the simulator. |

See §6 for their required signatures and §8 for the independence rule.

### 4.5 Shape, operands and width

| Field | Type | Req. | Meaning |
|---|---|:--:|---|
| `form` | string | no | `reg`, `reg_imm` or `block`. How the operands reach the instruction. Defaults to `block`. See §5. |
| `group` | string | no | `configuration`, `load-store`, `move`, `compute`, `reduction` or `other`. What role the instruction plays in the family. Defaults to `compute`. |
| `operands` | string[] | **yes** | The register operands, e.g. `["rd", "rs1", "rs2"]`. Which ones are required depends on `form`. |
| `words` | int | **yes** | Length of the model array in 32-bit words, `1 ≤ words ≤ 256`. Forced to 2 for `reg` and `reg_imm`; for `block` it is the operand block width. |
| `src_regs` | int | no | `reg` only: 1 or 2 source registers. With 1, `in[1]` is zero. Defaults to 2. |
| `imm_bits` | int | no | `reg_imm` only: width of the immediate, `1 ≤ imm_bits ≤ 5`. |
| `reads_state` | string[] | no | Names of register files and CSRs the instruction reads. Every name must be declared in §6. |
| `writes_state` | string[] | no | Names of register files and CSRs the instruction writes. |
| `state_words` | int | no | `block` only: how many leading words of `in[]`/`out[]` carry live state rather than fresh operands. See §6a. |

`form` is the first decision about an instruction, not a late one: it fixes the
operand traffic, and operand traffic is usually what decides whether the
instruction is worth building at all.

For a `block` instruction, `words` is the single most consequential number in
the file: it sets the traffic paid on every invocation, the array lengths in
both models, and the size of both memory blocks. For a register form there is
no block and no traffic.

### 4.6 Cost fields

These exist so the claim "this instruction is worth building" can be
contradicted. See §10.

| Field | Type | Req. | Meaning |
|---|---|:--:|---|
| `replaces` | string | **yes** | The `loop_id` (or bare function name) from the `HotLoopReport` that this instruction replaces. Must name a loop that was actually mined. |
| `invocations` | int | **yes** | How many times this instruction runs per call of that loop. Must be ≥ 1. |
| `mac_ops` | int | no | 32×32 multiplies performed per invocation. Must be ≥ 0. |
| `marshal_words` | int | no | Words the caller must copy per invocation to lay the operands out. Must be ≥ 0. |

---

## 5. The instruction ABI

Every form is harnessed through the **same C model signature** —
`void f(const uint32_t in[words], uint32_t out[words])` — so link 1, the
vector generator and the Gate-2 differential test are shared by all of them.
What differs is only how that array binds to machine state.

### 5.1 `form: reg` — a register-to-register primitive

```
<mnemonic> rd, rs1, rs2        (src_regs = 2; omit rs2 when src_regs = 1)

    in[0]   = x[rs1]
    in[1]   = x[rs2], or 0 when src_regs = 1
    out[0] -> x[rd]
```

`words` is 2. No memory is touched and no operand crosses the bus, so the
instruction pays only the coprocessor issue overhead. This is the shape of
almost every instruction in the RISC-V scalar crypto extensions —
`sha256sig0 rd, rs1`, `xperm8 rd, rs1, rs2` — and the first shape to consider
for anything whose operands fit in two registers.

`out[1]` is **not architecturally visible**: the instruction's only result is
`x[rd]`. The host-side harness zeroes it before comparing, because the
hardware cannot produce it, so a model is free to leave anything there.

### 5.2 `form: reg_imm` — a primitive with a small immediate

```
<mnemonic> rd, rs1, imm

    in[0]   = x[rs1]
    in[1]   = zero-extended imm      (imm_bits wide)
    out[0] -> x[rd]
```

`words` is 2. The immediate is part of the *encoding*, not a runtime operand,
which has two consequences:

* **The decode mask covers only `funct3` and the opcode.** Bits 31:20 are the
  immediate, so there is no `funct7` to decode on, and a `reg_imm` instruction
  therefore claims an entire `funct3` slot — all 128 `funct7` values of it.
  §11 rejects anything else that lands in that slot.
* **`imm_bits` is capped at 5.** Gate 2's firmware has to name the immediate
  at assembly time, so it emits one `.insn i` per value and the differential
  test covers the field exhaustively. 5 bits is 32 cases, which covers what
  immediates in this space are for — a byte selector, a round index, a rotate
  amount. An operand too wide to enumerate belongs in a register.

### 5.3 `form: block` — an operand block in memory

```
<mnemonic> rd, rs1, rs2

    rs1   address of the input block   (words × uint32, little-endian)
    rs2   address of the output block  (words × uint32)
    rd    written 0, so rd is a real destination and the R-type is well formed
```

Consequences a producer must respect:

* Both blocks are `words` long. The input and output blocks are the **same
  size**, even when the result is smaller; unused output words are written
  (conventionally 0) rather than left alone.
* The unit masters the bus for `2 × words` transfers on every invocation. That
  is the dominant cost of this form and the reason §9 bounds `words`.
* The caller pays to lay the block out. That cost is `marshal_words`, and it
  is charged twice (in and out) by the cost model.
* The spec should state what happens when the two blocks **overlap or alias**,
  because real callers do that. The usual safe wording is that the instruction
  reads all `words` input words before writing any output word.

A draft missing a required operand is rejected at the tool boundary with a
message spelling out the ABI for its form, because it has no way to say where
its result goes.

---

## 6. Architectural state

An extension may add state. Most should not: a family of pure
register-to-register primitives is the cleanest thing an extension can be, and
`register_files: []` with `csrs: []` is a good answer, not an empty one. What
the format insists on is that state which *is* held gets declared, because
undeclared state is still built, still context-switched and still paid for —
it is only unreviewed.

### `register_files[]`

| Field | Type | Req. | Meaning |
|---|---|:--:|---|
| `name` | string | **yes** | Architectural name used in assembly, e.g. `mt`. Unique within the spec. |
| `count` | int | **yes** | How many registers. Must be ≥ 1. |
| `width_bits` | int | **yes** | Bits per register. Must be ≥ 1. |
| `purpose` | string | no | What it holds and why the extension needs it. |
| `reset` | string | no | Value at reset, or `"undefined"`. |
| `context` | string | no | How it is saved and restored across a context switch. |

### `csrs[]`

| Field | Type | Req. | Meaning |
|---|---|:--:|---|
| `name` | string | **yes** | e.g. `xmcfg`. |
| `address` | string | **yes** | Parsed with `int(x, 0)`, so `"0x800"`. Must lie in RISC-V's custom CSR space (below) and must not collide with another CSR in the spec. |
| `purpose` | string | no | |
| `width_bits` | int | no | Defaults to 32. |
| `access` | string | no | `RW`, `RO`, `WARL` or `WLRL`. Defaults to `RW`. |
| `fields` | string | no | Bitfield layout, as a table or prose. |
| `reset` | string | no | |

The custom CSR ranges, which are the only addresses a non-standard extension
may use:

| Range | Mode / access |
|---|---|
| `0x5C0`–`0x5FF` | supervisor, read/write |
| `0x800`–`0x8FF` | unprivileged, read/write |
| `0x9C0`–`0x9FF` | supervisor, read/write |
| `0xBC0`–`0xBFF` | machine, read/write |
| `0xCC0`–`0xCFF` | unprivileged, read-only |
| `0xDC0`–`0xDFF` | supervisor, read-only |
| `0xFC0`–`0xFFF` | machine, read-only |

Anything outside them aliases an architectural CSR, and §11 rejects it.

Configuration belongs in a CSR rather than in every instruction's operands: a
CSR is written once and read by a whole loop, where an operand field is paid
for on every invocation. This is what the XuanTie matrix extension does with
its geometry, and it is why that extension needs `configuration` instructions
as well as `compute` ones.

Instructions name the state they touch in `reads_state` and `writes_state`.
Every name must be declared here, and a register file that no instruction
names is a finding (§9) — state nothing can reach is area the design cannot
use. An extension with a register file generally needs `load-store` and
`configuration` instructions to go with its `compute` ones.

### 6a. Live state in a `block` operand — `state_words`

RED's verification rests on every model being a **pure function of its input
array**: link 1 stress-runs `c_model` on generated vectors, and Gate 2
compares it against an independently written `spike_model` on 10⁵ more. A
model that remembers something between calls cannot be tested that way.

So a `block` instruction that carries state threads it through the operand
block instead of hiding it in the unit:

```
in[0 .. state_words-1]    the incoming state
in[state_words .. ]       this invocation's fresh operands
out[0 .. state_words-1]   the outgoing state
```

A running CRC is the natural example: `state_words: 1`, `in[0]` is the CRC
coming in and `out[0]` is the CRC going out, and the 16 words between them are
the message line. The state is in memory where the caller can see it, the
model stays pure, and no register file is needed. Threading state this way is
the recommended pattern, not a compromise.

`state_words` must satisfy `0 ≤ state_words ≤ words`. Register forms must be
pure — they have nowhere to thread state through — so §11 rejects a `reg` or
`reg_imm` instruction that sets `state_words` or `writes_state`.

### 6b. A register file the instructions keep between invocations

`state_words` puts the state in the caller's memory. A real register file
stays in the unit, and that is also expressible — it is what the XuanTie
matrix extension does, and it is the right shape for a tiled kernel: a named
file, `load-store` instructions moving tiles in and out, `compute`
instructions operating on it, and configuration in a CSR.

The model holds the file as **file-scope state**:

```c
/* the architectural register file: 4 tiles x 16 words */
static uint32_t mt[4][16];

void mload_model(const uint32_t in[16], uint32_t out[16])
{
    for (int i = 0; i < 16; i++) mt[0][i] = in[i];
    for (int i = 0; i < 16; i++) out[i] = 0;
}
```

Nothing about the signature changes, so link 1, Gate 3, Gate 4 and Gate 2 all
work unmodified. The state survives between calls because it is `static`,
which is precisely what distinguishes a register file from an operand block.
The ISS implementer writes the same state in its own C++ model, and Gate 2
drives both from reset with the same vector sequence, so a divergent state
transition surfaces as a divergent output.

Two conditions make that last sentence true, and a spec that misses them gets
a green Gate 2 that means nothing:

* **Every instruction must be observable.** Gate 2 compares outputs only. An
  instruction that writes the file and returns a constant produces the same
  output for every input, and the differential test passes without testing
  anything. Return something that depends on the state.
* **The file must be reachable.** A register file no instruction loads or
  stores is area the design cannot use, and §9 reports it.

The declaration in `register_files` is the architecture; the `static` is one
implementation of it. Both are required — the first is what gets reviewed and
priced, the second is what gets executed.

---

## 7. The four descriptions, and which one is normative

An instruction carries four texts, and they are not four drafts of the same
thing. Writing any one of them as a restatement of another throws away the
only thing it was there to provide.

| Field | Role | Audience |
|---|---|---|
| `semantics` | Prose. What the instruction is for, what the caller must arrange, and what is architecturally visible: traps, alignment, ordering, aliasing, state touched. | Reviewers and humans |
| `pseudocode` | **The normative operation.** Single-assignment, no control flow beyond bounded loops, in the style of the operation blocks in the RISC-V specifications. | Everyone; it is the definition |
| `c_model` | A C99 implementation of the pseudo-code, written by the designer. A verification artifact, not part of the published spec. | link 1, Gate 3, Gate 4 |
| `spike_model` | A C++ implementation of the same pseudo-code, written by a different agent that never sees `c_model`. | Gate 2 |

Where the prose and the pseudo-code disagree, **the pseudo-code wins** and the
prose is the defect. `semantics` should therefore not re-derive the arithmetic
line by line; it should say the things pseudo-code cannot.

The last two look like duplicates of each other, and that is the point: they
are a differential pair (§8). Two agents implement the same pseudo-code
without seeing each other's work, and Gate 2 passes only when they agree on
10⁵ vectors. That agreement is evidence that `pseudocode` is unambiguous — a
single model, however careful, is evidence of nothing. Collapse them into one
and Gate 2 becomes a test of a function against itself.

### `c_model`

C99. Must define, exactly:

```c
void <ident>_model(const uint32_t in[<words>], uint32_t out[<words>])
```

where `<ident>` is `mnemonic` with every character that cannot appear in a C
identifier replaced by `_`, prefixed with `_` if it would otherwise start with
a digit. `secp256r1.modadd` → `secp256r1_modadd` → `secp256r1_modadd_model`.
`red.state_def.c_identifier` is the authority.

The validator checks that the string `<ident>_model` appears in `c_model` and
rejects the draft otherwise. Include whatever `#include` lines you need; the
model is compiled standalone.

The model must be a **pure, total, memory-safe function of its input block**:

* total — defined for every `2^(32 × words)` input, no undefined behaviour,
  no trapping;
* pure — no state carried between invocations;
* self-contained — no allocation, I/O, clock, or non-local jumps;
* writes every one of the `words` output words, because an unwritten word
  leaks whatever the caller left there.

These are not style preferences. Each is a check in link 1 or Gate 4 that
fails the design.

### `spike_model`

C++. Must define:

```c++
void <ident>_iss(const uint32_t in[<words>], uint32_t out[<words>])
```

It is compiled into a Spike extension so the real simulator decodes the
instruction from the rv32 instruction stream and executes it with operands in
architectural registers, going through the MMU.

---

## 8. The independence rule

`spike_model` exists to be an *independent* reading of the specification. That
only means something if it was written without sight of `c_model`, so the
format carries a write rule rather than a convention:

* The designer's `write_spec` **drops any `spike_model` it is sent** and
  restores whatever the ISS agent last wrote.
* The ISS agent's tool serves the spec with every `c_model` **stripped out**
  and accepts only `spike_model` back.

A consumer merging two specs, or a producer round-tripping one, must preserve
this. Copying `c_model` into `spike_model` produces a file that passes Gate 2
and means nothing.

**What agreement does and does not prove.** It proves the prose and encoding
were unambiguous enough that two readers built the same machine. It does not
prove that machine is correct, and when the algorithm is canonical the two
implementations tend to converge anyway. What Gate 2 adds unconditionally is
that the instruction really decodes and executes: a wrong `funct3`, an
encoding collision or a broken operand convention fails here and cannot fail
in link 1.

---

## 9. Design shape — what `red/structure.py` enforces

An ISA extension is a small set of composable operations plus whatever state
they need, with the structure of the computation left in software. It is not
one opcode that performs a kernel end to end. Two published specs show the two
legitimate shapes:

* **The RISC-V scalar crypto extensions** are almost entirely two-register
  primitives — `sha256sig0 rd, rs1`, `xperm8 rd, rs1, rs2`,
  `aes32esmi rd, rs1, rs2, bs`. No instruction performs a whole hash or a
  whole block cipher. Each performs the one step the base ISA is bad at, and
  the round loop stays in software.
* **The XuanTie matrix extension** does take on a whole tile multiply, and
  pays for it by declaring the cost: named tile and accumulator register
  files, geometry in CSRs, and separate configuration, load/store and compute
  instructions. The state is architectural, so it can be reviewed and
  context-switched.

The shape that is neither is the one this section exists to catch: a single
`block` instruction that reads a large operand block, performs the kernel and
writes the result, while declaring no architectural state at all. It still
holds every one of those words somewhere — it has simply not said so, which
puts the dominant cost of the design outside review.

`red/structure.py` measures this from the spec's own fields and emits ordinary
`ReviewFinding`s, merged into the review report under the reviewer name
`structure`. They are computed, not judged, so a designer cannot argue one
down; it can only change the design.

| Rule | id | Severity | Threshold |
|---|---|---|---|
| A `block` instruction computes — arithmetic addressing memory | `load-store` | **blocking** | `form: block` and `group` not in `load-store`, `move` |
| A `block` instruction's operand block exceeds the core's whole register file, twice over | `block-too-wide` | **blocking** | `words > 64` (2,048 bits) |
| A `block` instruction wider than the register file declares no register file | `undeclared-buffer` | major | `words > 32` (1,024 bits) and `state_words == 0` and `register_files == []` |
| The extension is one instruction and it is a wide `block` | `monolith` | major | 1 instruction, `form: block`, `words ≥ 32` |
| A declared register file that no instruction reads or writes | `unreachable-state` | major | referenced by 0 instructions |
| A declared CSR that no instruction reads or writes | `unreachable-csr` | minor | referenced by 0 instructions |
| Declared state dwarfs the core's own | `state-too-large` | major | `state_bits > 4 × 1,024` |
| A multi-instruction extension with no `rationale` | `no-rationale` | minor | — |

Each finding's `evidence` begins with `[<id>]`, and `structure.rule_of()`
reads it back, so a consumer can tell which rule fired without matching on
prose. The distinction matters: "this operand block is larger than the
machine" and "this arithmetic addresses memory" are different objections with
different fixes, and the specs under `eval/spec/` — which were built as RTL
and measured — trip the second without tripping the first.

### Load/store discipline

RISC-V addresses memory from loads and stores; arithmetic reads and writes
registers. `block` form exists so an instruction can carry an operand too wide
for a register, and for a while nothing stopped it being used for arithmetic
as well, so designs arrived as memory-to-memory ALU operations. The shipped
micro-ecc extension is three of them: a 16-word multiply, a reduction and a
subtract, each reading two memory blocks and writing one.

The constraint underneath is real — a 256-bit field element does not fit in
two 32-bit registers — and the architectural answer to it is an extension
register file with loads and stores, exactly as the vector and matrix
extensions do:

```
vld.v    vt0, (rs1)      form: block, group: load-store   memory -> vt0
vmul.vv  vt2, vt0, vt1   form: reg,   group: compute      registers only
vst.v    vt2, (rs1)      form: block, group: load-store   vt2 -> memory
```

§6b shows how to make that register file executable by both models.

The two thresholds are anchored to the one state figure every RV32 core has:
its 32 × 32 = 1,024-bit architectural register file. An operand block up to
that size is something the caller could plausibly have held in registers; past
twice it, the unit holds more state than the processor it attaches to, and
"instruction" has stopped being the right word.

They are calibrated against what RED has actually shipped. micro-ecc's
16-word multiply and libcrc's 17-word CRC step (16 words of message plus one
word of running CRC) sit under the soft bound and are not flagged.
matrixmul's 200-word tile multiply — one instruction performing a complete
5×10×10 binary32 matrix multiply-accumulate behind one opcode, with 6,400 bits
of operand state and no declared register file — is three times the hard
bound, and is the design this check was written from.

---

## 10. How the cost fields are consumed

The performance gate turns the cost fields into a predicted whole-application
speedup. The per-invocation cost is:

```
cycles = fixed + beat × 2 × words + mac × mac_ops + logic_steps
caller  = marshal_words × cycles_per_marshal_word × 2      (in and out)
```

with `fixed`, `beat`, `mac` and `cycles_per_marshal_word` measured on the
target core's RTL rather than assumed (on the bundled PicoRV32: 28, 2, 1 and
12.9). Those constants live in [`red/constants.py`](red/constants.py); the
model is [`red/cost.py`](red/cost.py).

Three of these are **not taken on the producer's word**:

* **`mac_ops` is measured.** The gate compiles `c_model` and runs it under
  callgrind, then prices the instruction on what it actually executes. A spec
  declaring `mac_ops = 0` for a 512-iteration bit-serial reduction was
  repriced from 92 to 7,590 cycles, turning a claimed win into a slowdown.
* **Work the declaration cannot account for is charged anyway**, as
  `logic_steps`, at the rate hardware runs sequential logic. Pricing
  everything as multiplies was a real defect: bit-serial models measured
  297–301 cycles on RTL where a multiply-rate model predicted ~112.
* **`replaces` must name a mined loop**, exactly. Otherwise an instruction can
  be credited for work it never did.

`invocations` is the one field RED cannot derive. The obvious estimator is
*biased* — the model and the kernel are different implementations of the same
work, so the ratio of their instruction counts reads 31 for a true 128 and 147
for a true 4. So the gate does not substitute a number it cannot stand behind.
Instead:

* the tool boundary **rejects** a declaration more than 10× from what the
  profile implies, because that is a mistake rather than bias;
* the gate **reports** a surviving inconsistency as `invocations UNVERIFIED`
  rather than silently pricing it.

A producer should derive `invocations` from the data volume in the profiled
workload and say how in `description`. The shipped libcrc spec does this
explicitly, which is the standard to copy.

Finally, the demanded speedup is capped at the Amdahl ceiling
`1 / (1 − coverage)`: a design covering 47% of an application cannot exceed
1.90× however good the silicon, so it is held to a reachable bar.

---

## 11. Validation rules

The complete normative list, in the order `validate_isa_spec` applies it. Each
is an outright rejection.

**Spec level**

1. `name` is non-empty.
2. `instructions` is non-empty.

**Architectural state** (`validate_architectural_state`)

2a. Every register file has a unique non-empty `name`, and `count` and
    `width_bits` are positive `int`s.
2b. Every CSR has a non-empty `name` and an `address` that parses with
    `int(x, 0)`.
2c. Every CSR address lies in a custom CSR range (§6) and no two CSRs share
    an address.
2d. Every CSR `access` is one of `RW`, `RO`, `WARL`, `WLRL`.
2e. Every name in any instruction's `reads_state` / `writes_state` is
    declared in `register_files` or `csrs`.

**Per instruction**

3. `mnemonic` and `encoding` are both non-empty.
4. `encoding`, `opcode`, `funct3`, `funct7`, `mnemonic` are all **strings**.
   There is no scalar coercion on read: `"words": "17"` stays a string and
   fails. A nested `encoding` object is the one exception (§12).
5. `opcode` normalises to one of the four custom opcodes.
6. `(opcode, funct3, funct7)` is unique across the spec.
7. `semantics` and `pseudocode` are both non-empty.
8. `c_model` is non-empty.
9. `words` is an `int` in `[1, 256]`.
10. `mac_ops`, `invocations`, `marshal_words` are non-negative `int`s.
11. `invocations ≥ 1`.
12. `replaces` is non-empty.
13. `form` normalises to `reg`, `reg_imm` or `block`.
14. `operands` contains what the form requires: `rd`, `rs1`, `rs2` for
    `block`; `rd`, `rs1` and (when `src_regs == 2`) `rs2` for `reg`; `rd` and
    `rs1` for `reg_imm`.
15. A register form has `words == 2` and, for `reg`, `src_regs` in `{1, 2}`.
16. A register form sets neither `state_words` nor `writes_state` — it has
    nowhere to put state (§6a).
17. `reg_imm` has `imm_bits` in `[1, 5]`.
18. `state_words` is a non-negative `int` and does not exceed `words`.
19. `c_model` contains the string `<ident>_model`.

**Additionally enforced at the designer's tool boundary** (`write_spec`), not
by `validate_isa_spec`:

20. `replaces` names a loop present in the run's `HotLoopReport`.
21. `invocations` is within 10× of what the profile implies.
22. Any incoming `spike_model` is discarded (§8).

Rule 6 has one subtlety. A `reg_imm` instruction is an I-type: bits 31:20 are
its immediate, so it has no `funct7` to decode on and claims **every**
`funct7` of its `funct3`. Its slot key is therefore `(opcode, funct3, "*")`,
and any other instruction sharing that `(opcode, funct3)` collides with it.

Structural findings (§9) are *not* on this list. They do not reject a draft;
they enter the review report, and a blocking one is resolved the same way any
blocking review finding is — by revising the design.

A rejection is returned to the producer as `Rejected: invalid ISASpec — <why>`
rather than thrown, so a draft can be corrected and resubmitted.

---

## 12. Normalisation applied on acceptance

The tool boundary repairs shape differences rather than rejecting them,
because they are not design errors:

**Nested encoding.** A producer may write

```yaml
encoding:
  opcode: custom-0
  funct3: '110'
  funct7: '0000011'
```

and the flat fields are lifted out of it, with `encoding` rebuilt as a real
R-type layout string. A list of field assignments would not tell a reviewer or
the independent implementer where the operands sit, so the layout is
synthesised: `funct7 rs2 rs1 funct3 rd <opcode bits>`.

**Opcode spelling.** `custom0`, `CUSTOM_1`, `custom 2` and `custom-3` all
normalise to the canonical `custom-N`. The canonical form is what is written
back to disk.

**Form and group spelling.** `reg-imm`, `REG_IMM`, `immediate` and `i-type`
all normalise to `reg_imm`; `register` and `r-type` to `reg`; `memory`,
`mem` and `tile` to `block`. An unrecognised `group` normalises to `compute`
rather than failing, since it is descriptive.

**Register-form width.** A register form's `words` is decided by the form, not
by the producer, so it is set to 2 on acceptance instead of being rejected;
`reg_imm` likewise has `src_regs` set to 1. A draft that got the form right is
not bounced over a field it had no freedom in.

---

## 13. A complete example

A real two-instruction extension, valid against the validator, clean against
the structural check (§9), and byte-identical to what `to_yaml` emits — a test
regenerates it and compares, so it cannot drift from the code.

It shows the shape §9 asks for: the kernel is the bit loop, so that is what
gets an instruction; the buffer walk stays in C. `crc32.w` is the primitive,
a pure two-register step. `crc32.line` exists only because the 28-cycle issue
overhead would otherwise be paid per word on a long buffer, and it threads the
running CRC through `in[0]`/`out[0]` (`state_words: 1`, §6a) rather than
holding it in the unit — which is why the extension needs no architectural
state at all.

`spike_model` is absent from every instruction, and that is correct for a
designer-written draft: the designer is not permitted to supply one (§8). The
ISS agent fills it in later, and the loop fills in `passes_link1`,
`passes_gate2`, `link1_vectors`, `gate2_vectors` and `tests`.

```yaml
base_isa: rv32im
critic_rounds: 0
csrs: []
description: 'Reflected CRC-32 (polynomial 0xEDB88320) for libcrc. Two instructions: a register-to-register step the caller uses on a tail or a short buffer, and a block form that folds a 64-byte line in one invocation.'
encodings: []
gate2_vectors: 0
instructions:
- c_model: |-
    /* One table-free reflected CRC-32 step (polynomial 0xEDB88320).
       in[0] = running CRC, in[1] = the next message word, out[0] = new CRC. */
    void crc32_w_model(const uint32_t in[2], uint32_t out[2])
    {
        uint32_t crc = in[0] ^ in[1];
        int b;
        for (b = 0; b < 32; b++)
            crc = (crc >> 1) ^ (0xEDB88320u & (uint32_t)(-(int32_t)(crc & 1u)));
        out[0] = crc;
    }
  encoding: 0000000 rs2 rs1 000 rd 0001011
  form: reg
  funct3: '000'
  funct7: '0000000'
  group: compute
  imm_bits: 0
  invocations: 1
  mac_ops: 0
  marshal_words: 0
  mnemonic: crc32.w
  name: crc32.step-word
  opcode: custom-0
  operands:
  - rd
  - rs1
  - rs2
  pseudocode: |-
    crc <- X(rs1) XOR X(rs2)
    for b in 0..31:
        crc <- (crc >> 1) XOR (0xEDB88320 AND -(crc AND 1))
    X(rd) <- crc
  reads_state: []
  replaces: crc32::inner
  semantics: 'Folds one 32-bit message word into a running CRC-32. x[rs1] is the running CRC, x[rs2] is the message word, and x[rd] receives the updated CRC. Touches no memory and traps on nothing: every operand value is legal.'
  spike_model: ''
  src_regs: 2
  state_words: 0
  words: 2
  writes_state: []
- c_model: |-
    /* The same step folded over a 64-byte line, so one invocation covers a whole
       cache line's worth of message. in[0] is the incoming CRC and out[0] the
       outgoing one -- that is the state_words = 1 contract. */
    void crc32_line_model(const uint32_t in[17], uint32_t out[17])
    {
        uint32_t crc = in[0];
        int w, b;
        for (w = 1; w < 17; w++) {
            crc ^= in[w];
            for (b = 0; b < 32; b++)
                crc = (crc >> 1) ^ (0xEDB88320u & (uint32_t)(-(int32_t)(crc & 1u)));
        }
        out[0] = crc;
        for (w = 1; w < 17; w++)
            out[w] = 0u;
    }
  encoding: 0000001 rs2 rs1 000 rd 0001011
  form: block
  funct3: '000'
  funct7: '0000001'
  group: compute
  imm_bits: 0
  invocations: 64
  mac_ops: 0
  marshal_words: 0
  mnemonic: crc32.line
  name: crc32.step-line
  opcode: custom-0
  operands:
  - rd
  - rs1
  - rs2
  pseudocode: |-
    crc <- in[0]
    for w in 1..16:
        crc <- crc XOR in[w]
        for b in 0..31:
            crc <- (crc >> 1) XOR (0xEDB88320 AND -(crc AND 1))
    out[0] <- crc
    for w in 1..16: out[w] <- 0
    X(rd) <- 0
  reads_state: []
  replaces: crc32::outer
  semantics: Folds 64 bytes into a running CRC-32. x[rs1] is the address of a 17-word input block whose first word is the incoming CRC and whose remaining 16 words are the message line; x[rs2] addresses a 17-word output block whose first word receives the updated CRC and whose remaining words are written zero. x[rd] receives 0. All 17 input words are read before any output word is written, so the blocks may alias.
  spike_model: ''
  src_regs: 2
  state_words: 1
  words: 17
  writes_state: []
link1_vectors: 0
name: RED-CRC32
passes_gate2: false
passes_link1: false
rationale: 'The bit loop is the kernel, not the buffer walk, so the buffer walk stays in C. crc32.w is the primitive; crc32.line exists only because a 28-cycle issue overhead per word would otherwise swamp it on long buffers, and 16 words is the line the caller already has contiguous. Neither instruction holds state between invocations: the running CRC is an operand.'
register_files: []
state_note: The extension adds no architectural state. Both instructions are pure functions of their operands, so nothing has to be saved or restored across a context switch.
tests: []
version: 1.0.0
```

---

## 14. What the format deliberately does not express

Knowing the boundaries matters as much as knowing the fields.

* **Variable-length operands.** `words` is a constant per instruction. An
  instruction over a caller-chosen length is expressed as a fixed-size
  instruction the caller loops over, which is what every shipped spec does.
* **Multi-result instructions.** A register form writes exactly one register,
  `x[rd]`; a `block` instruction writes `rd` as 0 and returns everything
  through its output block. An instruction that has to write two registers
  cannot be expressed.
* **Flag and condition-code side effects.** There is no field for them, and
  the differential harness could not observe them if there were.
* **A register file addressed by its own index field.** §6 lets a spec
  declare a register file and §6b shows how to implement one the harness can
  execute, but the operand forms in §5 encode `rd`/`rs1`/`rs2` as base-ISA
  registers. An instruction that selects `mt3` from a dedicated index field in
  its encoding is describable in `semantics` and `encoding`, and its model can
  hold the file, but the harness always drives it with one fixed index — so
  the *selection* is unverified even though the state transition is not.
* **A family composing.** Gate 2 tests each instruction alone, in its own
  machine, from reset. It never runs a load, then a compute, then a store in
  sequence, so a disagreement about how two instructions interact *through* a
  shared register file is outside what it can see.
* **Microarchitectural intent.** The format says what the instruction computes
  and what it costs, not how to build it. Pipeline depth, datapath width and
  resource sharing are the RTL implementer's decisions, and a spec that pins
  them is over-specified. The `semantics` field often carries a non-binding
  "hardware note" instead.
* **Exceptions and privilege.** Instructions are required to be total, so
  there is no field for the traps they raise, because they raise none.
* **Memory ordering.** Beyond the aliasing statement in `semantics`, the
  format says nothing about concurrency. RED targets in-order single-issue
  cores.

---

## 15. Consuming a spec

A tool that reads an ISASpec should, in order:

1. **Validate it.** Do not trust a file because it parses.
2. **Ignore `passes_*`** unless you produced them or trust whoever did (§3).
3. **Decode the encoding yourself** from `opcode`, `funct3`, `funct7`. Treat
   `encoding` as documentation for humans; it is text, and the three fields
   are the data.
4. **Pick the model you need.** For correctness, `c_model` is the reference.
   For a differential test, you need both, and you must not have generated one
   from the other.
5. **Re-derive the cost** rather than trusting it. The cost constants are
   core-specific, and a spec designed against one `CoreProfile` priced on
   another is meaningless.

---

## 16. Stability

This describes version 1 of the format, as implemented today. The schema is a
set of Python dataclasses, and compatibility rests on three habits rather than
a version negotiation mechanism:

* new fields are added with defaults, so a **file written by an older
  producer still loads** in a newer reader;
* the reverse does **not** hold. Because unknown keys are rejected (§2), a
  file carrying a field added after your reader was built fails to load
  outright rather than degrading. Readers that must tolerate newer files
  should drop unknown keys before deserialising;
* `ISASpec.version` is the **producer's** version string for its own
  extension, not the format's. There is no format version field. To
  distinguish format revisions, key off the presence of fields.

Fields added since the first version, all with defaults: on `ISASpec`,
`base_isa`, `rationale`, `register_files`, `csrs` and `state_note`; on
`InstructionSpec`, `form`, `src_regs`, `imm_bits`, `group`, `reads_state`,
`writes_state` and `state_words`. A spec written before they existed loads
unchanged and behaves exactly as it did: `form` defaults to `block`, which is
the ABI that used to be the only one.

Removing or retyping a field is a breaking change and has not happened. The
checks in [`tests/test_env.py`](tests/test_env.py) exercise the round-trip,
every validation rule above, and the rejection of malformed drafts; run them
after any schema change.
