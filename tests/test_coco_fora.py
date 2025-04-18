import os
import random
from dataclasses import replace
from pathlib import Path

import cocotb
import forastero
import forastero_io.axi4lite
import forastero_io.axi4stream
from cocotb.clock import Clock
from cocotb.handle import HierarchyObject
from cocotb.log import SimLog
from cocotb.runner import get_runner
from cocotb.triggers import ClockCycles, FallingEdge, RisingEdge, Timer
from forastero import BaseBench, DriverEvent, IORole, MonitorEvent, SeqContext
from forastero.io import io_plain_style
from forastero.sequence import SeqProxy
from forastero_io.axi4stream import (
    AXI4StreamBackpressure,
    AXI4StreamInitiator,
    AXI4StreamIO,
    AXI4StreamMonitor,
    AXI4StreamTarget,
    AXI4StreamTransfer,
    axi4stream_backpressure,
)
from icecream import ic


class Testbench(BaseBench):
    """Testbench for the SPI controller."""

    def __init__(self, dut) -> None:
        # For every @Testbench.testcase() annotation,
        # a Testbench instance is created

        super().__init__(dut, clk=dut.clk, rst=dut.rst)
        # Creates an interface from the standalone DUT signals.
        # - matches signals with the starting prefix "s_axis_tx"
        # - and ending with the standard axistream {tdata, tvalid, tready, tlast, etc.}
        # - IORole determines which signals this interface drives
        #   - Initiator drives one set
        #   - Responder drives another set
        # - IORole **is from perspective of DUT, not testbench**
        #
        # Example DUT verilog signals:
        # module foo (
        # , input logic [15:0] s_axis_tx_tdata   <- initiator drive
        # , input logic s_axis_tx_tvalid         <- initiator drive
        # , output logic s_axis_tx_tready        <- responder drive
        # )
        #
        inbound_io = AXI4StreamIO(
            dut, "s_axis_tx", IORole.RESPONDER, io_style=io_plain_style
        )

        # Knows how to drive their corresponding signals (tdata, tvalid in this case)
        # - this is from perspective of Testbench!
        #
        # AXI4StreamInitiator is a subclass of BaseDriver
        # - required to implement `async def drive(self, transaction: AXI4StreamTransfer)`
        #
        # BaseDriver on __init__()
        # - launches an infinite loop coroutine
        # - waits for transactions to be queued, and calls `drive()` onto the signals
        initiator = AXI4StreamInitiator(self, inbound_io, self.clk, self.rst)

        self.register("inbound_drv", initiator)

        outbound_io = AXI4StreamIO(
            dut, "m_axis_rx", IORole.INITIATOR, io_style=io_plain_style
        )

        # Monitors:
        # - have a infinite loop on the given interface looking for transactions
        # - when a valid transaction occurs, the monitor publishes:
        #     self.publish(MonitorEvent.CAPTURE, obj)
        # - BaseMonitor::__init()__:
        #    cocotb.start_soon(self._monitor_loop())
        #
        # AXI4StreamMonitor:
        # - Infinite loop awaiting (tvalid and tready) -> publish capture
        monitor = AXI4StreamMonitor(self, outbound_io, self.clk, self.rst)

        # Targets:
        # - Are from the **Testbench perspective** (tb is receiver of outbound_io, tb is a target of outbound_io)
        # - An axi4stream target drives `tready` transactions only
        #
        # is also a subclass of BaseDriver
        #
        # BaseDriver on __init__()
        # - launches an infinite loop coroutine
        # - waits for transactions to be queued, and calls `drive()` onto the signals
        #
        # where the target drive is: `drive(AXI4StreamBackpressure)`
        # - like the StreamInitiator -> this is a single setting of the tready() signal
        # - the BaseDriver loop continually accepts new AXI4StreamBackpressure transactions (enables setting, clearing tready)
        driver = AXI4StreamTarget(self, outbound_io, self.clk, self.rst)

        # On thing I don't like about the dynamic registration approach
        # is losing the type of "monitor".  I wonder if I can keep it somehow
        #
        # Register:
        #
        # - registers a BaseDriver, Monitor, with this Testbench
        #
        # Mostly useful for:
        # - Setting deterministic random seed for components
        # - Registering monitors with the scoreboard
        self.register("outbound_mon", monitor)
        self.register("outbound_drv", driver)


@cocotb.test(timeout_time=10000, timeout_unit="ns")
async def smoke_cocotb_standalone(dut: HierarchyObject):
    await Timer(100, "ns")
    cocotb.start_soon(Clock(dut.clk, 10, units="ns").start())
    await RisingEdge(dut.clk)
    await ClockCycles(dut.clk, 10)


@Testbench.testcase(timeout=2000)
async def smoke_forastero(tb: Testbench, _log: SimLog):
    await ClockCycles(tb.clk, 10)


@Testbench.testcase(timeout=2000)
async def forastero_direct_drive(tb: Testbench, log: SimLog):
    await ClockCycles(tb.clk, 10)

    # Manually set the "tready" and keep it forever
    bp = AXI4StreamBackpressure(ready=True)
    tb.outbound_drv.enqueue(bp)

    for _ in range(4):
        # Manually drive transaction into the DUT
        d = tb.random.randint(0, 0xFFFF)
        elem = AXI4StreamTransfer(data=d)
        tb.inbound_drv.enqueue(elem)

        # Manually drive expected output transaction from DUT
        tb.scoreboard.channels["outbound_mon"].push_reference(elem)

        # Manually wait for the capture
        resp = await tb.outbound_mon.wait_for(MonitorEvent.CAPTURE)
        log.info(resp)


@Testbench.testcase(timeout=2000)
async def simple_forastero(tb: Testbench, log: SimLog):
    tb.schedule(axi4stream_backpressure(driver=tb.outbound_drv))

    for _ in range(4):
        elem = AXI4StreamTransfer(data=tb.random.getrandbits(16))
        tb.inbound_drv.enqueue(elem)
        tb.scoreboard.channels["outbound_mon"].push_reference(elem)

    await ClockCycles(tb.clk, 100)


def test_coco_fora() -> None:
    """Test the SPI controller."""
    sim = os.getenv("SIM", "icarus")
    proj_path = Path(__file__).resolve().parent.parent
    hdl_path = proj_path / "hdl"

    sources = [
        hdl_path / "axis_loopback.sv",
    ]
    runner = get_runner(sim)
    runner.build(
        verilog_sources=sources,
        hdl_toplevel="axis_loopback",
        waves=True,
    )

    # Within pytest `test_coco_fora` is registered.
    # In cocotb land, you can select individual registered test cases as well.
    #
    #   TESTCASE="forastero_direct_drive" uv run pytest tests/test_coco_fora.py -s
    #   TESTCASE="forastero_direct_drive" uv run pytest -s

    runner.test(hdl_toplevel="axis_loopback", test_module="test_coco_fora,")

    assert True
