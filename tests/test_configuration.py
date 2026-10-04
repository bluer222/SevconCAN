import asyncio
import json
import sys
import unittest
from unittest.mock import AsyncMock

sys.path.insert(0, "src")

from sevcon_can import (AccessError, ConfigurationRestoreError,
                        ConfigurationSnapshot, export_configuration,
                        plan_configuration_restore, restore_configuration)
from sevcon_can.simulator import SIM_PASSWORD, create_pair


class TestConfiguration(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.dev, self.sim = create_pair(heartbeat_ms=0)
        await self.dev.start()
        await self.sim.start()
        await self.dev.login(SIM_PASSWORD)
        self.keys = [(0x2C01, 2), (0x2C01, 1)]

    async def asyncTearDown(self):
        await self.dev.close()
        await self.sim.close()

    async def test_snapshot_roundtrip_and_raw_restore(self):
        await self.dev.write(0x2C01, 2, 48.5)
        snapshot = await export_configuration(self.dev, self.keys)
        copied = ConfigurationSnapshot.from_json(snapshot.to_json())
        self.assertEqual(copied, snapshot)
        await self.dev.write(0x2C01, 2, 40)
        plan = await plan_configuration_restore(self.dev, copied, self.keys)
        self.assertEqual(len(plan), 1)
        self.assertEqual(await self.dev.read_physical(0x2C01, 2), 40)
        completed = await restore_configuration(self.dev, copied, self.keys)
        self.assertEqual(len(completed), 1)
        self.assertEqual(await self.dev.read_physical(0x2C01, 2), 48.5)

    async def test_identity_mismatch_prevents_any_write(self):
        snapshot = await export_configuration(self.dev, self.keys)
        snapshot.identity["revision"] += 1
        self.dev.write_raw = AsyncMock(wraps=self.dev.write_raw)
        with self.assertRaises(AccessError):
            await restore_configuration(self.dev, snapshot, self.keys)
        self.dev.write_raw.assert_not_called()

    async def test_complete_preflight_prevents_partial_write(self):
        snapshot = await export_configuration(self.dev, self.keys)
        await self.dev.write(0x2C01, 2, 40)
        snapshot.values["2C01:01"] = "00"
        self.dev.write_raw = AsyncMock(wraps=self.dev.write_raw)
        with self.assertRaises(ValueError):
            await restore_configuration(self.dev, snapshot, self.keys)
        self.dev.write_raw.assert_not_called()
        self.assertEqual(await self.dev.read_physical(0x2C01, 2), 40)

    async def test_failed_reads_are_explicit(self):
        from sevcon_can.exceptions import SdoAbortError
        self.dev.read_raw = AsyncMock(wraps=self.dev.read_raw)
        original = self.dev.read_raw._mock_wraps

        async def read(index, sub):
            if (index, sub) == self.keys[1]:
                raise SdoAbortError(0x08000000, index, sub)
            return await original(index, sub)

        self.dev.read_raw.side_effect = read
        snapshot = await export_configuration(self.dev, self.keys)
        self.assertIn("2C01:01", snapshot.errors)
        self.assertNotIn("2C01:01", snapshot.values)

    async def test_partial_failure_reports_verified_and_uncertain_parameter(self):
        snapshot = await export_configuration(self.dev, self.keys)
        await self.dev.write(0x2C01, 2, 40)
        await self.dev.write(0x2C01, 1, 40)
        original = self.dev.write_raw

        async def write(index, sub, data):
            if (index, sub) == self.keys[1]:
                raise AccessError("injected write failure")
            await original(index, sub, data)

        self.dev.write_raw = write
        with self.assertRaises(ConfigurationRestoreError) as caught:
            await restore_configuration(self.dev, snapshot, self.keys)
        self.assertEqual(caught.exception.failed, self.keys[1])
        self.assertEqual(len(caught.exception.completed), 1)
        self.assertEqual(caught.exception.completed[0].subindex, 2)

    async def test_requires_preop_and_excludes_communication_changes(self):
        snapshot = await export_configuration(self.dev, [(0x5900, 1)])
        with self.assertRaises(AccessError):
            await restore_configuration(self.dev, snapshot, [(0x5900, 1)])
        snapshot = await export_configuration(self.dev, self.keys)
        await self.dev.goto_operational()
        with self.assertRaises(AccessError):
            await restore_configuration(self.dev, snapshot, self.keys)

    def test_rejects_malformed_snapshot(self):
        for data in ({}, {"schema": "unknown"},
                     {"schema": "sevcon-can.snapshot.v1", "identity": {},
                      "values": {"2C01:02": "zz"}}):
            with self.assertRaises(ValueError):
                ConfigurationSnapshot.from_json(json.dumps(data))


if __name__ == "__main__":
    unittest.main()
