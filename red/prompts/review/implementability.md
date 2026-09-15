Can each instruction actually be **built** as a coprocessor for this core, and
what would it cost?

Work through, per instruction: how many bus beats its operand block implies
(`2 x words`, in and out), how much state the datapath must hold, whether it
duplicates hardware the CPU already has (the core has a 32x32 multiplier), and
what the resulting latency is from the calibration figures above. Flag anything
whose latency is dominated by moving its operands rather than by its arithmetic,
and anything whose area would be a large fraction of a small core (a 16-word
operand buffer alone is 512 bits of registers).


The operand ABI is fixed and a bus-mastering coprocessor for it has already been
built and measured on this core (+74% area). Report the area and latency that
*this* design implies — buffer width in flops, whether it duplicates a unit the
core already has, how many cycles its operand traffic costs — rather than
objecting to the ABI. And check each instruction's declared `mac_ops` against
what its `c_model` actually computes: a reduction implemented as a bit-serial
loop is not a zero-latency operation, whatever the field says.
