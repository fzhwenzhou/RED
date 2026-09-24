/* PCPI implementation of eval/spec/libcrc.json. */
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
    wire [2:0] decoded_op = pcpi_insn[14:12];
    wire insn_any = pcpi_valid && pcpi_insn[6:0] == 7'b1111011
        && pcpi_insn[31:25] == 0 && decoded_op <= 2;
    reg [2:0] op;
    wire [4:0] nwords = op == 0 ? 10 : 9;
    reg [31:0] operands [0:15], results [0:15];
    reg [31:0] in_addr, out_addr;
    reg [4:0] idx;
    reg [5:0] byte_idx;
    reg [2:0] bit_idx;
    reg [63:0] crc64;
    reg [31:0] crc32;
    reg [15:0] crc16;
    integer k;

    wire [3:0] data_word = (op == 0 ? 2 : 1) + byte_idx[5:2];
    wire [31:0] packed_word = operands[data_word];
    wire [7:0] data_byte = packed_word >> ({3'b0, byte_idx[1:0]} * 8);
    wire [63:0] crc64_pre = bit_idx == 0 ? crc64 ^ ({56'b0, data_byte} << 56) : crc64;
    wire [31:0] crc32_pre = bit_idx == 0 ? crc32 ^ data_byte : crc32;
    wire [15:0] crc16_pre = bit_idx == 0 ? crc16 ^ data_byte : crc16;
    wire [63:0] crc64_next = (crc64_pre << 1) ^
        (64'h42F0E1EBA9EA3693 & {64{crc64_pre[63]}});
    wire [31:0] crc32_next = (crc32_pre >> 1) ^
        (32'hEDB88320 & {32{crc32_pre[0]}});
    wire [15:0] crc16_next = (crc16_pre >> 1) ^
        (16'hA001 & {16{crc16_pre[0]}});

    assign mem_wdata = results[idx[3:0]];
    localparam [3:0] S_IDLE=0, S_LOAD=1, S_INIT=2, S_BIT=3,
                     S_STORE_SETUP=4, S_STORE=5, S_DONE=6, S_WAIT=7;
    reg [3:0] state;
    assign busy = state != S_IDLE;
    reg mem_issued;
    wire beat_done = mem_valid && mem_issued && mem_ready;
    wire [31:0] next_off = {25'd0, (idx + 1'b1), 2'b00};

    always @(posedge clk) begin
        if (!resetn) begin
            state<=S_IDLE; mem_valid<=0; mem_wstrb<=0; mem_issued<=0;
            pcpi_wait<=0; pcpi_ready<=0; pcpi_wr<=0; pcpi_rd<=0;
            idx<=0; byte_idx<=0; bit_idx<=0; op<=0;
            crc64<=0; crc32<=0; crc16<=0;
        end else begin
            pcpi_ready<=0; pcpi_wr<=0;
            if (mem_valid && !mem_issued) mem_issued<=1;
            case (state)
                S_IDLE: if (insn_any) begin
                    op<=decoded_op; in_addr<=pcpi_rs1; out_addr<=pcpi_rs2;
                    idx<=0; pcpi_wait<=1; mem_valid<=1; mem_addr<=pcpi_rs1;
                    mem_wstrb<=0; mem_issued<=0; state<=S_LOAD;
                    for (k=0;k<16;k=k+1) results[k]<=0;
                end
                S_LOAD: if (beat_done) begin
                    operands[idx[3:0]]<=mem_rdata; mem_issued<=0;
                    if (idx+1 == nwords) begin mem_valid<=0; state<=S_INIT; end
                    else begin idx<=idx+1; mem_addr<=in_addr+next_off; end
                end
                S_INIT: begin
                    crc64<={operands[1],operands[0]};
                    crc32<=operands[0]; crc16<=operands[0][15:0];
                    byte_idx<=0; bit_idx<=0; state<=S_BIT;
                end
                S_BIT: begin
                    if (op==0) crc64<=crc64_next;
                    else if (op==1) crc16<=crc16_next;
                    else crc32<=crc32_next;
                    if (bit_idx==7) begin
                        bit_idx<=0;
                        if (byte_idx==31) begin
                            if (op==0) begin results[0]<=crc64_next[31:0]; results[1]<=crc64_next[63:32]; end
                            else if (op==1) results[0]<={16'b0,crc16_next};
                            else results[0]<=crc32_next;
                            state<=S_STORE_SETUP;
                        end else byte_idx<=byte_idx+1;
                    end else bit_idx<=bit_idx+1;
                end
                S_STORE_SETUP: begin
                    idx<=0; mem_valid<=1; mem_addr<=out_addr;
                    mem_wstrb<=4'b1111; mem_issued<=0; state<=S_STORE;
                end
                S_STORE: if (beat_done) begin
                    mem_issued<=0;
                    if (idx+1 == nwords) begin mem_valid<=0; mem_wstrb<=0; state<=S_DONE; end
                    else begin idx<=idx+1; mem_addr<=out_addr+next_off; end
                end
                S_DONE: begin pcpi_wait<=0; pcpi_ready<=1; pcpi_wr<=1; pcpi_rd<=0; state<=S_WAIT; end
                S_WAIT: if (!pcpi_valid) state<=S_IDLE;
                default: state<=S_IDLE;
            endcase
        end
    end
endmodule
