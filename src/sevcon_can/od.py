"""Object dictionary model and EDS/DCF parser (CiA 306 with Sevcon extensions).

Sevcon's DVT writes metadata as ``;SEVCONFIELD KEY=VALUE`` comment lines
inside each section. They are parsed here because they carry information the
plain EDS lacks:

* ``SCALING`` / ``UNITS`` - physical value = raw * SCALING, in UNITS.
* ``ACCESS_LEVEL`` - minimum Sevcon login level (5000h,1) needed to write.
* ``WRITE_IN_PREOPERATIONAL_ONLY=TRUE`` - writable only in NMT pre-op.
* ``<value>=<label>`` lines - enumeration labels.
* ``NUMBER_FORMAT=BIT_SPLIT`` followed by ``SPLIT=n`` blocks with
  ``PARAMETERNAME``, ``BITMASK``, ``BITSHIFT`` and their own labels -
  bitfield definitions.
* ``OBJECT`` - Sevcon's symbolic name (e.g. ``STATUSWORD``).
* ``SECTION`` / ``CATEGORY`` (CONFIGURATION, MONITORING, LOGGING).

``configparser`` drops comment lines, so this module reads EDS files line by
line.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterator, List, Optional, Tuple, Union

from . import datatypes as dt
from .exceptions import ObjectDictionaryError

VAR = 7
ARRAY = 8
RECORD = 9
DEFSTRUCT = 6
DEFTYPE = 5
DOMAIN_OBJ = 2

READABLE = frozenset({"ro", "rw", "rwr", "rww", "const"})
WRITABLE = frozenset({"wo", "rw", "rwr", "rww"})

#: Keys that always belong to the object even after a SPLIT block started.
_OBJECT_KEYS = frozenset({
    "OBJECT_VERSION", "ACCESS_LEVEL", "EEPROM", "UNITS", "SCALING", "NUMBER_FORMAT",
    "CATEGORY", "SECTION", "OBJECT", "WRITE_IN_PREOPERATIONAL_ONLY", "MASTER",
    "NUMBER_SPLIT", "SOFT_HIGH", "SOFT_LOW", "COMMS_TIMEOUT", "DUMMY_VPDO",
})
#: Keys that belong to the current SPLIT block.
_SPLIT_KEYS = frozenset({
    "PARAMETERNAME", "BITMASK", "BITSHIFT", "LOW_LIMIT", "HIGH_LIMIT", "NUMBERFORMAT",
})

_SECTION_RE = re.compile(r"^\[([^\]]+)\]\s*$")
_INDEX_RE = re.compile(r"^([0-9A-Fa-f]{4})$")
_SUB_RE = re.compile(r"^([0-9A-Fa-f]{4})sub([0-9A-Fa-f]{1,2})$")
_LABEL_KEY_RE = re.compile(r"^-?(?:0[xX][0-9A-Fa-f]+|\d+)$")
_NODEID_RE = re.compile(r"\$NODEID", re.IGNORECASE)


def parse_int(text: str, node_id: Optional[int] = None) -> int:
    """Parse an EDS integer: decimal, 0x hex, or ``$NODEID+expr``."""
    s = text.strip()
    if _NODEID_RE.search(s):
        if node_id is None:
            raise ValueError(f"{text!r} needs a node id")
        s = _NODEID_RE.sub(str(node_id), s)
        total = 0
        for term in re.split(r"\s*\+\s*", s):
            total += parse_int(term)
        return total
    neg = s.startswith("-")
    if neg:
        s = s[1:]
    if s.lower().startswith("0x"):
        v = int(s, 16)
    else:
        v = int(s.lstrip("0") or "0", 10)
    return -v if neg else v


def normalize_name(name: str) -> str:
    """Lower-case snake form used for name lookups."""
    return re.sub(r"[^0-9a-z]+", "_", name.lower()).strip("_")


@dataclass
class BitField:
    """One field of a Sevcon BIT_SPLIT object."""

    name: str
    mask: int
    shift: int
    low: Optional[int] = None
    high: Optional[int] = None
    labels: Dict[int, str] = field(default_factory=dict)

    def extract(self, raw: int) -> int:
        return (raw & self.mask) >> self.shift

    def insert(self, raw: int, value: int) -> int:
        return (raw & ~self.mask) | ((value << self.shift) & self.mask)

    def describe(self, raw: int) -> Union[int, str]:
        v = self.extract(raw)
        return self.labels.get(v, v)


@dataclass
class Variable:
    """A single readable/writable value (VAR, or a sub-index of RECORD/ARRAY)."""

    index: int
    subindex: int
    name: str
    data_type: int
    access: str = "rw"
    low: Optional[int] = None
    high: Optional[int] = None
    default_text: Optional[str] = None
    value_text: Optional[str] = None
    pdo_mappable: bool = False
    obj_flags: int = 0
    parent_name: Optional[str] = None
    # Sevcon metadata
    scaling: Optional[float] = None
    units: Optional[str] = None
    labels: Dict[int, str] = field(default_factory=dict)
    bitfields: List[BitField] = field(default_factory=list)
    access_level: Optional[int] = None
    preop_only: bool = False
    category: Optional[str] = None
    section: Optional[str] = None
    tag: Optional[str] = None
    number_format: Optional[str] = None
    eeprom: Optional[str] = None
    soft_low: Optional[float] = None
    soft_high: Optional[float] = None
    extra: Dict[str, str] = field(default_factory=dict)

    # ---- identity ----
    @property
    def key(self) -> Tuple[int, int]:
        return (self.index, self.subindex)

    @property
    def path(self) -> str:
        return f"{self.parent_name}.{self.name}" if self.parent_name else self.name

    def __repr__(self) -> str:
        return (f"Variable(0x{self.index:04X}:{self.subindex:02X} {self.path!r} "
                f"{dt.NAMES.get(self.data_type, self.data_type)} {self.access}"
                + (f" x{self.scaling:g} {self.units}" if self.scaling is not None else "") + ")")

    # ---- access ----
    @property
    def readable(self) -> bool:
        return self.access in READABLE

    @property
    def writable(self) -> bool:
        return self.access in WRITABLE

    @property
    def size(self) -> Optional[int]:
        return dt.size_of(self.data_type)

    def default(self, node_id: Optional[int] = None):
        return self._typed(self.default_text, node_id)

    def value(self, node_id: Optional[int] = None):
        """DCF ParameterValue (falls back to DefaultValue)."""
        if self.value_text is not None and self.value_text != "":
            return self._typed(self.value_text, node_id)
        return self.default(node_id)

    def _typed(self, text: Optional[str], node_id: Optional[int]):
        if text is None:
            return None
        if self.data_type in dt.INTEGER_TYPES:
            try:
                v = parse_int(text, node_id)
            except ValueError:
                return None
            bounds = dt.limits(self.data_type)
            if bounds is not None and bounds[0] < 0 and v > bounds[1]:
                bits = dt.size_of(self.data_type) * 8
                if v < 1 << bits:
                    v -= 1 << bits
            return bool(v) if self.data_type == dt.BOOLEAN else v
        if self.data_type in dt.FLOAT_TYPES:
            return float(text)
        if self.data_type in (dt.OCTET_STRING, dt.DOMAIN):
            t = text.strip()
            if t.lower().startswith("0x"):
                h = t[2:]
                return bytes.fromhex(h if len(h) % 2 == 0 else "0" + h)
            return t.encode("latin-1")
        return text

    # ---- codec ----
    def decode_raw(self, data: bytes):
        return dt.decode(self.data_type, data)

    def encode_raw(self, value) -> bytes:
        return dt.encode(self.data_type, value)

    def to_physical(self, raw):
        """Apply Sevcon SCALING to a raw integer."""
        if self.scaling is None or not isinstance(raw, int) or isinstance(raw, bool):
            return raw
        return raw * self.scaling

    def from_physical(self, value) -> int:
        """Inverse of :meth:`to_physical` (rounds to the nearest raw step)."""
        if self.scaling is None:
            if isinstance(value, float) and value.is_integer():
                return int(value)
            return value
        if isinstance(value, bool):
            return int(value)
        return int(round(float(value) / self.scaling))

    def describe(self, raw):
        """Human-readable interpretation: enum label, bitfield dict, or None."""
        if self.bitfields and isinstance(raw, int):
            return {bf.name: bf.describe(raw) for bf in self.bitfields}
        if self.labels and isinstance(raw, int):
            return self.labels.get(raw)
        return None

    def label_to_raw(self, label: str) -> int:
        for k, v in self.labels.items():
            if v.strip().lower() == label.strip().lower():
                return k
        raise ValueError(f"{label!r} is not a label of {self.path}: {sorted(self.labels.values())}")

    def raw_limits(self) -> Tuple[Optional[int], Optional[int]]:
        """Normalize signed EDS limits without changing the source metadata."""
        lo, hi = self.low, self.high
        lim = dt.limits(self.data_type)
        # EDS limits of signed types are often written as raw two's complement
        # (e.g. LowLimit=0x8000 for INTEGER16).
        if lim is not None and lim[0] < 0:
            # Some Gen4 signed ranges end at +32768, one past INTEGER16.
            # Interpreting that decimal boundary as -32768 rejects real values.
            if lo == lim[0] and hi == lim[1] + 1:
                hi = lim[1]
            bits = dt.size_of(self.data_type) * 8  # type: ignore[operator]
            if lo is not None and lo > lim[1]:
                lo -= 1 << bits
            if hi is not None and hi > lim[1]:
                hi -= 1 << bits
        return lo, hi

    def check_range(self, raw: int) -> None:
        if isinstance(raw, bool) or not isinstance(raw, int):
            return
        lo, hi = self.raw_limits()
        if lo is not None and hi is not None and lo > hi:
            return  # EDS gives no usable range
        if lo is not None and raw < lo:
            raise ValueError(f"{raw} below LowLimit {lo} of {self.path}")
        if hi is not None and raw > hi:
            raise ValueError(f"{raw} above HighLimit {hi} of {self.path}")


@dataclass
class Record:
    """RECORD (9) or ARRAY (8) object holding sub-index Variables."""

    index: int
    name: str
    object_type: int = RECORD
    subs: Dict[int, Variable] = field(default_factory=dict)
    sub_number: Optional[int] = None
    obj_flags: int = 0
    category: Optional[str] = None
    section: Optional[str] = None
    tag: Optional[str] = None
    extra: Dict[str, str] = field(default_factory=dict)

    def __getitem__(self, key: Union[int, str]) -> Variable:
        if isinstance(key, int):
            try:
                return self.subs[key]
            except KeyError:
                raise ObjectDictionaryError(f"0x{self.index:04X} has no sub-index {key}") from None
        n = normalize_name(key)
        for v in self.subs.values():
            if normalize_name(v.name) == n:
                return v
        raise ObjectDictionaryError(f"0x{self.index:04X} {self.name!r} has no entry {key!r}")

    def __iter__(self) -> Iterator[Variable]:
        return iter(self.subs[k] for k in sorted(self.subs))

    def __len__(self) -> int:
        return len(self.subs)

    def __contains__(self, key: int) -> bool:
        return key in self.subs

    def __repr__(self) -> str:
        kind = "ARRAY" if self.object_type == ARRAY else "RECORD"
        return f"Record(0x{self.index:04X} {self.name!r} {kind} subs={sorted(self.subs)})"


Entry = Union[Variable, Record]
KeyLike = Union[int, str, Tuple[int, int]]


@dataclass
class ObjectDictionary:
    objects: Dict[int, Entry] = field(default_factory=dict)
    file_info: Dict[str, str] = field(default_factory=dict)
    device_info: Dict[str, str] = field(default_factory=dict)
    commissioning: Dict[str, str] = field(default_factory=dict)
    comments: List[str] = field(default_factory=list)
    source: Optional[str] = None
    is_dcf: bool = False
    dummy_usage: Dict[int, bool] = field(default_factory=dict)

    # ---- container ----
    def __getitem__(self, key: Union[int, str]) -> Entry:
        if isinstance(key, int):
            try:
                return self.objects[key]
            except KeyError:
                raise ObjectDictionaryError(f"object 0x{key:04X} not in dictionary") from None
        return self._by_name(key)

    def __contains__(self, key: Union[int, str]) -> bool:
        try:
            self[key]
        except ObjectDictionaryError:
            return False
        return True

    def __iter__(self) -> Iterator[Entry]:
        return iter(self.objects[k] for k in sorted(self.objects))

    def __len__(self) -> int:
        return len(self.objects)

    def add(self, entry: Entry) -> None:
        self.objects[entry.index] = entry
        self._names = None  # type: ignore[attr-defined]

    def variables(self) -> Iterator[Variable]:
        for obj in self:
            if isinstance(obj, Variable):
                yield obj
            else:
                yield from obj

    # ---- identity ----
    @property
    def vendor_id(self) -> Optional[int]:
        v = self.device_info.get("VendorNumber")
        return parse_int(v) if v else None

    @property
    def product_code(self) -> Optional[int]:
        v = self.device_info.get("ProductNumber")
        return parse_int(v) if v else None

    @property
    def revision(self) -> Optional[int]:
        v = self.device_info.get("RevisionNumber")
        return parse_int(v) if v else None

    # ---- lookup ----
    def variable(self, key: KeyLike, subindex: Optional[int] = None) -> Variable:
        """Resolve a key to a single Variable.

        Accepted keys: ``0x6041``; ``(0x5100, 1)``; ``0x5100, 1``;
        ``"0x5100:1"`` / ``"5100h,1"`` / ``"5100sub1"``; a name path
        ``"Device Measurements.Battery Voltage"``; a unique leaf name
        ``"Battery Voltage"`` (case/punctuation-insensitive); or a Sevcon tag
        ``"STATUSWORD"``.
        """
        if isinstance(key, tuple):
            key, subindex = key
        if isinstance(key, str):
            parsed = _parse_index_text(key)
            if parsed is not None:
                key, s = parsed
                subindex = s if subindex is None else subindex
        if isinstance(key, int):
            entry = self[key]
            if isinstance(entry, Variable):
                if subindex not in (None, 0):
                    raise ObjectDictionaryError(f"0x{key:04X} is a VAR; no sub-index {subindex}")
                return entry
            if subindex is None:
                raise ObjectDictionaryError(f"0x{key:04X} {entry.name!r} is a record; give a sub-index")
            return entry[subindex]
        entry = self._by_name(key)  # type: ignore[arg-type]
        if isinstance(entry, Record):
            if subindex is None:
                raise ObjectDictionaryError(f"{key!r} is a record; give a sub-index")
            return entry[subindex]
        return entry

    def _name_index(self) -> Dict[str, List[Entry]]:
        idx = getattr(self, "_names", None)
        if idx is not None:
            return idx
        idx = {}
        for obj in self:
            for n in {normalize_name(obj.name), normalize_name(obj.tag or "")} - {""}:
                idx.setdefault(n, []).append(obj)
            if isinstance(obj, Record):
                for v in obj:
                    for n in {normalize_name(f"{obj.name}.{v.name}"),
                              normalize_name(f"{obj.tag}.{v.name}") if obj.tag else "",
                              normalize_name(v.tag or ""),
                              normalize_name(v.name)} - {""}:
                        idx.setdefault(n, []).append(v)
        self._names = idx  # type: ignore[attr-defined]
        return idx

    def _by_name(self, name: str) -> Entry:
        hits = self._name_index().get(normalize_name(name), [])
        if not hits:
            raise ObjectDictionaryError(f"no object named {name!r}")
        # Prefer a top-level object, then a unique sub entry.
        top = [h for h in hits if isinstance(h, Record) or (isinstance(h, Variable) and h.parent_name is None)]
        if len(top) == 1:
            return top[0]
        if len(hits) == 1:
            return hits[0]
        where = ", ".join(
            f"0x{h.index:04X}" + (f":{h.subindex:02X}" if isinstance(h, Variable) and h.parent_name else "")
            for h in hits[:8])
        raise ObjectDictionaryError(f"name {name!r} is ambiguous ({where}); use index or Parent.Name")

    def search(self, text: str) -> List[Variable]:
        """Search names, addresses, labels, bitfields and Sevcon metadata."""
        t = text.casefold()
        result = []
        for var in self.variables():
            names = [var.path, var.tag or "", var.category or "", var.section or "",
                     var.eeprom or "", f"{var.index:04X}:{var.subindex:02X}",
                     *var.labels.values()]
            for bitfield in var.bitfields:
                names.extend([bitfield.name, *bitfield.labels.values()])
            if any(t in name.casefold() for name in names):
                result.append(var)
        return result


_INDEX_TEXT_RE = re.compile(
    r"^\s*(0x)?([0-9A-Fa-f]{4})(h)?\s*"
    r"(?:(sub|[:,.])\s*(0x)?([0-9A-Fa-f]{1,3})(h)?)?\s*$",
    re.IGNORECASE,
)


def _parse_index_text(text: str) -> Optional[Tuple[int, Optional[int]]]:
    """Parse ``"0x5100:1"``, ``"5100h,1"``, ``"5100sub1"``, ``"6041"``.

    The sub-index is decimal (the manual writes ``4651h, 10``) unless it has
    a ``0x`` prefix or ``h`` suffix, or uses the EDS ``sub`` form (hex).
    A bare four-letter word such as ``"Fade"`` is not an index.
    """
    m = _INDEX_TEXT_RE.match(text)
    if not m:
        return None
    hex_prefix, idx, h_suffix, sep, sub_prefix, sub, sub_h = m.groups()
    if not (hex_prefix or h_suffix or sep or idx.isdigit()):
        return None
    if sub is None:
        return int(idx, 16), None
    if sub_prefix or sub_h or sep.lower() == "sub":
        value = int(sub, 16)
    elif sub.isdigit():
        value = int(sub, 10)
    else:
        return None
    return (int(idx, 16), value) if value <= 255 else None


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------

@dataclass
class _Section:
    name: str
    keys: Dict[str, str] = field(default_factory=dict)
    sevcon: List[Tuple[str, str]] = field(default_factory=list)
    comments: List[str] = field(default_factory=list)


def _read_sections(text: str) -> List[_Section]:
    sections: List[_Section] = []
    cur: Optional[_Section] = None
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        m = _SECTION_RE.match(line)
        if m:
            cur = _Section(m.group(1).strip())
            sections.append(cur)
            continue
        if cur is None:
            continue
        if line.startswith(";"):
            body = line[1:].strip()
            if body.upper().startswith("SEVCONFIELD"):
                kv = body[len("SEVCONFIELD"):].strip()
                k, _, v = kv.partition("=")
                cur.sevcon.append((k.strip(), v.strip()))
            else:
                cur.comments.append(body)
            continue
        k, sep, v = line.partition("=")
        if sep:
            cur.keys[k.strip()] = v.strip()
    return sections


def _apply_sevcon(target: Union[Variable, Record], pairs: List[Tuple[str, str]]) -> None:
    split: Optional[BitField] = None
    for k, v in pairs:
        ku = k.upper()
        if ku == "SPLIT":
            split = BitField(name=f"split{v}", mask=0, shift=0)
            if isinstance(target, Variable):
                target.bitfields.append(split)
            continue
        if split is not None and ku in _SPLIT_KEYS:
            if ku == "PARAMETERNAME":
                split.name = v
            elif ku == "BITMASK":
                split.mask = parse_int(v)
            elif ku == "BITSHIFT":
                split.shift = parse_int(v)
            elif ku == "LOW_LIMIT":
                split.low = parse_int(v)
            elif ku == "HIGH_LIMIT":
                split.high = parse_int(v)
            continue
        if _LABEL_KEY_RE.match(k):
            labels = split.labels if split is not None else (
                target.labels if isinstance(target, Variable) else None)
            if labels is not None:
                labels.setdefault(parse_int(k), v.strip())
            continue
        if isinstance(target, Record):
            if ku == "CATEGORY":
                target.category = v
            elif ku == "SECTION":
                target.section = v
            elif ku == "OBJECT":
                target.tag = v
            else:
                target.extra[ku] = v
            continue
        if ku == "SCALING":
            try:
                target.scaling = float(v)
            except ValueError:
                target.extra[ku] = v
        elif ku == "UNITS":
            target.units = v or None
        elif ku == "ACCESS_LEVEL":
            target.access_level = parse_int(v)
        elif ku == "WRITE_IN_PREOPERATIONAL_ONLY":
            target.preop_only = v.upper() == "TRUE"
        elif ku == "CATEGORY":
            target.category = v
        elif ku == "SECTION":
            target.section = v
        elif ku == "OBJECT":
            target.tag = v
        elif ku == "NUMBER_FORMAT":
            # BIT_SPLIT is more informative than BASE16 when both appear.
            if target.number_format != "BIT_SPLIT":
                target.number_format = v
        elif ku == "EEPROM":
            target.eeprom = v
        elif ku == "SOFT_LOW":
            target.soft_low = float(parse_int(v)) if _LABEL_KEY_RE.match(v) else float(v)
        elif ku == "SOFT_HIGH":
            target.soft_high = float(parse_int(v)) if _LABEL_KEY_RE.match(v) else float(v)
        else:
            target.extra[ku] = v


def _make_variable(sec: _Section, index: int, subindex: int, parent: Optional[Record]) -> Variable:
    k = sec.keys

    def opt_int(name: str) -> Optional[int]:
        v = k.get(name)
        if v is None or v == "":
            return None
        try:
            return parse_int(v)
        except ValueError:
            return None

    try:
        data_type = parse_int(k.get("DataType", "0x0007"))
    except ValueError as e:
        raise ObjectDictionaryError(f"[{sec.name}] bad DataType {k.get('DataType')!r}") from e
    var = Variable(
        index=index,
        subindex=subindex,
        name=k.get("ParameterName", f"0x{index:04X}:{subindex:02X}"),
        data_type=data_type,
        access=k.get("AccessType", "rw").lower(),
        low=opt_int("LowLimit"),
        high=opt_int("HighLimit"),
        default_text=k.get("DefaultValue"),
        value_text=k.get("ParameterValue"),
        pdo_mappable=k.get("PDOMapping", "0").strip() not in ("0", ""),
        obj_flags=opt_int("ObjFlags") or 0,
        parent_name=parent.name if parent is not None else None,
    )
    _apply_sevcon(var, sec.sevcon)
    if parent is not None:
        for attr in ("category", "section"):
            if getattr(var, attr) is None:
                setattr(var, attr, getattr(parent, attr))
    return var


def parse_eds(text: str, source: Optional[str] = None) -> ObjectDictionary:
    """Parse EDS or DCF text."""
    sections = _read_sections(text)
    od = ObjectDictionary(source=source)
    by_name = {s.name.upper(): s for s in sections}
    od.file_info = dict(by_name.get("FILEINFO", _Section("")).keys)
    od.device_info = dict(by_name.get("DEVICEINFO", _Section("")).keys)
    od.dummy_usage = {
        int(key[-4:], 16): bool(parse_int(value))
        for key, value in by_name.get("DUMMYUSAGE", _Section("")).keys.items()
        if re.fullmatch(r"Dummy[0-9A-Fa-f]{4}", key, re.IGNORECASE)
    }
    od.commissioning = dict(by_name.get("DEVICECOMISSIONING", by_name.get("DEVICECOMMISSIONING", _Section(""))).keys)
    com = by_name.get("COMMENTS")
    if com is not None:
        od.comments = [com.keys[k] for k in sorted(com.keys) if k.lower().startswith("line")]
    od.is_dcf = bool(od.commissioning) or any("ParameterValue" in s.keys for s in sections)

    subs: Dict[int, List[Tuple[int, _Section]]] = {}
    tops: Dict[int, _Section] = {}
    for sec in sections:
        m = _INDEX_RE.match(sec.name)
        if m:
            tops[int(m.group(1), 16)] = sec
            continue
        m = _SUB_RE.match(sec.name)
        if m:
            subs.setdefault(int(m.group(1), 16), []).append((int(m.group(2), 16), sec))

    for index in sorted(tops):
        sec = tops[index]
        try:
            otype = parse_int(sec.keys.get("ObjectType", "0x7"))
        except ValueError:
            otype = VAR
        if otype in (VAR, DOMAIN_OBJ, DEFTYPE):
            od.add(_make_variable(sec, index, 0, None))
            continue
        rec = Record(
            index=index,
            name=sec.keys.get("ParameterName", f"0x{index:04X}"),
            object_type=otype,
            sub_number=parse_int(sec.keys["SubNumber"]) if sec.keys.get("SubNumber") else None,
            obj_flags=parse_int(sec.keys.get("ObjFlags", "0") or "0"),
        )
        _apply_sevcon(rec, sec.sevcon)
        compact = sec.keys.get("CompactSubObj")
        if compact:
            n = parse_int(compact)
            base = _Section(sec.name, dict(sec.keys))
            for i in range(1, n + 1):
                s = _Section(f"{sec.name}sub{i:X}", dict(base.keys), list(sec.sevcon))
                s.keys["ParameterName"] = f"{rec.name}{i}"
                rec.subs[i] = _make_variable(s, index, i, rec)
            zero = _Section(f"{sec.name}sub0", {"ParameterName": "Number of entries", "DataType": "0x0005",
                                                "AccessType": "ro", "DefaultValue": str(n)})
            rec.subs[0] = _make_variable(zero, index, 0, rec)
        for subindex, ssec in subs.get(index, []):
            rec.subs[subindex] = _make_variable(ssec, index, subindex, rec)
        od.add(rec)
    return od


def load_eds(path: Union[str, Path]) -> ObjectDictionary:
    p = Path(path)
    try:
        text = p.read_text(encoding="latin-1")
    except OSError as e:
        raise ObjectDictionaryError(f"cannot read {p}: {e}") from e
    return parse_eds(text, source=str(p))


def bundled_eds_path() -> Path:
    """Path of the Gen4 EDS shipped with the package."""
    return Path(__file__).with_name("data") / "Gen4.eds"


_GEN4_CACHE: Dict[bool, ObjectDictionary] = {}


def gen4_od(*, include_supplemental: bool = False) -> ObjectDictionary:
    """Bundled dictionary, optionally adding firmware-dependent missing entries.

    Supplements never replace an EDS entry. Their ``extra`` metadata records
    the source and firmware; availability on a controller is not established.
    Pass ``include_supplemental=True`` to select these alternate definitions.
    Only select them when they match your firmware. A custom EDS
    loaded with :func:`load_eds` is never supplemented. Cached dictionaries
    are shared and must be treated as read-only.
    """
    if include_supplemental not in _GEN4_CACHE:
        od = load_eds(bundled_eds_path())
        if include_supplemental:
            supplement = load_eds(bundled_eds_path().with_name("Gen4-supplement.eds"))
            for entry in supplement:
                if entry.index not in od.objects:
                    od.add(entry)
                elif isinstance(entry, Record) and isinstance(od[entry.index], Record):
                    target = od[entry.index]
                    for var in entry:
                        if var.subindex not in target.subs:
                            var.parent_name = target.name
                            target.subs[var.subindex] = var
            od.source += " + Gen4-supplement.eds"
        _GEN4_CACHE[include_supplemental] = od
    return _GEN4_CACHE[include_supplemental]
