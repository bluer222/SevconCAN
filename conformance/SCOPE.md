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
   comparisons have separate regressions. Diagnostic-log tests cover complete
   FIFO iteration, selector/reset serialization, all event counters and both
   twelve-field min/max monitors. Logout tests require fresh level-zero
   verification and propagate verification failures.
4. With the optional backend dependencies installed, real python-can virtual
   backends and pyserial SLCAN loopback exercise frame conversion, remote
   DLC, error payloads, serial parsing, receiver failures and resource cleanup.
5. `--interop` additionally requires `conformance/interop.py`. It exchanges
   messages with the independent `canopen` package over python-can's virtual
   bus, in both client/server directions. Checks include expedited and
   segmented SDO, block-upload fallback, CRC block transfers, injected lost
   segments, NMT/heartbeat and EMCY.
6. `--installed PATH` additionally checks an existing `5630h` JSON export
   offline. It validates row bounds, sequence and unique addresses, then
   compares raw metadata with the baseline EDS. Cached derived fields and
   mismatch summaries are ignored. A complete scan with zero missing/absent
   entries and zero schema disagreements passes. Unreported `0..0` limits
   remain explicitly unverified. This stage does not verify identity,
   scaling or hardware behavior and does not weaken `--require-complete`.

The core installation can skip optional backend tests when dependencies are
missing. Install `.[dev]` and use `--interop` for the complete software gate.
Test counts and skips are printed. Missing independent-stack dependencies
cause an explicit failure when `--interop` is requested.

## Limits

- This offline review issued no controller or USB adapter commands. Two
  physical value dumps and an installed-dictionary export were reviewed;
  [target evidence](../docs/TARGET_EVIDENCE.md) records what they establish.
  Electrical behavior, USB firmware, CAN bus load, drive timing and parameter
  writes remain unverified.
- Ratings tables contain the target's 36/48 V, Size 2, 275/110 A values. This
  does not establish that the bundled EDS matches every 634A42203. The supplied
  unit's complete export matches its address set and reported schema fields;
  scaling and units still come from the EDS, and the product header differs.
- The simulator is a protocol fixture, with limits described in
  `docs/IMPLEMENTATION_STATUS.md`. The independent stack reduces reliance on
  that fixture but does not supply physical validation.
- Firmware flashing and proprietary DVT services remain unimplemented.
  LSS is unsupported according to the bundled EDS's `LSS_Supported=0`.
- Source pinning verifies the checked-in documents, not their currency or
  suitability for the exact installed firmware.
- The audit currently retains 34 unresolved index/subindex references. Run
  `python conformance/run.py --interop --require-complete` to fail on these
  known gaps. None is installed in the supplied target export. Firmware
  programming, proprietary command semantics and domain-log decoding remain
  open even when the target object-dictionary gate passes.

## Acceptance

A failed test, source hash mismatch, stale generated table or failed
independent exchange fails the selected gate. Pinned documents were not
changed during the implementation expansion. The final full software gate
was run with optional backend and independent-stack dependencies installed;
see `docs/IMPLEMENTATION_STATUS.md` for the recorded result.
