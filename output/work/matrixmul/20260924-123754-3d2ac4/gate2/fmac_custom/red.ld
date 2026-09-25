
OUTPUT_ARCH("riscv")
ENTRY(_start)
SECTIONS
{
  . = 0x80000000;
  .text.init : { *(.text.init) }
  .text      : { *(.text) *(.text.*) }
  .rodata    : { *(.rodata) *(.rodata.*) *(.srodata) *(.srodata.*) }
  .data      : { *(.data) *(.data.*) *(.sdata) *(.sdata.*) }
  .bss       : { *(.bss) *(.bss.*) *(.sbss) *(.sbss.*) *(COMMON) }
  . = ALIGN(8);
  .htif      : { *(.htif) }
  . = ALIGN(16);
  begin_signature = .;
  .signature : { *(.signature) }
  end_signature = .;
  . = ALIGN(16);
  . = . + 0x10000;
  _stack_top = .;
}
