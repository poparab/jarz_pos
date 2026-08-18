"""Tests for POS audit-marker routing, channel stamping and territory precedence.

Covers the pure parts of the Woo↔POS order-parity work:

* **F-13** — internal markers move out of ``remarks`` (which means "the customer's
  note" on the WooCommerce lane) into ``custom_pos_audit_markers``, mirroring back
  into ``remarks`` only for the markers a consumer outside this package still
  reads there.
* **F-14** — the ``channel`` argument is persisted instead of discarded.
* **F-07** — ``Address.city`` beats ``Address.state`` when resolving a Territory,
  explicitly and on purpose.
* **F-09** — an unresolved territory is caught and shouted about instead of
  quietly shipping free. Blank AND ``All Territories`` both count as unresolved:
  the tree root has ``delivery_income`` 0 and no ``pos_profile``, so it is
  operationally identical to NULL, and which of the two a path produces must not
  decide whether the order gets reviewed.
* **F-08** — every invoice reaches ``record_invoice_territory_exception``. It
  fires rarely in the wild (25 POS-origin mismatches on production), so the wiring
  is pinned by test rather than left to chance.

These use unittest mocking so they run without a live Frappe / ERPNext instance,
matching ``test_territory_pos_profile.py``.
"""
from __future__ import annotations

import sys
import types
import unittest
from types import SimpleNamespace
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
    frappe.log_error = MagicMock()
    return frappe


if "frappe" not in sys.modules:
    sys.modules["frappe"] = _make_frappe_stub()


from jarz_pos.utils import invoice_utils  # noqa: E402
from jarz_pos.utils.invoice_utils import (  # noqa: E402
    ORDER_CHANNEL_FIELD,
    POS_AUDIT_MARKERS_FIELD,
    append_pos_audit_marker,
    describe_address_territory_resolution,
    is_unresolved_territory,
    stamp_order_channel,
)


def _invoice(**overrides):
    """A bare stand-in for a Sales Invoice document."""
    doc = SimpleNamespace(remarks="", woo_source_type="")
    setattr(doc, POS_AUDIT_MARKERS_FIELD, "")
    setattr(doc, ORDER_CHANNEL_FIELD, "")
    for key, value in overrides.items():
        setattr(doc, key, value)
    return doc


def _markers(doc):
    return getattr(doc, POS_AUDIT_MARKERS_FIELD, "") or ""


# ===========================================================================
# F-13: marker routing
# ===========================================================================

class TestAppendPosAuditMarker(unittest.TestCase):
    """``remarks`` must stop meaning two different things per lane."""

    def test_marker_lands_in_audit_field_and_leaves_remarks_alone(self):
        doc = _invoice()

        append_pos_audit_marker(doc, "[LINE DISCOUNTS]")

        self.assertEqual(_markers(doc), "[LINE DISCOUNTS]")
        self.assertEqual(doc.remarks, "", "remarks is the customer's note, not an audit log")

    def test_customer_note_in_remarks_is_never_disturbed(self):
        doc = _invoice(remarks="Please ring the bell twice")

        append_pos_audit_marker(doc, "[CUSTOM LINE PRICING]")

        self.assertEqual(doc.remarks, "Please ring the bell twice")
        self.assertEqual(_markers(doc), "[CUSTOM LINE PRICING]")

    def test_mirrored_marker_is_written_to_both_fields(self):
        doc = _invoice()

        append_pos_audit_marker(doc, "[PICKUP]", mirror_to_remarks=True)

        self.assertEqual(_markers(doc), "[PICKUP]")
        self.assertIn("[PICKUP]", doc.remarks)

    def test_mirrored_marker_appends_after_an_existing_customer_note(self):
        doc = _invoice(remarks="Leave with the doorman")

        append_pos_audit_marker(doc, "[PICKUP]", mirror_to_remarks=True)

        self.assertEqual(doc.remarks, "Leave with the doorman\n[PICKUP]")

    def test_repeated_marker_is_not_duplicated(self):
        doc = _invoice()

        append_pos_audit_marker(doc, "[ZERO SHIPPING OVERRIDE]")
        append_pos_audit_marker(doc, "[ZERO SHIPPING OVERRIDE]")

        self.assertEqual(_markers(doc), "[ZERO SHIPPING OVERRIDE]")

    def test_multiple_markers_stack_on_separate_lines(self):
        doc = _invoice()

        append_pos_audit_marker(doc, "[LINE DISCOUNTS]")
        append_pos_audit_marker(doc, "[CUSTOM LINE PRICING]")
        append_pos_audit_marker(doc, "[POLICY PRICE LIST] B2B A")

        self.assertEqual(
            _markers(doc).split("\n"),
            ["[LINE DISCOUNTS]", "[CUSTOM LINE PRICING]", "[POLICY PRICE LIST] B2B A"],
        )

    def test_blank_marker_is_a_no_op(self):
        doc = _invoice()

        append_pos_audit_marker(doc, "")
        append_pos_audit_marker(doc, "   ")
        append_pos_audit_marker(doc, None)

        self.assertEqual(_markers(doc), "")

    def test_marker_write_can_never_raise(self):
        """An audit marker must not be able to fail invoice creation."""

        class _Hostile:
            def __setattr__(self, name, value):
                raise RuntimeError("field is read-only")

            def __getattr__(self, name):
                raise RuntimeError("field is unreadable")

        append_pos_audit_marker(_Hostile(), "[PICKUP]", mirror_to_remarks=True)


class TestMarkerMirrorRouting(unittest.TestCase):
    """``_append_audit_marker`` mirrors only the tags with a surviving reader."""

    def setUp(self):
        try:
            from jarz_pos.services import invoice_creation
        except Exception as exc:  # pragma: no cover - needs the full bench import chain
            self.skipTest(f"invoice_creation import requires a bench context: {exc}")
        self.invoice_creation = invoice_creation

    def test_marker_tag_strips_the_parameterised_tail(self):
        tag = self.invoice_creation._marker_tag
        self.assertEqual(tag("[ORDER PURPOSE] Employee"), "[ORDER PURPOSE]")
        self.assertEqual(tag("[PICKUP]"), "[PICKUP]")
        self.assertEqual(tag("  [LINE DISCOUNTS]  "), "[LINE DISCOUNTS]")
        self.assertEqual(tag("no brackets here"), "no brackets here")
        self.assertEqual(tag("[unterminated"), "[unterminated")
        self.assertEqual(tag(""), "")

    def test_pickup_is_still_mirrored_because_three_consumers_read_it(self):
        doc = _invoice()

        self.invoice_creation._append_audit_marker(doc, "[PICKUP]")

        self.assertIn("[PICKUP]", _markers(doc))
        self.assertIn("[PICKUP]", doc.remarks)

    def test_order_purpose_mirrors_with_its_parameter_intact(self):
        doc = _invoice()

        self.invoice_creation._append_audit_marker(doc, "[ORDER PURPOSE] Employee")

        self.assertIn("[ORDER PURPOSE] Employee", _markers(doc))
        self.assertIn("[ORDER PURPOSE] Employee", doc.remarks)

    def test_policy_price_list_has_no_reader_so_it_leaves_remarks(self):
        doc = _invoice()

        self.invoice_creation._append_audit_marker(doc, "[POLICY PRICE LIST] B2B A")

        self.assertIn("[POLICY PRICE LIST] B2B A", _markers(doc))
        self.assertEqual(doc.remarks, "", "no consumer reads this tag out of remarks")

    def test_every_mirrored_tag_is_a_bare_bracketed_tag(self):
        """Guards the registry against an entry the tag parser could never match."""
        for tag in self.invoice_creation._REMARKS_MIRRORED_TAGS:
            self.assertEqual(self.invoice_creation._marker_tag(tag), tag)


# ===========================================================================
# F-14: channel stamping
# ===========================================================================

class TestStampOrderChannel(unittest.TestCase):
    """A POS order must stop reading as "unknown source" in channel analytics."""

    def test_channel_and_source_type_are_persisted(self):
        doc = _invoice()

        stamped = stamp_order_channel(doc, "flutter")

        self.assertEqual(stamped, "flutter")
        self.assertEqual(getattr(doc, ORDER_CHANNEL_FIELD), "flutter")
        self.assertEqual(doc.woo_source_type, "pos")

    def test_channel_is_trimmed(self):
        doc = _invoice()

        self.assertEqual(stamp_order_channel(doc, "  web  "), "web")
        self.assertEqual(getattr(doc, ORDER_CHANNEL_FIELD), "web")

    def test_existing_woo_attribution_wins_over_the_pos_default(self):
        """An order pushed POS → Woo and pulled back carries real attribution."""
        doc = _invoice(woo_source_type="utm")

        stamp_order_channel(doc, "flutter")

        self.assertEqual(doc.woo_source_type, "utm")
        self.assertEqual(getattr(doc, ORDER_CHANNEL_FIELD), "flutter")

    def test_blank_channel_stamps_nothing(self):
        doc = _invoice()

        self.assertEqual(stamp_order_channel(doc, ""), "")
        self.assertEqual(stamp_order_channel(doc, None), "")
        self.assertEqual(getattr(doc, ORDER_CHANNEL_FIELD), "")
        self.assertEqual(doc.woo_source_type, "")

    def test_stamping_can_never_raise(self):
        class _Hostile:
            def __setattr__(self, name, value):
                raise RuntimeError("field is read-only")

        self.assertEqual(stamp_order_channel(_Hostile(), "flutter"), "")


# ===========================================================================
# F-07: territory precedence
# ===========================================================================

class TestAddressTerritoryPrecedence(unittest.TestCase):
    """city beats state, and the divergence is measurable rather than assumed."""

    def test_precedence_is_city_then_state(self):
        self.assertEqual(invoice_utils.ADDRESS_TERRITORY_FIELD_PRECEDENCE, ("city", "state"))

    def _resolver(self, mapping):
        return lambda value: mapping.get(str(value or "").strip())

    def test_city_wins_when_both_fields_resolve_to_different_territories(self):
        """The governorate in `state` is a group node with no delivery_income."""
        resolver = self._resolver({"New Cairo": "EGNEWCAIRO", "Cairo": "Cairo"})

        with patch.object(invoice_utils, "resolve_territory_name", side_effect=resolver):
            row = {"city": "New Cairo", "state": "Cairo"}
            self.assertEqual(invoice_utils._territory_from_address_row(row), "EGNEWCAIRO")
            detail = describe_address_territory_resolution(row)

        self.assertEqual(detail["resolved_territory"], "EGNEWCAIRO")
        self.assertEqual(detail["resolved_from"], "city")
        self.assertEqual(detail["city_territory"], "EGNEWCAIRO")
        self.assertEqual(detail["state_territory"], "Cairo")
        self.assertTrue(detail["fields_disagree"])

    def test_state_is_used_only_when_city_cannot_resolve(self):
        resolver = self._resolver({"Cairo": "EGCAIRO"})

        with patch.object(invoice_utils, "resolve_territory_name", side_effect=resolver):
            row = {"city": "Some Unknown Compound", "state": "Cairo"}
            self.assertEqual(invoice_utils._territory_from_address_row(row), "EGCAIRO")
            detail = describe_address_territory_resolution(row)

        self.assertEqual(detail["resolved_from"], "state")
        self.assertIsNone(detail["city_territory"])
        self.assertFalse(detail["fields_disagree"], "only one field resolved — nothing to disagree about")

    def test_agreeing_fields_do_not_count_as_a_disagreement(self):
        """POS-written addresses put the Territory in both fields on the Woo lane."""
        resolver = self._resolver({"EGNASRCITY": "EGNASRCITY"})

        with patch.object(invoice_utils, "resolve_territory_name", side_effect=resolver):
            detail = describe_address_territory_resolution(
                {"city": "EGNASRCITY", "state": "EGNASRCITY"}
            )

        self.assertEqual(detail["resolved_territory"], "EGNASRCITY")
        self.assertFalse(detail["fields_disagree"])

    def test_blank_state_makes_the_precedence_a_no_op(self):
        """The POS lane writes `city` and never writes `state` at all."""
        resolver = self._resolver({"EGMAADI": "EGMAADI"})

        with patch.object(invoice_utils, "resolve_territory_name", side_effect=resolver):
            detail = describe_address_territory_resolution({"city": "EGMAADI", "state": ""})

        self.assertEqual(detail["resolved_from"], "city")
        self.assertIsNone(detail["state_territory"])
        self.assertFalse(detail["fields_disagree"])

    def test_unresolvable_address_reports_nothing_rather_than_guessing(self):
        with patch.object(invoice_utils, "resolve_territory_name", return_value=None):
            detail = describe_address_territory_resolution({"city": "Atlantis", "state": "Atlantis"})

        self.assertIsNone(detail["resolved_territory"])
        self.assertIsNone(detail["resolved_from"])
        self.assertFalse(detail["fields_disagree"])

    def test_diagnosis_survives_a_resolver_that_raises(self):
        with patch.object(invoice_utils, "resolve_territory_name", side_effect=RuntimeError("db down")):
            detail = describe_address_territory_resolution({"city": "Maadi", "state": "Cairo"})

        self.assertIsNone(detail["resolved_territory"])

    def test_non_dict_input_is_tolerated(self):
        for bad in (None, "", 0, []):
            detail = describe_address_territory_resolution(bad)
            self.assertIsNone(detail["resolved_territory"])


# ===========================================================================
# F-09 / F-17: territory seeding and is_pos
# ===========================================================================

class TestSetInvoiceFieldsTerritorySeed(unittest.TestCase):
    """An unresolvable territory must not masquerade as a deliberate free delivery."""

    def _call(self, customer_territory):
        invoice_doc = SimpleNamespace()
        customer = SimpleNamespace(
            name="CUST-001", customer_name="Alice", territory=customer_territory
        )
        profile = SimpleNamespace(
            name="POS-1",
            company="Jarz Trading",
            selling_price_list="Standard Selling",
            currency="EGP",
        )
        logger = MagicMock()

        with patch.object(invoice_utils, "frappe") as mock_frappe:
            mock_frappe.utils.today.return_value = "2026-08-19"
            mock_frappe.utils.nowtime.return_value = "12:00:00"
            invoice_utils.set_invoice_fields(invoice_doc, customer, profile, None, logger)

        return invoice_doc, logger

    def test_customer_territory_is_used_when_present(self):
        invoice_doc, _ = self._call("EGMAADI")

        self.assertEqual(invoice_doc.territory, "EGMAADI")

    def test_missing_territory_stays_blank_instead_of_all_territories(self):
        invoice_doc, _ = self._call(None)

        self.assertIsNone(
            invoice_doc.territory,
            "'All Territories' has delivery_income = 0, so seeding it would bill "
            "zero shipping with no trace",
        )

    def test_blank_territory_stays_blank(self):
        invoice_doc, _ = self._call("")

        self.assertIsNone(invoice_doc.territory)

    def test_an_all_territories_customer_is_not_rewritten(self):
        """Left exactly as resolved; STEP 6.A of create_pos_invoice does the shouting."""
        invoice_doc, _ = self._call("All Territories")

        self.assertEqual(invoice_doc.territory, "All Territories")

    def test_is_pos_is_always_set(self):
        """F-17: the POS lane stamps is_pos unconditionally; pos_profile is set too."""
        for territory in ("EGMAADI", None):
            invoice_doc, _ = self._call(territory)
            self.assertEqual(invoice_doc.is_pos, 1)
            self.assertEqual(invoice_doc.pos_profile, "POS-1")


class TestIsUnresolvedTerritory(unittest.TestCase):
    """The tree root carries no delivery income and no branch, so it answers nothing."""

    def test_blank_values_are_unresolved(self):
        for value in (None, "", "   "):
            self.assertTrue(is_unresolved_territory(value), value)

    def test_all_territories_is_unresolved_however_it_is_spelled(self):
        for value in ("All Territories", "all territories", "  ALL TERRITORIES  "):
            self.assertTrue(is_unresolved_territory(value), value)

    def test_a_real_territory_is_resolved(self):
        for value in ("EGMAADI", "Nasr City - مدينه نصر", "Cairo"):
            self.assertFalse(is_unresolved_territory(value), value)


# ===========================================================================
# F-08 / F-09: the review-queue call is actually wired into the pipeline
# ===========================================================================

class TestTerritoryExceptionIsRecorded(unittest.TestCase):
    """The exception call fires rarely in the wild (25 POS-origin mismatches on
    production), so it is pinned here rather than left to chance."""

    def setUp(self):
        try:
            from jarz_pos.services import invoice_creation
        except Exception as exc:  # pragma: no cover - needs the full bench import chain
            self.skipTest(f"invoice_creation import requires a bench context: {exc}")
        self.invoice_creation = invoice_creation

    def test_recorder_is_skipped_when_the_service_is_not_installed(self):
        recorder = MagicMock()
        with patch.object(self.invoice_creation, "_TERRITORY_EXCEPTIONS_AVAILABLE", False), \
             patch.object(self.invoice_creation, "record_invoice_territory_exception", recorder):
            self.invoice_creation._record_territory_exception(_invoice(name="INV-1"), MagicMock())

        recorder.assert_not_called()

    def test_recorder_is_called_with_the_invoice(self):
        recorder = MagicMock(return_value="JTE-0001")
        logger = MagicMock()
        doc = _invoice(name="INV-1")

        with patch.object(self.invoice_creation, "_TERRITORY_EXCEPTIONS_AVAILABLE", True), \
             patch.object(self.invoice_creation, "record_invoice_territory_exception", recorder):
            self.invoice_creation._record_territory_exception(doc, logger)

        recorder.assert_called_once_with(doc)

    def test_a_recorder_that_raises_cannot_break_invoice_creation(self):
        recorder = MagicMock(side_effect=RuntimeError("doctype missing"))

        with patch.object(self.invoice_creation, "_TERRITORY_EXCEPTIONS_AVAILABLE", True), \
             patch.object(self.invoice_creation, "record_invoice_territory_exception", recorder), \
             patch.object(self.invoice_creation, "frappe") as mock_frappe:
            mock_frappe.log_error = MagicMock()
            # Must return normally — a submitted invoice is never undone by a log write.
            self.invoice_creation._record_territory_exception(_invoice(name="INV-1"), MagicMock())

        recorder.assert_called_once()

    def test_create_pos_invoice_reaches_the_recorder(self):
        """End-to-end through the pipeline: STEP 10.5 must actually run."""
        from jarz_pos.services.delivery_promotions import DeliveryPromotionDecision

        invoice_creation = self.invoice_creation
        inv = _invoice(name="TEST-INV-001", territory=None, items=[], taxes=[])
        customer = SimpleNamespace(name="CUST-001", customer_name="Alice", territory=None)
        pos_profile = SimpleNamespace(
            name="Nasr city",
            company="Jarz Company",
            selling_price_list="Standard Selling",
            currency="EGP",
        )
        recorder = MagicMock(return_value="JTE-0001")

        def _set_fields(invoice_doc, customer_doc, profile, delivery_datetime, logger):
            invoice_doc.customer = customer_doc.name
            invoice_doc.pos_profile = profile.name
            invoice_doc.territory = customer_doc.territory

        with patch.object(invoice_creation, "_TERRITORY_EXCEPTIONS_AVAILABLE", True), \
             patch.object(invoice_creation, "record_invoice_territory_exception", recorder), \
             patch.object(invoice_creation, "validate_cart_data", return_value=[{"item_code": "ITEM-1"}]), \
             patch.object(invoice_creation, "_parse_delivery_charges", return_value=[]), \
             patch.object(invoice_creation, "validate_delivery_datetime", return_value=None), \
             patch.object(invoice_creation, "validate_customer", return_value=customer), \
             patch.object(invoice_creation, "validate_pos_profile", return_value=pos_profile), \
             patch.object(
                 invoice_creation,
                 "_process_cart_items",
                 return_value=[{"item_code": "ITEM-1", "qty": 1, "price_list_rate": 100.0}],
             ), \
             patch.object(invoice_creation, "_create_invoice_document", return_value=inv), \
             patch.object(invoice_creation, "set_invoice_fields", side_effect=_set_fields), \
             patch.object(invoice_creation, "resolve_customer_shipping_address", return_value=None), \
             patch.object(invoice_creation, "resolve_order_territory", return_value=None), \
             patch.object(invoice_creation, "add_items_to_invoice"), \
             patch.object(invoice_creation, "_set_initial_state_for_sales_partner"), \
             patch.object(invoice_creation, "_validate_and_calculate_document"), \
             patch.object(invoice_creation, "_save_document"), \
             patch.object(invoice_creation, "_submit_document"), \
             patch.object(invoice_creation, "_maybe_register_online_payment_to_partner"), \
             patch.object(invoice_creation, "_prepare_response", return_value={"success": True}), \
             patch.object(invoice_creation._delivery_promotions, "resolve_delivery_promotion",
                          return_value=DeliveryPromotionDecision()), \
             patch.object(invoice_creation, "frappe") as mock_frappe:
            mock_frappe.local.site = "test-site"
            mock_frappe.logger.return_value = MagicMock()
            mock_frappe.utils.now.return_value = "2026-08-19 12:00:00"
            # The price list must exist; no Territory does, which is the point.
            mock_frappe.db.exists.side_effect = (
                lambda doctype, name=None: doctype == "Price List"
            )
            mock_frappe.get_all.return_value = []
            mock_frappe.get_roles.return_value = ["JARZ Manager"]

            invoice_creation.create_pos_invoice(
                cart_json="[]",
                customer_name=customer.name,
                pos_profile_name=pos_profile.name,
                channel="web",
            )

        recorder.assert_called_once_with(inv)
        # F-14 rides the same pipeline run.
        self.assertEqual(getattr(inv, ORDER_CHANNEL_FIELD), "web")
        self.assertEqual(inv.woo_source_type, "pos")
        # F-09: the unresolved territory is marked, not silently shipped free.
        self.assertIn("[UNRESOLVED TERRITORY]", _markers(inv))


if __name__ == "__main__":
    unittest.main()
