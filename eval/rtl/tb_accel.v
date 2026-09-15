/*
 *  tb_accel — differential test of red_accel against the ISASpec's C models.
 *
 *  Closes the C-model <=> RTL link of the design's verification chain. For each
 *  vector produced by eval/scripts/gen_vectors.py it writes the input block to
 *  memory, issues the instruction over PCPI exactly as picorv32 would, and
 *  compares the output block the coprocessor wrote back against what the C
 *  model computed. It also reports each instruction's measured latency, which
 *  is what the cycle accounting in eval/RESULTS.md is built on.
 *
 *  Run:  iverilog -o tb_accel.vvp eval/rtl/tb_accel.v eval/rtl/red_accel.v
 *        vvp tb_accel.vvp +vectors=eval/build/vectors.txt
 */

`timescale 1 ns / 1 ps

module tb_accel;
	localparam MAXW    = 16;
	localparam IN_ADDR  = 32'h0000_1000;
	localparam OUT_ADDR = 32'h0000_2000;

	reg clk = 1, resetn = 0;
	always #5 clk = ~clk;

	// ---- DUT ----
	reg         pcpi_valid = 0;
	reg  [31:0] pcpi_insn  = 0;
	reg  [31:0] pcpi_rs1   = 0;
	reg  [31:0] pcpi_rs2   = 0;
	wire        pcpi_wr, pcpi_wait, pcpi_ready;
	wire [31:0] pcpi_rd;

	wire        mem_valid;
	wire [31:0] mem_addr, mem_wdata;
	wire [ 3:0] mem_wstrb;
	reg  [31:0] mem_rdata;
	reg         mem_ready = 0;
	wire        busy;

	red_accel dut (
		.clk(clk), .resetn(resetn),
		.pcpi_valid(pcpi_valid), .pcpi_insn(pcpi_insn),
		.pcpi_rs1(pcpi_rs1), .pcpi_rs2(pcpi_rs2),
		.pcpi_wr(pcpi_wr), .pcpi_rd(pcpi_rd),
		.pcpi_wait(pcpi_wait), .pcpi_ready(pcpi_ready),
		.mem_valid(mem_valid), .mem_addr(mem_addr), .mem_wdata(mem_wdata),
		.mem_wstrb(mem_wstrb), .mem_rdata(mem_rdata), .mem_ready(mem_ready),
		.busy(busy)
	);

	// ---- 1-cycle SRAM, same shape as the picorv32 testbench memory ----
	reg [31:0] memory [0:4095];
	always @(posedge clk) begin
		mem_ready <= 0;
		if (mem_valid && !mem_ready) begin
			mem_ready <= 1;
			mem_rdata <= memory[mem_addr[15:2]];
			if (mem_wstrb[0]) memory[mem_addr[15:2]][ 7: 0] <= mem_wdata[ 7: 0];
			if (mem_wstrb[1]) memory[mem_addr[15:2]][15: 8] <= mem_wdata[15: 8];
			if (mem_wstrb[2]) memory[mem_addr[15:2]][23:16] <= mem_wdata[23:16];
			if (mem_wstrb[3]) memory[mem_addr[15:2]][31:24] <= mem_wdata[31:24];
		end
	end

	// ---- vector file ----
	integer fd, rc, v, i, funct3, nwords, errors, nvec, alias_pass;
	reg [31:0] out_base;
	reg [31:0] vin  [0:MAXW-1];
	reg [31:0] vexp [0:MAXW-1];
	reg [31:0] got;
	reg [1023:0] vfile;

	integer t0, cycles, cyc_min [0:7], cyc_max [0:7], cyc_sum [0:7], cyc_n [0:7];
	integer cycle_counter;
	always @(posedge clk) cycle_counter <= resetn ? cycle_counter + 1 : 0;

	initial begin
		if (!$value$plusargs("vectors=%s", vfile)) vfile = "eval/build/vectors.txt";
		for (i = 0; i < 8; i = i + 1) begin
			cyc_min[i] = 1000000; cyc_max[i] = 0; cyc_sum[i] = 0; cyc_n[i] = 0;
		end
		cycle_counter = 0;
		errors = 0; nvec = 0;

		repeat (5) @(posedge clk);
		resetn <= 1;
		repeat (2) @(posedge clk);

	  for (alias_pass = 0; alias_pass <= 1; alias_pass = alias_pass + 1) begin
		fd = $fopen(vfile, "r");
		if (fd == 0) begin
			$display("FAIL: cannot open vector file %0s", vfile);
			$finish;
		end
		v  = 0;
		rc = $fscanf(fd, "%h\n", funct3);
		while (rc == 1) begin
			rc = $fscanf(fd, "%h\n", nwords);
			for (i = 0; i < MAXW; i = i + 1) rc = $fscanf(fd, "%h\n", vin[i]);
			for (i = 0; i < MAXW; i = i + 1) rc = $fscanf(fd, "%h\n", vexp[i]);

			// input block into memory; poison the output block so a coprocessor
			// that fails to write a word cannot accidentally look correct.
			out_base = alias_pass ? IN_ADDR : OUT_ADDR;
			for (i = 0; i < MAXW; i = i + 1) begin
				memory[(IN_ADDR >> 2) + i] = vin[i];
				if (!alias_pass) memory[(OUT_ADDR >> 2) + i] = 32'hDEADBEEF;
			end

			// issue the instruction the way picorv32 drives PCPI
			t0 = cycle_counter;
			pcpi_insn  <= {7'b0000000, 5'd0, 5'd0, funct3[2:0], 5'd0, 7'b0001011};
			pcpi_rs1   <= IN_ADDR;
			pcpi_rs2   <= out_base;
			pcpi_valid <= 1;
			@(posedge clk);
			while (!pcpi_ready) @(posedge clk);
			cycles = cycle_counter - t0;
			pcpi_valid <= 0;
			if (pcpi_rd !== 32'd0)
				$display("FAIL vector %0d: rd=%08x, the ABI writes 0", v, pcpi_rd);
			@(posedge clk);

			// check the output block
			for (i = 0; i < nwords; i = i + 1) begin
				got = memory[(out_base >> 2) + i];
				if (got !== vexp[i]) begin
					errors = errors + 1;
					if (errors <= 5)
						$display("FAIL vector %0d (funct3=%0d)%0s word %0d: RTL %08x != C model %08x",
						         v, funct3, alias_pass ? " [in==out]" : "", i, got, vexp[i]);
				end
			end

			cyc_n[funct3[2:0]]   = cyc_n[funct3[2:0]] + 1;
			cyc_sum[funct3[2:0]] = cyc_sum[funct3[2:0]] + cycles;
			if (cycles < cyc_min[funct3[2:0]]) cyc_min[funct3[2:0]] = cycles;
			if (cycles > cyc_max[funct3[2:0]]) cyc_max[funct3[2:0]] = cycles;
			nvec = nvec + 1;
			v    = v + 1;
			rc   = $fscanf(fd, "%h\n", funct3);
		end
		$fclose(fd);
		$display("pass %0s: %0d vectors, %0d errors so far",
		         alias_pass ? "in==out (aliased)" : "in!=out (distinct)", v, errors);
	  end

		$display("");
		$display("=== red_accel vs ISASpec C models ===");
		$display("vectors : %0d", nvec);
		$display("errors  : %0d", errors);
		for (i = 0; i < 8; i = i + 1)
			if (cyc_n[i] > 0)
				$display("funct3=%0d latency: min %0d  max %0d  mean %0d cycles  (n=%0d)",
				         i, cyc_min[i], cyc_max[i], cyc_sum[i] / cyc_n[i], cyc_n[i]);
		$display("");
		if (errors == 0 && nvec > 0)
			$display("tb_accel: PASS");
		else
			$display("tb_accel: FAIL (%0d mismatches over %0d vectors)", errors, nvec);
		$finish;
	end
endmodule
