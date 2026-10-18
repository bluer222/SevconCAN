import asyncio
import sys
import unittest
from unittest.mock import AsyncMock

sys.path.insert(0, "src")

from sevcon_can.exceptions import AccessError, ProtocolError, SdoAbortError, StateError
from sevcon_can.pdo import decode_pdo
from sevcon_can.simulator import SIM_PASSWORD, create_pair


class TestPdoServices(unittest.TestCase):
    def test_tpdo_timing_round_trip_stays_disabled_until_complete(self):
        async def go():
            dev, sim = create_pair()
            async with dev, sim:
                await dev.login(SIM_PASSWORD)
                original = dev.write_raw
                timing_writes = []

                async def check_write(index, sub, data):
                    if index == 0x1800 and sub in (3, 5):
                        self.assertTrue((await dev.read(0x1800, 1)).raw & 0x80000000)
                        timing_writes.append((sub, int.from_bytes(data, "little")))
                    await original(index, sub, data)

                dev.write_raw = check_write
                for enabled in (True, False):
                    config = await dev.configure_pdo(
                        True, 1, [(0x6041, 0, 16)], cob_id=0x180 + dev.node_id,
                        transmission_type=255, inhibit_time=20, event_timer=250,
                        enabled=enabled)
                    self.assertEqual(config["enabled"], enabled)
                    self.assertEqual(config["inhibit_time"], 20)
                    self.assertEqual(config["event_timer"], 250)
                self.assertEqual(timing_writes, [(3, 20), (5, 250)] * 2)
                config = await dev.configure_pdo(True, 1, [(0x6041, 0, 16)])
                self.assertEqual(config["inhibit_time"], 20)
                self.assertEqual(config["event_timer"], 250)
                self.assertEqual(len(timing_writes), 4)
        asyncio.run(go())

    def test_timing_rejections_preflight_before_any_write(self):
        async def go():
            dev, sim = create_pair()
            async with dev, sim:
                await dev.login(SIM_PASSWORD)
                dev.write_raw = AsyncMock()
                for field in ("inhibit_time", "event_timer"):
                    for value, error in ((-1, ValueError), (0x8000, ValueError),
                                         (1.5, TypeError), ("10", TypeError), (True, TypeError)):
                        with self.subTest(field=field, value=value), self.assertRaises(error):
                            await dev.configure_pdo(True, 1, [(0x6041, 0, 16)], **{field: value})
                    with self.assertRaisesRegex(ValueError, "only supported for TPDOs"):
                        await dev.configure_pdo(False, 1, [(0x6071, 0, 16)], **{field: 0})
                # Wire-width validation still runs before disabling when EDS limits are absent.
                dev.variable(0x1800, 5).high = None
                with self.assertRaises(ValueError):
                    await dev.configure_pdo(True, 1, [(0x6041, 0, 16)], event_timer=0x10000)
                dev.variable(0x1800, 5).access = "ro"
                with self.assertRaises(AccessError):
                    await dev.configure_pdo(True, 1, [(0x6041, 0, 16)], event_timer=10)
                dev.write_raw.assert_not_awaited()
        asyncio.run(go())

    def test_timing_write_failure_leaves_tpdo_disabled(self):
        async def go():
            dev, sim = create_pair()
            async with dev, sim:
                await dev.login(SIM_PASSWORD)
                original = sim._sdo_write

                def reject_timer(index, sub, data):
                    if (index, sub) == (0x1800, 5):
                        raise SdoAbortError(0x08000022, index, sub)
                    return original(index, sub, data)

                sim._server.write = reject_timer
                with self.assertRaises(SdoAbortError):
                    await dev.configure_pdo(
                        True, 1, [(0x6041, 0, 16)], inhibit_time=20, event_timer=250)
                self.assertFalse((await dev.read_pdo_configuration(True, 1))["enabled"])
                self.assertEqual((await dev.read(0x1800, 3)).raw, 20)
        asyncio.run(go())

    def test_rtr_types_and_live_request_payload_length(self):
        async def go():
            dev, sim = create_pair()
            async with dev, sim:
                await dev.login(SIM_PASSWORD)
                for transmission in (252, 253):
                    config = await dev.configure_pdo(
                        True, 1, [(0x6041, 0, 16), (0x5100, 4, 8)],
                        cob_id=0x180 + dev.node_id, transmission_type=transmission)
                    self.assertEqual(config["transmission_type"], transmission)
                    await dev.request_tpdo(1)
                    request = dev.network.transport.sent[-1]
                    self.assertEqual(request.arbitration_id, 0x180 + dev.node_id)
                    self.assertTrue(request.is_remote)
                    self.assertEqual(request.data, b"")
                    self.assertEqual(request.dlc, 3)
                await dev.request_tpdo(1, cob_id=0x20012345, mapping=[(0x6041, 0, 16)])
                self.assertTrue(dev.network.transport.sent[-1].is_extended)
                self.assertEqual(dev.network.transport.sent[-1].arbitration_id, 0x12345)
        asyncio.run(go())

    def test_rtr_rejects_disabled_forbidden_and_invalid_mapping(self):
        async def go():
            dev, sim = create_pair()
            async with dev, sim:
                for cob_id, message in ((0x800001A0, "disabled"),
                                         (0x400001A0, "does not allow RTR")):
                    with self.assertRaisesRegex(StateError, message):
                        await dev.request_tpdo(1, cob_id=cob_id, mapping=[(0x6041, 0, 16)])
                with self.assertRaises(ProtocolError):
                    await dev.request_tpdo(1, cob_id=0x1A0, mapping=[(0x6041, 0, 65)])
                self.assertEqual(dev.network.transport.sent, [])
        asyncio.run(go())

    def test_dummy_rpdo_padding_is_ignored_and_following_value_is_applied(self):
        async def go():
            dev, sim = create_pair()
            async with dev, sim:
                self.assertTrue(dev.od.dummy_usage[1])
                self.assertTrue(dev.od.dummy_usage[5])
                self.assertFalse(dev.od.dummy_usage[2])
                await dev.login(SIM_PASSWORD)
                mapping = [(5, 0, 8), (0x6040, 0, 16)]
                await dev.configure_pdo(False, 1, mapping, cob_id=0x200 + dev.node_id,
                                        transmission_type=255)
                await dev.goto_operational()
                await dev.send_rpdo(1, [(5, 0, 0xA5), (0x6040, 0, 6)])
                self.assertEqual((await dev.status()).state, "Ready to switch on")
                self.assertNotIn((5, 0), sim._store)
        asyncio.run(go())

    def test_invalid_dummy_mapping_and_rpdo_rtr_type_reject_before_writes(self):
        async def go():
            dev, sim = create_pair()
            async with dev, sim:
                await dev.login(SIM_PASSWORD)
                original = dev.write_raw
                dev.write_raw = AsyncMock()
                for mapping in ([(2, 0, 8)], [(5, 1, 8)], [(5, 0, 16)]):
                    with self.assertRaises(ValueError):
                        await dev.configure_pdo(False, 1, mapping)
                with self.assertRaises(ValueError):
                    await dev.configure_pdo(True, 1, [(5, 0, 8)])
                for transmission in (252, 253):
                    with self.assertRaises(ValueError):
                        await dev.configure_pdo(False, 1, [(0x6040, 0, 16)],
                                                transmission_type=transmission)
                dev.write_raw.assert_not_awaited()
                dev.write_raw = original
                with self.assertRaises(ProtocolError):
                    decode_pdo(b"\x00\x00", [(5, 0, 16)], dev.od)
        asyncio.run(go())


if __name__ == "__main__":
    unittest.main()
