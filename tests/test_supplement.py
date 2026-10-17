import sys
import unittest
from dataclasses import asdict
from pathlib import Path
from unittest.mock import AsyncMock

sys.path.insert(0, "src")
sys.path.insert(0, "tools")

from sevcon_can import AccessError, ObjectDictionary, Record, Variable, gen4_od, load_eds
from sevcon_can import datatypes as dt
from sevcon_can.simulator import create_pair, SIM_PASSWORD
import generate_supplement


class TestSupplement(unittest.TestCase):
    def test_source_definitions_preserved_without_vehicle_values(self):
        base = gen4_od(include_supplemental=False)
        expanded = gen4_od(include_supplemental=True)
        source = load_eds(generate_supplement.SOURCE)
        original = {var.key: var for var in base.variables()}
        extras = [var for var in expanded.variables() if var.key not in original]
        self.assertEqual(len(extras), 89)
        for var in expanded.variables():
            if var.key in original:
                self.assertEqual(asdict(var), asdict(original[var.key]), var.key)
        for var in extras:
            expected = source.variable(var.key)
            for name in ("data_type", "access", "low", "high", "scaling", "units",
                         "preop_only", "access_level", "pdo_mappable", "default_text"):
                self.assertEqual(getattr(var, name), getattr(expected, name), (var.key, name))
            self.assertIsNone(var.value_text)
            self.assertEqual(var.extra["SUPPLEMENTAL_SOURCE"], generate_supplement.SOURCE.name)
            self.assertEqual(var.extra["FIRMWARE_DEPENDENT"], "TRUE")
        self.assertNotIn(0x291F, base)
        self.assertNotIn(0x291F, load_eds(Path("specs/od/Gen4.eds")))

    def test_supplement_is_fresh(self):
        self.assertEqual(generate_supplement.OUT.read_text(encoding="latin-1"),
                         generate_supplement.render())

    def test_search_reaches_bitfields_labels_and_addresses(self):
        od = gen4_od(include_supplemental=True)
        keys = {var.key for var in od.search("creep")}
        self.assertIn((0x291F, 0), keys)
        self.assertIn((0x2911, 1), keys)
        self.assertIn((0x291F, 0), {var.key for var in od.search("291f:00")})
        self.assertIn((0x5000, 1), {var.key for var in gen4_od().search("Engineering")})

    def test_signed_hex_defaults_are_decoded_as_wire_values(self):
        for sub in (3, 7):
            var = gen4_od().variable((0x2870, sub))
            self.assertEqual(var.default(), -100)
            self.assertEqual(var.encode_raw(var.default()), b"\x9c\xff")

    def test_full_decimal_subindices_and_subentry_tags(self):
        var = Variable(0x2345, 255, "Last entry", dt.UNSIGNED8,
                       parent_name="Test", tag="LAST_ENTRY")
        od = ObjectDictionary()
        od.add(Record(0x2345, "Test", subs={255: var}))
        for key in ("2345h,255", "0x2345:255", "2345subFF", "LAST_ENTRY"):
            self.assertIs(od.variable(key), var)

    def test_target_motor_temperature_values_fit_signed_eds_limits(self):
        # Captured in SevCANWiz/sevcon-values2.json on product0705301B rev10026.
        for subindex, wire in ((3, b"\x78\x00"), (4, b"\x64\x00"), (5, b"\x00\x00")):
            var = gen4_od().variable((0x4621, subindex))
            self.assertEqual(var.raw_limits(), (-32768, 32767))
            var.check_range(var.decode_raw(wire))
            self.assertEqual((var.low, var.high), (-32768, 32768))
            with self.assertRaises(ValueError):
                var.check_range(32768)
        signed = Variable(0x2345, 0, "Hex signed", dt.INTEGER16, low=0x8000, high=0xFFFF)
        self.assertEqual(signed.raw_limits(), (-32768, -1))
        signed.check_range(-1)
        with self.assertRaises(ValueError):
            signed.check_range(0)


class TestSupplementAccess(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.dev, self.sim = create_pair(heartbeat_ms=0, od=gen4_od(include_supplemental=True))
        await self.dev.start()
        await self.sim.start()

    async def asyncTearDown(self):
        await self.dev.close()
        await self.sim.close()

    async def test_creep_torque_ratio_and_permissions(self):
        self.assertEqual(await self.dev.read_physical("Creep Torque"), 0)
        with self.assertRaises(AccessError):
            await self.dev.write("Creep Torque", None, 0.1)
        await self.dev.login(SIM_PASSWORD)
        written = await self.dev.write("Creep Torque", None, 0.1)
        self.assertEqual(written.raw, 100)
        self.assertEqual(await self.dev.read_raw(0x291F, 0), b"\x64\x00")
        self.assertAlmostEqual(await self.dev.read_physical("CREEP_TORQUE"), 0.1)
        for value in (-0.001, 0.501):
            with self.assertRaises(ValueError):
                await self.dev.write("Creep Torque", None, value)

    async def test_preop_restriction_rejects_stopped_and_unknown_states(self):
        await self.dev.login(SIM_PASSWORD)
        var = self.dev.variable((0x607E, 0))
        self.dev.write_raw = AsyncMock()
        for state in (0, 4, 5, None):
            self.dev.nmt_state = AsyncMock(return_value=state)
            with self.assertRaises(AccessError):
                await self.dev._check_writable(var, 0)
        self.dev.write_raw.assert_not_called()
        self.dev.nmt_state = AsyncMock(return_value=127)
        await self.dev._check_writable(var, 0)
        self.dev.nmt_state.assert_awaited_with(cached=False)
