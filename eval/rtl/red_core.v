/*
 *  red_core — PicoRV32, optionally with the RED accelerator attached.
 *
 *  ENABLE_ACCEL selects between the two cores the evaluation compares:
 *      0 -> stock PicoRV32                       (the baseline binary's target)
 *      1 -> PicoRV32 + red_accel over PCPI       (runs the extended binary)
 *
 *  Everything else about the CPU is identical between the two, so a cycle or
 *  area difference is attributable to the extension and nothing else.
 *
 *  picorv32 ORs the external PCPI result with its internal mul/div coprocessors
 *  (see the pcpi_int_* mux in picorv32.v), so ENABLE_FAST_MUL and an external
 *  PCPI device coexist: MUL/MULH keep working while custom-0 goes to red_accel.
 *
 *  Bus arbitration is a 1-bit mux. The accelerator only owns the bus while the
 *  CPU is stalled on pcpi_wait, so the two never contend.
 */

`timescale 1 ns / 1 ps

module red_core #(
	parameter [ 0:0] ENABLE_ACCEL  = 1,
	parameter [31:0] PROGADDR_RESET = 32'h 0000_0000,
	parameter [31:0] STACKADDR      = 32'h 0002_0000
) (
	input clk, resetn,
	output trap,

	output        mem_valid,
	output        mem_instr,
	input         mem_ready,
	output [31:0] mem_addr,
	output [31:0] mem_wdata,
	output [ 3:0] mem_wstrb,
	input  [31:0] mem_rdata
);
	// ---- CPU side ----
	wire        cpu_mem_valid, cpu_mem_instr;
	wire [31:0] cpu_mem_addr, cpu_mem_wdata;
	wire [ 3:0] cpu_mem_wstrb;
	wire        cpu_mem_ready;

	wire        pcpi_valid;
	wire [31:0] pcpi_insn, pcpi_rs1, pcpi_rs2;
	wire        pcpi_wr, pcpi_wait, pcpi_ready;
	wire [31:0] pcpi_rd;

	picorv32 #(
		.ENABLE_COUNTERS (1),
		.ENABLE_MUL      (0),
		.ENABLE_FAST_MUL (1),          // same multiplier in both cores
		.ENABLE_DIV      (1),
		.ENABLE_PCPI     (ENABLE_ACCEL),
		.ENABLE_IRQ      (0),
		.COMPRESSED_ISA  (0),
		.PROGADDR_RESET  (PROGADDR_RESET),
		.STACKADDR       (STACKADDR)
	) cpu (
		.clk(clk), .resetn(resetn), .trap(trap),
		.mem_valid(cpu_mem_valid), .mem_instr(cpu_mem_instr),
		.mem_ready(cpu_mem_ready), .mem_addr(cpu_mem_addr),
		.mem_wdata(cpu_mem_wdata), .mem_wstrb(cpu_mem_wstrb),
		.mem_rdata(mem_rdata),
		.pcpi_valid(pcpi_valid), .pcpi_insn(pcpi_insn),
		.pcpi_rs1(pcpi_rs1), .pcpi_rs2(pcpi_rs2),
		.pcpi_wr(pcpi_wr), .pcpi_rd(pcpi_rd),
		.pcpi_wait(pcpi_wait), .pcpi_ready(pcpi_ready),
		.irq(32'b0)
	);

	// ---- accelerator side ----
	wire        acc_busy;
	wire        acc_mem_valid;
	wire [31:0] acc_mem_addr, acc_mem_wdata;
	wire [ 3:0] acc_mem_wstrb;

	generate if (ENABLE_ACCEL) begin : accel
		red_accel unit (
			.clk(clk), .resetn(resetn),
			.pcpi_valid(pcpi_valid), .pcpi_insn(pcpi_insn),
			.pcpi_rs1(pcpi_rs1), .pcpi_rs2(pcpi_rs2),
			.pcpi_wr(pcpi_wr), .pcpi_rd(pcpi_rd),
			.pcpi_wait(pcpi_wait), .pcpi_ready(pcpi_ready),
			.mem_valid(acc_mem_valid), .mem_addr(acc_mem_addr),
			.mem_wdata(acc_mem_wdata), .mem_wstrb(acc_mem_wstrb),
			.mem_rdata(mem_rdata), .mem_ready(acc_busy && mem_ready),
			.busy(acc_busy)
		);
	end else begin : no_accel
		assign pcpi_wr = 0, pcpi_rd = 32'b0, pcpi_wait = 0, pcpi_ready = 0;
		assign acc_busy = 0, acc_mem_valid = 0;
		assign acc_mem_addr = 32'b0, acc_mem_wdata = 32'b0, acc_mem_wstrb = 4'b0;
	end endgenerate

	// ---- bus mux ----
	assign mem_valid     = acc_busy ? acc_mem_valid : cpu_mem_valid;
	assign mem_instr     = acc_busy ? 1'b0          : cpu_mem_instr;
	assign mem_addr      = acc_busy ? acc_mem_addr  : cpu_mem_addr;
	assign mem_wdata     = acc_busy ? acc_mem_wdata : cpu_mem_wdata;
	assign mem_wstrb     = acc_busy ? acc_mem_wstrb : cpu_mem_wstrb;
	assign cpu_mem_ready = acc_busy ? 1'b0          : mem_ready;
endmodule
