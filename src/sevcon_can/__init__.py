"""Async CANopen client for Sevcon Gen4 motor controllers.

The high-level entry point is :class:`AsyncSevconGen4` (``device.py``),
mirroring ``ve_direct.AsyncVEDirect``: typed reads/writes with scaling,
units and enum labels, login, NMT, heartbeat/EMCY/TPDO consumers, the
DSP402 drive state machine, and a simulator that runs without hardware.
"""
from .cyclic import HeartbeatProducer, SyncProducer
from .device import AsyncSevconGen4, Reading, TARGET_CONTROLLER
from .discovery import DictionaryEntry, discover_dictionary, iter_dictionary_entries
from .descriptions import OBJECT_DESCRIPTIONS, describe
from .configuration import (
    ConfigurationChange, ConfigurationRestoreError, ConfigurationSnapshot,
    export_configuration, plan_configuration_restore, restore_configuration,
)
from .drive import (
    command_bits,
    command_names,
    decode_state,
    describe_status,
    operation_mode_from_raw,
    operation_mode_to_raw,
)
from .emcy import EmcyConsumer, EmcyMessage, decode_emcy, encode_emcy
from .exceptions import (
    AccessError,
    ObjectDictionaryError,
    ProtocolError,
    SdoAbortError,
    SevconError,
    StateError,
    TimeoutError,
    TransportError,
)
from .faults import by_level, led_category, lookup as fault_lookup, search as fault_search
from .logs import (
    EventCounter, FifoLogEntry, LogTimestamp, read_event_counters, read_fifo_log,
    read_operational_monitor, reset_diagnostic_log,
)
from .network import FrameQueue, Network, Subscription
from .nmt import (
    HeartbeatMonitor,
    NmtMaster,
    decode_heartbeat,
    nmt_command_frame,
    state_name,
)
from .od import ObjectDictionary, Record, Variable, bundled_eds_path, gen4_od, load_eds
from .pdo import (
    PdoConsumer,
    decode_mapping_word,
    decode_pdo,
    decode_sync_counter,
    encode_mapping_word,
    encode_pdo,
    read_mapping,
)
from .sdo import SdoClient, SdoServer, crc16
from .simulator import SimulatedGen4, create_pair
from .transport import (
    CanFrame,
    PythonCanTransport,
    SocketCanTransport,
    Transport,
    VirtualBus,
    VirtualTransport,
)

__all__ = [
    "ConfigurationChange", "ConfigurationRestoreError", "ConfigurationSnapshot",
    "export_configuration", "plan_configuration_restore", "restore_configuration",
    "AsyncSevconGen4", "Reading", "TARGET_CONTROLLER",
    "DictionaryEntry", "discover_dictionary", "iter_dictionary_entries",
    "describe", "OBJECT_DESCRIPTIONS",
    "ObjectDictionary", "Variable", "Record", "load_eds", "gen4_od", "bundled_eds_path",
    "SdoClient", "SdoServer", "crc16",
    "Network", "Subscription", "FrameQueue",
    "CanFrame", "Transport", "SocketCanTransport", "PythonCanTransport",
    "VirtualBus", "VirtualTransport",
    "HeartbeatProducer", "SyncProducer",
    "NmtMaster", "HeartbeatMonitor", "nmt_command_frame", "decode_heartbeat", "state_name",
    "EmcyConsumer", "EmcyMessage", "decode_emcy", "encode_emcy",
    "PdoConsumer", "decode_pdo", "encode_pdo", "read_mapping",
    "decode_mapping_word", "encode_mapping_word", "decode_sync_counter",
    "decode_state", "describe_status", "command_bits", "command_names",
    "operation_mode_to_raw", "operation_mode_from_raw",
    "fault_lookup", "fault_search", "by_level", "led_category",
    "EventCounter", "FifoLogEntry", "LogTimestamp", "read_event_counters", "read_fifo_log",
    "read_operational_monitor", "reset_diagnostic_log",
    "SimulatedGen4", "create_pair",
    "SevconError", "TransportError", "TimeoutError", "ProtocolError",
    "ObjectDictionaryError", "AccessError", "SdoAbortError", "StateError",
]

__version__ = "0.1.0"
