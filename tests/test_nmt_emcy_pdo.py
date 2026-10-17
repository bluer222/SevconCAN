import sys
import unittest

sys.path.insert(0, "src")

from sevcon_can.emcy import EmcyConsumer, decode_emcy, encode_emcy
from sevcon_can.network import Network
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
from sevcon_can.transport import CanFrame, VirtualBus


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

    def test_configured_standard_and_extended_identifiers(self):
        for cob_id in (0x321, 0x20000321):
            with self.subTest(cob_id=cob_id):
                frame = encode_emcy(0x20, 0x8130, 0x11, 0x4581, cob_id=cob_id)
                decoded = decode_emcy(frame, 0x20, cob_id=cob_id)
                self.assertEqual((decoded.node_id, decoded.fault_id), (0x20, 0x4581))
                with self.assertRaises(ValueError):
                    decode_emcy(frame)
                net = Network(VirtualBus().endpoint())
                consumer = EmcyConsumer(net, 0x20, cob_id=cob_id)
                net.dispatch(CanFrame(frame.arbitration_id, frame.data,
                                      is_extended=not frame.is_extended))
                self.assertTrue(consumer.queue.empty())
                net.dispatch(frame)
                self.assertEqual(consumer.queue.get_nowait().node_id, 0x20)
                consumer.close()

    def test_configured_identifier_validation(self):
        net = Network(VirtualBus().endpoint())
        for cob_id in (0x800000A0, 0x400000A0, 0x12345, 0x100000000, -1):
            with self.subTest(cob_id=cob_id):
                with self.assertRaises(ValueError):
                    EmcyConsumer(net, 0x20, cob_id=cob_id)
                with self.assertRaises(ValueError):
                    encode_emcy(0x20, 0x1000, 1, cob_id=cob_id)
        with self.assertRaises(ValueError):
            EmcyConsumer(net, cob_id=0x321)
        with self.assertRaises(ValueError):
            decode_emcy(CanFrame(0x321, bytes(8)), cob_id=0x321)
        for node in (0, 128):
            with self.assertRaises(ValueError):
                EmcyConsumer(net, node)

    def test_all_nodes_consumer_only_accepts_predefined_standard_emcys(self):
        net = Network(VirtualBus().endpoint())
        consumer = EmcyConsumer(net)
        for frame in (CanFrame(0x321, bytes(8)),
                      CanFrame(0xA0, bytes(8), is_extended=True),
                      CanFrame(0xA0, is_remote=True, dlc=8)):
            net.dispatch(frame)
            self.assertTrue(consumer.queue.empty())
        net.dispatch(encode_emcy(0x20, 0x1000, 1))
        self.assertEqual(consumer.queue.get_nowait().node_id, 0x20)
        consumer.close()


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
        for counter in (0, 241, 255):
            with self.assertRaises(ProtocolError):
                decode_sync_counter(bytes((counter,)))


if __name__ == "__main__":
    unittest.main()
