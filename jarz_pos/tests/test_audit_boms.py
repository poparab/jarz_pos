"""Small (147 ml) jar packaging rule in ``scripts/audit_boms``.

Site-less: the pure ``small_packaging_problems`` carries the rule, and the SQL
rule is exercised with ``frappe.db.sql`` patched.
"""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from jarz_pos.scripts import audit_boms as ab


class TestSmallPackagingProblems(unittest.TestCase):
    def test_correct_small_bom_has_no_problems(self):
        codes = ["Cheesecake Mix", "Glass Jar 147", "Jar Lid 147", "Label Molten 147"]
        self.assertEqual(ab.small_packaging_problems(codes), ([], []))

    def test_unsuffixed_212_jar_and_lid_are_wrong(self):
        codes = ["Glass Jar", "Jar Lid"]
        wrong, missing = ab.small_packaging_problems(codes)
        self.assertEqual(wrong, ["Glass Jar", "Jar Lid"])
        self.assertEqual(missing, ["Glass Jar 147", "Jar Lid 147"])

    def test_330_and_212_packaging_are_wrong(self):
        codes = ["Glass Jar 147", "Jar Lid 147", "Jar Lid 330", "Label Molten 212"]
        wrong, missing = ab.small_packaging_problems(codes)
        self.assertEqual(wrong, ["Jar Lid 330", "Label Molten 212"])
        self.assertEqual(missing, [])

    def test_label_not_ending_147_is_wrong(self):
        codes = ["Glass Jar 147", "Jar Lid 147", "Label Molten"]
        wrong, _missing = ab.small_packaging_problems(codes)
        self.assertEqual(wrong, ["Label Molten"])

    def test_missing_lid_is_reported(self):
        wrong, missing = ab.small_packaging_problems(["Glass Jar 147"])
        self.assertEqual(wrong, [])
        self.assertEqual(missing, ["Jar Lid 147"])


class TestRuleSmallJarPackaging(unittest.TestCase):
    def test_findings_per_bom(self):
        rows = [
            {"name": "BOM-OK", "item": "Lotus Small", "item_code": "Glass Jar 147"},
            {"name": "BOM-OK", "item": "Lotus Small", "item_code": "Jar Lid 147"},
            {"name": "BOM-BAD", "item": "Molten Small", "item_code": "Glass Jar"},
            {"name": "BOM-BAD", "item": "Molten Small", "item_code": "Jar Lid 147"},
        ]
        fake = MagicMock()
        fake.db.sql.return_value = rows
        with patch.object(ab, "frappe", fake):
            findings = ab.rule_small_jar_packaging()

        self.assertEqual(
            [(f["rule"], f["subject"], f["bom"]) for f in findings],
            [
                ("wrong_size_packaging", "Molten Small", "BOM-BAD"),
                ("small_packaging_missing", "Molten Small", "BOM-BAD"),
            ],
        )
        self.assertIn("Glass Jar 147", findings[1]["detail"])


class TestFinishedGroups(unittest.TestCase):
    def test_small_is_a_finished_group(self):
        self.assertEqual(ab.FINISHED_GROUPS, ("Small", "Medium", "Large"))


if __name__ == "__main__":
    unittest.main()
