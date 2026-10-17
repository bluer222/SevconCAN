# Implementation status for 634A42203

The implementation targets documented Gen4 CANopen services and the supplied
object dictionary. The public manual and a part number cannot identify the
firmware, customer passwords or motor configuration installed in a particular
unit. Two existing user-provided physical value dumps were reviewed offline;
no new controller commands were issued during this work.

## Current completeness result

Repeated independent audits found and fixed omissions in parameter coverage,
EMCY identifiers, PDO remote requests and padding, binary values, per-object
SDO timeouts, SYNC counters, heartbeat cleanup and state-cache validation.
The default dictionary remains the original 1,340 entries. The explicit
`--supplemental` selection provides 89 additional source-backed definitions,
including `291Fh` creep torque. It never replaces a baseline definition.

The [coverage audit](COVERAGE.md) accounts for 358 explicit index/subindex
references: 300 baseline schemas, 10 optional schemas, 14 address boundaries
and 34 unresolved references. Zero references are unreviewed. This is not
full controller completeness. Unknown parameter formats are still open,
and lexical coverage does not prove that all prose or proprietary services
have been implemented.

`python conformance/run.py --interop` checks software correctness, source
pinning and coverage freshness. Add `--require-complete` to make unresolved
schemas fail the gate. That strict gate currently fails as intended.

The actual target reports product `0x0705301B`, revision `0x00010026`,
software `0705.0013`, and PMAC firmware. Its Engineering value dump contains
1,328 successful reads, three access-level aborts and nine write-only skips.
The dumps did not enumerate the table or request creep torque, so equal
1,340-entry counts do not establish identical address sets. See
[target evidence](TARGET_EVIDENCE.md).

The next controller-specific check is `sevcon-can --channel can0 --node <id>
discover --json`, with the actual adapter and node selected. It enumerates
`5630h`, reports missing and absent entries and metadata disagreements, and
writes only the metadata cursor. It cannot supply unknown names or scaling.
No CAN interface was available in this execution environment.

## Coverage

| Function | Available implementation | Evidence and limits |
| --- | --- | --- |
| Classic CAN transport | Linux SocketCAN; optional python-can interfaces | Virtual backend, frame conversion and serial protocol tests; no USB hardware test |
| Jhoinrch connection | Candlelight through Linux SocketCAN or direct gs_usb; SLCAN through serial | Requires compatible observed firmware; model name alone is insufficient |
| SDO | Expedited, segmented, block upload/download, CRC, retransmission, aborts, per-transfer timeouts and cancellation | EDS COMMS_TIMEOUT honored; independent exchanges including loss; target dumps include a 9,488-byte upload |
| Object dictionary | EDS/DCF parser, raw access, scaling, labels, limits, bitfields, optional supplement and login permissions | Exact custom EDS supported; supplements remain firmware dependent; names, labels and bitfields searchable |
| Installed dictionary discovery | Bounded `5630h` enumeration, resume, missing/absent entries and schema differences | Internal datatype mapping verified against EDS/OVMS; duplicate/zero addresses rejected; no invented names or scaling |
| NMT | Start/stop/pre-operational/reset; heartbeat monitor; bounded state polling | Simulator and independent stack tests |
| EMCY | Error register and Sevcon fault decoding; explicit/live `1014h` identifiers | Standard/extended identifiers; CLI follows live configuration; independent producer test |
| PDO | Five live records, TPDO subscriptions/RTR requests, types 252/253, RPDO sends, dummy mappings and remapping | Pre-operational and EDS restrictions enforced; fifth PDO needs configured identifier; extended support depends on firmware |
| SYNC | One-shot using live 1005h; host periodic producer | Python scheduling is not a hard real-time guarantee |
| Host heartbeat | Producer with explicit host node id and state | Host identity must match the vehicle's heartbeat configuration |
| Drive control | State-based enable, mode selection, quick stop/disable commands and reset edges | Does not commission vehicle application, motor or contactors |
| Encoder | Reads EDS-defined 4630h/6090h values; typed writes available | No automatic Sin/Cos alignment or invented firmware subindices |
| Parameters | Save/default-restore signatures; JSON raw snapshots; selected restore with preflight and read-back | Selected restore is sequential, not atomic, and never saves implicitly |
| User interfaces | CLI metadata search/describe, installed dictionary scan, monitor, shell and searchable browser GUI | Exact/optional dictionary selection, visible provenance, binary hex access; dumps retain errors and cancellation |
| Firmware update | Not implemented | No verified Gen4 .dld/.dldx transport/file format available in the reviewed evidence |
| Proprietary DVT functions | Not implemented | No claim of full DVT compatibility |
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
python conformance/run.py --interop
```

This checks pinned specifications, generated tables, unit/integration tests,
and independent CANopen exchanges using python-can's virtual bus. The
independent gate fails explicitly if its dependencies are missing. Core-only
installations can run `python conformance/run.py`; optional backend tests may
be skipped when their dependencies are absent, and the output reports that.

Recorded on 2026-10-16 with Python 3.14.7, python-can 4.6.1 and canopen 2.4.1:
**237 tests passed with zero skips; 17 independent interoperability checks
passed.** Pinned source hashes, generated tables, optional dictionary
freshness and coverage-accounting checks passed. The strict coverage check
returned failure for its 34 unresolved references, as intended. GUI HTTP
tests and a headless-browser check verified optional parameter search,
source labels, watch/write flows and binary values without JavaScript errors.

Before a later hardware acceptance run, establish the exact Jhoinrch model,
firmware, host driver, node id, CAN bitrate, credentials and controller EDS.
The work completed here does not require that run, but physical compatibility
must remain an open claim until it is performed.
