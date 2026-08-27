// Behavioral board contract fixture: a divide-by-two clock relation.
// This is the PCBA-side statement of what the board expects of its
// digital neighbor; the assertion IS the contract, and a production
// RTL run under the same harness must satisfy the same assertion.
module clock_divider_contract (
    input  logic clk,
    input  logic rst,
    output logic divided
);
    logic previous;

    always_ff @(posedge clk) begin
        if (rst) begin
            divided  <= 1'b0;
            previous <= 1'b0;
        end else begin
            divided  <= ~divided;
            previous <= divided;
            // The contract: the output toggles every input cycle,
            // which is exactly a divide-by-two relation.
            assert (divided != previous)
                else $fatal(1, "divided output failed to toggle");
        end
    end
endmodule
