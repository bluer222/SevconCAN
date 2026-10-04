#!/usr/bin/env python3
"""Receive streamed TPDO values while requesting a temperature through SDO.

    python examples/async_stream_and_request.py --sim --seconds 3
    python examples/async_stream_and_request.py --channel can0 --node 0x20

Hardware mode only reads and listens. It uses the existing TPDO mapping and
does not enable PDOs, change NMT state, send SYNC or command the motor.
The simulator alone is configured to stream battery voltage and current.
"""
from __future__ import annotations

import argparse
import asyncio
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from sevcon_can.device import AsyncSevconGen4
from sevcon_can.od import load_eds
from sevcon_can.simulator import SIM_PASSWORD, create_pair


async def read_requested(dev: AsyncSevconGen4) -> None:
    """Each read sends an SDO request and waits for the controller's reply."""
    while True:
        reading = await dev.read(0x5100, 4)  # Device Measurements.Heatsink Temperature
        print(f"REQUEST SDO: {reading.variable.path} = "
              f"{reading.physical} {reading.unit or ''}", flush=True)
        await asyncio.sleep(1)


async def read_streamed(dev: AsyncSevconGen4, consumer) -> None:
    """Wait for TPDO frames. This loop sends no requests."""
    async for values in consumer.updates():
        fields = []
        for value in values:
            var = dev.variable(value.index, value.subindex)
            fields.append(f"{var.path} = {var.to_physical(value.raw)} {var.units or ''}")
        print("STREAM TPDO: " + "; ".join(fields), flush=True)


async def show_both(dev: AsyncSevconGen4, tpdo: int, seconds: float) -> None:
    # These initial SDO reads discover the identifier and mapping on this device.
    config = await dev.read_pdo_configuration(True, tpdo)
    consumer = None
    tasks = []
    try:
        if config["enabled"] and config["mapping"]:
            print(f"TPDO{tpdo}: COB-ID 0x{config['cob_id']:08X}, "
                  f"transmission type {config['transmission_type']}")
            if config["transmission_type"] <= 240:
                print("Synchronous TPDO: an existing SYNC source is required.")
            consumer = dev.subscribe_tpdo(tpdo, config["cob_id"], config["mapping"])
            tasks.append(asyncio.create_task(read_streamed(dev, consumer)))
        else:
            print(f"TPDO{tpdo} is disabled or unmapped; only SDO reads will appear. "
                  "Use --sim to demonstrate both methods.")
        tasks.append(asyncio.create_task(read_requested(dev)))
        # Propagate any reader failure; cancellation closes both readers on timeout.
        await asyncio.wait_for(asyncio.gather(*tasks), timeout=seconds)
    except asyncio.TimeoutError:
        pass
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if consumer is not None:
            consumer.close()


async def simulated_stream(simulator, tpdo: int) -> None:
    # The simulator has no automatic TPDO timer, so emit frames explicitly.
    while True:
        await simulator.send_tpdo(tpdo)
        await asyncio.sleep(0.25)


async def run(args) -> None:
    od = load_eds(args.eds) if args.eds else None
    if not args.sim:
        async with AsyncSevconGen4.from_socketcan(args.channel, args.node, od=od) as dev:
            await show_both(dev, args.tpdo, args.seconds)
        return

    dev, simulator = create_pair(node_id=args.node, **({"od": od} if od else {}))
    async with dev, simulator:
        await dev.login(SIM_PASSWORD)
        await dev.configure_pdo(
            True, args.tpdo, [(0x5100, 1, 16), (0x5100, 2, 16)],
            cob_id=0x180 + args.node, transmission_type=255,
        )
        await dev.goto_operational()
        producer = asyncio.create_task(simulated_stream(simulator, args.tpdo))
        try:
            await show_both(dev, args.tpdo, args.seconds)
        finally:
            producer.cancel()
            await asyncio.gather(producer, return_exceptions=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--sim", action="store_true")
    parser.add_argument("--channel", default="can0", help="SocketCAN adapter interface")
    parser.add_argument("--node", type=lambda value: int(value, 0), default=0x20)
    parser.add_argument("--tpdo", type=int, choices=range(1, 6), default=1)
    parser.add_argument("--eds", help="EDS matching the controller firmware")
    parser.add_argument("--seconds", type=float, default=5)
    args = parser.parse_args()
    if not 1 <= args.node <= 127 or not 0 < args.seconds < float("inf"):
        parser.error("node must be 1..127 and seconds must be finite and positive")
    try:
        asyncio.run(run(args))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
