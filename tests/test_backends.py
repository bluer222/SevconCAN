import sys
import unittest

sys.path.insert(0, "src")

from sevcon_can.cli import build_parser, main_async
from sevcon_can.device import AsyncSevconGen4
from sevcon_can.exceptions import TransportError


def run(coro):
    import asyncio
    return asyncio.run(coro)


class TestFactories(unittest.TestCase):
    def test_gs_usb_missing_interface(self):
        with self.assertRaises(TransportError):
            AsyncSevconGen4.from_gs_usb("can9-nonexistent", 0x20)

    def test_python_can_hint_without_package(self):
        from unittest.mock import patch
        with patch.dict(sys.modules, {"can": None}):
            with self.assertRaises(TransportError) as ctx:
                AsyncSevconGen4.from_python_can(
                    0x20, interface="slcan", channel="/dev/ttyACM0",
                    bitrate=1000000)
            self.assertIn("python-can", str(ctx.exception))

    def test_backend_requires_port(self):
        args = build_parser().parse_args(["--backend", "slcan", "info"])
        self.assertEqual(args.backend, "slcan")
        self.assertIsNone(args.port)
        self.assertEqual(run(main_async(["--backend", "slcan", "info"])), 1)

    def test_backend_defaults(self):
        args = build_parser().parse_args(["info"])
        self.assertEqual((args.backend, args.bitrate), ("socketcan", 1000000))


class TestWireFrames(unittest.TestCase):
    def test_error_mask_and_payload_survive_socketcan(self):
        from sevcon_can.transport import CanFrame, pack_socketcan, unpack_socketcan
        frame = CanFrame(0x1000, bytes(range(8)), is_error=True)
        decoded = unpack_socketcan(pack_socketcan(frame))
        self.assertEqual(decoded.arbitration_id, 0x1000)
        self.assertEqual(decoded.data, frame.data)
        self.assertTrue(decoded.is_error)

    def test_remote_dlc_survives_socketcan(self):
        from sevcon_can.transport import CanFrame, pack_socketcan, unpack_socketcan
        decoded = unpack_socketcan(pack_socketcan(CanFrame(0x120, is_remote=True, dlc=8)))
        self.assertTrue(decoded.is_remote)
        self.assertEqual(decoded.dlc, 8)
        self.assertEqual(decoded.data, b"")

    def test_reject_malformed_wire_frame(self):
        from sevcon_can.transport import unpack_socketcan
        with self.assertRaises(TransportError):
            unpack_socketcan(b"bad")


class TestBackendOptions(unittest.TestCase):
    def options(self, argv):
        from sevcon_can.cli import backend_options
        return backend_options(build_parser().parse_args(argv + ["info"]))

    def test_direct_gs_usb_defaults(self):
        self.assertEqual(self.options(["--backend", "gs_usb"]),
                         dict(interface="gs_usb", channel=0, index=0, bitrate=1000000))

    def test_direct_gs_usb_usb_address(self):
        opts = self.options(["--backend", "gs_usb", "--usb-bus", "1", "--usb-address", "7"])
        self.assertEqual((opts["bus"], opts["address"]), (1, 7))
        self.assertNotIn("index", opts)
        with self.assertRaises(ValueError):
            self.options(["--backend", "gs_usb", "--usb-bus", "1"])

    def test_slcan_serial_rate_is_distinct(self):
        opts = self.options(["--backend", "slcan", "--port", "COM4", "--serial-baudrate", "3000000"])
        self.assertEqual(opts["tty_baudrate"], 3000000)
        self.assertEqual(opts["bitrate"], 1000000)


try:
    import can
except ImportError:
    can = None


@unittest.skipIf(can is None, "optional python-can dependency is not installed")
class TestPythonCanVirtual(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        import uuid
        from sevcon_can.transport import PythonCanTransport
        channel = str(uuid.uuid4())
        self.transport = PythonCanTransport(interface="virtual", channel=channel)
        self.peer = can.Bus(interface="virtual", channel=channel)

    async def asyncTearDown(self):
        await self.transport.close()
        self.peer.shutdown()

    async def test_bidirectional_classic_frames(self):
        import asyncio
        from sevcon_can.transport import CanFrame
        await self.transport.send(CanFrame(0x601, b"\x40\x18\x10\x01\x00\x00\x00\x00"))
        msg = await asyncio.to_thread(self.peer.recv, 1)
        self.assertEqual(msg.arbitration_id, 0x601)
        self.assertEqual(msg.dlc, 8)
        self.peer.send(can.Message(arbitration_id=0x581, data=b"\x43\x18\x10\x01\x12\x34\x00\x00", is_extended_id=False))
        frame = await asyncio.wait_for(self.transport.recv(), 1)
        self.assertEqual(frame.data[-4:], b"\x12\x34\x00\x00")

    async def test_remote_error_and_fd_frames(self):
        import asyncio
        self.peer.send(can.Message(arbitration_id=0x180, is_extended_id=False, is_remote_frame=True, dlc=8))
        frame = await asyncio.wait_for(self.transport.recv(), 1)
        self.assertTrue(frame.is_remote)
        self.assertEqual(frame.dlc, 8)
        self.peer.send(can.Message(arbitration_id=0x1000, data=bytes(range(8)), is_extended_id=False, is_error_frame=True))
        frame = await asyncio.wait_for(self.transport.recv(), 1)
        self.assertTrue(frame.is_error)
        self.assertEqual(frame.arbitration_id, 0x1000)
        self.assertEqual(frame.data, bytes(range(8)))
        self.peer.send(can.Message(arbitration_id=0x581, data=bytes(range(12)), is_extended_id=False, is_fd=True))
        self.peer.send(can.Message(arbitration_id=0x701, data=b"\x05", is_extended_id=False))
        frame = await asyncio.wait_for(self.transport.recv(), 1)
        self.assertEqual(frame.arbitration_id, 0x701)

    async def test_receiver_error_wakes_waiter(self):
        import asyncio
        waiter = asyncio.create_task(self.transport.recv())
        await asyncio.sleep(0)
        self.transport._on_error(can.CanOperationError("adapter unplugged"))
        with self.assertRaisesRegex(TransportError, "adapter unplugged"):
            await asyncio.wait_for(waiter, 1)
        with self.assertRaisesRegex(TransportError, "adapter unplugged"):
            await self.transport.recv()

    async def test_close_wakes_waiter(self):
        import asyncio
        waiter = asyncio.create_task(self.transport.recv())
        await asyncio.sleep(0)
        await self.transport.close()
        with self.assertRaisesRegex(TransportError, "closed"):
            await asyncio.wait_for(waiter, 1)

    async def test_notifier_stop_failure_still_shuts_down_bus(self):
        from unittest.mock import patch
        with patch.object(self.transport._notifier, "stop", side_effect=RuntimeError("stop failed")):
            with self.assertRaisesRegex(TransportError, "stop failed"):
                await self.transport.close()
        self.assertTrue(self.transport._bus._is_shutdown)
        # Stop the real notifier thread after exercising the failure branch.
        import asyncio
        await asyncio.to_thread(self.transport._notifier.stop)

    async def test_borrowed_bus_is_not_shutdown(self):
        from sevcon_can.transport import PythonCanTransport
        borrowed = PythonCanTransport(self.peer)
        await borrowed.close()
        self.assertFalse(self.peer._is_shutdown)


@unittest.skipIf(can is None, "optional python-can dependency is not installed")
class TestSlcanLoopback(unittest.IsolatedAsyncioTestCase):
    async def test_slcan_ascii_protocol_via_pyserial_loopback(self):
        import asyncio
        try:
            import serial
        except ImportError:
            self.skipTest("optional pyserial dependency is not installed")
        from sevcon_can.transport import CanFrame, PythonCanTransport
        # pyserial loop:// echoes bytes. This exercises the real python-can
        # SLCAN ASCII encoder/parser and notifier without a USB adapter.
        transport = PythonCanTransport(interface="slcan", channel="loop://",
                                       bitrate=1000000, sleep_after_open=0)
        try:
            await transport.send(CanFrame(0x601, b"\x40\x18\x10\x01\x00\x00\x00\x00"))
            received = await asyncio.wait_for(transport.recv(), 1)
            self.assertEqual(received.arbitration_id, 0x601)
            self.assertEqual(received.data, b"\x40\x18\x10\x01\x00\x00\x00\x00")
            await transport.send(CanFrame(0x180, is_remote=True, dlc=8))
            received = await asyncio.wait_for(transport.recv(), 1)
            self.assertTrue(received.is_remote)
            self.assertEqual(received.dlc, 8)
        finally:
            await transport.close()


if __name__ == "__main__":
    unittest.main()
