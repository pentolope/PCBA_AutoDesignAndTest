// Behavioral board DUT: the PCBA reference implementation of the
// divide-by-two relation. Carries no assertions - the contract lives
// in the checker.
module clock_divider_dut (
    input  logic clk,
    input  logic rst,
    output logic [0:0] divided
);
    always_ff @(posedge clk) begin
        if (rst) divided <= 1'b0;
        else     divided <= ~divided;
    end
endmodule
