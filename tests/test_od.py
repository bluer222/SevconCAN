import sys
import unittest

sys.path.insert(0, "src")

from sevcon_can.exceptions import ObjectDictionaryError
from sevcon_can.od import gen4_od, load_eds, normalize_name


class TestEdsBasics(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.od = gen4_od()

    def test_identity(self):
        self.assertEqual(self.od.vendor_id, 0x1E)
        self.assertEqual(self.od.product_code, 0x07055037)
        self.assertEqual(self.od.revision, 0x00010026)
        self.assertGreaterEqual(len(self.od.objects), 300)

    def test_login_objects(self):
        level = self.od.variable((0x5000, 1))
        self.assertTrue(level.readable and not level.writable)
        self.assertEqual(level.labels[4], "Engineering")
        password = self.od.variable((0x5000, 2))
        self.assertTrue(password.writable and not password.readable)

    def test_measurements_scaling(self):
        volts = self.od.variable((0x5100, 1))
        self.assertEqual(volts.scaling, 0.0625)
        self.assertEqual(volts.units, "V")
        self.assertEqual(volts.to_physical(800), 50.0)
        self.assertEqual(volts.from_physical(50.0), 800)
        amps = self.od.variable((0x5100, 2))
        self.assertEqual(amps.units, "A")

    def test_nmt_state_labels(self):
        state = self.od.variable((0x5110, 0))
        self.assertEqual(state.labels[5], "Operational")
        self.assertEqual(state.labels[127], "Pre-Operational")
        abort = self.od.variable((0x5310, 0))
        self.assertIn(8, abort.labels)  # "Access level too low"

    def test_operation_modes(self):
        mode = self.od.variable((0x6060, 0))
        self.assertEqual(mode.labels[5], "voltage")
        self.assertTrue(mode.preop_only)

    def test_name_lookup(self):
        var = self.od.variable("Device Measurements.Battery Voltage")
        self.assertEqual((var.index, var.subindex), (0x5100, 1))
        tag = self.od.variable("STATUSWORD")
        self.assertEqual(tag.index, 0x6041)
        # Bare "battery_voltage" exists twice (5100h:1 and 4602h:11).
        with self.assertRaises(ObjectDictionaryError):
            self.od.variable("battery_voltage")

    def test_store_signatures(self):
        store = self.od.variable((0x1010, 1))
        self.assertEqual(store.labels[0x65766173], "Save to EEPROM")
        restore = self.od.variable((0x1011, 1))
        self.assertEqual(restore.labels[0x64616F6C], "Load from EEPROM")

    def test_unknown_raises(self):
        with self.assertRaises(ObjectDictionaryError):
            self.od.variable((0x9999, 0))

    def test_pdo_objects_present(self):
        for index in (0x1400, 0x1600, 0x1800, 0x1A00):
            self.assertIn(index, self.od.objects)

    def test_sdo_servers(self):
        for index in (0x1200, 0x1201, 0x1202, 0x1203):
            self.assertIn(index, self.od.objects)


if __name__ == "__main__":
    unittest.main()
