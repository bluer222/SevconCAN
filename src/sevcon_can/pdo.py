"""CANopen PDO handling (CiA 301 section 7.2.2) for the Gen4.

The Gen4 provides 5 RPDOs and 5 TPDOs. Each PDO is described by a
communication record (``1400h..1404h`` for RPDO, ``1800h..1804h`` for
TPDO) and a mapping record (``1600h..1604h`` / ``1A00h..1A04h``):

* comm sub 1: COB-ID (bit 31 clear = valid; bits 0-10 the CAN id).
  EDS defaults such as ``0x80000202`` mean "disabled" (bit 31 set).
* comm sub 2: transmission type (0 = acyclic sync, 1..240 = every nth
  SYNC, 254/255 = event driven).
* comm sub 3: inhibit time (100 us steps, TPDO).
* comm sub 5: event timer (ms, TPDO; 0 = disabled).
* mapping sub 0: number of mapped objects; sub 1-8: one u32 each,
  ``index << 16 | subindex << 8 | length_bits``.

This module encodes/decodes PDO payloads against such a mapping. The
data-type sizes come from the live object dictionary so Sevcon's
``BIT_SPLIT`` fields and BOOLEAN-as-1-bit mappings are handled. Use
:class:`sevcon_can.device.AsyncSevconGen4` for the exchange with a real
controller; the helpers here are pure functions plus thin async readers.
"""
from __future__ import annotations

import struct
from dataclasses import dataclass
from typing import AsyncIterator, List, Optional, Sequence, Tuple

from . import datatypes as dt
from .exceptions import ObjectDictionaryError, ProtocolError, TransportError
from .network import Network
from .od import ObjectDictionary
from .transport import CanFrame

# Predefined connection set (CiA 301 Table 36).
RPDO_BASE = (0x200, 0x300, 0x400, 0x500)
TPDO_BASE = (0x180, 0x280, 0x380, 0x480)
SYNC_COB_ID = 0x080

COB_ID_VALID_BIT = 0x80000000
COB_ID_EXTENDED_BIT = 0x20000000
COB_ID_RTR_BIT = 0x40000000

TRANSMISSION_TYPES = {
    0: "acyclic (sync)",
    **{n: f"every {n}th SYNC" for n in range(1, 241)},
    254: "event (manufacturer specific)",
    255: "event (device profile specific)",
}

MappingEntry = Tuple[int, int, int]  # (index, subindex, length_bits)


def decode_mapping_word(word: int) -> MappingEntry:
    return (word >> 16) & 0xFFFF, (word >> 8) & 0xFF, word & 0xFF


def encode_mapping_word(index: int, subindex: int, length_bits: int) -> int:
    if not 0 <= index <= 0xFFFF or not 0 <= subindex <= 0xFF or not 1 <= length_bits <= 64:
        raise ValueError("mapping requires index 0..65535, subindex 0..255 and length 1..64")
    return (index << 16) | (subindex << 8) | length_bits


def cob_id_valid(cob_id: int) -> bool:
    return not cob_id & COB_ID_VALID_BIT


def cob_id_can_id(cob_id: int) -> int:
    if not 0 <= cob_id <= 0xFFFFFFFF:
        raise ValueError("COB-ID must be an unsigned 32-bit value")
    if not cob_id & COB_ID_EXTENDED_BIT and cob_id & 0x1FFFF800:
        raise ValueError("standard COB-ID contains identifier bits above bit 10")
    return cob_id & (0x1FFFFFFF if cob_id & COB_ID_EXTENDED_BIT else 0x7FF)


def default_cob_id(is_tpdo: bool, pdo_number: int, node_id: int) -> int:
    """Predefined-connection-set COB-ID for PDO ``pdo_number`` (0-based)."""
    if not 1 <= node_id <= 127:
        raise ValueError("node id must be 1..127")
    if not 0 <= pdo_number < 4:
        raise ValueError("only PDOs 1..4 have predefined COB-IDs; read the configured COB-ID")
    return (TPDO_BASE if is_tpdo else RPDO_BASE)[pdo_number] + node_id


@dataclass
class MappedValue:
    index: int
    subindex: int
    raw: object
    bits: int


def read_mapping(od: ObjectDictionary, is_tpdo: bool, pdo_number: int) -> List[MappingEntry]:
    """Read a PDO mapping from the object dictionary (mapping records only).

    ``pdo_number`` is 0-based (PDO 1 = 0). Raises
    :class:`ObjectDictionaryError` for an unknown PDO number.
    """
    base = 0x1A00 if is_tpdo else 0x1600
    index = base + pdo_number
    if index not in od.objects:
        raise ObjectDictionaryError(f"PDO mapping 0x{index:04X} not in dictionary")
    entry = od.objects[index]
    words: List[int] = []
    if hasattr(entry, "subs"):
        count = entry.subs[0].value() if 0 in entry.subs else len(entry.subs) - 1
        for sub in range(1, int(count or 0) + 1):
            if sub in entry.subs:
                words.append(int(entry.subs[sub].value()))
    return [decode_mapping_word(w) for w in words if w]


def expected_length(mapping: Sequence[MappingEntry]) -> int:
    validate_mapping(mapping)
    return (sum(length for _, _, length in mapping) + 7) // 8


def validate_mapping(mapping: Sequence[MappingEntry]) -> None:
    """Reject invalid or over-sized classic CAN PDO mappings."""
    if len(mapping) > 64:
        raise ProtocolError("PDO mapping contains more than 64 entries")
    for index, subindex, length in mapping:
        if not 0 <= index <= 0xFFFF or not 0 <= subindex <= 0xFF or not 1 <= length <= 64:
            raise ProtocolError("invalid PDO mapping entry")
    if sum(length for _, _, length in mapping) > 64:
        raise ProtocolError("PDO mapping exceeds the classic CAN 64-bit payload")


def _expected_bits(od: ObjectDictionary, index: int, subindex: int) -> Optional[int]:
    try:
        var = od.variable((index, subindex))
    except ObjectDictionaryError:
        return None
    return dt.bit_length(var.data_type)


def decode_pdo(data: bytes, mapping: Sequence[MappingEntry],
               od: Optional[ObjectDictionary] = None) -> List[MappedValue]:
    """Decode up to 8 payload bytes against ``mapping``.

    Values are returned as integers (bit fields are extracted, byte
    fields little-endian). When ``od`` is given, each entry's mapped
    length is cross-checked against the data type; a mismatch raises
    :class:`ProtocolError` instead of silently misaligning the payload.
    """
    validate_mapping(mapping)
    if len(data) > 8:
        raise ProtocolError("PDO payload exceeds 8 bytes")
    bits = int.from_bytes(bytes(data).ljust(8, b"\x00")[:8], "little")
    total = sum(length for _, _, length in mapping)
    if len(data) * 8 < total:
        raise ProtocolError(f"PDO payload too short: {len(data)} bytes for {total} bits")
    out: List[MappedValue] = []
    pos = 0
    for index, subindex, length in mapping:
        if od is not None:
            want = _expected_bits(od, index, subindex)
            if want is not None and want != length and not (
                    want == 1 and length == 1):
                # BOOLEAN maps as 1 bit; allow an 8-bit mapping of a
                # BOOLEAN only if explicitly configured (skip check).
                if not (want == 1 and length == 8):
                    raise ProtocolError(
                        f"PDO map 0x{index:04X}:{subindex:02X} length {length} "
                        f"bits does not match {want}-bit data type")
        raw = (bits >> pos) & ((1 << length) - 1 if length < 64 else (1 << 64) - 1)
        # Sign-extend using the dictionary type when known.
        if od is not None and length in (8, 16, 24, 32):
            try:
                var = od.variable((index, subindex))
                lim = dt.limits(var.data_type)
                if lim is not None and lim[0] < 0 and raw >= (1 << (length - 1)):
                    raw -= 1 << length
            except ObjectDictionaryError:
                pass
        out.append(MappedValue(index, subindex, raw, length))
        pos += length
    return out


def encode_pdo(values: Sequence[tuple[int, int, int]],
               mapping: Sequence[MappingEntry]) -> bytes:
    """Encode ``values`` (``(index, subindex, raw)``) against ``mapping``.

    Entries must appear in mapping order; signed values are stored two's
    complement. Returns 1-8 payload bytes.
    """
    validate_mapping(mapping)
    if len(values) != len(mapping):
        raise ValueError(f"{len(values)} values for {len(mapping)} mapped objects")
    bits = 0
    pos = 0
    for (index, subindex, raw), (mi, ms, length) in zip(values, mapping):
        if (index, subindex) != (mi, ms):
            raise ValueError(f"value 0x{index:04X}:{subindex:02X} out of mapping order "
                             f"(want 0x{mi:04X}:{ms:02X})")
        if not -(1 << (length - 1)) <= int(raw) < (1 << length):
            raise ValueError(f"value {raw} does not fit in {length} bits")
        mask = (1 << length) - 1
        bits |= (int(raw) & mask) << pos
        pos += length
    return bits.to_bytes((pos + 7) // 8, "little")


def decode_sync_counter(data: bytes) -> Optional[int]:
    """SYNC payload is empty or carries a 1-byte counter (CiA 301 7.2.1)."""
    if len(data) == 0:
        return None
    if len(data) == 1:
        return data[0]
    raise ProtocolError(f"SYNC payload must be 0-1 bytes, got {len(data)}")


class PdoConsumer:
    """Decode TPDO frames of one PDO into :class:`MappedValue` lists."""

    def __init__(self, network: Network, cob_id: int,
                 mapping: Sequence[MappingEntry],
                 od: Optional[ObjectDictionary] = None, maxsize: int = 256) -> None:
        import asyncio
        if not cob_id_valid(cob_id):
            raise ValueError("cannot subscribe to a disabled PDO")
        validate_mapping(mapping)
        can_id = cob_id_can_id(cob_id)
        self.cob_id = cob_id
        self.mapping = list(mapping)
        self.od = od
        self.queue: "asyncio.Queue[Optional[list[MappedValue]]]" = asyncio.Queue(maxsize)
        self.dropped = 0
        self._error: Optional[BaseException] = None
        self._sub = network.subscribe(can_id, self, extended=bool(cob_id & COB_ID_EXTENDED_BIT))

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
        if frame.is_remote or frame.is_error:
            return
        try:
            decoded = decode_pdo(frame.data, self.mapping, self.od)
        except ProtocolError:
            return
        if self.queue.full():
            try:
                self.queue.get_nowait()
                self.dropped += 1
            except Exception:
                pass
        self.queue.put_nowait(decoded)

    async def updates(self) -> AsyncIterator[List[MappedValue]]:
        while True:
            message = await self.queue.get()
            if message is None:
                self.queue.put_nowait(None)
                raise TransportError(f"CAN consumer stopped: {self._error}") from self._error
            yield message

    def close(self) -> None:
        self.on_network_error(TransportError("consumer is closed"))
        self._sub.cancel()
