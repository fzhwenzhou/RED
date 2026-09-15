Gate 4 — the security gate — has **rejected** your ISASpec.

These are not opinions. Every blocking finding below was established by running
your own C model: a sanitizer trapped it, two calls on the same input disagreed,
callgrind counted different instruction counts for two operand values, or the
encoding bits were decoded and do not say what you declared. The findings marked
*advisory* come from the security reviewer's reading and are worth your
attention, but the gate is held by the mechanical ones.

${FINDINGS}

## What is required

Fix every **blocking** finding. For each one, the fix is named in the finding;
apply it as stated unless you can say why it is wrong.

Three of these have standard, cheap fixes that you should reach for by reflex:

- **Data-dependent latency or a branch on operand data.** Make the path
  branch-free. Compute both arms and select with a mask:

      uint32_t m = (uint32_t)0 - (uint32_t)(cond);   /* cond is 0 or 1 */
      r = (a & m) | (b & ~m);

  and give every loop a trip count that depends only on `words`, never on the
  data. A conditional final subtraction in a modular reduction becomes: always
  compute `t = x - p`, then select `t` or `x` on the borrow with the mask above.

- **An unwritten output word.** Write all `words` words of `out[]` on every
  path, zero-padding the ones your result does not use. The caller's buffer is
  `words` words and it reads all of them.

- **An out-of-bounds access.** Read only `in[0 .. words-1]`, write only
  `out[0 .. words-1]`. If you need more room, raise `words` — and remember that
  raising it raises the operand traffic the performance gate charges you for, so
  check the arithmetic still pays.

## What must not change

You already have a design that passed the performance gate. **Change as little
as possible.** A branch-free select costs a handful of cycles and keeps the
`mac_ops` and `words` you were credited for; redesigning the instruction's shape
to dodge a security finding will send you back through Gate 3 and usually
arrives somewhere worse. Keep the same instruction, the same operand widths and
the same kernel — change only the code path the finding names.

Read your current draft with `${READ_SPEC}`, the hot loops with `${READ_REPORT}`,
the full security report with `${READ_STATUS}`, and the core with `${READ_CORE}`.
Write the revised spec with `${WRITE_SPEC}`, note anything durable with
`${APPEND_KNOWLEDGE}`, and call `${FINISH}` when every blocking finding is
addressed. Your revised C model will be re-run through exactly the same checks,
so a fix you only describe in prose will fail again.
