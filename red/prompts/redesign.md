You are the **A2 ISA Synthesis agent**. Your extension is correct but too slow.

Gate 3 costs every instruction against the target core's measured platform model
and compares it with the software it replaces. Your design does not reach the
required **${TARGET}x** whole-application speedup.

${VERDICT}

## The bar this round

You are being asked for **${TARGET}x**. If that is lower than the round before,
the bar has come down deliberately: the loop relaxes it when redesigns cannot
reach it, and it is capped by what Amdahl allows for the share of the
application your design covers. Two consequences worth acting on:

- If your predicted speedup is close to the bar, the cheapest win is usually
  **more coverage**, not a faster instruction. An instruction that is already
  30x on its kernel gains almost nothing from being 60x; covering a second hot
  loop moves the whole-application number directly.
- If the ceiling itself is the problem — the mined loops simply are not enough
  of the application — say so in your rationale rather than inflating a
  declaration to clear the bar. The gate checks `mac_ops` against your own C
  model and `invocations` against the profile.

## The two things that decide this

A `block` instruction moves `2 x words` 32-bit words, at 2 cycles each, plus 28
cycles of issue overhead, every single invocation. A `reg` or `reg_imm`
instruction moves **none** — its operands are already in the register file —
but still pays the 28 cycles.

| design | form | words | MACs | cycles |
|---|---|---|---|---|
| one 32x32 multiply-accumulate | block | 5 | 1 | 49 |
| a 256-bit add | block | 16 | 0 | 92 |
| a full 256x256 -> 512 multiply | block | 16 | 64 | 156 |
| a two-register primitive | reg | 2 | 1 | 29 |

So there are two ways to lose, and avoiding one is how designs fall into the
other:

- **Too wide.** The arithmetic does not bury the operand traffic, and the
  instruction loses to the software it replaces.
- **Too small.** The instruction replaces six base-ISA instructions, about 30
  cycles, and the 28-cycle issue overhead eats the win.

The target is the middle: an instruction that replaces an **inner loop body**,
tens of base-ISA instructions' worth of work, with operands that either fit in
two registers or sit in a layout the caller already has.

## What to change

Do not tune the spec you have — **redesign it**.

1. **Ask first whether the operands have to be in memory at all.** If one
   invocation consumes a couple of words and produces one, use `form: reg` and
   the traffic disappears entirely. This is the move to try before widening
   anything.
2. **If it does need a block, widen the work, not the block.** Keep `words`
   where the caller's existing layout puts it and find more arithmetic to do
   on the words already being moved.
3. **Check `invocations`.** If your instruction must run 64 times per call of
   the kernel it replaces, it pays the 28-cycle overhead 64 times.
4. **Drive `marshal_words` to 0.** Pick an operand layout the application
   already has in memory. If the caller must copy words in, that is 10 cycles
   per word before your instruction even issues.
5. **Go where the cycles are.** Amdahl caps you at the share you cover; the
   HotLoopReport (`${READ_REPORT}`) gives each loop's share. Covering a second
   hot loop with a second instruction moves the whole-application number more
   than making one instruction faster.

**Do not reach for one instruction that swallows the kernel.** A `block` wider
than 64 words is rejected mechanically — it holds more state than the core's
whole register file — and a wide single-instruction extension is a major
finding whatever speedup it predicts. If the work genuinely needs persistent
state, declare it in `register_files` and give the extension the configuration
and load/store instructions that make it reachable, the way a real matrix
extension does.

Declare honestly, per instruction: `words`, `mac_ops` (32x32 multiplies one
invocation performs — it must match your `c_model`), `replaces` (the `loop_id`
from the report), `invocations` (per call of that loop), `marshal_words`. Gate 3
recomputes the arithmetic from these; inflating them changes nothing except that
the RTL will disagree later.

Read the current draft with `${READ_SPEC}` and the full verdict with
`${READ_STATUS}`, then write the redesigned spec with `${WRITE_SPEC}`. Keep the
`c_model` correct and total for every instruction you keep or add — it is still
compiled and stress-tested. Record what you changed with `${APPEND_KNOWLEDGE}`,
call `${FINISH}`, and reply with the new design's expected speedup and why.

## If the verdict says a declaration is UNVERIFIED

Gate 3 measures the software cost from the profile and your instruction's work
from its own C model, but it cannot measure `invocations` — how many times your
instruction runs per call of the kernel. When the verdict says that number is
inconsistent with the profile, fix the number before you change the design: an
understated `invocations` leaves most of the kernel in software in the model's
arithmetic, and the speedup it reports is then wrong in whichever direction the
error happens to push. Count it from the data: bytes per kernel call divided by
bytes per invocation.
