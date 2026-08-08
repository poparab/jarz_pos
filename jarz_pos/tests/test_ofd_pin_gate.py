"""Tests for A6 — the Out-for-Delivery pin gate.

Three things are locked down here, and the third is the reason the module exists.

1. **The flag really is a switch.** With ``require_delivery_pin_for_ofd`` off,
   an order with no pin at all produces no blocker — landing this code changes
   nothing until somebody arms it. With it on, the same order is blocked.
2. **"No pin" means what the pin writer means.** A Frappe Float column is
   ``NOT NULL DEFAULT 0``, so an address that was never pinned reads back as
   ``0.0``, not ``NULL``. A naive ``if not lat`` check would pass for a genuine
   pin at longitude 0, and a naive ``is not None`` check would fail to catch the
   unpinned case — which is the whole gate. Both directions are asserted.
3. **BOTH call sites enforce it.** ``_build_ofd_preview_errors`` is duplicated in
   ``api/kanban.py`` and ``api/trips.py``; the spec's warning is that patching one
   leaves the other silently dispatching pinless orders. So the gate is exercised
   *through each function* rather than only against the service, and the two are
   asserted to be calling the same shared object.

Pure ``unittest`` with mocks — no site, no fixtures.
"""

import unittest
from unittest.mock import MagicMock, patch

from jarz_pos.services import ofd_pin_gate

#: The sentence the blocker must contain. Staff need the fix, not the diagnosis:
#: "no pin" sends them to a manager, "add a location pin to the customer's
#: address" sends them to the field they can edit.
FIX_TEXT = "Add a location pin to the customer's address before dispatch"


def _preview(*invoice_names):
    """A shortage preview carrying only the keys the gate reads."""
    return {
        "invoices": list(invoice_names),
        "validation_errors": [],
        "warehouse_mismatches": [],
        "blocking_shortages": [],
        "requires_reason": False,
        "blocking": False,
    }


class OfdPinGateFlagTests(unittest.TestCase):
    """The soft-launch switch."""

    def test_flag_off_produces_no_error_even_with_no_pin(self):
        with patch.object(ofd_pin_gate, "pin_gate_enabled", return_value=False), patch.object(
            ofd_pin_gate, "invoice_has_delivery_pin", return_value=False
        ) as pin_lookup:
            errors = ofd_pin_gate.build_missing_pin_errors(_preview("INV-1", "INV-2"))

        self.assertEqual(errors, [])
        # Not merely "no error": with the gate disarmed it must not even look,
        # so arming it can never be blamed for a slow board.
        pin_lookup.assert_not_called()

    def test_flag_on_blocks_when_address_has_no_pin(self):
        with patch.object(ofd_pin_gate, "pin_gate_enabled", return_value=True), patch.object(
            ofd_pin_gate, "invoice_has_delivery_pin", return_value=False
        ):
            errors = ofd_pin_gate.build_missing_pin_errors(_preview("INV-1"))

        self.assertEqual(len(errors), 1)
        self.assertIn("INV-1", errors[0])
        self.assertIn(FIX_TEXT, errors[0])

    def test_flag_on_passes_when_address_has_a_pin(self):
        with patch.object(ofd_pin_gate, "pin_gate_enabled", return_value=True), patch.object(
            ofd_pin_gate, "invoice_has_delivery_pin", return_value=True
        ):
            errors = ofd_pin_gate.build_missing_pin_errors(_preview("INV-1", "INV-2"))

        self.assertEqual(errors, [])

    def test_one_error_per_pinless_invoice_in_a_trip(self):
        pins = {"INV-1": True, "INV-2": False, "INV-3": False}
        with patch.object(ofd_pin_gate, "pin_gate_enabled", return_value=True), patch.object(
            ofd_pin_gate,
            "invoice_has_delivery_pin",
            side_effect=lambda name: pins[name],
        ):
            errors = ofd_pin_gate.build_missing_pin_errors(_preview("INV-1", "INV-2", "INV-3"))

        self.assertEqual(len(errors), 2)
        self.assertTrue(all(FIX_TEXT in message for message in errors))

    def test_preview_without_invoices_key_is_not_an_error(self):
        """Both API suites hand this function hand-written partial previews."""
        with patch.object(ofd_pin_gate, "pin_gate_enabled", return_value=True), patch.object(
            ofd_pin_gate, "invoice_has_delivery_pin", return_value=False
        ):
            self.assertEqual(ofd_pin_gate.build_missing_pin_errors({}), [])
            self.assertEqual(ofd_pin_gate.build_missing_pin_errors(None), [])
            self.assertEqual(
                ofd_pin_gate.build_missing_pin_errors({"invoices": []}), []
            )

    def test_lookup_failure_fails_open_but_is_logged(self):
        """A schema gap must not stop the whole company dispatching.

        The gate is an operational quality control, not an accounting invariant,
        so a broken lookup degrades to "not enforced". The degradation is logged,
        because a bare ``except: pass`` here is how a v16 rejection once hid for
        a month.
        """
        with patch.object(ofd_pin_gate, "pin_gate_enabled", return_value=True), patch.object(
            ofd_pin_gate, "invoice_has_delivery_pin", side_effect=RuntimeError("no column")
        ), patch.object(ofd_pin_gate, "frappe") as mock_frappe:
            errors = ofd_pin_gate.build_missing_pin_errors(_preview("INV-1"))

        self.assertEqual(errors, [])
        self.assertTrue(mock_frappe.log_error.called)

    def test_flag_read_survives_a_missing_site(self):
        """``pin_gate_enabled`` is called from modules whose ``frappe`` is mocked.

        ``api/kanban`` and ``api/trips`` unit suites patch *their* ``frappe`` but
        not this module's, so a hard failure in the flag read would break tests
        that have nothing to do with the gate.
        """
        with patch.object(ofd_pin_gate, "frappe") as mock_frappe:
            mock_frappe.db.get_single_value.side_effect = RuntimeError("no site")
            self.assertFalse(ofd_pin_gate.pin_gate_enabled())


class PinLookupTests(unittest.TestCase):
    """What counts as "has a pin", against the real lookup."""

    def _run(self, invoice_row, address_row):
        def _get_value(doctype, name, fields, as_dict=False):
            if doctype == "Sales Invoice":
                return invoice_row
            if doctype == "Address":
                return address_row
            return None

        with patch.object(ofd_pin_gate, "frappe") as mock_frappe:
            mock_frappe.db.get_value.side_effect = _get_value
            return ofd_pin_gate.invoice_has_delivery_pin("INV-1")

    def test_valid_pin_passes(self):
        self.assertTrue(
            self._run(
                {"shipping_address_name": "ADDR-1", "customer_address": None},
                {"custom_latitude": 30.0444, "custom_longitude": 31.2357},
            )
        )

    def test_unpinned_address_reads_back_as_zero_and_is_rejected(self):
        """Frappe Floats are NOT NULL DEFAULT 0, so "never pinned" is (0, 0)."""
        self.assertFalse(
            self._run(
                {"shipping_address_name": "ADDR-1", "customer_address": None},
                {"custom_latitude": 0.0, "custom_longitude": 0.0},
            )
        )

    def test_null_coordinates_are_rejected(self):
        self.assertFalse(
            self._run(
                {"shipping_address_name": "ADDR-1", "customer_address": None},
                {"custom_latitude": None, "custom_longitude": None},
            )
        )

    def test_southern_and_western_hemispheres_are_valid_pins(self):
        """A sign check masquerading as an emptiness check would reject these."""
        self.assertTrue(
            self._run(
                {"shipping_address_name": "ADDR-1", "customer_address": None},
                {"custom_latitude": -33.8688, "custom_longitude": -70.6693},
            )
        )

    def test_invoice_with_no_address_at_all_is_blocked(self):
        self.assertFalse(
            self._run(
                {"shipping_address_name": None, "customer_address": None},
                None,
            )
        )

    def test_shipping_address_wins_over_billing_address(self):
        """Gating on ``customer_address`` would gate on the wrong place.

        For a B2B order the billing address is head office and the shipping
        address is the branch that actually receives the delivery.
        """
        seen = {}

        def _get_value(doctype, name, fields, as_dict=False):
            if doctype == "Sales Invoice":
                return {"shipping_address_name": "SHIP-1", "customer_address": "BILL-1"}
            seen["address"] = name
            return {"custom_latitude": 30.0, "custom_longitude": 31.0}

        with patch.object(ofd_pin_gate, "frappe") as mock_frappe:
            mock_frappe.db.get_value.side_effect = _get_value
            ofd_pin_gate.invoice_has_delivery_pin("INV-1")

        self.assertEqual(seen["address"], "SHIP-1")

    def test_billing_address_is_the_fallback(self):
        seen = {}

        def _get_value(doctype, name, fields, as_dict=False):
            if doctype == "Sales Invoice":
                return {"shipping_address_name": "", "customer_address": "BILL-1"}
            seen["address"] = name
            return {"custom_latitude": 30.0, "custom_longitude": 31.0}

        with patch.object(ofd_pin_gate, "frappe") as mock_frappe:
            mock_frappe.db.get_value.side_effect = _get_value
            self.assertTrue(ofd_pin_gate.invoice_has_delivery_pin("INV-1"))

        self.assertEqual(seen["address"], "BILL-1")


class BothCallSitesEnforceTheGateTests(unittest.TestCase):
    """The point of A6: neither dispatch path may bypass the gate.

    ``_build_ofd_preview_errors`` exists twice, and both copies feed a real
    blocker path: ``api.kanban.update_invoice_state`` turns its result into a
    ``_failure`` response, and ``api.trips.send_trip_for_delivery`` turns its
    result into a ``frappe.throw``. Testing only the service would leave the
    "somebody patched one copy" failure completely uncovered — which is exactly
    how this bug is specified to happen.
    """

    def setUp(self):
        from jarz_pos.api import kanban, trips

        self.kanban = kanban
        self.trips = trips

    def test_kanban_call_site_enforces_the_gate(self):
        with patch.object(ofd_pin_gate, "pin_gate_enabled", return_value=True), patch.object(
            ofd_pin_gate, "invoice_has_delivery_pin", return_value=False
        ):
            errors = self.kanban._build_ofd_preview_errors(_preview("INV-1"))

        self.assertTrue(any(FIX_TEXT in message for message in errors), errors)

    def test_trips_call_site_enforces_the_gate(self):
        with patch.object(ofd_pin_gate, "pin_gate_enabled", return_value=True), patch.object(
            ofd_pin_gate, "invoice_has_delivery_pin", return_value=False
        ):
            errors = self.trips._build_ofd_preview_errors(_preview("INV-1"))

        self.assertTrue(any(FIX_TEXT in message for message in errors), errors)

    def test_neither_call_site_adds_the_error_when_the_flag_is_off(self):
        with patch.object(ofd_pin_gate, "pin_gate_enabled", return_value=False), patch.object(
            ofd_pin_gate, "invoice_has_delivery_pin", return_value=False
        ):
            kanban_errors = self.kanban._build_ofd_preview_errors(_preview("INV-1"))
            trip_errors = self.trips._build_ofd_preview_errors(_preview("INV-1"))

        self.assertEqual(kanban_errors, [])
        self.assertEqual(trip_errors, [])

    def test_both_call_sites_share_one_implementation(self):
        """No second copy of the rule may creep back in.

        Both modules must be holding the *same* module object, so a fix applied
        once applies to both. If someone re-inlines the check into either API
        module this assertion still passes but the two tests above would begin to
        diverge — which is why all three exist together.
        """
        self.assertIs(self.kanban.ofd_pin_gate, ofd_pin_gate)
        self.assertIs(self.trips.ofd_pin_gate, ofd_pin_gate)

    def test_existing_preview_errors_are_preserved_alongside_the_gate(self):
        """The gate appends; it must not swallow shortage or warehouse blockers."""
        preview = _preview("INV-1")
        preview["validation_errors"] = ["Invoice INV-1 must be submitted"]
        preview["blocking_shortages"] = [
            {
                "invoice_names": ["INV-1"],
                "item_code": "ITEM-1",
                "required_qty": 2,
                "warehouse": "Main",
                "available_qty": 0,
            }
        ]

        with patch.object(ofd_pin_gate, "pin_gate_enabled", return_value=True), patch.object(
            ofd_pin_gate, "invoice_has_delivery_pin", return_value=False
        ):
            kanban_errors = self.kanban._build_ofd_preview_errors(preview)
            trip_errors = self.trips._build_ofd_preview_errors(preview)

        for errors in (kanban_errors, trip_errors):
            self.assertTrue(any("must be submitted" in message for message in errors))
            self.assertTrue(any("ITEM-1" in message for message in errors))
            self.assertTrue(any(FIX_TEXT in message for message in errors))


if __name__ == "__main__":
    unittest.main()
