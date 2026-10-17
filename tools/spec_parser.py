#!/usr/bin/env python3
"""
Parsers for the documents fetched by ``tools/fetch_specs.py``.

Every parser reads one document (``pdftotext -layout`` text, spreadsheet, or
source file) and returns plain Python data. ``tools/generate_tables.py``
turns that data into ``src/sevcon_can/tables.py``; the test suite reparses
the documents and fails if the committed tables drift from them.

Parsers fail loudly: a table that is not found, or found with a different
shape than expected, raises :class:`SpecParseError` rather than returning a
partial result.

Documents and what is taken from each:

* CiA 301 V4.2.0 - SDO abort codes (Table 22), EMCY error code classes
  (Table 25) and codes (Table 26), data types (Table 44), object
  descriptions (1000h-1FFFh).
* CiA DS301 V4.02 - SDO abort codes (Table 20), cross-check for V4.2.
* CiA DSP402 V2.0 - device control commands (Table 4), device state bits
  (Table 7), object descriptions (6000h-67FFh).
* CiA DS401 V2.1 - object descriptions (6000h-67FFh). The Gen4 implements
  these as its second logical device at +800h (6800h-6FFFh).
* Gen4 Size 2/4/6 Product Manual - CANopen abort reasons of 5310h
  (Table 33), LED flash categories (Table 34), output ratings (Table 4),
  voltage ranges (Table 2), and every ``XXXXh`` object reference in prose.
* Gen4 Product Manual V3.4 - the same object references (older wording).
* ``fault codes.xlsx`` (Sevcon, 2013) - fault ID table.
* OVMS ``rt_sevcon_faults.cpp`` / Victron ``sevcon.c`` - independent fault
  tables used for coverage and cross-checks.
"""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

ROOT = Path(__file__).resolve().parent.parent
SPECS = ROOT / "specs"
TEXT = SPECS / "text"
EXTRA = SPECS / "extra"
OD_DIR = SPECS / "od"


class SpecParseError(Exception):
    pass


def read_text(name: str) -> str:
    p = TEXT / f"{name}.txt"
    if not p.exists():
        raise SpecParseError(f"{p} missing; run tools/fetch_specs.py")
    return p.read_text(encoding="utf-8", errors="replace")


def _between(text: str, start: str, end: str, *, occurrence: int = -1) -> str:
    """Slice from the ``occurrence``-th ``start`` (default last, skipping the
    table of contents) to the next ``end``."""
    starts = [m.start() for m in re.finditer(re.escape(start), text)]
    if not starts:
        raise SpecParseError(f"marker {start!r} not found")
    s = starts[occurrence]
    e = text.find(end, s + len(start))
    if e < 0:
        raise SpecParseError(f"end marker {end!r} not found after {start!r}")
    return text[s:e]


_PAGE_NOISE_RE = re.compile(
    r"^\s*(©\s*CiA.*|CANopen application layer and communication profile\s*|\d+\s+©.*|\d{1,3}\s*)$"
)


def _clean_lines(block: str) -> List[str]:
    return [ln for ln in block.splitlines() if not _PAGE_NOISE_RE.match(ln)]


# ---------------------------------------------------------------------------
# CiA 301
# ---------------------------------------------------------------------------

_ABORT_ROW_RE = re.compile(r"^\s*([0-9A-F]{4})\s+([0-9A-F]{4})\s*h\s{2,}(\S.*?)\s*$")


def parse_sdo_abort_codes(text: str, *, table: str = "Table 22: SDO abort codes",
                          end: str = "The abort codes not listed shall be reserved") -> Dict[int, str]:
    """CiA 301 Table 22 (V4.2) / Table 20 (DS301 V4.02). Wrapped descriptions
    are joined."""
    block = _between(text, table, end)
    codes: Dict[int, str] = {}
    last: Optional[int] = None
    for ln in _clean_lines(block)[1:]:
        m = _ABORT_ROW_RE.match(ln)
        if m:
            last = int(m.group(1) + m.group(2), 16)
            codes[last] = m.group(3)
            continue
        cont = ln.strip()
        if last is not None and cont and not cont.lower().startswith(("abort code", "table")):
            if re.match(r"^\s{10,}\S", ln):
                codes[last] += " " + cont
    for k, v in codes.items():
        codes[k] = re.sub(r"\s+", " ", v).strip().rstrip(".")
    if len(codes) < 25:
        raise SpecParseError(f"SDO abort table too short ({len(codes)})")
    return codes


_EMCY_ROW_RE = re.compile(r"^\s*([0-9A-F]{2}(?:[0-9A-F]{2}|xx))h\s{2,}(\S.*?)\s*$")


def parse_emcy_classes(text: str) -> Dict[str, str]:
    """CiA 301 V4.2 Table 25: ``{"10xx": "Generic error", ...}``."""
    block = _between(text, "Table 25: Emergency error code classes", "Table 26: Emergency error codes")
    out: Dict[str, str] = {}
    for ln in _clean_lines(block):
        m = _EMCY_ROW_RE.match(ln)
        if m and m.group(1).endswith("xx"):
            out[m.group(1)] = m.group(2)
    if len(out) < 20:
        raise SpecParseError(f"EMCY class table too short ({len(out)})")
    return out


def parse_emcy_codes(text: str) -> Dict[int, str]:
    """CiA 301 V4.2 Table 26 (wrapped rows joined)."""
    block = _between(text, "Table 26: Emergency error codes", "The emergency object is optional")
    out: Dict[int, str] = {}
    last: Optional[int] = None
    for ln in _clean_lines(block)[1:]:
        m = _EMCY_ROW_RE.match(ln)
        if m and not m.group(1).endswith("xx"):
            last = int(m.group(1), 16)
            out[last] = m.group(2)
        elif last is not None and re.match(r"^\s{25,}\S", ln) and "Error code" not in ln:
            out[last] += " " + ln.strip()
    out = {k: re.sub(r"\s+", " ", v).replace("–", "-").strip() for k, v in out.items()}
    if len(out) < 30:
        raise SpecParseError(f"EMCY code table too short ({len(out)})")
    return out


_DTYPE_ROW_RE = re.compile(r"^\s*([0-9A-F]{4})h\s+DEFTYPE\s+([A-Z0-9_]+)\s*$")


def parse_data_types(text: str) -> Dict[int, str]:
    """CiA 301 V4.2 Table 44, basic DEFTYPE entries 0001h-001Bh."""
    block = _between(text, "Table 44: Object dictionary data types", "0020h")
    out = {int(m.group(1), 16): m.group(2) for m in map(_DTYPE_ROW_RE.match, block.splitlines()) if m}
    if len(out) != 25:
        raise SpecParseError(f"expected 25 basic data types, got {len(out)}")
    return out


# ---------------------------------------------------------------------------
# Object descriptions (CiA 301 / 401 / 402 share the block format)
# ---------------------------------------------------------------------------

@dataclass
class ProfileObject:
    index: int
    name: str
    object_code: str
    data_type: str
    category: str
    index_end: Optional[int] = None  # for "1400h to 15FFh" ranges


#: Object description blocks in which the document itself omits a row.
#: DSP402 V2.0 p.166 "6050h vl slow down time" has no Category line.
DOC_OMISSIONS: Dict[int, frozenset] = {0x6050: frozenset({"category"})}

#: Index cells misprinted in DS401 V2.1: "6006g" (p.33) and "6250" without
#: the h suffix (p.63). Both are confirmed by the object list table.
DOC_INDEX_TYPOS: Dict[str, str] = {"6006g": "6006h", "6250": "6250h"}

#: DS401 V2.1 misprints of "Category": "Categrory" (6260h, p.64) and
#: "Catagory" (642Bh, p.104).
_CATEGORY_MISPRINTS = ("categrory", "catagory")
_OD_FIELD_RE = re.compile(
    r"^\s*(INDEX|Index|Name|Object [Cc]ode|Data [Tt]ype|Category|Categrory|Catagory)\s{2,}(\S.*?)\s*$")
_INDEX_VAL_RE = re.compile(r"^([0-9A-F]{4})\s*h(?:\s*(?:to|-|–|\.\.)\s*([0-9A-F]{4})\s*h)?", re.I)


def parse_object_descriptions(text: str) -> List[ProfileObject]:
    """All ``OBJECT DESCRIPTION`` blocks. Name continuation lines (indented
    under the value column) are joined; format-definition blocks whose index
    is not hexadecimal are skipped."""
    lines = text.splitlines()
    out: List[ProfileObject] = []
    i = 0
    while i < len(lines):
        if lines[i].strip().upper() != "OBJECT DESCRIPTION":
            i += 1
            continue
        fields: Dict[str, str] = {}
        col: Optional[int] = None
        last_key: Optional[str] = None
        j = i + 1
        while j < len(lines) and j < i + 30:
            ln = lines[j]
            if ln.strip().upper().startswith("ENTRY DESCRIPTION") or ln.strip().upper() == "OBJECT DESCRIPTION":
                break
            m = _OD_FIELD_RE.match(ln)
            if m:
                key = m.group(1).lower().replace(" ", "_")
                if key in _CATEGORY_MISPRINTS:
                    key = "category"
                fields[key] = DOC_INDEX_TYPOS.get(m.group(2), m.group(2)) if key == "index" else m.group(2)
                col = ln.index(m.group(2))
                last_key = key
            elif col is not None and last_key == "name" and ln.strip() and len(ln) > col \
                    and ln[:col].strip() == "" and not _PAGE_NOISE_RE.match(ln):
                fields["name"] += " " + ln.strip()
            j += 1
        i = j
        m = _INDEX_VAL_RE.match(fields.get("index", ""))
        if not m:
            continue
        idx = int(m.group(1), 16)
        missing = {"name", "object_code", "data_type", "category"} - set(fields)
        allowed = DOC_OMISSIONS.get(idx, frozenset())
        if missing - allowed:
            raise SpecParseError(f"object {fields.get('index')} missing {sorted(missing)}")
        for k in missing:
            fields[k] = ""
        out.append(ProfileObject(
            index=idx,
            name=re.sub(r"\s+", " ", fields["name"]).strip(),
            object_code=fields["object_code"].strip().upper(),
            data_type=re.sub(r"\s+", " ", fields["data_type"]).strip(),
            category=fields["category"].strip(),
            index_end=int(m.group(2), 16) if m.group(2) else None,
        ))
    if not out:
        raise SpecParseError("no OBJECT DESCRIPTION blocks found")
    return out


# ---------------------------------------------------------------------------
# CiA 402
# ---------------------------------------------------------------------------

_STATE_ROW_RE = re.compile(r"^\s*([01x]{4} [01x]{4} [01x]{4} [01x]{4})\s{2,}(\S.*?)\s*$")


def parse_ds402_states(text: str) -> List[Tuple[int, int, str]]:
    """DSP402 Table 7: ``[(mask, value, state_name), ...]`` for statusword."""
    block = _between(text, "BITS 0 – 3, 5 AND 6:", "Table 7:", occurrence=0)
    out = []
    for ln in block.splitlines():
        m = _STATE_ROW_RE.match(ln)
        if not m:
            continue
        bits = m.group(1).replace(" ", "")
        mask = value = 0
        for pos, ch in enumerate(reversed(bits)):
            if ch != "x":
                mask |= 1 << pos
                if ch == "1":
                    value |= 1 << pos
        out.append((mask, value, m.group(2).strip()))
    if len(out) != 8:
        raise SpecParseError(f"expected 8 DS402 states, got {len(out)}")
    return out


_CMD_ROW_RE = re.compile(
    r"^(\S.*?)\s{2,}([01X])\s+([01X])\s+([01X])\s+([01X])\s+([01X])\s+(\S+)\s*$"
)
_CMD_FAULT_RESET_RE = re.compile(r"^(Fault reset)\s+(X)\s+(X)\s+(X)\s+(X)\s+(\S+)\s*$")


@dataclass
class Ds402Command:
    name: str
    #: bits of the controlword: 7 fault reset, 3 enable op, 2 quick stop,
    #: 1 enable voltage, 0 switch on. Value is "0", "1" or "X".
    bits: Dict[int, str]
    transitions: str


def parse_ds402_commands(text: str) -> List[Ds402Command]:
    """DSP402 Table 4. The Fault reset row's bit 7 is printed as a rising
    edge figure that pdftotext drops; it is restored as ``"1"`` (the table
    caption and 10.3.1 define fault reset as a 0->1 edge of bit 7)."""
    block = _between(text, "Device control commands are triggered by", "Table 4:", occurrence=0)
    out: List[Ds402Command] = []
    for ln in block.splitlines():
        s = ln.strip()
        m = _CMD_ROW_RE.match(s)
        if m:
            fr, eo, qs, ev, so = m.group(2, 3, 4, 5, 6)
            out.append(Ds402Command(m.group(1), {7: fr, 3: eo, 2: qs, 1: ev, 0: so}, m.group(7)))
            continue
        m = _CMD_FAULT_RESET_RE.match(s)
        if m:
            eo, qs, ev, so = m.group(2, 3, 4, 5)
            out.append(Ds402Command(m.group(1), {7: "1", 3: eo, 2: qs, 1: ev, 0: so}, m.group(6)))
    if len(out) != 8:
        raise SpecParseError(f"expected 8 DS402 commands, got {len(out)}")
    return out


# ---------------------------------------------------------------------------
# Gen4 manual
# ---------------------------------------------------------------------------

GEN4_MANUAL = "Gen4-Size-2-6-Product-Manual"


def parse_gen4_abort_reasons(text: str) -> Dict[int, str]:
    """Table 33 (reasons reported in 5310h after abort 08000000h).

    Three columns of ``<n> <text>``; a description can wrap onto the next
    line in the same column. Column positions are learned from the number
    tokens on each row.
    """
    block = _between(text, "Object 5310h gives the exact", "Table 33")
    lines = block.splitlines()[2:]
    entries: Dict[int, str] = {}
    columns: List[Tuple[int, int]] = []  # (text column start, number)
    tok = re.compile(r"(?:(?<=\s)|^)(\d{1,2})\s{2,}(\S(?:.*?\S)?)(?=\s{3,}\d{1,2}\s|\s*$)")
    for ln in lines:
        if not ln.strip():
            continue
        hits = list(tok.finditer(ln))
        if hits and all(int(h.group(1)) <= 40 for h in hits):
            columns = []
            for h in hits:
                n = int(h.group(1))
                entries[n] = h.group(2).strip()
                columns.append((h.start(2), n))
            continue
        # Continuation: attach each text run to the nearest column start.
        for m in re.finditer(r"\S(?:.*?\S)?(?=\s{3,}|\s*$)", ln):
            if not columns:
                break
            start = m.start()
            col = min(columns, key=lambda c: abs(c[0] - start))
            entries[col[1]] += " " + m.group(0).strip()
    entries = {k: re.sub(r"\s+", " ", v).replace("pre- operational", "pre-operational") for k, v in entries.items()}
    if sorted(entries) != list(range(0, 34)):
        raise SpecParseError(f"Table 33 numbers {sorted(entries)}")
    return entries


def parse_gen4_led_categories(text: str) -> Dict[int, str]:
    """Table 34: LED flash count -> fault category."""
    block = _between(text, "LED flashes        Fault category", "Table 34")
    out: Dict[int, str] = {}
    for ln in block.splitlines()[1:]:
        m = re.match(r"^\s*(\d+)(?:\s*\(off\))?\s{2,}(\S.*?)\s*$", ln)
        if m:
            out[int(m.group(1))] = m.group(2)
    if sorted(out) != list(range(0, 16)):
        raise SpecParseError(f"Table 34 flash counts {sorted(out)}")
    return out


@dataclass
class Rating:
    voltage: str
    size: int
    short_term_a: int
    continuous_a: int


def parse_gen4_ratings(text: str) -> List[Rating]:
    """Table 4: input voltage x frame size -> 2 min and 1 h current ratings.

    The voltage label sits on the middle row of each three-row group, so
    rows are collected first and labels assigned by group order.
    """
    block = _between(text, "Input (Vdc)       Function", "Table 4")
    rows: List[Tuple[Optional[str], int, int, int]] = []
    for ln in block.splitlines()[2:]:
        m = re.match(r"^\s*([\d /]+V)?\s*size\s+(\d)\s+(\d+)\s+(\d+)\s*$", ln)
        if m:
            label = m.group(1).replace(" ", "") if m.group(1) else None
            rows.append((label, int(m.group(2)), int(m.group(3)), int(m.group(4))))
    if len(rows) != 10:
        raise SpecParseError(f"Table 4 has {len(rows)} rows")
    # Sizes ascend within a voltage class. A class also ends when a second
    # label arrives (24V has only size 2, immediately followed by the
    # 24/36V size 4 row).
    groups: List[List[Tuple[Optional[str], int, int, int]]] = []
    for r in rows:
        cur = groups[-1] if groups else None
        if cur is None or r[1] <= cur[-1][1] or (r[0] and any(x[0] for x in cur)):
            groups.append([r])
        else:
            cur.append(r)
    out: List[Rating] = []
    for g in groups:
        labels = [r[0] for r in g if r[0]]
        if len(labels) != 1:
            raise SpecParseError(f"Table 4 group without a single voltage label: {g}")
        for r in g:
            out.append(Rating(labels[0], r[1], r[2], r[3]))
    return out


@dataclass
class VoltageRange:
    voltage: str
    conventional_min: float
    conventional_max: float
    limit_min: float
    limit_max: float
    overvoltage: float


def parse_gen4_voltage_ranges(text: str) -> List[VoltageRange]:
    """Table 2: five voltage classes. Each row's ``a to b`` pairs may wrap
    onto the following line; upper bounds are filled in column order."""
    block = _between(text, "Conventional        16.8V", "Table 2")
    lines = block.splitlines()
    labels = ["24V", "24/36V", "36/48V", "72/80V", "96/110V"]

    def cells(ln: str) -> List[str]:
        return [c for c in re.split(r"\s{2,}", ln[20:].strip()) if c]

    def pairs(start_pat: str) -> List[Tuple[float, float]]:
        for k, ln in enumerate(lines):
            if re.match(start_pat, ln.strip()):
                vals: List[List[Optional[float]]] = []
                for c in cells(ln):
                    m = re.fullmatch(r"(\d+(?:\.\d+)?)\s*V\s+to(?:\s+(\d+(?:\.\d+)?)\s*V)?", c)
                    if not m:
                        raise SpecParseError(f"Table 2 row {start_pat!r}: bad cell {c!r}")
                    vals.append([float(m.group(1)), float(m.group(2)) if m.group(2) else None])
                pending = [v for v in vals if v[1] is None]
                nxt = []
                if k + 1 < len(lines):
                    for c in cells(lines[k + 1]):
                        m = re.match(r"(\d+(?:\.\d+)?)\s*V\b", c)
                        if not m:
                            raise SpecParseError(f"Table 2 row {start_pat!r}: bad closer {c!r}")
                        nxt.append(m.group(1))
                if len(nxt) < len(pending):
                    raise SpecParseError(f"Table 2 row {start_pat!r}: {len(pending)} open ranges, {len(nxt)} closers")
                for v, closer in zip(pending, nxt):
                    v[1] = float(closer)
                if len(vals) != 5:
                    raise SpecParseError(f"Table 2 row {start_pat!r}: {len(vals)} columns")
                return [(a, b) for a, b in vals]  # type: ignore[misc]
        raise SpecParseError(f"Table 2 row {start_pat!r} not found")

    conv = pairs(r"^Conventional\s")
    lim = pairs(r"^Working\s")
    over_line = next((ln for ln in lines if re.match(r"^\s*Non-\s+\d", ln)), None)
    if over_line is None:
        raise SpecParseError("Table 2 overvoltage row not found")
    over = [float(x) for x in re.findall(r"(\d+(?:\.\d+)?)\s*V", over_line)]
    if len(over) != 5:
        raise SpecParseError(f"Table 2 overvoltage row has {len(over)} values")
    return [VoltageRange(labels[i], conv[i][0], conv[i][1], lim[i][0], lim[i][1], over[i]) for i in range(5)]


_OBJ_REF_RE = re.compile(
    r"(?<![0-9A-Za-z])(?:([1-6][0-9A-F]{3})\s?h(?![a-z])"
    r"|0x([1-6][0-9A-F]{3})(?![0-9A-Za-z]))", re.IGNORECASE)


def parse_object_references(text: str) -> Dict[int, List[str]]:
    """Every ``XXXXh`` or ``0xXXXX`` reference in prose -> context snippets.

    Only 1000h-6FFFh (the communication, manufacturer and profile areas) is
    scanned, and ``41X0h``-style templates are excluded by the pattern.
    """
    refs: Dict[int, List[str]] = {}
    for ln in text.splitlines():
        for m in _OBJ_REF_RE.finditer(ln):
            idx = int(m.group(1) or m.group(2), 16)
            refs.setdefault(idx, []).append(ln.strip())
    return refs


# ---------------------------------------------------------------------------
# Fault tables
# ---------------------------------------------------------------------------

@dataclass
class Fault:
    fid: int
    message: str
    level: Optional[int] = None
    led_flashes: Optional[int] = None
    uid: Optional[int] = None
    type: str = ""
    display: str = ""
    description: str = ""
    action: str = ""
    sources: List[str] = field(default_factory=list)


_XLSX_NS = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}


def _xlsx_rows(path: Path) -> List[Dict[str, str]]:
    with zipfile.ZipFile(path) as z:
        shared = []
        if "xl/sharedStrings.xml" in z.namelist():
            root = ET.fromstring(z.read("xl/sharedStrings.xml"))
            for si in root.findall("m:si", _XLSX_NS):
                shared.append("".join(t.text or "" for t in si.iter(f"{{{_XLSX_NS['m']}}}t")))
        sheet = ET.fromstring(z.read("xl/worksheets/sheet1.xml"))
    rows = []
    for r in sheet.findall(".//m:row", _XLSX_NS):
        row: Dict[str, str] = {}
        for c in r.findall("m:c", _XLSX_NS):
            col = re.match(r"[A-Z]+", c.get("r", "")).group(0)  # type: ignore[union-attr]
            v = c.find("m:v", _XLSX_NS)
            if v is None or v.text is None:
                is_ = c.find("m:is", _XLSX_NS)
                text = "".join(t.text or "" for t in is_.iter(f"{{{_XLSX_NS['m']}}}t")) if is_ is not None else ""
            elif c.get("t") == "s":
                text = shared[int(v.text)]
            else:
                text = v.text
            row[col] = text
        rows.append(row)
    return rows


def parse_fault_xlsx(path: Optional[Path] = None) -> Dict[int, Fault]:
    """Sevcon ``fault codes.xlsx``: header row ``Level, LED Flashes, UID,
    FID, Type, Display, Message, Description, Recommended Action``."""
    path = path or (EXTRA / "fault-codes" / "fault codes.xlsx")
    rows = _xlsx_rows(path)
    header_row = next((r for r in rows if "FID" in r.values()), None)
    if header_row is None:
        raise SpecParseError("fault sheet header not found")
    col = {v.strip(): k for k, v in header_row.items()}
    need = ["Level", "LED Flashes", "UID", "FID", "Type", "Display", "Message", "Description", "Recommended Action"]
    if any(n not in col for n in need):
        raise SpecParseError(f"fault sheet columns {sorted(col)}")
    out: Dict[int, Fault] = {}
    for r in rows[rows.index(header_row) + 1:]:
        fid_text = r.get(col["FID"], "").strip()
        if not re.fullmatch(r"0x[0-9A-Fa-f]{4}", fid_text):
            continue

        def num(name: str) -> Optional[int]:
            t = r.get(col[name], "").strip()
            return int(float(t)) if re.fullmatch(r"\d+(?:\.0)?", t) else None

        fid = int(fid_text, 16)
        f = Fault(
            fid=fid,
            message=r.get(col["Message"], "").strip(),
            level=num("Level"),
            led_flashes=num("LED Flashes"),
            uid=num("UID"),
            type=r.get(col["Type"], "").strip(),
            display=r.get(col["Display"], "").strip(),
            description=re.sub(r"\s+", " ", r.get(col["Description"], "")).strip(),
            action=re.sub(r"\s+", " ", r.get(col["Recommended Action"], "")).strip(),
            sources=["sevcon-xlsx"],
        )
        if fid in out:
            raise SpecParseError(f"duplicate FID 0x{fid:04X} in fault sheet")
        out[fid] = f
    if len(out) < 100:
        raise SpecParseError(f"fault sheet has only {len(out)} faults")
    return out


_OVMS_FAULT_RE = re.compile(
    r'\{\s*0x([0-9A-Fa-f]{4}),\s*FCT_(\w+),\s*"((?:[^"\\]|\\.)*)",\s*"((?:[^"\\]|\\.)*)",\s*"((?:[^"\\]|\\.)*)"\s*\}'
)


def parse_ovms_faults(path: Optional[Path] = None) -> Dict[int, Fault]:
    path = path or (EXTRA / "ovms-rt_sevcon_faults.cpp")
    text = path.read_text(encoding="utf-8", errors="replace")
    out: Dict[int, Fault] = {}
    for m in _OVMS_FAULT_RE.finditer(text):
        fid = int(m.group(1), 16)
        out.setdefault(fid, Fault(
            fid=fid, message=m.group(3), type=m.group(2),
            description=m.group(4), action=m.group(5), sources=["ovms"],
        ))
    if len(out) < 100:
        raise SpecParseError(f"OVMS fault table has only {len(out)} faults")
    return out


_VICTRON_FAULT_RE = re.compile(r'\{\s*0x([0-9A-Fa-f]{4}),\s*"((?:[^"\\]|\\.)*)"\s*\}')


def parse_victron_faults(path: Optional[Path] = None) -> Dict[int, List[str]]:
    """Victron's table repeats some IDs with different names (e.g. 0x4542
    'Device test waiting' / 'Low Oil'); all names are kept."""
    path = path or (EXTRA / "victron-sevcon.c")
    text = path.read_text(encoding="utf-8", errors="replace")
    out: Dict[int, List[str]] = {}
    for m in _VICTRON_FAULT_RE.finditer(text):
        out.setdefault(int(m.group(1), 16), []).append(m.group(2).strip())
    if len(out) < 100:
        raise SpecParseError(f"Victron fault table has only {len(out)} faults")
    return out


def parse_emcy_fault_offsets() -> Dict[str, Tuple[int, int]]:
    """Byte offsets of the 16-bit little-endian Sevcon fault ID inside the
    8-byte EMCY payload, as implemented by each independent client."""
    found: Dict[str, Tuple[int, int]] = {}
    ovms = (EXTRA / "ovms-rt_sevcon_faults.cpp").read_text(errors="replace")
    # OVMS's emcy.data[] starts after the 3-byte CiA header.
    m = re.search(r"emcy\.data\[(\d)\]\s*<<\s*8\s*\|\s*emcy\.data\[(\d)\]", ovms)
    if m:
        found["ovms"] = (int(m.group(2)) + 3, int(m.group(1)) + 3)
    vic = (EXTRA / "victron-sevcon.c").read_text(errors="replace")
    m = re.search(r"mdata\[(\d)\]\s*\|\s*\(\s*message->mdata\[(\d)\]\s*<<\s*8\)", vic)
    if m:
        found["victron"] = (int(m.group(1)), int(m.group(2)))
    sim = (EXTRA / "victron-sevcon_gen4.py").read_text(errors="replace")
    m = re.search(r'emcy\.send\(0x[0-9A-Fa-f]+,\s*0x[0-9A-Fa-f]+,\s*b"\\x([0-9A-Fa-f]{2})\\x([0-9A-Fa-f]{2})', sim)
    if m:
        found["victron-sim-fid"] = (int(m.group(2) + m.group(1), 16), 0)
    return found


def parse_ovms_product_codes(path: Optional[Path] = None) -> Dict[str, Tuple[int, str]]:
    path = path or (EXTRA / "ovms-rt_sevcon.h")
    text = path.read_text(errors="replace")
    return {m.group(1): (int(m.group(2), 16), m.group(3).strip())
            for m in re.finditer(r"#define\s+(SC_Gen4_\w+)\s+0x([0-9A-Fa-f]+)\s*//\s*(.*)", text)}


def main() -> None:  # pragma: no cover - manual inspection helper
    t301 = read_text("CiA-301-V4.2")
    print("abort codes", len(parse_sdo_abort_codes(t301)))
    print("emcy codes", len(parse_emcy_codes(t301)))
    print("data types", len(parse_data_types(t301)))
    print("301 objects", len(parse_object_descriptions(t301)))
    t402 = read_text("CiA-DSP402-V2.0")
    print("402 objects", len(parse_object_descriptions(t402)))
    print("402 states", parse_ds402_states(t402))
    print("402 commands", parse_ds402_commands(t402))
    print("401 objects", len(parse_object_descriptions(read_text("CiA-DS401-V2.1"))))
    man = read_text(GEN4_MANUAL)
    print("abort reasons", parse_gen4_abort_reasons(man))
    print("LED", parse_gen4_led_categories(man))
    print("ratings", parse_gen4_ratings(man))
    print("voltages", parse_gen4_voltage_ranges(man))
    print("faults", len(parse_fault_xlsx()), len(parse_ovms_faults()), len(parse_victron_faults()))
    print("emcy offsets", parse_emcy_fault_offsets())
    print("product codes", parse_ovms_product_codes())


if __name__ == "__main__":  # pragma: no cover
    main()
