import importlib.util
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, "src")

from sevcon_can.catalog import build_catalog, write_catalog
from sevcon_can.exceptions import ObjectDictionaryError
from sevcon_can.od import gen4_od
from sevcon_can import object_catalog


class TestCatalog(unittest.TestCase):
    def setUp(self):
        self.od = gen4_od()

    def test_all_objects_and_exact_authorization_partition(self):
        catalog = build_catalog(self.od)
        variables = {var.key: var for var in self.od.variables()}
        self.assertEqual(set(catalog["ALL_OBJECTS"]), set(variables))
        keys = []
        for level, entries in catalog["BY_REQUIRED_AUTH_LEVEL"].items():
            keys.extend(entries)
            for key in entries:
                self.assertEqual(variables[key].access_level, level)
        self.assertEqual(len(keys), len(set(keys)))
        self.assertEqual(set(keys), set(variables))
        self.assertTrue(catalog["BY_REQUIRED_AUTH_LEVEL"][None])
        self.assertIn("Battery Voltage [V]", catalog["ALL_OBJECTS"][(0x5100, 1)])

    def test_cumulative_write_authorization_excludes_read_only(self):
        catalog = build_catalog(self.od)
        previous = set()
        for level, entries in catalog["WRITABLE_BY_AUTH_LEVEL"].items():
            self.assertTrue(previous <= set(entries))
            for key in entries:
                var = self.od.variable(key)
                self.assertTrue(var.writable)
                self.assertTrue(var.access_level is None or var.access_level <= level)
            previous = set(entries)
        self.assertNotIn((0x5100, 1), previous)
        self.assertIn((0x2800, 0), catalog["WRITABLE_BY_AUTH_LEVEL"][4])
        self.assertNotIn((0x2800, 0), catalog["WRITABLE_BY_AUTH_LEVEL"][3])
        self.assertTrue(catalog["OBJECT_METADATA"][(0x6060, 0)]["preop_only"])

    def test_default_stream_empty_but_disabled_mappings_retained(self):
        catalog = build_catalog(self.od)
        self.assertEqual(catalog["STREAM_OBJECTS"], {})
        self.assertTrue(catalog["TPDO_OBJECTS"][1])
        self.assertEqual(len(catalog["TPDO_CONFIGURATION"]), 5)

    def test_live_enabled_stream_and_unknown_mapping(self):
        configs = {
            1: {"enabled": True, "cob_id": 0x1A0, "transmission_type": 255,
                "mapping": [(0x5100, 1, 16), (0x5100, 2, 16)]},
            2: {"enabled": False, "cob_id": 0x800002A0, "transmission_type": 255,
                "mapping": [(0x5100, 4, 16)]},
        }
        catalog = build_catalog(self.od, tpdo_configurations=configs)
        self.assertEqual(set(catalog["STREAM_OBJECTS"]), {(0x5100, 1), (0x5100, 2)})
        self.assertIn((0x5100, 4), catalog["TPDO_OBJECTS"][2])
        configs[1]["mapping"] = [(0xFFFF, 0, 16)]
        with self.assertRaises(ObjectDictionaryError):
            build_catalog(self.od, tpdo_configurations=configs)

    def test_generated_module_matches_catalog_and_is_standalone(self):
        configs = {1: {"enabled": True, "cob_id": 0x1A0,
                       "transmission_type": 255, "mapping": [(0x5100, 1, 16)]}}
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "lookups.py"
            expected = write_catalog(path, self.od, tpdo_configurations=configs)
            self.assertNotIn("import ", path.read_text())
            spec = importlib.util.spec_from_file_location("lookups", path)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            for name, value in expected.items():
                self.assertEqual(getattr(module, name), value, name)
            self.assertIs(module.AUTH_LEVEL_4, module.BY_REQUIRED_AUTH_LEVEL[4])
            self.assertIs(module.AUTH_LEVEL_UNSPECIFIED, module.BY_REQUIRED_AUTH_LEVEL[None])
            self.assertIs(module.WRITABLE_AUTH_LEVEL_4, module.WRITABLE_BY_AUTH_LEVEL[4])
            self.assertNotIn((0x5000, 2), module.READABLE_OBJECTS)

    def test_committed_module_matches_bundled_eds(self):
        for name, value in build_catalog(gen4_od(include_supplemental=True)).items():
            self.assertEqual(getattr(object_catalog, name), value, name)


if __name__ == "__main__":
    unittest.main()
