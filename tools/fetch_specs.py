#!/usr/bin/env python3
"""
Fetch every public document describing the Sevcon Gen4 CANopen interface.

Sources fall into three groups:

* Sevcon/BorgWarner Gen4 manuals, datasheet and fault-code sheets (PDF).
* CiA specifications the Gen4 claims conformance to: DS301 (communication
  profile), DS401 V2.1 (generic I/O) and DSP402 V2.x (drives).
* Machine-readable object dictionaries: a Sevcon-generated Gen4 EDS and
  three DCF dumps taken from real controllers.

Layout produced under ``specs/``::

    specs/pdf/<name>.pdf        downloaded PDFs (raw, pinned by sha256)
    specs/text/<name>.txt       ``pdftotext -layout`` extraction
    specs/od/<name>.eds|.dcf    object dictionaries
    specs/extra/...             unpacked archives, misc files
    specs/MANIFEST.json         url, sha256, size, fetched_on per file

Modes:

* default        download files that are missing locally, then extract.
* ``--check``    offline: verify every local file against MANIFEST sha256.
* ``--refresh``  re-download everything and report content changes.

``pdftotext`` comes from poppler. On NixOS::

    NIXPKGS_ALLOW_UNFREE=1 nix-shell -p poppler-utils unrar \
        --run 'python3 tools/fetch_specs.py'
"""

from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parent.parent
SPECS = ROOT / "specs"
MANIFEST_PATH = SPECS / "MANIFEST.json"

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

_GH = "https://raw.githubusercontent.com/brtiberio/Sevcon_gen4/master"
_OVMS = ("https://raw.githubusercontent.com/openvehicles/Open-Vehicle-Monitoring-System-3/"
         "master/vehicle/OVMS.V3/components/vehicle_renaulttwizy/src")
_VIC = "https://raw.githubusercontent.com/victronenergy/dbus-canopen-motordrive/master"


@dataclass(frozen=True)
class Source:
    name: str
    kind: str  # "pdf" | "od" | "extra"
    urls: Tuple[str, ...]
    description: str
    filename: Optional[str] = None
    #: Files the user supplied locally take precedence over a download.
    local: Tuple[str, ...] = field(default_factory=tuple)

    @property
    def target(self) -> Path:
        if self.kind == "pdf":
            return SPECS / "pdf" / f"{self.name}.pdf"
        sub = "od" if self.kind == "od" else "extra"
        return SPECS / sub / (self.filename or self.name)


SOURCES: List[Source] = [
    # ---- Sevcon Gen4 manuals ------------------------------------------
    Source(
        "Gen4-Product-Manual-V3.4", "pdf",
        (
            "https://www.e-kart.fr/images/stories/technique/SEVCON/GEN4-Product-Manual-V3-4.pdf",
            "https://www.thunderstruck-ev.com/images/Gen4%20Product%20Manual%20V3%204.pdf",
            f"{_GH}/documents/Manuals/Gen4%20Product%20Manual%20V3%204.pdf",
        ),
        "Gen4 Applications Reference Manual, doc 177/52701 Rev 3.4",
        local=("Gen4 Product Manual V3 4.pdf",),
    ),
    Source(
        "Gen4-Product-Manual-V3.3", "pdf",
        ("https://www.e-kart.fr/images/stories/technique/SEVCON/GEN4-Product-Manual-V3-3.pdf",),
        "Gen4 Applications Reference Manual, doc 177/52701 Rev 3.3",
    ),
    Source(
        "Gen4-Size-2-6-Product-Manual", "pdf",
        ("https://electricdriveengineering.com.au/wp-content/uploads/2023/02/Gen4-Size-2-6-Product-Manual.pdf",),
        "Gen4 Size 2/4/6 Product Manual (BorgWarner era); includes 'Controlling Motors via CANopen'",
    ),
    Source(
        "Gen4-Product-Manual-V2.2", "pdf",
        (
            "http://www.quadmotion.se/shop/files/214845_Sevcon48V/Gen4_Product_Manual_V2.2.en.pdf",
            f"{_GH}/documents/Manuals/Gen4_Product_Manual_V2.2.en.pdf",
        ),
        "Gen4 Product Manual V2.2",
    ),
    Source(
        "Gen4-Product-Manual-V2.0", "pdf",
        ("https://www.e-kart.fr/images/stories/technique/SEVCON/GEN4-Product-Manual-V2-0-30-01-2009.pdf",),
        "Gen4 Product Manual V2.0x (2009)",
    ),
    Source(
        "Gen4-Datasheet-Size-2-4-6-2016", "pdf",
        (f"{_GH}/documents/Datasheet/gen4-size-2-4-6-july-2016.pdf",),
        "Gen4 Size 2/4/6 datasheet, July 2016",
    ),
    Source(
        "Gen4-Fault-Codes-Short", "pdf",
        ("https://www.thunderstruck-ev.com/images/companies/1/Gen4%20fault%20code%20short%20version.pdf",),
        "Gen4 LED flash fault identification, short version (Thunderstruck)",
    ),
    Source(
        "Sevcon-AppNote-Vehicle-Setup", "pdf",
        (f"{_GH}/documents/Manuals/SEVCON_App-Note_Vehicle-Setup-and-Configuration.pdf",),
        "Sevcon application note: vehicle setup and configuration",
    ),
    Source(
        "Sevcon-DVT-Tutorial", "pdf",
        (f"{_GH}/documents/Manuals/DVT%20Tutorial.pdf",),
        "Sevcon DVT (DriveWizard) tutorial",
    ),
    Source(
        "Sevcon-Gen4-Programming-Cable", "pdf",
        (f"{_GH}/documents/other%20docs/Cable-35-broches-PROG.pdf",),
        "Gen4 35-pin programming cable pinout",
    ),
    # ---- CiA specifications -------------------------------------------
    Source(
        "CiA-DS301-V4.02", "pdf",
        (
            "http://overpof.free.fr/schneider/CAN%20&%20CANopen/CANopen/%A9CiA%20CANCANopen%20CD%20V5.1/standard/ds301.pdf",
            "https://doc.ioot.pro/_downloads/744ecdbdca5af7b419b8f5463f6f1b29/ds301.pdf",
        ),
        "CiA DS301 V4.02 Application layer and communication profile",
    ),
    Source(
        "CiA-301-V4.2", "pdf",
        ("https://forum.opencyphal.org/uploads/short-url/mNWuvY23DYckSFoSbfam0ji3YWn.pdf",),
        "CiA 301 V4.2.0 Application layer and communication profile",
    ),
    Source(
        "CiA-DSP402-V2.0", "pdf",
        ("http://overpof.free.fr/schneider/CAN%20&%20CANopen/CANopen/%A9CiA%20CANCANopen%20CD%20V5.1/standard/dsp402.pdf",),
        "CiA DSP402 V2.0 Device profile drives and motion control",
    ),
    Source(
        "CiA-DS401-V2.1", "pdf",
        (
            "https://doc.ioot.pro/_downloads/22c01eff2273ea4ca6b09dba94106703/ds401.pdf",
            "http://overpof.free.fr/schneider/CAN%20&%20CANopen/CANopen/%A9CiA%20CANCANopen%20CD%20V5.1/standard/ds401.pdf",
        ),
        "CiA DS401 V2.1 Device profile for generic I/O modules",
    ),
    Source(
        "CiA-DSP306-V1.3", "pdf",
        ("http://overpof.free.fr/schneider/CAN%20&%20CANopen/CANopen/%A9CiA%20CANCANopen%20CD%20V5.1/standard/dsp306.pdf",),
        "CiA DSP306 Electronic data sheet specification",
    ),
    # ---- Object dictionaries ------------------------------------------
    Source(
        "Gen4.eds", "od", (f"{_GH}/Gen4.eds",),
        "Sevcon-generated EDS, product 0x07055037 rev 0x00010026",
    ),
    Source(
        "Gen4-AC24ls-rev0x00010017.dcf", "od",
        ("https://gist.githubusercontent.com/kasparsd/1e164f0f9ad1a61448a96deb7bd0682c/"
         "raw/8733cd63aadb74b0bd7c47054183b72963e0cc55/AC24ls.dcf",),
        "Sevcon-generated AC24ls DCF, product 0x07065037 rev 0x00010017; includes creep torque",
    ),
    Source(
        "Gen4-110V-300A-rev0x00010026-a.dcf", "od",
        (f"{_GH}/documents/2019-06-13-ME0913-0705-0013-GEN4-110V-300A-PARINAUTES-Ok-a-vide-19h39.dcf",),
        "DCF dump, Gen4 110V 300A, firmware rev 0x00010026",
    ),
    Source(
        "Gen4-110V-300A-rev0x00010026-b.dcf", "od",
        (f"{_GH}/documents/2019-06-25-ME0913-0705-0013-GEN4-110V-300A-KES-Ok-a-vide-10h11-Pre-Op.dcf",),
        "DCF dump, Gen4 110V 300A, firmware rev 0x00010026 (pre-operational)",
    ),
    Source(
        "Gen4-110V-300A-rev0x00010015.dcf", "od",
        (f"{_GH}/documents/2019-06-24-MOTEUR-UK0319-55-110V-300A-S4-GEN4-Config-origine.dcf",),
        "DCF dump, Gen4 110V 300A size 4, firmware rev 0x00010015",
    ),
    Source(
        "fault-codes.rar", "extra", (f"{_GH}/documents/fault%20codes.rar",),
        "Archive of Gen4 fault code tables",
    ),
    Source(
        "SEVCON-GEN4-Calculs-tensions-batteries.xlsx", "extra",
        (f"{_GH}/documents/SEVCON-GEN4-Calculs-tensions-batteries.xlsx",),
        "Battery voltage threshold calculator",
    ),
    # ---- Independent field implementations (cross-checks) ------------
    Source(
        "ovms-rt_sevcon_faults.cpp", "extra", (f"{_OVMS}/rt_sevcon_faults.cpp",),
        "OVMS Renault Twizy (Gen4) fault table and EMCY decoding",
    ),
    Source(
        "ovms-rt_sevcon.cpp", "extra", (f"{_OVMS}/rt_sevcon.cpp",),
        "OVMS Renault Twizy Gen4 client: login (5000h), NMT state (5110h)",
    ),
    Source(
        "ovms-dcf-download.pl", "extra",
        ("https://raw.githubusercontent.com/openvehicles/Open-Vehicle-Server/master/"
         "clients/diagmode/dcf_download.pl",),
        "OVMS installed Sevcon OD enumeration through 5630h, including internal datatype codes",
    ),
    Source(
        "ovms-rt_sevcon.h", "extra", (f"{_OVMS}/rt_sevcon.h",),
        "OVMS Renault Twizy Gen4 product codes (1018h,2)",
    ),
    Source(
        "victron-sevcon.c", "extra", (f"{_VIC}/src/drivers/sevcon.c",),
        "Victron dbus-canopen-motordrive Gen4 driver: fault table, EMCY decoding",
    ),
    Source(
        "victron-sevcon_gen4.py", "extra", (f"{_VIC}/tools/simulators/sevcon_gen4.py",),
        "Victron Gen4 simulator: 5100h scaling, EMCY example",
    ),
]


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def load_manifest() -> Dict[str, dict]:
    if MANIFEST_PATH.exists():
        return json.loads(MANIFEST_PATH.read_text())
    return {}


def save_manifest(manifest: Dict[str, dict]) -> None:
    MANIFEST_PATH.parent.mkdir(parents=True, exist_ok=True)
    MANIFEST_PATH.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")


def _download(url: str, timeout: float = 60.0) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def _looks_valid(src: Source, data: bytes) -> bool:
    if src.kind == "pdf":
        return data[:5] == b"%PDF-"
    if src.target.suffix in (".eds", ".dcf"):
        return b"[FileInfo]" in data[:4096] or b"[DeviceInfo]" in data[:8192]
    if src.target.suffix == ".rar":
        return data[:4] == b"Rar!"
    return len(data) > 0


def fetch(src: Source, force: bool = False) -> Tuple[Optional[Path], str]:
    """Return (path, origin). origin is the URL or 'local:<file>'."""
    target = src.target
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists() and not force:
        return target, "cached"
    for local in src.local:
        p = ROOT / local
        if p.exists():
            shutil.copyfile(p, target)
            return target, f"local:{local}"
    errors = []
    for url in src.urls:
        try:
            data = _download(url)
        except (urllib.error.URLError, OSError) as e:
            errors.append(f"{url}: {e}")
            continue
        if not _looks_valid(src, data):
            errors.append(f"{url}: unexpected content ({data[:16]!r})")
            continue
        target.write_bytes(data)
        return target, url
    print(f"  FAILED {src.name}:", *errors, sep="\n    ", file=sys.stderr)
    return None, ""


def extract_text(pdf: Path) -> Optional[Path]:
    exe = shutil.which("pdftotext")
    if not exe:
        print("  pdftotext not found; skipping text extraction", file=sys.stderr)
        return None
    out = SPECS / "text" / (pdf.stem + ".txt")
    out.parent.mkdir(parents=True, exist_ok=True)
    res = subprocess.run([exe, "-layout", str(pdf), str(out)], capture_output=True, text=True)
    if res.returncode != 0:
        print(f"  pdftotext failed on {pdf.name}: {res.stderr.strip()}", file=sys.stderr)
        return None
    return out


def unpack(archive: Path) -> Optional[Path]:
    # The fault-code RAR uses a method that only unrar decodes (7-Zip and
    # p7zip report "Unsupported Method"). unrar is unfree in nixpkgs:
    #   NIXPKGS_ALLOW_UNFREE=1 nix-shell -p unrar poppler-utils --run ...
    exe = shutil.which("unrar")
    if not exe:
        print(f"  no unrar; leaving {archive.name} packed", file=sys.stderr)
        return None
    dest = archive.parent / archive.stem
    dest.mkdir(exist_ok=True)
    res = subprocess.run([exe, "x", "-o+", str(archive), str(dest) + "/"],
                         capture_output=True, text=True)
    if res.returncode != 0:
        print(f"  unpack failed: {res.stderr.strip()}", file=sys.stderr)
        return None
    for pdf in dest.rglob("*.pdf"):
        extract_text(pdf)
    return dest


def run(force: bool) -> int:
    manifest = load_manifest()
    today = datetime.datetime.now(datetime.timezone.utc).date().isoformat()
    failures = 0
    for src in SOURCES:
        old = manifest.get(src.name, {})
        path, origin = fetch(src, force=force)
        if path is None:
            failures += 1
            continue
        digest = sha256(path)
        if old.get("sha256") and old["sha256"] != digest:
            print(f"CHANGED {src.name}: {old['sha256'][:12]} -> {digest[:12]}")
        entry = {
            "description": src.description,
            "file": str(path.relative_to(ROOT)),
            "sha256": digest,
            "size": path.stat().st_size,
            "urls": list(src.urls),
            "origin": old.get("origin") if origin == "cached" else origin,
            "fetched_on": old.get("fetched_on") if origin == "cached" else today,
        }
        if src.kind == "pdf":
            txt = extract_text(path)
            if txt is not None:
                entry["text"] = str(txt.relative_to(ROOT))
        elif path.suffix == ".rar":
            dest = unpack(path)
            if dest is not None:
                entry["unpacked"] = sorted(
                    str(p.relative_to(ROOT)) for p in dest.rglob("*") if p.is_file()
                )
        manifest[src.name] = entry
        print(f"OK {src.name:48s} {entry['size']:>9,d}  {digest[:12]}  {entry['origin']}")
    save_manifest(manifest)
    return 1 if failures else 0


def check() -> int:
    manifest = load_manifest()
    bad = 0
    for src in SOURCES:
        entry = manifest.get(src.name)
        if entry is None:
            print(f"MISSING-MANIFEST {src.name}")
            bad += 1
            continue
        p = ROOT / entry["file"]
        if not p.exists():
            print(f"MISSING {src.name}")
            bad += 1
        elif sha256(p) != entry["sha256"]:
            print(f"MODIFIED {src.name}")
            bad += 1
        else:
            print(f"OK {src.name}")
    return 1 if bad else 0


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--check", action="store_true", help="verify local files against MANIFEST (offline)")
    g.add_argument("--refresh", action="store_true", help="re-download everything and report changes")
    args = ap.parse_args(argv)
    if args.check:
        return check()
    return run(force=args.refresh)


if __name__ == "__main__":
    sys.exit(main())
