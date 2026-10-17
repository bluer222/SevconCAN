"""Structured diagnostic logs through the installed Gen4 OD records.

The selector/count/reset procedures follow OVMS rt_sevcon_faults.cpp's
QueryLogs/ResetLogs and the supplied EDS. The monitor-domain layout also
follows Sevcon DVT and the physical 0705 controller's scalar/domain dumps.
Times count controller operating hours, not UTC time.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
import math
from typing import Any, Dict, List, Optional, TYPE_CHECKING

from . import datatypes as dt
from .exceptions import ProtocolError, TimeoutError

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
    saved: bool = False

    @property
    def raw_event_id(self) -> int:
        """The complete 4201h..420Ah:1 word, including its saved flag."""
        return self.event_id | (0x8000 if self.saved else 0)


@dataclass(frozen=True)
class MonitorDomain:
    values: Dict[str, "Reading"]
    scaling_conflicts: Dict[str, Dict[str, Any]]
    raw: bytes


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
    """Read all ten event counters, separating bit 15's saved indication.

    The remaining fifteen bits identify the event. This encoding follows
    Sevcon DVT's ``uut.tcl`` get_event_log procedure; ``raw_event_id`` retains
    the original word. Unused rows are included.
    """
    _bounds(start, limit)
    async with device._diagnostic_log_lock:
        result = []
        for position in _positions(10, start, limit):
            values = [await _raw(device, 0x4201 + position, sub) for sub in range(1, 7)]
            result.append(EventCounter(position, values[0] & 0x7FFF,
                LogTimestamp(values[1], values[2]),
                LogTimestamp(values[3], values[4]), values[5],
                saved=bool(values[0] & 0x8000)))
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


async def read_operational_monitor_domain(device, *, sevcon: bool = False) -> MonitorDomain:
    """Read the verified 0705 twelve-word monitor domain at 5600h:4/:5.

    The payload contains the same raw values, in order, as 4300h/4301h:2..D;
    it has no length prefix. DVT adds a four-byte prefix in its own SDO API.
    Other product families and payload sizes are rejected because their
    layouts can differ. Names, signed types, scaling and units come from the
    selected scalar OD. Voltage interpretations that conflict with DVT are
    reported with both conversions; matching raw layouts does not establish
    which physical conversion applies to this controller.
    """
    from .device import Reading

    index, subindex = (0x4301, 5) if sevcon else (0x4300, 4)
    variables = [device.variable(index, sub) for sub in range(2, 14)]
    if any(var.data_type != dt.INTEGER16 for var in variables):
        raise ProtocolError("0705 monitor domains require twelve INTEGER16 scalar entries")
    async with device._diagnostic_log_lock:
        vendor = await _raw(device, 0x1018, 1)
        product = await _raw(device, 0x1018, 2)
        if vendor != 0x1E or product >> 16 != 0x0705:
            raise ProtocolError("monitor-domain layout is verified only for Sevcon product family 0705")
        data = await device.read_raw(0x5600, subindex)
        if len(data) != 24:
            raise ProtocolError(f"0705 monitor domain must contain 24 bytes, received {len(data)}")
        result, conflicts = {}, {}
        for position, var in enumerate(variables):
            word = data[position * 2:position * 2 + 2]
            raw = var.decode_raw(word)
            result[var.name] = Reading(var, raw, var.to_physical(raw),
                                       var.describe(raw), var.units)
            if position < 4:
                # DVT uses unsigned 12.4 voltage words, while the supplied
                # 0705 EDS declares signed 8.8 scalar values. Preserve both.
                conflicts[var.name] = {
                    "dictionary_scale": var.scaling if var.scaling is not None else 1,
                    "dvt_scale": 1 / 16,
                    "dictionary_signed": True,
                    "dvt_signed": False,
                    "dictionary_physical": var.to_physical(raw),
                    "dvt_physical": int.from_bytes(word, "little") / 16,
                }
        return MonitorDomain(result, conflicts, bytes(data))


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


async def reset_all_diagnostic_logs(device, *, timeout: float = 31,
                                    poll_interval: float = 1) -> None:
    """Explicitly clear all diagnostic logs, timers and operating-hour counters.

    This destructive reset writes 4000h:1 and waits for the controller to
    return its reset flag to zero, following DVT's clear_all_logs procedure.
    The selected OD and controller enforce access; no login or NMT change is
    performed. The supplied EDS requires level 5. The timeout includes lock
    acquisition, the write, SDO reads and polling delays. A timeout does not
    establish whether the controller completed the reset.
    """
    for name, value in (("timeout", timeout), ("poll_interval", poll_interval)):
        if (isinstance(value, bool) or not isinstance(value, (int, float))
                or not math.isfinite(value) or value <= 0):
            raise ValueError(f"{name} must be finite and positive")

    async def reset_and_wait():
        async with device._diagnostic_log_lock:
            await device.write(0x4000, 1, True)
            while await _raw(device, 0x4000, 1) != 0:
                await asyncio.sleep(poll_interval)

    try:
        await asyncio.wait_for(reset_and_wait(), timeout)
    except asyncio.TimeoutError:
        raise TimeoutError("timed out waiting for the 4000h:1 diagnostic reset flag to clear") from None
