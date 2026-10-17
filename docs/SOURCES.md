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

## Recovered isPointless archive

On 2026-10-17 the user identified the unavailable public repository
`https://github.com/isPointless/Sevcon`. GitHub's repository API and raw README
paths returned 404. This does not distinguish deletion from a private repository.
Software Heritage preserved a full Git visit on 2025-10-21, snapshot
`087dce64c2da1bfea6338a47b369b6a5ab932c71`, containing revision
[`78cd3f65594fcbbbc88cc125f4ba969c10bf69c7`](https://archive.softwareheritage.org/browse/revision/78cd3f65594fcbbbc88cc125f4ba969c10bf69c7/).
The revision is dated 2024-02-09. It does not establish that later changes
were preserved or that its dictionaries match firmware `0705.0013`.

Recovered documents were verified against their archived Git blob IDs and
inspected without running software. They remain local research inputs rather
than replacements for the installed EDS.

| Archived path | Git blob ID | Finding |
| --- | --- | --- |
| `Sevcon_Documentation/Object Dictionary FW 58_34.xlsx` | `c0e37931e58bf25d5e8ef610347b12e10d0b5c5c` | Third worksheet, N837/N850: system/fault FIFO resets remove entries only up to the current login level. N1348: `5640h:1` commands are defined in `obd_cli.tcl`; command values are not provided. |
| `Sevcon_Documentation/Object Dictionary FW 44A.xlsx` | `352906f2d0846fdf5a97036d17657d52bdae18ba` | Third worksheet, N789/N802 corroborate selective FIFO clearing. Its domain-log definitions differ from the installed target. |
| `Sevcon manuals /PMAC encoder commission by Sevcon DVT macros.pdf` | `bb033c8b3c10aef0cfeea591b39848b2ad434102` | Commissioning reference; does not establish the target's proprietary command numbers or binary log formats. |

The complete `Sevcon DVT/SevconDVT_Customer_Online_2.exe` was recovered and
verified as 21,076,357 bytes, Git blob
[`9599c3cc59a9f46c919b353e433708afd97cc5de`](https://archive.softwareheritage.org/browse/content/sha1_git:9599c3cc59a9f46c919b353e433708afd97cc5de/),
SHA-256 `7f3b821987fc91c3634442ff7a235b90e0b7f47400e0dde5d3ac1db7146fb510`.
Some archive responses truncated with HTTP 200; resumed downloads were accepted
only after the complete blob and SHA-256 matched. Static extraction with
7-Zip followed by innoextract recovered 160 files from its nested `dvtc.exe`.
Neither installer nor the recovered Tcl or DLL code was executed.

The following files under `app/common/program` define the implemented
protocol facts. They are not runtime dependencies and are not vendored here.

| File | SHA-256 | Used for |
| --- | --- | --- |
| `load.tcl` | `e0b886478fcb0231fb2265427c25a6fbfe68ad3153929fa1cc1c030c0f62c767` | Gen4 bootloader identity, memory capability records, DLD/DLDx container, Intel HEX data, packet checksum, transfer reset and commit checksum |
| `uut.tcl` | `15f784f539e74c4637c9f88f854b0e49e4042fa1fb634601ac4401b65368d547` | Event counter saved bit, selective/all-log resets and 24-byte min/max monitor domain |
| `sdo_client.tcl` | `3bd2ee8d7ef174f72e1a684760f8bac656667de5000f624bb3989be6dc84077c` | DVT's four-byte segmented-upload length prefix is host API metadata, not part of the domain wire payload |

The recovered `dvtint.dll` files are native PE libraries, not .NET assemblies.
Their transport role and the available Tcl source made native decompilation
unnecessary for these workflows. The package contains no `obd_cli.tcl` or
numeric `5640h` command definitions. Its `5600h:8` ASCII CLI procedure targets
an entry absent from this unit and was not enabled.

Four complete firmware packages were independently recovered and checked
against their archived Git blobs. All Intel HEX checksums pass; each has one
contiguous memory-space-5 image. The Python parser accepts each with matching
address and byte counts. Their hardware-compatibility fields are empty, and
none establishes target-controller compatibility.

| Firmware package | Git blob ID |
| --- | --- |
| `SN0058.28.dld` | `25d0c529acd9cedd11f9a6201f052c1b29aff633` |
| `UK0319_35.dld` | `1e875cc3540a76a52527b0028b80a38e8862ccf3` |
| `UK0319B38.dld` | `a4bf24713c290033a7be4cf6197ac431ee2cba62` |
| `UK0319A44.dld` | `af81b5a30e05d7180abd8d5d0fb37c571e663496` |

A local preservation copy lives in `.local-backups/sevcon-dvt/`, excluded by
the repository's `.gitignore`. Its `README.md`, `MANIFEST.json` and
`vault-status.json` record provenance, file checksums, known incomplete
downloads and the full-snapshot recovery status. The preserved files are
local only; Git commits and pushes do not include this directory.
