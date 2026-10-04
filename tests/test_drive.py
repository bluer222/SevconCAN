import sys
import unittest

sys.path.insert(0, "src")

from sevcon_can import drive
from sevcon_can.tables import DS402_COMMANDS, DS402_STATES


class TestStates(unittest.TestCase):
    def test_all_documented_states_decode(self):
        self.assertEqual(drive.decode_state(0x0040), "Switch on disabled")
        self.assertEqual(drive.decode_state(0x0021), "Ready to switch on")
        self.assertEqual(drive.decode_state(0x0023), "Switched on")
        self.assertEqual(drive.decode_state(0x0027), "Operation enabled")
        self.assertEqual(drive.decode_state(0x0007), "Quick stop active")
        self.assertEqual(drive.decode_state(0x000F), "Fault reaction active")
        self.assertEqual(drive.decode_state(0x0008), "Fault")
        self.assertEqual(drive.decode_state(0x0000), "Not ready to switch on")

    def test_extra_bits_ignored(self):
        # Bit 9 (remote) and bit 11 (internal limit) must not change state.
        self.assertEqual(drive.decode_state(0x0027 | 0x0200), "Operation enabled")
        self.assertEqual(drive.decode_state(0x0040 | 0x0200 | 0x0800), "Switch on disabled")

    def test_eight_states_in_tables(self):
        self.assertEqual(len(DS402_STATES), 8)


class TestCommands(unittest.TestCase):
    def test_command_bits(self):
        self.assertEqual(drive.command_bits("Shutdown"), 0x06)
        self.assertEqual(drive.command_bits("Enable operation"), 0x0F)
        self.assertEqual(drive.command_bits("Fault reset"), 0x80)
        self.assertEqual(drive.command_bits("Disable voltage"), 0x00)

    def test_aliases(self):
        for alias in ("enable_operation", "Enable operation", "enable operation"):
            resolved = drive.COMMAND_ALIASES.get(alias.strip().lower(), alias)
            self.assertEqual(drive.command_bits(resolved), 0x0F)

    def test_unknown_command(self):
        with self.assertRaises(KeyError):
            drive.command_bits("Warp drive")

    def test_enable_sequence_valid(self):
        for name in drive.ENABLE_SEQUENCE:
            drive.command_bits(name)


class TestModes(unittest.TestCase):
    def test_gen4_modes(self):
        self.assertEqual(drive.operation_mode_to_raw("speed"), 3)
        self.assertEqual(drive.operation_mode_to_raw("torque"), 4)
        self.assertEqual(drive.operation_mode_to_raw("voltage"), 5)
        self.assertEqual(drive.operation_mode_from_raw(5), "voltage")
        with self.assertRaises(ValueError):
            drive.operation_mode_to_raw("position")
        with self.assertRaises(ValueError):
            drive.operation_mode_to_raw(1)  # DSP402 profile position: no Gen4 EDS entry


class TestDescribe(unittest.TestCase):
    def test_flags(self):
        info = drive.describe_status(0x0027)
        self.assertTrue(info.operation_enabled)
        self.assertFalse(info.fault)
        info = drive.describe_status(0x0008 | 0x0080)
        self.assertTrue(info.fault)
        self.assertTrue(info.warning)


if __name__ == "__main__":
    unittest.main()
