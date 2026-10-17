# Sources

Every document the implementation is built from. All files are pinned
by SHA-256 in `specs/MANIFEST.json`; `tools/fetch_specs.py` downloads
(`--refresh` re-downloads and reports content changes, `--check`
verifies offline) and extracts text with `pdftotext -layout`.

## Sevcon / BorgWarner Gen4

| Document | File | Origin |
| --- | --- | --- |
| Gen4 Applications Reference Manual Rev 3.4 (doc 177/52701) | `specs/pdf/Gen4-Product-Manual-V3.4.pdf` | local manual + e-kart.fr / thunderstruck-ev.com / brtiberio/Sevcon_gen4 |
| Gen4 Applications Reference Manual Rev 3.3 | `specs/pdf/Gen4-Product-Manual-V3.3.pdf` | e-kart.fr |
| Gen4 Size 2/4/6 Product Manual (BorgWarner era; includes "Controlling Motors via CANopen") | `specs/pdf/Gen4-Size-2-6-Product-Manual.pdf` | electricdriveengineering.com.au |
| Gen4 Product Manual V2.2 | `specs/pdf/Gen4-Product-Manual-V2.2.pdf` | quadmotion.se / brtiberio/Sevcon_gen4 |
| Gen4 Product Manual V2.0x (2009) | `specs/pdf/Gen4-Product-Manual-V2.0.pdf` | e-kart.fr |
| Gen4 Size 2/4/6 datasheet, July 2016 | `specs/pdf/Gen4-Datasheet-Size-2-4-6-2016.pdf` | brtiberio/Sevcon_gen4 |
| Gen4 LED flash fault identification (short) | `specs/pdf/Gen4-Fault-Codes-Short.pdf` | thunderstruck-ev.com |
| Application note: vehicle setup and configuration | `specs/pdf/Sevcon-AppNote-Vehicle-Setup.pdf` | brtiberio/Sevcon_gen4 |
| DVT (DriveWizard) tutorial | `specs/pdf/Sevcon-DVT-Tutorial.pdf` | brtiberio/Sevcon_gen4 |
| Gen4 35-pin programming cable pinout | `specs/pdf/Sevcon-Gen4-Programming-Cable.pdf` | brtiberio/Sevcon_gen4 |

## CiA specifications (the Gen4 claims DS401 + DSP402 conformance)

| Document | File | Used for |
| --- | --- | --- |
| CiA 301 V4.2.0 application layer and communication profile | `specs/pdf/CiA-301-V4.2.pdf` | SDO abort codes (Table 22), EMCY classes/codes (Tables 25/26), data types (Table 44), 1000h–1FFFh objects |
| CiA DS301 V4.02 | `specs/pdf/CiA-DS301-V4.02.pdf` | abort-code cross-check (Table 20) |
| CiA DSP402 V2.0 drives and motion control | `specs/pdf/CiA-DSP402-V2.0.pdf` | device control commands (Table 4), state bits (Table 7), 6000h–67FFh objects |
| CiA DS401 V2.1 generic I/O modules | `specs/pdf/CiA-DS401-V2.1.pdf` | 6000h–67FFh objects (Gen4 maps them at +0x800: 6800h–6FFFh) |
| CiA DSP306 V1.3 electronic data sheet specification | `specs/pdf/CiA-DSP306-V1.3.pdf` | EDS format reference for `od.py` |

## Machine-readable object dictionaries

| File | Contents |
| --- | --- |
| `specs/od/Gen4.eds` (+ bundled copy in `src/sevcon_can/data/`) | Sevcon-generated EDS, product `0x07055037` rev `0x00010026`; the client's dictionary |
| `specs/od/Gen4-110V-300A-rev0x00010026-a.dcf` | DCF dump from a real 110 V/300 A unit |
| `specs/od/Gen4-110V-300A-rev0x00010026-b.dcf` | same, pre-operational capture |
| `specs/od/Gen4-110V-300A-rev0x00010015.dcf` | older firmware rev `0x00010015` dump |
| `specs/od/Gen4-AC24ls-rev0x00010017.dcf` | Sevcon-generated field DCF, product `0x07065037`, revision `0x00010017`, [pinned original](https://gist.githubusercontent.com/kasparsd/1e164f0f9ad1a61448a96deb7bd0682c/raw/8733cd63aadb74b0bd7c47054183b72963e0cc55/AC24ls.dcf); defines `291Fh` creep torque |

`tools/generate_supplement.py` extracts 89 missing definitions from AC24ls
into the optional packaged `Gen4-supplement.eds`. It strips vehicle parameter
values, preserves metadata and source identity, and never replaces baseline
entries. The optional result is a mixed catalog, not an exact firmware EDS.
Conflicting definitions must be selected through a matching custom dictionary.

## Independent field implementations (cross-checks)

| File | Used for |
| --- | --- |
| `specs/extra/ovms-rt_sevcon.cpp` | login sequence (`5000h:3`=0, `5000h:2`=`0x4BDF` → level 4), NMT state via `5110h` |
| `specs/extra/ovms-rt_sevcon.h` | known product codes (`1018h:2`) |
| `specs/extra/ovms-rt_sevcon_faults.cpp` | fault table, EMCY fault-id byte offsets, scalar FIFO selection/count/timestamps, event counters and selective log resets; min/max fields cross-checked with the installed EDS |
| `specs/extra/victron-sevcon.c` | fault table + EMCY decoding (`mdata[3]\|mdata[4]<<8`) |
| `specs/extra/victron-sevcon_gen4.py` | `5100h` 0.0625 scaling, EMCY example |
| `specs/extra/ovms-dcf-download.pl` | [OVMS implementation](https://github.com/openvehicles/Open-Vehicle-Server/blob/master/clients/diagmode/dcf_download.pl) of zero-based `5630h` enumeration and internal datatype/access codes |
| `specs/extra/fault-codes/fault codes.xlsx` | authoritative Sevcon fault table (2013) |
| `specs/extra/SEVCON-GEN4-Calculs-tensions-batteries.xlsx` | battery threshold calculator (reference) |

## Online review for the implementation expansion

Reviewed on 2026-10-02. These pages support the adapter/backend design and
cross-check the pinned manufacturer manual. They are references, not new
pinned inputs to the generated tables.

| Source | Used for |
| --- | --- |
| [Original Sevcon V3.4 manual](https://www.e-kart.fr/images/stories/technique/SEVCON/GEN4-Product-Manual-V3-4.pdf) | CANopen configuration, master/slave and encoder setup; firmware numbering/wiring ambiguities |
| [CANable getting started](https://canable.io/getting-started.html) | Candlelight SocketCAN versus SLCAN firmware paths |
| [python-can gs_usb](https://python-can.readthedocs.io/en/stable/interfaces/gs_usb.html) | Direct USB dependencies, platforms and adapter index/bus/address selection |
| [python-can SLCAN](https://python-can.readthedocs.io/en/stable/interfaces/slcan.html) | Serial device path, CAN bitrate and separate tty baudrate |
| [python-can source](https://github.com/hardbyte/python-can) | Notifier lifecycle, frame conversion and backend behavior |
| [canopen source](https://github.com/canopen-python/canopen) | Independent SDO client/server, block transfer recovery, NMT and EMCY interoperability |
| [CiA CAN Newsletter December 2024](https://www.can-cia.org/fileadmin/cia/documents/publications/cnlm/december_2024/24-4_cnlm.pdf) | Four predefined PDO identifiers; fifth PDO requires its configured identifier |

The independent gate was exercised with `canopen` 2.4.1, `python-can` 4.6.1,
pyserial and their virtual/serial loopback backends. USB driver packages were
installed to verify dependency availability, but no USB hardware was opened.
