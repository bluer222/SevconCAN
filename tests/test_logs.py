import asyncio
import sys
import unittest

sys.path.insert(0, "src")

from sevcon_can.exceptions import AccessError
from sevcon_can.logs import (
    LogTimestamp, read_event_counters, read_fifo_log,
    read_operational_monitor, reset_diagnostic_log,
)
from sevcon_can.simulator import SIM_PASSWORD, create_pair


def fifo_fixture(sim, kind, count):
    """Independent scalar peer: selecting a row changes the output registers."""
    control, selector, output = ((0x4100, 0x4101, 0x4102) if kind == "system"
                                 else (0x4110, 0x4111, 0x4112))
    sim._store[(control, 2)] = count.to_bytes(2, "little")
    original = sim._server.read

    def read(index, sub):
        if index == output and 1 <= sub <= 6:
            position = int.from_bytes(sim._store[(selector, 0)], "little")
            values = (0x4000 + position, 7, 239, position & 0xFF, 0xBB, 0xCC)
            return sim.od.variable(index, sub).encode_raw(values[sub - 1])
        return original(index, sub)

    sim._server.read = read


class TestDiagnosticLogs(unittest.TestCase):
    def test_both_fifo_schemas_all_rows_and_pagination(self):
        async def go():
            dev, sim = create_pair()
            async with dev, sim:
                await dev.login(SIM_PASSWORD)
                fifo_fixture(sim, "system", 103)
                fifo_fixture(sim, "faults", 103)
                writes = []
                original_write = sim._server.write

                def record(index, sub, data):
                    writes.append((index, sub, bytes(data)))
                    original_write(index, sub, data)

                sim._server.write = record
                for kind, selector in (("system", 0x4101), ("faults", 0x4111)):
                    rows = await read_fifo_log(dev, kind)
                    self.assertEqual(len(rows), 103)
                    self.assertEqual((rows[-1].position, rows[-1].event_id), (102, 0x4066))
                    self.assertEqual(rows[-1].timestamp, LogTimestamp(7, 239))
                    self.assertEqual(rows[-1].timestamp.elapsed_seconds, 7 * 3600 + 239 * 15)
                    self.assertEqual(rows[-1].data, bytes((102, 0xBB, 0xCC)))
                    limited = await read_fifo_log(dev, kind, start=100, limit=2)
                    self.assertEqual([row.position for row in limited], [100, 101])
                    self.assertEqual(await read_fifo_log(dev, kind, limit=0), [])
                    self.assertEqual(writes[-1], (selector, 0, b"\x65\x00"))
                self.assertEqual({(i, s) for i, s, _ in writes}, {(0x4101, 0), (0x4111, 0)})
        asyncio.run(go())

    def test_shared_cursor_and_reset_are_serialized(self):
        async def go():
            dev, sim = create_pair()
            async with dev, sim:
                await dev.login(SIM_PASSWORD)
                fifo_fixture(sim, "system", 4)
                trace = []
                original_read, original_write = sim._server.read, sim._server.write

                def read(index, sub):
                    if index == 0x4102:
                        trace.append(("read", sub))
                    return original_read(index, sub)

                def write(index, sub, data):
                    trace.append(("reset" if index == 0x4100 else "select", sub))
                    original_write(index, sub, data)

                sim._server.read, sim._server.write = read, write
                first, second, _ = await asyncio.gather(
                    read_fifo_log(dev, "system", start=0, limit=2),
                    read_fifo_log(dev, "system", start=2, limit=2),
                    reset_diagnostic_log(dev, "system"))
                self.assertEqual([row.event_id for row in first], [0x4000, 0x4001])
                self.assertEqual([row.event_id for row in second], [0x4002, 0x4003])
                self.assertEqual(trace[-1], ("reset", 1))
        asyncio.run(go())

    def test_all_event_counters_preserve_raw_times_and_unused_entries(self):
        async def go():
            dev, sim = create_pair()
            async with dev, sim:
                for sub, raw in enumerate((0x4581, 7, 2, 8, 239, 65535), 1):
                    var = sim.od.variable(0x4201, sub)
                    sim._store[var.key] = var.encode_raw(raw)
                rows = await read_event_counters(dev)
                self.assertEqual(len(rows), 10)
                self.assertEqual(rows[0].event_id, 0x4581)
                self.assertEqual(rows[0].first, LogTimestamp(7, 2))
                self.assertEqual(rows[0].last, LogTimestamp(8, 239))
                self.assertEqual(rows[0].count, 65535)
                self.assertEqual(rows[-1].position, 9)
                self.assertEqual(rows[-1].count, 0)
                self.assertEqual([row.position for row in await read_event_counters(
                    dev, start=8, limit=2)], [8, 9])
                self.assertTrue(all(frame.data[0] == 0x40 for frame in dev.network.transport.sent))
        asyncio.run(go())

    def test_both_monitors_include_second_motor_scaling_sign_and_units(self):
        async def go():
            dev, sim = create_pair()
            async with dev, sim:
                for index in (0x4300, 0x4301):
                    for sub, raw in ((2, 12000), (8, -300), (9, 650), (12, -40)):
                        var = sim.od.variable(index, sub)
                        sim._store[var.key] = var.encode_raw(raw)
                    values = await read_operational_monitor(dev, sevcon=index == 0x4301)
                    self.assertEqual(len(values), 12)
                    self.assertEqual(values["Minimum Battery Voltage"].physical, 46.875)
                    self.assertEqual(values["Minimum Battery Voltage"].unit, "V")
                    self.assertEqual(values["Minimum Motor Current 2"].raw, -300)
                    self.assertEqual(values["Maximum Motor Current 2"].physical, 650)
                    self.assertEqual(values["Minimum Device Temperature"].physical, -40)
                self.assertTrue(all(frame.data[0] == 0x40 for frame in dev.network.transport.sent))
        asyncio.run(go())

    def test_explicit_resets_permissions_and_input_validation(self):
        async def go():
            dev, sim = create_pair()
            async with dev, sim:
                await dev.login(SIM_PASSWORD)
                for kind, index in (("system", 0x4100), ("faults", 0x4110),
                                    ("events", 0x4200), ("customer", 0x4300)):
                    await reset_diagnostic_log(dev, kind)
                    self.assertEqual(sim._store[(index, 1)], b"\x01")
                with self.assertRaises(AccessError):
                    await reset_diagnostic_log(dev, "sevcon")
                sim._store[(0x5000, 1)] = b"\x05"
                dev._access_level = None
                await reset_diagnostic_log(dev, "sevcon")
                self.assertEqual(sim._store[(0x4301, 1)], b"\x01")
                for kwargs in ({"start": -1}, {"limit": -1}, {"limit": 0.5}, {"start": 65536}):
                    with self.assertRaises(ValueError):
                        await read_fifo_log(dev, "system", **kwargs)
                with self.assertRaises(ValueError):
                    await read_event_counters(dev, start=11)
                with self.assertRaises(ValueError):
                    await read_fifo_log(dev, "system", start=1)
                with self.assertRaises(ValueError):
                    await read_fifo_log(dev, "unknown")
                with self.assertRaises(ValueError):
                    await reset_diagnostic_log(dev, "all")
        asyncio.run(go())


if __name__ == "__main__":
    unittest.main()
