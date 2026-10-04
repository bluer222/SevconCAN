"""CAN transports.

A transport moves raw CAN frames. Three implementations are provided:

* :class:`SocketCanTransport` - Linux SocketCAN through the standard library
  (``socket.AF_CAN``) and the asyncio event loop. No dependencies.
* :class:`PythonCanTransport` - any interface supported by ``python-can``
  (PCAN, Kvaser, IXXAT, Vector, slcan, gs_usb, ...). Optional dependency.
* :class:`VirtualBus` / :class:`VirtualTransport` - in-memory bus for tests
  and the :mod:`sevcon_can.simulator`.

Custom transports implement the :class:`Transport` protocol: ``send``,
``recv`` and ``close`` coroutines.
"""
from __future__ import annotations

import asyncio
import errno
import socket
import struct
import sys
import time
from dataclasses import dataclass, field
from typing import Any, List, Optional

from .exceptions import TransportError

CAN_EFF_FLAG = 0x80000000
CAN_RTR_FLAG = 0x40000000
CAN_ERR_FLAG = 0x20000000
CAN_SFF_MASK = 0x000007FF
CAN_EFF_MASK = 0x1FFFFFFF

_CAN_FRAME = struct.Struct("=IB3x8s")


@dataclass
class CanFrame:
    """One classic CAN 2.0 frame."""

    arbitration_id: int
    data: bytes = b""
    is_extended: bool = False
    is_remote: bool = False
    is_error: bool = False
    timestamp: float = field(default_factory=time.monotonic)
    dlc: Optional[int] = None

    def __post_init__(self) -> None:
        self.data = bytes(self.data)
        if len(self.data) > 8:
            raise ValueError("classic CAN frames carry at most 8 data bytes")
        if self.dlc is None:
            self.dlc = len(self.data)
        if not 0 <= self.dlc <= 8:
            raise ValueError("classic CAN DLC must be between 0 and 8")
        if not self.is_remote and self.dlc != len(self.data):
            raise ValueError("data frame DLC must equal payload length")
        limit = CAN_EFF_MASK if self.is_extended or self.is_error else CAN_SFF_MASK
        if not 0 <= self.arbitration_id <= limit:
            raise ValueError(f"arbitration id 0x{self.arbitration_id:X} out of range")

    def __repr__(self) -> str:
        kind = "x" if self.is_extended else ""
        rtr = " RTR" if self.is_remote else ""
        err = " ERR" if self.is_error else ""
        return f"CanFrame(0x{self.arbitration_id:03X}{kind} [{len(self.data)}] {self.data.hex(' ')}{rtr}{err})"


class Transport:
    """Async CAN transport protocol."""

    async def send(self, frame: CanFrame) -> None:  # pragma: no cover - interface
        raise NotImplementedError

    async def recv(self) -> CanFrame:  # pragma: no cover - interface
        """Wait for the next frame. Raises TransportError once closed."""
        raise NotImplementedError

    async def close(self) -> None:  # pragma: no cover - interface
        raise NotImplementedError


# ---------------------------------------------------------------------------
# SocketCAN (stdlib)
# ---------------------------------------------------------------------------

def pack_socketcan(frame: CanFrame) -> bytes:
    can_id = frame.arbitration_id
    if frame.is_extended:
        can_id |= CAN_EFF_FLAG
    if frame.is_remote:
        can_id |= CAN_RTR_FLAG
    if frame.is_error:
        can_id |= CAN_ERR_FLAG
    return _CAN_FRAME.pack(can_id, frame.dlc, frame.data.ljust(8, b"\x00"))


def unpack_socketcan(raw: bytes, timestamp: Optional[float] = None) -> CanFrame:
    if len(raw) != _CAN_FRAME.size:
        raise TransportError("expected a 16-byte classic SocketCAN frame")
    can_id, dlc, data = _CAN_FRAME.unpack(raw)
    if dlc > 8:
        raise TransportError("invalid classic CAN DLC")
    is_ext = bool(can_id & CAN_EFF_FLAG)
    return CanFrame(
        arbitration_id=can_id & (CAN_EFF_MASK if is_ext or can_id & CAN_ERR_FLAG else CAN_SFF_MASK),
        data=b"" if can_id & CAN_RTR_FLAG else data[:dlc],
        dlc=dlc,
        is_extended=is_ext,
        is_remote=bool(can_id & CAN_RTR_FLAG),
        is_error=bool(can_id & CAN_ERR_FLAG),
        timestamp=time.monotonic() if timestamp is None else timestamp,
    )


class SocketCanTransport(Transport):
    """Linux SocketCAN raw socket driven by the asyncio loop.

    The interface bitrate is configured outside this process, e.g.::

        sudo ip link set can0 type can bitrate 1000000 restart-ms 100
        sudo ip link set can0 up

    Gen4 defaults to 1 Mbit/s (manual Table 5); the configured rate is in
    object 5900h.

    ``error_frames=True`` delivers controller error frames (bus-off,
    error-passive...) as frames with ``is_error=True``.

    While the kernel TX queue is full (``ENOBUFS``), sends are retried for up
    to ``tx_queue_timeout`` seconds before failing.
    """

    def __init__(self, channel: str, *, error_frames: bool = False,
                 receive_own_messages: bool = False, tx_queue_timeout: float = 0.5) -> None:
        self.tx_queue_timeout = tx_queue_timeout
        if not sys.platform.startswith("linux") or not hasattr(socket, "AF_CAN"):
            raise TransportError("SocketCAN requires Linux")
        self.channel = channel
        try:
            sock = socket.socket(socket.AF_CAN, socket.SOCK_RAW, socket.CAN_RAW)
        except OSError as e:
            raise TransportError(f"cannot create CAN socket: {e}") from e
        try:
            if error_frames:
                sock.setsockopt(socket.SOL_CAN_RAW, socket.CAN_RAW_ERR_FILTER, struct.pack("=I", 0x1FFFFFFF))
            if receive_own_messages:
                sock.setsockopt(socket.SOL_CAN_RAW, socket.CAN_RAW_RECV_OWN_MSGS, 1)
            sock.bind((channel,))
            sock.setblocking(False)
        except OSError as e:
            sock.close()
            raise TransportError(f"cannot open SocketCAN interface {channel!r}: {e}") from e
        self._sock = sock
        self._closed = False

    async def send(self, frame: CanFrame) -> None:
        if self._closed:
            raise TransportError("transport is closed")
        loop = asyncio.get_running_loop()
        data = pack_socketcan(frame)
        deadline = loop.time() + self.tx_queue_timeout
        while True:
            try:
                await loop.sock_sendall(self._sock, data)
                return
            except OSError as e:
                # ENOBUFS: kernel TX queue full (burst, bus-off or no ACK).
                if e.errno == errno.ENOBUFS and loop.time() < deadline:
                    await asyncio.sleep(0.001)
                    continue
                raise TransportError(f"CAN send failed: {e}") from e

    async def recv(self) -> CanFrame:
        if self._closed:
            raise TransportError("transport is closed")
        loop = asyncio.get_running_loop()
        try:
            raw = await loop.sock_recv(self._sock, 16)
        except OSError as e:
            raise TransportError(f"CAN receive failed: {e}") from e
        if not raw:
            raise TransportError("CAN socket closed")
        return unpack_socketcan(raw)

    async def close(self) -> None:
        if not self._closed:
            self._closed = True
            self._sock.close()


# ---------------------------------------------------------------------------
# python-can
# ---------------------------------------------------------------------------

class PythonCanTransport(Transport):
    """Adapter for any ``python-can`` bus.

    Pass either an existing ``can.BusABC`` instance or keyword arguments for
    ``can.Bus(...)``, e.g. ``PythonCanTransport(interface="pcan",
    channel="PCAN_USBBUS1", bitrate=1000000)``.
    """

    def __init__(self, bus: Any = None, **bus_kwargs: Any) -> None:
        try:
            import can  # type: ignore[import-not-found]
        except ImportError as e:
            raise TransportError("python-can not installed; pip install 'sevcon-can[python-can]'") from e
        self._loop = asyncio.get_running_loop()
        self._can = can
        self._owns_bus = bus is None
        bus_kwargs.setdefault("ignore_config", True)
        try:
            self._bus = bus if bus is not None else can.Bus(**bus_kwargs)
        except Exception as e:  # python-can raises many interface-specific types
            raise TransportError(f"cannot open python-can bus: {e}") from e
        self._queue: "asyncio.Queue[Optional[CanFrame]]" = asyncio.Queue()
        self._closed = False
        self._error: Optional[Exception] = None
        transport = self

        class Listener(can.Listener):
            def on_message_received(self, msg):
                transport._on_message(msg)

            def on_error(self, error):
                transport._on_error(error)

        try:
            self._notifier = can.Notifier(self._bus, [Listener()], timeout=0.1, loop=self._loop)
        except Exception as e:
            if self._owns_bus:
                self._bus.shutdown()
            raise TransportError(f"cannot start python-can receiver: {e}") from e

    def _on_error(self, error: Exception) -> None:
        self._error = error
        self._loop.call_soon_threadsafe(self._queue.put_nowait, None)

    def _on_message(self, msg: Any) -> None:
        if self._closed:
            return
        # CANopen Gen4 uses classic CAN. Do not silently truncate CAN FD.
        if getattr(msg, "is_fd", False):
            return
        try:
            frame = CanFrame(
                arbitration_id=msg.arbitration_id,
                data=bytes(msg.data),
                is_extended=bool(msg.is_extended_id),
                is_remote=bool(msg.is_remote_frame),
                is_error=bool(msg.is_error_frame),
                dlc=msg.dlc,
            )
        except ValueError as e:
            self._on_error(e)
            return
        self._loop.call_soon_threadsafe(self._queue.put_nowait, frame)

    async def send(self, frame: CanFrame) -> None:
        if self._closed:
            raise TransportError("transport is closed")
        msg = self._can.Message(
            arbitration_id=frame.arbitration_id,
            data=frame.data,
            is_extended_id=frame.is_extended,
            is_remote_frame=frame.is_remote,
            is_error_frame=frame.is_error,
            dlc=frame.dlc,
        )
        try:
            await self._loop.run_in_executor(None, lambda: self._bus.send(msg, timeout=0.1))
        except self._can.CanError as e:
            raise TransportError(f"CAN send failed: {e}") from e

    async def recv(self) -> CanFrame:
        if self._closed:
            raise TransportError("transport is closed")
        if self._error is not None:
            raise TransportError(f"CAN receive failed: {self._error}") from self._error
        frame = await self._queue.get()
        if frame is None:
            self._queue.put_nowait(None)
            if self._error is not None:
                raise TransportError(f"CAN receive failed: {self._error}") from self._error
            raise TransportError("transport is closed")
        return frame

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._queue.put_nowait(None)
        error = None
        try:
            await asyncio.to_thread(self._notifier.stop)
        except Exception as e:
            error = e
        try:
            if self._owns_bus:
                await asyncio.to_thread(self._bus.shutdown)
        except Exception as e:
            if error is None:
                error = e
        if error is not None:
            raise TransportError(f"CAN close failed: {error}") from error


# ---------------------------------------------------------------------------
# Virtual bus
# ---------------------------------------------------------------------------

class VirtualBus:
    """In-memory CAN bus. Every frame sent by one endpoint is delivered to
    all other endpoints (like SocketCAN without RECV_OWN_MSGS)."""

    def __init__(self) -> None:
        self._endpoints: List["VirtualTransport"] = []
        self.log: List[CanFrame] = []
        self.log_enabled = False

    def endpoint(self) -> "VirtualTransport":
        ep = VirtualTransport(self)
        self._endpoints.append(ep)
        return ep

    def _deliver(self, sender: "VirtualTransport", frame: CanFrame) -> None:
        if self.log_enabled:
            self.log.append(frame)
        for ep in self._endpoints:
            if ep is not sender and not ep._closed:
                ep._queue.put_nowait(CanFrame(
                    frame.arbitration_id, frame.data, frame.is_extended,
                    frame.is_remote, frame.is_error, dlc=frame.dlc,
                ))

    def _detach(self, ep: "VirtualTransport") -> None:
        if ep in self._endpoints:
            self._endpoints.remove(ep)


class VirtualTransport(Transport):
    def __init__(self, bus: VirtualBus) -> None:
        self._bus = bus
        self._queue: "asyncio.Queue[Optional[CanFrame]]" = asyncio.Queue()
        self._closed = False
        self.sent: List[CanFrame] = []

    async def send(self, frame: CanFrame) -> None:
        if self._closed:
            raise TransportError("transport is closed")
        self.sent.append(frame)
        self._bus._deliver(self, frame)

    async def recv(self) -> CanFrame:
        if self._closed:
            raise TransportError("transport is closed")
        frame = await self._queue.get()
        if frame is None:
            raise TransportError("transport is closed")
        return frame

    async def close(self) -> None:
        if not self._closed:
            self._closed = True
            self._queue.put_nowait(None)
            self._bus._detach(self)
