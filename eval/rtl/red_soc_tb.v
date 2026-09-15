/*
 *  red_soc_tb — the SoC the end-to-end evaluation runs on.
 *
 *  red_core (PicoRV32 +/- the RED accelerator) plus a 1-cycle SRAM and three
 *  MMIO registers. Identical for the baseline and extended runs except for the
 *  ENABLE_ACCEL parameter, so the cycle difference is the extension's alone.
 *
 *      +firmware=<file.hex>   program image ($readmemh, 32-bit words)
 *      +timeout=<cycles>      abort instead of hanging (default 2,000,000,000)
 *
 *  MMIO:
 *      0x10000000  write  putchar (byte)
 *      0x10000004  write  end simulation; value is the exit status
 *      0x10000008  read   cycles since reset
 */

`timescale 1 ns / 1 ps

module red_soc_tb;
	parameter [0:0] ENABLE_ACCEL = 1;
	localparam MEM_WORDS = 65536;            // 256 KiB

	reg clk = 1, resetn = 0;
	always #5 clk = ~clk;

	wire trap;
	wire        mem_valid, mem_instr;
	wire [31:0] mem_addr, mem_wdata;
	wire [ 3:0] mem_wstrb;
	reg  [31:0] mem_rdata;
	reg         mem_ready;

	red_core #(.ENABLE_ACCEL(ENABLE_ACCEL)) core (
		.clk(clk), .resetn(resetn), .trap(trap),
		.mem_valid(mem_valid), .mem_instr(mem_instr), .mem_ready(mem_ready),
		.mem_addr(mem_addr), .mem_wdata(mem_wdata), .mem_wstrb(mem_wstrb),
		.mem_rdata(mem_rdata)
	);

	reg [31:0] memory [0:MEM_WORDS-1];
	reg [63:0] cycles = 0;
	always @(posedge clk) cycles <= resetn ? cycles + 1 : 0;

	// ---- memory + MMIO ----
	always @(posedge clk) begin
		mem_ready <= 0;
		if (mem_valid && !mem_ready) begin
			if (mem_addr < (MEM_WORDS << 2)) begin
				mem_ready <= 1;
				mem_rdata <= memory[mem_addr >> 2];
				if (mem_wstrb[0]) memory[mem_addr >> 2][ 7: 0] <= mem_wdata[ 7: 0];
				if (mem_wstrb[1]) memory[mem_addr >> 2][15: 8] <= mem_wdata[15: 8];
				if (mem_wstrb[2]) memory[mem_addr >> 2][23:16] <= mem_wdata[23:16];
				if (mem_wstrb[3]) memory[mem_addr >> 2][31:24] <= mem_wdata[31:24];
			end else if (mem_addr == 32'h1000_0000 && mem_wstrb) begin
				mem_ready <= 1;
				$write("%c", mem_wdata[7:0]);
				$fflush();
			end else if (mem_addr == 32'h1000_0004 && mem_wstrb) begin
				mem_ready <= 1;
				$display("\n[sim] finished: status=%0d  cycles=%0d", mem_wdata, cycles);
				$finish;
			end else if (mem_addr == 32'h1000_0008) begin
				mem_ready <= 1;
				mem_rdata <= cycles[31:0];
			end else begin
				mem_ready <= 1;
				mem_rdata <= 32'h0;
				$display("[sim] unmapped access at 0x%08x (wstrb=%b)", mem_addr, mem_wstrb);
			end
		end
	end

	// +trace_accel: log every bus beat the coprocessor performs
	always @(posedge clk) begin
		if (resetn && $test$plusargs("trace_accel") && core.acc_busy
		    && mem_valid && mem_ready)
			$display("[accel] %0s addr=%08x data=%08x",
			         mem_wstrb ? "WR" : "RD", mem_addr,
			         mem_wstrb ? mem_wdata : mem_rdata);
	end

	reg [1023:0] firmware;
	integer timeout;
	initial begin
		if (!$value$plusargs("firmware=%s", firmware)) begin
			$display("[sim] give +firmware=<hex>"); $finish;
		end
		if (!$value$plusargs("timeout=%d", timeout)) timeout = 2000000000;
		for (integer i = 0; i < MEM_WORDS; i = i + 1) memory[i] = 32'h0;
		$readmemh(firmware, memory);
		repeat (10) @(posedge clk);
		resetn <= 1;
	end

	always @(posedge clk) begin
		if (resetn && trap) begin
			$display("\n[sim] TRAP after %0d cycles", cycles);
			$finish;
		end
		if (resetn && cycles > timeout) begin
			$display("\n[sim] TIMEOUT after %0d cycles", cycles);
			$finish;
		end
	end
endmodule
