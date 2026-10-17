# Gen4 CANopen object reference (EDS `0x07055037` rev `0x00010026`)

334 objects. Names, scaling and limits below come from the bundled EDS
(`src/sevcon_can/data/Gen4.eds`); look any object up live with
`dev.search("<text>")` or `dev.read(...)`. Physical value = raw ×
SCALING unless noted.

## Communication area (CiA 301)

| Object | Name | Notes |
| --- | --- | --- |
| 1000h | Device type | const |
| 1001h | Error register | ro, EMCY byte 2 mirrors this |
| 1005h / 1006h | SYNC COB-ID / cycle period | master: `0x40000080` + rate; slaves `0x80` |
| 1010h:1 / 1011h:1 | Store / restore | write `"save"` `0x65766173` / `"load"` `0x64616F6C` |
| 1014h | EMCY COB-ID | set `0x80 + node` |
| 1016h / 1017h | Heartbeat consumer / producer | ms; configure both sides |
| 1018h | Identity | live vendor/product/revision/serial; EDS defaults are not installed-device identity |
| 1200h–1203h | SDO servers | 1200h default `0x600/0x580 + node`; 1201h–1203h disabled (`0`) |
| 1400h–1404h / 1600h–1604h | RPDO comm / mapping | 8 entries max per PDO |
| 1800h–1804h / 1A00h–1A04h | TPDO comm / mapping | defaults disabled (`0x80000202`…) |
| 1280h–1288h | Client SDOs | for Gen4-as-master configurations |

## Manufacturer area (Sevcon)

| Object | Name | Notes |
| --- | --- | --- |
| 5000h:1/2/3 | Access level / password / user id | login: `3`=0, `2`=`0x4BDF` → level 4; `2`=0 logs out |
| 5001h | Password key | customer-specific encryption input |
| 2800h | Force pre-operational | BOOLEAN rw, level 4; 1 = pre-op, 0 = operational |
| 2830h | Assumed severity for non-Sevcon EMCYs | |
| 5100h:1/2/3/4 | Battery V / battery A / heatsink °C / capacitor V | 0.0625 V/A scales |
| 5110h | NMT state | 4 stopped, 5 operational, 127 pre-op |
| 5300h:1/2/3 | Active fault count / select / id | live fault list |
| 5310h | General abort reason | Table 33 code after SDO abort `08000000h` |
| 5610h:1/2 | Event id → text | write id, read controller description |
| 5620h | Configuration checksum | |
| 5630h | Installed object-table metadata | `dev.discover_dictionary()`; only the table cursor is written |
| 5900h | Physical layer (bitrate) | Gen4 default 1 Mbit/s |
| 2020h/2021h/2040h/2060h | Motor drive VPDO images (left/right/pump/steer) | torque/velocity/controlword/statusword images |
| 21xxh–2xxxh | Switches, profiles, limits, protection | use `dev.search()` |
| 3000h–3400h | VPDO mapping images | internal routing, not CANopen PDOs |
| 4600h / 4602h | Motor measurements / AC debug | incl. torque demand/actual (Nm) |
| 6800h/6802h | Digital input states / polarity | DS401 + `0x800` (second logical device) |
| 6C01h/6C11h/6C43h | Analog inputs / contactor outputs / error modes | DS401 + `0x800` |

## Drive area (DSP402, subset present in EDS)

| Object | Name | Notes |
| --- | --- | --- |
| 6040h | Controlword | rww; commands in `drive.command_bits` |
| 6041h | Statusword | ro; states in `drive.decode_state` |
| 6060h / 6061h | Mode selection / display | speed 3, torque 4; EDS lists voltage 5 but its HighLimit excludes it; pre-op write |
| 6071h / 6072h | Target / max torque | 0.1 % of peak |
| 6074h / 6076h / 6077h / 6078h | Torque demand / peak / actual / current | feedbacks |
| 2900h | Traction control mode | 0 torque/speed, 1 closed-loop speed; keep consistent with 6060h |

The optional dictionary adds 89 source-backed definitions, including `291Fh`
creep torque, seat braking, motor nameplate/maps and automatic configuration.
Select it with `gen4_od(include_supplemental=True)` or `--supplemental`.
Its definitions come from another firmware and never override baseline
entries. Creep torque has ratio scaling `0.001`, so physical `0.05` means 5%.
Other firmware differences require a matching custom EDS/DCF.

See [coverage](COVERAGE.md) for every explicit manual reference and remaining
schema gaps. Unknown typed keys raise `ObjectDictionaryError`; raw SDO access
can address entries without local metadata. Fault ids are listed in
`tables.GEN4_FAULTS` via `faults.search("<text>")`.
