"""In-memory Gen4 simulator for tests and examples (no hardware needed).

Mirrors ``ve_direct.transport.SimulatedTransport``: a virtual Gen4 node
that speaks real SDO/NMT/heartbeat/EMCY/TPDO frames over a
:class:`VirtualBus`, backed by the bundled EDS defaults.

.. code-block:: python

    from sevcon_can.simulator import create_pair

    dev, sim = create_pair(node_id=0x20)
    async with dev, sim:
        await dev.login(0x4BDF)
        print(await dev.read_physical(0x5100, 1))  # battery volts

Behaviors implemented (all grounded in the manual / EDS / OVMS):

* SDO server on the default (``0x600``/``0x580``) connection set, with
  CiA abort codes for unknown objects (``0x06020000``), unknown
  sub-indices (``0x06090011``), reads of write-only objects
  (``0x06010001``) and writes of read-only objects (``0x06010002``).
* Login: writing ``5000h:3`` stores the user id; writing ``5000h:2``
  with ``0`` logs out (level 0) and with ``SIM_PASSWORD`` (``0x4BDF``,
  the OVMS Twizy engineering password) grants level 4. Any other
  password aborts with ``0x06090030`` (invalid value).
* NMT slave: standard commands on COB-ID 0x000 addressed to the node
  (or broadcast 0) update ``5110h`` and the heartbeat byte; ``2800h``
  writes do the same (manual section 7.3: 1 = pre-operational,
  0 = operational). Reset commands re-initialise the store.
* Heartbeat producer honouring ``1017h`` (default 500 ms here so tests
  do not wait; write ``1017h = 0`` to silence it).
* Minimal DSP402 drive: ``6040h`` writes move ``6041h`` through
  Switch on disabled (``0x40``) -> Ready (``0x21``) -> Switched on
  (``0x23``) -> Operation enabled (``0x27``); bit 7 resets a latched
  fault. ``6060h`` writes echo to ``6061h``.
* Fault list: ``inject_fault(fid)`` appends to the ``5300h`` list and
  emits an EMCY frame (Sevcon layout: fault id in payload bytes 3-4).
  ``5610h`` resolves ids to ``tables`` messages.
* ``1010h:1``/``1011h:1`` accept the CiA save/load signatures.
"""
from __future__ import annotations

import asyncio
import struct
import time
from copy import deepcopy
from typing import Dict, List, Optional, Tuple

from . import datatypes as dt
from .discovery import ACCESS_TYPES, DATA_TYPES
from .emcy import encode_emcy
from .network import Network
from .nmt import (
    HEARTBEAT_BASE,
    NMT_COB_ID,
    NMT_PREOP,
    NMT_RESET_COMM,
    NMT_RESET_NODE,
    NMT_START,
    NMT_STOP,
    STATE_OPERATIONAL,
    STATE_PREOP,
    STATE_STOPPED,
)
from .od import ObjectDictionary, Variable, gen4_od
from .pdo import (COB_ID_EXTENDED_BIT, cob_id_can_id, cob_id_valid,
                  decode_mapping_word, decode_pdo, encode_pdo, is_dummy_mapping, SYNC_COB_ID)
from .sdo import (
    ABORT_NO_OBJECT,
    ABORT_NO_SUBINDEX,
    ABORT_READ_ONLY,
    ABORT_VALUE_RANGE,
    ABORT_WRITE_ONLY,
    SdoServer,
)
from .exceptions import SevconError
from .tables import GEN4_FAULTS
from .transport import CanFrame, VirtualBus

#: Password accepted by the simulator for the engineering level.
SIM_PASSWORD = 0x4BDF
SIM_USER_ID = 0

STORE_SIGNATURE = 0x65766173
RESTORE_SIGNATURE = 0x64616F6C

_U16 = struct.Struct("<H")
_U32 = struct.Struct("<I")

# Statusword values used by the simulated drive (without the remote bit).
ST_SWITCH_ON_DISABLED = 0x0040
ST_READY = 0x0021
ST_SWITCHED_ON = 0x0023
ST_OP_ENABLED = 0x0027
ST_QUICK_STOP = 0x0007
ST_FAULT = 0x0008


class SimulatedGen4:
    """A virtual Gen4 node. Attach to a :class:`Network`, then ``start()``."""

    def __init__(self, node_id: int = 0x20,
                 od: Optional[ObjectDictionary] = None,
                 heartbeat_ms: int = 500) -> None:
        if not 1 <= node_id <= 127:
            raise ValueError("node id must be 1..127")
        if not 0 <= heartbeat_ms <= 32767:
            raise ValueError("heartbeat period must be 0..32767 ms")
        self.node_id = node_id
        self.od = od if od is not None else deepcopy(gen4_od())
        self.heartbeat_ms = heartbeat_ms
        self.network: Optional[Network] = None
        self._server: Optional[SdoServer] = None
        self._store: Dict[Tuple[int, int], bytes] = {}
        self._faults: List[int] = []
        self._fault_select = 0
        self._event_select = 0
        self._statusword = ST_SWITCH_ON_DISABLED
        self._controlword = 0
        self._pending_rpdos = {}
        self._subscriptions = []
        self._tasks: List["asyncio.Task[None]"] = []
        self.sent_emcy: List[Tuple[int, bytes]] = []
        self._hb_state = STATE_PREOP
        self._init_store()

    # ---- lifecycle ----
    def attach(self, network: Network) -> "SimulatedGen4":
        self.network = network
        return self

    async def start(self) -> None:
        if self.network is None:
            raise RuntimeError("attach(network) before start()")
        await self.network.start()
        self._server = SdoServer(
            self.network, self.node_id, self._sdo_read, self._sdo_write)
        self._server.start()
        self._subscriptions = [
            self.network.subscribe(NMT_COB_ID, self._on_nmt),
            self.network.subscribe(None, self._on_pdo_frame),
        ]
        self._hb_state = self._state()
        self._tasks = [
            asyncio.get_running_loop().create_task(self._heartbeat_loop()),
        ]

    async def close(self) -> None:
        for subscription in self._subscriptions:
            subscription.cancel()
        self._subscriptions.clear()
        for task in self._tasks:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        self._tasks.clear()
        if self._server is not None:
            await self._server.close()
            self._server = None

    async def __aenter__(self) -> "SimulatedGen4":
        await self.start()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    # ---- store ----
    def _init_store(self) -> None:
        from .exceptions import ObjectDictionaryError  # local: keeps import light
        self._store.clear()
        for var in self.od.variables():
            try:
                value = var.value(self.node_id)
            except Exception:
                value = None
            if value is None:
                try:
                    value = var.default(self.node_id)
                except Exception:
                    value = None
            if value is None:
                size = dt.size_of(var.data_type)
                raw = b"\x00" * size if size else b""
            else:
                try:
                    raw = var.encode_raw(value)
                except Exception:
                    size = dt.size_of(var.data_type)
                    raw = b"\x00" * size if size else b""
            self._store[(var.index, var.subindex)] = raw
        self._statusword = ST_SWITCH_ON_DISABLED
        self._controlword = 0
        self._pending_rpdos.clear()
        self._store[(0x1017, 0)] = _U16.pack(self.heartbeat_ms)
        if not int.from_bytes(self._store.get((0x1014, 0), b""), "little"):
            self._store[(0x1014, 0)] = _U32.pack(0x80 + self.node_id)
        self._store[(0x5000, 1)] = bytes((0,))
        self._store[(0x5110, 0)] = bytes((STATE_PREOP,))
        self._store[(0x6041, 0)] = _U16.pack(self._statusword)
        self._hb_state = STATE_PREOP
        self._faults.clear()
        self._dictionary_entries = list(self.od.variables())
        self._dictionary_cursor = 0

    def _find(self, index: int, subindex: int) -> Variable:
        from .exceptions import ObjectDictionaryError  # noqa: F401  (re-export guard)
        try:
            entry = self.od[index]
        except Exception:
            raise _abort(ABORT_NO_OBJECT, index, subindex)
        if isinstance(entry, Variable):
            if subindex != 0:
                raise _abort(ABORT_NO_SUBINDEX, index, subindex)
            return entry
        try:
            return entry[subindex]
        except Exception:
            raise _abort(ABORT_NO_SUBINDEX, index, subindex)

    # ---- SDO handlers ----
    def _sdo_read(self, index: int, subindex: int) -> bytes:
        from .exceptions import SdoAbortError
        var = self._find(index, subindex)
        if not var.readable:
            raise SdoAbortError(ABORT_WRITE_ONLY, index, subindex)
        if index == 0x5630 and subindex:
            return self._dictionary_metadata(subindex)
        if (index, subindex) == (0x5300, 1):
            return _U16.pack(len(self._faults))
        if (index, subindex) == (0x5300, 3):
            fid = self._faults[self._fault_select] if self._faults else 0
            return _U16.pack(fid)
        if (index, subindex) == (0x5610, 2):
            fault = GEN4_FAULTS.get(self._event_select)
            text = fault.message if fault else f"Unknown fault 0x{self._event_select:04X}"
            return text.encode("latin-1")
        return bytes(self._store.get((index, subindex), b""))

    def _dictionary_metadata(self, subindex: int) -> bytes:
        if subindex == 1:
            return _U16.pack(len(self._dictionary_entries))
        var = self._dictionary_entries[self._dictionary_cursor]
        internal_type = next((code for code, dtype in DATA_TYPES.items()
                              if dtype == var.data_type), 14)
        access = "rw" if var.access in ("rwr", "rww") else var.access
        access_code = next((code for code, name in ACCESS_TYPES.items() if name == access), 0)
        values = {
            2: (var.index, 2), 3: (var.subindex, 1),
            4: (getattr(self.od[var.index], "object_type", 7), 1),
            5: (internal_type, 1), 6: (access_code, 1),
            7: (int(var.low or 0) & 0xFFFFFFFF, 4),
            8: (int(var.high or 0) & 0xFFFFFFFF, 4),
            9: (int(var.pdo_mappable), 1),
            10: (int(var.extra.get("OBJECT_VERSION", "0"), 0), 2),
            11: (var.obj_flags, 1),
        }
        value, size = values[subindex]
        return value.to_bytes(size, "little")

    def _sdo_write(self, index: int, subindex: int, data: bytes) -> None:
        from .exceptions import SdoAbortError
        var = self._find(index, subindex)
        if not var.writable:
            raise SdoAbortError(ABORT_READ_ONLY, index, subindex)
        if (index, subindex) == (0x5630, 1):
            cursor = int.from_bytes(data, "little")
            if len(data) != 2 or cursor >= len(self._dictionary_entries):
                raise SdoAbortError(ABORT_VALUE_RANGE, index, subindex)
            self._dictionary_cursor = cursor
            return
        if (index, subindex) == (0x5000, 2):
            password = int.from_bytes(data[:2], "little")
            if password == 0:
                self._store[(0x5000, 1)] = bytes((0,))
            elif password == SIM_PASSWORD:
                self._store[(0x5000, 1)] = bytes((4,))
            else:
                raise SdoAbortError(ABORT_VALUE_RANGE, index, subindex)
            self._store[(index, subindex)] = bytes(data)
            return
        if (index, subindex) == (0x2800, 0):
            self._store[(index, subindex)] = bytes(data)
            self._set_state(STATE_PREOP if data[:1] == b"\x01" else STATE_OPERATIONAL)
            return
        if (index, subindex) == (0x5300, 2):
            self._fault_select = int.from_bytes(data[:2], "little")
            self._store[(index, subindex)] = bytes(data)
            return
        if (index, subindex) == (0x5610, 1):
            self._event_select = int.from_bytes(data[:2], "little")
            self._store[(index, subindex)] = bytes(data)
            return
        if (index, subindex) == (0x6040, 0):
            self._store[(index, subindex)] = bytes(data)
            self._drive_transition(_U16.unpack(data[:2].ljust(2, b"\x00"))[0])
            return
        if (index, subindex) == (0x6060, 0):
            self._store[(index, subindex)] = bytes(data)
            self._store[(0x6061, 0)] = bytes(data[:1].ljust(1, b"\x00"))
            return
        if (index, subindex) == (0x1010, 1):
            if _U32.unpack(data[:4].ljust(4, b"\x00"))[0] != STORE_SIGNATURE:
                raise SdoAbortError(ABORT_VALUE_RANGE, index, subindex)
            return
        if (index, subindex) == (0x1011, 1):
            if _U32.unpack(data[:4].ljust(4, b"\x00"))[0] != RESTORE_SIGNATURE:
                raise SdoAbortError(ABORT_VALUE_RANGE, index, subindex)
            return
        self._store[(index, subindex)] = bytes(data)

    # ---- NMT slave ----
    def _on_nmt(self, frame: CanFrame) -> None:
        if frame.is_remote or frame.is_error or frame.is_extended or len(frame.data) != 2:
            return
        command, target = frame.data[0], frame.data[1]
        if target not in (0, self.node_id):
            return
        if command == NMT_START:
            self._set_state(STATE_OPERATIONAL)
        elif command == NMT_STOP:
            self._set_state(STATE_STOPPED)
        elif command == NMT_PREOP:
            self._set_state(STATE_PREOP)
        elif command in (NMT_RESET_NODE, NMT_RESET_COMM):
            self._init_store()
            self._set_state(STATE_PREOP, boot=True)

    def _state(self) -> int:
        raw = self._store.get((0x5110, 0), bytes((STATE_PREOP,)))
        return raw[0] if raw else STATE_PREOP

    def _set_state(self, state: int, boot: bool = False) -> None:
        if state != STATE_OPERATIONAL:
            self._pending_rpdos.clear()
        self._store[(0x5110, 0)] = bytes((state,))
        self._hb_state = state
        if boot and self.network is not None:
            # Fire-and-forget bootup heartbeat; the periodic loop follows.
            async def _boot() -> None:
                try:
                    await self.network.send(  # type: ignore[union-attr]
                        CanFrame(HEARTBEAT_BASE + self.node_id, b"\x00"))
                except Exception:
                    pass
            asyncio.get_running_loop().create_task(_boot())

    async def _heartbeat_loop(self) -> None:
        while True:
            try:
                period = int.from_bytes(
                    self._store.get((0x1017, 0), b"\x00\x00")[:2], "little")
            except Exception:
                period = 0
            if period <= 0:
                await asyncio.sleep(0.01)
                continue
            await asyncio.sleep(period / 1000.0)
            # A disable/change while sleeping takes effect before transmission.
            current = int.from_bytes(self._store.get((0x1017, 0), b"\x00\x00"), "little")
            if current != period:
                continue
            if self.network is None:
                continue
            try:
                await self.network.send(CanFrame(
                    HEARTBEAT_BASE + self.node_id, bytes((self._hb_state,))))
            except Exception:
                return

    # ---- drive ----
    def _drive_transition(self, controlword: int) -> None:
        rising_reset = bool(controlword & 0x0080 and not self._controlword & 0x0080)
        self._controlword = controlword
        if self._statusword == ST_FAULT:
            if rising_reset:
                self._statusword = ST_SWITCH_ON_DISABLED
        elif not controlword & 0x0002:
            self._statusword = ST_SWITCH_ON_DISABLED
        elif not controlword & 0x0004:
            if self._statusword in (ST_OP_ENABLED, ST_QUICK_STOP):
                self._statusword = ST_QUICK_STOP
        elif (controlword & 0x0087) == 0x0006:
            self._statusword = ST_READY
        elif (controlword & 0x008F) == 0x0007:
            if self._statusword in (ST_READY, ST_SWITCHED_ON, ST_OP_ENABLED):
                self._statusword = ST_SWITCHED_ON
        elif (controlword & 0x008F) == 0x000F:
            if self._statusword in (ST_READY, ST_SWITCHED_ON, ST_OP_ENABLED, ST_QUICK_STOP):
                self._statusword = ST_OP_ENABLED
        self._store[(0x6041, 0)] = _U16.pack(self._statusword)

    def latch_fault(self, fault_id: int = 0x4981,
                    error_code: int = 0xFF00, error_register: int = 0x81) -> None:
        """Latch a drive fault: statusword -> Fault plus EMCY + 5300h entry."""
        self._statusword = ST_FAULT
        self._store[(0x6041, 0)] = _U16.pack(self._statusword)
        self.inject_fault(fault_id, error_code, error_register)

    # ---- faults / EMCY ----
    def inject_fault(self, fault_id: int, error_code: int = 0xFF00,
                     error_register: int = 0x81, extra: bytes = b"\x00\x00\x00") -> None:
        if fault_id not in self._faults:
            self._faults.append(fault_id)
        cob_id = int.from_bytes(self._store[(0x1014, 0)], "little")
        if not cob_id_valid(cob_id):
            return
        frame = encode_emcy(self.node_id, error_code, error_register, fault_id, extra,
                            cob_id=cob_id)
        self.sent_emcy.append((fault_id, bytes(frame.data)))

        async def _send() -> None:
            try:
                await self.network.send(frame)  # type: ignore[union-attr]
            except Exception:
                pass

        try:
            asyncio.get_running_loop().create_task(_send())
        except RuntimeError:
            pass

    # ---- PDOs ----
    def _mapping(self, is_tpdo: bool, pdo_number: int):
        index = (0x1A00 if is_tpdo else 0x1600) + pdo_number - 1
        count = int.from_bytes(self._store.get((index, 0), b"\x00"), "little")
        return [decode_mapping_word(int.from_bytes(self._store[(index, sub)], "little"))
                for sub in range(1, count + 1)]

    def _apply_rpdo(self, number: int, data: bytes) -> None:
        try:
            cob = int.from_bytes(self._store[(0x1400 + number - 1, 1)], "little")
            if not cob_id_valid(cob) or self._state() != STATE_OPERATIONAL:
                return
            decoded = decode_pdo(data, self._mapping(False, number), self.od)
            updates = []
            for value in decoded:
                if is_dummy_mapping(self.od, value.index, value.subindex, value.bits):
                    continue
                var = self.od.variable((value.index, value.subindex))
                if not var.writable or not var.pdo_mappable:
                    return
                var.check_range(value.raw)
                updates.append((value.index, value.subindex, var.encode_raw(value.raw)))
            for index, subindex, raw in updates:
                # RPDO uses the same register side effects as SDO in this model.
                self._sdo_write(index, subindex, raw)
        except (SevconError, ValueError, KeyError):
            return

    def _on_pdo_frame(self, frame: CanFrame) -> None:
        if frame.is_remote or frame.is_error or self._state() != STATE_OPERATIONAL:
            return
        sync_id = int.from_bytes(self._store.get((0x1005, 0), _U32.pack(SYNC_COB_ID)), "little")
        sync_extended = bool(sync_id & COB_ID_EXTENDED_BIT)
        if frame.arbitration_id == cob_id_can_id(sync_id) and frame.is_extended == sync_extended:
            if len(frame.data) > 1 or frame.data and not 1 <= frame.data[0] <= 240:
                return
            pending, self._pending_rpdos = self._pending_rpdos, {}
            for number, payload in pending.items():
                self._apply_rpdo(number, payload)
            return
        for number in range(1, 6):
            index = 0x1400 + number - 1
            cob = int.from_bytes(self._store.get((index, 1), _U32.pack(0x80000000)), "little")
            if not cob_id_valid(cob) or frame.is_extended != bool(cob & COB_ID_EXTENDED_BIT):
                continue
            if frame.arbitration_id != cob_id_can_id(cob):
                continue
            transmission = int.from_bytes(self._store.get((index, 2), b"\xFF"), "little")
            if transmission <= 240:
                self._pending_rpdos[number] = frame.data
            elif transmission in (254, 255):
                self._apply_rpdo(number, frame.data)

    async def send_tpdo(self, pdo_number: int, *, force: bool = False) -> CanFrame:
        """Send live mapped values; force permits test injection while disabled.

        This simulator does not model periodic/inhibit timers for TPDOs.
        """
        if self.network is None:
            raise RuntimeError("simulator not attached")
        if not 1 <= pdo_number <= 5:
            raise ValueError("PDO number must be 1..5")
        cob = int.from_bytes(self._store[(0x1800 + pdo_number - 1, 1)], "little")
        if not force and (not cob_id_valid(cob) or self._state() != STATE_OPERATIONAL):
            raise ValueError("TPDO is disabled or node is not operational")
        mapping = self._mapping(True, pdo_number)
        values = []
        for index, subindex, _length in mapping:
            raw = self._store.get((index, subindex), b"\x00")
            var = self.od.variable((index, subindex))
            values.append((index, subindex, var.decode_raw(raw)))
        payload = encode_pdo(values, mapping)
        frame = CanFrame(cob_id_can_id(cob), payload,
                         is_extended=bool(cob & COB_ID_EXTENDED_BIT))
        await self.network.send(frame)
        return frame


def _abort(code: int, index: int, subindex: int):
    from .exceptions import SdoAbortError
    raise SdoAbortError(code, index, subindex)


def create_pair(node_id: int = 0x20, **kwargs) -> Tuple["AsyncSevconGen4", SimulatedGen4]:  # noqa: F821
    """Create a ``(device, simulator)`` pair sharing one virtual bus.

    Both are unstarted; use ``async with dev, sim:``.
    """
    from .device import AsyncSevconGen4
    bus = VirtualBus()
    sim = SimulatedGen4(node_id, **kwargs)
    sim.attach(Network(bus.endpoint()))
    dev = AsyncSevconGen4(Network(bus.endpoint()), node_id, od=sim.od, owns_network=True)
    return dev, sim
