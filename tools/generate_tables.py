#!/usr/bin/env python3
"""
Generate ``src/sevcon_can/tables.py`` from the fetched specifications.

    python3 tools/generate_tables.py           # write tables.py
    python3 tools/generate_tables.py --check   # exit 1 if tables.py is stale

Run ``tools/fetch_specs.py`` first. The output is deterministic, so the
generated file can be committed and reviewed like source.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, List, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent))
import spec_parser as sp  # noqa: E402

ROOT = sp.ROOT
OUT = ROOT / "src" / "sevcon_can" / "tables.py"

HEADER = '''"""
Tables generated from the CANopen and Sevcon specifications.

DO NOT EDIT. Regenerate with ``python3 tools/generate_tables.py``; the
source documents are listed in ``SOURCE_DOCUMENTS`` with their SHA-256.
"""
from typing import Dict, List, NamedTuple, Optional, Tuple


class ProfileObjectInfo(NamedTuple):
    index: int
    name: str
    object_code: str
    data_type: str
    category: str
    index_end: Optional[int]


class Ds402CommandInfo(NamedTuple):
    name: str
    bits: Tuple[Tuple[int, str], ...]  # (controlword bit, "0"/"1"/"X")
    transitions: str


class RatingInfo(NamedTuple):
    voltage: str
    size: int
    short_term_a: int  # 2 minute rating
    continuous_a: int  # 60 minute rating


class VoltageRangeInfo(NamedTuple):
    voltage: str
    conventional_min: float
    conventional_max: float
    limit_min: float
    limit_max: float
    overvoltage: float


class FaultInfo(NamedTuple):
    fid: int
    message: str
    level: Optional[int]
    led_flashes: Optional[int]
    uid: Optional[int]
    type: str
    display: str
    description: str
    action: str
    aliases: Tuple[str, ...]
    sources: Tuple[str, ...]

'''

#: Documents read by this generator (names as in specs/MANIFEST.json).
DOCUMENTS = [
    "CiA-301-V4.2", "CiA-DSP402-V2.0", "CiA-DS401-V2.1", sp.GEN4_MANUAL,
    "fault-codes.rar", "ovms-rt_sevcon_faults.cpp", "ovms-rt_sevcon.h", "victron-sevcon.c",
]


def _hexkey(n: int, width: int) -> str:
    return f"0x{n:0{width}X}"


def _dict(name: str, typ: str, items: Iterable[Tuple[str, Any]]) -> str:
    lines = [f"{name}: {typ} = {{"]
    for k, v in items:
        lines.append(f"    {k}: {v!r},")
    lines.append("}\n")
    return "\n".join(lines)


def _list(name: str, typ: str, items: Iterable[Any]) -> str:
    lines = [f"{name}: {typ} = ["]
    for v in items:
        lines.append(f"    {v!r},")
    lines.append("]\n")
    return "\n".join(lines)


def _profile(objs: List[sp.ProfileObject]) -> List[Tuple[str, Any]]:
    seen: Dict[int, sp.ProfileObject] = {}
    for o in objs:
        seen.setdefault(o.index, o)
    return [(_hexkey(i, 4), _ProfileRepr(o)) for i, o in sorted(seen.items())]


class _ProfileRepr:
    def __init__(self, o: sp.ProfileObject):
        self.o = o

    def __repr__(self) -> str:
        o = self.o
        end = "None" if o.index_end is None else _hexkey(o.index_end, 4)
        return (f"ProfileObjectInfo({_hexkey(o.index, 4)}, {o.name!r}, {o.object_code!r}, "
                f"{o.data_type!r}, {o.category!r}, {end})")


class _Raw:
    def __init__(self, text: str):
        self.text = text

    def __repr__(self) -> str:
        return self.text


def merge_faults() -> Dict[int, Tuple]:
    """Sevcon's spreadsheet is authoritative; OVMS fills IDs it lacks and
    Victron contributes names. Every name seen is kept as an alias."""
    xlsx = sp.parse_fault_xlsx()
    ovms = sp.parse_ovms_faults()
    vic = sp.parse_victron_faults()
    out: Dict[int, Tuple] = {}
    for fid in sorted(set(xlsx) | set(ovms) | set(vic)):
        base = xlsx.get(fid) or ovms.get(fid)
        sources: List[str] = []
        names: List[str] = []
        if fid in xlsx:
            sources.append("sevcon-xlsx")
            names.append(xlsx[fid].message)
        if fid in ovms:
            sources.append("ovms")
            names.append(ovms[fid].message)
        if fid in vic:
            sources.append("victron")
            names.extend(vic[fid])
        message = base.message if base else vic[fid][0]
        aliases: List[str] = []
        for n in names:
            if n and n != message and n not in aliases:
                aliases.append(n)
        out[fid] = (
            fid, message,
            base.level if base else None,
            base.led_flashes if base else None,
            base.uid if base else None,
            base.type if base else "",
            base.display if base else "",
            base.description if base else "",
            base.action if base else "",
            tuple(aliases), tuple(sources),
        )
    return out


def render() -> str:
    t301 = sp.read_text("CiA-301-V4.2")
    t402 = sp.read_text("CiA-DSP402-V2.0")
    t401 = sp.read_text("CiA-DS401-V2.1")
    man = sp.read_text(sp.GEN4_MANUAL)
    manifest = json.loads((sp.SPECS / "MANIFEST.json").read_text())
    entries = manifest.get("sources", manifest)

    parts = [HEADER]
    parts.append(_dict("SOURCE_DOCUMENTS", "Dict[str, str]",
                       ((repr(d), entries[d]["sha256"]) for d in DOCUMENTS)))
    parts.append(_dict("SDO_ABORT_CODES", "Dict[int, str]",
                       ((_hexkey(k, 8), v) for k, v in sorted(sp.parse_sdo_abort_codes(t301).items()))))
    classes = sp.parse_emcy_classes(t301)
    parts.append("#: Error code classes keyed by the high byte of the EMCY error code.")
    parts.append(_dict("EMCY_ERROR_CLASSES", "Dict[int, str]",
                       ((_hexkey(int(k[:2], 16), 2), v) for k, v in classes.items())))
    parts.append(_dict("EMCY_ERROR_CODES", "Dict[int, str]",
                       ((_hexkey(k, 4), v) for k, v in sorted(sp.parse_emcy_codes(t301).items()))))
    parts.append(_dict("DATA_TYPES", "Dict[int, str]",
                       ((_hexkey(k, 4), v) for k, v in sorted(sp.parse_data_types(t301).items()))))
    parts.append(_dict("CIA301_OBJECTS", "Dict[int, ProfileObjectInfo]",
                       _profile(sp.parse_object_descriptions(t301))))
    parts.append("#: CiA 401 objects at their profile index; the Gen4 adds 0x800.")
    parts.append(_dict("CIA401_OBJECTS", "Dict[int, ProfileObjectInfo]",
                       _profile(sp.parse_object_descriptions(t401))))
    parts.append(_dict("CIA402_OBJECTS", "Dict[int, ProfileObjectInfo]",
                       _profile(sp.parse_object_descriptions(t402))))
    parts.append("#: (statusword mask, value, state name), CiA 402 Table 7.")
    parts.append(_list("DS402_STATES", "List[Tuple[int, int, str]]",
                       (_Raw(f"(0x{m:04X}, 0x{v:04X}, {n!r})") for m, v, n in sp.parse_ds402_states(t402))))
    parts.append("#: Device control commands, CiA 402 Table 4.")
    parts.append(_list("DS402_COMMANDS", "List[Ds402CommandInfo]",
                       (_Raw(f"Ds402CommandInfo({c.name!r}, {tuple(sorted(c.bits.items(), reverse=True))!r}, "
                             f"{c.transitions!r})") for c in sp.parse_ds402_commands(t402))))
    parts.append("#: 5310h last abort reason, Gen4 manual Table 33.")
    parts.append(_dict("GEN4_ABORT_REASONS", "Dict[int, str]",
                       sorted(sp.parse_gen4_abort_reasons(man).items())))
    parts.append("#: Fault LED flash count categories, Gen4 manual Table 34.")
    parts.append(_dict("GEN4_LED_CATEGORIES", "Dict[int, str]",
                       sorted(sp.parse_gen4_led_categories(man).items())))
    parts.append(_list("GEN4_RATINGS", "List[RatingInfo]",
                       (_Raw(f"RatingInfo({r.voltage!r}, {r.size}, {r.short_term_a}, {r.continuous_a})")
                        for r in sp.parse_gen4_ratings(man))))
    parts.append(_list("GEN4_VOLTAGE_RANGES", "List[VoltageRangeInfo]",
                       (_Raw(f"VoltageRangeInfo({v.voltage!r}, {v.conventional_min}, {v.conventional_max}, "
                             f"{v.limit_min}, {v.limit_max}, {v.overvoltage})")
                        for v in sp.parse_gen4_voltage_ranges(man))))
    faults = merge_faults()
    parts.append(_dict("GEN4_FAULTS", "Dict[int, FaultInfo]",
                       ((_hexkey(fid, 4), _Raw("FaultInfo(" + ", ".join(
                           _hexkey(x, 4) if i == 0 else repr(x) for i, x in enumerate(f)) + ")"))
                        for fid, f in faults.items())))
    parts.append("#: Product codes (1018h sub 2) of known units, from OVMS.")
    parts.append(_dict("GEN4_KNOWN_PRODUCT_CODES", "Dict[int, str]",
                       ((_hexkey(code, 8), desc) for _, (code, desc) in
                        sorted(sp.parse_ovms_product_codes().items(), key=lambda kv: kv[1][0]))))
    return "\n".join(parts)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--check", action="store_true", help="fail if tables.py differs from the specs")
    args = ap.parse_args()
    text = render()
    if args.check:
        if not OUT.exists() or OUT.read_text() != text:
            print(f"{OUT} is stale; run tools/generate_tables.py", file=sys.stderr)
            return 1
        print("tables.py up to date")
        return 0
    OUT.write_text(text)
    print(f"wrote {OUT} ({len(text.splitlines())} lines)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
