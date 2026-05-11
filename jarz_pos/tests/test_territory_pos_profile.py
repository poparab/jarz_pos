"""Tests for territory → POS Profile helpers.

These tests use unittest mocking so they run without a live Frappe / ERPNext
instance (no bench test harness required).
"""
from __future__ import annotations

import json
import sys
import types
import unittest
from unittest.mock import MagicMock, patch


# ---------------------------------------------------------------------------
# Minimal Frappe stub so the module can be imported outside bench
# ---------------------------------------------------------------------------

def _make_frappe_stub():
    frappe = types.ModuleType("frappe")

    class ValidationError(Exception):
        pass

    frappe.ValidationError = ValidationError

    def throw(msg, exc=None, title=None):
        raise (exc or ValidationError)(msg)

    frappe.throw = throw
    frappe.db = MagicMock()
    frappe.get_meta = MagicMock()
    return frappe


if "frappe" not in sys.modules:
    sys.modules["frappe"] = _make_frappe_stub()
else:
    # Patch in the ValidationError stub if the real frappe is missing it
    frappe_mod = sys.modules["frappe"]
    if not hasattr(frappe_mod, "ValidationError"):
        frappe_mod.ValidationError = Exception  # type: ignore


import frappe  # noqa: E402  (after stub registration)

# Re-import helpers fresh each test class (patch objects replace frappe.db / frappe.get_meta)
from jarz_pos.utils.invoice_utils import (  # noqa: E402
    assert_pos_profile_matches_territory,
    resolve_territory_pos_profile,
)


class TestResolveTerritoryPosProfile(unittest.TestCase):
    """Unit tests for resolve_territory_pos_profile()."""

    def _meta_with_field(self):
        meta = MagicMock()
        meta.get_field.return_value = MagicMock()  # truthy → field exists
        return meta

    def _meta_without_field(self):
        meta = MagicMock()
        meta.get_field.return_value = None  # falsy → field absent
        return meta

    def test_blank_customer_returns_none(self):
        self.assertIsNone(resolve_territory_pos_profile(""))
        self.assertIsNone(resolve_territory_pos_profile(None))

    def test_customer_with_no_territory_returns_none(self):
        with patch.object(frappe.db, "get_value", return_value=None):
            self.assertIsNone(resolve_territory_pos_profile("CUST-001"))

    def test_territory_field_absent_returns_none(self):
        def _db_get(doctype, name, field):
            if doctype == "Customer":
                return "Maadi"
            return "JARZ-Profile"

        with patch.object(frappe.db, "get_value", side_effect=_db_get):
            with patch.object(frappe, "get_meta", return_value=self._meta_without_field()):
                self.assertIsNone(resolve_territory_pos_profile("CUST-001"))

    def test_territory_field_present_but_unset_returns_none(self):
        def _db_get(doctype, name, field):
            if doctype == "Customer":
                return "Maadi"
            return None  # Territory has no POS profile set

        with patch.object(frappe.db, "get_value", side_effect=_db_get):
            with patch.object(frappe, "get_meta", return_value=self._meta_with_field()):
                self.assertIsNone(resolve_territory_pos_profile("CUST-001"))

    def test_returns_territory_pos_profile(self):
        def _db_get(doctype, name, field):
            if doctype == "Customer":
                return "Maadi"
            return "Maadi-POS"

        with patch.object(frappe.db, "get_value", side_effect=_db_get):
            with patch.object(frappe, "get_meta", return_value=self._meta_with_field()):
                result = resolve_territory_pos_profile("CUST-001")
        self.assertEqual(result, "Maadi-POS")


class TestAssertPosProfileMatchesTerritory(unittest.TestCase):
    """Unit tests for assert_pos_profile_matches_territory()."""

    def _meta_with_field(self):
        meta = MagicMock()
        meta.get_field.return_value = MagicMock()
        return meta

    def _setup_match(self, customer_territory="Maadi", territory_profile="Maadi-POS"):
        """Return a db.get_value side_effect where selected == territory."""
        def _db_get(doctype, name, field):
            if doctype == "Customer":
                return customer_territory
            return territory_profile
        return _db_get

    # ------------------------------------------------------------------
    # Happy path: profiles match
    # ------------------------------------------------------------------

    def test_match_passes_silently(self):
        with patch.object(frappe.db, "get_value", side_effect=self._setup_match()):
            with patch.object(frappe, "get_meta", return_value=self._meta_with_field()):
                # Should not raise
                assert_pos_profile_matches_territory("CUST-001", "Maadi-POS", override=False)

    def test_match_with_override_true_also_passes(self):
        with patch.object(frappe.db, "get_value", side_effect=self._setup_match()):
            with patch.object(frappe, "get_meta", return_value=self._meta_with_field()):
                assert_pos_profile_matches_territory("CUST-001", "Maadi-POS", override=True)

    # ------------------------------------------------------------------
    # Mismatch path: different profiles
    # ------------------------------------------------------------------

    def test_mismatch_raises_without_override(self):
        def _db_get(doctype, name, field):
            if doctype == "Customer":
                return "Maadi"
            return "Maadi-POS"

        with patch.object(frappe.db, "get_value", side_effect=_db_get):
            with patch.object(frappe, "get_meta", return_value=self._meta_with_field()):
                with self.assertRaises(Exception) as ctx:
                    assert_pos_profile_matches_territory(
                        "CUST-001", "Downtown-POS", override=False
                    )
        payload = json.loads(str(ctx.exception))
        self.assertEqual(payload["code"], "POS_PROFILE_TERRITORY_MISMATCH")
        self.assertEqual(payload["selected_profile"], "Downtown-POS")
        self.assertEqual(payload["territory_profile"], "Maadi-POS")
        self.assertEqual(payload["customer_territory"], "Maadi")

    def test_mismatch_override_true_passes(self):
        def _db_get(doctype, name, field):
            if doctype == "Customer":
                return "Maadi"
            return "Maadi-POS"

        with patch.object(frappe.db, "get_value", side_effect=_db_get):
            with patch.object(frappe, "get_meta", return_value=self._meta_with_field()):
                # Should NOT raise when override=True
                assert_pos_profile_matches_territory(
                    "CUST-001", "Downtown-POS", override=True
                )

    # ------------------------------------------------------------------
    # No territory profile → always requires confirmation
    # ------------------------------------------------------------------

    def test_no_territory_profile_raises_without_override(self):
        def _db_get(doctype, name, field):
            if doctype == "Customer":
                return "Maadi"
            return None  # Territory has no POS profile

        with patch.object(frappe.db, "get_value", side_effect=_db_get):
            with patch.object(frappe, "get_meta", return_value=self._meta_with_field()):
                with self.assertRaises(Exception) as ctx:
                    assert_pos_profile_matches_territory(
                        "CUST-001", "Downtown-POS", override=False
                    )
        payload = json.loads(str(ctx.exception))
        self.assertEqual(payload["code"], "POS_PROFILE_TERRITORY_MISMATCH")
        self.assertEqual(payload["territory_profile"], "")

    def test_no_territory_profile_override_true_passes(self):
        def _db_get(doctype, name, field):
            if doctype == "Customer":
                return "Maadi"
            return None

        with patch.object(frappe.db, "get_value", side_effect=_db_get):
            with patch.object(frappe, "get_meta", return_value=self._meta_with_field()):
                assert_pos_profile_matches_territory(
                    "CUST-001", "Downtown-POS", override=True
                )

    def test_walking_customer_no_territory_raises_without_override(self):
        """Walking Customer has no territory → requires confirmation."""
        with patch.object(frappe.db, "get_value", return_value=None):
            with self.assertRaises(Exception) as ctx:
                assert_pos_profile_matches_territory(
                    "Walking Customer", "Downtown-POS", override=False
                )
        payload = json.loads(str(ctx.exception))
        self.assertEqual(payload["code"], "POS_PROFILE_TERRITORY_MISMATCH")

    def test_walking_customer_override_true_passes(self):
        with patch.object(frappe.db, "get_value", return_value=None):
            assert_pos_profile_matches_territory(
                "Walking Customer", "Downtown-POS", override=True
            )


if __name__ == "__main__":
    unittest.main()
