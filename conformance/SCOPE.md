# Conformance scope

`conformance/run.py` checks the host implementation offline. It is not a CiA
certification or a hardware acceptance test.

## Checks

1. Pinned source files match `specs/MANIFEST.json` SHA-256 hashes.
2. `tables.py` matches regeneration from the pinned documents. Parser tests
   check the documented tables, ratings, voltage ranges, faults, data types,
   abort reasons, drive commands and state masks.
   `Gen4-supplement.eds` matches extraction from the pinned AC24ls DCF.
   The explicit-address coverage report is fresh, and new references cannot
   pass without a schema or an explicit unresolved/range classification.
3. Unit and integration tests cover EDS parsing and typed access; SDO
   expedited/segmented/block transfers, CRC, loss recovery, malformed frames,
   timeout and cancellation; NMT/heartbeat/EMCY; PDO identifiers/mapping;
   state-based drive control; cyclic producers; selected configuration
   restore; CLI and browser GUI flows.
   Added checks cover supplemental codecs, signed ranges, raw/binary writes,
   installed dictionary metadata/integrity, live EMCY identifiers, RTR TPDOs,
   RPDO padding, per-object timeouts and shutdown/state-cache behavior.
   Concurrent fault selection and canonical signed/unsigned metadata-limit
   comparisons have separate regressions.
4. With the optional backend dependencies installed, real python-can virtual
   backends and pyserial SLCAN loopback exercise frame conversion, remote
   DLC, error payloads, serial parsing, receiver failures and resource cleanup.
5. `--interop` additionally requires `conformance/interop.py`. It exchanges
   messages with the independent `canopen` package over python-can's virtual
   bus, in both client/server directions. Checks include expedited and
   segmented SDO, block-upload fallback, CRC block transfers, injected lost
   segments, NMT/heartbeat and EMCY.

The core installation can skip optional backend tests when dependencies are
missing. Install `.[dev]` and use `--interop` for the complete software gate.
Test counts and skips are printed. Missing independent-stack dependencies
cause an explicit failure when `--interop` is requested.

## Limits

- No controller or USB adapter was connected during this work. Two earlier
  physical value dumps were reviewed; [target evidence](../docs/TARGET_EVIDENCE.md)
  records exactly what they establish. Real electrical behavior,
  USB enumeration/firmware, CAN bus load, drive timing and installed parameter
  values remain unverified.
- Ratings tables contain the target's 36/48 V, Size 2, 275/110 A values. This
  does not establish that the bundled EDS matches a specific 634A42203.
- The simulator is a protocol fixture, with limits described in
  `docs/IMPLEMENTATION_STATUS.md`. The independent stack reduces reliance on
  that fixture but does not supply physical validation.
- Firmware flashing and proprietary DVT services remain unimplemented.
  LSS is unsupported according to the bundled EDS's `LSS_Supported=0`.
- Source pinning verifies the checked-in documents, not their currency or
  suitability for the exact installed firmware.
- The audit currently retains 34 unresolved index/subindex references. Run
  `python conformance/run.py --interop --require-complete` to fail on these
  known gaps. The normal software gate does not claim controller completeness.

## Acceptance

A failed test, source hash mismatch, stale generated table or failed
independent exchange fails the selected gate. Pinned documents were not
changed during the implementation expansion. The final full software gate
was run with optional backend and independent-stack dependencies installed;
see `docs/IMPLEMENTATION_STATUS.md` for the recorded result.
