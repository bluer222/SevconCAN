#!/usr/bin/env python3
"""Generate importable lookup dictionaries, optionally using live TPDO mappings.

python3 tools/generate_dictionaries.py
python3 tools/generate_dictionaries.py --eds controller.eds --out controller_objects.py
python3 tools/generate_dictionaries.py --channel can0 --node 0x20 --out controller_objects.py
"""
import argparse
import asyncio
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from sevcon_can.catalog import write_catalog
from sevcon_can.device import AsyncSevconGen4
from sevcon_can.od import gen4_od, load_eds


async def live_configurations(channel, node, od):
    async with AsyncSevconGen4.from_socketcan(channel, node, od=od) as dev:
        return {number: await dev.read_pdo_configuration(True, number)
                for number in range(1, 6) if 0x1800 + number - 1 in od.objects}


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--eds", help="EDS/DCF file; defaults to bundled Gen4 EDS")
    parser.add_argument("--out", default=str(Path(__file__).resolve().parents[1] /
                                           "src/sevcon_can/object_catalog.py"))
    parser.add_argument("--channel", help="SocketCAN interface for live TPDO mappings")
    parser.add_argument("--node", type=lambda value: int(value, 0), default=0x20)
    args = parser.parse_args()
    if not 1 <= args.node <= 127:
        parser.error("node must be 1..127")
    od = load_eds(args.eds) if args.eds else gen4_od(include_supplemental=True)
    configs = asyncio.run(live_configurations(args.channel, args.node, od)) if args.channel else None
    catalog = write_catalog(args.out, od, tpdo_configurations=configs)
    print(f"Wrote {args.out}: {len(catalog['ALL_OBJECTS'])} objects, "
          f"{len(catalog['STREAM_OBJECTS'])} configured stream objects")


if __name__ == "__main__":
    main()
