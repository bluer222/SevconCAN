import asyncio
from copy import deepcopy
import sys
import unittest

sys.path.insert(0, "src")

from sevcon_can import datatypes as dt
from sevcon_can.discovery import DATA_TYPES
from sevcon_can.exceptions import ProtocolError, SdoAbortError
from sevcon_can.od import Variable
from sevcon_can.sdo import ABORT_NO_SUBINDEX
from sevcon_can.simulator import create_pair


class TestDiscovery(unittest.TestCase):
    def test_ignored_cursor_or_reserved_address_cannot_claim_complete_scan(self):
        async def go():
            dev, sim = create_pair()
            first = sim._dictionary_entries[0]
            sim._dictionary_entries = [first, first]
            async with dev, sim:
                with self.assertRaisesRegex(ProtocolError, "repeated dictionary address"):
                    await dev.discover_dictionary()
                metadata = sim._dictionary_metadata
                sim._dictionary_entries = [first]
                sim._dictionary_metadata = lambda sub: b"\x00\x00" if sub == 2 else metadata(sub)
                with self.assertRaisesRegex(ProtocolError, "0000:00"):
                    await dev.discover_dictionary()
        asyncio.run(go())

    def test_existing_address_with_different_metadata_is_not_a_match(self):
        async def go():
            dev, sim = create_pair()
            dev.od = deepcopy(sim.od)
            original = dev.od.variable((0x4641, 3))
            live = deepcopy(original)
            live.data_type = dt.UNSIGNED16
            live.access = "ro"
            live.low = 0
            live.high = 32767
            live.extra["OBJECT_VERSION"] = "99"
            sim._dictionary_entries = [live]
            async with dev, sim:
                report = await dev.discover_dictionary()
                self.assertEqual(report["missing_from_dictionary"], [])
                mismatch, = report["metadata_mismatches"]
                self.assertEqual(mismatch["index"], 0x4641)
                self.assertEqual(set(mismatch["differences"]),
                                 {"data_type", "access", "object_version", "low_limit_raw", "high_limit_raw"})
                self.assertTrue(report["absent_from_controller"])
                partial = await dev.discover_dictionary(limit=0)
                self.assertIsNone(partial["absent_from_controller"])
        asyncio.run(go())

    def test_internal_data_type_codes_are_not_cia_codes(self):
        self.assertEqual(DATA_TYPES[0], dt.BOOLEAN)
        self.assertEqual(DATA_TYPES[4], dt.INTEGER64)
        self.assertEqual(DATA_TYPES[8], dt.UNSIGNED64)
        self.assertEqual(DATA_TYPES[11], dt.OCTET_STRING)
        self.assertEqual(DATA_TYPES[12], dt.REAL32)
        self.assertEqual(DATA_TYPES[13], dt.REAL64)
        self.assertEqual(DATA_TYPES[15], dt.DOMAIN)
        self.assertNotIn(14, DATA_TYPES)

    def test_full_scan_missing_entry_raw_limits_and_only_cursor_writes(self):
        async def go():
            dev, sim = create_pair()
            dev.od = deepcopy(sim.od)
            unknown = Variable(0x7F00, 0, "Installed-only parameter", dt.INTEGER32,
                               low=-100, high=100)
            sim.od.add(unknown)
            sim._dictionary_entries = [sim.od.variable(0x1000), unknown]
            writes = []
            original = dev.write_raw

            async def record_write(index, subindex, data):
                writes.append((index, subindex, bytes(data)))
                await original(index, subindex, data)

            dev.write_raw = record_write
            async with dev, sim:
                before = dict(sim._store)
                report = await dev.discover_dictionary()
                self.assertEqual(report["entry_count"], 2)
                self.assertTrue(report["complete"])
                self.assertEqual([entry["index"] for entry in report["entries"]],
                                 [0x1000, 0x7F00])
                missing, = report["missing_from_dictionary"]
                self.assertEqual(missing["index"], 0x7F00)
                self.assertIsNone(missing["dictionary_path"])
                self.assertEqual(missing["internal_data_type"], 3)
                self.assertEqual(missing["data_type"], dt.INTEGER32)
                self.assertEqual(missing["low_limit_raw"], (-100) & 0xFFFFFFFF)
                self.assertEqual(sim._store, before)
                self.assertEqual(writes, [(0x5630, 1, b"\x00\x00"),
                                          (0x5630, 1, b"\x01\x00")])
        asyncio.run(go())

    def test_bounded_resume_empty_and_concurrent_iterators(self):
        async def go():
            dev, sim = create_pair()
            sim._dictionary_entries = sim._dictionary_entries[:3]
            async with dev, sim:
                partial = await dev.discover_dictionary(start=1, limit=1)
                self.assertEqual([e["table_index"] for e in partial["entries"]], [1])
                self.assertFalse(partial["complete"])
                self.assertEqual((await dev.discover_dictionary(limit=0))["entries"], [])

                async def collect(start):
                    return [entry.index async for entry in dev.iter_dictionary_entries(start=start)]

                first, second = await asyncio.gather(collect(0), collect(1))
                expected = [var.index for var in sim._dictionary_entries]
                self.assertEqual(first, expected)
                self.assertEqual(second, expected[1:])
                sim._dictionary_entries = []
                self.assertTrue((await dev.discover_dictionary())["complete"])
        asyncio.run(go())

    def test_unknown_codes_and_older_firmware_without_object_flags(self):
        async def go():
            dev, sim = create_pair()
            sim._dictionary_entries = sim._dictionary_entries[:1]
            metadata = sim._dictionary_metadata

            def older_metadata(subindex):
                if subindex == 5:
                    return b"\x7F"
                if subindex == 6:
                    return b"\xFF"
                if subindex == 11:
                    raise SdoAbortError(ABORT_NO_SUBINDEX, 0x5630, subindex)
                return metadata(subindex)

            sim._dictionary_metadata = older_metadata
            async with dev, sim:
                report = await dev.discover_dictionary()
                entry, = report["entries"]
                self.assertEqual(entry["internal_data_type"], 0x7F)
                self.assertEqual(entry["access_code"], 0xFF)
                self.assertIsNone(entry["data_type"])
                self.assertIsNone(entry["access"])
                self.assertIsNone(entry["object_flags"])
        asyncio.run(go())

    def test_bad_lengths_bounds_and_controller_errors_are_reported(self):
        async def go():
            dev, sim = create_pair()
            sim._dictionary_entries = sim._dictionary_entries[:1]
            async with dev, sim:
                for kwargs in ({"start": -1}, {"start": 0x10000}, {"start": 2},
                               {"limit": -1}, {"limit": 0x10000}, {"limit": 0.5}):
                    with self.assertRaises(ValueError):
                        await dev.discover_dictionary(**kwargs)
                metadata = sim._dictionary_metadata
                sim._dictionary_metadata = lambda sub: b"\x00" if sub == 2 else metadata(sub)
                with self.assertRaisesRegex(ProtocolError, "5630h:02 metadata must be 2 bytes"):
                    await dev.discover_dictionary()

                def abort(sub):
                    raise SdoAbortError(0x08000022, 0x5630, sub)

                sim._dictionary_metadata = abort
                with self.assertRaises(SdoAbortError) as caught:
                    await dev.discover_dictionary()
                self.assertEqual(caught.exception.code, 0x08000022)
        asyncio.run(go())


if __name__ == "__main__":
    unittest.main()
