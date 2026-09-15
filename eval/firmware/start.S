/* Minimal reset vector for red_soc_tb: set the stack, run main, stop the sim. */
	.section .init
	.global _start
_start:
	li   sp, 0x00040000          /* top of the 256 KiB SRAM */
	call main
	li   t0, 0x10000004          /* MMIO: end simulation, a0 = status */
	sw   a0, 0(t0)
1:	j    1b
