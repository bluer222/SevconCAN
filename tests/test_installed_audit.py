from copy import deepcopy
import sys
import unittest

sys.path.insert(0, "src")
sys.path.insert(0, "tools")

from audit_installed import audit_installed
from sevcon_can.od import parse_eds


class TestInstalledAudit(unittest.TestCase):
    def setUp(self):
        self.od = parse_eds("""
[1000]
ParameterName=Device type
ObjectType=7
DataType=7
AccessType=const
LowLimit=0
HighLimit=0xFFFFFFFF
;SEVCONFIELD OBJECT_VERSION=1
[2345]
ParameterName=Signed setting
ObjectType=7
DataType=3
AccessType=rw
LowLimit=-32768
HighLimit=32767
PDOMapping=1
;SEVCONFIELD OBJECT_VERSION=2
""")
        self.report = {
            "entry_count": 2, "start": 0, "complete": True,
            "entries": [
                {"table_index": 0, "index": 0x1000, "subindex": 0,
                 "object_type": 7, "internal_data_type": 7, "access_code": 3,
                 "low_limit_raw": 0, "high_limit_raw": 0,
                 "pdo_mapping_raw": 0, "object_version": 1, "object_flags": 255},
                {"table_index": 1, "index": 0x2345, "subindex": 0,
                 "object_type": 7, "internal_data_type": 2, "access_code": 2,
                 "low_limit_raw": 0xFFFF8000, "high_limit_raw": 32767,
                 "pdo_mapping_raw": 1, "object_version": 2, "object_flags": None},
            ],
        }

    def test_recomputes_stale_summaries_and_derived_fields(self):
        self.report.update(metadata_mismatches=[{"old": "limit comparison"}],
                           missing_from_dictionary=[{"stale": True}],
                           absent_from_controller=[{"stale": True}])
        for entry in self.report["entries"]:
            entry.update(dictionary_path=None, data_type=-1,
                         access="invalid cached access", data_type_name="stale")
        result = audit_installed(self.report, self.od)
        self.assertTrue(result["passed"])
        self.assertEqual(result["metadata_mismatches"], [])
        self.assertEqual(result["missing_from_dictionary"], [])
        self.assertEqual(result["absent_from_controller"], [])
        self.assertEqual(result["limit_summary"],
                         {"reported": 1, "unreported_or_ambiguous": 1})
        self.assertFalse(result["identity_verified"])

    def test_raw_metadata_disagreements_fail_the_gate(self):
        self.report["entries"][1].update(
            internal_data_type=6, access_code=0, pdo_mapping_raw=0,
            object_version=9, low_limit_raw=0, high_limit_raw=500)
        result = audit_installed(self.report, self.od)
        self.assertFalse(result["passed"])
        mismatch, = result["metadata_mismatches"]
        self.assertEqual(set(mismatch["differences"]), {
            "data_type", "access", "pdo_mapping_raw", "object_version",
            "low_limit_raw", "high_limit_raw"})

    def test_equal_row_count_does_not_prove_equal_address_sets(self):
        self.report["entries"][1]["index"] = 0x2999
        result = audit_installed(self.report, self.od)
        self.assertFalse(result["passed"])
        self.assertEqual(result["missing_from_dictionary"][0]["index"], 0x2999)
        self.assertIsNone(result["missing_from_dictionary"][0]["dictionary_path"])
        self.assertEqual(result["absent_from_controller"][0]["index"], 0x2345)

    def test_empty_selected_dictionary_cannot_pass_with_an_empty_export(self):
        report = {"entry_count": 0, "start": 0, "complete": True, "entries": []}
        for text in ("This is not an EDS file.\n",
                     "[2345]\nObjectType=9\nParameterName=Empty record\n"):
            with self.subTest(text=text):
                with self.assertRaisesRegex(ValueError, "no variable entries"):
                    audit_installed(report, parse_eds(text))

    def test_partial_scan_does_not_prove_absence(self):
        self.report.update(complete=False, entries=self.report["entries"][:1])
        result = audit_installed(
            self.report, self.od,
            unresolved_references=[{"index": 0x2345, "subindex": None}],
            supplemental_keys={(0x291F, 0)})
        self.assertFalse(result["passed"])
        self.assertIsNone(result["absent_from_controller"])
        for field in ("global_unresolved_references", "optional_supplement_entries"):
            self.assertIsNone(result[field]["not_installed"])
            self.assertEqual(result[field]["unverified"], 1)

    def test_full_scan_projects_global_and_optional_references(self):
        result = audit_installed(
            self.report, self.od,
            unresolved_references=[{"index": 0x2345, "subindex": None},
                                   {"index": 0x2345, "subindex": 1}],
            supplemental_keys={(0x291F, 0)})
        self.assertEqual(result["global_unresolved_references"], {
            "total": 2, "installed": [{"index": 0x2345, "subindex": None}],
            "not_installed": 1, "unverified": 0})
        self.assertEqual(result["optional_supplement_entries"]["not_installed"], 1)

    def test_malformed_or_incoherent_exports_are_rejected(self):
        changes = [
            lambda r: r.update(entry_count=True),
            lambda r: r.update(complete=1),
            lambda r: r.update(start=1),
            lambda r: r["entries"].pop(),
            lambda r: r["entries"][0].pop("internal_data_type"),
            lambda r: r["entries"][0].update(table_index=1),
            lambda r: r["entries"][1].update(index=0),
            lambda r: r["entries"][1].update(index=0x1000),
            lambda r: r["entries"][0].update(low_limit_raw=-1),
            lambda r: r["entries"][0].update(high_limit_raw=0x100000000),
            lambda r: r["entries"][0].update(subindex=256),
            lambda r: r["entries"][0].update(object_flags=True),
        ]
        for change in changes:
            with self.subTest(change=change):
                report = deepcopy(self.report)
                change(report)
                with self.assertRaises(ValueError):
                    audit_installed(report, self.od)


if __name__ == "__main__":
    unittest.main()
