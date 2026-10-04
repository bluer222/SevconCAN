#!/usr/bin/env python3
"""Drive a Gen4: log in, enable the DSP402 state machine, apply torque.

Real hardware (torque mode, 10 % of peak for 3 s, then stop)::

    python examples/async_control.py --channel can0 --node 0x20 --password 0x4BDF

Simulator (no hardware)::

    python examples/async_control.py --sim

CAUTION: on real hardware this energises the motor. Secure the vehicle
(wheels off the ground, direction neutral) before running.
"""
from __future__ import annotations

import argparse
import asyncio
import sys

sys.path.insert(0, "src")

from sevcon_can.device import AsyncSevconGen4
from sevcon_can.simulator import SIM_PASSWORD, create_pair


async def control(dev: AsyncSevconGen4, password: int, torque_percent: float,
                  hold_s: float) -> None:
    level = await dev.login(password)
    print(f"access level: {level}")
    print(f"mode: {await dev.set_operation_mode('torque')}")
    try:
        # Clear a previous demand before enabling the power stage.
        await dev.write(0x6071, 0, 0.0)
        info = await dev.enable_drive()
        print(f"drive: statusword 0x{info.raw:04X} {info.state}")
        # 6071h target torque, 0.1 % of peak per EDS scaling.
        await dev.write(0x6071, 0, torque_percent)
        print(f"target torque: {torque_percent} % of peak for {hold_s} s")
        await asyncio.sleep(hold_s)
    finally:
        # Shutdown is attempted even when the zero-demand write fails.
        # Shield a separate task so cancellation cannot skip these writes.
        async def stop():
            try:
                await dev.write(0x6071, 0, 0.0)
            finally:
                await dev.drive_command("Shutdown")
        cleanup = asyncio.create_task(stop())
        try:
            await asyncio.shield(cleanup)
        except asyncio.CancelledError:
            await cleanup
            raise
    info = await dev.status()
    print(f"drive: statusword 0x{info.raw:04X} {info.state}")


async def real(channel: str, node: int, password: int,
               torque_percent: float, hold_s: float) -> None:
    dev = AsyncSevconGen4.from_socketcan(channel, node)
    async with dev:
        await control(dev, password, torque_percent, hold_s)


async def sim(torque_percent: float, hold_s: float) -> None:
    dev, simulator = create_pair()
    async with dev, simulator:
        await control(dev, SIM_PASSWORD, torque_percent, hold_s)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--channel", default="can0")
    ap.add_argument("--node", type=lambda s: int(s, 0), default=0x20)
    ap.add_argument("--password", type=lambda s: int(s, 0), default=SIM_PASSWORD)
    ap.add_argument("--torque", type=float, default=10.0, help="percent of peak")
    ap.add_argument("--hold", type=float, default=3.0, help="seconds")
    ap.add_argument("--sim", action="store_true", help="use the simulator")
    args = ap.parse_args()
    if not args.sim:
        print("CAUTION: this energises the motor. Secure the vehicle first.")
    try:
        if args.sim:
            asyncio.run(sim(args.torque, min(args.hold, 1.0)))
        else:
            asyncio.run(real(args.channel, args.node, args.password,
                             args.torque, args.hold))
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
