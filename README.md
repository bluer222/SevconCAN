# sevcon-can

An async Python CANopen client for Sevcon Gen4 motor controllers. The target
is the **634A42203, 36/48 V, 275 A, Size 2, Sin/Cos** controller. The bundled
EDS describes a Gen4 firmware revision; it has not been established as the
exact dictionary installed on every 634A42203.

The library implements parameter access, drive commands, faults, live PDO
configuration, RPDO transmission, heartbeat monitoring and SYNC. Offline
validation includes fault injection and exchanges with an independent
CANopen stack. Two user-supplied physical value dumps support SDO read
compatibility with firmware `0705.0013`. A full installed-dictionary export
matches all 1,340 baseline addresses and their reported schema metadata;
see [target evidence](docs/TARGET_EVIDENCE.md).
Adapter behavior and motor-control writes remain unverified. This is not a
replacement for every proprietary DVT service.
See [implementation status](docs/IMPLEMENTATION_STATUS.md) for coverage and
remaining limits.

Requires Python 3.9 or newer. The core has no runtime dependencies.

## Install and choose an adapter

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install .                    # Linux SocketCAN and simulator
python -m pip install '.[slcan]'           # SLCAN serial firmware
python -m pip install '.[gs-usb]'          # direct Candlelight USB access
```

Choose by the adapter's observed interface. The Jhoinrch brand does not
identify its firmware. On Linux, Candlelight can appear as a SocketCAN
interface such as `can0`. Configure it to match the controller's CAN bitrate;
the documented Gen4 default is 1 Mbit/s.

```sh
sudo ip link set can0 type can bitrate 1000000 restart-ms 100
sudo ip link set can0 up
sevcon-can adapters
sevcon-can --channel can0 --node 0x20 info
```

For direct Candlelight USB or SLCAN:

```sh
sevcon-can --backend gs_usb --usb-index 0 --bitrate 1000000 info
sevcon-can --backend slcan --port /dev/ttyACM0 --bitrate 1000000 info
```

`--node 0x20` is an example and the CLI default. Use the configured controller
node id. [Adapter instructions](docs/ADAPTERS.md) cover USB dependencies,
Windows COM ports, serial baudrate, wiring and diagnostics.

## Read and configure a controller

```python
import asyncio
from sevcon_can import AsyncSevconGen4, load_eds

async def main():
    # Prefer the EDS/DCF supplied for your controller firmware.
    od = load_eds("controller.eds")
    async with AsyncSevconGen4.from_socketcan("can0", node_id=0x20, od=od) as dev:
        print(await dev.validate_identity())
        print(await dev.read("Device Measurements.Battery Voltage"))
        print(await dev.active_faults())
        print(await dev.status())

asyncio.run(main())
```

`read()` returns `Reading.raw`, `physical`, `label` and `unit`. Numeric writes
use physical units where the dictionary specifies a scale. Enum labels also
work, such as `await dev.write(0x6060, 0, "torque")`. Login permissions, ranges,
read/write access and pre-operational restrictions are checked before a
parameter write. Range/access checks can involve read-only SDO queries.
Use a hexadecimal string such as `"0x10"` to write a raw integer without
scaling. Binary parameters accept bytes in Python or a string such as
`"hex:00 ff 10"` in Python, the CLI and the GUI.

Supply the credentials assigned to your controller with `login(password,
user_id=0)`. The frequently published `0x4BDF` password comes from the OVMS
Twizy implementation and is the simulator password. It is not established as
a universal Sevcon credential.

The CLI supports `--eds controller.eds`, `validate-identity`, `encoder`, typed
`read`/`write`, `read-raw`/`write-raw`, login/logout, NMT, faults, drive commands,
and live monitoring. Raw SDO access can address objects absent from the EDS;
it bypasses local metadata checks and requires the correct wire format.

```sh
sevcon-can --channel can0 --eds controller.eds validate-identity
sevcon-can --channel can0 --eds controller.eds encoder
sevcon-can --channel can0 monitor --interval 1
sevcon-can --sim shell
sevcon-can search torque
sevcon-can objects --json > parameter-metadata.json
sevcon-can describe STATUSWORD
sevcon-can --supplemental search creep
sevcon-can --channel can0 discover --json > installed-dictionary.json
```

`objects`, `search` and `describe` inspect dictionary metadata without opening
the CAN adapter. Searches include bit fields and their labels. `discover`
enumerates the connected controller's metadata table at `5630h`, reports
entries absent from the selected dictionary, and supports `--start` and
`--limit` for partial scans. Its JSON also lists mismatched types, versions,
access, PDO flags and limits, and entries absent from a complete controller
scan. It writes only the metadata cursor. It does not
change parameter values, log in, or change NMT state. Discovered raw limits
and internal type codes do not establish physical scaling, units, names or
required login levels. Names in the report come from the selected dictionary.
Keep other tools that use the same metadata cursor idle during enumeration.

The default dictionary contains the original 1,340 entries. Select
`--supplemental` to include 89 additional entries documented by other
firmware dictionaries, including creep torque at `291Fh`. Their source,
product and firmware revision remain visible. These optional definitions
do not establish support on your controller. Prefer its exact `--eds` file;
`--eds` and `--supplemental` are mutually exclusive. Python callers can use
`gen4_od(include_supplemental=True)` for the same optional definitions.

Creep torque uses the source DCF's ratio scale: `0.05` means 5%, encoded as
raw `50`; its documented range is `0.0` to `0.5`. For an offline round trip:

```sh
sevcon-can --sim --supplemental --password 0x4BDF write CREEP_TORQUE 0 0.05
```

This validates the client codec for firmware that provides this parameter.
The target's installed-dictionary scan excludes `291Fh`, and a direct read
returned object-not-found. Native creep torque through this object is absent
on firmware `0705.0013` in this unit. None of the 89 supplemental entries is
installed, so use the baseline dictionary for this controller.

## Diagnostic logs

System and fault FIFO readers preserve event IDs, all three event-data bytes
and controller operating timestamps. Event counters include all ten slots,
their counts and first/last timestamps. Customer and Sevcon min/max monitors
include both motor-current channels and retain the dictionary's units.

```python
rows = await dev.fifo_log("faults")
counters = await dev.event_counters()
monitor = await dev.operational_monitor()
```

```sh
sevcon-can --channel can0 --node 1 logs faults --start 0 --limit 20 --json
sevcon-can --channel can0 --node 1 logs events --json
sevcon-can --channel can0 --node 1 logs customer --json
```

FIFO reads select positions but do not clear logs. Without `--limit`, FIFO
and counter commands read all rows. Timestamps count operating hours plus
quarter minutes, not UTC. JSON also includes elapsed seconds. Calls sharing
a device serialize selectors and resets; new controller events can still
change a FIFO during a scan. Login restrictions apply to selector writes.

`await dev.reset_diagnostic_log(kind)` or `reset-log KIND` explicitly clears
one of `system`, `faults`, `events`, `customer` or `sevcon`. It enforces the
selected dictionary's access rules without an implicit login, state change
or store. Clearing the Sevcon lifetime monitor requires level 5 in the
baseline EDS. Clearing a log does not clear active faults. These helpers use
the scalar `4100h` through `4301h` records; `5600h` binary domain-log layouts
remain undecoded.

A successful FIFO reset can retain entries whose log level exceeds the
current access level. Read the FIFO again to inspect what remains.

## Importable parameter dictionaries

The generated `sevcon_can.object_catalog` module contains 1,429 descriptions
from the bundled EDS and documented supplemental definitions. Each description
dictionary uses `(index, subindex)` keys and human-readable parameter paths
with units as string values.

```python
from sevcon_can.object_catalog import (
    ALL_OBJECTS, STREAM_OBJECTS, AUTH_LEVEL_4, WRITABLE_AUTH_LEVEL_4,
)

print(ALL_OBJECTS[(0x5100, 1)])  # Device Measurements.Battery Voltage [V]
for key, description in STREAM_OBJECTS.items():
    print(key, description)
```

`AUTH_LEVEL_0` through `AUTH_LEVEL_5` group entries by their exact EDS
`ACCESS_LEVEL`. `AUTH_LEVEL_UNSPECIFIED` retains entries without that metadata.
`BY_REQUIRED_AUTH_LEVEL` collects those groups by numeric level, with `None`
for unspecified entries. The client uses these levels to check writes.
A controller can also restrict reads; a readable entry's presence in a
catalog does not grant read access.
`WRITABLE_AUTH_LEVEL_0` through `WRITABLE_AUTH_LEVEL_5` contain writable entries
available at each level, including lower-level entries and entries with no EDS
level restriction. Other rules, including pre-operational-only writes, still
apply. `READABLE_OBJECTS` and `OBJECT_METADATA` provide read access and metadata.
`OBJECT_METADATA[key]["extra"]` identifies supplemental sources and their
firmware product/revision. Inclusion in a generated catalog is documentation,
not a successful read from the connected controller.

`TPDO_OBJECTS` groups all mapped entries by PDO number, including disabled PDOs.
`STREAM_OBJECTS` combines enabled TPDO mappings. The bundled defaults disable
all TPDOs, so its stream dictionary is empty. Generate a module from your live
controller to capture its actual configuration:

```sh
python3 tools/generate_dictionaries.py --channel can0 --node 0x20 --out controller_objects.py
```

This only reads communication and mapping records. It does not enable PDOs or
request every parameter value. Enabled mappings still depend on the controller's
NMT state, transmission type, SYNC and events to produce traffic. `STREAM_SOURCE`
identifies whether the output used live records or EDS/DCF settings.

To regenerate offline from the bundled EDS or another firmware dictionary:

```sh
python3 tools/generate_dictionaries.py
python3 tools/generate_dictionaries.py --eds controller.eds --out controller_objects.py
```

The output has no imports or runtime dependencies and can be copied into a
larger project and used as `from controller_objects import ALL_OBJECTS`.
The generator is also available as `sevcon_can.catalog.build_catalog()` and
`write_catalog()` for projects that already have an object dictionary or live
PDO configuration records.

## Live PDOs and drive control

Use the controller's live PDO configuration, including its identifier and
mapping. PDO 5 has no predefined CANopen identifier.

```python
config = await dev.read_pdo_configuration(True, 1)
consumer = await dev.subscribe_tpdo_live(1)
async for values in consumer.updates():
    print(values)
```

For streamed TPDO values alongside explicitly requested SDO values:

```sh
python3 examples/async_stream_and_request.py can0 0x20
```

The example requests battery voltage, battery current and heatsink temperature
through SDO, then listens to TPDO1 using its live identifier and mapping.
Readable objects can be accessed through SDO even when they also appear in a
TPDO. The example only reads and listens; TPDO1 must already be enabled.
The bundled EDS defaults disable all five TPDOs; the live device configuration
determines which values stream and when. Synchronous TPDOs need an existing SYNC
source, and normal TPDO transmission requires the node to be operational.

`configure_pdo()` checks all proposed values before applying the CANopen
sequence to disable the PDO, clear the mapping, write the entries, restore
the mapping count and enable it. TPDO configuration is permitted outside
pre-operational unless the loaded EDS marks a written entry as requiring that
state. Login requirements still apply, and firmware may reject operational
remapping. CANopen calls support for operational remapping
[dynamic PDO mapping](https://www.can-cia.org/can-knowledge/pdo-protocol).
RPDO configuration still requires pre-operational. A failed remapping write
after the disable step leaves the PDO disabled.
`send_rpdo()` sends raw mapped values using the live configuration;
synchronous RPDOs apply on a later SYNC.
`request_tpdo()` sends an RTR request with the live identifier and mapped DLC
when the controller permits remote requests. TPDO transmission types 252 and
253 are supported. RPDO dummy mappings use the selected EDS's `DummyUsage`
declarations and preserve padding positions.

`await dev.subscribe_emcy_live()` follows the controller's configured `1014h`
identifier, including extended identifiers. Its consumer has a separate queue
from the default predefined-identifier consumer.

```python
await dev.login(your_password)
await dev.goto_preop()
await dev.configure_pdo(False, 1, [(0x6071, 0, 16)],
                        cob_id=0x220, transmission_type=255)
```

Mapping and motor commands depend on the vehicle's master/slave and VPDO
configuration. The example above does not commission a motor or establish
that an installed application will accept external torque demands. Sin/Cos
alignment, motor maps, contactors and application interlocks require the
correct unit configuration.

`examples/async_control.py` demonstrates drive enabling, fault handling and
cleanup that attempts zero torque and shutdown on interruption. On real
hardware it can energize a motor. Its `--sim` mode needs no hardware.

`examples/async_cruise.py` is a cruise-control master: the pedal stays
analog into the Gen4 while cruise demand goes over CAN (RPDO1 =
controlword + target torque), with engage/adjust from the keyboard,
pedal-override and brake cancel, no-regen coast-down, and the RPDO
timeout armed to Inhibit Drive. The brake input is a Pi GPIO pin
(`--brake gpio --brake-pin 17`, via gpiozero), a keyboard key, or off:

For a host participating in heartbeat supervision and synchronous PDOs,
`HeartbeatProducer` and `SyncProducer` run on an existing network:

```python
from sevcon_can import HeartbeatProducer, SyncProducer

async with HeartbeatProducer(dev.network, node_id=1, period_ms=100), \
           SyncProducer(dev.network, period_ms=20, cob_id=0x80):
    await run_your_control_loop()
```

Use a host node id distinct from every controller. Configure the controller's
heartbeat consumer and SYNC identifier accordingly. These producers skip
missed intervals, report send failures on close, and stop on context exit.
Python scheduling does not guarantee hard real-time delivery.

## Parameter snapshots

Backups contain live identity, dictionary metadata, exact raw EEPROM parameter
bytes and any failed reads. Restore requires explicitly selected parameters,
matching identity, login and pre-operational state. It checks the entire
selection first, writes sequentially and verifies every write by reading it
back. Failures report completed changes and the failed parameter. It does
not call `store()` automatically.

```sh
sevcon-can --sim backup snapshot.json
sevcon-can --sim --password 0x4BDF config-plan snapshot.json --key 2C01:02
```

Use `config-restore` to apply the selected changes. The Python equivalents
are `export_configuration`, `plan_configuration_restore` and
`restore_configuration`. Communication records and node/bitrate settings are
excluded from generic restore because they need their own configuration
sequence. A snapshot is not a DVT DCF export or a firmware image.

## Browser GUI

The localhost GUI shows live readings, faults and per-object Get/Set controls.
It accepts the same three adapter paths and a controller-specific EDS.
Search for a parameter by name, address, tag or bit field, then click
**Watch** to add it to the table. **List all parameters** shows every entry
in the selected dictionary. Add `--supplemental` to browse optional
definitions such as creep torque; their firmware source is shown in the table.
Each table row shows an **Accepts** column (enum options such as
`speed (3), torque (4)`, or the numeric range with physical
units) and a **?** button that explains the object in plain language.
Labels outside the dictionary's numeric limits are marked and rejected.
Binary readings display as `hex:...`, which can also be entered for a binary write.

```sh
python3 tools/sevcon_gui.py --sim
python3 tools/sevcon_gui.py --sim --supplemental
python3 tools/sevcon_gui.py --channel can0 --node 0x20 --eds controller.eds
python3 tools/sevcon_gui.py --backend gs_usb --usb-index 0
python3 tools/sevcon_gui.py --backend slcan --can-port /dev/ttyACM0
```

`--port` selects the HTTP server port. `--can-port` selects the CAN adapter's
serial port. No browser libraries or external web services are required.

Click **Dump all values** to read every readable entry in the loaded object
dictionary, then **Download JSON** to save it for review. The file includes
controller identity, dictionary metadata, collection timestamps, raw wire
bytes, interpreted values, units and per-object errors. Write-only entries
are listed without attempting to read them. Progress and cancellation are
available; cancelled scans can be downloaded as partial dumps. Values are
read sequentially, so their timestamps can differ. The dump sends no
parameter writes and includes monitoring values as well as configuration.

## Validation

```sh
python -m pip install '.[dev]'
python conformance/run.py --interop
python conformance/run.py --interop --installed installed-dictionary.json
python examples/async_log.py --sim
python examples/async_control.py --sim
```

The gate checks pinned source hashes, generated table freshness, the test
suite and independent `canopen`/`python-can` interoperability over a virtual
bus. Tests cover protocol loss and malformed frames, block transfer CRC and
retransmission, live PDO changes, drive-state transitions, adapter conversions,
serial protocol parsing, configuration restore failures and GUI shutdown.
The optional `--installed` stage reads an existing discovery export without
CAN I/O. It validates row integrity and recomputes address/schema comparisons
from raw fields, ignoring cached mismatch lists. Run
`python tools/audit_installed.py installed-dictionary.json --json` for its
standalone report. The target export passes with zero missing definitions
or metadata disagreements. Its 169 reported limit pairs agree; 1,171 `0..0`
pairs leave limits unverified. The export does not establish identity,
scaling or behavior. Software checks cannot verify electrical signaling,
USB firmware or real bus load.

The coverage audit accounts for every explicit address found in the pinned
manufacturer texts. Run `python conformance/run.py --interop --require-complete`
to additionally fail while a reviewed reference lacks a verified schema.
It currently reports 34 unresolved references, listed with evidence in
[the coverage report](docs/COVERAGE.md). None occurs in the target's installed
table. They remain unresolved for other firmware, and the global strict
gate still fails. Passing the target dictionary check establishes object
coverage for the export; firmware flashing, proprietary command semantics
and hardware behavior remain outside that result.

Sources and firmware ambiguities are documented in [sources](docs/SOURCES.md)
and [specification exceptions](docs/SPEC_EXCEPTIONS.md). Generated
`src/sevcon_can/tables.py` must be rebuilt with `tools/generate_tables.py` if
its pinned sources change.
