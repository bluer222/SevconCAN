#!/usr/bin/env python3
"""Offline acceptance gate: spec pins + tables freshness + test suite.

    python conformance/run.py

Fails on missing evidence (modified spec, stale tables, failing test).
Needs no network and no CAN hardware.
"""
from __future__ import annotations

import subprocess
import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def run(*argv: str) -> int:
    proc = subprocess.run([sys.executable, *argv], cwd=ROOT,
                          capture_output=True, text=True)
    print(proc.stdout, end="")
    print(proc.stderr, end="")
    return proc.returncode


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--interop", action="store_true",
                        help="also require the independent canopen/python-can gate")
    args = parser.parse_args()
    failures = 0

    print("== spec pins ==")
    if run("tools/fetch_specs.py", "--check") != 0:
        failures += 1

    print("== tables freshness ==")
    if run("tools/generate_tables.py", "--check") != 0:
        failures += 1

    print("== test suite ==")
    if run("-m", "unittest", "discover", "-s", "tests") != 0:
        failures += 1

    if args.interop:
        print("== independent CANopen stack ==")
        if run("conformance/interop.py") != 0:
            failures += 1

    print("== result ==")
    if failures:
        print(f"CONFORMANCE FAILED ({failures} stage(s))")
        return 1
    print("CONFORMANCE PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
