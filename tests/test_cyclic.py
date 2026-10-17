import asyncio
import sys
import unittest

sys.path.insert(0, "src")

from sevcon_can.cyclic import HeartbeatProducer, SyncProducer
from sevcon_can.exceptions import TransportError
from sevcon_can.network import Network
from sevcon_can.transport import VirtualBus


class TestCyclic(unittest.TestCase):
    def test_heartbeat_bootup_periodic_state_and_close(self):
        async def go():
            bus = VirtualBus()
            peer = bus.endpoint()
            net = Network(bus.endpoint())
            async with HeartbeatProducer(net, 127, period_ms=5, bootup=True) as producer:
                self.assertEqual((await peer.recv()).data, b"\x00")
                self.assertEqual((await peer.recv()).data, b"\x05")
                producer.state = 127
                self.assertEqual((await peer.recv()).data, b"\x7f")
            with self.assertRaises(asyncio.TimeoutError):
                await asyncio.wait_for(peer.recv(), 0.02)
            await net.close()
        asyncio.run(go())

    def test_sync_counter_extended_and_close(self):
        async def go():
            bus = VirtualBus()
            peer = bus.endpoint()
            net = Network(bus.endpoint())
            async with SyncProducer(net, period_ms=2, cob_id=0x60012345, counter_overflow=2):
                frames = [await peer.recv() for _ in range(3)]
                self.assertEqual([frame.data for frame in frames], [b"\x01", b"\x02", b"\x01"])
                self.assertTrue(all(frame.is_extended and frame.arbitration_id == 0x12345 for frame in frames))
            with self.assertRaises(asyncio.TimeoutError):
                await asyncio.wait_for(peer.recv(), 0.02)
            await net.close()
        asyncio.run(go())

    def test_empty_sync(self):
        async def go():
            bus = VirtualBus()
            peer = bus.endpoint()
            net = Network(bus.endpoint())
            async with SyncProducer(net, period_ms=2):
                frame = await peer.recv()
                self.assertEqual((frame.arbitration_id, frame.data), (0x80, b""))
            await net.close()
        asyncio.run(go())

    def test_send_failure_propagates_on_context_exit(self):
        async def go():
            net = Network(VirtualBus().endpoint())
            with self.assertRaises(TransportError):
                async with SyncProducer(net, period_ms=2):
                    await net.close()
                    await asyncio.sleep(0.01)
        asyncio.run(go())

    def test_no_catchup_burst_after_slow_send(self):
        async def go():
            net = Network(VirtualBus().endpoint())
            times = []
            async def slow_send(frame):
                times.append(asyncio.get_running_loop().time())
                await asyncio.sleep(0.015)
            net.send = slow_send
            async with SyncProducer(net, period_ms=10):
                await asyncio.sleep(0.07)
            self.assertGreaterEqual(len(times), 2)
            self.assertTrue(all(later - earlier >= 0.022 for earlier, later in zip(times, times[1:])))
            await net.close()
        asyncio.run(go())

    def test_validation(self):
        net = Network(VirtualBus().endpoint())
        for period in (0, -1, float("nan"), float("inf")):
            with self.assertRaises(ValueError):
                SyncProducer(net, period_ms=period)
        for node_id in (0, 128):
            with self.assertRaises(ValueError):
                HeartbeatProducer(net, node_id)
        with self.assertRaises(ValueError):
            HeartbeatProducer(net, 1, state=0)
        for overflow in (0, 1, 241):
            with self.assertRaises(ValueError):
                SyncProducer(net, counter_overflow=overflow)
        with self.assertRaises(ValueError):
            SyncProducer(net, cob_id=0x80000080)


if __name__ == "__main__":
    unittest.main()
