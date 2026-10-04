"""Typed errors for sevcon_can."""
from __future__ import annotations

from typing import Optional


class SevconError(Exception):
    """Base error."""


class TransportError(SevconError):
    """CAN interface failure (interface missing, bus closed, send failed...)."""


class TimeoutError(SevconError):
    """No response within the allowed time."""


class ProtocolError(SevconError):
    """The remote node violated the CANopen protocol (bad command specifier,
    toggle bit, sequence number, CRC, size...)."""


class ObjectDictionaryError(SevconError, KeyError):
    """Unknown object, or an EDS/DCF that cannot be parsed."""

    def __str__(self) -> str:  # KeyError would otherwise repr() the message
        return str(self.args[0]) if self.args else ""


class AccessError(SevconError):
    """Local access check failed before transmission (read of a write-only
    object, write of a read-only object, value outside limits, insufficient
    Sevcon access level, or a pre-operational-only object written while
    operational)."""


class SdoAbortError(SevconError):
    """SDO transfer aborted by either side.

    ``code`` is the CiA 301 abort code. For ``0x08000000`` (general error)
    the Gen4 records a more specific reason in object 5310h; when the client
    reads it, ``sevcon_reason`` holds that number and ``sevcon_reason_text``
    its Table 33 description.
    """

    def __init__(self, code: int, index: int = 0, subindex: int = 0,
                 sevcon_reason: Optional[int] = None) -> None:
        from .tables import SDO_ABORT_CODES, GEN4_ABORT_REASONS
        self.code = code
        self.index = index
        self.subindex = subindex
        self.text = SDO_ABORT_CODES.get(code, "Unknown abort code")
        self.sevcon_reason = sevcon_reason
        self.sevcon_reason_text = (
            GEN4_ABORT_REASONS.get(sevcon_reason) if sevcon_reason is not None else None
        )
        msg = f"SDO abort 0x{code:08X} on 0x{index:04X}:{subindex:02X}: {self.text}"
        if self.sevcon_reason_text:
            msg += f" (Gen4 reason {sevcon_reason}: {self.sevcon_reason_text})"
        super().__init__(msg)


class StateError(SevconError):
    """A CiA 402 or NMT state transition did not reach the requested state."""
