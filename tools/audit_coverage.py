#!/usr/bin/env python3
"""Account for explicit Gen4 manual addresses against every pinned EDS/DCF.

    python tools/audit_coverage.py --check
    python tools/audit_coverage.py --json
    python tools/audit_coverage.py --require-complete

Known missing schemas remain unresolved. A successful freshness check is not
a claim that a particular controller implements every documented function.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from sevcon_can.od import load_eds
from spec_parser import _OBJ_REF_RE

OUT = ROOT / "docs" / "COVERAGE.md"

# These denote broad address regions, not assertions that each address exists.
RANGES = {
    0x15FF, 0x17FF, 0x19FF, 0x1BFF, 0x2000, 0x20FF, 0x22FF, 0x24FF,
    0x25FF, 0x2600, 0x26FF, 0x292F, 0x3FFF, 0x6000,
}

# Review notes only: none of these creates an object or redirects an address.
UNRESOLVED = {
    0x2137: "Traction inhibit in V2.0/V2.2; no matching dictionary schema.",
    0x2780: "Apply hours-counter offsets in older manuals; no matching schema.",
    0x2787: "Vehicle drive hours in the Size 2/6 manual; no matching schema.",
    0x2905: "Inconsistent references to inching, brake fault behavior and feathering. Pinned dictionaries put inching at 2919h, brake parameters at 2903h and feathering at 290Eh. No verified address alias.",
    0x291B: "Application note's speed-limit ramps resemble dictionary 291Eh, but no schema or verified address alias for 291Bh.",
    0x2925: "Service-required driveability profile; no matching schema.",
    0x2929: "Power-steering speed threshold in the Size 2/6 manual; no matching schema.",
    0x3100: "Digital-output VPDO mapping documented; no matching schema in the pinned controller dictionaries.",
    0x4613: "Endpoint of an older motor-map range; no schema establishes whether this map exists on a target firmware.",
    0x4691: "Older analogue-input wire-off configuration; no matching schema.",
    0x4692: "Older analogue-input two-wire configuration; no matching schema.",
    0x46B0: "Start of the older analogue-output characteristic range 46B0h to 46B5h; no matching schema.",
    0x46B5: "End of the older analogue-output characteristic range 46B0h to 46B5h; no matching schema.",
    0x5301: "Older fault-clear object; no matching schema. No fault-reset alias is assumed.",
    0x5302: "Older fault-clear object; no matching schema. No fault-reset alias is assumed.",
    0x5510: "Customer product code in the Size 2/6 manual; no matching schema.",
    0x6026: "Application-note Sdo_ro example for removing a mapping; no matching schema or verified correction.",
    0x60F9: "Application note identifies this as the espAC speed-loop address and 4651h as the Gen4 address; not evidence of Gen4 support.",
    0x6410: "Older manuals name this motor-data address; the application note assigns it to espAC and 4641h to Gen4. Firmware-specific schema is unresolved.",
}

UNRESOLVED_SUBS = {
    (0x2903, 4): "Minimum brake-coil current/wire-off detection; absent from pinned schemas.",
    (0x2903, 6): "Brake holding-torque check; absent from pinned schemas.",
    (0x290E, 4): "Feathering configuration in the newer manual; absent from pinned schemas.",
    (0x290E, 5): "Feathering configuration in the newer manual; absent from pinned schemas.",
    (0x2910, 17): "Included in the manual's broad throttle subindex range 2 to 21; absent from pinned schemas.",
    (0x2910, 18): "Included in the manual's broad throttle subindex range 2 to 21; absent from pinned schemas.",
    (0x2910, 19): "Included in the manual's broad throttle subindex range 2 to 21; absent from pinned schemas.",
    (0x2910, 20): "Included in the manual's broad throttle subindex range 2 to 21; absent from pinned schemas.",
    (0x2910, 22): "Additional driveability flags in the newer manual; absent from pinned schemas.",
    (0x3200, 4): "Older manual's fourth analogue output; pinned dictionaries describe fewer outputs.",
    (0x4630, 15): "Sin/Cos latency numbering conflicts with the pinned EDS. Other latency fields are not assumed to be aliases.",
}


def direct_subindices(tail: str) -> list[int]:
    """Parse only subindices explicitly attached to an address, in decimal."""
    import re
    match = re.match(
        r"^\s*(?:,\s*(?:sub[- ]*(?:indices|index)\s*)?"
        r"|sub[- ]*(?:indices|index)\s+)(\d{1,3})"
        r"(?:\s*(to|and|\+|,)\s*(\d{1,3}))?", tail, re.IGNORECASE)
    if not match:
        return []
    start = int(match[1])
    if match[3] is None:
        return [start] if start <= 255 else []
    end = int(match[3])
    if start > 255 or end > 255:
        return []
    if match[2].lower() == "to" and end >= start:
        return list(range(start, end + 1))
    return [start, end]


def audit(root: Path = ROOT) -> dict:
    paths = sorted([*(root / "specs/od").glob("*.eds"),
                    *(root / "specs/od").glob("*.dcf")])
    dictionaries = {p.name: load_eds(p) for p in paths}
    if "Gen4.eds" not in dictionaries:
        raise ValueError("the baseline Gen4.eds is missing")
    schemas = {name: {v.key for v in od.variables()}
               for name, od in dictionaries.items()}
    supplement_path = root / "src/sevcon_can/data/Gen4-supplement.eds"
    supplement = load_eds(supplement_path) if supplement_path.exists() else None
    supplemental_keys = {v.key for v in supplement.variables()} if supplement else set()
    found = {}
    manuals = sorted([*(root / "specs/text").glob("Gen4*.txt"),
                      *(root / "specs/text").glob("Sevcon*.txt")])
    if not manuals:
        raise ValueError("no manufacturer manual text files found")
    for path in manuals:
        # splitlines also treats PDF form feeds as lines, breaking source links.
        for line_number, line in enumerate(path.read_text().split("\n"), 1):
            for match in _OBJ_REF_RE.finditer(line):
                index = int(match[1] or match[2], 16)
                occurrence = {"file": str(path.relative_to(root)),
                              "line": line_number, "text": line.strip()}
                found.setdefault((index, None), []).append(occurrence)
                for subindex in direct_subindices(line[match.end():]):
                    found.setdefault((index, subindex), []).append(occurrence)

    references = []
    for (index, subindex), occurrences in sorted(
            found.items(), key=lambda item: (item[0][0], -1 if item[0][1] is None else item[0][1])):
        evidence = [name for name, od in dictionaries.items()
                    if (index in od.objects if subindex is None
                        else (index, subindex) in schemas[name])]
        if "Gen4.eds" in evidence:
            status, note = "baseline-schema", "Defined in the bundled firmware dictionary."
        elif evidence:
            status, note = "alternate-schema", "Defined only in another firmware dictionary; support on the target controller is unverified."
        elif index in RANGES and subindex is None:
            status, note = "range-boundary", "Boundary of a documented address region; not a declaration that this address exists."
        elif index in UNRESOLVED:
            status, note = "unresolved", UNRESOLVED[index]
        elif (index, subindex) in UNRESOLVED_SUBS:
            status, note = "unresolved", UNRESOLVED_SUBS[index, subindex]
        else:
            status, note = "unreviewed", "No dictionary schema or reviewed classification."
        if "Gen4.eds" in evidence:
            implementation = "baseline"
            implementation_source = "Gen4.eds"
        elif supplement is not None and (
                index in supplement.objects if subindex is None
                else (index, subindex) in supplemental_keys):
            implementation = "optional-supplement"
            entry = (supplement[index] if subindex is None
                     else supplement.variable((index, subindex)))
            implementation_source = entry.extra.get("SUPPLEMENTAL_SOURCE")
        else:
            implementation = "custom-eds-only" if evidence else "none"
            implementation_source = None
        references.append({"index": index, "subindex": subindex,
                           "status": status, "note": note,
                           "implementation": implementation,
                           "implementation_source": implementation_source,
                           "dictionaries": evidence, "occurrences": occurrences})
    return {
        "manuals": [str(p.relative_to(root)) for p in manuals],
        "dictionaries": [{"file": name, "objects": len(od.objects),
                          "variables": len(schemas[name]),
                          "product": od.product_code, "revision": od.revision}
                         for name, od in dictionaries.items()],
        "references": references,
        "counts": dict(sorted(Counter(r["status"] for r in references).items())),
    }


def render(report: dict) -> str:
    refs = report["references"]
    index_count = sum(r["subindex"] is None for r in refs)
    lines = ["# Gen4 object coverage audit", "",
             "Generated by `python tools/audit_coverage.py`. Do not edit by hand.", "",
             f"The audit accounts for {index_count} distinct explicit index references and "
             f"{len(refs) - index_count} directly attached index/subindex references "
             f"across {len(report['manuals'])} pinned manufacturer documents.", "",
             "It recognizes both `291fh` and `0x291F`, decimal subindices, explicit "
             "subindex lists and ranges. It does not infer implicit prose back-references "
             "or expand `41X0h` templates. Each row retains its source text in the JSON report.", "",
             "A schema in another firmware dictionary does not establish support on the "
             "connected controller. Missing schemas are unresolved, not unsupported. "
             "No datatype, scaling, access restriction or address alias is invented for them. "
             "This report measures documentation coverage, not hardware acceptance or "
             "proprietary DVT/firmware-download completeness.", "",
             "`--check` rejects stale output and newly unreviewed addresses. "
             "`--require-complete` also rejects every unresolved schema. "
             "`--json` includes every occurrence with its filename, line and quoted context.", "",
             "| Classification | References |", "| --- | ---: |"]
    for status, count in report["counts"].items():
        lines.append(f"| {status} | {count} |")
    lines += ["", "## Firmware dictionaries", "",
              "Files are kept separate because an address can have different datatypes, "
              "units or meanings between firmware versions. Counts describe each source "
              "independently, including record count entries. Baseline entries are available "
              "through the default client. Optional-supplement entries require an explicit "
              "supplemental selection, and use the source named in the JSON report. "
              "Custom-EDS-only entries require selecting that exact EDS/DCF. A row listing "
              "several source dictionaries does not claim that their definitions agree.", "",
              "| Dictionary | Product | Revision | Objects | Entries |", "| --- | --- | --- | ---: | ---: |"]
    for dictionary in report["dictionaries"]:
        lines.append(f"| [{dictionary['file']}](../specs/od/{dictionary['file']}) "
                     f"| 0x{dictionary['product']:08X} | 0x{dictionary['revision']:08X} "
                     f"| {dictionary['objects']} | {dictionary['variables']} |")

    lines += ["", "## Unresolved schemas", "",
              "These functions remain open until matching firmware metadata or a verified "
              "manufacturer definition is available. Similar names at other addresses "
              "are review leads, not automatic corrections.", "",
              "| Address | Reason | Evidence |", "| --- | --- | --- |"]
    for reference in refs:
        if reference["status"] in {"unresolved", "unreviewed"}:
            lines.append(f"| {_address(reference)} | {reference['note']} | {_link(reference)} |")
    lines += ["", "## All explicit references", "",
              "Index-only rows establish that a record exists, not that every subindex "
              "mentioned elsewhere exists. Attached subindices have their own rows.", "",
              "| Address | Classification | Client access | Dictionary evidence | First manual occurrence |",
              "| --- | --- | --- | --- | --- |"]
    for reference in refs:
        dictionaries = ", ".join(reference["dictionaries"]) or "none"
        lines.append(f"| {_address(reference)} | {reference['status']} | "
                     f"{reference['implementation']} | {dictionaries} | {_link(reference)} |")
    return "\n".join(lines) + "\n"


def _address(reference: dict) -> str:
    subindex = reference["subindex"]
    return f"0x{reference['index']:04X}" + (f":{subindex:02X}" if subindex is not None else "")


def _link(reference: dict) -> str:
    occurrence = reference["occurrences"][0]
    return f"[{Path(occurrence['file']).name}:{occurrence['line']}](../{occurrence['file']}#L{occurrence['line']})"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--json", action="store_true", help="print the complete report as JSON")
    parser.add_argument("--require-complete", action="store_true")
    args = parser.parse_args(argv)
    report = audit()
    output = render(report)
    if args.json:
        print(json.dumps(report, indent=2))
    elif args.check:
        if not OUT.exists() or OUT.read_text() != output:
            print("COVERAGE.md is stale; run python tools/audit_coverage.py", file=sys.stderr)
            return 1
    elif not args.require_complete:
        OUT.write_text(output)
        print(f"wrote {OUT.relative_to(ROOT)}")
    unresolved = report["counts"].get("unresolved", 0)
    unreviewed = report["counts"].get("unreviewed", 0)
    if not args.json:
        print(f"Coverage audit: {len(report['references'])} references, "
              f"{unresolved} unresolved, {unreviewed} unreviewed")
    return int(bool(unreviewed or (args.require_complete and unresolved)))


if __name__ == "__main__":
    raise SystemExit(main())
