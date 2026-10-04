"""CANopen emergency (EMCY) messages with Sevcon fault decoding.

Wire format (CiA 301 section 7.2.6): COB-ID ``0x80 + node``, 8 bytes --

===========  =====================================================
bytes 0-1    EMCY error code, little-endian (see
             ``tables.EMCY_ERROR_CODES``)
byte 2       error register (CiA 301 Table 24, bit flags)
bytes 3-7    manufacturer-specific
===========  =====================================================

Sevcon layout of bytes 3-7, confirmed by two independent field
implementations (see ``tools/spec_parser.py::parse_emcy_fault_offsets``):

* OVMS ``rt_sevcon_faults.cpp`` reads the fault id as
  ``emcy.data[0] | emcy.data[1] << 8`` where its ``data[]`` starts after
  the 3-byte CiA header -- i.e. payload bytes 3-4.
* Victron ``sevcon.c`` reads ``mdata[3] | mdata[4] << 8`` directly off
  the 8-byte payload, with bytes 5-7 as extra data.

So: bytes 3-4 = Sevcon fault ID (``tables.GEN4_FAULTS`` key),
bytes 5-7 = additional info word. A fault ID of 0 means "no fault"
(the EMCY is purely the CiA error code).
"""
from __future__ import annotations

import asyncio
import struct
import time
from dataclasses import dataclass
from typing import AsyncIterator, Optional

from .exceptions import TransportError
from .network import Network
from .tables import EMCY_ERROR_CLASSES, EMCY_ERROR_CODES, GEN4_FAULTS
from .transport import CanFrame

EMCY_BASE = 0x080

# Error register bits (CiA 301 Table 24).
ERROR_REGISTER_BITS = {
    0x01: "generic error",
    0x02: "current",
    0x04: "voltage",
    0x08: "temperature",
    0x10: "communication error",
    0x20: "device profile specific",
    0x40: "reserved",
    0x80: "manufacturer specific",
}

_U16 = struct.Struct("<H")


@dataclass
class EmcyMessage:
    """One decoded EMCY frame."""

    node_id: int
    error_code: int
    error_register: int
    fault_id: int
    extra: bytes
    timestamp: float

    @property
    def error_text(self) -> str:
        return EMCY_ERROR_CODES.get(
            self.error_code,
            EMCY_ERROR_CLASSES.get(self.error_code & 0xFF00, "unknown EMCY error code"),
        )

    @property
    def register_flags(self) -> tuple[str, ...]:
        return tuple(
            name for bit, name in sorted(ERROR_REGISTER_BITS.items()) if self.error_register & bit
        )

    @property
    def fault(self):
        return GEN4_FAULTS.get(self.fault_id) if self.fault_id else None

    @property
    def fault_text(self) -> Optional[str]:
        fault = self.fault
        return fault.message if fault else None

    def __repr__(self) -> str:
        base = f"EmcyMessage(node={self.node_id} code=0x{self.error_code:04X} {self.error_text!r}"
        if self.fault_id:
            base += f" fault=0x{self.fault_id:04X} {self.fault_text!r}"
        return base + ")"


def decode_emcy(frame: CanFrame) -> EmcyMessage:
    """Decode a raw EMCY frame. Raises :class:`ValueError` if the COB-ID
    is outside ``0x81..0xFF`` or the payload is not 8 bytes."""
    if frame.is_remote or frame.is_extended or frame.is_error:
        raise ValueError("EMCY must be a standard data frame")
    node_id = frame.arbitration_id - EMCY_BASE
    if not 1 <= node_id <= 127:
        raise ValueError(f"not an EMCY frame: {frame!r}")
    if len(frame.data) != 8:
        raise ValueError(f"EMCY payload must be 8 bytes, got {len(frame.data)}: {frame!r}")
    error_code = _U16.unpack_from(frame.data, 0)[0]
    error_register = frame.data[2]
    fault_id = _U16.unpack_from(frame.data, 3)[0]
    return EmcyMessage(
        node_id=node_id,
        error_code=error_code,
        error_register=error_register,
        fault_id=fault_id,
        extra=bytes(frame.data[5:8]),
        timestamp=frame.timestamp,
    )


def encode_emcy(node_id: int, error_code: int, error_register: int,
                fault_id: int = 0, extra: bytes = b"\x00\x00\x00") -> CanFrame:
    """Build an EMCY frame (used by the simulator and tests)."""
    if not 1 <= node_id <= 127:
        raise ValueError("node id must be 1..127")
    extra = bytes(extra).ljust(3, b"\x00")[:3]
    return CanFrame(
        EMCY_BASE + node_id,
        _U16.pack(error_code) + bytes((error_register,)) + _U16.pack(fault_id) + extra,
    )


class EmcyConsumer:
    """Queue decoded EMCY messages from one node (or every node).

    Mirrors ``ve_direct``'s ``async_notifications()``: frames received
    while the caller does something else are queued (bounded, oldest
    dropped) and :meth:`notifications` yields them forever.
    """

    def __init__(self, network: Network, node_id: Optional[int] = None, maxsize: int = 256) -> None:
        self.network = network
        self.node_id = node_id
        self.queue: "asyncio.Queue[Optional[EmcyMessage]]" = asyncio.Queue(maxsize)
        self.dropped = 0
        self._error: Optional[BaseException] = None
        if node_id is None:
            self._sub = network.subscribe(None, self)
        else:
            self._sub = network.subscribe(EMCY_BASE + node_id, self)

    def __call__(self, frame: CanFrame) -> None:
        if self._error is None:
            self._on_frame(frame)

    def on_network_error(self, error: BaseException) -> None:
        if self._error is not None:
            return
        self._error = error
        if self.queue.full():
            self.queue.get_nowait()
        self.queue.put_nowait(None)

    def _on_frame(self, frame: CanFrame) -> None:
        if not EMCY_BASE + 1 <= frame.arbitration_id <= EMCY_BASE + 127:
            return
        if frame.is_extended or len(frame.data) != 8:
            return
        try:
            message = decode_emcy(frame)
        except ValueError:
            return
        if self.queue.full():
            try:
                self.queue.get_nowait()
                self.dropped += 1
            except Exception:
                pass
        self.queue.put_nowait(message)

    async def notifications(self) -> AsyncIterator[EmcyMessage]:
        while True:
            message = await self.queue.get()
            if message is None:
                self.queue.put_nowait(None)
                raise TransportError(f"CAN consumer stopped: {self._error}") from self._error
            yield message

    def close(self) -> None:
        self.on_network_error(TransportError("consumer is closed"))
        self._sub.cancel()
