import sys
import unittest

sys.path.insert(0, "src")

from sevcon_can.descriptions import OBJECT_DESCRIPTIONS, accepts_text, describe
from sevcon_can.od import gen4_od


class TestDescribe(unittest.TestCase):
    def test_statusword(self):
        text = describe(0x6041)
        self.assertIn("Statusword", text)
        self.assertIn("Operation enabled", text)
        self.assertIn("0x0027", text)
        self.assertIn("remote", text)

    def test_controlword(self):
        text = describe(0x6040)
        self.assertIn("Fault reset", text)
        self.assertIn("Shutdown", text)
        self.assertIn("Enable operation", text)

    def test_login(self):
        text = describe(0x5000)
        self.assertIn("0x4BDF", text)
        self.assertIn("Engineering", text)

    def test_measurements(self):
        text = describe(0x5100)
        self.assertIn("battery voltage", text.lower())
        self.assertIn("0.0625", text.replace("x0.0625", "0.0625"))

    def test_abort_reasons_listed(self):
        text = describe(0x5310)
        self.assertIn("8=", text)  # access level too low
        self.assertIn("Table 33", text)

    def test_profile_lineage(self):
        # A CiA object with no curated paragraph still explains itself.
        text = describe(0x1000)
        self.assertIn("CiA 301", text)
        # DS401 +0x800 mapping is recognised.
        self.assertIn("DS401", describe(0x6800))

    def test_unknown_index(self):
        text = describe(0x9999)
        self.assertIn("0x9999", text)

    def test_subindex_noted(self):
        self.assertIn("sub-index 1", describe(0x5100, 1))

    def test_curated_count(self):
        self.assertGreaterEqual(len(OBJECT_DESCRIPTIONS), 30)

    def test_accepts_enum(self):
        od = gen4_od()
        text = accepts_text(od.variable((0x6060, 0)))
        self.assertIn("speed (3)", text)
        self.assertIn("torque (4)", text)
        self.assertIn("voltage (5)", text)

    def test_accepts_range_with_physical(self):
        od = gen4_od()
        text = accepts_text(od.variable((0x5100, 1)))
        self.assertIn("UNSIGNED16", text)
        self.assertIn("0…65535", text)
        self.assertIn("V", text)

    def test_accepts_fallback_type(self):
        od = gen4_od()
        var = od.variable((0x1018, 4))
        self.assertTrue(accepts_text(var))  # serial: type name at minimum


if __name__ == "__main__":
    unittest.main()
