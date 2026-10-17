import asyncio
import sys
import unittest
from unittest.mock import AsyncMock

sys.path.insert(0, "src")
from sevcon_can import TimeoutError
from sevcon_can.simulator import create_pair


class TestObjectTimeouts(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.dev, self.sim = create_pair(heartbeat_ms=0)
        await self.dev.start()
        await self.sim.start()

    async def asyncTearDown(self):
        await self.dev.close()
        await self.sim.close()

    async def test_per_transfer_timeout_does_not_leak_to_next_transfer(self):
        original = self.sim.network.send

        async def delayed(frame):
            await asyncio.sleep(0.03)
            await original(frame)

        self.sim.network.send = delayed
        self.dev.sdo.timeout = 0.005
        self.assertEqual(await self.dev.sdo.upload(0x5000, 1, timeout=0.2), b"\x00")
        self.assertEqual(self.dev.sdo.timeout, 0.005)
        with self.assertRaises(TimeoutError):
            await self.dev.sdo.upload(0x5000, 1)
        self.assertIsNone(self.dev.sdo._transfer_timeout)

    async def test_eds_domain_response_timeout_applies_to_raw_and_typed_access(self):
        self.dev.sdo.upload = AsyncMock(return_value=b"test")
        self.assertEqual((await self.dev.read(0x5600, 1)).raw, b"test")
        self.dev.sdo.upload.assert_awaited_once_with(0x5600, 1, timeout=20.0)
        self.dev.sdo.download = AsyncMock()
        await self.dev.write_raw(0x5600, 1, b"test")
        self.dev.sdo.download.assert_awaited_once_with(0x5600, 1, b"test", timeout=20.0)

    async def test_timeout_validation_precedes_traffic(self):
        self.dev.network.send = AsyncMock()
        for timeout in (0, -1, float("nan"), float("inf")):
            with self.assertRaises(ValueError):
                await self.dev.sdo.upload(0x5000, 1, timeout=timeout)
            with self.assertRaises(ValueError):
                await self.dev.sdo.download(0x5000, 3, b"\x00", timeout=timeout)
        self.dev.network.send.assert_not_called()
