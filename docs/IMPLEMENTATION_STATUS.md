# Implementation status for 634A42203

The implementation targets documented Gen4 CANopen services and the supplied
object dictionary. The public manual and a part number cannot identify the
firmware, customer passwords or motor configuration installed in a particular
unit. No physical testing was performed.

## Coverage

| Function | Available implementation | Evidence and limits |
| --- | --- | --- |
| Classic CAN transport | Linux SocketCAN; optional python-can interfaces | Virtual backend, frame conversion and serial protocol tests; no USB hardware test |
| Jhoinrch connection | Candlelight through Linux SocketCAN or direct gs_usb; SLCAN through serial | Requires compatible observed firmware; model name alone is insufficient |
| SDO | Expedited, segmented, block upload/download, CRC, retransmission, aborts, timeouts and cancellation | Independent canopen client/server exchanges, including injected loss; configured standard/extended channels |
| Object dictionary | EDS/DCF parser, raw access, scaling, labels, limits, bitfields and login permissions | Bundled EDS has firmware-specific metadata; custom EDS supported |
| NMT | Start/stop/pre-operational/reset; heartbeat monitor; bounded state polling | Simulator and independent stack tests |
| EMCY | Error register and Sevcon fault decoding | Sevcon bytes cross-checked against OVMS/Victron; independent producer test |
| PDO | Five live communication/mapping records, TPDO subscriptions, RPDO sends and remapping sequence | Fifth PDO requires a configured identifier; extended support must be allowed by actual EDS/firmware |
| SYNC | One-shot using live 1005h; host periodic producer | Python scheduling is not a hard real-time guarantee |
| Host heartbeat | Producer with explicit host node id and state | Host identity must match the vehicle's heartbeat configuration |
| Drive control | State-based enable, mode selection, quick stop/disable commands and reset edges | Does not commission vehicle application, motor or contactors |
| Encoder | Reads EDS-defined 4630h/6090h values; typed writes available | No automatic Sin/Cos alignment or invented firmware subindices |
| Parameters | Save/default-restore signatures; JSON raw snapshots; selected restore with preflight and read-back | Selected restore is sequential, not atomic, and never saves implicitly |
| User interfaces | CLI, monitor, shell and local browser GUI with full value dump | Custom dictionary and adapter paths; JSON downloads include raw values, per-object errors and partial cancellation results |
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
not automatically model TPDO event/inhibit timers, motor physics, current
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

Recorded on 2026-10-02 with Python 3.14.7, python-can 4.6.1 and canopen 2.4.1:
**153 tests passed with zero skips; 17 independent interoperability checks
passed.** Pinned source hashes and generated tables passed. A wheel was built
and the simulator drive example completed successfully. GUI HTTP tests
verified full value dumps, download headers, individual read errors,
write-only skips, single active scan and cancellation with partial download.

Before a later hardware acceptance run, establish the exact Jhoinrch model,
firmware, host driver, node id, CAN bitrate, credentials and controller EDS.
The work completed here does not require that run, but physical compatibility
must remain an open claim until it is performed.
