import sys
import unittest

sys.path.insert(0, "src")
sys.path.insert(0, "tools")

import spec_parser as sp
from sevcon_can import faults
from sevcon_can.tables import (
    GEN4_FAULTS,
    GEN4_LED_CATEGORIES,
    GEN4_RATINGS,
    GEN4_VOLTAGE_RANGES,
)


class TestFaultTables(unittest.TestCase):
    def test_size(self):
        self.assertGreaterEqual(len(GEN4_FAULTS), 200)
        self.assertEqual(len(GEN4_LED_CATEGORIES), 16)

    def test_known_entries(self):
        self.assertIn("Throttle", GEN4_FAULTS[0x4581].message)
        self.assertIn("Battery", GEN4_FAULTS[0x45C3].message)

    def test_lookup_and_search(self):
        self.assertEqual(faults.lookup(0x4581).fid, 0x4581)
        with self.assertRaises(KeyError):
            faults.lookup(0x0001)
        hits = faults.search("throttle")
        self.assertTrue(any(f.fid == 0x4581 for f in hits))
        self.assertEqual(faults.led_category(7), "Battery faults")
        with self.assertRaises(KeyError):
            faults.led_category(99)

    def test_independent_sources_agree(self):
        # Every OVMS/Victron id must exist in the merged table.
        ovms = sp.parse_ovms_faults()
        victron = sp.parse_victron_faults()
        missing = [fid for fid in list(ovms) + list(victron) if fid not in GEN4_FAULTS]
        self.assertEqual(missing, [])


class TestRatings(unittest.TestCase):
    def test_target_controller_rating(self):
        match = [r for r in GEN4_RATINGS
                 if r.voltage == "36/48V" and r.size == 2]
        self.assertEqual(len(match), 1)
        self.assertEqual((match[0].short_term_a, match[0].continuous_a), (275, 110))

    def test_voltage_window(self):
        match = [v for v in GEN4_VOLTAGE_RANGES if v.voltage == "36/48V"]
        self.assertEqual(len(match), 1)
        self.assertEqual((match[0].conventional_min, match[0].conventional_max),
                         (25.2, 57.6))


class TestTablesFreshness(unittest.TestCase):
    def test_generate_tables_check(self):
        import generate_tables
        text = generate_tables.render()
        out = generate_tables.OUT
        self.assertTrue(out.exists(), "tables.py missing")
        self.assertEqual(out.read_text(), text,
                         "tables.py is stale; run tools/generate_tables.py")


if __name__ == "__main__":
    unittest.main()
