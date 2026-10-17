import contextlib
import io
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, "src")
sys.path.insert(0, "tools")

import audit_coverage
from spec_parser import parse_object_references


class TestManualCoverage(unittest.TestCase):
    def test_lowercase_creep_and_application_note_syntax(self):
        refs = parse_object_references(
            "Set creep torque at 291fh.\nRead object 0x606c.\n"
            "Ignore 41X0h templates and 0x40000080 COB-IDs.")
        self.assertEqual(set(refs), {0x291F, 0x606C})

    def test_direct_subindices_and_ranges(self):
        self.assertEqual(audit_coverage.direct_subindices(",4+16)"), [4, 16])
        self.assertEqual(audit_coverage.direct_subindices(", sub-indices 2 to 5."),
                         [2, 3, 4, 5])
        self.assertEqual(audit_coverage.direct_subindices(" to 17FFh"), [])

    def test_new_missing_reference_requires_review(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "specs/od").mkdir(parents=True)
            (root / "specs/text").mkdir()
            (root / "specs/od/Gen4.eds").write_text(
                "[1000]\nParameterName=Device type\nObjectType=7\n"
                "DataType=7\nAccessType=ro\n")
            (root / "specs/text/Gen4-Test.txt").write_text(
                "A newly documented parameter is at 2999h.\n")
            report = audit_coverage.audit(root)
            self.assertEqual(report["counts"], {"unreviewed": 1})
            self.assertEqual(report["references"][0]["index"], 0x2999)

    def test_pinned_creep_schema_and_known_unknowns_are_visible(self):
        report = audit_coverage.audit()
        refs = {(r["index"], r["subindex"]): r for r in report["references"]}
        self.assertEqual(refs[0x291F, None]["status"], "alternate-schema")
        self.assertIn("Gen4-AC24ls-rev0x00010017.dcf",
                      refs[0x291F, None]["dictionaries"])
        self.assertEqual(refs[0x291F, None]["implementation"], "optional-supplement")
        self.assertEqual(refs[0x291F, None]["implementation_source"],
                         "Gen4-AC24ls-rev0x00010017.dcf")
        self.assertEqual(refs[0x6040, None]["implementation"], "baseline")
        self.assertEqual(refs[0x4630, 15]["implementation"], "none")
        self.assertEqual(refs[0x4630, 15]["status"], "unresolved")
        self.assertEqual(refs[0x2910, 22]["status"], "unresolved")
        self.assertEqual(refs[0x15FF, None]["status"], "range-boundary")
        self.assertEqual(report["counts"].get("unreviewed", 0), 0)
        self.assertGreater(report["counts"]["unresolved"], 0)

    def test_freshness_is_distinct_from_completeness(self):
        report = audit_coverage.audit()
        with contextlib.redirect_stdout(io.StringIO()):
            with patch.object(audit_coverage, "audit", return_value=report):
                self.assertEqual(audit_coverage.main(["--check"]), 0)
                self.assertEqual(audit_coverage.main(["--require-complete"]), 1)


if __name__ == "__main__":
    unittest.main()
