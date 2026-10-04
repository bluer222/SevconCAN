"""CiA DSP402 drive state machine (device control).

The Gen4 implements the 402 objects that exist in its EDS --

* ``6040h`` controlword (rww), ``6041h`` statusword (ro),
* ``6060h`` mode of operation selection
  (``3 = speed, 4 = torque, 5 = voltage`` -- mode 5 is Sevcon-specific),
* ``6061h`` mode display,

-- and follows the standard controlword/state table. The canonical
command bit patterns and state masks come from ``tables.DS402_COMMANDS``
/ ``tables.DS402_STATES`` (parsed from DSP402 V2.0 Tables 4 and 7 by
``tools/spec_parser.py``), so this module contains no hand-copied magic
numbers for those.

Statusword bit meanings used here (DSP402 9.2.2):

* bits 0-3, 5, 6: state code (decoded via ``DS402_STATES``),
* bit 4: voltage enabled, bit 5: quick stop, bit 7: warning,
* bit 8: manufacturer specific, bit 9: remote, bit 10: target reached,
  bit 11: internal limit active, bits 12-13: mode specific,
  bit 15: homing attained (mode specific use varies).

Typical enable sequence (DSP402 Figure 6)::

    Shutdown -> Switch on -> Enable operation

with ``Fault reset`` (bit 7 rising edge) to leave the Fault state.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional

from .tables import DS402_COMMANDS, DS402_STATES

# Controlword bits (DSP402 Table 3).
CW_SWITCH_ON = 0x0001
CW_ENABLE_VOLTAGE = 0x0002
CW_QUICK_STOP = 0x0004
CW_ENABLE_OPERATION = 0x0008
CW_FAULT_RESET = 0x0080
CW_HALT = 0x0100

# Statusword bits (DSP402 Table 6).
SW_READY_TO_SWITCH_ON = 0x0001
SW_SWITCHED_ON = 0x0002
SW_OPERATION_ENABLED = 0x0004
SW_FAULT = 0x0008
SW_VOLTAGE_ENABLED = 0x0010
SW_QUICK_STOP = 0x0020
SW_SWITCH_ON_DISABLED = 0x0040
SW_WARNING = 0x0080
SW_REMOTE = 0x0200
SW_TARGET_REACHED = 0x0400
SW_INTERNAL_LIMIT = 0x0800


def decode_state(statusword: int) -> str:
    """Decode the CiA 402 state name from a statusword.

    Longest (most specific) mask first so e.g. ``Operation enabled``
    wins over the less specific ``Switch on disabled`` pattern.
    """
    for mask, value, name in sorted(DS402_STATES, key=lambda s: bin(s[0]).count("1"), reverse=True):
        if statusword & mask == value:
            return name
    return "Unknown"


def command_bits(name: str) -> int:
    """Controlword bits for a DSP402 device-control command.

    ``"X"`` (don't care) bits are left clear; bit 7 (fault reset) is only
    set for the Fault reset command. Raises :class:`KeyError` for an
    unknown command name.
    """
    for cmd in DS402_COMMANDS:
        if cmd.name.lower() == name.lower():
            word = 0
            for bit, want in cmd.bits:
                if want == "1":
                    word |= 1 << bit
            return word
    raise KeyError(f"unknown DS402 command {name!r} (want one of {command_names()})")


def command_names() -> List[str]:
    seen: List[str] = []
    for cmd in DS402_COMMANDS:
        if cmd.name not in seen:
            seen.append(cmd.name)
    return seen


# Friendly aliases used by AsyncSevconGen4.drive_command().
COMMAND_ALIASES = {
    "shutdown": "Shutdown",
    "switch_on": "Switch on",
    "switch on": "Switch on",
    "disable_voltage": "Disable voltage",
    "disable voltage": "Disable voltage",
    "quick_stop": "Quick stop",
    "quick stop": "Quick stop",
    "disable_operation": "Disable operation",
    "disable operation": "Disable operation",
    "enable_operation": "Enable operation",
    "enable operation": "Enable operation",
    "fault_reset": "Fault reset",
    "fault reset": "Fault reset",
}

# Gen4 operation modes: EDS labels of 6060h plus the DSP402 name.
OPERATION_MODES: Dict[str, int] = {
    "speed": 3,
    "torque": 4,
    "voltage": 5,
}
OPERATION_MODE_NAMES: Dict[int, str] = {v: k for k, v in OPERATION_MODES.items()}


def operation_mode_to_raw(mode: str | int) -> int:
    if isinstance(mode, int):
        if mode not in OPERATION_MODE_NAMES:
            raise ValueError(f"Gen4 supports modes {sorted(OPERATION_MODE_NAMES)}; got {mode}")
        return mode
    try:
        return OPERATION_MODES[mode.strip().lower()]
    except KeyError:
        raise ValueError(f"unknown operation mode {mode!r} (want speed/torque/voltage)") from None


def operation_mode_from_raw(raw: int) -> str:
    return OPERATION_MODE_NAMES.get(raw, f"mode {raw}")


@dataclass
class StatusInfo:
    raw: int
    state: str
    operation_enabled: bool
    fault: bool
    warning: bool
    switch_on_disabled: bool
    target_reached: bool
    remote: bool


def describe_status(statusword: int) -> StatusInfo:
    return StatusInfo(
        raw=statusword & 0xFFFF,
        state=decode_state(statusword),
        operation_enabled=decode_state(statusword) == "Operation enabled",
        fault=bool(statusword & SW_FAULT),
        warning=bool(statusword & SW_WARNING),
        switch_on_disabled=bool(statusword & SW_SWITCH_ON_DISABLED),
        target_reached=bool(statusword & SW_TARGET_REACHED),
        remote=bool(statusword & SW_REMOTE),
    )


#: Poll this sequence to bring a drive from Switch on disabled to
#: Operation enabled (DSP402 Figure 6, transitions 2, 3, 4).
ENABLE_SEQUENCE = ("Shutdown", "Switch on", "Enable operation")
