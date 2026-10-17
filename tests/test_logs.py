import asyncio
from copy import deepcopy
import sys
import unittest

sys.path.insert(0, "src")

from sevcon_can import datatypes as dt
from sevcon_can.exceptions import AccessError, ProtocolError, SdoAbortError, TimeoutError
from sevcon_can.logs import (
    LogTimestamp, read_event_counters, read_fifo_log,
    read_operational_monitor, read_operational_monitor_domain,
    reset_all_diagnostic_logs, reset_diagnostic_log,
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
    def test_reset_all_polls_until_zero_and_propagates_abort_and_access_errors(self):
        async def go():
            dev, sim = create_pair()
            async with dev, sim:
                with self.assertRaises(AccessError):
                    await reset_all_diagnostic_logs(dev, poll_interval=0.001)
                self.assertEqual(sim._store[(0x4000, 1)], b"\0")
                sim._store[(0x5000, 1)] = b"\x05"
                dev._access_level = None
                trace = []
                original_read, original_write = sim._server.read, sim._server.write
                states = iter((b"\1", b"\1", b"\0"))

                def read(index, sub):
                    if (index, sub) == (0x4000, 1):
                        state = next(states)
                        trace.append(("read", state))
                        return state
                    return original_read(index, sub)

                def write(index, sub, data):
                    trace.append(("write", index, sub, bytes(data)))
                    return original_write(index, sub, data)

                sim._server.read, sim._server.write = read, write
                await reset_all_diagnostic_logs(dev, poll_interval=0.001)
                self.assertEqual(trace, [("write", 0x4000, 1, b"\1"),
                                         ("read", b"\1"), ("read", b"\1"), ("read", b"\0")])
                self.assertFalse(dev._diagnostic_log_lock.locked())

                def abort(index, sub):
                    if (index, sub) == (0x4000, 1):
                        raise SdoAbortError(0x06010000, index, sub)
                    return original_read(index, sub)

                sim._server.read = abort
                with self.assertRaises(SdoAbortError) as caught:
                    await reset_all_diagnostic_logs(dev, poll_interval=0.001)
                self.assertEqual(caught.exception.code, 0x06010000)
                self.assertFalse(dev._diagnostic_log_lock.locked())
                sim._server.read = original_read
                with self.assertRaisesRegex(TimeoutError, "reset flag"):
                    await reset_all_diagnostic_logs(dev, timeout=0.02, poll_interval=0.001)
                self.assertEqual(sim._store[(0x4000, 1)], b"\1")
                self.assertFalse(dev._diagnostic_log_lock.locked())
        asyncio.run(go())

    def test_reset_all_timeout_cancels_blocked_read_and_caller_cancellation_propagates(self):
        class BlockedPeer:
            def __init__(self):
                self._diagnostic_log_lock = asyncio.Lock()
                self.read_started = asyncio.Event()
                self.read_cancelled = asyncio.Event()
                self.writes = []

            async def write(self, index, sub, value):
                self.writes.append((index, sub, value))

            async def read(self, index, sub):
                self.read_started.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    self.read_cancelled.set()

        async def go():
            peer = BlockedPeer()
            with self.assertRaises(TimeoutError):
                await reset_all_diagnostic_logs(peer, timeout=0.01)
            self.assertEqual(peer.writes, [(0x4000, 1, True)])
            self.assertTrue(peer.read_cancelled.is_set())
            self.assertFalse(peer._diagnostic_log_lock.locked())
            peer = BlockedPeer()
            task = asyncio.create_task(reset_all_diagnostic_logs(peer))
            await peer.read_started.wait()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertTrue(peer.read_cancelled.is_set())
            self.assertFalse(peer._diagnostic_log_lock.locked())
            # The deadline also covers waiting for a concurrent cursor/reset
            # transaction, so a timed-out waiter cannot reset later.
            peer = BlockedPeer()
            await peer._diagnostic_log_lock.acquire()
            with self.assertRaises(TimeoutError):
                await reset_all_diagnostic_logs(peer, timeout=0.01)
            self.assertEqual(peer.writes, [])
            peer._diagnostic_log_lock.release()
            for value in (0, -1, float("inf"), float("nan"), True, "1", None):
                for name in ("timeout", "poll_interval"):
                    with self.assertRaises(ValueError):
                        await reset_all_diagnostic_logs(peer, **{name: value})
            self.assertEqual(peer.writes, [])
        asyncio.run(go())

    def test_monitor_domains_match_independent_physical_scalar_fixtures(self):
        # These payloads and scalar words were independently recorded from the
        # physical 0705 controller. They contain no DVT API length prefix.
        fixtures = (
            (False, "80012a041c0028040000e201ff7f008063fd811214004600",
             (384, 1066, 28, 1064, 0, 482, 32767, -32768, -669, 4737, 20, 70)),
            (True, "84012a04180026040000d501ff7f008079fe561215004100",
             (388, 1066, 24, 1062, 0, 469, 32767, -32768, -391, 4694, 21, 65)),
        )

        async def go():
            dev, sim = create_pair()
            async with dev, sim:
                sim._store[(0x1018, 1)] = bytes.fromhex("1e000000")
                sim._store[(0x1018, 2)] = bytes.fromhex("1b300507")
                for sevcon, payload_hex, scalar_words in fixtures:
                    index, subindex = (0x4301, 5) if sevcon else (0x4300, 4)
                    payload = bytes.fromhex(payload_hex)
                    sim._store[(0x5600, subindex)] = payload
                    for sub, raw in enumerate(scalar_words, 2):
                        sim._store[(index, sub)] = raw.to_bytes(2, "little", signed=True)
                    sent_before = len(dev.network.transport.sent)
                    report = await read_operational_monitor_domain(dev, sevcon=sevcon)
                    requests = [frame.data for frame in dev.network.transport.sent[sent_before:]
                                if frame.data[0] == 0x40]
                    self.assertEqual([tuple(request[1:4]) for request in requests],
                                     [(0x18, 0x10, 1), (0x18, 0x10, 2),
                                      (0x00, 0x56, subindex)])
                    self.assertEqual(report.raw, payload)
                    self.assertEqual(tuple(v.raw for v in report.values.values()), scalar_words)
                    scalar = await read_operational_monitor(dev, sevcon=sevcon)
                    self.assertEqual(report.values, scalar)
                    self.assertEqual(len(report.scaling_conflicts), 4)
                    conflict = report.scaling_conflicts["Maximum Battery Voltage"]
                    self.assertEqual(conflict["dictionary_scale"], 1 / 256)
                    self.assertEqual(conflict["dictionary_physical"], 1066 / 256)
                    self.assertEqual(conflict["dvt_scale"], 1 / 16)
                    self.assertEqual(conflict["dvt_physical"], 1066 / 16)
                    self.assertTrue(conflict["dictionary_signed"])
                    self.assertFalse(conflict["dvt_signed"])
        asyncio.run(go())

    def test_monitor_domain_rejects_unknown_layout_and_uses_custom_dictionary_scale(self):
        async def go():
            dev, sim = create_pair()
            dev.od = deepcopy(dev.od)
            async with dev, sim:
                sim._store[(0x1018, 1)] = bytes.fromhex("1e000000")
                sim._store[(0x1018, 2)] = bytes.fromhex("1b300507")
                for length in (0, 2, 23, 25, 28, 36):
                    sim._store[(0x5600, 4)] = b"\0" * length
                    with self.assertRaisesRegex(ProtocolError, "24 bytes"):
                        await read_operational_monitor_domain(dev)
                sim._store[(0x5600, 4)] = b"\xff\xff" * 12
                dev.variable(0x4300, 2).scaling = 1 / 16
                report = await read_operational_monitor_domain(dev)
                conflict = report.scaling_conflicts["Minimum Battery Voltage"]
                # Equal scales still leave DVT's unsigned interpretation in
                # conflict with a custom EDS that retains INTEGER16.
                self.assertEqual(conflict["dictionary_scale"], 1 / 16)
                self.assertEqual(conflict["dictionary_physical"], -1 / 16)
                self.assertEqual(conflict["dvt_physical"], 65535 / 16)
                for vendor, product in ((0x20, 0x0705301B), (0x1E, 0x0402301B)):
                    sim._store[(0x1018, 1)] = vendor.to_bytes(4, "little")
                    sim._store[(0x1018, 2)] = product.to_bytes(4, "little")
                    sent_before = len(dev.network.transport.sent)
                    with self.assertRaisesRegex(ProtocolError, "0705"):
                        await read_operational_monitor_domain(dev)
                    requests = [frame.data for frame in dev.network.transport.sent[sent_before:]
                                if frame.data[0] == 0x40]
                    self.assertTrue(all(request[1:3] == b"\x18\x10" for request in requests))
                dev.variable(0x4300, 2).data_type = dt.UNSIGNED16
                sent_before = len(dev.network.transport.sent)
                with self.assertRaisesRegex(ProtocolError, "INTEGER16"):
                    await read_operational_monitor_domain(dev)
                self.assertEqual(len(dev.network.transport.sent), sent_before)
        asyncio.run(go())

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
                for sub, raw in enumerate((0xC581, 7, 2, 8, 239, 65535), 1):
                    var = sim.od.variable(0x4201, sub)
                    sim._store[var.key] = var.encode_raw(raw)
                rows = await read_event_counters(dev)
                self.assertEqual(len(rows), 10)
                self.assertEqual(rows[0].event_id, 0x4581)
                self.assertTrue(rows[0].saved)
                self.assertEqual(rows[0].raw_event_id, 0xC581)
                self.assertEqual(rows[0].first, LogTimestamp(7, 2))
                self.assertEqual(rows[0].last, LogTimestamp(8, 239))
                self.assertEqual(rows[0].count, 65535)
                self.assertEqual(rows[-1].position, 9)
                self.assertEqual(rows[-1].count, 0)
                self.assertFalse(rows[-1].saved)
                self.assertEqual(rows[-1].raw_event_id, 0)
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
