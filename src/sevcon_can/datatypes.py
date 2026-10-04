"""CANopen basic data types (CiA 301 section 7.1, Table 44).

All numeric types are little-endian on the wire. INTEGER24/40/48/56 and
UNSIGNED24/40/48/56 are packed in the minimum number of bytes.
"""
from __future__ import annotations

import struct
from typing import Dict, Optional, Union

BOOLEAN = 0x0001
INTEGER8 = 0x0002
INTEGER16 = 0x0003
INTEGER32 = 0x0004
UNSIGNED8 = 0x0005
UNSIGNED16 = 0x0006
UNSIGNED32 = 0x0007
REAL32 = 0x0008
VISIBLE_STRING = 0x0009
OCTET_STRING = 0x000A
UNICODE_STRING = 0x000B
TIME_OF_DAY = 0x000C
TIME_DIFFERENCE = 0x000D
DOMAIN = 0x000F
INTEGER24 = 0x0010
REAL64 = 0x0011
INTEGER40 = 0x0012
INTEGER48 = 0x0013
INTEGER56 = 0x0014
INTEGER64 = 0x0015
UNSIGNED24 = 0x0016
UNSIGNED40 = 0x0018
UNSIGNED48 = 0x0019
UNSIGNED56 = 0x001A
UNSIGNED64 = 0x001B

NAMES: Dict[int, str] = {
    BOOLEAN: "BOOLEAN", INTEGER8: "INTEGER8", INTEGER16: "INTEGER16",
    INTEGER32: "INTEGER32", UNSIGNED8: "UNSIGNED8", UNSIGNED16: "UNSIGNED16",
    UNSIGNED32: "UNSIGNED32", REAL32: "REAL32", VISIBLE_STRING: "VISIBLE_STRING",
    OCTET_STRING: "OCTET_STRING", UNICODE_STRING: "UNICODE_STRING",
    TIME_OF_DAY: "TIME_OF_DAY", TIME_DIFFERENCE: "TIME_DIFFERENCE",
    DOMAIN: "DOMAIN", INTEGER24: "INTEGER24", REAL64: "REAL64",
    INTEGER40: "INTEGER40", INTEGER48: "INTEGER48", INTEGER56: "INTEGER56",
    INTEGER64: "INTEGER64", UNSIGNED24: "UNSIGNED24", UNSIGNED40: "UNSIGNED40",
    UNSIGNED48: "UNSIGNED48", UNSIGNED56: "UNSIGNED56", UNSIGNED64: "UNSIGNED64",
}

#: (size in bytes, signed) for integer types.
_INTS: Dict[int, tuple] = {
    BOOLEAN: (1, False),
    INTEGER8: (1, True), INTEGER16: (2, True), INTEGER24: (3, True),
    INTEGER32: (4, True), INTEGER40: (5, True), INTEGER48: (6, True),
    INTEGER56: (7, True), INTEGER64: (8, True),
    UNSIGNED8: (1, False), UNSIGNED16: (2, False), UNSIGNED24: (3, False),
    UNSIGNED32: (4, False), UNSIGNED40: (5, False), UNSIGNED48: (6, False),
    UNSIGNED56: (7, False), UNSIGNED64: (8, False),
}

STRING_TYPES = frozenset({VISIBLE_STRING, OCTET_STRING, UNICODE_STRING, DOMAIN})
INTEGER_TYPES = frozenset(_INTS)
FLOAT_TYPES = frozenset({REAL32, REAL64})

Value = Union[int, float, bool, str, bytes]


def size_of(data_type: int) -> Optional[int]:
    """Wire size in bytes, or None for variable-length types."""
    if data_type in _INTS:
        return _INTS[data_type][0]
    if data_type == REAL32:
        return 4
    if data_type == REAL64:
        return 8
    if data_type in (TIME_OF_DAY, TIME_DIFFERENCE):
        return 6
    return None


def bit_length(data_type: int) -> Optional[int]:
    """PDO mapping length in bits (BOOLEAN maps as 1 bit per CiA 301)."""
    if data_type == BOOLEAN:
        return 1
    s = size_of(data_type)
    return None if s is None else s * 8


def limits(data_type: int) -> Optional[tuple]:
    """(min, max) of an integer type."""
    if data_type not in _INTS:
        return None
    if data_type == BOOLEAN:
        return (0, 1)
    size, signed = _INTS[data_type]
    bits = size * 8
    if signed:
        return (-(1 << (bits - 1)), (1 << (bits - 1)) - 1)
    return (0, (1 << bits) - 1)


def decode(data_type: int, data: bytes) -> Value:
    """Decode raw SDO/PDO bytes into a Python value."""
    if data_type in _INTS:
        size, signed = _INTS[data_type]
        if len(data) < size:
            raise ValueError(f"{NAMES.get(data_type, data_type)} needs {size} bytes, got {len(data)}")
        v = int.from_bytes(data[:size], "little", signed=signed)
        return bool(v & 1) if data_type == BOOLEAN else v
    if data_type == REAL32:
        return struct.unpack("<f", data[:4])[0]
    if data_type == REAL64:
        return struct.unpack("<d", data[:8])[0]
    if data_type == VISIBLE_STRING:
        return data.split(b"\x00", 1)[0].decode("latin-1")
    if data_type == UNICODE_STRING:
        return data.decode("utf-16-le").split("\x00", 1)[0]
    return bytes(data)


def encode(data_type: int, value: Value) -> bytes:
    """Encode a Python value into raw SDO/PDO bytes."""
    if data_type in _INTS:
        if isinstance(value, float):
            if not value.is_integer():
                raise ValueError(f"{NAMES[data_type]} needs an integer, got {value!r}")
            value = int(value)
        if not isinstance(value, (int, bool)):
            raise TypeError(f"{NAMES[data_type]} needs an integer, got {type(value).__name__}")
        lo, hi = limits(data_type)  # type: ignore[misc]
        iv = int(value)
        if not lo <= iv <= hi:
            raise ValueError(f"{iv} out of range for {NAMES[data_type]} [{lo}, {hi}]")
        size, signed = _INTS[data_type]
        return iv.to_bytes(size, "little", signed=signed)
    if data_type == REAL32:
        return struct.pack("<f", float(value))  # type: ignore[arg-type]
    if data_type == REAL64:
        return struct.pack("<d", float(value))  # type: ignore[arg-type]
    if data_type == VISIBLE_STRING:
        return value.encode("latin-1") if isinstance(value, str) else bytes(value)  # type: ignore[arg-type]
    if data_type == UNICODE_STRING:
        return value.encode("utf-16-le") if isinstance(value, str) else bytes(value)  # type: ignore[arg-type]
    if isinstance(value, str):
        raise TypeError(f"{NAMES.get(data_type, data_type)} needs bytes")
    return bytes(value)  # type: ignore[arg-type]
