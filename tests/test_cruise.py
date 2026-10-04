import sys
import unittest
from pathlib import Path

sys.path.insert(0, "src")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "examples"))

from async_cruise import CruiseController  # noqa: E402


class TestCruise(unittest.TestCase):
    def test_pedal_passthrough_when_off(self):
        c = CruiseController()
        demand, event = c.update(42.0, False)
        self.assertEqual((demand, event), (42.0, None))

    def test_engage_latches_target(self):
        c = CruiseController()
        self.assertIn("engaged", c.engage(40.0))
        demand, event = c.update(40.0, False)
        self.assertEqual(demand, 40.0)
        self.assertIsNone(event)
        # Pedal released: cruise still holds.
        demand, _ = c.update(0.0, False)
        self.assertEqual(demand, 40.0)

    def test_engage_refused_at_rest(self):
        c = CruiseController()
        self.assertIn("refused", c.engage(2.0))
        self.assertFalse(c.engaged)

    def test_pedal_override_disengages_bumpless(self):
        c = CruiseController()
        c.engage(40.0)
        demand, event = c.update(50.0, False)
        self.assertFalse(c.engaged)
        self.assertEqual(demand, 50.0)  # follows the pedal, no jump
        self.assertIn("override", event)

    def test_small_pedal_does_not_cancel(self):
        c = CruiseController()
        c.engage(40.0)
        demand, _ = c.update(42.0, False)  # within margin
        self.assertEqual(demand, 40.0)
        self.assertTrue(c.engaged)

    def test_brake_disengages_and_coasts(self):
        c = CruiseController()
        c.engage(40.0)
        demand, event = c.update(40.0, True)
        self.assertEqual(demand, 0.0)
        self.assertFalse(c.engaged)
        self.assertIn("brake", event)

    def test_brake_when_off_stays_zero(self):
        c = CruiseController()
        demand, event = c.update(30.0, True)
        self.assertEqual(demand, 0.0)
        self.assertIsNone(event)

    def test_adjust(self):
        c = CruiseController()
        self.assertEqual(c.adjust(1.0), "not engaged")
        c.engage(40.0)
        c.adjust(5.0)
        self.assertEqual(c.target, 45.0)
        c.adjust(-200.0)
        self.assertEqual(c.target, 0.0)  # clamped, never negative

    def test_never_negative(self):
        c = CruiseController()  # no regen: demands are >= 0 by construction
        for pedal in (-5.0, 0.0, 30.0):
            demand, _ = c.update(pedal, False)
            self.assertGreaterEqual(demand, 0.0)


if __name__ == "__main__":
    unittest.main()
