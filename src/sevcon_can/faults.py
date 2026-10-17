"""Sevcon fault access: table lookup plus live controller reads.

Two layers, mirroring ``ve_direct``'s generated tables + device access:

* Static: :func:`lookup`, :func:`search`, :func:`led_category` over
  ``tables.GEN4_FAULTS`` (245 entries merged from Sevcon's 2013 fault
  spreadsheet, OVMS and Victron -- see ``tools/generate_tables.py``).
* Live: :func:`read_active_faults` / :func:`describe_fault` implement the
  manual's no-DVT procedure (Size 2/4/6 Product Manual, Fault
  identification): ``5300h`` sub 1 counts active faults, write the
  position to sub 2 and read the fault id from sub 3; ``5610h``
  translates an id to the controller's own text (write id to sub 1,
  read sub 2).

Fault id layout: high byte = level/LED group context, low byte = index.
``tables.GEN4_LED_CATEGORIES`` maps the LED flash count (manual
Table 34) to a category such as ``"Battery faults"``.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional

from .od import normalize_name
from .tables import GEN4_FAULTS, GEN4_LED_CATEGORIES, FaultInfo


def lookup(fault_id: int) -> FaultInfo:
    """Return the fault record for ``fault_id`` (raises ``KeyError``)."""
    try:
        return GEN4_FAULTS[fault_id]
    except KeyError:
        raise KeyError(f"unknown Gen4 fault id 0x{fault_id:04X}") from None


def search(text: str) -> List[FaultInfo]:
    """Case-insensitive substring search over messages, descriptions and
    display codes."""
    needle = text.lower()
    out = []
    for fault in GEN4_FAULTS.values():
        hay = " ".join((fault.message, fault.description, fault.display,
                        fault.type, *(fault.aliases or ()))).lower()
        if needle in hay:
            out.append(fault)
    return sorted(out, key=lambda f: f.fid)


def led_category(flashes: int) -> str:
    try:
        return GEN4_LED_CATEGORIES[flashes]
    except KeyError:
        raise KeyError(f"unknown LED flash count {flashes}") from None


def by_level(level: int) -> List[FaultInfo]:
    return sorted((f for f in GEN4_FAULTS.values() if f.level == level), key=lambda f: f.fid)


@dataclass
class ActiveFault:
    position: int
    fault_id: int
    info: Optional[FaultInfo]
    controller_text: Optional[str] = None


async def read_active_faults(device: "AsyncSevconGen4") -> List[ActiveFault]:  # noqa: F821
    """Read the live active-fault list via ``5300h``.

    Serialize selection and reads among callers sharing this device. The
    controller's live fault list can still change during the scan.
    """
    async with device._fault_list_lock:
        count = int(await device.read_physical(0x5300, 1))
        faults: List[ActiveFault] = []
        for position in range(count):
            await device.write(0x5300, 2, position)
            fid = int(await device.read_physical(0x5300, 3))
            faults.append(ActiveFault(position, fid, GEN4_FAULTS.get(fid)))
        return faults


async def describe_fault(device: "AsyncSevconGen4", fault_id: int) -> str:  # noqa: F821
    """Ask the controller for its own text of ``fault_id`` via ``5610h``."""
    async with device._fault_text_lock:
        await device.write(0x5610, 1, fault_id)
        return str(await device.read_physical(0x5610, 2))
