`timescale 1ns / 1ps

module axis_loopback (
    input logic clk
    , input logic rst
    // incoming axi stream
    , input logic [15:0] s_axis_tx_tdata
    , input logic s_axis_tx_tvalid
    , output logic s_axis_tx_tready
    // outgoing axi stream
    , output logic [15:0] m_axis_rx_tdata
    , output logic m_axis_rx_tvalid
    , input logic m_axis_rx_tready
);

  // State machine states
  typedef enum logic [1:0] {
    IDLE,     // Waiting for incoming transaction
    CAPTURE,  // Capturing data
    SEND      // Sending captured data
  } state_t;

  // All registers in the module
  typedef struct packed {
    state_t      state;  // Current state
    logic [15:0] data;   // Captured data register
  } regs_t;

  regs_t init;
  regs_t next;
  regs_t curr;

  initial begin
    init.state = IDLE;
    init.data  = '0;
  end

  always_ff @(posedge clk) begin
    if (rst) curr <= init;
    else curr <= next;
  end

  always_comb begin
    next = curr;

    // Default output values
    s_axis_tx_tready = 1'b0;
    m_axis_rx_tvalid = 1'b0;
    m_axis_rx_tdata = curr.data;

    unique case (curr.state)
      IDLE: begin
        if (s_axis_tx_tvalid) begin
          next.state = CAPTURE;
        end
      end

      CAPTURE: begin
        s_axis_tx_tready = 1'b1;
        if (s_axis_tx_tvalid) begin
          next.data = s_axis_tx_tdata;
        end
        next.state = SEND;
      end

      SEND: begin
        m_axis_rx_tvalid = 1'b1;
        if (m_axis_rx_tready) begin
          next.state = IDLE;
        end
      end

      default: begin
        next.state = IDLE;
      end
    endcase
  end

endmodule
