// PCBA-owned contract checker: the assertions ARE the board contract.
// Every DUT - the behavioral board model or production RTL - runs
// against exactly this module; substituting the DUT never touches it.
module clock_divider_checker (
    input logic clk,
    input logic rst,
    input logic [0:0] divided
);
    logic [0:0] previous;
    logic armed;

    always_ff @(posedge clk) begin
        if (rst) begin
            armed    <= 1'b0;
            previous <= 1'b0;
        end else begin
            // Registered sampling: at each posedge both `divided` and
            // `previous` hold pre-edge values, so the comparison sees
            // two consecutive sampled values and never races the
            // DUT's nonblocking update. `armed` skips the first
            // sample after reset release.
            if (armed)
                assert (divided != previous)
                    else $fatal(1, "contract: divided failed to toggle");
            previous <= divided;
            armed    <= 1'b1;
        end
    end
endmodule
