"""CANopen NMT master and heartbeat handling (CiA 301 section 7.2.7).

The Gen4 is an NMT slave. This module implements the master side:

* :func:`nmt_command_frame` builds the 2-byte NMT command sent on COB-ID
  0x000 (first byte command, second byte node id, 0 = all nodes).
* :class:`HeartbeatMonitor` decodes heartbeat frames (COB-ID ``0x700`` +
  node, one state byte) and tracks the last known state of each node.
* :class:`NmtMaster` sends commands over a :class:`Network` and can wait
  for a node to reach a state, either via heartbeat or by polling the
  Gen4's ``5110h`` NMT-state object.

Gen4 specifics (Size 2/4/6 Product Manual, section 7.3):

* Object ``2800h`` (BOOLEAN): write ``1`` on the master node to force the
  network to pre-operational, ``0`` to restore operational. DVT does this
  automatically; other tools must do it before writing objects flagged
  ``WRITE_IN_PREOPERATIONAL_ONLY``.
* Object ``5110h`` mirrors the NMT state: 4 = stopped, 5 = operational,
  127 = pre-operational (same values as the heartbeat byte).
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Callable, Dict, Optional

from .exceptions import TimeoutError, TransportError
from .network import Network
from .transport import CanFrame

NMT_COB_ID = 0x000
HEARTBEAT_BASE = 0x700
EMCY_BASE = 0x080

# NMT commands (CiA 301 Table 38).
NMT_START = 0x01  #: enter operational
NMT_STOP = 0x02  #: enter stopped
NMT_PREOP = 0x80  #: enter pre-operational
NMT_RESET_NODE = 0x81  #: reset node
NMT_RESET_COMM = 0x82  #: reset communication

COMMAND_NAMES = {
    NMT_START: "start (operational)",
    NMT_STOP: "stop",
    NMT_PREOP: "pre-operational",
    NMT_RESET_NODE: "reset node",
    NMT_RESET_COMM: "reset communication",
}

# NMT states, shared by the heartbeat byte and Gen4 object 5110h.
STATE_BOOTUP = 0
STATE_STOPPED = 4
STATE_OPERATIONAL = 5
STATE_PREOP = 127

STATE_NAMES = {
    STATE_BOOTUP: "bootup",
    STATE_STOPPED: "stopped",
    STATE_OPERATIONAL: "operational",
    STATE_PREOP: "pre-operational",
}


def nmt_command_frame(command: int, node_id: int) -> CanFrame:
    """Build an NMT command frame (COB-ID 0x000)."""
    if command not in COMMAND_NAMES:
        raise ValueError(f"unknown NMT command 0x{command:02X}")
    if not 0 <= node_id <= 127:
        raise ValueError("node id must be 0..127 (0 = all nodes)")
    return CanFrame(NMT_COB_ID, bytes((command, node_id)))


def decode_heartbeat(frame: CanFrame) -> tuple[int, int]:
    """Decode a heartbeat frame into ``(node_id, state)``.

    Raises :class:`ValueError` for frames outside ``0x700..0x77F`` or with
    a payload other than one byte.
    """
    if frame.is_remote or frame.is_extended or frame.is_error:
        raise ValueError("heartbeat must be a standard data frame")
    node = frame.arbitration_id - HEARTBEAT_BASE
    if not 1 <= node <= 127 or len(frame.data) != 1:
        raise ValueError(f"not a heartbeat frame: {frame!r}")
    if frame.data[0] not in STATE_NAMES:
        raise ValueError("invalid heartbeat state")
    return node, frame.data[0]


def state_name(state: int) -> str:
    return STATE_NAMES.get(state, f"unknown({state})")


@dataclass
class HeartbeatEvent:
    node_id: int
    state: int
    timestamp: float

    @property
    def name(self) -> str:
        return state_name(self.state)


class HeartbeatMonitor:
    """Track heartbeat states of one or more nodes.

    Subscribe to ``0x700 + node`` for every watched node; the latest event
    per node is kept in :attr:`last`. :meth:`wait_state` blocks until a
    node reaches a state (or times out).
    """

    def __init__(self, network: Network) -> None:
        self.network = network
        self.last: Dict[int, HeartbeatEvent] = {}
        self._subs = []
        self._waiters: Dict[int, list[asyncio.Queue[HeartbeatEvent]]] = {}

    def watch(self, node_id: int) -> None:
        if not 1 <= node_id <= 127:
            raise ValueError("node id must be 1..127")
        if node_id in self._waiters:
            return
        queues: list[asyncio.Queue[HeartbeatEvent]] = []

        def on_frame(frame: CanFrame, node_id: int = node_id) -> None:
            try:
                _, state = decode_heartbeat(frame)
            except ValueError:
                return
            event = HeartbeatEvent(node_id, state, time.monotonic())
            self.last[node_id] = event
            for queue in tuple(queues):
                if queue.full():
                    queue.get_nowait()
                queue.put_nowait(event)

        self._subs.append(network_subscribe(self.network, HEARTBEAT_BASE + node_id, on_frame))
        self._waiters[node_id] = queues

    def unwatch(self, node_id: int) -> None:
        queue = self._waiters.pop(node_id, None)
        if queue is not None:
            for i, sub in enumerate(self._subs):
                if getattr(sub, "_hb_node", None) == node_id:
                    sub.cancel()
                    del self._subs[i]
                    break

    def state_of(self, node_id: int) -> Optional[int]:
        event = self.last.get(node_id)
        return event.state if event else None

    def age_of(self, node_id: int) -> Optional[float]:
        event = self.last.get(node_id)
        return time.monotonic() - event.timestamp if event else None

    async def wait_state(self, node_id: int, state: int, timeout: float = 5.0, *,
                         since: Optional[float] = None) -> HeartbeatEvent:
        """Wait until ``node_id`` reports ``state`` via heartbeat."""
        self.watch(node_id)
        current = self.last.get(node_id)
        if current is not None and current.state == state and (since is None or current.timestamp >= since):
            return current
        queue: asyncio.Queue[HeartbeatEvent] = asyncio.Queue(maxsize=256)
        queues = self._waiters[node_id]
        queues.append(queue)
        try:
            async def _wait() -> HeartbeatEvent:
                while True:
                    event = await queue.get()
                    if event.state == state and (since is None or event.timestamp >= since):
                        return event

            return await asyncio.wait_for(_wait(), timeout)
        except asyncio.TimeoutError:
            raise TimeoutError(
                f"node {node_id} did not reach {state_name(state)} within {timeout:g} s"
            ) from None
        finally:
            if queue in queues:
                queues.remove(queue)

    def close(self) -> None:
        for sub in self._subs:
            sub.cancel()
        self._subs.clear()
        self._waiters.clear()


def network_subscribe(network: Network, cob_id: int, callback: Callable) -> object:
    sub = network.subscribe(cob_id, callback)
    setattr(sub, "_hb_node", cob_id - HEARTBEAT_BASE)
    return sub


class NmtMaster:
    """Send NMT commands; optionally confirm via heartbeat or 5110h."""

    def __init__(self, network: Network, heartbeat: Optional[HeartbeatMonitor] = None) -> None:
        self.network = network
        self.heartbeat = heartbeat or HeartbeatMonitor(network)

    async def command(self, command: int, node_id: int = 0) -> None:
        try:
            await self.network.send(nmt_command_frame(command, node_id))
        except TransportError:
            raise
        except ValueError:
            raise

    async def start(self, node_id: int = 0) -> None:
        await self.command(NMT_START, node_id)

    async def stop(self, node_id: int = 0) -> None:
        await self.command(NMT_STOP, node_id)

    async def preop(self, node_id: int = 0) -> None:
        await self.command(NMT_PREOP, node_id)

    async def reset_node(self, node_id: int = 0) -> None:
        await self.command(NMT_RESET_NODE, node_id)

    async def reset_communication(self, node_id: int = 0) -> None:
        await self.command(NMT_RESET_COMM, node_id)

    async def wait_heartbeat_state(self, node_id: int, state: int, timeout: float = 5.0) -> HeartbeatEvent:
        return await self.heartbeat.wait_state(node_id, state, timeout)

    def close(self) -> None:
        self.heartbeat.close()
