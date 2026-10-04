"""Malformed-peer and lifecycle checks beyond simulator happy paths."""
import asyncio
import sys
import unittest

sys.path.insert(0, "src")

from sevcon_can.exceptions import ProtocolError, TransportError
from sevcon_can.network import FrameQueue, Network
from sevcon_can.pdo import (
    COB_ID_EXTENDED_BIT, PdoConsumer, cob_id_can_id, default_cob_id,
    decode_pdo, encode_mapping_word, encode_pdo,
)
from sevcon_can.nmt import decode_heartbeat, HeartbeatMonitor
from sevcon_can.emcy import decode_emcy
from sevcon_can.sdo import ABORT_COMMAND, ABORT_SEQUENCE, SdoClient, SdoServer
from sevcon_can.transport import CanFrame, VirtualBus
from test_sdo import SdoHarness


class TestPdoValidation(unittest.TestCase):
    def test_only_four_predefined_pdos(self):
        self.assertEqual(default_cob_id(True, 3, 32), 0x4A0)
        for number in (-1, 4, 5):
            with self.assertRaises(ValueError):
                default_cob_id(True, number, 32)

    def test_mapping_capacity_and_value_ranges(self):
        for mapping in ([(0x2000, 0, 0)], [(0x2000, 0, 65)], [(0x2000, 0, 32)] * 3):
            with self.assertRaises(ProtocolError):
                decode_pdo(bytes(8), mapping)
            with self.assertRaises(ProtocolError):
                encode_pdo([(i, s, 0) for i, s, _ in mapping], mapping)
        for raw in (256, -129):
            with self.assertRaises(ValueError):
                encode_pdo([(0x2000, 0, raw)], [(0x2000, 0, 8)])
        self.assertEqual(encode_pdo([(0x2000, 0, -128)], [(0x2000, 0, 8)]), b"\x80")
        with self.assertRaises(ValueError):
            encode_mapping_word(0x10000, 0, 8)

    def test_cob_id_validation(self):
        self.assertEqual(cob_id_can_id(COB_ID_EXTENDED_BIT | 0x12345), 0x12345)
        with self.assertRaises(ValueError):
            cob_id_can_id(0x12345)

    def test_extended_pdo_subscription_and_remote_rejection(self):
        net = Network(VirtualBus().endpoint())
        consumer = PdoConsumer(net, COB_ID_EXTENDED_BIT | 0x12345, [(0x2000, 0, 8)])
        net.dispatch(CanFrame(0x12345, b"\x05", is_extended=True, is_remote=True))
        self.assertTrue(consumer.queue.empty())
        net.dispatch(CanFrame(0x12345, b"\x05", is_extended=True))
        self.assertEqual(consumer.queue.get_nowait()[0].raw, 5)
        consumer.close()
        self.assertIsNone(consumer.queue.get_nowait())
        net.dispatch(CanFrame(0x12345, b"\x06", is_extended=True))
        self.assertTrue(consumer.queue.empty())

    def test_standard_and_extended_identifier_are_distinct(self):
        net = Network(VirtualBus().endpoint())
        standard, extended = [], []
        net.subscribe(0x120, standard.append)
        net.subscribe(0x120, extended.append, extended=True)
        net.dispatch(CanFrame(0x120, is_extended=True))
        self.assertEqual(len(standard), 0)
        self.assertEqual(len(extended), 1)

    def test_heartbeat_and_emcy_require_data_frames(self):
        for flags in ({"is_extended": True}, {"is_remote": True}, {"is_error": True}):
            with self.assertRaises(ValueError):
                decode_heartbeat(CanFrame(0x701, b"\x05", **flags))
            with self.assertRaises(ValueError):
                decode_emcy(CanFrame(0x81, bytes(8), **flags))
        with self.assertRaises(ValueError):
            decode_heartbeat(CanFrame(0x701, b"\x85"))
        monitor = HeartbeatMonitor(Network(VirtualBus().endpoint()))
        with self.assertRaises(ValueError):
            monitor.watch(0)

    def test_heartbeat_waiters_receive_fresh_broadcasts(self):
        async def go():
            net = Network(VirtualBus().endpoint())
            monitor = HeartbeatMonitor(net)
            monitor.watch(1)
            net.dispatch(CanFrame(0x701, b"\x05"))
            net.dispatch(CanFrame(0x701, b"\x7f"))
            tasks = [asyncio.create_task(monitor.wait_state(1, 5)) for _ in range(2)]
            await asyncio.sleep(0)
            self.assertTrue(all(not task.done() for task in tasks))
            net.dispatch(CanFrame(0x701, b"\x05"))
            events = await asyncio.wait_for(asyncio.gather(*tasks), 0.2)
            self.assertEqual([event.state for event in events], [5, 5])
            monitor.close()
            await net.close()
        asyncio.run(go())


class TestSdoFaults(unittest.TestCase):
    def test_empty_and_multi_block_transfers(self):
        async def go():
            harness = SdoHarness()
            await harness.start()
            harness.client.block_size = 3
            harness.server.block_size = 3
            try:
                for payload in (b"", b"x", bytes(range(256)) * 3):
                    for block in (False, True):
                        await harness.client.download(0x2000, 0, payload, block=block)
                        self.assertEqual(await harness.client.upload(0x2000, 0, block=block), payload)
            finally:
                await harness.stop()
        asyncio.run(go())

    def test_configurable_extended_sdo_channel(self):
        async def go():
            bus = VirtualBus()
            client_net, server_net = Network(bus.endpoint()), Network(bus.endpoint())
            await client_net.start()
            await server_net.start()
            ids = {"rx_cob_id": COB_ID_EXTENDED_BIT | 0x12345,
                   "tx_cob_id": COB_ID_EXTENDED_BIT | 0x12346}
            store = {(0x2000, 0): b"configured channel"}
            client = SdoClient(client_net, 1, **ids)
            server = SdoServer(server_net, 1, lambda i, s: store[(i, s)],
                               lambda i, s, d: store.__setitem__((i, s), d), **ids)
            server.start()
            try:
                self.assertEqual(await client.upload(0x2000, 0), b"configured channel")
                await client.download(0x2001, 0, b"abcd")
                self.assertEqual(store[(0x2001, 0)], b"abcd")
            finally:
                client.close()
                await server.close()
                await client_net.close()
                await server_net.close()
        asyncio.run(go())

    def test_block_loss_retransmits_partial_ack(self):
        async def go():
            harness = SdoHarness(store={(0x2000, 0): bytes(range(100))})
            await harness.start()
            original_send = harness.server_net.send
            lost = False
            async def drop_segment(frame):
                nonlocal lost
                if frame.data[0] == 2 and not lost:
                    lost = True
                    return
                await original_send(frame)
            harness.server_net.send = drop_segment
            try:
                self.assertEqual(await harness.client.upload(0x2000, 0, block=True), bytes(range(100)))
                self.assertTrue(lost)
                harness.server_net.send = original_send
                original_client_send = harness.client_net.send
                lost = False
                async def drop_download(frame):
                    nonlocal lost
                    if frame.data[0] == 2 and not lost:
                        lost = True
                        return
                    await original_client_send(frame)
                harness.client_net.send = drop_download
                await harness.client.download(0x2001, 0, bytes(range(100)), block=True)
                self.assertEqual(harness.store[(0x2001, 0)], bytes(range(100)))
                self.assertTrue(lost)
            finally:
                await harness.stop()
        asyncio.run(go())

    def test_repeated_no_progress_block_ack_is_bounded(self):
        async def go():
            harness = SdoHarness()
            await harness.start()
            harness.server._block_down_segment = lambda data: harness.server._reply(b"\xA2\x00\x40")
            try:
                with self.assertRaisesRegex(ProtocolError, "no progress"):
                    await asyncio.wait_for(harness.client.download(0x2000, 0, bytes(21), block=True), 0.5)
            finally:
                await harness.stop()
        asyncio.run(go())

    def test_repeated_no_progress_block_upload_is_bounded(self):
        async def go():
            harness = SdoHarness(store={(0x2000, 0): bytes(range(100))})
            await harness.start()
            original_send = harness.server_net.send
            async def drop_first_segment(frame):
                if frame.data[0] == 1:
                    return
                await original_send(frame)
            harness.server_net.send = drop_first_segment
            try:
                with self.assertRaisesRegex(ProtocolError, "no progress"):
                    await asyncio.wait_for(harness.client.upload(0x2000, 0, block=True), 0.5)
            finally:
                await harness.stop()
        asyncio.run(go())

    def test_block_upload_crc_mismatch_aborts(self):
        async def go():
            harness = SdoHarness(store={(0x2000, 0): bytes(range(100))})
            await harness.start()
            original_send = harness.server_net.send
            async def corrupt_crc(frame):
                if frame.data[0] & 0xE3 == 0xC1:
                    corrupted = bytearray(frame.data)
                    corrupted[1] ^= 1
                    frame = CanFrame(frame.arbitration_id, corrupted)
                await original_send(frame)
            harness.server_net.send = corrupt_crc
            try:
                with self.assertRaisesRegex(ProtocolError, "CRC mismatch"):
                    await harness.client.upload(0x2000, 0, block=True)
            finally:
                await harness.stop()
        asyncio.run(go())

    def test_cancellation_aborts_and_releases_lock(self):
        async def go():
            harness = SdoHarness(store={(0x2000, 0): b"long segmented payload"})
            await harness.start()
            original_send = harness.server_net.send
            segment_sent = asyncio.Event()
            async def drop_segment(frame):
                if frame.data[0] >> 5 == 0:
                    segment_sent.set()
                    return
                await original_send(frame)
            harness.server_net.send = drop_segment
            requests = []
            sub = harness.client_net.subscribe(0x600 + harness.node_id, requests.append)
            # VirtualBus does not echo sender frames, so observe server requests.
            sub.cancel()
            sub = harness.server_net.subscribe(0x600 + harness.node_id, requests.append)
            try:
                task = asyncio.create_task(harness.client.upload(0x2000, 0))
                await asyncio.wait_for(segment_sent.wait(), 1)
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
                harness.server_net.send = original_send
                self.assertEqual(await harness.client.upload(0x2000, 0), b"long segmented payload")
                self.assertTrue(any(frame.data[0] == 0x80 for frame in requests))
            finally:
                sub.cancel()
                await harness.stop()
        asyncio.run(go())

    def test_short_response_aborts(self):
        async def go():
            bus = VirtualBus()
            net = Network(bus.endpoint())
            client = SdoClient(net, 1)
            try:
                task = asyncio.create_task(client.upload(0x2000, 0))
                await asyncio.sleep(0)
                net.dispatch(CanFrame(0x581, b"\x43"))
                with self.assertRaisesRegex(ProtocolError, "8-byte"):
                    await task
            finally:
                client.close()
                await net.close()
        asyncio.run(go())

    def test_server_rejects_ack_beyond_segments_sent(self):
        net = Network(VirtualBus().endpoint())
        server = SdoServer(net, 1, lambda i, s: b"x", lambda *args: None)
        server._on_frame(CanFrame(0x601, bytes.fromhex("A400200020000000")))
        server._out.get_nowait()
        server._on_frame(CanFrame(0x601, b"\xA3" + bytes(7)))
        server._out.get_nowait()
        server._on_frame(CanFrame(0x601, b"\xA2\x02\x20" + bytes(5)))
        abort = server._out.get_nowait()
        self.assertEqual(abort[0], 0x80)
        self.assertEqual(int.from_bytes(abort[4:], "little"), ABORT_SEQUENCE)

    def test_server_rejects_unused_bytes_in_nonfinal_segment(self):
        net = Network(VirtualBus().endpoint())
        server = SdoServer(net, 1, lambda *args: b"", lambda *args: None)
        server._on_frame(CanFrame(0x601, bytes.fromhex("2100200007000000")))
        server._out.get_nowait()
        server._on_frame(CanFrame(0x601, b"\x02" + bytes(7)))
        abort = server._out.get_nowait()
        self.assertEqual(int.from_bytes(abort[4:], "little"), ABORT_COMMAND)

    def test_client_close_wakes_pending_sdo_and_rejects_new_transfer(self):
        async def go():
            net = Network(VirtualBus().endpoint())
            client = SdoClient(net, 1, timeout=10)
            task = asyncio.create_task(client.upload(0x2000, 0))
            await asyncio.sleep(0)
            client.close()
            with self.assertRaises(TransportError):
                await asyncio.wait_for(task, 0.2)
            with self.assertRaises(TransportError):
                await client.upload(0x2000, 0)
            await net.close()
        asyncio.run(go())

    def test_device_close_wakes_pending_sdo(self):
        async def go():
            from sevcon_can.device import AsyncSevconGen4
            net = Network(VirtualBus().endpoint())
            dev = AsyncSevconGen4(net, 1, sdo_timeout=10)
            task = asyncio.create_task(dev.sdo.upload(0x2000, 0))
            await asyncio.sleep(0)
            await dev.close()
            with self.assertRaises(TransportError):
                await asyncio.wait_for(task, 0.2)
            await net.close()
        asyncio.run(go())

    def test_consumer_close_and_network_close_wake_iterators(self):
        async def go():
            from sevcon_can.emcy import EmcyConsumer
            for close_network in (False, True):
                net = Network(VirtualBus().endpoint())
                consumers = [PdoConsumer(net, 0x181, [(0x2000, 0, 8)]), EmcyConsumer(net, 1)]
                streams = [consumers[0].updates(), consumers[1].notifications()]
                tasks = [asyncio.create_task(stream.__anext__()) for stream in streams]
                await asyncio.sleep(0)
                if close_network:
                    await net.close()
                else:
                    for consumer in consumers:
                        consumer.close()
                for task in tasks:
                    with self.assertRaises(TransportError):
                        await asyncio.wait_for(task, 0.2)
                for consumer in consumers:
                    consumer.close()
                await net.close()
        asyncio.run(go())

    def test_network_close_wakes_pending_sdo(self):
        async def go():
            net = Network(VirtualBus().endpoint())
            client = SdoClient(net, 1, timeout=10)
            task = asyncio.create_task(client.upload(0x2000, 0))
            await asyncio.sleep(0)
            await net.close()
            with self.assertRaises(TransportError):
                await asyncio.wait_for(task, 0.2)
            client.close()
        asyncio.run(go())


if __name__ == "__main__":
    unittest.main()
