Is the spec **legal and complete** as a RISC-V extension?

Check: every encoding sits in the custom-0/1/2/3 opcode space and nowhere else;
no two instructions share an (opcode, funct3, funct7) triple; the encoding string
is consistent with the flat `opcode`/`funct3`/`funct7` fields; nothing collides
with the base ISA or a ratified extension; `words` matches what the semantics and
pseudo-code actually read and write; the prose semantics, the pseudo-code and the
declared operand packing agree with one another; and every instruction is total —
defined for all inputs, including all-zeros and all-ones, with no case the spec
leaves undefined.
