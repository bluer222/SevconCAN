import json
import re
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, "src")
sys.path.insert(0, "tools")

import generate_catalog as gc
from sevcon_can.od import bundled_eds_path, load_eds


class TestCatalog(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.od = load_eds(bundled_eds_path())
        cls.records = gc.collect(cls.od)

    def test_collect_covers_dictionary(self):
        self.assertEqual(len(self.records), 1340)
        by_key = {(r["index"], r["sub"]): r for r in self.records}
        volts = by_key[(0x5100, 1)]
        self.assertEqual(volts["name"], "Battery Voltage")
        self.assertEqual(volts["scaling"], 0.0625)
        self.assertEqual(volts["units"], "V")
        login = by_key[(0x5000, 1)]
        self.assertEqual(login["labels"]["4"], "Engineering")

    def test_page_structure(self):
        page = gc.render_page(self.od, "Test")
        for marker in ("id=\"q\"", "id=\"fAccess\"", "id=\"fCat\"", "id=\"fLevel\"",
                       "id=\"fPreop\"", "id=\"fWritable\"", "id=\"fq\"",
                       "showTab", "const VARS =", "const FAULTS ="):
            self.assertIn(marker, page)
        self.assertNotIn("__VARS__", page)
        self.assertNotIn("__TITLE__", page)
        m = re.search(r"const VARS = (\[.*?\]);", page, re.S)
        self.assertEqual(len(json.loads(m.group(1))), 1340)
        m = re.search(r"const FAULTS = (\[.*?\]);", page, re.S)
        self.assertGreaterEqual(len(json.loads(m.group(1))), 200)

    def test_cli_writes_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = str(Path(tmp) / "catalog.html")
            self.assertEqual(gc.main(["--out", out]), 0)
            text = Path(out).read_text(encoding="utf-8")
            self.assertIn("Battery Voltage", text)
            self.assertIn("0x5100", text)


if __name__ == "__main__":
    unittest.main()
