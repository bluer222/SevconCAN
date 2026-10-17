"""Human-readable descriptions of key Gen4 objects ("what is statusword?").

Each entry is grounded in a specific source: the CiA profile tables in
``tables.py`` (parsed from the PDFs by ``tools/spec_parser.py``), the
Gen4 EDS, or the Size 2/4/6 Product Manual section noted. :func:`describe`
combines the curated paragraph with generated detail (profile lineage,
DS402 states/commands, enum labels) so the GUI and shell can explain
any object, not just list it.
"""
from __future__ import annotations

from typing import Dict, List, Optional

from .tables import (
    CIA301_OBJECTS,
    CIA401_OBJECTS,
    CIA402_OBJECTS,
    DS402_COMMANDS,
    DS402_STATES,
    GEN4_ABORT_REASONS,
)

#: Curated paragraphs: index -> description. Sources in brackets.
OBJECT_DESCRIPTIONS: Dict[int, str] = {
    0x6041: (
        "Statusword: 16-bit drive status (DSP402 9.2.2, read-only). "
        "Bits 0-3, 5 and 6 encode the device-control state "
        "(Not ready to switch on, Switch on disabled, Ready to switch on, "
        "Switched on, Operation enabled, Quick stop active, Fault reaction "
        "active, Fault). Bit 4 = voltage enabled, bit 5 = quick stop, "
        "bit 7 = warning, bit 8 = manufacturer specific, bit 9 = remote "
        "(Gen4 normally sets it), bit 10 = target reached, "
        "bit 11 = internal limit active. The client decodes the state "
        "with drive.decode_state()."),
    0x6040: (
        "Controlword: 16-bit drive command (DSP402 9.2.1, write-only "
        "pattern). Standard commands are Shutdown, Switch on, "
        "Disable voltage, Quick stop, Disable operation and Enable "
        "operation (bits 0-3); bit 7 high performs a Fault reset on a "
        "0-to-1 edge; bit 8 halts. Typical bring-up: Shutdown, "
        "Switch on, Enable operation (see device.enable_drive())."),
    0x6060: (
        "Mode of operation selection. Gen4 supports 3 = speed, 4 = torque "
        "and the Sevcon-specific 5 = voltage (EDS labels). Writable only "
        "in pre-operational. Keep consistent with the traction control "
        "mode 2900h (manual warns 2900h on the master and 6060h on "
        "traction nodes must agree). Read-back is 6061h."),
    0x6061: "Mode of operation display: the active mode (3 speed, 4 torque).",
    0x6071: "Target torque in torque mode, 0.1 % of peak (EDS scaling).",
    0x6072: "Maximum torque, 0.1 % of peak.",
    0x6074: "Torque demand (feedback), 0.1 % of peak.",
    0x6077: "Torque actual value (feedback), 0.1 % of peak.",
    0x6078: "Current actual value (feedback), A RMS.",
    0x5000: (
        "Sevcon login. Sub 3 = user ID, sub 2 = password (write-only), "
        "sub 1 = granted access level (read-only): 0 logged out, 1 User, "
        "2 Dealer, 3 Production, 4 Engineering, 5 SEVCON. The password is "
        "checked against an encryption of password + user ID + password "
        "key (5001h); passwords are customer-specific. Writing 0 logs "
        "out. Known-good engineering password on Twizy units: 0x4BDF "
        "(from OVMS, not from Sevcon documentation)."),
    0x5001: "Password key: customer-specific input to the login encryption.",
    0x5100: (
        "Device measurements (all read-only): sub 1 battery voltage, "
        "sub 2 battery current, sub 3 capacitor voltage (all x0.0625), "
        "sub 4 heatsink temperature (DegC), subs 6/7 fast 8-bit battery/"
        "capacitor volts (x0.25), sub 8 logic 12 V rail."),
    0x5110: ("NMT state mirror: 4 stopped, 5 operational, 127 "
             "pre-operational (same values as the heartbeat byte)."),
    0x2800: ("Force pre-operational (BOOLEAN, login level 4). Write 1 on "
             "the master to take the network pre-operational before "
             "writing pre-op-only objects, 0 to restore operational. "
             "DVT does this automatically; other tools must do it "
             "explicitly (see device.goto_preop())."),
    0x5300: ("Active fault list. Sub 1 counts active faults; write a "
             "position (0 = highest priority) to sub 2 and read the "
             "fault ID from sub 3. Resolve IDs via the fault table "
             "(faults.lookup) or 5610h."),
    0x5610: ("Event text. Write a fault ID to sub 1, read the "
             "controller's own description from sub 2."),
    0x5310: ("CANopen general abort reason: after an SDO abort 08000000h, "
             "this holds the specific cause (manual Table 33, e.g. 8 = "
             "access level too low). The client attaches it automatically."),
    0x1010: 'Store parameters: write the ASCII signature "save" '
            "(0x65766173) to sub 1 to persist to EEPROM.",
    0x1011: 'Restore parameters: write "load" (0x64616F6C) to sub 1.',
    0x1018: ("Identity: sub 1 vendor (Sevcon 0x1E), sub 2 product code "
             "(EDS default 0x0101503C; the file-level ProductNumber is "
             "0x07055037), sub 3 firmware revision, sub 4 serial."),
    0x1017: "Heartbeat producer period in ms (0 disables).",
    0x1016: "Heartbeat consumer: per-node timeout supervised by this node.",
    0x1014: "EMCY COB-ID, normally 0x80 + node ID.",
    0x1001: "Error register: bit flags mirrored in EMCY byte 2.",
    0x1200: "Default SDO server (COB-IDs 0x600/0x580 + node).",
    0x1201: "Extra SDO servers 1201h-1203h (disabled by default, COB 0).",
    0x1400: "RPDO communication record (COB-ID, transmission type, inhibit).",
    0x1600: "RPDO mapping record (up to 8 index:sub:length entries).",
    0x1800: "TPDO communication record (defaults disabled: 0x80000202...).",
    0x1A00: "TPDO mapping record.",
    0x2900: ("Traction control mode: 0 = torque/speed mode, 1 = closed-loop "
             "speed control. Must agree with 6060h across nodes."),
    0x291F: ("Creep torque applies torque when drive is selected and the throttle "
             "is closed. The Gen4 V3.4 manual documents this at 291Fh. "
             "Its wire type, scaling and limits come from the selected "
             "firmware dictionary; availability depends on the installed firmware."),
    0x2C01: "Battery over-voltage protection thresholds (x0.0625 V).",
    0x2721: "Vehicle speed (x0.0625 kph).",
    0x2790: "Battery status: sub 1 = remaining charge % (BDI).",
    0x6800: ("Digital input states. Second logical device: the Gen4 maps "
             "the CiA 401 profile at +0x800 (6800h here = 6000h in DS401)."),
    0x6C01: "Analog input values (DS401 +0x800).",
    0x6C11: "Analog (contactor drive) output values (DS401 +0x800).",
    0x4600: "Additional motor measurements: Id/Iq, Ud/Uq, AC current/voltage, temps.",
    0x4602: ("AC motor debug: IGBT junction temps, torque demand/actual/max "
             "(Nm), cutback factors, measured encoder offset (Sin/Cos setup)."),
    0x2020: "Motor VPDO image (left): controlword/statusword/velocity/torque images.",
    0x5900: "Physical layer settings (CAN bitrate; Gen4 default 1 Mbit/s).",
    0x2830: ("Assumed fault severity for EMCYs from non-Sevcon nodes "
             "(the Gen4 only reacts natively to Sevcon EMCYs)."),
}


def _profile_line(index: int) -> Optional[str]:
    if index in CIA402_OBJECTS:
        info = CIA402_OBJECTS[index]
        end = f" (range to 0x{info.index_end:04X})" if info.index_end else ""
        return (f"CiA DSP402 drive profile object{end}: {info.name} "
                f"[{info.object_code}, {info.data_type}, {info.category}].")
    if index in CIA301_OBJECTS:
        info = CIA301_OBJECTS[index]
        return (f"CiA 301 communication object: {info.name} "
                f"[{info.object_code}, {info.data_type}, {info.category}].")
    shifted = index - 0x800
    if shifted in CIA401_OBJECTS:
        info = CIA401_OBJECTS[shifted]
        return (f"CiA DS401 generic-I/O object at +0x800: {info.name} "
                f"[{info.object_code}, {info.data_type}, {info.category}].")
    return None


def _state_list() -> str:
    states = sorted(DS402_STATES, key=lambda s: s[1])
    return "States: " + "; ".join(f"0x{v:04X} {n}" for _, v, n in states) + "."


def _command_list() -> str:
    names: List[str] = []
    for cmd in DS402_COMMANDS:
        if cmd.name not in names:
            names.append(cmd.name)
    return ("Commands: " + ", ".join(names) + ".")


def accepts_text(var) -> str:
    """One-line summary of what values a variable accepts, from the EDS:
    enum options, numeric limits, or the data type."""
    if var.labels:
        options = []
        for raw, label in sorted(var.labels.items()):
            text = f"{label} ({raw})"
            try:
                var.check_range(raw)
            except ValueError:
                text += " [outside EDS limits]"
            options.append(text)
        return ", ".join(options)
    if var.bitfields:
        return "bit fields: " + ", ".join(bf.name for bf in var.bitfields)
    from . import datatypes as dt
    dtype = dt.NAMES.get(var.data_type, f"0x{var.data_type:04X}")
    if var.data_type in (dt.OCTET_STRING, dt.DOMAIN, dt.TIME_OF_DAY, dt.TIME_DIFFERENCE):
        return f"{dtype}: bytes or hex:01 02"
    if var.low is not None or var.high is not None:
        low, high = var.raw_limits()
        lo = low if low is not None else "…"
        hi = high if high is not None else "…"
        text = f"{dtype} {lo}…{hi}"
        if var.scaling is not None:
            phys = [float(x) * var.scaling for x in (lo, hi)
                    if isinstance(x, (int, float))]
            if phys:
                unit = f" {var.units}" if var.units else ""
                text += f" raw (= {('…'.join(f'{p:g}{unit}' for p in phys))})"
        return text
    return dtype


def source_text(var) -> str:
    """Identify supplemental parameters whose availability depends on firmware."""
    source = var.extra.get("SUPPLEMENTAL_SOURCE")
    if not source:
        return ""
    return (f"Firmware dependent. Source {source}; "
            f"product {var.extra.get('FIRMWARE_PRODUCT', 'unspecified')}, "
            f"revision {var.extra.get('FIRMWARE_REVISION', 'unspecified')}.")


def describe(index: int, subindex: Optional[int] = None) -> str:
    """Explain an object in plain language.

    Combines the curated paragraph, the CiA profile lineage, the
    DS402 state/command enumerations (for 6040h/6041h), the EDS access
    type and the Gen4 abort-reason table (for 5310h). Falls back to the
    profile line, or a generic pointer when nothing is known.
    """
    parts: List[str] = []
    if index in OBJECT_DESCRIPTIONS:
        parts.append(OBJECT_DESCRIPTIONS[index])
    profile = _profile_line(index)
    if profile:
        parts.append(profile)
    if index == 0x6041:
        parts.append(_state_list())
    if index == 0x6040:
        parts.append(_command_list())
    if index == 0x5310:
        parts.append("Reasons: " + "; ".join(
            f"{k}={v}" for k, v in sorted(GEN4_ABORT_REASONS.items())) + ".")
    if not parts:
        parts.append(
            f"0x{index:04X} is a Sevcon manufacturer object "
            f"(2000h-5FFFh). See its EDS ParameterName, scaling, units "
            f"and labels in the table, or docs/OBJECTS.md.")
    text = " ".join(parts)
    if subindex not in (None,):
        text += f" (sub-index {subindex})."
    return text
