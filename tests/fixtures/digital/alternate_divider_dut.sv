// A second, differently-coded correct DUT: stands in for production
// RTL substituting the behavioral model under the SAME checker.
module alternate_divider_dut (
    input  logic clk,
    input  logic rst,
    output logic [0:0] divided
);
    logic [3:0] count;

    always_ff @(posedge clk) begin
        if (rst) count <= '0;
        else     count <= count + 4'd1;
    end

    assign divided = count[0:0];
endmodule
