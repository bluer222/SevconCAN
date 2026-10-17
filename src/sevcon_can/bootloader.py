"""Gen4 D0701 bootloader protocol derived from Sevcon DVT load.tcl.

Only the Gen4 checksum variant is implemented. Programming never enters or
exits bootloader mode implicitly. The first programmed block can erase its
memory section. Image compatibility with a particular motor/controller is
not established by an Intel HEX checksum or by matching memory geometry.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import hashlib
from pathlib import Path
import re
from typing import Dict, Optional, Sequence, Tuple, Union

from .exceptions import ProtocolError, SdoAbortError, SevconError, StateError
from .nmt import STATE_PREOP


@dataclass(frozen=True)
class _Section:
    name: str
    index: int
    minimum_size: int
    programming_timeout: float


# load_dld in the recovered 2014 DVT source, including its encrypted transports.
SECTIONS = {
    0: _Section("xc164-int", 0x5A20, 16, 10.0),
    1: _Section("dsp", 0x5A21, 256, 40.0),
    2: _Section("eeprom", 0x5A22, 1, 10.0),
    3: _Section("xc164-ext", 0x5A23, 1, 30.0),
    4: _Section("at49bv-ext", 0x5A23, 1, 30.0),
    5: _Section("dsp-zeffer", 0x5A24, 256, 60.0),
    6: _Section("flash-config-a", 0x5A25, 256, 30.0),
    7: _Section("flash-config-b", 0x5A26, 256, 30.0),
    8: _Section("display", 0x5A23, 256, 15.0),
    9: _Section("dsp-zeffer-encrypted", 0x5A27, 256, 60.0),
    10: _Section("iop-encrypted", 0x5A47, 256, 60.0),
}


def _section(name: str) -> _Section:
    for section in SECTIONS.values():
        if section.name == name:
            return section
    raise ValueError(f"unsupported bootloader memory section {name!r}")


def _uint(value: int, maximum: int, name: str) -> None:
    if type(value) is not int or not 0 <= value <= maximum:
        raise ValueError(f"{name} must be an integer in 0..{maximum}")


@dataclass(frozen=True)
class BootloaderIdentity:
    version: str
    revision: Optional[int]


@dataclass(frozen=True)
class ImageSection:
    memory_space: int
    start_address: int
    data: bytes

    def __post_init__(self):
        if type(self.memory_space) is not int or self.memory_space not in SECTIONS:
            raise ValueError("unsupported DLD memory space")
        _uint(self.start_address, 0xFFFFFFFF, "image start address")
        if not isinstance(self.data, (bytes, bytearray, memoryview)):
            raise ValueError("image data must be bytes-like")
        object.__setattr__(self, "data", bytes(self.data))
        if not self.data or self.start_address + len(self.data) > 0x100000000:
            raise ValueError("image must contain nonempty, bounded byte data")

    @property
    def name(self) -> str:
        return SECTIONS[self.memory_space].name

    @property
    def size(self) -> int:
        return len(self.data)


@dataclass(frozen=True)
class DldImage:
    sections: Tuple[ImageSection, ...]
    source_sha256: Optional[str] = None

    def __post_init__(self):
        object.__setattr__(self, "sections", tuple(self.sections))
        if not self.sections or any(not isinstance(s, ImageSection) for s in self.sections):
            raise ValueError("DLD must contain supported image sections")
        indices = [SECTIONS[s.memory_space].index for s in self.sections]
        if len(set(indices)) != len(indices):
            raise ValueError("duplicate or aliased DLD memory sections")


def _intel_hex(lines: Sequence[str]) -> Tuple[int, bytes]:
    base = None
    finished = False
    chunks = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        if finished:
            raise ValueError("Intel HEX data follows its EOF record")
        if not re.fullmatch(r":[0-9A-Fa-f]+", line) or len(line) % 2 != 1:
            raise ValueError("invalid Intel HEX record syntax")
        raw = bytes.fromhex(line[1:])
        if len(raw) < 5 or len(raw) != raw[0] + 5 or sum(raw) & 0xFF:
            raise ValueError("invalid Intel HEX record length or checksum")
        address, kind, data = int.from_bytes(raw[1:3], "big"), raw[3], raw[4:-1]
        if kind == 4:
            if address or len(data) != 2:
                raise ValueError("invalid Intel HEX extended linear address")
            base = int.from_bytes(data, "big") << 16
        elif kind == 1:
            if address or data:
                raise ValueError("invalid Intel HEX EOF record")
            finished = True
        elif kind == 0:
            if base is None:
                raise ValueError("Intel HEX data precedes its extended linear address")
            if base + address + len(data) > 0x100000000:
                raise ValueError("Intel HEX address exceeds 32 bits")
            if data:
                chunks.append((base + address, data))
        else:
            raise ValueError(f"unsupported Intel HEX record type {kind:02X}")
    if not finished or not chunks:
        raise ValueError("Intel HEX section needs data and an EOF record")
    chunks.sort()
    start = end = chunks[0][0]
    for address, data in chunks:
        if address != end:
            raise ValueError("Intel HEX section contains a gap or overlapping data")
        end += len(data)
    return start, b"".join(data for _, data in chunks)


def parse_dld(text: str) -> DldImage:
    """Parse the documented DLD/DLDx text container without executing content.

    Recognizes memory spaces 0..A and Intel HEX records 00/01/04. Sparse,
    overlapping, duplicate and unknown sections are rejected, as are missing
    or premature outer +EOF markers. Package headers
    do not provide a verified controller/motor compatibility check.
    """
    lines = text.splitlines()
    boundaries = [position for position, line in enumerate(lines) if line.strip() == "+EOF"]
    if len(boundaries) != 1 or any(line.strip() for line in lines[boundaries[0] + 1:]):
        raise ValueError("DLD needs one final EOF container marker")
    sections = []
    cursor = 0
    while cursor < len(lines):
        line = lines[cursor].strip()
        cursor += 1
        if line.startswith("+TARGET_MEMORY_SPACE"):
            match = re.fullmatch(r"\+TARGET_MEMORY_SPACE(?:\s+([0-9A-Fa-f]))?", line)
            if not match:
                raise ValueError("invalid DLD memory space directive")
            space = match[1]
            while cursor < len(lines) and not lines[cursor].strip():
                cursor += 1
            if space is None:
                if cursor == len(lines):
                    raise ValueError("DLD memory space value is missing")
                space = lines[cursor].strip()
                cursor += 1
            if not re.fullmatch(r"[0-9A-Fa-f]", space) or int(space, 16) not in SECTIONS:
                raise ValueError("unsupported DLD memory space")
            while cursor < len(lines) and not lines[cursor].strip():
                cursor += 1
            if cursor == len(lines) or lines[cursor].strip() != "+CODE":
                raise ValueError("DLD memory space needs a CODE section")
            cursor += 1
            first = cursor
            while cursor < len(lines) and lines[cursor].strip() != "+ENDCODE":
                cursor += 1
            if cursor == len(lines):
                raise ValueError("DLD code section is missing ENDCODE")
            start, data = _intel_hex(lines[first:cursor])
            sections.append(ImageSection(int(space, 16), start, data))
            cursor += 1
        elif line in ("+CODE", "+ENDCODE") or line.startswith(":"):
            raise ValueError("orphan DLD code or Intel HEX record")
    return DldImage(tuple(sections), hashlib.sha256(text.encode("utf-8")).hexdigest())


def load_dld(path: Union[str, Path]) -> DldImage:
    raw = Path(path).read_bytes()
    image = parse_dld(raw.decode("utf-8-sig"))
    return DldImage(image.sections, hashlib.sha256(raw).hexdigest())


@dataclass(frozen=True)
class MemoryCapabilities:
    section: str
    index: int
    max_block_size: int
    page_size: int
    start_address: int
    length: int

    def __post_init__(self):
        if self.index != _section(self.section).index:
            raise ValueError("memory section does not match its protocol index")
        for name in ("max_block_size", "page_size"):
            _uint(getattr(self, name), 0xFFFF, name)
        for name in ("start_address", "length"):
            _uint(getattr(self, name), 0xFFFFFFFF, name)
        if (not self.page_size or self.max_block_size < self.page_size or not self.length
                or self.length % self.page_size
                or self.start_address + self.length > 0x100000000):
            raise ValueError("invalid bootloader block/page/device geometry")


def packet_checksum(address: int, data: bytes) -> int:
    """Gen4's 16-bit one's complement sum of address, length and payload bytes."""
    _uint(address, 0xFFFFFFFF, "packet address")
    if not data or len(data) > 0xFFFF or address + len(data) > 0x100000000:
        raise ValueError("invalid bootloader packet length")
    return (0xFFFF - sum(address.to_bytes(4, "little"))
            - sum(len(data).to_bytes(2, "little")) - sum(data)) & 0xFFFF


@dataclass(frozen=True)
class ProgrammingPacket:
    section: str
    index: int
    address: int
    data: bytes
    checksum: int


@dataclass(frozen=True)
class PlannedSection:
    image: ImageSection
    capabilities: MemoryCapabilities
    packets: Tuple[ProgrammingPacket, ...]


@dataclass(frozen=True)
class ProgrammingPlan:
    sections: Tuple[PlannedSection, ...]
    bootloader: Optional[BootloaderIdentity] = None


def preflight_image(image: DldImage, capabilities: Sequence[MemoryCapabilities]) -> ProgrammingPlan:
    """Check every section and page before producing a program plan, without I/O.

    This Gen4 workflow requires contiguous data starting at the advertised
    device start. Final pages are zero-padded within the device, never clipped.
    """
    if not isinstance(image, DldImage):
        raise ValueError("programming needs a parsed DLD image")
    by_name = {caps.section: caps for caps in capabilities}
    if len(by_name) != len(capabilities):
        raise ValueError("duplicate section capabilities")
    result = []
    for section in image.sections:
        caps = by_name.get(section.name)
        if caps is None:
            raise ValueError(f"missing capabilities for {section.name}")
        if section.start_address != caps.start_address:
            raise ValueError(f"{section.name} image must start at the device start address")
        if section.size < _section(section.name).minimum_size:
            raise ValueError(f"{section.name} image is below the documented minimum size")
        padded_length = ((section.size + caps.page_size - 1) // caps.page_size) * caps.page_size
        if padded_length > caps.length:
            raise ValueError(f"{section.name} image or page padding exceeds the device")
        data = section.data + bytes(padded_length - section.size)
        block = caps.max_block_size // caps.page_size * caps.page_size
        packets = tuple(ProgrammingPacket(section.name, caps.index,
                        caps.start_address + offset, data[offset:offset + block],
                        packet_checksum(caps.start_address + offset, data[offset:offset + block]))
                        for offset in range(0, len(data), block))
        result.append(PlannedSection(section, caps, packets))
    return ProgrammingPlan(tuple(result))


@dataclass(frozen=True)
class PacketReceipt:
    section: str
    address: int
    length: int
    checksum: int


@dataclass(frozen=True)
class SectionReceipt:
    section: str
    bytes_written: int
    data_checksum: int


@dataclass(frozen=True)
class ProgrammingResult:
    """One program call's receipts and a snapshot of the shared session.

    complete describes this call. Earlier failed sections remain listed in
    pending_sections and prevent committing even after this call succeeds.
    """
    bootloader: BootloaderIdentity
    complete: bool
    packets: Tuple[PacketReceipt, ...]
    sections: Tuple[SectionReceipt, ...]
    session_checksum: Optional[int] = None
    pending_sections: Tuple[str, ...] = ()

    @property
    def commit_checksum(self) -> Optional[int]:
        if not self.complete or self.pending_sections:
            return None
        if self.session_checksum is not None:
            return self.session_checksum
        return sum(section.data_checksum for section in self.sections) & 0xFFFF


@dataclass
class _BootloaderState:
    # Physical protocol indices also identify the aliased external sections.
    checksums: Dict[int, int] = field(default_factory=dict)
    pending: Dict[int, str] = field(default_factory=dict)
    last_result: Optional[ProgrammingResult] = None

    def clear(self):
        self.checksums.clear()
        self.pending.clear()
        self.last_result = None


class BootloaderProgrammingError(SevconError):
    """Programming stopped; the failed write may have reached flash already."""

    def __init__(self, result: ProgrammingResult, section: str,
                 address: Optional[int], stage: str, cause: Exception):
        self.result, self.section, self.address = result, section, address
        self.stage, self.cause = stage, cause
        super().__init__(f"bootloader programming stopped in {section} at {stage} "
                         f"after {len(result.packets)} acknowledged packets: {cause}")


class Gen4Bootloader:
    """Explicit Gen4 bootloader operations over the device's existing SDO channel.

    Instances share device._bootloader_lock. State changes are not performed
    during identify/preflight. Program resets the download sequence after each
    successful section, but never exits bootloader or resets after an error.
    """

    def __init__(self, device):
        self.device = device
        if not hasattr(device, "_bootloader_state"):
            device._bootloader_state = _BootloaderState()
        self._state = device._bootloader_state

    @property
    def last_result(self) -> Optional[ProgrammingResult]:
        """Latest programming progress shared by every loader for this device."""
        return self._state.last_result

    def _progress(self, identity, complete, packets, sections) -> ProgrammingResult:
        self._state.last_result = ProgrammingResult(identity, complete, tuple(packets), tuple(sections),
            sum(self._state.checksums.values()) & 0xFFFF,
            tuple(self._state.pending[index] for index in sorted(self._state.pending)))
        return self._state.last_result

    async def _read(self, index: int, subindex: int, size: Optional[int] = None) -> bytes:
        self.device._check_open()
        data = await self.device.sdo.upload(index, subindex, timeout=1.0)
        if size is not None and len(data) != size:
            raise ProtocolError(f"0x{index:04X}:{subindex:02X} expected {size} bytes, got {len(data)}")
        return data

    async def _write(self, index: int, subindex: int, data: bytes, timeout: float = 1.0):
        self.device._check_open()
        await self.device.sdo.download(index, subindex, data, timeout=timeout)

    def _invalidate_application_state(self):
        # The mode change can happen even if its SDO acknowledgement is lost.
        self.device._access_level = None
        self.device._nmt_state = None

    async def _identify(self) -> BootloaderIdentity:
        try:
            raw = await self._read(0x100A, 0)
        except SdoAbortError as error:
            if error.code != 0x06020000:
                raise
            raw = await self._read(0x5A10, 0)
        try:
            version = raw.split(b"\x00", 1)[0].decode("ascii").strip()
        except UnicodeDecodeError as error:
            raise ProtocolError("bootloader identifier is not ASCII") from error
        match = re.fullmatch(r"D0701\.([0-9]{4}|----)(?:\s.*)?", version)
        if not match:
            raise StateError(f"unsupported bootloader {version!r}; expected Gen4 D0701")
        return BootloaderIdentity(version, int(match[1]) if match[1] != "----" else None)

    async def identify(self) -> BootloaderIdentity:
        async with self.device._bootloader_lock:
            return await self._identify()

    async def _capabilities(self, section: str) -> MemoryCapabilities:
        spec = _section(section)
        values = [int.from_bytes(await self._read(spec.index, sub, size), "little")
                  for sub, size in ((1, 2), (2, 2), (3, 4), (4, 4))]
        return MemoryCapabilities(section, spec.index, *values)

    async def capabilities(self, section: str) -> MemoryCapabilities:
        _section(section)
        async with self.device._bootloader_lock:
            await self._identify()
            return await self._capabilities(section)

    async def _preflight(self, image: DldImage) -> ProgrammingPlan:
        if not isinstance(image, DldImage):
            raise ValueError("programming needs a parsed DLD image")
        identity = await self._identify()
        caps = [await self._capabilities(section.name) for section in image.sections]
        return ProgrammingPlan(preflight_image(image, caps).sections, identity)

    async def preflight(self, image: DldImage) -> ProgrammingPlan:
        async with self.device._bootloader_lock:
            return await self._preflight(image)

    async def program(self, image: DldImage) -> ProgrammingResult:
        """Preflight all sections, program packets and reset each accepted section.

        An ACK for the packet checksum confirms the controller accepted that
        packet. A failed/cancelled operation can leave erased or partial flash.
        No retries, login, NMT transitions, bootloader entry or exit are implied.
        Cancellation propagates; last_result retains acknowledged progress.
        Successful calls accumulate the latest checksum for each section in
        this device session; an earlier failed section must be fully retried
        before end can commit the session.
        """
        async with self.device._bootloader_lock:
            plan = await self._preflight(image)
            identity = plan.bootloader
            packets, sections = [], []
            self._progress(identity, False, packets, sections)
            for section in plan.sections:
                address, stage = None, "address"
                self._state.pending[section.capabilities.index] = section.image.name
                self._progress(identity, False, packets, sections)
                try:
                    for packet in section.packets:
                        address = packet.address
                        for sub, stage, data in (
                                (5, "address", packet.address.to_bytes(4, "little")),
                                (6, "length", len(packet.data).to_bytes(2, "little")),
                                (7, "data", packet.data),
                                (8, "checksum", packet.checksum.to_bytes(2, "little"))):
                            timeout = _section(packet.section).programming_timeout if sub == 8 else 1.0
                            await self._write(packet.index, sub, data, timeout)
                        packets.append(PacketReceipt(packet.section, packet.address,
                                                     len(packet.data), packet.checksum))
                        self._progress(identity, False, packets, sections)
                    address, stage = None, "sequence_reset"
                    await self._write(0x5A32, 0, b"\x01")
                    receipt = SectionReceipt(section.image.name,
                        sum(len(packet.data) for packet in section.packets),
                        sum(sum(packet.data) for packet in section.packets) & 0xFFFF)
                    sections.append(receipt)
                    self._state.checksums[section.capabilities.index] = receipt.data_checksum
                    self._state.pending.pop(section.capabilities.index)
                    self._progress(identity, False, packets, sections)
                except asyncio.CancelledError:
                    raise
                except Exception as error:
                    raise BootloaderProgrammingError(self.last_result, section.image.name,
                                                     address, stage, error) from error
            return self._progress(identity, True, packets, sections)

    async def read_eeprom(self, *, start: Optional[int] = None,
                          length: Optional[int] = None) -> bytes:
        """Read EEPROM via its address/length/data selector; no flash writes.

        Other memory sections are not documented as readable and are not
        exposed by this helper. Returned bytes start at the requested address;
        no artificial zero prefix is inserted.
        """
        async with self.device._bootloader_lock:
            await self._identify()
            caps = await self._capabilities("eeprom")
            start = caps.start_address if start is None else start
            length = caps.length if length is None else length
            _uint(start, 0xFFFFFFFF, "EEPROM start")
            _uint(length, 0xFFFFFFFF, "EEPROM length")
            if (start < caps.start_address or start + length > caps.start_address + caps.length
                    or (start - caps.start_address) % caps.page_size or length % caps.page_size):
                raise ValueError("EEPROM read is outside the device or not page aligned")
            block = caps.max_block_size // caps.page_size * caps.page_size
            data = []
            for offset in range(0, length, block):
                size = min(block, length - offset)
                await self._write(caps.index, 5, (start + offset).to_bytes(4, "little"))
                await self._write(caps.index, 6, size.to_bytes(2, "little"))
                data.append(await self._read(caps.index, 7, size))
            return b"".join(data)

    async def start(self) -> None:
        """Request bootloader entry explicitly; caller handles login/NMT state."""
        async with self.device._bootloader_lock:
            self.device._check_open()
            if await self.device.nmt_state(cached=False) != STATE_PREOP:
                raise StateError("bootloader entry requires pre-operational; call goto_preop() first")
            try:
                await self.device.write(0x5400, 0, 0xB0)
            finally:
                self._invalidate_application_state()
            self._state.clear()

    async def reset(self) -> None:
        """Explicitly reset the bootloader download sequence, not the application."""
        async with self.device._bootloader_lock:
            await self._identify()
            await self._write(0x5A32, 0, b"\x01")

    async def end(self, checksum: int) -> None:
        """Explicitly commit/exit using the known image checksum, without guessing.

        The supplied checksum is the sum of the latest successfully programmed
        payload for each section in this device session, modulo 65536.
        Successful program results expose it as commit_checksum. This
        does not use the legacy write-FFFF/read-back fallback.
        """
        _uint(checksum, 0xFFFF, "commit checksum")
        async with self.device._bootloader_lock:
            if self._state.pending or (self.last_result is not None and not self.last_result.complete):
                raise StateError("cannot exit bootloader after incomplete programming")
            if self.last_result is not None and checksum != self.last_result.commit_checksum:
                raise ValueError("commit checksum differs from the completed programming result")
            await self._identify()
            try:
                await self._write(0x5A31, 0, checksum.to_bytes(2, "little"))
            finally:
                self._invalidate_application_state()
            self._state.clear()
