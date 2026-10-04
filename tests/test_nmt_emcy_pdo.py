import sys
import unittest

sys.path.insert(0, "src")

from sevcon_can.emcy import decode_emcy, encode_emcy
from sevcon_can.nmt import (
    HEARTBEAT_BASE,
    NMT_PREOP,
    NMT_START,
    decode_heartbeat,
    nmt_command_frame,
    state_name,
)
from sevcon_can.pdo import (
    decode_mapping_word,
    decode_pdo,
    decode_sync_counter,
    encode_mapping_word,
    encode_pdo,
)
from sevcon_can.transport import CanFrame


class TestNmt(unittest.TestCase):
    def test_command_frame(self):
        frame = nmt_command_frame(NMT_START, 0x20)
        self.assertEqual(frame.arbitration_id, 0x000)
        self.assertEqual(frame.data, bytes((NMT_START, 0x20)))
        with self.assertRaises(ValueError):
            nmt_command_frame(0xFF, 1)
        with self.assertRaises(ValueError):
            nmt_command_frame(NMT_START, 128)

    def test_heartbeat_roundtrip(self):
        frame = CanFrame(HEARTBEAT_BASE + 0x20, b"\x05")
        node, state = decode_heartbeat(frame)
        self.assertEqual((node, state), (0x20, 5))
        self.assertEqual(state_name(5), "operational")
        self.assertEqual(state_name(127), "pre-operational")
        with self.assertRaises(ValueError):
            decode_heartbeat(CanFrame(0x200, b"\x05"))
        with self.assertRaises(ValueError):
            decode_heartbeat(CanFrame(HEARTBEAT_BASE + 1, b"\x05\x00"))

    def test_preop_constant(self):
        self.assertEqual(NMT_PREOP, 0x80)


class TestEmcy(unittest.TestCase):
    def test_sevcon_layout(self):
        # Fault id lives in payload bytes 3-4 (OVMS + Victron agree).
        frame = encode_emcy(0x20, 0xFF00, 0x81, 0x4581, b"\x01\x02\x03")
        message = decode_emcy(frame)
        self.assertEqual(message.node_id, 0x20)
        self.assertEqual(message.error_code, 0xFF00)
        self.assertEqual(message.fault_id, 0x4581)
        self.assertEqual(message.extra, b"\x01\x02\x03")
        self.assertIn("Throttle", message.fault_text or "")

    def test_no_fault(self):
        frame = encode_emcy(0x20, 0x8130, 0x11)
        message = decode_emcy(frame)
        self.assertEqual(message.fault_id, 0)
        self.assertIsNone(message.fault)
        self.assertIn("heartbeat", message.error_text.lower())

    def test_register_flags(self):
        message = decode_emcy(encode_emcy(0x20, 0x1000, 0x81))
        self.assertIn("generic error", message.register_flags)
        self.assertIn("manufacturer specific", message.register_flags)

    def test_rejects_bad_frames(self):
        with self.assertRaises(ValueError):
            decode_emcy(CanFrame(0x200, b"\x00" * 8))
        with self.assertRaises(ValueError):
            decode_emcy(CanFrame(0x80 + 1, b"\x00" * 7))


class TestPdo(unittest.TestCase):
    MAPPING = [
        decode_mapping_word(0x60410010),
        decode_mapping_word(0x60610008),
    ]

    def test_mapping_word(self):
        self.assertEqual(decode_mapping_word(0x60410010), (0x6041, 0, 16))
        self.assertEqual(encode_mapping_word(0x6041, 0, 16), 0x60410010)

    def test_roundtrip(self):
        payload = encode_pdo([(0x6041, 0, 0x27), (0x6061, 0, 3)], self.MAPPING)
        self.assertEqual(len(payload), 3)
        decoded = decode_pdo(payload, self.MAPPING)
        self.assertEqual([(v.index, v.subindex, v.raw) for v in decoded],
                         [(0x6041, 0, 0x27), (0x6061, 0, 3)])

    def test_short_payload_rejected(self):
        from sevcon_can.exceptions import ProtocolError
        with self.assertRaises(ProtocolError):
            decode_pdo(b"\x27", [(0x6041, 0, 16)])

    def test_order_enforced(self):
        with self.assertRaises(ValueError):
            encode_pdo([(0x6061, 0, 3), (0x6041, 0, 0x27)], self.MAPPING)

    def test_sync_counter(self):
        self.assertIsNone(decode_sync_counter(b""))
        self.assertEqual(decode_sync_counter(b"\x42"), 0x42)
        from sevcon_can.exceptions import ProtocolError
        with self.assertRaises(ProtocolError):
            decode_sync_counter(b"\x01\x02")


if __name__ == "__main__":
    unittest.main()
