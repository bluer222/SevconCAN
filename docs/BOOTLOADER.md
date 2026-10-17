# Gen4 bootloader

The client implements the `D0701` Gen4 checksum protocol recovered from
Sevcon DVT's Tcl source. Source provenance and component hashes are recorded
in [sources](SOURCES.md). No physical firmware write or bootloader transition
was performed during implementation. The installed application version
`0705.0013` does not identify this unit's bootloader version.

## Inspect and preflight

```python
from sevcon_can import Gen4Bootloader, load_dld

image = load_dld("firmware.dld")
loader = Gen4Bootloader(dev)
identity = await loader.identify()
plan = await loader.preflight(image)
```

`load_dld` parses the text container used by DLD/DLDx, retaining the source
file's SHA-256. It checks Intel HEX record lengths, checksums, supported
record types, address bounds, contiguous data and EOF markers. It rejects
duplicate or aliased sections, gaps, overlaps and unsupported formats.
It does not execute Tcl, unpack executable firmware or decrypt payloads.
Known encrypted memory sections transport their existing bytes unchanged.

Identification reads `100Ah`, falling back to `5A10h` only for an
object-not-found abort. Only `D0701` identifiers are accepted; other
controllers' CRC protocols are not treated as Gen4 equivalents.
Preflight reads each section's block size, page size, start and length.
Every section must pass before the first programming write. Images must be
contiguous from the section's advertised start address. Final pages are
zero-padded inside the advertised bounds, never truncated to fit.

For offline geometry checks, use `preflight_image(image, capabilities)`.
CLI package inspection opens no CAN adapter, and malformed packages are
rejected before opening an adapter for a plan or program command.

```sh
sevcon-can firmware-info firmware.dld
sevcon-can --channel can0 --node 1 bootloader-info --section dsp-zeffer
sevcon-can --channel can0 --node 1 firmware-plan firmware.dld
```

The last two commands expect the controller already to be in bootloader mode.
Matching memory geometry does not establish the right motor firmware,
controller part number or image authenticity. The four recovered sample
packages validate the parser only; none is a recommended image for this unit.

## Program and exit explicitly

```python
result = await loader.program(image)
print(result.complete, result.commit_checksum)
# A previous failed section can remain pending in this session.
if result.commit_checksum is None:
    raise RuntimeError("The programming session still has incomplete sections")
await loader.end(result.commit_checksum)
```

Programming writes each packet's address, length, data and one's-complement
checksum in that order. A checksum acknowledgement records an accepted
packet. The first programmed block can erase its memory section. Successful
sections reset the bootloader transfer sequence through `5A32h`.
Programming leaves the controller in bootloader mode.

Loader instances sharing one device also share checksum and failure history.
Separate programming calls accumulate the latest checksum for each physical
section; reprogramming a section replaces its earlier checksum. A failed
section remains pending until that section is successfully reprogrammed.
`result.complete` describes the current call. `commit_checksum` is unavailable
while any section in the shared session remains pending. Transfer resets
preserve this bookkeeping; acknowledged entry or exit starts a new session.

A failed or cancelled operation may leave erased or partial flash. Errors
report acknowledged progress and the failed stage; cancellation propagates
and `loader.last_result` retains progress. There are no automatic retries,
entry, exit, login or NMT transitions, and no transfer reset after failure.
An acknowledgement verifies protocol acceptance, not an independent flash
read-back. The source documents only EEPROM as readable through this workflow.

```sh
sevcon-can --channel can0 --node 1 firmware-program firmware.dld
sevcon-can --channel can0 --node 1 bootloader-end 0x1234
```

The checksum above is a placeholder. Use the completed programming result's
reported commit checksum for the actual session. Exit is a separate command
that commits the image and can run it. Running incompatible firmware can
leave the controller unable to start normally.

Separate CLI invocations do not share checksum history. Program all required
sections in one package and one `firmware-program` command. For multiple
packages in the same bootloader session, use the Python API with the same
device object. Other clients' writes are outside this client's bookkeeping.

`loader.start()` and `bootloader-start` explicitly request entry using
`5400h`. Entry requires a fresh pre-operational state read. The caller supplies
login credentials and sets that state; no transitions are added automatically. `loader.reset()` or
`bootloader-reset` resets the transfer sequence without exiting bootloader.

`await loader.read_eeprom()` reads raw EEPROM section bytes through its
address/length/data registers. Optional `start` and `length` are validated
against live geometry. `bootloader-read-eeprom backup.bin` writes those bytes
to a file. Other tools using the same bootloader selectors must be idle.

## Validation limits

Tests use an independent SDO peer to verify sizes, ordering, checksums,
timeouts, section preflight, failures, cancellation and session state.
Four independently verified real DLD packages exercise the offline parser.
These checks do not prove electrical behavior, bootloader revision support
on this unit, image compatibility, or physical erase/program/commit behavior.
