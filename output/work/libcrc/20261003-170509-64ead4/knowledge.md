
libcrc round 1: hot op is crc_32() (table-driven, ~5-6 instr/byte: (crc>>8)^crc_tab32[(crc^b)&0xFF]). Harness: 64KB LCG-filled buffer, 600 iters of crc_32 over full buffer + 1500B frame => ~40MB processed, est ~200M+ instrs, nearly all in crc_32. If short, raise ITERS.
