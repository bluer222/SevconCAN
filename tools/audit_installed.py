#!/usr/bin/env python3
"""Verify installed OD coverage from an existing 5630h export, without CAN I/O.

    python tools/audit_installed.py installed-dictionary.json
    python tools/audit_installed.py installed-dictionary.json --json

This gate checks the selected dictionary against the exported firmware table.
It does not establish controller identity, missing limits or hardware behavior.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys
from typing import Iterable, Optional

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from sevcon_can.discovery import compare_dictionary
from sevcon_can.exceptions import ObjectDictionaryError
from sevcon_can.od import ObjectDictionary, bundled_eds_path, load_eds
from audit_coverage import audit

RAW_FIELDS = {
    "table_index": 0xFFFF, "index": 0xFFFF, "subindex": 0xFF,
    "object_type": 0xFF, "internal_data_type": 0xFF, "access_code": 0xFF,
    "low_limit_raw": 0xFFFFFFFF, "high_limit_raw": 0xFFFFFFFF,
    "pdo_mapping_raw": 0xFF, "object_version": 0xFFFF,
    "object_flags": 0xFF,
}


def _integer(value, maximum: int, name: str) -> int:
    if type(value) is not int or not 0 <= value <= maximum:
        raise ValueError(f"{name} must be an integer in 0..{maximum}")
    return value


def validate_export(report: dict) -> None:
    """Reject malformed, incoherent or repeated metadata before comparison."""
    if not isinstance(report, dict):
        raise ValueError("installed export must be a JSON object")
    count = _integer(report.get("entry_count"), 0xFFFF, "entry_count")
    start = _integer(report.get("start"), 0xFFFF, "start")
    complete = report.get("complete")
    if type(complete) is not bool:
        raise ValueError("complete must be a boolean")
    entries = report.get("entries")
    if not isinstance(entries, list):
        raise ValueError("entries must be an array")
    if start > count or start + len(entries) > count:
        raise ValueError("exported rows exceed the reported table bounds")
    if complete and (start != 0 or len(entries) != count):
        raise ValueError("complete export must contain every row starting at zero")
    seen = set()
    for offset, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise ValueError(f"row {offset} must be an object")
        for field, maximum in RAW_FIELDS.items():
            if field not in entry:
                raise ValueError(f"row {offset} is missing {field}")
            if field == "object_flags" and entry[field] is None:
                continue
            _integer(entry[field], maximum, f"row {offset} {field}")
        if entry["table_index"] != start + offset:
            raise ValueError(f"row {offset} has a nonsequential table_index")
        key = entry["index"], entry["subindex"]
        if key[0] == 0 or key in seen:
            raise ValueError(f"invalid or duplicate address {key[0]:04X}:{key[1]:02X}")
        seen.add(key)


def _projection(references: Iterable[dict], installed: set, complete: bool) -> dict:
    references = list(references)
    indices = {index for index, subindex in installed}
    present = [{"index": ref["index"], "subindex": ref["subindex"]}
               for ref in references if (
                   ref["index"] in indices if ref["subindex"] is None
                   else (ref["index"], ref["subindex"]) in installed)]
    remaining = len(references) - len(present)
    return {"total": len(references), "installed": present,
            "not_installed": remaining if complete else None,
            "unverified": 0 if complete else remaining}


def audit_installed(report: dict, od: ObjectDictionary, *,
                    unresolved_references: Iterable[dict] = (),
                    supplemental_keys: Iterable[tuple] = ()) -> dict:
    """Recompute coverage from raw rows, ignoring cached export summaries."""
    validate_export(report)
    dictionary_entries = sum(1 for _ in od.variables())
    if not dictionary_entries:
        raise ValueError("selected dictionary contains no variable entries")
    complete = report["complete"]
    comparison = compare_dictionary(od, report["entries"], complete=complete)
    installed = {(row["index"], row["subindex"]) for row in comparison["entries"]}
    passed = complete and not any(comparison[name] for name in (
        "missing_from_dictionary", "metadata_mismatches", "absent_from_controller"))
    result = {"scope": "installed-object-dictionary", "passed": passed,
              "complete": complete, "entry_count": report["entry_count"],
              "rows_checked": len(installed),
              "dictionary_entries": dictionary_entries,
              "identity_verified": False,
              "limit_summary": comparison["limit_summary"],
              "missing_from_dictionary": comparison["missing_from_dictionary"],
              "metadata_mismatches": comparison["metadata_mismatches"],
              "absent_from_controller": comparison["absent_from_controller"],
              "global_unresolved_references": _projection(
                  unresolved_references, installed, complete),
              "optional_supplement_entries": _projection(
                  ({"index": index, "subindex": subindex}
                   for index, subindex in sorted(supplemental_keys)), installed, complete)}
    return result


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("installed", type=Path, help="existing installed dictionary JSON export")
    parser.add_argument("--eds", type=Path, help="exact EDS/DCF to compare; defaults to bundled EDS")
    parser.add_argument("--json", action="store_true", help="print the recomputed report as JSON")
    args = parser.parse_args(argv)
    try:
        source = args.installed.read_bytes()
        od = load_eds(args.eds or bundled_eds_path())
        global_report = audit()
        baseline = load_eds(bundled_eds_path())
        baseline_keys = {var.key for var in baseline.variables()}
        supplement = load_eds(bundled_eds_path().with_name("Gen4-supplement.eds"))
        supplemental_keys = {var.key for var in supplement.variables()} - baseline_keys
        result = audit_installed(
            json.loads(source), od,
            unresolved_references=(ref for ref in global_report["references"]
                                   if ref["status"] == "unresolved"),
            supplemental_keys=supplemental_keys)
    except (OSError, ValueError, KeyError, ObjectDictionaryError) as error:
        print(f"Installed dictionary audit failed: {error}", file=sys.stderr)
        return 1
    result["source_sha256"] = hashlib.sha256(source).hexdigest()
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print(f"Installed dictionary audit {'PASSED' if result['passed'] else 'FAILED'}: "
              f"{result['rows_checked']}/{result['entry_count']} rows checked; "
              f"{result['dictionary_entries']} EDS entries")
        print(f"Missing definitions: {len(result['missing_from_dictionary'])}; "
              f"metadata disagreements: {len(result['metadata_mismatches'])}; "
              f"EDS entries absent from controller: "
              f"{len(result['absent_from_controller']) if result['complete'] else 'unverified'}")
        limits = result["limit_summary"]
        print(f"Limits reported: {limits['reported']}; "
              f"unreported or ambiguous: {limits['unreported_or_ambiguous']}")
        for label, field in (("Global unresolved references", "global_unresolved_references"),
                             ("Optional supplemental entries", "optional_supplement_entries")):
            projection = result[field]
            print(f"{label}: {len(projection['installed'])} installed; "
                  f"{projection['not_installed'] if result['complete'] else 'unverified'} not installed")
        print("This checks exported OD coverage; it does not verify identity or hardware behavior.")
    return int(not result["passed"])


if __name__ == "__main__":
    raise SystemExit(main())
