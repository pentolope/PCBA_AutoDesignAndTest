// Deliberately wrong DUT: the output never toggles, so the contract
// checker must fire. Proves assertion failures are detected.
module broken_divider_dut (
    input  logic clk,
    input  logic rst,
    output logic [0:0] divided
);
    always_ff @(posedge clk) begin
        if (rst) divided <= 1'b0;
        else     divided <= divided;
    end
endmodule
