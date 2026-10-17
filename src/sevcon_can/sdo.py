"""CiA 301 SDO client and server.

The client implements expedited, segmented and block transfers (upload and
download, block transfers with CRC). The server is used by
:mod:`sevcon_can.simulator` and supports the same protocols.

COB-IDs default to the predefined connection set: requests on
``0x600 + node``, responses on ``0x580 + node``. The Gen4 offers further
SDO servers (1201h-1203h) on configurable COB-IDs; pass them explicitly.
"""
from __future__ import annotations

import asyncio
import logging
import math
import struct
from contextlib import asynccontextmanager
from typing import Callable, List, Optional

from .exceptions import ProtocolError, SdoAbortError, TimeoutError, TransportError
from .network import FrameQueue, Network
from .pdo import COB_ID_EXTENDED_BIT, cob_id_can_id, cob_id_valid
from .transport import CanFrame

log = logging.getLogger(__name__)

# Abort codes used by this implementation (CiA 301 Table 22).
ABORT_TOGGLE = 0x05030000
ABORT_TIMEOUT = 0x05040000
ABORT_COMMAND = 0x05040001
ABORT_BLOCK_SIZE = 0x05040002
ABORT_SEQUENCE = 0x05040003
ABORT_CRC = 0x05040004
ABORT_OUT_OF_MEMORY = 0x05040005
ABORT_UNSUPPORTED = 0x06010000
ABORT_WRITE_ONLY = 0x06010001
ABORT_READ_ONLY = 0x06010002
ABORT_NO_OBJECT = 0x06020000
ABORT_LENGTH = 0x06070010
ABORT_LENGTH_HIGH = 0x06070012
ABORT_LENGTH_LOW = 0x06070013
ABORT_NO_SUBINDEX = 0x06090011
ABORT_VALUE_RANGE = 0x06090030
ABORT_VALUE_HIGH = 0x06090031
ABORT_VALUE_LOW = 0x06090032
ABORT_GENERAL = 0x08000000
ABORT_DEVICE_STATE = 0x08000022

_U32 = struct.Struct("<I")


# ---------------------------------------------------------------------------
# CRC-16 (CiA 301 7.2.4.3.16: polynomial x^16 + x^12 + x^5 + 1, initial 0)
# ---------------------------------------------------------------------------

def _crc_table() -> List[int]:
    table = []
    for i in range(256):
        crc = i << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) if crc & 0x8000 else (crc << 1)
        table.append(crc & 0xFFFF)
    return table


_CRC_TABLE = _crc_table()


def crc16(data: bytes, crc: int = 0) -> int:
    for b in data:
        crc = ((crc << 8) & 0xFFFF) ^ _CRC_TABLE[((crc >> 8) ^ b) & 0xFF]
    return crc


def _frame(*parts: bytes) -> bytes:
    return b"".join(parts).ljust(8, b"\x00")[:8]


def _mux(index: int, subindex: int) -> bytes:
    return struct.pack("<HB", index, subindex)


def abort_frame(index: int, subindex: int, code: int) -> bytes:
    return _frame(b"\x80", _mux(index, subindex), _U32.pack(code))


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------

class SdoClient:
    """SDO client for one server.

    Transfers on one client are serialized; use separate clients (separate
    Gen4 SDO servers) for concurrent transfers.
    """

    def __init__(self, network: Network, node_id: int, *,
                 rx_cob_id: Optional[int] = None, tx_cob_id: Optional[int] = None,
                 timeout: float = 1.0, block_size: int = 32) -> None:
        if not 1 <= node_id <= 127:
            raise ValueError("node id must be 1..127")
        if not 1 <= block_size <= 127:
            raise ValueError("block size must be 1..127")
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("timeout must be finite and positive")
        self.network = network
        self.node_id = node_id
        #: client -> server (request) COB-ID
        self.rx_cob_id = rx_cob_id if rx_cob_id is not None else 0x600 + node_id
        #: server -> client (response) COB-ID
        self.tx_cob_id = tx_cob_id if tx_cob_id is not None else 0x580 + node_id
        if not cob_id_valid(self.rx_cob_id) or not cob_id_valid(self.tx_cob_id):
            raise ValueError("SDO channel is disabled")
        self._rx_can_id = cob_id_can_id(self.rx_cob_id)
        self._tx_can_id = cob_id_can_id(self.tx_cob_id)
        self.timeout = timeout
        self._transfer_timeout: Optional[float] = None
        self.block_size = block_size
        self._closed = False
        self._lock = asyncio.Lock()
        self._rx = FrameQueue(maxsize=256)
        self._sub = network.subscribe(self._tx_can_id, self._rx,
                                      extended=bool(self.tx_cob_id & COB_ID_EXTENDED_BIT))

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            self._rx.on_network_error(TransportError("SDO client is closed"))
            self._sub.cancel()

    # ---- low level ----
    async def _send(self, data: bytes) -> None:
        if self._closed:
            raise TransportError("SDO client is closed")
        await self.network.send(CanFrame(self._rx_can_id, _frame(data),
                                         is_extended=bool(self.rx_cob_id & COB_ID_EXTENDED_BIT)))

    async def _recv(self, index: int, subindex: int, timeout: Optional[float] = None) -> bytes:
        try:
            if timeout is None:
                timeout = self._transfer_timeout if self._transfer_timeout is not None else self.timeout
            frame = await self._rx.get(timeout)
        except TimeoutError:
            try:
                await self._send(abort_frame(index, subindex, ABORT_TIMEOUT))
            except TransportError:
                pass
            raise TimeoutError(f"SDO timeout on 0x{index:04X}:{subindex:02X} (node {self.node_id})") from None
        if self._closed:
            raise TransportError("SDO client is closed")
        if frame.is_remote or frame.is_error or len(frame.data) != 8:
            await self._fail(index, subindex, ABORT_COMMAND, "SDO response must be an 8-byte data frame")
        return frame.data

    @asynccontextmanager
    async def _transfer(self, index: int, subindex: int, timeout: Optional[float] = None):
        # Cancellation must release the server's segmented/block transfer state.
        # Abort while holding the lock so the next caller cannot race cleanup.
        if timeout is not None and (not math.isfinite(timeout) or timeout <= 0):
            raise ValueError("timeout must be finite and positive")
        async with self._lock:
            if self._closed:
                raise TransportError("SDO client is closed")
            self._rx.clear()
            self._transfer_timeout = timeout
            try:
                yield
            except asyncio.CancelledError:
                try:
                    await self._send(abort_frame(index, subindex, ABORT_GENERAL))
                except TransportError:
                    pass
                raise
            finally:
                self._transfer_timeout = None

    def _check_abort(self, resp: bytes, index: int, subindex: int) -> None:
        if resp[0] == 0x80:
            raise SdoAbortError(_U32.unpack_from(resp, 4)[0], index, subindex)

    async def _fail(self, index: int, subindex: int, code: int, msg: str) -> None:
        try:
            await self._send(abort_frame(index, subindex, code))
        except TransportError:
            pass
        raise ProtocolError(f"0x{index:04X}:{subindex:02X}: {msg}")

    def _check_mux(self, resp: bytes, index: int, subindex: int) -> bool:
        return struct.unpack_from("<HB", resp, 1) == (index, subindex)

    # ---- upload ----
    async def upload(self, index: int, subindex: int, *, block: bool = False,
                     timeout: Optional[float] = None) -> bytes:
        """Read an object, optionally overriding this transfer's response timeout.

        ``block=True`` uses block upload with CRC.
        """
        async with self._transfer(index, subindex, timeout):
            if block:
                return await self._block_upload(index, subindex)
            await self._send(_frame(b"\x40", _mux(index, subindex)))
            resp = await self._recv(index, subindex)
            return await self._upload_from_initiate(resp, index, subindex)

    async def _upload_from_initiate(self, resp: bytes, index: int, subindex: int) -> bytes:
        self._check_abort(resp, index, subindex)
        if resp[0] >> 5 != 2:
            await self._fail(index, subindex, ABORT_COMMAND, f"unexpected upload response 0x{resp[0]:02X}")
        if not self._check_mux(resp, index, subindex):
            await self._fail(index, subindex, ABORT_COMMAND, "response for another object")
        expedited, size_set = resp[0] & 0x02, resp[0] & 0x01
        if expedited:
            n = (resp[0] >> 2) & 0x03 if size_set else 0
            return bytes(resp[4:8 - n])
        size = _U32.unpack_from(resp, 4)[0] if size_set else None
        data = bytearray()
        toggle = 0
        while True:
            await self._send(bytes([0x60 | (toggle << 4)]))
            seg = await self._recv(index, subindex)
            self._check_abort(seg, index, subindex)
            if seg[0] >> 5 != 0:
                await self._fail(index, subindex, ABORT_COMMAND, f"unexpected segment 0x{seg[0]:02X}")
            if (seg[0] >> 4) & 1 != toggle:
                await self._fail(index, subindex, ABORT_TOGGLE, "toggle bit not alternated")
            n = (seg[0] >> 1) & 0x07
            if n and not seg[0] & 1:
                await self._fail(index, subindex, ABORT_COMMAND, "non-final segment declares unused bytes")
            data += seg[1:8 - n]
            if size is not None and len(data) > size:
                await self._fail(index, subindex, ABORT_LENGTH_HIGH, f"more than {size} bytes received")
            if seg[0] & 0x01:
                break
            toggle ^= 1
        if size is not None and len(data) != size:
            await self._fail(index, subindex, ABORT_LENGTH_LOW, f"expected {size} bytes, got {len(data)}")
        return bytes(data)

    async def _block_upload(self, index: int, subindex: int) -> bytes:
        blksize = self.block_size
        await self._send(_frame(bytes([0xA4]), _mux(index, subindex), bytes([blksize, 0])))
        resp = await self._recv(index, subindex)
        self._check_abort(resp, index, subindex)
        if resp[0] >> 5 == 2:  # server switched to a normal upload
            return await self._upload_from_initiate(resp, index, subindex)
        if resp[0] >> 5 != 6 or resp[0] & 0x01:
            await self._fail(index, subindex, ABORT_COMMAND, f"unexpected block upload response 0x{resp[0]:02X}")
        if not self._check_mux(resp, index, subindex):
            await self._fail(index, subindex, ABORT_COMMAND, "response for another object")
        server_crc = bool(resp[0] & 0x04)
        size = _U32.unpack_from(resp, 4)[0] if resp[0] & 0x02 else None
        await self._send(bytes([0xA3]))
        data = bytearray()
        done = False
        stalled = 0
        while not done:
            expected = 1
            while True:
                seg = await self._recv(index, subindex)
                seq, last = seg[0] & 0x7F, bool(seg[0] & 0x80)
                if seq > blksize:
                    await self._fail(index, subindex, ABORT_SEQUENCE, "sequence exceeds negotiated block size")
                if seq == 0:
                    self._check_abort(seg, index, subindex)
                    await self._fail(index, subindex, ABORT_SEQUENCE, "sequence number 0")
                if seq == expected:
                    data += seg[1:8]
                    expected += 1
                    if last:
                        done = True
                        break
                if seq >= blksize or (last and seq != expected - 1):
                    break
            stalled = stalled + 1 if expected == 1 else 0
            if stalled >= 3:
                await self._fail(index, subindex, ABORT_SEQUENCE, "block upload made no progress after three blocks")
            await self._send(bytes([0xA2, expected - 1, blksize]))
        end = await self._recv(index, subindex)
        self._check_abort(end, index, subindex)
        if end[0] >> 5 != 6 or end[0] & 0x03 != 1:
            await self._fail(index, subindex, ABORT_COMMAND, f"expected block upload end, got 0x{end[0]:02X}")
        n = (end[0] >> 2) & 0x07
        if n:
            del data[-n:]
        if size is not None and len(data) != size:
            await self._fail(index, subindex, ABORT_LENGTH, f"expected {size} bytes, got {len(data)}")
        if server_crc and struct.unpack_from("<H", end, 1)[0] != crc16(bytes(data)):
            await self._fail(index, subindex, ABORT_CRC, "CRC mismatch")
        await self._send(bytes([0xA1]))
        return bytes(data)

    # ---- download ----
    async def download(self, index: int, subindex: int, data: bytes, *,
                       force_segmented: bool = False, block: bool = False,
                       timeout: Optional[float] = None) -> None:
        """Write an object. Up to 4 bytes go expedited unless
        ``force_segmented``; ``block=True`` uses block download (with CRC)."""
        data = bytes(data)
        async with self._transfer(index, subindex, timeout):
            if block:
                await self._block_download(index, subindex, data)
                return
            if len(data) <= 4 and data and not force_segmented:
                await self._send(_frame(bytes([0x23 | ((4 - len(data)) << 2)]), _mux(index, subindex), data))
                await self._expect_download_ack(index, subindex)
                return
            await self._send(_frame(b"\x21", _mux(index, subindex), _U32.pack(len(data))))
            await self._expect_download_ack(index, subindex)
            toggle = 0
            pos = 0
            while True:
                chunk = data[pos:pos + 7]
                pos += 7
                last = pos >= len(data)
                await self._send(_frame(bytes([(toggle << 4) | ((7 - len(chunk)) << 1) | int(last)]), chunk))
                resp = await self._recv(index, subindex)
                self._check_abort(resp, index, subindex)
                if resp[0] & 0xE0 != 0x20:
                    await self._fail(index, subindex, ABORT_COMMAND, f"unexpected segment ack 0x{resp[0]:02X}")
                if (resp[0] >> 4) & 1 != toggle:
                    await self._fail(index, subindex, ABORT_TOGGLE, "toggle bit not alternated")
                if last:
                    return
                toggle ^= 1

    async def _expect_download_ack(self, index: int, subindex: int) -> None:
        resp = await self._recv(index, subindex)
        self._check_abort(resp, index, subindex)
        if resp[0] != 0x60:
            await self._fail(index, subindex, ABORT_COMMAND, f"unexpected download response 0x{resp[0]:02X}")
        if not self._check_mux(resp, index, subindex):
            await self._fail(index, subindex, ABORT_COMMAND, "response for another object")

    async def _block_download(self, index: int, subindex: int, data: bytes) -> None:
        await self._send(_frame(bytes([0xC6]), _mux(index, subindex), _U32.pack(len(data))))
        resp = await self._recv(index, subindex)
        self._check_abort(resp, index, subindex)
        if resp[0] >> 5 != 5 or resp[0] & 0x03 != 0:
            await self._fail(index, subindex, ABORT_COMMAND, f"unexpected block download response 0x{resp[0]:02X}")
        if not self._check_mux(resp, index, subindex):
            await self._fail(index, subindex, ABORT_COMMAND, "response for another object")
        server_crc = bool(resp[0] & 0x04)
        blksize = resp[4]
        if not 1 <= blksize <= 127:
            await self._fail(index, subindex, ABORT_BLOCK_SIZE, f"invalid block size {blksize}")
        chunks = [data[i:i + 7] for i in range(0, len(data), 7)] or [b""]
        pos = 0
        stalled = 0
        while pos < len(chunks):
            sent = 0
            for k in range(blksize):
                if pos + k >= len(chunks):
                    break
                last = pos + k == len(chunks) - 1
                await self._send(_frame(bytes([(0x80 if last else 0) | (k + 1)]), chunks[pos + k]))
                sent += 1
            ack = await self._recv(index, subindex)
            self._check_abort(ack, index, subindex)
            if ack[0] != 0xA2:
                await self._fail(index, subindex, ABORT_COMMAND, f"expected block ack, got 0x{ack[0]:02X}")
            if ack[1] > sent:
                await self._fail(index, subindex, ABORT_SEQUENCE, f"ack sequence {ack[1]} > {sent}")
            stalled = stalled + 1 if ack[1] == 0 else 0
            if stalled >= 3:
                await self._fail(index, subindex, ABORT_SEQUENCE, "block download made no progress after three acknowledgments")
            pos += ack[1]
            blksize = ack[2]
            if not 1 <= blksize <= 127:
                await self._fail(index, subindex, ABORT_BLOCK_SIZE, f"invalid block size {blksize}")
        n = 7 - len(chunks[-1])
        crc = crc16(data) if server_crc else 0
        await self._send(_frame(bytes([0xC1 | (n << 2)]), struct.pack("<H", crc)))
        end = await self._recv(index, subindex)
        self._check_abort(end, index, subindex)
        if end[0] != 0xA1:
            await self._fail(index, subindex, ABORT_COMMAND, f"expected block download end, got 0x{end[0]:02X}")


# ---------------------------------------------------------------------------
# Server
# ---------------------------------------------------------------------------

ReadHandler = Callable[[int, int], bytes]
WriteHandler = Callable[[int, int, bytes], None]


class SdoServer:
    """SDO server. ``read(index, sub) -> bytes`` and ``write(index, sub,
    data)`` implement the object dictionary; they raise
    :class:`SdoAbortError` to abort with a specific code. Any other
    exception aborts with 0x08000000."""

    def __init__(self, network: Network, node_id: int, read: ReadHandler, write: WriteHandler, *,
                 rx_cob_id: Optional[int] = None, tx_cob_id: Optional[int] = None,
                 block_size: int = 64) -> None:
        if not 1 <= node_id <= 127:
            raise ValueError("node id must be 1..127")
        if not 1 <= block_size <= 127:
            raise ValueError("block size must be 1..127")
        self.network = network
        self.node_id = node_id
        self.rx_cob_id = rx_cob_id if rx_cob_id is not None else 0x600 + node_id
        self.tx_cob_id = tx_cob_id if tx_cob_id is not None else 0x580 + node_id
        if not cob_id_valid(self.rx_cob_id) or not cob_id_valid(self.tx_cob_id):
            raise ValueError("SDO channel is disabled")
        self._rx_can_id = cob_id_can_id(self.rx_cob_id)
        self._tx_can_id = cob_id_can_id(self.tx_cob_id)
        self.read = read
        self.write = write
        self.block_size = block_size
        self._out: "asyncio.Queue[bytes]" = asyncio.Queue()
        self._task: Optional["asyncio.Task[None]"] = None
        self._sub = network.subscribe(self._rx_can_id, self._on_frame,
                                      extended=bool(self.rx_cob_id & COB_ID_EXTENDED_BIT))
        self._reset()

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.get_running_loop().create_task(self._sender())

    async def close(self) -> None:
        self._sub.cancel()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def _sender(self) -> None:
        while True:
            data = await self._out.get()
            try:
                await self.network.send(CanFrame(self._tx_can_id, data,
                                                 is_extended=bool(self.tx_cob_id & COB_ID_EXTENDED_BIT)))
            except TransportError as e:
                log.warning("SDO server send failed: %s", e)

    def _reply(self, data: bytes) -> None:
        self._out.put_nowait(_frame(data))

    def _reset(self) -> None:
        self._state = "idle"
        self._index = 0
        self._sub_index = 0
        self._buf = bytearray()
        self._toggle = 0
        self._size: Optional[int] = None
        self._pos = 0
        self._blksize = 0
        self._expected = 1
        self._block_start = 0
        self._crc = False
        self._last = False

    def _abort(self, code: int) -> None:
        self._reply(abort_frame(self._index, self._sub_index, code))
        self._reset()

    def _call_read(self) -> Optional[bytes]:
        try:
            return bytes(self.read(self._index, self._sub_index))
        except SdoAbortError as e:
            self._abort(e.code)
        except Exception:  # noqa: BLE001 - mapped to an SDO abort
            log.exception("SDO read handler failed")
            self._abort(ABORT_GENERAL)
        return None

    def _call_write(self, data: bytes) -> bool:
        try:
            self.write(self._index, self._sub_index, bytes(data))
            return True
        except SdoAbortError as e:
            self._abort(e.code)
        except Exception:  # noqa: BLE001
            log.exception("SDO write handler failed")
            self._abort(ABORT_GENERAL)
        return False

    def _on_frame(self, frame: CanFrame) -> None:
        if frame.is_remote or frame.is_error or len(frame.data) != 8:
            return
        d = frame.data
        if self._state == "block_down_seg":
            self._block_down_segment(d)
            return
        ccs = d[0] >> 5
        if ccs == 4:  # abort from client
            self._reset()
            return
        initiate = ccs in (1, 2) or (ccs == 5 and d[0] & 0x03 == 0) or (ccs == 6 and d[0] & 0x01 == 0)
        if initiate:
            self._reset()
            self._index, self._sub_index = struct.unpack_from("<HB", d, 1)
        handler = {
            1: self._initiate_download,
            2: self._initiate_upload,
            0: self._download_segment,
            3: self._upload_segment,
            5: self._block_upload,
            6: self._block_download,
        }.get(ccs)
        if handler is None:
            self._abort(ABORT_COMMAND)
            return
        handler(d)

    # ---- expedited / segmented ----
    def _initiate_upload(self, d: bytes) -> None:
        data = self._call_read()
        if data is None:
            return
        mux = _mux(self._index, self._sub_index)
        if 0 < len(data) <= 4:
            self._reply(_frame(bytes([0x43 | ((4 - len(data)) << 2)]), mux, data))
            self._reset()
            return
        self._buf = bytearray(data)
        self._pos = 0
        self._toggle = 0
        self._state = "upload_seg"
        self._reply(_frame(b"\x41", mux, _U32.pack(len(data))))

    def _upload_segment(self, d: bytes) -> None:
        if self._state != "upload_seg":
            self._abort(ABORT_COMMAND)
            return
        if (d[0] >> 4) & 1 != self._toggle:
            self._abort(ABORT_TOGGLE)
            return
        chunk = self._buf[self._pos:self._pos + 7]
        self._pos += 7
        last = self._pos >= len(self._buf)
        self._reply(_frame(bytes([(self._toggle << 4) | ((7 - len(chunk)) << 1) | int(last)]), bytes(chunk)))
        self._toggle ^= 1
        if last:
            self._reset()

    def _initiate_download(self, d: bytes) -> None:
        mux = _mux(self._index, self._sub_index)
        expedited, size_set = d[0] & 0x02, d[0] & 0x01
        if expedited:
            n = (d[0] >> 2) & 0x03 if size_set else 0
            if self._call_write(d[4:8 - n]):
                self._reply(_frame(b"\x60", mux))
                self._reset()
            return
        self._size = _U32.unpack_from(d, 4)[0] if size_set else None
        self._buf = bytearray()
        self._toggle = 0
        self._state = "download_seg"
        self._reply(_frame(b"\x60", mux))

    def _download_segment(self, d: bytes) -> None:
        if self._state != "download_seg":
            self._abort(ABORT_COMMAND)
            return
        if (d[0] >> 4) & 1 != self._toggle:
            self._abort(ABORT_TOGGLE)
            return
        n = (d[0] >> 1) & 0x07
        if n and not d[0] & 1:
            self._abort(ABORT_COMMAND)
            return
        self._buf += d[1:8 - n]
        if self._size is not None and len(self._buf) > self._size:
            self._abort(ABORT_LENGTH_HIGH)
            return
        toggle = self._toggle
        if d[0] & 0x01:
            if self._size is not None and len(self._buf) != self._size:
                self._abort(ABORT_LENGTH)
                return
            if not self._call_write(bytes(self._buf)):
                return
            self._reply(bytes([0x20 | (toggle << 4)]))
            self._reset()
            return
        self._reply(bytes([0x20 | (toggle << 4)]))
        self._toggle ^= 1

    # ---- block upload ----
    def _block_upload(self, d: bytes) -> None:
        cs = d[0] & 0x03
        if cs == 0 and self._state == "idle":
            blksize = d[4]
            if not 1 <= blksize <= 127:
                self._abort(ABORT_BLOCK_SIZE)
                return
            data = self._call_read()
            if data is None:
                return
            self._buf = bytearray(data)
            self._blksize = blksize
            self._crc = bool(d[0] & 0x04)
            self._pos = 0
            self._state = "block_up_wait_start"
            self._reply(_frame(bytes([0xC6 | (0x04 if self._crc else 0)]),
                               _mux(self._index, self._sub_index), _U32.pack(len(data))))
        elif cs == 3 and self._state == "block_up_wait_start":
            self._send_upload_block()
        elif cs == 2 and self._state == "block_up_data":
            ackseq, blksize = d[1], d[2]
            if not 1 <= blksize <= 127 or ackseq > self._block_sent:
                self._abort(ABORT_BLOCK_SIZE if not 1 <= blksize <= 127 else ABORT_SEQUENCE)
                return
            self._pos = self._block_start + ackseq * 7
            self._blksize = blksize
            total = max(1, -(-len(self._buf) // 7))
            if self._pos // 7 >= total:
                n = 7 - (len(self._buf) - (total - 1) * 7)
                crc = crc16(bytes(self._buf)) if self._crc else 0
                self._state = "block_up_end"
                self._reply(_frame(bytes([0xC1 | (n << 2)]), struct.pack("<H", crc)))
            else:
                self._send_upload_block()
        elif cs == 1 and self._state == "block_up_end":
            self._reset()
        else:
            self._abort(ABORT_COMMAND)

    def _send_upload_block(self) -> None:
        self._state = "block_up_data"
        self._block_start = self._pos
        total = max(1, -(-len(self._buf) // 7))
        first = self._pos // 7
        self._block_sent = 0
        for k in range(self._blksize):
            i = first + k
            if i >= total:
                break
            chunk = bytes(self._buf[i * 7:i * 7 + 7])
            last = i == total - 1
            self._reply(_frame(bytes([(0x80 if last else 0) | (k + 1)]), chunk))
            self._block_sent += 1

    # ---- block download ----
    def _block_download(self, d: bytes) -> None:
        cs = d[0] & 0x01
        if cs == 0 and self._state == "idle":
            self._crc = bool(d[0] & 0x04)
            self._size = _U32.unpack_from(d, 4)[0] if d[0] & 0x02 else None
            self._buf = bytearray()
            self._blksize = self.block_size
            self._expected = 1
            self._last = False
            self._state = "block_down_seg"
            self._reply(_frame(bytes([0xA4]), _mux(self._index, self._sub_index), bytes([self._blksize])))
        elif cs == 1 and self._state == "block_down_end":
            n = (d[0] >> 2) & 0x07
            if n:
                del self._buf[-n:]
            if self._size is not None and len(self._buf) != self._size:
                self._abort(ABORT_LENGTH)
                return
            if self._crc and struct.unpack_from("<H", d, 1)[0] != crc16(bytes(self._buf)):
                self._abort(ABORT_CRC)
                return
            if self._call_write(bytes(self._buf)):
                self._reply(b"\xA1")
                self._reset()
        else:
            self._abort(ABORT_COMMAND)

    def _block_down_segment(self, d: bytes) -> None:
        seq, last = d[0] & 0x7F, bool(d[0] & 0x80)
        if seq == 0:
            if d[0] == 0x80:
                self._reset()
            else:
                self._abort(ABORT_SEQUENCE)
            return
        if seq > self._blksize:
            self._abort(ABORT_SEQUENCE)
            return
        if seq == self._expected:
            self._buf += d[1:8]
            self._expected += 1
            if self._size is not None and len(self._buf) > self._size + (7 if self._size == 0 else 6):
                self._abort(ABORT_LENGTH_HIGH)
                return
            if last:
                self._last = True
        if seq >= self._blksize or last:
            self._reply(bytes([0xA2, self._expected - 1, self._blksize]))
            self._expected = 1
            if self._last:
                self._state = "block_down_end"
