# Implementation status for 634A42203

The implementation targets documented Gen4 CANopen services and the supplied
object dictionary. The public manual and a part number cannot identify the
firmware, customer passwords or motor configuration installed in a particular
unit. Two user-provided physical value dumps and a full installed-dictionary
export were reviewed offline. This review issued no controller commands.

## Current completeness result

Repeated independent audits found and fixed omissions in parameter coverage,
EMCY identifiers, PDO remote requests and padding, binary values, per-object
SDO timeouts, SYNC counters, heartbeat cleanup and state-cache validation.
Fault list/text selectors are serialized across callers sharing one device,
and discovery limit comparisons reject noncanonical high bits rather than
silently masking metadata disagreements.
The default dictionary remains the original 1,340 entries. The explicit
`--supplemental` selection provides 89 additional source-backed definitions,
including `291Fh` creep torque. It never replaces a baseline definition.

The global [coverage audit](COVERAGE.md) accounts for 358 explicit index/subindex
references: 300 baseline schemas, 10 optional schemas, 14 address boundaries
and 34 unresolved references. Zero references are unreviewed. All 34 unresolved
references are absent from the actual target's table. They remain unknown
schemas for other firmware. Lexical and object coverage do not prove that all
prose or proprietary services have been implemented.

`python conformance/run.py --interop` checks software correctness, source
pinning and coverage freshness. Add `--require-complete` to make unresolved
schemas fail the gate. That strict gate currently fails as intended.

The actual target reports product `0x0705301B`, revision `0x00010026`,
software `0705.0013`, and PMAC firmware. Its Engineering value dump contains
1,328 successful reads, three access-level aborts and nine write-only skips.
A `5630h` enumeration of the target found the same 1,340 addresses as the
bundled EDS, with matching data type, access, PDO mapping and object version
on every row. The 169 limits it reports also match; 1,171 zero pairs leave
limits unverified. None of the supplement entries, including creep torque,
are installed. See
[target evidence](TARGET_EVIDENCE.md).

`sevcon-can --channel can0 --node <id> discover --json` repeats this check
for another controller. It writes only the metadata cursor and cannot
supply unknown names or scaling.

`python conformance/run.py --interop --installed installed-dictionary.json`
also verifies the existing export without CAN I/O. It recomputes comparisons
from raw rows, rejects incomplete or malformed scans, and reports zero target
object/schema gaps. The export itself contains no identity, so association
with this controller relies on the supplied capture context. The EDS product
header mismatch remains visible.

Structured diagnostic readers now cover both FIFO logs, all ten event
counters and all twelve values in each customer/Sevcon min/max monitor.
Counters separate the saved flag from the event ID and preserve its raw word.
Both 24-byte min/max domains match their scalar records in both physical
dumps. Their parser retains the DVT/EDS voltage-scaling conflict explicitly.
The reset-all workflow separately clears diagnostics and operating hours,
enforces the EDS's level-5 rule and waits for the completion flag to clear.
Selectors and explicit resets are serialized per device. No read clears a
log. Logout accepts the observed login-failed abort only after a successful
fresh read of access level zero; transport failures cannot report success.

Recovery of the unavailable isPointless repository from Software Heritage
provided DVT's actual Tcl source. The Gen4 `D0701` bootloader workflow now
supports DLD/DLDx text parsing, live capability preflight, page programming,
checksum acknowledgements, EEPROM reads and explicit entry/reset/exit.
Programming tracks progress and incomplete sections across loader instances
sharing a device, and never exits automatically. Four real archived packages
validate parsing; none establishes compatibility with this controller.
See [bootloader usage and limits](BOOTLOADER.md).

Full function completeness remains open. The recovered package does not
contain `obd_cli.tcl`, so installed `5640h` command numbers and semantics
remain unknown. Other `5600h` binary FIFO/error-record layouts also remain
undecoded. The target's bootloader revision and physical programming behavior
have not been verified. Raw access to these objects does not establish their
remaining workflows, and matching raw monitor words does not resolve their
voltage conversion. Further work needs the missing command script, matching
binary schemas or independent captures and measurements.

## Coverage

| Function | Available implementation | Evidence and limits |
| --- | --- | --- |
| Classic CAN transport | Linux SocketCAN; optional python-can interfaces | Virtual backend, frame conversion and serial protocol tests; no USB hardware test |
| Jhoinrch connection | Candlelight through Linux SocketCAN or direct gs_usb; SLCAN through serial | Requires compatible observed firmware; model name alone is insufficient |
| SDO | Expedited, segmented, block upload/download, CRC, retransmission, aborts, per-transfer timeouts and cancellation | EDS COMMS_TIMEOUT honored; independent exchanges including loss; target dumps include a 9,488-byte upload |
| Object dictionary | EDS/DCF parser, raw access, scaling, labels, limits, bitfields, optional supplement and login permissions | Exact custom EDS supported; supplements remain firmware dependent; names, labels and bitfields searchable |
| Installed dictionary discovery | Bounded `5630h` enumeration, resume, missing/absent entries, schema differences and offline export gate | Full target scan matches baseline addresses and reported metadata; `0..0` limits remain unverified; duplicate/zero addresses rejected; no invented names or scaling |
| NMT | Start/stop/pre-operational/reset; heartbeat monitor; bounded state polling | Simulator and independent stack tests |
| EMCY | Error register and Sevcon fault decoding; explicit/live `1014h` identifiers | Standard/extended identifiers; CLI follows live configuration; independent producer test |
| PDO | Five live records, TPDO subscriptions/RTR requests, types 252/253, RPDO sends, dummy mappings and remapping | Pre-operational and EDS restrictions enforced; fifth PDO needs configured identifier; extended support depends on firmware |
| SYNC | One-shot using live 1005h; host periodic producer | Python scheduling is not a hard real-time guarantee |
| Host heartbeat | Producer with explicit host node id and state | Host identity must match the vehicle's heartbeat configuration |
| Drive control | State-based enable, mode selection, quick stop/disable commands and reset edges | Does not commission vehicle application, motor or contactors |
| Encoder | Reads EDS-defined 4630h/6090h values; typed writes available | No automatic Sin/Cos alignment or invented firmware subindices |
| Parameters | Save/default-restore signatures; JSON raw snapshots; selected restore with preflight and read-back | Selected restore is sequential, not atomic, and never saves implicitly |
| Diagnostic logs | Both FIFO logs, saved event flags/counters, scalar and `0705` monitor domains, selective resets and bounded reset-all | DVT/OVMS procedures, installed EDS and both physical dumps; explicit voltage interpretation conflict; other binary record domains remain raw |
| User interfaces | CLI metadata search/describe, installed dictionary scan, diagnostic logs, monitor, shell and searchable browser GUI | Exact/optional dictionary selection, visible provenance, binary hex access; dumps retain errors and cancellation |
| Firmware update | Gen4 `D0701` DLD/DLDx parser, all-section preflight, checksummed programming, EEPROM reads, explicit entry/reset/exit | Recovered DVT source, independent peer tests, four real parser samples; no physical flash test or verified target image/bootloader revision |
| Proprietary DVT command channel | Raw `5640h` access only | Command vocabulary and response semantics unverified; no claim of full DVT compatibility |
| LSS | Not implemented | Bundled EDS explicitly declares LSS_Supported=0 |

## Dictionary and credentials

The bundled EDS DeviceInfo declares product `0x07055037`, revision
`0x00010026`, while its default live identity product at `1018h:2` is
`0x0101503C`. `validate_identity()` reports header mismatches and object
defaults separately. It never silently treats that discrepancy as proof of
634A42203 compatibility. Matching identity still does not prove that every
optional object exists. Raw SDO remains available for correctly documented
objects that are absent from a chosen EDS.

The engineering password `0x4BDF` is documented by the OVMS Twizy code and
accepted by the simulator. It is not a universal credential. Passwords and
user ids can be customer-specific.

## Sin/Cos ambiguities

Sevcon's V3.4 manual refers to Sin/Cos latency at `4630h:15` in decimal. The
bundled EDS lacks subindex `0x0F` and defines latency selection and adjustment
under other subindices. The inspection API follows the supplied dictionary;
it does not fabricate an entry from the manual's numbering.

The same manual's Sin/Cos wiring figure and adjacent prose disagree about
the sine connector pin. Use the build-specific harness drawing for wiring.
This software does not resolve that electrical ambiguity or calibrate encoder
offset without the installed motor and sensor.
[Sevcon V3.4 manual, motor commutation sensor section](https://www.e-kart.fr/images/stories/technique/SEVCON/GEN4-Product-Manual-V3-4.pdf).

## Simulator limits

The simulator exchanges actual CANopen frames and models live RPDO mapping,
asynchronous reception, deferred synchronous reception, heartbeat disabling,
fault-reset edges and PDO validity. Leaving operational discards pending
RPDOs. TPDO frames can be injected using the live mapping; the simulator does
not automatically model TPDO event/inhibit/RTR timing, motor physics, current
regulation, contactor timing, persistent EEPROM or every application interlock.
Simulator credentials and defaults are test inputs.

Independent software-stack checks reduce the chance that matching mistakes
in our client and simulator pass unnoticed. They do not establish the
controller's actual implementation of an optional service or a USB adapter's
firmware behavior.

## Reproduction

Install the project with its `dev` extra and run:

```sh
python conformance/run.py --interop --installed installed-dictionary.json
```

This checks pinned specifications, generated tables, unit/integration tests,
and independent CANopen exchanges using python-can's virtual bus. The
independent gate fails explicitly if its dependencies are missing. Core-only
installations can run `python conformance/run.py`; optional backend tests may
be skipped when their dependencies are absent, and the output reports that.

Recorded on 2026-10-17 with Python 3.14.7, python-can 4.6.1 and canopen 2.4.1:
**288 tests passed with zero skips; 17 independent interoperability checks
passed.** Pinned source hashes, generated tables, optional dictionary
freshness, coverage-accounting and installed-export checks passed. The strict
coverage check returned failure for its 34 unresolved references, as intended. GUI HTTP
tests passed in this run. An earlier headless-browser check verified optional
parameter search, source labels, watch/write flows and binary values without
JavaScript errors.

The supplied captures establish node 1 at 500 kbit/s and report the installed
dictionary above. A later hardware acceptance run still needs the adapter's
firmware/driver details, suitable credentials and a controlled vehicle setup.
Motor-control writes and physical timing remain unverified.
