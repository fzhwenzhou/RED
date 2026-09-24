/* Simulation model of eval/spec/matrixmul.json.
 * Uses SystemVerilog shortreal to implement the spec's IEEE-754 operations.
 * This model is cycle-accurate at the PCPI/memory boundary but is behavioral
 * and intentionally excluded from synthesis area claims.
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
    wire insn_any = pcpi_valid && pcpi_insn[6:0] == 7'b0001011
        && pcpi_insn[14:12] == 0 && pcpi_insn[31:25] == 7'b0000110;
    reg [31:0] operands [0:4], results [0:4];
    reg [31:0] in_addr, out_addr, p0, p1, s0;
    reg [2:0] idx;
    integer k;

    `ifdef VERILATOR
    import "DPI-C" function int f32_mul_dpi(input int x, input int y);
    import "DPI-C" function int f32_add_dpi(input int x, input int y);
    function automatic [31:0] f32_mul(input [31:0] x, input [31:0] y);
        f32_mul = f32_mul_dpi(x, y);
    endfunction
    function automatic [31:0] f32_add(input [31:0] x, input [31:0] y);
        f32_add = f32_add_dpi(x, y);
    endfunction
    `else
    function automatic is_nan(input [31:0] x);
        is_nan = x[30:23] == 8'hff && x[22:0] != 0;
    endfunction
    function automatic [31:0] f32_mul(input [31:0] x, input [31:0] y);
        shortreal a, b, r;
        reg [31:0] z;
        begin
            a = $bitstoshortreal(x); b = $bitstoshortreal(y); r = a * b;
            z = $shortrealtobits(r);
            f32_mul = is_nan(z) ? 32'h7fc00000 : z;
        end
    endfunction
    function automatic [31:0] f32_add(input [31:0] x, input [31:0] y);
        shortreal a, b, r;
        reg [31:0] z;
        begin
            a = $bitstoshortreal(x); b = $bitstoshortreal(y); r = a + b;
            z = $shortrealtobits(r);
            f32_add = is_nan(z) ? 32'h7fc00000 : z;
        end
    endfunction
    `endif

    assign mem_wdata = results[idx];
    localparam [3:0] S_IDLE=0,S_LOAD=1,S_MUL0=2,S_MUL1=3,S_ADD0=4,
                     S_ADD1=5,S_STORE_SETUP=6,S_STORE=7,S_DONE=8,S_WAIT=9;
    reg [3:0] state;
    assign busy = state != S_IDLE;
    reg mem_issued;
    wire beat_done = mem_valid && mem_issued && mem_ready;
    wire [31:0] next_off = {27'd0,(idx+1'b1),2'b00};

    always @(posedge clk) begin
        if (!resetn) begin
            state<=S_IDLE; mem_valid<=0; mem_wstrb<=0; mem_issued<=0;
            pcpi_wait<=0; pcpi_ready<=0; pcpi_wr<=0; pcpi_rd<=0; idx<=0;
        end else begin
            pcpi_ready<=0; pcpi_wr<=0;
            if (mem_valid && !mem_issued) mem_issued<=1;
            case(state)
                S_IDLE: if (insn_any) begin
                    in_addr<=pcpi_rs1; out_addr<=pcpi_rs2; idx<=0;
                    for(k=0;k<5;k=k+1) results[k]<=0;
                    pcpi_wait<=1; mem_valid<=1; mem_addr<=pcpi_rs1;
                    mem_wstrb<=0; mem_issued<=0; state<=S_LOAD;
                end
                S_LOAD: if (beat_done) begin
                    operands[idx]<=mem_rdata; mem_issued<=0;
                    if(idx==4) begin mem_valid<=0; state<=S_MUL0; end
                    else begin idx<=idx+1; mem_addr<=in_addr+next_off; end
                end
                S_MUL0: begin p0<=f32_mul(operands[0],operands[2]); state<=S_MUL1; end
                S_MUL1: begin p1<=f32_mul(operands[1],operands[3]); state<=S_ADD0; end
                S_ADD0: begin s0<=f32_add(operands[4],p0); state<=S_ADD1; end
                S_ADD1: begin results[0]<=f32_add(s0,p1); state<=S_STORE_SETUP; end
                S_STORE_SETUP: begin idx<=0; mem_valid<=1; mem_addr<=out_addr;
                    mem_wstrb<=4'b1111; mem_issued<=0; state<=S_STORE; end
                S_STORE: if (beat_done) begin
                    mem_issued<=0;
                    if(idx==4) begin mem_valid<=0; mem_wstrb<=0; state<=S_DONE; end
                    else begin idx<=idx+1; mem_addr<=out_addr+next_off; end
                end
                S_DONE: begin pcpi_wait<=0;pcpi_ready<=1;pcpi_wr<=1;pcpi_rd<=0;state<=S_WAIT;end
                S_WAIT: if(!pcpi_valid) state<=S_IDLE;
                default: state<=S_IDLE;
            endcase
        end
    end
endmodule
