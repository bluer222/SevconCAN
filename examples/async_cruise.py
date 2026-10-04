#!/usr/bin/env python3
"""Cruise control master: pedal stays analog, cruise demand goes over CAN.

Architecture (see docs; Gen4 has no internal max-select for traction):
  Gen4 -> master : pedal value 2620h (polled here; TPDO with --pedal tpdo)
  master -> Gen4 : RPDO1 = controlword 6040h + target torque 6071h
  brake sensor   : Raspberry Pi GPIO (or keyboard/--sim for bench)

Behaviour: 'c' engages at the current pedal demand; the master then holds
that torque. Pedal pressed past target + margin, brake pressed, or 'q'
disengages (demand follows the pedal on pedal-override, 0 on brake —
this setup uses no regenerative braking, so disengage always coasts).
The Gen4's RPDO timeout (5902h = Inhibit Drive) faults the drive if the
master stops transmitting.

Real hardware::

    python3 examples/async_cruise.py --channel can0 --node 0x20 \\
        --password 0x4BDF --brake gpio --brake-pin 17

Bench (simulator, keyboard brake)::

    python3 examples/async_cruise.py --sim --brake key
    # keys: c engage  + / - adjust  b brake pulse  q quit
"""
from __future__ import annotations

import argparse
import asyncio
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from sevcon_can.device import AsyncSevconGen4  # noqa: E402
from sevcon_can.network import Network  # noqa: E402
from sevcon_can.pdo import PdoConsumer, decode_pdo  # noqa: E402
from sevcon_can.simulator import SIM_PASSWORD, create_pair  # noqa: E402
from sevcon_can.transport import CanFrame  # noqa: E402

RPDO_COB_OFFSET = 0x200  # RPDO1 = 0x200 + node (standard predefined set)
LOOP_HZ = 20.0


# ---------------------------------------------------------------------------
# Pure cruise logic (hardware-free; unit-tested in tests/test_cruise.py).
# Demands are torque % of peak, >= 0 (no regen in this setup: never negative).
# ---------------------------------------------------------------------------

@dataclass
class CruiseController:
    engage_min: float = 5.0   # % demand below which engage is refused
    pedal_margin: float = 3.0  # pedal % above target that cancels cruise

    engaged: bool = False
    target: float = 0.0

    def engage(self, pedal: float) -> str:
        if pedal < self.engage_min:
            return f"refused: pedal {pedal:.1f}% below minimum {self.engage_min:.1f}%"
        self.engaged = True
        self.target = pedal
        return f"engaged at {self.target:.1f}%"

    def disengage(self) -> str:
        self.engaged = False
        self.target = 0.0
        return "disengaged"

    def adjust(self, delta: float) -> str:
        if not self.engaged:
            return "not engaged"
        self.target = min(100.0, max(0.0, self.target + delta))
        return f"target {self.target:.1f}%"

    def update(self, pedal: float, brake: bool) -> tuple[float, Optional[str]]:
        """One tick. Returns (demand %, event or None)."""
        pedal = max(0.0, pedal)
        if brake:
            if self.engaged:
                self.disengage()
                return 0.0, "brake: disengaged, coasting"
            return 0.0, None
        if not self.engaged:
            return pedal, None
        if pedal > self.target + self.pedal_margin:
            self.disengage()
            return pedal, f"pedal override ({pedal:.1f}%): disengaged"
        return self.target, None


# ---------------------------------------------------------------------------
# Brake inputs
# ---------------------------------------------------------------------------

class BrakeInput:
    def pressed(self) -> bool:
        raise NotImplementedError

    def close(self) -> None:
        pass


class OffBrake(BrakeInput):
    def pressed(self) -> bool:
        return False


class KeyBrake(BrakeInput):
    """Brake pulses delivered by the keyboard thread ('b' key)."""

    def __init__(self) -> None:
        self._events = 0

    def pulse(self) -> None:
        self._events += 1

    def pressed(self) -> bool:
        if self._events:
            self._events -= 1
            return True
        return False


class GpioBrake(BrakeInput):
    """Normally-open brake switch to GND (pressed = low) with pull-up.

    Needs gpiozero on the Pi (pip install gpiozero); pin uses BCM numbering.
    """

    def __init__(self, pin: int = 17) -> None:
        try:
            from gpiozero import Button  # type: ignore[import-not-found]
        except ImportError as e:
            raise RuntimeError(
                "gpiozero not installed; on the Pi run: pip install gpiozero "
                "(or use --brake key)") from e
        self._button = Button(pin, pull_up=True)

    def pressed(self) -> bool:
        return self._button.is_pressed

    def close(self) -> None:
        self._button.close()


# ---------------------------------------------------------------------------
# Demand outputs
# ---------------------------------------------------------------------------

class DemandOutput:
    async def send(self, controlword: int, torque_pct: float) -> None:
        raise NotImplementedError


class RpdoDemand(DemandOutput):
    """Cyclic RPDO1: controlword (16 bit) + target torque (16 bit, 0.1 %/unit)."""

    def __init__(self, network: Network, node_id: int) -> None:
        self.network = network
        self.cob_id = RPDO_COB_OFFSET + node_id

    async def send(self, controlword: int, torque_pct: float) -> None:
        raw = int(round(max(0.0, min(100.0, torque_pct)) * 10.0))
        payload = (int(controlword) & 0xFFFF).to_bytes(2, "little") \
            + raw.to_bytes(2, "little")
        await self.network.send(CanFrame(self.cob_id, payload))


class SdoDemand(DemandOutput):
    """Bench fallback (simulator): SDO writes to 6071h. Not for driving."""

    def __init__(self, dev: AsyncSevconGen4) -> None:
        self.dev = dev

    async def send(self, controlword: int, torque_pct: float) -> None:
        await self.dev.write(0x6040, 0, controlword)
        await self.dev.write(0x6071, 0, max(0.0, min(100.0, torque_pct)))


async def configure_rpdo(dev: AsyncSevconGen4, password: int) -> int:
    """Map RPDO1 to 6040h + 6071h and arm the RPDO timeout. Returns COB-ID."""
    await dev.login(password)
    await dev.goto_preop()  # 5902h is pre-op-only
    cob_id = RPDO_COB_OFFSET + dev.node_id
    await dev.write(0x1600, 0, 2)
    await dev.write(0x1600, 1, 0x60400010)
    await dev.write(0x1600, 2, 0x60710010)
    await dev.write(0x1400, 1, cob_id)
    await dev.write(0x1400, 2, 255)  # event-driven (we send cyclically)
    await dev.write(0x5902, 0, 2)  # RPDO timeout -> Inhibit Drive
    await dev.write(0x6060, 0, 4)  # torque mode (pre-op-only, so before op)
    await dev.goto_operational()
    await dev.enable_drive()
    return cob_id


async def read_pedal_sdo(dev: AsyncSevconGen4) -> float:
    """Pedal demand 0..100 % from throttle value 2620h (fraction 0..1)."""
    return float(await dev.read_physical(0x2620, 0)) * 100.0


def keyboard_thread(keys: List[str], brake: KeyBrake, stop: threading.Event,
                    controller: CruiseController, pedal_getter) -> None:
    print("keys: c engage | + / - adjust | b brake | q quit")
    while not stop.is_set():
        try:
            line = input().strip().lower()
        except EOFError:
            break
        if line == "q":
            stop.set()
        elif line == "c":
            print(controller.engage(pedal_getter()))
        elif line == "+":
            print(controller.adjust(1.0))
        elif line == "-":
            print(controller.adjust(-1.0))
        elif line == "b":
            brake.pulse()
            print("brake pulse")


async def main_async(args) -> int:
    controller = CruiseController()
    pedal = [0.0]

    if args.sim:
        dev, sim = create_pair(node_id=args.node)
        await dev.start()
        await sim.start()
        closer = sim.close
    else:
        dev = AsyncSevconGen4.from_socketcan(args.channel, args.node)
        await dev.start()
        closer = None
    try:
        if args.sim:
            demand: DemandOutput = SdoDemand(dev)
            await dev.login(SIM_PASSWORD)
            await dev.set_operation_mode("torque")
            await dev.enable_drive()
            print("sim: RPDO frames not consumed by the simulator; "
                  "using SDO writes (bench only)")
        else:
            cob = await configure_rpdo(dev, args.password)
            demand = RpdoDemand(dev.network, args.node)
            print(f"RPDO1 configured on COB-ID {cob:#05x}; timeout = Inhibit Drive")

        if args.brake == "gpio":
            brake: BrakeInput = GpioBrake(args.brake_pin)
        elif args.brake == "key":
            brake = KeyBrake()
        else:
            print("WARNING: no brake input (--brake off); pedal-override only")
            brake = OffBrake()

        stop = threading.Event()
        kb = threading.Thread(target=keyboard_thread,
                              args=(["c"], brake, stop, controller,
                                    lambda: pedal[0]), daemon=True)
        kb.start()

        period = 1.0 / LOOP_HZ
        while not stop.is_set():
            pedal[0] = await read_pedal_sdo(dev)
            torque, event = controller.update(pedal[0], brake.pressed())
            await demand.send(0x000F, torque)  # 0x0F = Enable operation
            state = "CRUISE" if controller.engaged else "pedal "
            print(f"\r[{state}] pedal {pedal[0]:5.1f}% -> demand {torque:5.1f}%   ",
                  end="", flush=True)
            if event:
                print(f"\n{event}")
            await asyncio.sleep(period)
        await demand.send(0x000F, 0.0)
        print("\nstopped, demand zeroed")
        return 0
    finally:
        brake.close()
        await dev.close()
        if closer is not None:
            await closer()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--channel", default="can0")
    ap.add_argument("--node", type=lambda s: int(s, 0), default=0x20)
    ap.add_argument("--password", type=lambda s: int(s, 0), default=SIM_PASSWORD)
    ap.add_argument("--sim", action="store_true")
    ap.add_argument("--brake", choices=["off", "key", "gpio"], default="key")
    ap.add_argument("--brake-pin", type=int, default=17, help="BCM pin for --brake gpio")
    args = ap.parse_args(argv)
    try:
        return asyncio.run(main_async(args))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
