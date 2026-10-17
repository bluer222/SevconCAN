# Physical controller dump evidence

The user supplied two existing value dumps from the physical controller on
2026-10-16. They were inspected offline and read-only. No controller commands
were issued during this review. Full dumps, serial numbers, local absolute
paths and motor configuration values are not reproduced here.

| Source file | Bytes | SHA-256 |
| --- | ---: | --- |
| `sevcon-values.json` | 375975 | `4929b127592c93b887330783cde4f78342dae26a55212c89912e9a9179bee414` |
| `sevcon-values2.json` | 390606 | `9ddcb9b4b93476913f42151dc3b3dcaf94604c5ea60418d62b2aad12b4bb3551` |

Both files use schema `sevcanwiz.values.v1`, report `simulated=false`, and
identify the same controller. Live identity is vendor `0x0000001E`, product
`0x0705301B`, dictionary revision `0x00010026`. The device name includes
`DRL PMAC`, and the software version is `0705.0013`.

The user's [product listing](https://electricdrivesolutions.com/products/sevcon-34a42203),
reviewed on 2026-10-16, identifies a 634A42203, 36/48 V, 275 A, Size 2,
Sin/Cos controller. This agrees with the project's intended hardware target.
The listing does not specify the installed firmware or object dictionary.

The selected EDS declares product `0x07055037`, so both reports correctly
record a product mismatch. Matching vendor and revision do not establish
that every parameter definition matches this controller.

The Engineering dump reads the configured node id at `5900h:1` as 1.
With the same controller and its existing SocketCAN adapter configuration,
the next enumeration command is:

```sh
sevcon-can --channel can0 --node 1 discover --json > installed-dictionary.json
```

Use the actual adapter channel if it differs. This selects metadata rows
without changing motor parameters or NMT state.

## Scan results

| Observation | First dump | Second dump |
| --- | ---: | ---: |
| Scan start, UTC | 2026-10-16 21:32:59 | 2026-10-16 21:42:36 |
| Duration, seconds | 40.43 | 33.25 |
| Successful reads | 745 | 1328 |
| Failed reads | 586 | 3 |
| Write-only entries skipped | 9 | 9 |
| Live access level at `5000h:1` | 0 | 4, Engineering |
| Live NMT state at `5110h` | 5, Operational | 5, Operational |

Every recorded failure is an SDO abort `0x08000000` with Gen4 reason 8,
access level too low. The second scan successfully reads 583 entries that
failed in the first scan. It has no newly failing address.

The three remaining failures are `5603h:3` and `5603h:4`, DSP memory-peek
entries, and `5640h:1`, the CLI command entry. These are access failures,
not evidence that the functions are unsupported. The files do not establish
which password produced the Engineering session.

Each scan accounts for exactly the baseline EDS's 1340 variables: 1331 reads
attempted and nine write-only entries skipped. `complete=true` means that
the selected scan finished. It does not mean that all reads succeeded or
that all controller objects were discovered.

## What the bytes establish

All successful fixed-size payloads have the length required by the selected
EDS, and its codecs decode every successful payload. Both dumps include a
successful 9488-byte domain upload at `4617h:1`, together with other payloads
longer than four bytes. This is physical evidence of large SDO uploads.
The dumps do not identify the transfer mode or verify block download,
retransmission recovery, PDO remapping or drive-control writes.

The object paths, units, scaling and decoded labels in the files were supplied
by the selected EDS. They are not independent metadata returned by the
controller. Raw bytes and successful addresses provide stronger evidence
than a decoded label copied from that dictionary.

Both scans read `5630h:1` as 1340. The other table metadata fields are zero,
and the files contain no sequence of selected table rows. Equal entry counts
do not prove equal address sets. The new
`discover_dictionary()` workflow must enumerate the cursor before reporting
installed-only entries, absent dictionary entries or metadata disagreements.

Neither dump attempts `291Fh`, the optional creep-torque entry, or any of the
unresolved addresses listed in [the coverage audit](COVERAGE.md). Their
presence, absence and parameter formats on this controller remain unverified.
The AC induction firmware used as a supplemental source must not replace
the PMAC dictionary automatically.

Observed record maxima reinforce the need for firmware-specific definitions:

| Record | Live subindex 0 |
| --- | ---: |
| `2903h`, electromechanical brake | 3 |
| `290Eh`, brake feathering | 3 |
| `2910h`, throttle | 21 |
| `3200h`, analogue-output VPDO mapping | 3 |
| `4630h`, encoder | 21 |
| `4641h`, motor data | 43 |
| `5600h`, domain uploads | 10 |

Newer manual references such as `2903h:4`, `2903h:6`, `290Eh:4`, `290Eh:5`
and `2910h:22` exceed the corresponding observed maxima. A sparse record's
maximum does not prove that every intervening subindex exists. Direct reads
or table enumeration are still needed to establish support.

## Metadata discrepancies exposed by the scans

The Engineering scan reads valid positive motor temperature settings at
`4621h:3`, `4621h:4` and `4621h:5`. The EDS gives these INTEGER16 entries
limits of `-32768` and `32768`, with the latter one past the signed maximum.
Treating that positive decimal boundary as the negative wire value `0x8000`
incorrectly rejected normal settings. `Variable.raw_limits()` now bounds
this exact malformed full-range case to the representable signed maximum.
The original source metadata remains available. These are motor temperature
settings, not battery temperature settings.

Both scans also expose read-only metadata discrepancies: `5100h:8` returns
a 16-bit logic-supply reading above the EDS limit of 255, and `4602h:2B`
returns `0xFFFF` despite its declared upper limit of 4095. No signedness,
sentinel interpretation or replacement limits are inferred from these values.

`1010h:1` and `1011h:1` return capability bits when read. Their read value of
one need not lie in the EDS ranges for their respective write signatures.
This is expected command-object behavior, not a failed read.
