"""SDO reads and TPDO stream. Usage: python3 examples/async_stream_and_request.py can0 0x20"""
import asyncio
import sys

sys.path.insert(0, "src")

from sevcon_can.device import AsyncSevconGen4


async def main() -> None:
    channel = sys.argv[1]
    node = int(sys.argv[2], 0)
    async with AsyncSevconGen4.from_socketcan(channel, node) as dev:
        # SDO: request values and wait for their replies.
        print("battery V:", await dev.read_physical(0x5100, 1))
        print("battery A:", await dev.read_physical(0x5100, 2))
        print("heatsink C:", await dev.read_physical(0x5100, 4))

        # TPDO: read the live mapping once, then listen for streamed values.
        # TPDO1 must already be enabled; synchronous PDOs need an existing SYNC source.
        consumer = await dev.subscribe_tpdo_live(1)
        async for values in consumer.updates():
            for value in values:
                var = dev.variable(value.index, value.subindex)
                print("stream:", var.path, var.to_physical(value.raw), var.units or "")


asyncio.run(main())
