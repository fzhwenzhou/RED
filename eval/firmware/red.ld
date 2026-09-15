/* 256 KiB of SRAM at 0, matching red_soc_tb's memory. */
ENTRY(_start)
MEMORY { SRAM (rwx) : ORIGIN = 0x00000000, LENGTH = 0x00040000 }
SECTIONS {
	.text : {
		KEEP(*(.init))
		*(.text*)
		*(.rodata*) *(.srodata*)
	} > SRAM
	.data : {
		*(.data*) *(.sdata*)
		. = ALIGN(4);              /* makehex.py needs whole 32-bit words */
	} > SRAM
	.bss (NOLOAD) : {
		*(.bss*) *(.sbss*) *(COMMON)
	} > SRAM
	_end = .;
}
