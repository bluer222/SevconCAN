import asyncio
import sys
import unittest

sys.path.insert(0, "src")

from sevcon_can.simulator import SIM_PASSWORD, create_pair


def run(coro):
    return asyncio.run(coro)


class TestLogin(unittest.TestCase):
    def test_login_logout(self):
        async def go():
            dev, sim = create_pair()
            async with dev, sim:
                self.assertEqual(await dev.login(SIM_PASSWORD), 4)
                self.assertEqual(await dev.access_level(), 4)
                self.assertEqual(await dev.logout(), 0)
        run(go())

    def test_logout_abort_is_raised_while_still_logged_in(self):
        async def go():
            from sevcon_can.exceptions import SdoAbortError
            dev, sim = create_pair()
            write = sim._sdo_write

            def keep_level(index, subindex, data):
                if (index, subindex) == (0x5000, 2) and not any(data):
                    sim._store[(0x5310, 0)] = b"\x09\x00"
                    raise SdoAbortError(0x08000000, index, subindex)
                write(index, subindex, data)

            sim._sdo_write = keep_level
            async with dev, sim:
                await dev.login(SIM_PASSWORD)
                with self.assertRaises(SdoAbortError):
                    await dev.logout()
                self.assertEqual(await dev.access_level(), 4)
        run(go())

    def test_fresh_access_level_failure_cannot_reuse_cached_login(self):
        async def go():
            from unittest.mock import AsyncMock
            from sevcon_can.exceptions import TransportError
            dev, sim = create_pair()
            async with dev, sim:
                await dev.login(SIM_PASSWORD)
                dev.read_physical = AsyncMock(side_effect=TransportError("read unavailable"))
                with self.assertRaises(TransportError):
                    await dev.access_level(cached=False)
                self.assertIsNone(dev._access_level)
        run(go())

    def test_logout_verification_read_failure_is_not_success(self):
        async def go():
            from unittest.mock import AsyncMock
            from sevcon_can.exceptions import SdoAbortError, TransportError
            for password_aborts in (False, True):
                dev, sim = create_pair()
                async with dev, sim:
                    await dev.login(SIM_PASSWORD)
                    original = dev.write_raw

                    async def write(index, sub, data):
                        try:
                            await original(index, sub, data)
                        except SdoAbortError:
                            if password_aborts:
                                raise

                    dev.write_raw = write
                    dev.read_physical = AsyncMock(side_effect=TransportError("read unavailable"))
                    with self.assertRaises(TransportError):
                        await dev.logout()
                    self.assertIsNone(dev._access_level)
        run(go())

    def test_logout_does_not_suppress_unrelated_password_abort(self):
        async def go():
            from unittest.mock import AsyncMock
            from sevcon_can.exceptions import SdoAbortError
            dev, sim = create_pair()
            async with dev, sim:
                dev.write_raw = AsyncMock(side_effect=[None, SdoAbortError(0x08000000, 0x5000, 2, 8)])
                dev.access_level = AsyncMock(return_value=0)
                with self.assertRaises(SdoAbortError):
                    await dev.logout()
                dev.access_level.assert_not_awaited()
        run(go())

    def test_logout_without_password_abort_requires_level_zero(self):
        async def go():
            from unittest.mock import AsyncMock
            from sevcon_can.exceptions import StateError
            dev, sim = create_pair()
            async with dev, sim:
                await dev.login(SIM_PASSWORD)
                dev.write_raw = AsyncMock()
                with self.assertRaises(StateError):
                    await dev.logout()
                dev.access_level = AsyncMock(return_value=0)
                self.assertEqual(await dev.logout(), 0)
        run(go())

    def test_bad_password_aborts(self):
        async def go():
            from sevcon_can.exceptions import SdoAbortError
            dev, sim = create_pair()
            async with dev, sim:
                with self.assertRaises(SdoAbortError):
                    await dev.login(0x1234)
        run(go())


class TestTypedAccess(unittest.TestCase):
    def test_raw_hex_strings_bypass_scaling(self):
        async def go():
            dev, sim = create_pair()
            async with dev, sim:
                await dev.login(SIM_PASSWORD)
                written = await dev.write(0x6071, 0, "0x10")
                self.assertEqual(written.raw, 16)
                self.assertEqual((await dev.read(0x6071, 0)).raw, 16)
                written = await dev.write(0x6071, 0, "16")
                self.assertEqual(written.raw, 160)
                self.assertEqual((await dev.read(0x6071, 0)).raw, 160)
        run(go())

    def test_binary_hex_and_bytes_roundtrip(self):
        async def go():
            dev, sim = create_pair()
            async with dev, sim:
                await dev.login(SIM_PASSWORD)
                for value in ("hex:00 ff 10", b"\x00\xff\x10"):
                    await dev.write(0x4617, 1, value)
                    self.assertEqual((await dev.read(0x4617, 1)).raw, b"\x00\xff\x10")
                with self.assertRaises(ValueError):
                    await dev.write(0x4617, 1, "not binary hex")
        run(go())

    def test_labels_obey_same_ranges_as_numbers(self):
        async def go():
            from unittest.mock import AsyncMock
            dev, sim = create_pair()
            async with dev, sim:
                await dev.login(SIM_PASSWORD)
                dev.write_raw = AsyncMock(wraps=dev.write_raw)
                for value in (5, "voltage"):
                    with self.assertRaises(ValueError):
                        await dev.write(0x6060, 0, value)
                dev.write_raw.assert_not_called()
        run(go())

    def test_read_scaling_and_units(self):
        async def go():
            dev, sim = create_pair()
            async with dev, sim:
                reading = await dev.read(0x5100, 1)
                self.assertEqual(reading.unit, "V")
                self.assertEqual(reading.physical, reading.raw * 0.0625)
                by_name = await dev.read("Device Measurements.Battery Voltage")
                self.assertEqual(by_name.raw, reading.raw)
        run(go())

    def test_write_physical_and_label(self):
        async def go():
            dev, sim = create_pair()
            async with dev, sim:
                await dev.login(SIM_PASSWORD)
                await dev.goto_preop(verify=False)
                # Physical write: 48.5 V through the 0.0625 scale.
                await dev.write(0x2C01, 2, 48.5)
                back = await dev.read(0x2C01, 2)
                self.assertEqual(back.raw, round(48.5 / 0.0625))
                # Label write on the login level object is read-only; use mode.
                await dev.write(0x6060, 0, "torque")
                self.assertEqual(await dev.read_physical(0x6060, 0), 4)
        run(go())

    def test_access_errors_without_bus_traffic(self):
        async def go():
            from sevcon_can.exceptions import AccessError
            dev, sim = create_pair()
            async with dev, sim:
                before = len(sim.network.transport.log) if hasattr(
                    sim.network.transport, "log") else 0
                with self.assertRaises(AccessError):  # write a read-only object
                    await dev.write(0x5100, 1, 10)
                with self.assertRaises(AccessError):  # read a write-only object
                    await dev.read(0x5000, 2)
                with self.assertRaises(ValueError):  # above HighLimit 32767
                    await dev.write(0x1017, 0, 99999)
        run(go())

    def test_preop_only_enforced(self):
        async def go():
            from sevcon_can.exceptions import AccessError
            dev, sim = create_pair()
            async with dev, sim:
                await dev.login(SIM_PASSWORD)
                await dev.goto_operational()
                with self.assertRaises(AccessError):
                    await dev.write(0x6060, 0, 3)
                await dev.goto_preop()
                await dev.write(0x6060, 0, 3)
        run(go())


class TestNmtState(unittest.TestCase):
    def test_bootup_and_addressed_resets_invalidate_login_cache(self):
        from sevcon_can.nmt import NMT_RESET_COMM, NMT_RESET_NODE

        async def go():
            dev, sim = create_pair()
            async with dev, sim:
                await dev.login(SIM_PASSWORD)
                await dev.nmt.reset_node(dev.node_id)
                await dev.heartbeat.wait_state(dev.node_id, 0, timeout=1)
                self.assertIsNone(dev._access_level)
                self.assertEqual(await dev.access_level(), 0)
                for command, target in ((NMT_RESET_NODE, dev.node_id), (NMT_RESET_COMM, 0)):
                    dev._access_level = 4
                    dev._nmt_state = 5
                    await dev.nmt_command(command, target)
                    self.assertIsNone(dev._access_level)
                    self.assertIsNone(dev._nmt_state)
                dev._access_level = 4
                await dev.nmt_command(NMT_RESET_NODE, dev.node_id + 1)
                self.assertEqual(dev._access_level, 4)
        run(go())

    def test_state_transition_verification_rejects_stale_matching_cache(self):
        from sevcon_can.exceptions import StateError

        async def go():
            dev, sim = create_pair(heartbeat_ms=0)
            async with dev, sim:
                await dev.login(SIM_PASSWORD)
                original_write = sim._server.write
                sim._server.write = lambda i, s, d: (
                    None if (i, s) == (0x2800, 0) else original_write(i, s, d))
                original_wait = dev.wait_nmt_state

                async def bounded_wait(state):
                    return await original_wait(state, timeout=0.05)

                dev.wait_nmt_state = bounded_wait
                for actual, stale, transition in ((5, 127, dev.goto_preop),
                                                   (127, 5, dev.goto_operational)):
                    sim._set_state(actual)
                    dev._nmt_state = stale
                    with self.assertRaises(StateError):
                        await transition()
        run(go())

    def test_preop_operational_cycle(self):
        async def go():
            dev, sim = create_pair()
            async with dev, sim:
                await dev.login(SIM_PASSWORD)  # 2800h needs level 4
                self.assertEqual(await dev.goto_operational(), 5)
                self.assertEqual(await dev.nmt_state(), 5)
                self.assertEqual(await dev.goto_preop(), 127)
                self.assertEqual(await dev.nmt_state(), 127)
        run(go())

    def test_heartbeat_seen(self):
        async def go():
            dev, sim = create_pair()
            async with dev, sim:
                event = await dev.heartbeat.wait_state(dev.node_id, 127, timeout=5.0)
                self.assertEqual(event.state, 127)
        run(go())


class TestStoreRestore(unittest.TestCase):
    def test_store_restore_signatures(self):
        async def go():
            dev, sim = create_pair()
            async with dev, sim:
                await dev.login(SIM_PASSWORD)
                await dev.store()
                await dev.restore()
        run(go())


class TestDrive(unittest.TestCase):
    def test_enable_and_fault(self):
        async def go():
            from sevcon_can.exceptions import StateError
            dev, sim = create_pair()
            async with dev, sim:
                info = await dev.enable_drive()
                self.assertTrue(info.operation_enabled)
                self.assertEqual(info.state, "Operation enabled")
                sim.latch_fault(0x4981)
                info = await dev.status()
                self.assertTrue(info.fault)
                with self.assertRaises(StateError):
                    await dev.enable_drive()
                info = await dev.fault_reset()
                self.assertFalse(info.fault)
                info = await dev.enable_drive()
                self.assertTrue(info.operation_enabled)
        run(go())


class TestFaultsLive(unittest.TestCase):
    def test_live_configured_emcy_subscription_and_close(self):
        from sevcon_can.exceptions import TransportError

        async def go():
            dev, sim = create_pair()
            async with dev, sim:
                await dev.login(SIM_PASSWORD)
                await dev.write(0x1014, 0, 0x321)
                consumer = await dev.subscribe_emcy_live()
                sim.inject_fault(0x4581)
                notification = await asyncio.wait_for(
                    consumer.notifications().__anext__(), 1)
                self.assertEqual((notification.node_id, notification.fault_id),
                                 (dev.node_id, 0x4581))
                self.assertTrue(dev.emcy.queue.empty())
                stream = consumer.notifications()
                pending = asyncio.create_task(stream.__anext__())
                await asyncio.sleep(0)
                await dev.close()
                with self.assertRaises(TransportError):
                    await asyncio.wait_for(pending, 0.2)
                with self.assertRaises(TransportError):
                    dev.subscribe_emcy(0x321)
        run(go())

    def test_live_disabled_emcy_keeps_faults_and_rejects_subscription(self):
        async def go():
            dev, sim = create_pair()
            async with dev, sim:
                self.assertEqual((await dev.read(0x1014, 0)).raw, 0x80 + dev.node_id)
                # A firmware dictionary may allow the CiA 301 disable bit.
                await dev.write_raw(0x1014, 0, (0x800000A0).to_bytes(4, "little"))
                with self.assertRaisesRegex(ValueError, "disabled"):
                    await dev.subscribe_emcy_live()
                sim.inject_fault(0x4581)
                self.assertEqual(sim.sent_emcy, [])
                self.assertEqual([fault.fault_id for fault in await dev.active_faults()],
                                 [0x4581])
        run(go())

    def test_fault_list_and_text(self):
        async def go():
            dev, sim = create_pair()
            async with dev, sim:
                sim.inject_fault(0x4581)
                sim.inject_fault(0x45C3)
                await asyncio.sleep(0.1)
                active = await dev.active_faults()
                self.assertEqual([f.fault_id for f in active], [0x4581, 0x45C3])
                self.assertIn("Throttle", active[0].info.message)
                from sevcon_can.faults import describe_fault
                text = await describe_fault(dev, 0x4581)
                self.assertIn("Throttle", text)
        run(go())

    def test_emcy_notification(self):
        async def go():
            dev, sim = create_pair()
            async with dev, sim:
                sim.inject_fault(0x45C3)
                message = await asyncio.wait_for(
                    dev.emcy_notifications().__anext__(), 5.0)
                self.assertEqual(message.fault_id, 0x45C3)
                self.assertIn("Battery", message.fault_text)
        run(go())


class TestPdoLive(unittest.TestCase):
    def test_tpdo_remap_while_operational_keeps_state_and_disable_sequence(self):
        async def go():
            dev, sim = create_pair()
            async with dev, sim:
                await dev.login(SIM_PASSWORD)
                await dev.goto_operational()
                writes = []
                original = dev.write_raw

                async def record_write(index, sub, data):
                    writes.append((index, sub, int.from_bytes(data, "little")))
                    await original(index, sub, data)

                dev.write_raw = record_write
                config = await dev.configure_pdo(
                    True, 1, [(0x6041, 0, 16)],
                    cob_id=0x180 + dev.node_id, transmission_type=255)
                self.assertEqual(await dev.nmt_state(cached=False), 5)
                self.assertTrue(config["enabled"])
                self.assertEqual(config["mapping"], [(0x6041, 0, 16)])
                self.assertEqual(writes[0][:2], (0x1800, 1))
                self.assertTrue(writes[0][2] & 0x80000000)
                self.assertEqual(writes[1], (0x1A00, 0, 0))
                self.assertEqual(writes[-1], (0x1800, 1, 0x180 + dev.node_id))
                consumer = await dev.subscribe_tpdo_live(1)
                await sim.send_tpdo(1)
                values = await asyncio.wait_for(consumer.updates().__anext__(), 1)
                self.assertEqual([(v.index, v.subindex) for v in values], [(0x6041, 0)])
        run(go())

    def test_operational_tpdo_remap_respects_auth_and_eds_preop_rules(self):
        from unittest.mock import AsyncMock
        from sevcon_can.exceptions import AccessError

        async def go():
            dev, sim = create_pair()
            async with dev, sim:
                await dev.login(SIM_PASSWORD)
                await dev.goto_operational()
                await dev.logout()
                original = dev.write_raw
                dev.write_raw = AsyncMock()
                with self.assertRaises(AccessError):
                    await dev.configure_pdo(True, 1, [(0x6041, 0, 16)])
                dev.write_raw.assert_not_awaited()
                dev.write_raw = original
                await dev.login(SIM_PASSWORD)
                dev.write_raw = AsyncMock()
                # A matching firmware EDS can still require pre-op for a mapping entry.
                dev.variable(0x1A00, 1).preop_only = True
                with self.assertRaises(AccessError):
                    await dev.configure_pdo(True, 1, [(0x6041, 0, 16)])
                dev.write_raw.assert_not_awaited()
        run(go())

    def test_operational_tpdo_remap_propagates_firmware_rejection(self):
        from sevcon_can.exceptions import SdoAbortError

        async def go():
            dev, sim = create_pair()
            async with dev, sim:
                await dev.login(SIM_PASSWORD)
                await dev.goto_operational()
                original = sim._sdo_write

                def reject_mapping(index, sub, data):
                    if index == 0x1A00:
                        raise SdoAbortError(0x08000022, index, sub)
                    return original(index, sub, data)

                sim._server.write = reject_mapping
                with self.assertRaises(SdoAbortError) as caught:
                    await dev.configure_pdo(True, 1, [(0x6041, 0, 16)])
                self.assertEqual(caught.exception.code, 0x08000022)
                self.assertTrue((await dev.read(0x1800, 1)).raw & 0x80000000)
                self.assertEqual(await dev.nmt_state(cached=False), 5)
        run(go())

    def test_rpdo_remap_still_requires_preop(self):
        from unittest.mock import AsyncMock
        from sevcon_can.exceptions import StateError

        async def go():
            dev, sim = create_pair()
            async with dev, sim:
                await dev.login(SIM_PASSWORD)
                await dev.goto_operational()
                dev.write_raw = AsyncMock()
                with self.assertRaises(StateError):
                    await dev.configure_pdo(False, 1, [(0x6071, 0, 16)])
                dev.write_raw.assert_not_awaited()
        run(go())

    def test_tpdo_subscribe_and_decode(self):
        async def go():
            dev, sim = create_pair()
            async with dev, sim:
                await dev.login(SIM_PASSWORD)
                mapping = await dev.read_pdo_mapping(True, 1)
                await dev.configure_pdo(True, 1, mapping, cob_id=0x180 + dev.node_id)
                await dev.goto_operational()
                consumer = await dev.subscribe_tpdo_live(1)
                await sim.send_tpdo(1)
                update = await asyncio.wait_for(consumer.updates().__anext__(), 5.0)
                self.assertEqual(
                    [(v.index, v.subindex) for v in update],
                    [(0x2021, 1), (0x2021, 3), (0x2021, 5)])
                mapping = await dev.read_pdo_mapping(True, 1)
                self.assertEqual(mapping[0], (0x2021, 1, 16))
        run(go())


class TestRpdoIntegration(unittest.TestCase):
    def test_synchronous_rpdo_waits_for_sync(self):
        async def go():
            dev, sim = create_pair(heartbeat_ms=0)
            async with dev, sim:
                await dev.login(SIM_PASSWORD)
                await dev.configure_pdo(False, 1, [(0x6071, 0, 16)],
                                        cob_id=0x200 + dev.node_id, transmission_type=1)
                await dev.goto_operational()
                before = (await dev.read(0x6071, 0)).raw
                await dev.send_rpdo(1, [(0x6071, 0, -125)])
                self.assertEqual((await dev.read(0x6071, 0)).raw, before)
                await dev.send_sync()
                self.assertEqual((await dev.read(0x6071, 0)).raw, -125)
        run(go())

    def test_async_rpdo_ignores_preop_short_remote_and_wrong_frame_format(self):
        from sevcon_can.transport import CanFrame
        async def go():
            dev, sim = create_pair(heartbeat_ms=0)
            async with dev, sim:
                await dev.login(SIM_PASSWORD)
                cob = 0x200 + dev.node_id
                await dev.configure_pdo(False, 1, [(0x6071, 0, 16)],
                                        cob_id=cob, transmission_type=255)
                before = (await dev.read(0x6071, 0)).raw
                await dev.send_rpdo(1, [(0x6071, 0, 125)])
                self.assertEqual((await dev.read(0x6071, 0)).raw, before)
                await dev.goto_operational()
                for frame in (CanFrame(cob, b"\x7d"),
                              CanFrame(cob, b"", is_remote=True),
                              CanFrame(cob, b"\x7d\x00", is_extended=True)):
                    await dev.network.send(frame)
                    self.assertEqual((await dev.read(0x6071, 0)).raw, before)
                await dev.send_rpdo(1, [(0x6071, 0, 125)])
                self.assertEqual((await dev.read(0x6071, 0)).raw, 125)
        run(go())

    def test_pending_rpdo_is_discarded_on_leaving_operational(self):
        async def go():
            dev, sim = create_pair(heartbeat_ms=0)
            async with dev, sim:
                await dev.login(SIM_PASSWORD)
                await dev.configure_pdo(False, 1, [(0x6071, 0, 16)],
                                        cob_id=0x200 + dev.node_id, transmission_type=1)
                await dev.goto_operational()
                before = (await dev.read(0x6071, 0)).raw
                await dev.send_rpdo(1, [(0x6071, 0, 125)])
                await dev.goto_preop()
                await dev.goto_operational()
                await dev.send_sync()
                self.assertEqual((await dev.read(0x6071, 0)).raw, before)
        run(go())

    def test_sync_uses_live_standard_and_extended_cob_id(self):
        async def go():
            dev, sim = create_pair(heartbeat_ms=0)
            async with dev, sim:
                await dev.login(SIM_PASSWORD)
                await dev.configure_pdo(False, 1, [(0x6071, 0, 16)],
                                        cob_id=0x200 + dev.node_id, transmission_type=1)
                for cob_id, raw in ((0x40000123, 125), (0x20012345, -125)):
                    await dev.write(0x1005, 0, cob_id)
                    await dev.goto_operational()
                    await dev.send_rpdo(1, [(0x6071, 0, raw)])
                    await dev.send_sync(counter=1)
                    self.assertEqual((await dev.read(0x6071, 0)).raw, raw)
                    await dev.goto_preop()
                with self.assertRaises(ValueError):
                    await dev.send_sync(cob_id=0x80000080)
        run(go())

    def test_extended_rpdo_uses_live_configured_identifier(self):
        async def go():
            dev, sim = create_pair(heartbeat_ms=0)
            async with dev, sim:
                await dev.login(SIM_PASSWORD)
                dev.variable(0x1400, 1).high = 0xFFFFFFFF
                await dev.configure_pdo(False, 1, [(0x6071, 0, 16)],
                                        cob_id=0x20000000 | 0x1234567, transmission_type=255)
                await dev.goto_operational()
                await dev.send_rpdo(1, [(0x6071, 0, -125)])
                self.assertEqual((await dev.read(0x6071, 0)).raw, -125)
        run(go())

    def test_fifth_tpdo_extended_custom_id_uses_live_configuration(self):
        async def go():
            dev, sim = create_pair(heartbeat_ms=0)
            async with dev, sim:
                await dev.login(SIM_PASSWORD)
                # This is a synthetic dictionary that permits extended IDs.
                # The bundled EDS limits exclude the flag on disabled PDOs.
                dev.variable(0x1804, 1).high = 0xFFFFFFFF
                cob = 0x20000000 | 0x1234567
                await dev.configure_pdo(True, 5, [(0x6041, 0, 16)], cob_id=cob)
                consumer = await dev.subscribe_tpdo_live(5)
                await dev.goto_operational()
                frame = await sim.send_tpdo(5)
                self.assertTrue(frame.is_extended)
                self.assertEqual(frame.arbitration_id, 0x1234567)
                update = await asyncio.wait_for(consumer.updates().__anext__(), 0.3)
                self.assertEqual(update[0].raw, (await dev.read(0x6041, 0)).raw)
        run(go())


class TestMeasurements(unittest.TestCase):
    def test_measurements_dict(self):
        async def go():
            dev, sim = create_pair()
            async with dev, sim:
                values = await dev.measurements()
                self.assertTrue(any("Battery Voltage" in k for k in values))
        run(go())

    def test_identity(self):
        async def go():
            dev, sim = create_pair()
            async with dev, sim:
                identity = await dev.read_identity()
                self.assertEqual(identity["vendor"], 0x1E)
                # EDS default of 1018h:2 (the file-level ProductNumber is 0x07055037).
                self.assertEqual(identity["product_code"], 0x0101503C)
        run(go())


class TestControllerRegressions(unittest.TestCase):
    def test_custom_standard_and_extended_sdo_channels(self):
        from sevcon_can.device import AsyncSevconGen4
        from sevcon_can.network import Network
        from sevcon_can.sdo import SdoServer
        from sevcon_can.transport import VirtualBus
        async def go():
            for rx, tx in ((0x621, 0x5A1), (0x20012345, 0x20012346)):
                bus = VirtualBus()
                server_network = Network(bus.endpoint())
                dev = AsyncSevconGen4.from_transport(bus.endpoint(), 0x20,
                    sdo_rx_cob_id=rx, sdo_tx_cob_id=tx)
                server = SdoServer(server_network, 0x20,
                    lambda i, s: b"\x1e\x00\x00\x00", lambda i, s, d: None,
                    rx_cob_id=rx, tx_cob_id=tx)
                await server_network.start()
                server.start()
                try:
                    async with dev:
                        self.assertEqual((await dev.read(0x1018, 1)).raw, 0x1E)
                finally:
                    await server.close()
                    await server_network.close()
        run(go())

    def test_nmt_polling_without_heartbeats(self):
        from unittest.mock import AsyncMock
        async def go():
            dev, sim = create_pair()
            async with dev, sim:
                dev.nmt_state = AsyncMock(side_effect=[5, 5, 127])
                self.assertEqual(await dev.wait_nmt_state(127, timeout=0.3), 127)
                self.assertEqual(dev.nmt_state.await_count, 3)
        run(go())

    def test_nmt_timeout_bounds_unresponsive_poll(self):
        from sevcon_can.exceptions import StateError
        async def go():
            dev, sim = create_pair()
            async with dev, sim:
                async def stall(cached=True):
                    await asyncio.sleep(5)
                dev.nmt_state = stall
                with self.assertRaises(StateError):
                    await asyncio.wait_for(dev.wait_nmt_state(5, timeout=0.05), 0.3)
        run(go())

    def test_quick_stop_not_operation_enabled(self):
        from sevcon_can.drive import describe_status
        self.assertFalse(describe_status(0x0007).operation_enabled)

    def test_repeated_fault_reset_pulses_clear_bit(self):
        async def go():
            dev, sim = create_pair()
            async with dev, sim:
                for _ in range(2):
                    sim.latch_fault(0x4981)
                    self.assertFalse((await dev.fault_reset()).fault)
                    self.assertEqual(int((await dev.read(0x6040, 0)).raw) & 0x80, 0)
                    self.assertTrue((await dev.enable_drive()).operation_enabled)
        run(go())

    def test_enable_waits_for_state_not_unrelated_status_bit(self):
        from unittest.mock import AsyncMock
        from sevcon_can.drive import describe_status
        async def go():
            dev, sim = create_pair()
            async with dev, sim:
                dev.status = AsyncMock(side_effect=[describe_status(v) for v in
                    (0x40, 0x240, 0x21, 0x221, 0x23, 0x223, 0x27)])
                dev.drive_command = AsyncMock()
                self.assertTrue((await dev.enable_drive(timeout=1)).operation_enabled)
                self.assertEqual([c.args[0] for c in dev.drive_command.await_args_list],
                                 ["Shutdown", "Switch on", "Enable operation"])
        run(go())

    def test_failed_pdo_remap_stays_disabled(self):
        from unittest.mock import AsyncMock
        async def go():
            dev, sim = create_pair()
            async with dev, sim:
                await dev.login(SIM_PASSWORD)
                mapping = await dev.read_pdo_mapping(True, 1)
                original = dev.write_raw
                calls = 0
                async def failing_write(index, sub, data):
                    nonlocal calls
                    calls += 1
                    if calls == 3:
                        raise RuntimeError("interrupted remap")
                    await original(index, sub, data)
                dev.write_raw = failing_write
                with self.assertRaises(RuntimeError):
                    await dev.configure_pdo(True, 1, mapping, cob_id=0x180 + dev.node_id)
                self.assertTrue((await dev.read(0x1800, 1)).raw & 0x80000000)
                self.assertEqual((await dev.read(0x1A00, 0)).raw, 0)
        run(go())

    def test_invalid_pdo_mapping_never_writes(self):
        from unittest.mock import AsyncMock
        async def go():
            dev, sim = create_pair()
            async with dev, sim:
                await dev.login(SIM_PASSWORD)
                dev.write_raw = AsyncMock()
                with self.assertRaises(ValueError):
                    await dev.configure_pdo(True, 1, [(0x6041, 0, 32)], cob_id=0x180+dev.node_id)
                dev.write_raw.assert_not_awaited()
        run(go())

    def test_live_pdo_disabled_and_invalid_numbers(self):
        from sevcon_can.exceptions import StateError
        async def go():
            dev, sim = create_pair()
            async with dev, sim:
                with self.assertRaises(StateError):
                    await dev.subscribe_tpdo_live(1)
                with self.assertRaises(StateError):
                    await dev.send_rpdo(1, [])
                for number in (0, 6, -1):
                    with self.assertRaises(ValueError):
                        await dev.read_pdo_mapping(True, number)
        run(go())

    def test_identity_reports_header_default_discrepancy(self):
        async def go():
            dev, sim = create_pair()
            async with dev, sim:
                report = await dev.validate_identity()
                self.assertFalse(report["matches"])
                self.assertIn("product_code", report["mismatches"])
                self.assertEqual(report["identity"]["product_code"],
                                 report["object_defaults"]["product_code"])
                encoder = await dev.encoder_configuration()
                self.assertTrue(any("Sin" in name for name in encoder))
        run(go())

    def test_control_cleanup_on_cancel_and_failed_zero_write(self):
        from unittest.mock import AsyncMock
        from examples.async_control import control
        from sevcon_can.drive import describe_status
        async def go():
            dev = AsyncMock()
            dev.status.return_value = describe_status(0x21)
            dev.enable_drive.return_value = describe_status(0x27)
            dev.write.side_effect = [None, None, RuntimeError("bus failure")]
            task = asyncio.create_task(control(dev, 123, 10, 20))
            while dev.write.await_count < 2:
                await asyncio.sleep(0)
            task.cancel()
            with self.assertRaises(RuntimeError):
                await task
            self.assertEqual(dev.write.await_args_list[-1].args, (0x6071, 0, 0.0))
            dev.drive_command.assert_awaited_once_with("Shutdown")
        run(go())


if __name__ == "__main__":
    unittest.main()
