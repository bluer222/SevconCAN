# Jhoinrch USB-to-CAN adapters

Run `sevcon-can adapters` to list Linux CAN netdevs, detected direct
`gs_usb` devices, and serial-port candidates. Serial enumeration cannot
identify SLCAN firmware. Discovery sends no CAN frames.

Select the backend by the firmware that the adapter actually exposes. The
Jhoinrch name alone does not establish a model, USB protocol, or factory
firmware. CANable-compatible Candlelight and SLCAN interfaces are supported;
a proprietary vendor protocol requires its own python-can driver.

| Observed interface | Backend | Installation |
| --- | --- | --- |
| Linux CAN netdev such as `can0` | `socketcan` | No Python dependency |
| Candlelight USB device, accessed directly | `gs_usb` | `pip install 'sevcon-can[gs-usb]'` |
| Serial device such as `/dev/ttyACM0` or `COM4`, running SLCAN | `slcan` | `pip install 'sevcon-can[slcan]'` |

CANable documents Candlelight as a Linux `gs_usb` netdev and distinguishes
it from SLCAN serial firmware. These are firmware-specific paths, not proof
of compatibility with every Jhoinrch product.
[CANable getting started](https://canable.io/getting-started.html).

The Gen4 uses classic CAN. Its documented default rate is 1 Mbit/s; use the
configured controller rate if it has been changed. The library does not
change a Linux netdev's bitrate. The `--bitrate` argument configures direct
python-can backends only. CAN FD frames are excluded from the Gen4 receive
path instead of being truncated into classic frames.

## Linux SocketCAN

Inspect `ip -details link show` and `dmesg` to identify the actual interface.
A Candlelight adapter can appear as `can0`, `can1`, or another name. Linux
may need `sudo modprobe gs_usb` before the interface appears.

```sh
sudo ip link set can0 down
sudo ip link set can0 type can bitrate 1000000 restart-ms 100
sudo ip link set can0 up
ip -details -statistics link show can0
sevcon-can --channel can0 --node 0x20 info
```

```python
from sevcon_can import AsyncSevconGen4

async with AsyncSevconGen4.from_socketcan("can0", 0x20) as dev:
    print(await dev.read_identity())
```

`from_gs_usb("can0", 0x20)` is an alias for this Linux SocketCAN path.
It does not open USB directly. Administrator permission is normally needed
to configure a netdev; access to an existing CAN raw socket does not depend
on a serial-port group.

## Direct Candlelight USB

python-can's `gs_usb` backend supports Linux, Windows and macOS. It can
select a detected device by index or by USB bus and address. Its dependencies
include the upstream `gs-usb` package. Consult its platform instructions for
libusb, USB permissions and Windows driver setup.
[python-can gs_usb](https://python-can.readthedocs.io/en/stable/interfaces/gs_usb.html).

```sh
pip install 'sevcon-can[gs-usb]'
sevcon-can --backend gs_usb --usb-index 0 --bitrate 1000000 info
sevcon-can --backend gs_usb --usb-bus 1 --usb-address 7 --bitrate 1000000 info
```

```python
async with AsyncSevconGen4.from_python_can(
    0x20, interface="gs_usb", channel=0, index=0, bitrate=1000000
) as dev:
    print(await dev.read_identity())
```

USB bus/address values can change after reconnection. An index is an adapter
index, not a CAN node id. Use one owner for the USB device. On Linux the
kernel `gs_usb` driver can prevent direct USB access; prefer SocketCAN when
that driver already owns the device. Follow the driver documentation if a
direct USB connection is needed.

## SLCAN serial firmware

python-can uses a serial device path, Windows COM port or serial URL for
SLCAN. The serial baudrate is separate from the CAN bus bitrate. Its default
serial baudrate is 115200; set the rate required by the adapter firmware.
A `@baudrate` suffix on the port overrides `--serial-baudrate`.
[python-can SLCAN](https://python-can.readthedocs.io/en/stable/interfaces/slcan.html).

```sh
pip install 'sevcon-can[slcan]'
sevcon-can --backend slcan --port /dev/ttyACM0 --bitrate 1000000 info
sevcon-can --backend slcan --port COM4 --serial-baudrate 115200 --bitrate 1000000 info
```

```python
async with AsyncSevconGen4.from_python_can(
    0x20, interface="slcan", channel="/dev/ttyACM0",
    bitrate=1000000, tty_baudrate=115200
) as dev:
    print(await dev.read_identity())
```

On Linux, the user needs access to the serial device, often through the
`dialout` group. Do not let `slcand` and direct SLCAN access open the same
port. An adapter attached by `slcand` can instead use the SocketCAN backend.

## Controller selection and diagnostics

`--node` defaults to `0x20`. Confirm the configured node id. A heartbeat
COB-ID is `0x700 + node_id`, and boot-up frames use the same identifier.
A saved controller-specific EDS/DCF can be selected with `--eds path.eds`.

```sh
sevcon-can --channel can0 --node 0x20 --eds controller.eds info
sevcon-can --channel can0 --node 0x20 read-raw 0x1018 1
```

Raw SDO commands operate without EDS metadata. `write-raw` takes hexadecimal
bytes in wire order and bypasses dictionary access, scaling and range checks.
The controller still enforces its own access rules.

An SDO timeout can mean the wrong node id, bitrate, bus wiring, adapter
firmware, or a controller that is not responding. A drive fault alone does
not establish why an SDO timed out. Inspect `candump can0`, CAN interface
statistics, and EMCY messages alongside the application error.

## Wiring

Connect CAN-H and CAN-L to the controller's CAN connection and use the
manufacturer's grounding instructions. The programming harness reference is
`specs/pdf/Sevcon-Gen4-Programming-Cable.pdf`. Use termination at both physical
ends of the bus. If the adapter has a termination switch, its setting depends
on where the adapter is connected and what termination the harness already
provides. Controller power, connector orientation and pin numbering must
come from the controller and harness documents for the installed hardware.

The offline tests exercise python-can's virtual backend, serial protocol
responses, frame conversion and failure handling. They cannot establish
USB enumeration, electrical signaling, adapter firmware behavior, or the
object dictionary installed on a particular controller.
