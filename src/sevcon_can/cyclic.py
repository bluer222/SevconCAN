"""Host heartbeat and SYNC producers on an existing CAN network.

The host heartbeat node ID must differ from every controller node ID.
Configure the controller's heartbeat consumer and SYNC COB-ID separately.
These producers implement CANopen messages, not a motor control policy.
"""
from __future__ import annotations

import asyncio
import math
from typing import Optional

from .network import Network
from .nmt import HEARTBEAT_BASE, STATE_OPERATIONAL, STATE_STOPPED, STATE_PREOP
from .pdo import COB_ID_EXTENDED_BIT, SYNC_COB_ID, cob_id_can_id
from .transport import CanFrame


class _PeriodicProducer:
    def __init__(self, network: Network, period_ms: float) -> None:
        if not math.isfinite(period_ms) or period_ms <= 0:
            raise ValueError("period_ms must be finite and positive")
        self.network = network
        self.period_ms = period_ms
        self._task: Optional[asyncio.Task[None]] = None
        self._closed = False

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def _frame(self) -> CanFrame:
        raise NotImplementedError

    async def start(self) -> None:
        if self._closed:
            raise RuntimeError("producer is closed")
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name=type(self).__name__)
        elif self._task.done():
            # Retrieve and report send failures rather than restarting silently.
            await self._task

    async def _run(self) -> None:
        loop = asyncio.get_running_loop()
        period = self.period_ms / 1000
        deadline = loop.time()
        while True:
            await self.network.send(self._frame())
            deadline += period
            now = loop.time()
            if deadline <= now:
                # Skip missed intervals; never burst old heartbeat or SYNC frames.
                deadline = now + period
            await asyncio.sleep(max(0, deadline - now))

    async def close(self) -> None:
        self._closed = True
        if self._task is None:
            return
        task, self._task = self._task, None
        cancelled_here = not task.done()
        if cancelled_here:
            task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            if not cancelled_here:
                raise

    async def __aenter__(self):
        await self.start()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()


class HeartbeatProducer(_PeriodicProducer):
    """Produce the host's heartbeat at ``period_ms`` intervals.

    ``state`` may be 4, 5 or 127. Set it while running to update subsequent
    messages. ``bootup=True`` sends one initial boot-up message before the
    periodic heartbeat. Use a host node ID distinct from the controller.
    """

    def __init__(self, network: Network, node_id: int, *, period_ms: float = 100,
                 state: int = STATE_OPERATIONAL, bootup: bool = False) -> None:
        super().__init__(network, period_ms)
        if not 1 <= node_id <= 127:
            raise ValueError("host node id must be 1..127")
        self.node_id = node_id
        self.state = state
        self.bootup = bootup

    @property
    def state(self) -> int:
        return self._state

    @state.setter
    def state(self, value: int) -> None:
        if value not in (STATE_STOPPED, STATE_OPERATIONAL, STATE_PREOP):
            raise ValueError("heartbeat state must be stopped, operational or pre-operational")
        self._state = value

    async def start(self) -> None:
        if self._closed:
            raise RuntimeError("producer is closed")
        if self._task is None and self.bootup:
            await self.network.send(CanFrame(HEARTBEAT_BASE + self.node_id, b"\x00"))
        await super().start()

    def _frame(self) -> CanFrame:
        return CanFrame(HEARTBEAT_BASE + self.node_id, bytes((self.state,)))


class SyncProducer(_PeriodicProducer):
    """Produce SYNC frames at ``period_ms`` intervals.

    ``cob_id`` accepts object 1005h's full COB-ID, including bit 29 for an
    extended identifier and bit 30 for the producer flag. Configure every
    node to consume that identifier. ``counter_overflow=None`` sends empty
    SYNC frames; a value 1..240 sends counters cycling from 1 to that value.
    """

    def __init__(self, network: Network, *, period_ms: float = 20,
                 cob_id: int = SYNC_COB_ID,
                 counter_overflow: Optional[int] = None) -> None:
        super().__init__(network, period_ms)
        if cob_id & 0x80000000:
            raise ValueError("SYNC COB-ID bit 31 is reserved")
        self._can_id = cob_id_can_id(cob_id)
        self.cob_id = cob_id
        if counter_overflow is not None and not 1 <= counter_overflow <= 240:
            raise ValueError("SYNC counter overflow must be 1..240 or None")
        self.counter_overflow = counter_overflow
        self._counter = 0

    def _frame(self) -> CanFrame:
        data = b""
        if self.counter_overflow is not None:
            self._counter = self._counter % self.counter_overflow + 1
            data = bytes((self._counter,))
        return CanFrame(self._can_id, data, is_extended=bool(self.cob_id & COB_ID_EXTENDED_BIT))
