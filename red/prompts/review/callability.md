Can the **application** actually call each instruction, on its own data, without
paying more than the instruction saves?

The hot loops' source is summarised above. For each instruction ask: are the
operands the application holds already adjacent in memory in the exact layout
`rs1` requires, or must the caller marshal them into a block first? Does the
instruction **return every value the caller needs** — in particular, does a
routine the application already has return a carry, borrow, or flag that this
instruction discards? Would using it force the caller to keep a value in memory
that it currently keeps in registers across a loop?

Marshalling 8 words in and 8 out costs ~455 cycles; an instruction that saves
less than that is a net loss however fast its datapath is.
