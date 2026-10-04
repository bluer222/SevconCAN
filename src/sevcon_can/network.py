"""CAN network: one reader task, frames dispatched to subscribers by COB-ID.

All protocol objects (SDO client/server, NMT, heartbeat, EMCY, PDO, SYNC)
attach to a :class:`Network`. Subscriber callbacks run synchronously in the
reader task and must not block; they typically push into an
``asyncio.Queue`` or resolve a future.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Callable, Dict, List, Optional

from .exceptions import TransportError
from .transport import CanFrame, Transport

log = logging.getLogger(__name__)

Callback = Callable[[CanFrame], None]


class Subscription:
    """Handle returned by :meth:`Network.subscribe`; call :meth:`cancel` (or
    use as a context manager) to stop receiving."""

    def __init__(self, network: "Network", cob_id: Optional[int], callback: Callback) -> None:
        self._network = network
        self.cob_id = cob_id
        self.callback = callback
        self.active = True

    def cancel(self) -> None:
        if self.active:
            self.active = False
            self._network._remove(self)

    def __enter__(self) -> "Subscription":
        return self

    def __exit__(self, *exc: object) -> None:
        self.cancel()


class FrameQueue:
    """Bounded frame queue usable as a subscriber callback.

    When full the oldest frame is dropped (``dropped`` counts them), so a
    slow consumer never stalls the reader. :meth:`get` raises
    :class:`~sevcon_can.exceptions.TimeoutError` on timeout and
    :class:`TransportError` once the network reader has died.
    """

    def __init__(self, maxsize: int = 256) -> None:
        self._q: "asyncio.Queue[Optional[CanFrame]]" = asyncio.Queue(maxsize)
        self.dropped = 0
        self._error: Optional[BaseException] = None

    def __call__(self, frame: CanFrame) -> None:
        if self._q.full():
            try:
                self._q.get_nowait()
                self.dropped += 1
            except asyncio.QueueEmpty:  # pragma: no cover - race-free in one loop
                pass
        self._q.put_nowait(frame)

    def on_network_error(self, error: BaseException) -> None:
        self._error = error
        if self._q.full():
            self._q.get_nowait()
        self._q.put_nowait(None)

    def clear(self) -> None:
        while not self._q.empty():
            if self._q.get_nowait() is None:
                self._q.put_nowait(None)
                return

    def empty(self) -> bool:
        return self._q.empty()

    async def get(self, timeout: Optional[float] = None) -> CanFrame:
        from .exceptions import TimeoutError as SevconTimeout
        if self._error is not None and self._q.empty():
            raise TransportError(f"CAN reader stopped: {self._error}") from self._error
        try:
            if timeout is None:
                frame = await self._q.get()
            else:
                frame = await asyncio.wait_for(self._q.get(), timeout)
        except asyncio.TimeoutError:
            raise SevconTimeout(f"no CAN frame within {timeout:g} s") from None
        if frame is None:
            self._q.put_nowait(None)
            raise TransportError(f"CAN reader stopped: {self._error}") from self._error
        return frame


class Network:
    """Owns a :class:`~sevcon_can.transport.Transport` and its reader task.

    ``async with Network(transport) as net:`` starts the reader and closes
    the transport on exit.
    """

    def __init__(self, transport: Transport, *, close_transport: bool = True) -> None:
        self.transport = transport
        self._close_transport = close_transport
        self._subs: Dict[int, List[Subscription]] = {}
        self._any: List[Subscription] = []
        self._task: Optional["asyncio.Task[None]"] = None
        self._closed = False
        self.error: Optional[BaseException] = None
        self._error_frames: List[Subscription] = []
        self._send_lock = asyncio.Lock()
        self._extended_subs: Dict[int, List[Subscription]] = {}

    # ---- lifecycle ----
    async def start(self) -> None:
        if self._closed:
            raise TransportError("network is closed")
        if self._task is None:
            self._task = asyncio.get_running_loop().create_task(self._reader(), name="sevcon-can-reader")

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._notify_error(TransportError("network is closed"))
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
        if self._close_transport:
            await self.transport.close()

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def __aenter__(self) -> "Network":
        await self.start()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    # ---- I/O ----
    async def send(self, frame: CanFrame) -> None:
        if self._closed:
            raise TransportError("network is closed")
        if self.error is not None:
            raise TransportError(f"CAN reader stopped: {self.error}") from self.error
        async with self._send_lock:
            await self.transport.send(frame)

    async def send_message(self, cob_id: int, data: bytes = b"", *, remote: bool = False, extended: bool = False) -> None:
        await self.send(CanFrame(cob_id, bytes(data), is_remote=remote, is_extended=extended))

    def subscribe(self, cob_id: Optional[int], callback: Callback, *, extended: bool = False) -> Subscription:
        """Call ``callback(frame)`` for each received standard frame with this
        COB-ID, or for every frame when ``cob_id`` is None."""
        sub = Subscription(self, cob_id, callback)
        sub.extended = extended
        if cob_id is not None:
            limit = 0x1FFFFFFF if extended else 0x7FF
            if not 0 <= cob_id <= limit:
                raise ValueError("subscription CAN identifier out of range")
        if cob_id is None:
            self._any.append(sub)
        elif extended:
            self._extended_subs.setdefault(cob_id, []).append(sub)
        else:
            self._subs.setdefault(cob_id, []).append(sub)
        return sub

    def subscribe_errors(self, callback: Callback) -> Subscription:
        """Receive controller error frames (needs a transport opened with
        error frames enabled)."""
        sub = Subscription(self, -1, callback)
        self._error_frames.append(sub)
        return sub

    def _remove(self, sub: Subscription) -> None:
        if sub.cob_id is None:
            lst = self._any
        elif sub.cob_id == -1:
            lst = self._error_frames
        elif getattr(sub, "extended", False):
            lst = self._extended_subs.get(sub.cob_id, [])
        else:
            lst = self._subs.get(sub.cob_id, [])
        if sub in lst:
            lst.remove(sub)

    def dispatch(self, frame: CanFrame) -> None:
        """Deliver one frame to subscribers (used by the reader; public for
        transports that push frames themselves)."""
        if frame.is_error:
            targets = list(self._error_frames)
        elif frame.is_extended:
            targets = list(self._extended_subs.get(frame.arbitration_id, ())) + list(self._any)
        else:
            targets = list(self._subs.get(frame.arbitration_id, ())) + list(self._any)
        for sub in targets:
            if not sub.active:
                continue
            try:
                sub.callback(frame)
            except Exception:  # a faulty subscriber must not stop the bus
                log.exception("CAN subscriber for 0x%03X failed", frame.arbitration_id)

    async def _reader(self) -> None:
        try:
            while True:
                frame = await self.transport.recv()
                self.dispatch(frame)
        except asyncio.CancelledError:
            raise
        except BaseException as e:  # noqa: BLE001 - recorded and surfaced on next send
            if not self._closed:
                self.error = e
                log.error("CAN reader stopped: %s", e)
                self._notify_error(e)

    def _notify_error(self, error: BaseException) -> None:
        for lst in list(self._subs.values()) + list(self._extended_subs.values()) + [self._any]:
            for sub in list(lst):
                closer = getattr(sub.callback, "on_network_error", None)
                if closer is not None:
                    closer(error)
