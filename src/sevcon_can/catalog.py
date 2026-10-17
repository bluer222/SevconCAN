"""Generate standalone Python lookup dictionaries from an EDS and TPDO mappings."""
from __future__ import annotations

from pathlib import Path
from pprint import pformat
from typing import Mapping, Optional

from .od import ObjectDictionary
from .pdo import cob_id_valid, read_mapping


def build_catalog(od: ObjectDictionary, *,
                  tpdo_configurations: Optional[Mapping] = None) -> dict:
    """Build descriptions and access groups without requesting parameter values.

    Supply ``{pdo_number: await dev.read_pdo_configuration(True, pdo_number)}``
    for the live stream configuration. Otherwise use EDS/DCF parameter values,
    falling back to defaults. ACCESS_LEVEL specifies minimum write authorization;
    absent metadata is retained as None, not relabeled as level zero.
    """
    variables = sorted(od.variables(), key=lambda var: var.key)
    objects = {var.key: var.path + (f" [{var.units}]" if var.units else "")
               for var in variables}
    metadata = {var.key: {
        "access": var.access, "readable": var.readable, "writable": var.writable,
        "required_auth_level": var.access_level, "preop_only": var.preop_only,
        "data_type": var.data_type, "scaling": var.scaling, "units": var.units,
        "pdo_mappable": var.pdo_mappable,
        "low": var.low, "high": var.high, "labels": dict(var.labels),
        "bitfields": [{"name": field.name, "mask": field.mask, "shift": field.shift,
                       "low": field.low, "high": field.high, "labels": dict(field.labels)}
                      for field in var.bitfields],
        "category": var.category, "section": var.section, "tag": var.tag,
        "eeprom": var.eeprom, "extra": dict(var.extra),
    } for var in variables}
    levels = sorted(set(range(6)) | {
        var.access_level for var in variables if var.access_level is not None})
    required = {level: {var.key: objects[var.key] for var in variables
                        if var.access_level == level} for level in levels + [None]}
    writable = {level: {var.key: objects[var.key] for var in variables
                        if var.writable and (var.access_level is None or
                                             var.access_level <= level)}
                for level in levels}

    if tpdo_configurations is None:
        configs = {}
        for number in range(1, 6):
            comm = 0x1800 + number - 1
            if comm not in od.objects:
                continue
            cob = od.variable((comm, 1)).value()
            configs[number] = {
                "cob_id": cob, "enabled": cob_id_valid(cob),
                "transmission_type": od.variable((comm, 2)).value(),
                "mapping": read_mapping(od, True, number - 1),
            }
        stream_source = "EDS/DCF configuration; actual controller may differ"
    else:
        configs = {number: dict(config) for number, config in tpdo_configurations.items()}
        stream_source = "live TPDO configuration; transmission depends on NMT/SYNC/events"

    mapped = {}
    for number, config in sorted(configs.items()):
        # Resolve every mapping against the supplied EDS, including disabled PDOs.
        # A mismatched EDS must fail rather than silently omit unknown entries.
        mapped[number] = {}
        for index, subindex, _bits in config["mapping"]:
            var = od.variable((index, subindex))
            mapped[number][var.key] = objects[var.key]
    streamed = {key: description for number, entries in mapped.items()
                if configs[number]["enabled"] for key, description in entries.items()}
    try:
        level_names = dict(od.variable((0x5000, 1)).labels)
    except KeyError:
        level_names = {}
    return {
        "ALL_OBJECTS": objects, "OBJECT_METADATA": metadata,
        "AUTH_LEVEL_NAMES": level_names, "BY_REQUIRED_AUTH_LEVEL": required,
        "WRITABLE_BY_AUTH_LEVEL": writable,
        "TPDO_CONFIGURATION": configs, "TPDO_OBJECTS": mapped,
        "STREAM_OBJECTS": streamed, "STREAM_SOURCE": stream_source,
    }


def write_catalog(path, od: ObjectDictionary, *, tpdo_configurations=None) -> dict:
    """Write a dependency-free, importable .py module; return its catalog data."""
    catalog = build_catalog(od, tpdo_configurations=tpdo_configurations)
    lines = ['"""Generated Sevcon lookup dictionaries. Regenerate rather than edit.',
             '', 'Keys are (index, subindex); descriptions are EDS names and units.',
             'Authorization metadata preflights writes; controller read permissions may differ.',
             'Optional firmware definitions retain SUPPLEMENTAL_SOURCE and',
             'FIRMWARE_DEPENDENT in OBJECT_METADATA[key]["extra"]. Inclusion',
             'does not establish support on the connected controller.',
             'WRITABLE_BY_AUTH_LEVEL is cumulative and includes writable entries',
             'without an EDS level restriction. Pre-operational rules still apply.',
             'TPDO_OBJECTS includes disabled mappings; STREAM_OBJECTS includes only',
             'enabled mappings. Configuration alone does not guarantee traffic.',
             '"""', '', 'ALL_OBJECTS = {']
    lines.extend(f"    (0x{index:04X}, 0x{sub:02X}): {description!r},"
                 for (index, sub), description in catalog["ALL_OBJECTS"].items())
    lines.extend(['}', ''])
    for name in ("OBJECT_METADATA", "AUTH_LEVEL_NAMES", "TPDO_CONFIGURATION", "STREAM_SOURCE"):
        lines.extend([f"{name} = {pformat(catalog[name], sort_dicts=False, width=100)}", ''])
    lines.extend([
        'READABLE_OBJECTS = {key: desc for key, desc in ALL_OBJECTS.items()',
        '                    if OBJECT_METADATA[key]["readable"]}',
        'BY_REQUIRED_AUTH_LEVEL = {',
    ])
    for level in catalog["BY_REQUIRED_AUTH_LEVEL"]:
        lines.extend([
            f'    {level!r}: {{key: desc for key, desc in ALL_OBJECTS.items()',
            f'             if OBJECT_METADATA[key]["required_auth_level"] == {level!r}}},',
        ])
    lines.extend(['}', '', 'WRITABLE_BY_AUTH_LEVEL = {'])
    for level in catalog["WRITABLE_BY_AUTH_LEVEL"]:
        lines.extend([
            f'    {level}: {{key: desc for key, desc in ALL_OBJECTS.items()',
            '            if OBJECT_METADATA[key]["writable"] and',
            '            (OBJECT_METADATA[key]["required_auth_level"] is None or',
            f'             OBJECT_METADATA[key]["required_auth_level"] <= {level})}},',
        ])
    lines.extend(['}', '', 'TPDO_OBJECTS = {',
                  '    number: {(index, sub): ALL_OBJECTS[(index, sub)]',
                  '             for index, sub, bits in config["mapping"]}',
                  '    for number, config in TPDO_CONFIGURATION.items()',
                  '}', '', 'STREAM_OBJECTS = {',
                  '    key: desc for number, entries in TPDO_OBJECTS.items()',
                  '    if TPDO_CONFIGURATION[number]["enabled"]',
                  '    for key, desc in entries.items()', '}', ''])
    for level in catalog["WRITABLE_BY_AUTH_LEVEL"]:
        lines.extend([f'AUTH_LEVEL_{level} = BY_REQUIRED_AUTH_LEVEL[{level}]',
                      f'WRITABLE_AUTH_LEVEL_{level} = WRITABLE_BY_AUTH_LEVEL[{level}]'])
    lines.extend(['AUTH_LEVEL_UNSPECIFIED = BY_REQUIRED_AUTH_LEVEL[None]', ''])
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text('\n'.join(lines), encoding='utf-8')
    return catalog
