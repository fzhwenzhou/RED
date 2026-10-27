Is each instruction **actually faster** than the software it replaces, and does
the extension move the whole application?

Per instruction: compute its cost with the formula above, multiply by
`invocations` to get the cost per call of the kernel it replaces, add
`marshal_words x 10`, and compare against that kernel's measured cost (its share
of cycles and its call count are in the report). Give the ratio.

Check `invocations` especially: an instruction that runs 64 times per call pays
the 28-cycle issue overhead 64 times, and that alone usually sinks it. If an
instruction replaces a single inner statement — so that the issue overhead is
paid per statement — say so and say what the loop-body-sized version would
look like. Do not ask for an instruction that swallows the whole kernel: a
`block` wider than 64 words is rejected mechanically whatever it predicts. Then bound the whole-application
effect with Amdahl's law using the cycle shares in the hot-loop report — an
instruction covering 5% of cycles cannot deliver more than 1.05x however good it
is. Flag any instruction whose ratio is at or below 1.0, and say plainly if the
extension as a whole cannot reach a useful speedup.
