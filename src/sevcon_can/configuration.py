"""Export EEPROM parameters and restore an explicitly selected subset.

Snapshots contain exact SDO bytes and live identity, rather than rounded
physical values. Communication records require their dedicated configuration
procedures and are excluded from generic restoration.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .exceptions import AccessError, ProtocolError, SevconError

SCHEMA = "sevcon-can.snapshot.v1"
IDENTITY_FIELDS = ("vendor", "product_code", "revision", "serial")


@dataclass
class ConfigurationSnapshot:
    identity: Dict[str, int]
    values: Dict[str, str] = field(default_factory=dict)
    errors: Dict[str, str] = field(default_factory=dict)
    dictionary: Dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> str:
        return json.dumps({"schema": SCHEMA, "identity": self.identity,
                           "dictionary": self.dictionary, "values": self.values,
                           "errors": self.errors}, indent=2, sort_keys=True) + "\n"

    @classmethod
    def from_json(cls, text: str) -> "ConfigurationSnapshot":
        data = json.loads(text)
        if not isinstance(data, dict) or data.get("schema") != SCHEMA:
            raise ValueError("unsupported configuration snapshot schema")
        identity = data.get("identity")
        values = data.get("values")
        errors = data.get("errors", {})
        dictionary = data.get("dictionary", {})
        if not isinstance(identity, dict) or any(
                name not in IDENTITY_FIELDS or type(value) is not int or
                not 0 <= value <= 0xFFFFFFFF for name, value in identity.items()):
            raise ValueError("invalid snapshot identity")
        if not isinstance(values, dict) or not isinstance(errors, dict) or not isinstance(dictionary, dict):
            raise ValueError("invalid snapshot records")
        for key, value in values.items():
            _parse_key(key)
            if not isinstance(value, str):
                raise ValueError("snapshot values must be hexadecimal strings")
            bytes.fromhex(value)
        if any(not isinstance(k, str) or not isinstance(v, str) for k, v in errors.items()):
            raise ValueError("invalid snapshot errors")
        return cls(identity, values, errors, dictionary)


@dataclass(frozen=True)
class ConfigurationChange:
    index: int
    subindex: int
    path: str
    before: bytes
    after: bytes


class ConfigurationRestoreError(SevconError):
    """Restore stopped; completed changes and the failed key are explicit."""

    def __init__(self, completed: Sequence[ConfigurationChange], failed: Tuple[int, int], cause: Exception):
        self.completed = list(completed)
        self.failed = failed
        self.cause = cause
        super().__init__(f"restore stopped at 0x{failed[0]:04X}:{failed[1]:02X} "
                         f"after {len(completed)} verified changes: {cause}")


def _key(index: int, subindex: int) -> str:
    return f"{index:04X}:{subindex:02X}"


def _parse_key(key: str) -> Tuple[int, int]:
    if not isinstance(key, str) or len(key) != 7 or key[4] != ":":
        raise ValueError("snapshot keys must have form XXXX:YY")
    index, subindex = int(key[:4], 16), int(key[5:], 16)
    return index, subindex


async def _identity(device) -> Dict[str, int]:
    # Required reads must propagate errors; read_identity() is best effort.
    result = {}
    for sub, name in enumerate(IDENTITY_FIELDS, 1):
        data = await device.read_raw(0x1018, sub)
        if len(data) != 4:
            raise ProtocolError(f"identity {name} must be four bytes")
        result[name] = int.from_bytes(data, "little")
    return result


async def export_configuration(device, keys: Optional[Sequence[Tuple[int, int]]] = None) -> ConfigurationSnapshot:
    """Read EEPROM-backed, readable parameters. Failed reads are recorded.

    No writes or login occur. Pass keys to capture a smaller subset; without
    keys every readable EEPROM parameter in the supplied EDS is attempted.
    """
    snapshot = ConfigurationSnapshot(await _identity(device), dictionary={
        "source": device.od.source, "vendor": device.od.vendor_id,
        "product_code": device.od.product_code, "revision": device.od.revision})
    variables = ([device.variable(key) for key in keys] if keys is not None
                 else [v for v in device.od.variables() if v.eeprom and v.readable])
    for var in variables:
        if not var.readable or not var.eeprom:
            raise AccessError(f"{var.path} is not a readable EEPROM parameter")
        key = _key(var.index, var.subindex)
        try:
            snapshot.values[key] = (await device.read_raw(var.index, var.subindex)).hex()
        except SevconError as error:
            snapshot.errors[key] = str(error)
    return snapshot


async def plan_configuration_restore(device, snapshot: ConfigurationSnapshot,
                                     keys: Sequence[Tuple[int, int]], *,
                                     allow_other_serial: bool = False) -> List[ConfigurationChange]:
    """Validate every selected parameter and read its current value, without writes.

    Vendor, live product and revision must match the snapshot. Serial must also
    match unless explicitly allowing another unit. Node/bitrate, CANopen
    communication and PDO records need their dedicated APIs and cannot be
    restored here. Caller must already be logged in and in pre-operational.
    """
    identity = await _identity(device)
    names = IDENTITY_FIELDS[:3] if allow_other_serial else IDENTITY_FIELDS
    if any(name not in snapshot.identity or snapshot.identity[name] != identity[name] for name in names):
        raise AccessError("snapshot live identity does not match the connected controller")
    # Match dictionary metadata as well as live identity: do not decode bytes
    # with a different firmware dictionary.
    expected = {"vendor": device.od.vendor_id, "product_code": device.od.product_code,
                "revision": device.od.revision}
    if any(snapshot.dictionary.get(name) != value for name, value in expected.items()):
        raise AccessError("snapshot dictionary does not match the selected EDS")
    if await device.nmt_state(cached=False) != 127:
        raise AccessError("configuration restore requires pre-operational state")
    plan = []
    seen = set()
    for index, subindex in keys:
        if (index, subindex) in seen:
            raise ValueError("duplicate restore parameter")
        seen.add((index, subindex))
        var = device.variable((index, subindex))
        if index < 0x2000 or index in (0x5000, 0x5900, 0x5901, 0x5902):
            raise AccessError(f"0x{index:04X} requires a dedicated communication configuration procedure")
        if not var.eeprom or not var.readable or not var.writable:
            raise AccessError(f"{var.path} is not a readable/writable EEPROM parameter")
        key = _key(index, subindex)
        if key not in snapshot.values or key in snapshot.errors:
            raise AccessError(f"snapshot has no successful read of {key}")
        after = bytes.fromhex(snapshot.values[key])
        if var.size is not None and len(after) != var.size:
            raise ValueError(f"snapshot {key} has invalid byte length")
        raw = var.decode_raw(after)
        var.check_range(raw)
        await device._check_writable(var, raw)
        before = await device.read_raw(index, subindex)
        if before != after:
            plan.append(ConfigurationChange(index, subindex, var.path, before, after))
    return plan


async def restore_configuration(device, snapshot: ConfigurationSnapshot,
                                keys: Sequence[Tuple[int, int]], *,
                                allow_other_serial: bool = False) -> List[ConfigurationChange]:
    """Preflight, write and read back selected parameters. Does not call store().

    Changes are sequential and not atomic. A failure reports all verified
    changes, plus the failed parameter which may already have been written.
    """
    plan = await plan_configuration_restore(device, snapshot, keys,
                                            allow_other_serial=allow_other_serial)
    completed = []
    for change in plan:
        try:
            var = device.variable((change.index, change.subindex))
            await device._check_writable(var, var.decode_raw(change.after))
            current = await device.read_raw(change.index, change.subindex)
            if current != change.before:
                raise AccessError("parameter changed since preflight")
            await device.write_raw(change.index, change.subindex, change.after)
            if await device.read_raw(change.index, change.subindex) != change.after:
                raise ProtocolError("parameter read-back differs from snapshot")
        except Exception as error:
            raise ConfigurationRestoreError(completed, (change.index, change.subindex), error) from error
        completed.append(change)
    return completed
