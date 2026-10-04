import asyncio
import sys
import unittest

sys.path.insert(0, "src")

from sevcon_can.network import Network
from sevcon_can.sdo import SdoClient, SdoServer, crc16
from sevcon_can.transport import VirtualBus


def run(coro):
    return asyncio.run(coro)


class TestCrc(unittest.TestCase):
    def test_crc_known_vector(self):
        # CiA 301 CRC-16/XMODEM check value for "123456789".
        self.assertEqual(crc16(b"123456789"), 0x31C3)
        self.assertEqual(crc16(b""), 0)


class SdoHarness:
    """Client + server on one virtual bus with a dict-backed store."""

    def __init__(self, node_id=0x20, store=None):
        from sevcon_can.exceptions import SdoAbortError
        self.bus = VirtualBus()
        self.client_net = Network(self.bus.endpoint())
        self.server_net = Network(self.bus.endpoint())
        self.store = dict(store or {})
        self._SdoAbortError = SdoAbortError
        self.node_id = node_id

    def _read(self, index, sub):
        try:
            return self.store[(index, sub)]
        except KeyError:
            raise self._SdoAbortError(0x06020000, index, sub)

    def _write(self, index, sub, data):
        self.store[(index, sub)] = bytes(data)

    async def start(self):
        await self.client_net.start()
        await self.server_net.start()
        self.client = SdoClient(self.client_net, self.node_id)
        self.server = SdoServer(self.server_net, self.node_id, self._read, self._write)
        self.server.start()

    async def stop(self):
        self.client.close()
        await self.server.close()
        await self.client_net.close()
        await self.server_net.close()


class TestSdoTransfers(unittest.TestCase):
    def _roundtrip(self, payload: bytes, **kw):
        async def go():
            harness = SdoHarness(store={(0x2000, 0): payload})
            await harness.start()
            try:
                data = await harness.client.upload(0x2000, 0, **kw)
                self.assertEqual(data, payload)
                await harness.client.download(0x2001, 1, payload, **kw)
                self.assertEqual(harness.store[(0x2001, 1)], payload)
            finally:
                await harness.stop()
        run(go())

    def test_expedited(self):
        self._roundtrip(b"\x01\x02\x03")

    def test_segmented(self):
        self._roundtrip(bytes(range(32)))

    def test_block(self):
        self._roundtrip(bytes(range(100)), block=True)

    def test_forced_segmented(self):
        async def go():
            harness = SdoHarness(store={(0x2000, 0): b"\xAA\xBB"})
            await harness.start()
            try:
                await harness.client.download(0x2000, 0, b"\xAA\xBB", force_segmented=True)
                data = await harness.client.upload(0x2000, 0)
                self.assertEqual(data, b"\xAA\xBB")
            finally:
                await harness.stop()
        run(go())

    def test_abort_propagates_code(self):
        async def go():
            from sevcon_can.exceptions import SdoAbortError
            harness = SdoHarness()
            await harness.start()
            try:
                with self.assertRaises(SdoAbortError) as ctx:
                    await harness.client.upload(0x9999, 0)
                self.assertEqual(ctx.exception.code, 0x06020000)
            finally:
                await harness.stop()
        run(go())

    def test_timeout(self):
        async def go():
            from sevcon_can.exceptions import TimeoutError
            # A client for a node with no server never gets a reply.
            bus = VirtualBus()
            client_net = Network(bus.endpoint())
            await client_net.start()
            try:
                client = SdoClient(client_net, 0x21, timeout=0.05)
                with self.assertRaises(TimeoutError):
                    await client.upload(0x2000, 0)
                client.close()
            finally:
                await client_net.close()
        run(go())


if __name__ == "__main__":
    unittest.main()
