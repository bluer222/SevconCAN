#!/usr/bin/env python3
"""Log Gen4 telemetry: heartbeat state, key measurements and EMCY faults.

Real hardware::

    python examples/async_log.py --channel can0 --node 0x20

Simulator (no hardware)::

    python examples/async_log.py --sim
"""
from __future__ import annotations

import argparse
import asyncio
import sys

sys.path.insert(0, "src")

from sevcon_can.device import AsyncSevconGen4
from sevcon_can.nmt import state_name
from sevcon_can.simulator import create_pair

MEASUREMENTS = [
    (0x5100, 1),  # battery voltage, V
    (0x5100, 2),  # battery current, A
    (0x5100, 3),  # capacitor voltage, V
    (0x5100, 4),  # heatsink temperature, DegC
    (0x6077, 0),  # torque actual, % of peak
    (0x6078, 0),  # current actual, A RMS
]


async def log_once(dev: AsyncSevconGen4) -> None:
    state = await dev.nmt_state(cached=False)
    print(f"NMT: {state_name(state if state is not None else -1)}")
    for index, sub in MEASUREMENTS:
        try:
            reading = await dev.read(index, sub)
        except Exception as e:
            print(f"  0x{index:04X}:{sub:02X} unreadable: {e}")
            continue
        unit = f" {reading.unit}" if reading.unit else ""
        print(f"  {reading.variable.path}: {reading.physical}{unit}")


async def watch_emcy(dev: AsyncSevconGen4) -> None:
    async for message in dev.emcy_notifications():
        print(f"EMCY: {message}")


async def real(channel: str, node: int) -> None:
    dev = AsyncSevconGen4.from_socketcan(channel, node)
    async with dev:
        await log_once(dev)
        await watch_emcy(dev)


async def sim() -> None:
    dev, simulator = create_pair()
    async with dev, simulator:
        simulator.inject_fault(0x45C3)  # low battery cut, for demonstration
        await log_once(dev)
        await asyncio.wait_for(watch_emcy(dev), timeout=2.0)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--channel", default="can0")
    ap.add_argument("--node", type=lambda s: int(s, 0), default=0x20)
    ap.add_argument("--sim", action="store_true", help="use the simulator")
    args = ap.parse_args()
    try:
        if args.sim:
            asyncio.run(sim())
        else:
            asyncio.run(real(args.channel, args.node))
    except KeyboardInterrupt:
        pass
    except asyncio.TimeoutError:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
