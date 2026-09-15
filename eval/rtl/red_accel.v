/*
 *  red_accel — PicoRV32 PCPI coprocessor for a RED-designed ISA extension.
 *
 *  This is the A3 step of the RED design, done by hand for one ISASpec
 *  (output/db/<workload>/run_N/ISASpec.json). It implements the instructions
 *  exactly as Gate 2 verified them, including RED's fixed operand ABI:
 *
 *      <insn> rd, rs1, rs2
 *          rs1 = address of the input  block (`words` x uint32, little-endian)
 *          rs2 = address of the output block (`words` x uint32)
 *          rd  <- 0
 *
 *  Because the operands live in memory, the coprocessor is a bus master: it
 *  reads `words` words from rs1, computes, and writes `words` words to rs2. The
 *  CPU is stalled by pcpi_wait for the whole sequence, so a 1-bit `busy` output
 *  is all the arbitration the SoC needs — there is never contention.
 *
 *  Instruction table (from the ISASpec; opcode custom-0 = 0001011, funct7 = 0):
 *      funct3 000  mac96    words=5   in[0..2]=96b acc, in[3]=a, in[4]=b
 *      funct3 001  add256   words=16  in[0..7]=x, in[8..15]=y   -> out[0..7]
 *      funct3 010  sub256   words=16  in[0..7]=x, in[8..15]=y   -> out[0..7]
 *
 *  Verified against the ISASpec's C models by eval/rtl/tb_accel.v.
 */

`timescale 1 ns / 1 ps

module red_accel (
	input clk, resetn,

	// ---- PCPI slave (picorv32 coprocessor interface) ----
	input             pcpi_valid,
	/* verilator lint_off UNUSEDSIGNAL */
	input      [31:0] pcpi_insn,   // only opcode/funct3/funct7 concern this unit
	/* verilator lint_on UNUSEDSIGNAL */
	input      [31:0] pcpi_rs1,
	input      [31:0] pcpi_rs2,
	output reg        pcpi_wr,
	output reg [31:0] pcpi_rd,
	output reg        pcpi_wait,
	output reg        pcpi_ready,

	// ---- memory master (picorv32 native interface shape) ----
	output reg        mem_valid,
	output reg [31:0] mem_addr,
	output     [31:0] mem_wdata,
	output reg [ 3:0] mem_wstrb,
	input      [31:0] mem_rdata,
	input             mem_ready,

	output            busy          // 1 while this unit owns the memory bus
);
	localparam [6:0] OPCODE_CUSTOM0 = 7'b0001011;

	localparam [2:0] F3_MAC96  = 3'b000;
	localparam [2:0] F3_ADD256 = 3'b001;
	localparam [2:0] F3_SUB256 = 3'b010;

	// ---- decode -----------------------------------------------------------
	wire [6:0] opcode = pcpi_insn[6:0];
	wire [2:0] funct3 = pcpi_insn[14:12];
	wire [6:0] funct7 = pcpi_insn[31:25];

	wire insn_mac96  = pcpi_valid && opcode == OPCODE_CUSTOM0 && funct7 == 7'b0 && funct3 == F3_MAC96;
	wire insn_add256 = pcpi_valid && opcode == OPCODE_CUSTOM0 && funct7 == 7'b0 && funct3 == F3_ADD256;
	wire insn_sub256 = pcpi_valid && opcode == OPCODE_CUSTOM0 && funct7 == 7'b0 && funct3 == F3_SUB256;
	wire insn_any    = insn_mac96 || insn_add256 || insn_sub256;

	reg [2:0] op;                       // latched funct3
	wire [4:0] nwords = (op == F3_MAC96) ? 5'd5 : 5'd16;

	// ---- operand buffer ---------------------------------------------------
	reg [31:0] buf_mem [0:15];
	reg [31:0] in_addr, out_addr;
	reg [4:0]  idx;

	// ---- datapath ---------------------------------------------------------
	// add256 / sub256 share one 256-bit adder: x - y == x + ~y + 1.
	wire [255:0] opa = {buf_mem[ 7], buf_mem[ 6], buf_mem[ 5], buf_mem[ 4],
	                    buf_mem[ 3], buf_mem[ 2], buf_mem[ 1], buf_mem[ 0]};
	wire [255:0] opb = {buf_mem[15], buf_mem[14], buf_mem[13], buf_mem[12],
	                    buf_mem[11], buf_mem[10], buf_mem[ 9], buf_mem[ 8]};
	wire         do_sub = (op == F3_SUB256);
	wire [255:0] addsub = opa + (do_sub ? ~opb : opb) + (do_sub ? 256'd1 : 256'd0);

	// mac96: 96-bit accumulator += a * b
	wire [63:0] prod = buf_mem[3] * buf_mem[4];
	wire [95:0] acc  = {buf_mem[2], buf_mem[1], buf_mem[0]};
	wire [95:0] mac  = acc + {32'd0, prod};        // 96-bit wrap matches the C model

	// The word STORE is currently pushing out. Driven straight onto mem_wdata:
	// registering it would put word N-1's data on word N's address.
	reg [31:0] store_word;
	assign mem_wdata = store_word;
	always @* begin
		store_word = 32'd0;
		case (op)
			F3_MAC96:
				case (idx)
					5'd0: store_word = mac[31:0];
					5'd1: store_word = mac[63:32];
					5'd2: store_word = mac[95:64];
					default: store_word = 32'd0;      // out[3..4] = 0
				endcase
			F3_ADD256, F3_SUB256:
				store_word = (idx < 5'd8) ? addsub[idx[2:0]*32 +: 32]
				                          : 32'd0;    // out[8..15] = 0
			default: store_word = 32'd0;
		endcase
	end

	// ---- control ----------------------------------------------------------
	// S_WAIT exists because the PCPI contract keeps pcpi_valid asserted for a
	// cycle after pcpi_ready: returning straight to S_IDLE re-triggers the same
	// instruction on the stale operands. Wait for the core to drop valid.
	localparam [2:0] S_IDLE = 3'd0, S_LOAD = 3'd1, S_STORE = 3'd2,
	                 S_DONE = 3'd3, S_WAIT = 3'd4;
	reg [2:0] state;

	assign busy = (state != S_IDLE);

	// next word's byte offset, widened once so the address adds stay 32-bit
	wire [31:0] next_off = {25'd0, (idx + 5'd1), 2'b00};

	// A bus beat only completes once OUR request has been on the bus for a full
	// cycle. Without this the unit accepts the mem_ready still asserted from the
	// CPU's last instruction fetch as the answer to its own first read, and
	// latches the fetched instruction word as operand data. The CPU is stalled
	// on pcpi_wait by then, so that stale ready is the one cycle of overlap the
	// hand-over leaves behind.
	reg  mem_issued;
	wire beat_done = mem_valid && mem_issued && mem_ready;

	always @(posedge clk) begin
		if (!resetn) begin
			state      <= S_IDLE;
			mem_valid  <= 0;
			mem_wstrb  <= 4'b0000;
			mem_issued <= 0;
			pcpi_wait  <= 0;
			pcpi_ready <= 0;
			pcpi_wr    <= 0;
			pcpi_rd    <= 0;
			idx        <= 0;
		end else begin
			pcpi_ready <= 0;
			pcpi_wr    <= 0;
			if (mem_valid && !mem_issued) mem_issued <= 1;
			case (state)
				S_IDLE: begin
					if (insn_any) begin
						op        <= funct3;
						in_addr   <= pcpi_rs1;
						out_addr  <= pcpi_rs2;
						idx       <= 0;
						pcpi_wait <= 1;
						state     <= S_LOAD;
						// first read
						mem_valid  <= 1;
						mem_addr   <= pcpi_rs1;
						mem_wstrb  <= 4'b0000;
						mem_issued <= 0;
					end
				end
				S_LOAD: begin
					if (beat_done) begin
						buf_mem[idx[3:0]] <= mem_rdata;
						mem_issued <= 0;
						if (idx + 1 == nwords) begin
							// switch to storing: first write
							idx       <= 0;
							mem_valid <= 1;
							mem_addr  <= out_addr;
							mem_wstrb <= 4'b1111;
							state     <= S_STORE;
						end else begin
							idx       <= idx + 1;
							mem_valid <= 1;
							mem_addr  <= in_addr + next_off;
						end
					end
				end
				S_STORE: begin
					if (beat_done) begin
						mem_issued <= 0;
						if (idx + 1 == nwords) begin
							mem_valid <= 0;
							mem_wstrb <= 4'b0000;
							state     <= S_DONE;
						end else begin
							idx       <= idx + 1;
							mem_addr  <= out_addr + next_off;
						end
					end
				end
				S_DONE: begin
					pcpi_wait  <= 0;
					pcpi_ready <= 1;
					pcpi_wr    <= 1;
					pcpi_rd    <= 32'd0;      // the ABI writes 0 to rd
					state      <= S_WAIT;
				end
				S_WAIT: begin
					if (!pcpi_valid) state <= S_IDLE;
				end
				default: state <= S_IDLE;
			endcase
		end
	end
endmodule
