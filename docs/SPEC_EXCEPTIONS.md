# Specification exceptions

Defects, typos and ambiguities found in the source documents while
writing `tools/spec_parser.py`. Each entry names the workaround so a
reader can verify it against the original. Parser locations are given
as `spec_parser.py` symbols.

## CiA DSP402 V2.0

* **6050h "vl slow down time" has no Category line** (p. 166). The only
  object description in the three CiA documents missing a field.
  Workaround: `DOC_OMISSIONS = {0x6050: {"category"}}`, category stored
  as `""`.
* **Fault-reset bit 7 is a rising-edge figure, not a 0/1 cell.**
  `pdftotext` drops the graphic, leaving the row with four bit columns
  instead of five. The table caption and section 10.3.1 define fault
  reset as a 0→1 edge of bit 7, so the parser restores bit 7 as `"1"`
  (`_CMD_FAULT_RESET_RE` / `parse_ds402_commands`).

## CiA DS401 V2.1

* **Index typo "6006g"** (p. 33). Confirmed as 6006h by the object list
  table; fixed in `DOC_INDEX_TYPOS`.
* **Index "6250" without the `h` suffix** (p. 63). Same treatment.
* **"Categrory" (6260h, p. 64) and "Catagory" (642Bh, p. 104)** for
  "Category". Accepted as spellings of the Category field
  (`_CATEGORY_MISPRINTS`).

## Gen4 Size 2/4/6 Product Manual

* **Table 33 (5310h abort reasons) wraps descriptions** across lines
  within three newspaper-style columns, and once splits
  "pre-operational" across a line break. The parser learns column
  positions from the number tokens and joins continuations per column
  (`parse_gen4_abort_reasons`); "pre- operational" is re-joined.
* **Table 4 (ratings) puts the voltage label on the middle row** of
  each three-row size group (24 V has only size 2). Rows are grouped by
  ascending size with a new group on a second label
  (`parse_gen4_ratings`).
* **Table 2 (voltage ranges) wraps "a to b" upper bounds** onto the
  next line. Upper bounds are filled in column order
  (`parse_gen4_voltage_ranges`).
* **EDS vs manual object coverage.** The manuals reference objects absent
  from the bundled EDS, including `291Fh` creep torque. The optional
  supplement supplies verified definitions from another firmware without
  replacing baseline entries. Unknown typed keys raise
  `ObjectDictionaryError`; raw SDO access remains available. The generated
  [coverage report](COVERAGE.md) accounts for explicit indices and attached
  subindices, including unresolved schemas. It does not infer implicit
  prose back-references or prove installed firmware support.
* **EDS 1018h:2 default (`0x0101503C`) differs from the EDS
  DeviceInfo ProductNumber (`0x07055037`).** Both are real: the former
  is the live identity object (unit/family code), the latter the file
  product. `read_identity()` returns the live values; tests assert
  both.
* **Password semantics are one-directional.** The manual describes
  login as password + user-id → encryption → level, but publishes no
  algorithm or password values. The engineering password `0x4BDF` and
  the write-0-to-logout behavior come from the OVMS Twizy client,
  verified against real units by that project — not from Sevcon
  documentation. Other passwords are controller/customer specific.

## Fault-code spreadsheet (Sevcon, 2013)

* Several fault ids appear in OVMS/Victron but not in the spreadsheet
  (e.g. pure-warning ids) and Victron repeats some ids with different
  names (0x4542 "Device test waiting" / "Low Oil"). The merge in
  `generate_tables.merge_faults` keeps the spreadsheet authoritative
  for level/LED/display/description/action and records every other
  name as an alias with its source.
* Fault ids with no level/LED/UID (Victron-only entries) carry `None`
  in those fields — they are still decoded by name.

## EMCY manufacturer bytes

* CiA leaves bytes 3–7 open. The Sevcon layout (fault id in bytes 3–4,
  extra data in 5–7) is **not stated in any Sevcon manual**; it is
  established by two independent implementations agreeing byte for
  byte (OVMS `emcy.data[0..1]` after its 3-byte header; Victron
  `mdata[3..4]`). `parse_emcy_fault_offsets` re-derives the offsets
  from those sources on every tables build.

## Firmware and encoder metadata during implementation expansion

* **LSS is disabled in the bundled EDS.** `DeviceInfo.LSS_Supported=0`.
  Lack of an LSS implementation is not evidence of a missing service for
  that dictionary. A different firmware dictionary may declare otherwise.
* **Fifth PDO has no predefined CANopen identifier.** Default-ID helpers
  reject PDO 5 rather than inventing an identifier that collides with PDO 2.
  The live communication record or an explicit COB-ID must be used.
* **Sin/Cos latency numbering differs.** V3.4 refers to 4630h subindex 15
  decimal, while the bundled EDS has no subindex 0F and defines latency
  selection/adjustment under other subindices. `encoder_configuration()`
  reads only entries in the supplied dictionary.
* **Sin/Cos wiring figure versus prose.** V3.4 Figure 20 and its neighboring
  prose disagree on the sine connector pin. No automatic wiring conclusion
  or offset calibration is derived from this inconsistency.
* **Extended PDO flags exceed bundled EDS limits.** The bundled PDO COB-ID
  HighLimit excludes some extended/disabled flag combinations. Generic
  protocol code supports extended frames, while `configure_pdo()` obeys
  the selected EDS. Tests requiring extended configuration explicitly use
  a synthetic dictionary with the applicable limit widened.

## Findings from the repeated coverage audit

* **Lowercase references were omitted.** The old reference scanner missed
  `291fh` and application-note `0xXXXX` addresses. Both forms are now scanned.
* **Signed range boundary is one too high.** Motor temperature parameters
  `4621h:3/4/5` declare INTEGER16 limits `-32768..32768`. Treating `32768`
  as raw two's complement collapsed the allowed range to `-32768`.
  `Variable.raw_limits()` clamps this exact full-range boundary to `32767`.
  Genuine two's-complement negative limits retain their meaning. Successful
  target reads of 120, 100 and 0 exposed the defect.
* **Enum labels exceed limits.** `6060h` labels voltage mode 5 while declaring
  HighLimit 4; `5900h:2` labels values beyond its HighLimit. Labels and numeric
  writes now obey the same range checks. No capability is inferred from an
  inconsistent label alone.
* **Read permissions depend on firmware.** EDS readable access describes the
  operation, not a promise that a logged-out controller will permit it. The
  first target dump returned 586 access-level aborts; engineering login
  reduced that to three. Local ACCESS_LEVEL metadata preflights writes;
  reads still report controller permission errors.
