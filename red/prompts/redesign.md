You are the **A2 ISA Synthesis agent**. Your extension is correct but too slow.

Gate 3 costs every instruction against the target core's measured platform model
and compares it with the software it replaces. Your design does not reach the
required **${TARGET}x** whole-application speedup.

${VERDICT}

## The one thing that decides this

An instruction's operand traffic is fixed by its `words`: it moves `2 x words`
32-bit words, at 2 cycles each, plus 28 cycles of issue overhead, every single
invocation. So the question is not "is my datapath fast" — it is **how much
arithmetic does one invocation do for the words it moves**.

| design | words | MACs | cycles | MAC per word moved |
|---|---|---|---|---|
| one 32x32 multiply-accumulate | 5 | 1 | 49 | 0.10 |
| a 256-bit add | 16 | 0 | 92 | 0.00 |
| a full 256x256 -> 512 multiply | 16 | 64 | 156 | **2.00** |

The last one costs three times the first and does sixty-four times the work.
That is the whole game. The software it replaces costs ~9,000 cycles, so it is a
~58x win on that kernel; the first is a *loss* once the caller has marshalled
its operands.

## What to change

Do not tune the spec you have — **redesign it**.

1. **Move up a level.** Find the largest unit of work in the mined loops that
   has a small, well-defined operand block: a whole multi-word multiply, a whole
   modular reduction, a whole squaring. Replace the *loop*, not the loop body.
2. **Check `invocations`.** If your instruction must run 64 times per call of
   the kernel it replaces, it pays the 28-cycle overhead 64 times. One
   invocation per call is the target.
3. **Drive `marshal_words` to 0.** Pick an operand layout the application
   already has in memory. If the caller must copy words in, that is 10 cycles
   per word before your instruction even issues.
4. **Go where the cycles are.** Amdahl caps you at the share you cover; the
   HotLoopReport (`${READ_REPORT}`) gives each loop's share.
5. **Fewer, bigger instructions.** Two instructions that each replace a whole
   hot kernel beat six that each replace an inner statement.

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
