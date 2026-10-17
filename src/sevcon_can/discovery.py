"""Read the installed controller's OD table through Sevcon 5630h.

5630h:1 reads the table length and selects a zero-based row when written.
5630h:5 uses Sevcon's internal data-type enumeration, not CiA type numbers.
The EDS defines the metadata and OVMS's dcf-download.pl confirms iteration.
No parameter values, names, scaling or login levels are inferred from it.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, AsyncIterator, Dict, Optional

from . import datatypes as dt
from .exceptions import ObjectDictionaryError, ProtocolError, SdoAbortError
from .od import parse_int
from .sdo import ABORT_NO_SUBINDEX

# Labels of 5630h:5 in the supplied Gen4 EDS.
DATA_TYPES = {
    0: dt.BOOLEAN, 1: dt.INTEGER8, 2: dt.INTEGER16, 3: dt.INTEGER32,
    4: dt.INTEGER64, 5: dt.UNSIGNED8, 6: dt.UNSIGNED16,
    7: dt.UNSIGNED32, 8: dt.UNSIGNED64, 9: dt.VISIBLE_STRING,
    10: dt.DOMAIN, 11: dt.OCTET_STRING, 12: dt.REAL32,
    13: dt.REAL64, 15: dt.DOMAIN,
}
ACCESS_TYPES = {0: "ro", 1: "wo", 2: "rw", 3: "const"}


@dataclass(frozen=True)
class DictionaryEntry:
    """One installed OD entry, retaining the controller's raw metadata.

    Limits are unsigned 32-bit wire values. They are not decoded as signed
    values or physical units, and flags are not interpreted as permissions.
    A 0..0 pair means the controller table stores no range for the entry.
    Unknown internal type/access codes remain available without guessing.
    """

    table_index: int
    index: int
    subindex: int
    object_type: int
    internal_data_type: int
    access_code: int
    low_limit_raw: int
    high_limit_raw: int
    pdo_mapping_raw: int
    object_version: int
    object_flags: Optional[int]
    dictionary_path: Optional[str]

    @property
    def data_type(self) -> Optional[int]:
        return DATA_TYPES.get(self.internal_data_type)

    @property
    def access(self) -> Optional[str]:
        return ACCESS_TYPES.get(self.access_code)

    def to_dict(self) -> Dict[str, Any]:
        result = asdict(self)
        result.update(data_type=self.data_type, access=self.access,
                      data_type_name=dt.NAMES.get(self.data_type))
        return result


def _bounds(start: int, limit: Optional[int]) -> None:
    if type(start) is not int or not 0 <= start <= 0xFFFF:
        raise ValueError("start must be an integer in 0..65535")
    if limit is not None and (type(limit) is not int or not 0 <= limit <= 0xFFFF):
        raise ValueError("limit must be an integer in 0..65535 or None")


async def _uint(device, subindex: int, size: int) -> int:
    raw = await device.read_raw(0x5630, subindex)
    if len(raw) != size:
        raise ProtocolError(f"5630h:{subindex:02X} metadata must be {size} bytes, got {len(raw)}")
    return int.from_bytes(raw, "little")


async def _entries(device, count: int, start: int,
                   limit: Optional[int]) -> AsyncIterator[DictionaryEntry]:
    if start > count:
        raise ValueError(f"start {start} exceeds controller OD table length {count}")
    end = count if limit is None else min(count, start + limit)
    seen = set()
    for cursor in range(start, end):
        # Keep selection and its metadata reads together across discovery calls.
        async with device._dictionary_lock:
            await device.write_raw(0x5630, 1, cursor.to_bytes(2, "little"))
            values = [await _uint(device, sub, size) for sub, size in (
                (2, 2), (3, 1), (4, 1), (5, 1), (6, 1),
                (7, 4), (8, 4), (9, 1), (10, 2))]
            try:
                flags = await _uint(device, 11, 1)
            except SdoAbortError as error:
                if error.code != ABORT_NO_SUBINDEX:
                    raise
                flags = None
            key = values[0], values[1]
            if values[0] == 0 or key in seen:
                raise ProtocolError(
                    f"invalid or repeated dictionary address {values[0]:04X}:{values[1]:02X} "
                    f"at table row {cursor}; the metadata cursor may not have advanced")
            seen.add(key)
            try:
                path = device.variable(values[0], values[1]).path
            except ObjectDictionaryError:
                path = None
            entry = DictionaryEntry(cursor, *values, flags, path)
        yield entry


async def iter_dictionary_entries(device, *, start: int = 0,
                                  limit: Optional[int] = None) -> AsyncIterator[DictionaryEntry]:
    """Enumerate at most the reported 16-bit table length, or ``limit`` rows.

    Only the 5630h:1 metadata cursor is written. Other applications using that
    same cursor must be idle while a row is read. State/login stay unchanged;
    any controller access restriction or SDO error is reported to the caller.
    """
    _bounds(start, limit)
    count = await _uint(device, 1, 2)
    async for entry in _entries(device, count, start, limit):
        yield entry


async def discover_dictionary(device, *, start: int = 0,
                              limit: Optional[int] = None) -> Dict[str, Any]:
    """Return installed OD metadata, missing entries and schema disagreements.

    A matching address alone does not verify the selected dictionary's type,
    version or limits. Scaling and units are not supplied by this service.
    """
    _bounds(start, limit)
    count = await _uint(device, 1, 2)
    entries = [entry.to_dict() async for entry in _entries(device, count, start, limit)]
    complete = start == 0 and len(entries) == count
    installed = {(entry["index"], entry["subindex"]) for entry in entries}
    mismatches = []
    for entry in entries:
        if entry["dictionary_path"] is None:
            continue
        var = device.variable(entry["index"], entry["subindex"])
        differences = {}
        expected_access = "rw" if var.access in ("rwr", "rww") else var.access
        expected = {"data_type": var.data_type, "access": expected_access,
                    "pdo_mapping_raw": int(var.pdo_mappable)}
        if "OBJECT_VERSION" in var.extra:
            expected["object_version"] = parse_int(var.extra["OBJECT_VERSION"])
        for name, value in expected.items():
            if entry[name] != value:
                differences[name] = {"dictionary": value, "controller": entry[name]}
        limits = (("low_limit_raw", var.low), ("high_limit_raw", var.high))
        if entry["low_limit_raw"] == entry["high_limit_raw"] == 0:
            # Gen4 reports 0..0 for rows without a stored range, even constants
            # holding nonzero values, so this pair cannot disagree with the EDS.
            limits = ()
        for name, limit_value in limits:
            if limit_value is None:
                continue
            bounds = dt.limits(var.data_type)
            if bounds is None:
                continue
            representations = {limit_value & 0xFFFFFFFF}
            if bounds[0] < 0:
                # Accept canonical zero/sign extension of a narrow signed
                # limit, without hiding unrelated high bits or unsigned errors.
                bits = min(dt.size_of(var.data_type), 4) * 8
                narrow = limit_value & ((1 << bits) - 1)
                representations.add(narrow)
                signed = narrow - (1 << bits) if narrow & (1 << (bits - 1)) else narrow
                representations.add(signed & 0xFFFFFFFF)
            if entry[name] not in representations:
                differences[name] = {"dictionary": limit_value, "controller": entry[name]}
        if differences:
            mismatches.append({"index": entry["index"], "subindex": entry["subindex"],
                               "dictionary_path": var.path, "differences": differences})
    return {"entry_count": count, "start": start,
            "complete": complete,
            "entries": entries,
            "missing_from_dictionary": [entry for entry in entries
                                        if entry["dictionary_path"] is None],
            "metadata_mismatches": mismatches,
            "absent_from_controller": [
                {"index": var.index, "subindex": var.subindex, "dictionary_path": var.path}
                for var in device.od.variables() if var.key not in installed] if complete else None}
