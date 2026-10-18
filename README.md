> This project is primarily AI-generated code. Review and test it before using it with hardware.

# SevconCAN

An async Python CANopen library and CLI for Sevcon Gen4 motor controllers.
It reads and writes parameters, monitors faults and PDOs, backs up configuration,
and provides drive-control and bootloader tools. Requires Python 3.9 or newer.
The core has no runtime dependencies. For a desktop interface, use
[SevCANWiz](https://github.com/bluer222/SevCANWiz).

The target is a 634A42203 Size 2 controller. Read compatibility has evidence from
physical controller dumps; adapter behavior, drive-control writes and firmware
flashing still need hardware validation. Use the dictionary for your firmware.

## Usage

```sh
python3 -m pip install .
sevcon-can --sim info
sevcon-can --sim shell

# Read a controller through an already-configured SocketCAN interface.
sevcon-can --channel can0 --node 0x20 info
```

Replace the channel and node ID with your controller's settings. See the
[adapter guide](docs/ADAPTERS.md) for CAN setup, SLCAN and direct USB support.
For the local browser GUI, run `python3 tools/sevcon_gui.py --sim`.

## Documentation

- [Detailed usage](docs/USAGE.md): installation, Python API, CLI, logs, PDOs, configuration backups and validation.
- [Implementation status](docs/IMPLEMENTATION_STATUS.md) and [controller evidence](docs/TARGET_EVIDENCE.md): coverage and known limits.
- [Bootloader guide](docs/BOOTLOADER.md): firmware commands and compatibility limits.
- [Object dictionary](docs/OBJECTS.md) and [coverage report](docs/COVERAGE.md): parameter definitions and unresolved references.
- [Sources](docs/SOURCES.md), [specification exceptions](docs/SPEC_EXCEPTIONS.md) and [conformance scope](conformance/SCOPE.md): provenance and verification details.

## License

Project code is licensed under the GNU Affero General Public License v3.0 only,
`AGPL-3.0-only`. See [LICENCE](LICENCE). Bundled third-party reference material
retains its own copyright and license notices.
