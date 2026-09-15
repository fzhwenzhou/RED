Is each instruction **actually faster** than the software it replaces, and does
the extension move the whole application?

Per instruction: compute its cost with the formula above, multiply by
`invocations` to get the cost per call of the kernel it replaces, add
`marshal_words x 10`, and compare against that kernel's measured cost (its share
of cycles and its call count are in the report). Give the ratio.

Check `invocations` especially: an instruction that runs 64 times per call pays
the 28-cycle issue overhead 64 times, and that alone usually sinks it. If an
instruction replaces an inner statement rather than a whole kernel, say so and
say what the fused version would look like. Then bound the whole-application
effect with Amdahl's law using the cycle shares in the hot-loop report — an
instruction covering 5% of cycles cannot deliver more than 1.05x however good it
is. Flag any instruction whose ratio is at or below 1.0, and say plainly if the
extension as a whole cannot reach a useful speedup.
