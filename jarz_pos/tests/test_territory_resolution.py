"""Tests for resolving an Address to the Territory that prices its delivery.

`Address.city` only literally equals a Territory name when the POS wrote it.
WooCommerce stores a free-text label ("Nasr City - مدينه نصر"), and the Arabic
spelling in that label rarely matches the Territory master exactly ("مدينة نصر").
Getting this wrong makes an order settle against the wrong courier rate, so the
matching rules are pinned here.
"""

import unittest
from unittest.mock import patch


class TestArabicFolding(unittest.TestCase):
    """Test class for jarz_pos.utils.invoice_utils._fold_territory_lookup_text."""

    def test_folds_the_spelling_variants_that_appear_in_real_labels(self):
        from jarz_pos.utils.invoice_utils import _fold_territory_lookup_text as fold

        # ة/ه, أ/ا and diacritics are the variants seen between Woo and the master.
        self.assertEqual(fold("مدينه نصر"), fold("مدينة نصر"))
        self.assertEqual(fold("حدائق أكتوبر"), fold("حدائق اكتوبر"))
        self.assertEqual(fold("المعادي"), fold("المعادى"))

    def test_collapses_whitespace_and_case(self):
        from jarz_pos.utils.invoice_utils import _fold_territory_lookup_text as fold

        self.assertEqual(fold("  Nasr   City "), fold("nasr city"))

    def test_keeps_genuinely_different_names_apart(self):
        from jarz_pos.utils.invoice_utils import _fold_territory_lookup_text as fold

        self.assertNotEqual(fold("المعادي"), fold("المطريه"))
        self.assertNotEqual(fold("Maadi"), fold("Madinaty"))


class TestFoldedTerritoryLookup(unittest.TestCase):
    """Test class for the spelling-insensitive fallback in resolve_territory_name."""

    def _index(self, rows):
        """Patch the Territory table the folded index is built from."""
        from jarz_pos.utils import invoice_utils

        # Drop any index cached by an earlier test in the same request.
        if hasattr(invoice_utils.frappe.local, "_jarz_folded_territory_index"):
            delattr(invoice_utils.frappe.local, "_jarz_folded_territory_index")
        return patch.object(invoice_utils.frappe, "get_all", return_value=rows)

    def test_exact_match_still_wins_without_consulting_the_index(self):
        from jarz_pos.utils import invoice_utils

        with patch.object(invoice_utils, "_lookup_territory_by_field", return_value="EGMAADI"):
            with patch.object(invoice_utils, "_folded_territory_index") as index:
                self.assertEqual(invoice_utils.resolve_territory_name("EGMAADI"), "EGMAADI")
                index.assert_not_called()

    def test_recovers_a_label_whose_arabic_spelling_differs(self):
        from jarz_pos.utils import invoice_utils

        rows = [{"name": "EGNASRCITY", "territory_name": "EGNASRCITY",
                 "custom_territory_name_ar": "مدينة نصر"}]
        with patch.object(invoice_utils, "_lookup_territory_by_field", return_value=None):
            with patch.object(invoice_utils, "_territory_has_column", return_value=True):
                with self._index(rows):
                    resolved = invoice_utils.resolve_territory_name("Nasr City - مدينه نصر")

        self.assertEqual(resolved, "EGNASRCITY")

    def test_refuses_to_guess_when_two_territories_fold_alike(self):
        from jarz_pos.utils import invoice_utils

        rows = [
            {"name": "EGA", "territory_name": "EGA", "custom_territory_name_ar": "مدينه نصر"},
            {"name": "EGB", "territory_name": "EGB", "custom_territory_name_ar": "مدينة نصر"},
        ]
        with patch.object(invoice_utils, "_lookup_territory_by_field", return_value=None):
            with patch.object(invoice_utils, "_territory_has_column", return_value=True):
                with self._index(rows):
                    resolved = invoice_utils.resolve_territory_name("مدينه نصر")

        self.assertIsNone(resolved, "An ambiguous label must not pick a territory")

    def test_returns_none_when_nothing_matches(self):
        from jarz_pos.utils import invoice_utils

        rows = [{"name": "EGMAADI", "territory_name": "EGMAADI",
                 "custom_territory_name_ar": "المعادى"}]
        with patch.object(invoice_utils, "_lookup_territory_by_field", return_value=None):
            with patch.object(invoice_utils, "_territory_has_column", return_value=True):
                with self._index(rows):
                    self.assertIsNone(invoice_utils.resolve_territory_name("Atlantis - أتلانتس"))
