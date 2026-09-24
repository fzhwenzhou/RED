/*
 * PicoRV32 PCPI coprocessor for eval/spec/micro-ecc.json.
 * secp256r1.mult reads two 256-bit operands from a 16-word input block and
 * writes their 512-bit product to a 16-word output block. A single 32x32
 * multiplier is reused across the 64 schoolbook multiply-accumulate steps.
 */
`timescale 1 ns / 1 ps
module red_accel (
    input clk, resetn,
    input pcpi_valid, input [31:0] pcpi_insn, pcpi_rs1, pcpi_rs2,
    output reg pcpi_wr, output reg [31:0] pcpi_rd,
    output reg pcpi_wait, output reg pcpi_ready,
    output reg mem_valid, output reg [31:0] mem_addr,
    output [31:0] mem_wdata, output reg [3:0] mem_wstrb,
    input [31:0] mem_rdata, input mem_ready, output busy
);
    localparam [6:0] OPCODE_CUSTOM0 = 7'b0001011;
    wire insn_any = pcpi_valid && pcpi_insn[6:0] == OPCODE_CUSTOM0
        && pcpi_insn[14:12] == 3'b000 && pcpi_insn[31:25] == 7'b0000000;

    reg [31:0] operands [0:15];
    reg [31:0] product_words [0:15];
    reg [31:0] in_addr, out_addr;
    reg [4:0] idx;
    reg [2:0] row, col;
    reg [31:0] carry;
    integer k;

    wire [3:0] product_idx = {1'b0, row} + {1'b0, col};
    wire [63:0] limb_product = operands[{1'b0, row}] * operands[{1'b1, col}];
    wire [63:0] mac_result = limb_product
        + {32'b0, product_words[product_idx]} + {32'b0, carry};
    assign mem_wdata = product_words[idx[3:0]];

    localparam [2:0] S_IDLE=0, S_LOAD=1, S_MAC=2, S_CARRY=3,
                     S_STORE=4, S_DONE=5, S_WAIT=6;
    reg [2:0] state;
    assign busy = state != S_IDLE;
    wire [31:0] next_off = {25'd0, (idx + 5'd1), 2'b00};
    reg mem_issued;
    wire beat_done = mem_valid && mem_issued && mem_ready;

    always @(posedge clk) begin
        if (!resetn) begin
            state <= S_IDLE; mem_valid <= 0; mem_wstrb <= 0;
            mem_issued <= 0; pcpi_wait <= 0; pcpi_ready <= 0;
            pcpi_wr <= 0; pcpi_rd <= 0; idx <= 0;
            row <= 0; col <= 0; carry <= 0;
        end else begin
            pcpi_ready <= 0;
            pcpi_wr <= 0;
            if (mem_valid && !mem_issued) mem_issued <= 1;
            case (state)
                S_IDLE: if (insn_any) begin
                    in_addr <= pcpi_rs1; out_addr <= pcpi_rs2;
                    idx <= 0; row <= 0; col <= 0; carry <= 0;
                    for (k = 0; k < 16; k = k + 1) product_words[k] <= 0;
                    pcpi_wait <= 1;
                    mem_valid <= 1; mem_addr <= pcpi_rs1; mem_wstrb <= 0;
                    mem_issued <= 0; state <= S_LOAD;
                end
                S_LOAD: if (beat_done) begin
                    operands[idx[3:0]] <= mem_rdata;
                    mem_issued <= 0;
                    if (idx == 15) begin
                        mem_valid <= 0; state <= S_MAC;
                    end else begin
                        idx <= idx + 1; mem_addr <= in_addr + next_off;
                    end
                end
                S_MAC: begin
                    product_words[product_idx] <= mac_result[31:0];
                    carry <= mac_result[63:32];
                    if (col == 7) state <= S_CARRY;
                    else col <= col + 1;
                end
                S_CARRY: begin
                    product_words[{1'b1, row}] <= carry;
                    carry <= 0; col <= 0;
                    if (row == 7) begin
                        idx <= 0; mem_valid <= 1; mem_addr <= out_addr;
                        mem_wstrb <= 4'b1111; mem_issued <= 0; state <= S_STORE;
                    end else begin
                        row <= row + 1; state <= S_MAC;
                    end
                end
                S_STORE: if (beat_done) begin
                    mem_issued <= 0;
                    if (idx == 15) begin
                        mem_valid <= 0; mem_wstrb <= 0; state <= S_DONE;
                    end else begin
                        idx <= idx + 1; mem_addr <= out_addr + next_off;
                    end
                end
                S_DONE: begin
                    pcpi_wait <= 0; pcpi_ready <= 1; pcpi_wr <= 1;
                    pcpi_rd <= 0; state <= S_WAIT;
                end
                S_WAIT: if (!pcpi_valid) state <= S_IDLE;
                default: state <= S_IDLE;
            endcase
        end
    end
endmodule
