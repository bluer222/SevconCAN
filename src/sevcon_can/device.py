"""Async Gen4 client: typed SDO access + NMT + heartbeat + EMCY + PDO + drive.

Usage::

    from sevcon_can import AsyncSevconGen4

    async with AsyncSevconGen4.from_socketcan("can0", node_id=0x20) as dev:
        await dev.login(0x4BDF)          # engineering level (OVMS password)
        print(await dev.read("Device Measurements.Battery Voltage"))
        await dev.store()
        async for emcy in dev.emcy_notifications():
            print(emcy)

Concurrency mirrors ``ve_direct.AsyncVEDirect``: SDO transfers are
serialized inside :class:`SdoClient`; heartbeat/EMCY/TPDO frames arriving
at any time are queued for their consumers and never block SDO replies.

The client enforces the controller's access rules *before* transmitting
(read of a write-only object, write of a read-only object, value outside
``LowLimit``/``HighLimit``, insufficient Sevcon login level, or a
pre-operational-only object written while operational raise
:class:`AccessError` without touching the bus).
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any, AsyncIterator, Dict, List, Optional, Tuple

from . import datatypes as dt
from . import drive as _drive
from . import emcy as _emcy
from . import nmt as _nmt
from . import pdo as _pdo
from .drive import describe_status
from .emcy import EmcyConsumer, EmcyMessage
from .exceptions import (
    AccessError,
    ObjectDictionaryError,
    ProtocolError,
    SdoAbortError,
    StateError,
    TimeoutError,
)
from .network import Network
from .nmt import HeartbeatMonitor, NmtMaster
from .od import ObjectDictionary, Variable, gen4_od
from .sdo import ABORT_GENERAL, SdoClient
from .tables import GEN4_ABORT_REASONS
from .transport import CanFrame, SocketCanTransport, VirtualBus

#: The controller this package targets: 36/48 V nominal, Size 2 frame,
#: 275 A 2-minute / 110 A 60-minute rating, Sin/Cos encoder feedback.
#: Ratings and voltage window from the manual Tables 2 and 4
#: (``tables.GEN4_RATINGS`` / ``tables.GEN4_VOLTAGE_RANGES``).
TARGET_CONTROLLER = {
    "model": "634A42203",
    "voltage": "36/48V",
    "size": 2,
    "short_term_a": 275,
    "continuous_a": 110,
    "feedback": "Sin/Cos",
    "conventional_v": (25.2, 57.6),
    "limit_v": (19.3, 69.6),
    "overvoltage_v": 79.2,
}

#: CiA "save"/"load" signatures written to 1010h:1 / 1011h:1.
STORE_SIGNATURE = 0x65766173  # "save"
RESTORE_SIGNATURE = 0x64616F6C  # "load"

#: Password the OVMS Renault-Twizy client uses for the engineering level;
#: accepted by :class:`Simulator` and known-good against real Twizy units.
ENGINEERING_PASSWORD = 0x4BDF

GENERAL_ABORT_WITH_REASON = 0x08000000


@dataclass
class Reading:
    """One typed object read."""

    variable: Variable
    raw: Any
    physical: Any
    label: Any = None
    unit: Optional[str] = None

    def __repr__(self) -> str:
        text = f"{self.variable.path} raw={self.raw!r}"
        if self.physical != self.raw:
            text += f" physical={self.physical!r}"
            if self.unit:
                text += f" {self.unit}"
        if isinstance(self.label, str):
            text += f" ({self.label!r})"
        elif isinstance(self.label, dict):
            text += f" {self.label!r}"
        return f"Reading({text})"


class AsyncSevconGen4:
    """High-level async client for one Gen4 node."""

    def __init__(self, network: Network, node_id: int,
                 od: Optional[ObjectDictionary] = None, *,
                 sdo_timeout: float = 1.0,
                 sdo_rx_cob_id: Optional[int] = None,
                 sdo_tx_cob_id: Optional[int] = None,
                 owns_network: bool = False) -> None:
        if not 1 <= node_id <= 127:
            raise ValueError("node id must be 1..127")
        self.network = network
        self.node_id = node_id
        self.od = od if od is not None else gen4_od()
        self._owns_network = owns_network
        self.sdo = SdoClient(network, node_id, timeout=sdo_timeout,
                             rx_cob_id=sdo_rx_cob_id, tx_cob_id=sdo_tx_cob_id)
        self.nmt = NmtMaster(network)
        self.heartbeat = self.nmt.heartbeat
        self.heartbeat.watch(node_id)
        self.emcy = EmcyConsumer(network, node_id)
        self._pdos: List[_pdo.PdoConsumer] = []
        self._closed = False
        self._access_level: Optional[int] = None
        self._nmt_state: Optional[int] = None
        self._hb_sub = network.subscribe(
            _nmt.HEARTBEAT_BASE + node_id, self._on_heartbeat)

    # ---- construction ----
    @classmethod
    def from_socketcan(cls, channel: str, node_id: int, *,
                       od: Optional[ObjectDictionary] = None,
                       sdo_timeout: float = 1.0,
                       sdo_rx_cob_id: Optional[int] = None,
                       sdo_tx_cob_id: Optional[int] = None,
                       error_frames: bool = False) -> "AsyncSevconGen4":
        """Build a client on a Linux SocketCAN interface.

        Bring the interface up first, e.g. ``sudo ip link set can0 type
        can bitrate 1000000`` (Gen4 defaults to 1 Mbit/s, manual Table 5;
        the configured rate lives in object ``5900h``).
        """
        transport = SocketCanTransport(channel, error_frames=error_frames)
        return cls(Network(transport), node_id, od,
                   sdo_timeout=sdo_timeout, sdo_rx_cob_id=sdo_rx_cob_id,
                   sdo_tx_cob_id=sdo_tx_cob_id, owns_network=True)

    @classmethod
    def from_transport(cls, transport, node_id: int, *,
                       od: Optional[ObjectDictionary] = None,
                       sdo_timeout: float = 1.0,
                       sdo_rx_cob_id: Optional[int] = None,
                       sdo_tx_cob_id: Optional[int] = None) -> "AsyncSevconGen4":
        return cls(Network(transport), node_id, od,
                   sdo_timeout=sdo_timeout, sdo_rx_cob_id=sdo_rx_cob_id,
                   sdo_tx_cob_id=sdo_tx_cob_id, owns_network=True)

    @classmethod
    def from_gs_usb(cls, channel: str = "can0", node_id: int = 0x20, *,
                    od: Optional[ObjectDictionary] = None,
                    sdo_timeout: float = 1.0,
                    sdo_rx_cob_id: Optional[int] = None,
                    sdo_tx_cob_id: Optional[int] = None,
                    error_frames: bool = False) -> "AsyncSevconGen4":
        """Build a client on a CANable-class adapter (Jhoinrch RH-02 and
        similar) running Candlelight firmware.

        Candlelight presents the adapter through the kernel ``gs_usb``
        driver as a native SocketCAN netdev, so this is
        :meth:`from_socketcan` with adapter-specific bring-up documented.
        The Gen4 defaults to 1 Mbit/s (object ``5900h``); match it::

            sudo modprobe gs_usb
            sudo ip link set can0 type can bitrate 1000000 restart-ms 100
            sudo ip link set can0 up

        See ``docs/ADAPTERS.md`` for identifying the adapter, the SLCAN
        firmware alternative, termination and wiring.
        """
        return cls.from_socketcan(channel, node_id, od=od,
                                  sdo_timeout=sdo_timeout,
                                  sdo_rx_cob_id=sdo_rx_cob_id,
                                  sdo_tx_cob_id=sdo_tx_cob_id,
                                  error_frames=error_frames)

    @classmethod
    def from_python_can(cls, node_id: int, *,
                        od: Optional[ObjectDictionary] = None,
                        sdo_timeout: float = 1.0,
                        sdo_rx_cob_id: Optional[int] = None,
                        sdo_tx_cob_id: Optional[int] = None,
                        **bus_kwargs: Any) -> "AsyncSevconGen4":
        """Build a client on any ``python-can`` backend.

        Examples (RH-02 flashed with SLCAN firmware, or any PCAN/Kvaser/
        Vector interface)::

            AsyncSevconGen4.from_python_can(0x20, interface="slcan",
                                            channel="/dev/ttyACM0",
                                            bitrate=1000000)
            AsyncSevconGen4.from_python_can(0x20, interface="pcan",
                                            channel="PCAN_USBBUS1",
                                            bitrate=1000000)

        Requires ``pip install 'sevcon-can[python-can]'``.
        """
        from .transport import PythonCanTransport
        return cls.from_transport(PythonCanTransport(**bus_kwargs), node_id,
                                  od=od, sdo_timeout=sdo_timeout,
                                  sdo_rx_cob_id=sdo_rx_cob_id,
                                  sdo_tx_cob_id=sdo_tx_cob_id)

    async def _start(self) -> None:
        await self.network.start()

    async def start(self) -> "AsyncSevconGen4":
        """Start the network reader (also done by ``__aenter__``)."""
        await self._start()
        return self

    async def __aenter__(self) -> "AsyncSevconGen4":
        await self._start()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._hb_sub.cancel()
        self.sdo.close()
        self.emcy.close()
        self.heartbeat.close()
        for pdo in self._pdos:
            pdo.close()
        if self._owns_network:
            await self.network.close()

    def _check_open(self) -> None:
        if self._closed:
            from .exceptions import TransportError
            raise TransportError("device is closed")

    # ---- object lookup ----
    def variable(self, key, subindex: Optional[int] = None) -> Variable:
        return self.od.variable(key, subindex)

    def search(self, text: str) -> List[Variable]:
        return self.od.search(text)

    # ---- raw SDO ----
    async def read_raw(self, index: int, subindex: int) -> bytes:
        """Read raw SDO bytes (no access checks beyond existence)."""
        self._check_open()
        try:
            return await self.sdo.upload(index, subindex)
        except SdoAbortError as e:
            raise await self._with_gen4_reason(e)

    async def write_raw(self, index: int, subindex: int, data: bytes) -> None:
        self._check_open()
        try:
            await self.sdo.download(index, subindex, bytes(data))
        except SdoAbortError as e:
            raise await self._with_gen4_reason(e)

    async def _with_gen4_reason(self, error: SdoAbortError) -> SdoAbortError:
        """Attach the Gen4's detailed reason (``5310h``) to a general abort.

        The controller reports ``0x08000000`` for most application
        failures and stores the specific cause (manual Table 33) in
        ``5310h``; best effort -- the original error is returned if
        ``5310h`` itself cannot be read.
        """
        if error.code != ABORT_GENERAL or error.index == 0x5310:
            return error
        try:
            raw = await self.sdo.upload(0x5310, 0)
            reason = int.from_bytes(raw[:2], "little")
            return SdoAbortError(error.code, error.index, error.subindex, reason)
        except Exception:
            return error

    # ---- typed access ----
    async def read(self, key, subindex: Optional[int] = None) -> Reading:
        """Read an object by index, ``(index, sub)`` or name path.

        Returns a :class:`Reading` with ``raw`` (wire value), ``physical``
        (SCALING applied), ``label`` (enum/bitfield interpretation) and
        ``unit``. Write-only objects raise :class:`AccessError` locally.
        """
        var = self.variable(key, subindex)
        if not var.readable:
            raise AccessError(f"{var.path} (0x{var.index:04X}:{var.subindex:02X}) is write-only")
        data = await self.read_raw(var.index, var.subindex)
        raw = var.decode_raw(data)
        physical = var.to_physical(raw)
        return Reading(var, raw, physical, var.describe(raw), var.units)

    async def read_physical(self, key, subindex: Optional[int] = None) -> Any:
        return (await self.read(key, subindex)).physical

    async def write(self, key, subindex, value) -> Reading:
        """Write by index/name; ``value`` may be raw, physical, or a label.

        Strings are resolved against the object's enum labels first (so
        ``write(0x6060, 0, "torque")`` works); numeric values pass through
        inverse SCALING and range checks. Returns the post-write
        :class:`Reading` interpretation (no read-back is performed).
        """
        var = self.variable(key, subindex)
        raw = self._coerce_write(var, value)
        await self._check_writable(var, raw)
        await self.write_raw(var.index, var.subindex, var.encode_raw(raw))
        return Reading(var, raw, var.to_physical(raw), var.describe(raw), var.units)

    def _coerce_write(self, var: Variable, value: Any) -> Any:
        if isinstance(value, str):
            text = value.strip()
            if var.labels:
                try:
                    return var.label_to_raw(text)
                except ValueError:
                    pass
            if var.data_type in dt.STRING_TYPES or var.data_type == dt.DOMAIN:
                return text
            # Numeric string in physical units ("48.5") or raw ("0x10").
            try:
                number: Any = float(text) if "." in text else int(text, 0)
            except ValueError:
                raise AccessError(
                    f"{text!r} is not a label of {var.path}: "
                    f"{sorted(var.labels.values())}") from None
            value = number
        if isinstance(value, bool):
            raw = int(value)
        elif isinstance(value, (int, float)) and var.scaling is not None \
                and var.data_type not in dt.FLOAT_TYPES:
            raw = var.from_physical(value)
        elif isinstance(value, float) and value.is_integer() and var.data_type in dt.INTEGER_TYPES:
            raw = int(value)
        else:
            raw = value
        if isinstance(raw, int) and not isinstance(raw, bool):
            var.check_range(raw)
        return raw

    async def _check_writable(self, var: Variable, raw: Any) -> None:
        if not var.writable:
            raise AccessError(f"{var.path} (0x{var.index:04X}:{var.subindex:02X}) is read-only")
        if var.access_level is not None:
            level = await self.access_level()
            if level is None or level < var.access_level:
                raise AccessError(
                    f"{var.path} needs login level {var.access_level} "
                    f"(current: {level}); call login() first")
        if var.preop_only and await self.nmt_state() == _nmt.STATE_OPERATIONAL:
            raise AccessError(
                f"{var.path} is writable only in pre-operational; "
                "call goto_preop() first")

    # ---- login ----
    async def access_level(self, cached: bool = True) -> Optional[int]:
        """Current Sevcon login level (``5000h:1``), cached after login."""
        if cached and self._access_level is not None:
            return self._access_level
        try:
            level = int(await self.read_physical(0x5000, 1))
        except Exception:
            return self._access_level
        self._access_level = level
        return level

    async def login(self, password: int, user_id: int = 0) -> int:
        """Log in (``5000h:3`` user id, ``5000h:2`` password) and return the
        resulting level (``5000h:1``: 0 logged out .. 5 SEVCON).

        Raises :class:`StateError` if the controller does not grant a
        nonzero level.
        """
        self._check_open()
        var_pwd = self.variable((0x5000, 2))
        var_uid = self.variable((0x5000, 3))
        await self.write_raw(var_uid.index, var_uid.subindex, var_uid.encode_raw(user_id))
        try:
            await self.write_raw(var_pwd.index, var_pwd.subindex, var_pwd.encode_raw(password))
        except SdoAbortError as e:
            raise await self._with_gen4_reason(e)
        self._access_level = None
        level = await self.access_level(cached=False)
        if not level:
            raise StateError("login failed: controller stayed logged out (level 0)")
        return level

    async def logout(self) -> int:
        """Log out (user id and password 0) and return the level (expect 0)."""
        self._check_open()
        var_pwd = self.variable((0x5000, 2))
        var_uid = self.variable((0x5000, 3))
        await self.write_raw(var_uid.index, var_uid.subindex, var_uid.encode_raw(0))
        await self.write_raw(var_pwd.index, var_pwd.subindex, var_pwd.encode_raw(0))
        self._access_level = None
        return await self.access_level(cached=False) or 0

    # ---- NMT / state ----
    def _on_heartbeat(self, frame: CanFrame) -> None:
        try:
            _, state = _nmt.decode_heartbeat(frame)
        except ValueError:
            return
        self._nmt_state = state

    async def nmt_state(self, cached: bool = True) -> Optional[int]:
        """NMT state: live heartbeat value if seen, else ``5110h``."""
        if cached and self._nmt_state is not None:
            return self._nmt_state
        try:
            state = int(await self.read_physical(0x5110, 0))
        except Exception:
            if not cached:
                raise
            return self._nmt_state
        self._nmt_state = state
        return state

    async def nmt_command(self, command: int, node_id: Optional[int] = None) -> None:
        await self.nmt.command(command, self.node_id if node_id is None else node_id)

    async def goto_preop(self, verify: bool = True) -> Optional[int]:
        """Force pre-operational via ``2800h = 1`` (manual section 7.3)."""
        await self.write(0x2800, 0, True)
        if verify:
            return await self.wait_nmt_state(_nmt.STATE_PREOP)
        self._nmt_state = None
        return None

    async def goto_operational(self, verify: bool = True) -> Optional[int]:
        """Restore operational via ``2800h = 0``."""
        await self.write(0x2800, 0, False)
        if verify:
            return await self.wait_nmt_state(_nmt.STATE_OPERATIONAL)
        self._nmt_state = None
        return None

    async def wait_nmt_state(self, state: int, timeout: float = 5.0) -> int:
        """Wait for an NMT state via heartbeat, falling back to polling
        ``5110h`` when heartbeats are not configured (``1017h = 0``)."""
        self._check_open()
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        deadline = time.monotonic() + timeout
        current = self._nmt_state
        # Heartbeats update the cache concurrently. Poll throughout the same
        # deadline, rather than spending it all waiting for a disabled producer.
        while time.monotonic() < deadline:
            if self._nmt_state == state:
                return state
            remaining = deadline - time.monotonic()
            try:
                current = await asyncio.wait_for(
                    self.nmt_state(cached=False), remaining)
            except asyncio.TimeoutError:
                break
            if current == state:
                return state
            await asyncio.sleep(min(0.05, max(0, deadline - time.monotonic())))
        raise StateError(
            f"node {self.node_id} did not reach {_nmt.state_name(state)} "
            f"within {timeout:g} s (now {_nmt.state_name(current if current is not None else -1)})")

    # ---- store / restore ----
    async def store(self) -> None:
        """Persist parameters to EEPROM (``1010h:1 = "save"``)."""
        await self.write_raw(0x1010, 1, STORE_SIGNATURE.to_bytes(4, "little"))

    async def restore(self) -> None:
        """Restore EEPROM defaults (``1011h:1 = "load"``)."""
        await self.write_raw(0x1011, 1, RESTORE_SIGNATURE.to_bytes(4, "little"))

    # ---- EMCY ----
    def emcy_notifications(self) -> AsyncIterator[EmcyMessage]:
        return self.emcy.notifications()

    # ---- PDO ----
    def _pdo_indices(self, is_tpdo: bool, pdo_number: int) -> Tuple[int, int]:
        if not isinstance(pdo_number, int) or not 1 <= pdo_number <= 5:
            raise ValueError("Gen4 PDO number must be 1..5")
        offset = pdo_number - 1
        return (0x1800 if is_tpdo else 0x1400) + offset, (0x1A00 if is_tpdo else 0x1600) + offset

    async def read_pdo_configuration(self, is_tpdo: bool, pdo_number: int) -> Dict[str, Any]:
        """Read the live communication record and mapping, including validity.

        COB-ID is the complete 32-bit parameter, retaining disabled and
        extended-frame flags. Inhibit time is returned in raw 100 us units.
        """
        comm, _ = self._pdo_indices(is_tpdo, pdo_number)
        cob_id = int((await self.read(comm, 1)).raw)
        result = {"cob_id": cob_id, "enabled": _pdo.cob_id_valid(cob_id),
                  "transmission_type": int((await self.read(comm, 2)).raw),
                  "mapping": await self.read_pdo_mapping(is_tpdo, pdo_number)}
        if is_tpdo:
            result["inhibit_time"] = int((await self.read(comm, 3)).raw)
            result["event_timer"] = int((await self.read(comm, 5)).raw)
        return result

    async def subscribe_tpdo_live(self, pdo_number: int) -> _pdo.PdoConsumer:
        """Subscribe using the controller's live COB-ID and mapping."""
        config = await self.read_pdo_configuration(True, pdo_number)
        if not config["enabled"]:
            raise StateError(f"TPDO {pdo_number} is disabled")
        return self.subscribe_tpdo(pdo_number, config["cob_id"], config["mapping"])

    async def send_rpdo(self, pdo_number: int, values: list, *,
                        cob_id: Optional[int] = None, mapping: Optional[list] = None) -> None:
        """Send raw (index, subindex, value) entries using the live RPDO config.

        The RPDO's transmission type and controller state determine when
        it applies the values. Synchronous RPDOs require a later SYNC.
        """
        self._check_open()
        self._pdo_indices(False, pdo_number)
        if cob_id is None or mapping is None:
            config = await self.read_pdo_configuration(False, pdo_number)
            if cob_id is None:
                cob_id = config["cob_id"]
            if mapping is None:
                mapping = config["mapping"]
        if not _pdo.cob_id_valid(cob_id):
            raise StateError(f"RPDO {pdo_number} is disabled")
        can_id = _pdo.cob_id_can_id(cob_id)
        payload = _pdo.encode_pdo(values, mapping)
        await self.network.send(CanFrame(can_id, payload,
            is_extended=bool(cob_id & _pdo.COB_ID_EXTENDED_BIT)))

    async def configure_pdo(self, is_tpdo: bool, pdo_number: int, mapping: list, *,
                            cob_id: Optional[int] = None,
                            transmission_type: Optional[int] = None,
                            enabled: bool = True) -> Dict[str, Any]:
        """Remap a PDO in pre-operational using the CiA 301 disable/clear order.

        All metadata, lengths and access permissions are checked before
        writing. A failed remapping write leaves the PDO disabled; parameters are
        not persisted until store() is called explicitly.
        """
        comm, map_index = self._pdo_indices(is_tpdo, pdo_number)
        if await self.nmt_state(cached=False) != _nmt.STATE_PREOP:
            raise StateError("PDO configuration requires NMT pre-operational")
        _pdo.validate_mapping(mapping)
        if not mapping and enabled:
            raise ValueError("an enabled PDO must contain at least one mapping entry")
        words = []
        for index, sub, bits in mapping:
            var = self.variable(index, sub)
            if not var.pdo_mappable or bits not in (dt.bit_length(var.data_type),):
                raise ValueError(f"{var.path} is not mappable with {bits} bits")
            if is_tpdo and not var.readable or not is_tpdo and not var.writable:
                raise AccessError(f"{var.path} has the wrong access for this PDO")
            words.append(_pdo.encode_mapping_word(index, sub, bits))
        current = int((await self.read(comm, 1)).raw)
        selected = current if cob_id is None else cob_id
        _pdo.cob_id_can_id(selected)  # validate identifier/flags
        changes = [(comm, 1, current | _pdo.COB_ID_VALID_BIT), (map_index, 0, 0)]
        changes.extend((map_index, sub, word) for sub, word in enumerate(words, 1))
        changes.append((map_index, 0, len(words)))
        if transmission_type is not None:
            if transmission_type not in _pdo.TRANSMISSION_TYPES:
                raise ValueError("unsupported PDO transmission type")
            changes.append((comm, 2, transmission_type))
        final = selected & ~_pdo.COB_ID_VALID_BIT if enabled else selected | _pdo.COB_ID_VALID_BIT
        changes.append((comm, 1, final | _pdo.COB_ID_VALID_BIT))
        changes.append((comm, 1, final))
        for index, sub, raw in changes:
            var = self.variable(index, sub)
            var.check_range(raw)
            await self._check_writable(var, raw)
        # Write the requested COB-ID while still disabled before enabling it.
        for index, sub, raw in changes:
            await self.write_raw(index, sub, self.variable(index, sub).encode_raw(raw))
        return await self.read_pdo_configuration(is_tpdo, pdo_number)

    async def read_pdo_mapping(self, is_tpdo: bool, pdo_number: int) -> list:
        """Read a PDO mapping from the controller (mapping record words)."""
        _, index = self._pdo_indices(is_tpdo, pdo_number)
        count = int(await self.read_physical(index, 0))
        if not 0 <= count <= 8:
            raise ProtocolError(f"invalid PDO mapping count {count}")
        mapping = []
        for sub in range(1, count + 1):
            word = int(await self.read_physical(index, sub))
            mapping.append(_pdo.decode_mapping_word(word))
        return mapping

    def subscribe_tpdo(self, pdo_number: int, cob_id: Optional[int] = None,
                       mapping: Optional[list] = None) -> _pdo.PdoConsumer:
        """Subscribe to a TPDO; mapping defaults to the EDS mapping record.

        Prefer :meth:`read_pdo_mapping` first when the controller mapping
        may differ from the EDS.
        """
        self._check_open()
        self._pdo_indices(True, pdo_number)
        if cob_id is None:
            cob_id = _pdo.default_cob_id(True, pdo_number - 1, self.node_id)
        if mapping is None:
            mapping = _pdo.read_mapping(self.od, True, pdo_number - 1)
        consumer = _pdo.PdoConsumer(self.network, cob_id, mapping, self.od)
        self._pdos.append(consumer)
        return consumer

    async def send_sync(self, counter: Optional[int] = None, *,
                        cob_id: Optional[int] = None) -> None:
        """Transmit SYNC using live 1005h or an explicit full COB-ID parameter.

        Payload is empty or counter 1..240. Bit 30 identifies the device's
        producer role and does not change the frame sent by this client.
        """
        self._check_open()
        if counter is None:
            payload = b""
        elif not 1 <= counter <= 240:
            raise ValueError("SYNC counter must be 1..240")
        else:
            payload = bytes((counter,))
        if cob_id is None:
            cob_id = int((await self.read(0x1005, 0)).raw)
        if cob_id & _pdo.COB_ID_VALID_BIT:
            raise ValueError("SYNC COB-ID bit 31 is reserved and must be clear")
        await self.network.send(CanFrame(_pdo.cob_id_can_id(cob_id), payload,
            is_extended=bool(cob_id & _pdo.COB_ID_EXTENDED_BIT)))

    # ---- drive (DSP402) ----
    async def status(self) -> _drive.StatusInfo:
        return describe_status(int(await self.read_physical(0x6041, 0)))

    async def drive_command(self, name: str) -> _drive.StatusInfo:
        """Write a 402 device-control command (e.g. ``"Enable operation"``,
        ``"Fault reset"``) to ``6040h`` and return the fresh status."""
        alias = _drive.COMMAND_ALIASES.get(name.strip().lower(), name)
        word = _drive.command_bits(alias)
        if word & _drive.CW_FAULT_RESET:
            # A previous reset can leave bit 7 set. A fresh low/high edge
            # is necessary even when this method is called repeatedly.
            current = int(await self.read_physical(0x6040, 0))
            low = current & ~_drive.CW_FAULT_RESET
            await self.write(0x6040, 0, low)
            try:
                await self.write(0x6040, 0, low | _drive.CW_FAULT_RESET)
                return await self.status()
            finally:
                await self.write(0x6040, 0, low)
        await self.write(0x6040, 0, word)
        return await self.status()

    async def set_operation_mode(self, mode: str | int) -> str:
        """Select ``speed``/``torque``/``voltage`` (``6060h``) and return
        the controller's mode display (``6061h``)."""
        await self.write(0x6060, 0, _drive.operation_mode_to_raw(mode))
        return _drive.operation_mode_from_raw(int(await self.read_physical(0x6061, 0)))

    async def enable_drive(self, timeout: float = 5.0) -> _drive.StatusInfo:
        """Run Shutdown -> Switch on -> Enable operation, waiting for the
        statusword to move after each step. Raises :class:`StateError` if
        the drive does not reach Operation enabled (e.g. a latched fault
        needs ``fault_reset`` first)."""
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        deadline = time.monotonic() + timeout
        transitions = {
            "Switch on disabled": "Shutdown",
            "Ready to switch on": "Switch on",
            "Switched on": "Enable operation",
            "Quick stop active": "Enable operation",
        }
        last_state = None
        info = None
        while time.monotonic() < deadline:
            try:
                info = await asyncio.wait_for(self.status(), deadline - time.monotonic())
                if info.state == "Operation enabled":
                    return info
                if info.fault:
                    raise StateError(
                        f"drive is in {info.state} (statusword 0x{info.raw:04X}); "
                        "call fault_reset() first")
                command = transitions.get(info.state)
                if command is not None and info.state != last_state:
                    await asyncio.wait_for(
                        self.drive_command(command), max(0.001, deadline - time.monotonic()))
                    last_state = info.state
            except asyncio.TimeoutError:
                break
            await asyncio.sleep(min(0.02, max(0, deadline - time.monotonic())))
        detail = f"0x{info.raw:04X} {info.state!r}" if info else "unavailable"
        raise StateError(
            f"drive did not reach Operation enabled within {timeout:g} s "
            f"(statusword {detail})")

    async def fault_reset(self) -> _drive.StatusInfo:
        """Generate the bit 7 rising edge required by CiA 402, then clear it."""
        return await self.drive_command("Fault reset")

    # ---- measurements / faults ----
    async def measurements(self) -> Dict[str, Any]:
        """Core telemetry: battery/capacitor V+A, heatsink temperature."""
        out: Dict[str, Any] = {}
        for index, sub in ((0x5100, 1), (0x5100, 2), (0x5100, 3), (0x5100, 4)):
            try:
                reading = await self.read(index, sub)
                out[reading.variable.name] = reading.physical
            except Exception:
                continue
        return out

    async def active_faults(self) -> list:
        from . import faults as _faults
        return await _faults.read_active_faults(self)

    # ---- CANopen housekeeping ----
    async def producer_heartbeat(self, period_ms: int) -> None:
        """Set our... the controller's heartbeat production rate (``1017h``,
        ms; 0 disables)."""
        await self.write(0x1017, 0, period_ms)

    async def read_identity(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {}
        for sub, name in ((1, "vendor"), (2, "product_code"), (3, "revision"), (4, "serial")):
            try:
                out[name] = (await self.read(0x1018, sub)).raw
            except Exception:
                continue
        return out

    async def validate_identity(self) -> Dict[str, Any]:
        """Compare the live 1018h identity with EDS DeviceInfo metadata.

        A match supports dictionary selection, but cannot prove that the
        controller is model 634A42203 or that all optional objects exist.
        The bundled EDS has inconsistent header/1018h defaults, so defaults
        are reported separately and are never used to override the header.
        """
        live = await self.read_identity()
        expected = {"vendor": self.od.vendor_id, "product_code": self.od.product_code,
                    "revision": self.od.revision}
        mismatches = {}
        for name, value in expected.items():
            if value is not None and name in live and live[name] != value:
                mismatches[name] = {"expected": value, "actual": live[name]}
        missing = [name for name in expected if name not in live or expected[name] is None]
        defaults = {}
        for sub, name in ((1, "vendor"), (2, "product_code"), (3, "revision")):
            try:
                defaults[name] = self.variable(0x1018, sub).default(self.node_id)
            except ObjectDictionaryError:
                pass
        return {"identity": live, "expected": expected, "object_defaults": defaults,
                "mismatches": mismatches, "missing": missing,
                "matches": not mismatches and not missing, "dictionary": self.od.source}

    async def encoder_configuration(self) -> Dict[str, Reading]:
        """Read encoder setup and Sin/Cos measurements defined in this EDS.

        Uses 4630h and 6090h without assigning manual subindices that differ
        between firmware versions. Values retain raw, physical and label
        information; a failed read is surfaced to the caller.
        """
        result = {}
        for var in self.od.variables():
            if var.index in (0x4630, 0x6090) and var.subindex and var.readable:
                result[var.path] = await self.read(var.key)
        return result
