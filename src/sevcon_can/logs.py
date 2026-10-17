"""Structured diagnostic logs through the installed Gen4 scalar OD records.

The selector/count/reset procedures follow OVMS rt_sevcon_faults.cpp's
QueryLogs/ResetLogs and the supplied EDS. These are not parsers for 5600h
domain uploads. Times count controller operating hours, not UTC time.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional, TYPE_CHECKING

from .exceptions import ProtocolError

if TYPE_CHECKING:
    from .device import Reading


@dataclass(frozen=True)
class LogTimestamp:
    hours: int
    quarter_minutes: int

    @property
    def elapsed_seconds(self) -> int:
        return self.hours * 3600 + self.quarter_minutes * 15


@dataclass(frozen=True)
class FifoLogEntry:
    position: int
    event_id: int
    timestamp: LogTimestamp
    data: bytes


@dataclass(frozen=True)
class EventCounter:
    position: int
    event_id: int
    first: LogTimestamp
    last: LogTimestamp
    count: int


_FIFO = {"system": (0x4100, 0x4101, 0x4102),
         "faults": (0x4110, 0x4111, 0x4112)}
_RESET = {"system": 0x4100, "faults": 0x4110, "events": 0x4200,
          "customer": 0x4300, "sevcon": 0x4301}


def _bounds(start: int, limit: Optional[int]) -> None:
    if type(start) is not int or not 0 <= start <= 0xFFFF:
        raise ValueError("start must be an integer in 0..65535")
    if limit is not None and (type(limit) is not int or not 0 <= limit <= 0xFFFF):
        raise ValueError("limit must be an integer in 0..65535 or None")


def _positions(count: int, start: int, limit: Optional[int]) -> range:
    if not 0 <= count <= 0xFFFF:
        raise ProtocolError(f"invalid diagnostic log count {count}")
    if start > count:
        raise ValueError(f"start {start} exceeds log length {count}")
    return range(start, count if limit is None else min(count, start + limit))


async def _raw(device, index: int, subindex: int) -> int:
    return int((await device.read(index, subindex)).raw)


async def read_fifo_log(device, kind: str, *, start: int = 0,
                        limit: Optional[int] = None) -> List[FifoLogEntry]:
    """Read system/fault FIFO rows, selecting each position before its fields.

    All rows are read unless a limit is supplied. The 16-bit item count bounds
    the scan. Calls sharing a device serialize selection, reads and resets;
    faults arriving on the controller can still change the FIFO during a scan.
    """
    _bounds(start, limit)
    if kind not in _FIFO:
        raise ValueError("FIFO kind must be 'system' or 'faults'")
    control, selector, output = _FIFO[kind]
    async with device._diagnostic_log_lock:
        count = await _raw(device, control, 2)
        result = []
        for position in _positions(count, start, limit):
            await device.write(selector, 0, position)
            values = [await _raw(device, output, sub) for sub in range(1, 7)]
            result.append(FifoLogEntry(position, values[0],
                LogTimestamp(values[1], values[2]), bytes(values[3:6])))
        return result


async def read_event_counters(device, *, start: int = 0,
                              limit: Optional[int] = None) -> List[EventCounter]:
    """Read all ten event counters at 4201h..420Ah, including unused rows."""
    _bounds(start, limit)
    async with device._diagnostic_log_lock:
        result = []
        for position in _positions(10, start, limit):
            values = [await _raw(device, 0x4201 + position, sub) for sub in range(1, 7)]
            result.append(EventCounter(position, values[0],
                LogTimestamp(values[1], values[2]),
                LogTimestamp(values[3], values[4]), values[5]))
        return result


async def read_operational_monitor(device, *, sevcon: bool = False) -> Dict[str, "Reading"]:
    """Read all twelve customer/Sevcon monitor values, retaining OD units.

    Includes both motor-current channels. Reads of the Sevcon lifetime copy
    are separate from its level-5 reset permission.
    """
    index = 0x4301 if sevcon else 0x4300
    variables = [device.variable(index, sub) for sub in range(2, 14)]
    async with device._diagnostic_log_lock:
        return {var.name: await device.read(var.index, var.subindex) for var in variables}


async def reset_diagnostic_log(device, kind: str) -> None:
    """Explicitly clear one log; controller and selected EDS enforce access.

    Active faults are not cleared by these commands. No login, NMT transition
    or parameter-store operation is performed implicitly. Firmware may retain
    FIFO entries above the current access level; success does not mean empty.
    """
    if kind not in _RESET:
        raise ValueError("log kind must be 'system', 'faults', 'events', 'customer' or 'sevcon'")
    async with device._diagnostic_log_lock:
        await device.write(_RESET[kind], 1, True)
