"""What the till is told, what Sentry is told, and which addresses are refused.

Three defects are pinned here:

C1  A blank error. ``frappe.PermissionError`` is raised with NO arguments -- the
    real text is parked in ``frappe.flags.error_message`` -- so ``str(e)`` is "" and
    the cashier saw "Error during document validation: " and nothing else.

C2  One failure filed as two Sentry issues. sentry_sdk's LoggingIntegration turns
    every ERROR record into an event, and into an *exception* event when the record
    carries ``exc_info``. The inner pre-``throw`` ``logger.error`` therefore opened a
    second (message-only, traceback-free) issue for the same failure; the amendment
    path opened a third. Exactly one ERROR record may survive on a failure path.

A   "Selected shipping address is no longer available for this customer" thrown for
    an address that IS available: the resolver deliberately answers with the deduped
    survivor of an equivalent address, and the guard compared names.
"""

from __future__ import annotations

import unittest
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import frappe

from jarz_pos.services import invoice_creation
from jarz_pos.tests.test_invoice_creation_accounting import (
    _InvoiceDocCapture,
    _mock_customer,
    _mock_pos_profile,
)


class _Thrown(Exception):
    """Stand-in for frappe.throw so the message can be inspected."""


def _throwing(message, *args, **kwargs):  # noqa: ARG001
    raise _Thrown(str(message))


# ===========================================================================
# C1 + C2 -- _validate_and_calculate_document
# ===========================================================================

class TestDocumentValidationErrorMessage(unittest.TestCase):
    def _run_validation(self, exception, parked_message=None):
        """Drive _validate_and_calculate_document into ``exception``."""
        invoice_doc = SimpleNamespace(items=[], net_total=0.0, grand_total=0.0)
        invoice_doc.set_missing_values = MagicMock(side_effect=exception)
        invoice_doc.calculate_taxes_and_totals = MagicMock()
        logger = MagicMock()

        with patch.object(invoice_creation, "frappe") as mock_frappe:
            # A real dict, not a Mock: _exception_detail must read a genuine string.
            mock_frappe.flags = {} if parked_message is None else {"error_message": parked_message}
            mock_frappe.throw.side_effect = _throwing
            with self.assertRaises(_Thrown) as caught:
                invoice_creation._validate_and_calculate_document(invoice_doc, logger)

        return str(caught.exception), logger

    def test_argument_less_permission_error_reports_the_parked_message(self):
        # frappe/__init__.py raises PermissionError() with no args and parks the
        # text in frappe.flags.error_message. Pre-fix this produced exactly
        # "Error during document validation: ".
        message, _ = self._run_validation(
            frappe.PermissionError(),
            parked_message="Not permitted: Sales Invoice",
        )

        self.assertIn("Not permitted: Sales Invoice", message)
        self.assertFalse(message.rstrip().endswith(":"), message)

    def test_argument_less_error_without_a_parked_message_names_the_exception_class(self):
        message, _ = self._run_validation(frappe.PermissionError())

        self.assertIn("PermissionError", message)
        self.assertFalse(message.rstrip().endswith(":"), message)

    def test_ordinary_exception_text_is_unchanged(self):
        message, _ = self._run_validation(ValueError("Rate cannot be negative"))

        self.assertEqual(message, "Error during document validation: Rate cannot be negative")

    def test_the_pre_throw_record_is_not_error_level(self):
        # C2: an ERROR record here becomes a SECOND Sentry issue for a failure the
        # outer handler already files with a full traceback.
        message, logger = self._run_validation(ValueError("Rate cannot be negative"))

        logger.error.assert_not_called()
        logger.warning.assert_called_once()
        self.assertIn("Rate cannot be negative", str(logger.warning.call_args.args[0]))
        self.assertIn("Rate cannot be negative", message)


class TestInnerFailurePathsAreNotErrorLevel(unittest.TestCase):
    """Every inner pre-``throw`` log in this module must be a warning."""

    def test_missing_item_logs_a_warning_not_an_error(self):
        logger = MagicMock()
        with patch.object(invoice_creation, "frappe") as mock_frappe:
            mock_frappe.flags = {}
            mock_frappe.db.exists.return_value = False
            mock_frappe.throw.side_effect = _throwing
            with self.assertRaises(_Thrown):
                invoice_creation._process_regular_item({"item_code": "GHOST"}, logger)

        logger.error.assert_not_called()
        logger.warning.assert_called()

    def test_empty_cart_logs_a_warning_not_an_error(self):
        logger = MagicMock()
        with patch.object(invoice_creation, "frappe") as mock_frappe:
            mock_frappe.flags = {}
            mock_frappe.throw.side_effect = _throwing
            with self.assertRaises(_Thrown):
                invoice_creation._process_cart_items([], None, logger)

        logger.error.assert_not_called()
        logger.warning.assert_called()

    def test_document_save_failure_logs_a_warning_not_an_error(self):
        logger = MagicMock()
        invoice_doc = MagicMock()
        invoice_doc.insert.side_effect = frappe.PermissionError()

        with patch.object(invoice_creation, "frappe") as mock_frappe:
            mock_frappe.flags = {"error_message": "Not permitted: Sales Invoice"}
            mock_frappe.throw.side_effect = _throwing
            with self.assertRaises(_Thrown) as caught:
                invoice_creation._save_document(invoice_doc, None, logger)

        logger.error.assert_not_called()
        logger.warning.assert_called()
        self.assertIn("Not permitted: Sales Invoice", str(caught.exception))


class TestOuterErrorHandlerIsTheSingleSentryEvent(unittest.TestCase):
    def test_one_error_record_carrying_exc_info_and_the_real_cause(self):
        logger = MagicMock()

        with patch.object(invoice_creation, "frappe") as mock_frappe:
            mock_frappe.flags = {"error_message": "Not permitted: Sales Invoice"}
            invoice_creation._handle_invoice_creation_error(
                frappe.PermissionError(), "CUST-001", "Main POS", logger
            )

        # Exactly one ERROR record on the whole failure path, and it is the one
        # that carries the traceback -- that is the single Sentry issue.
        self.assertEqual(len(logger.error.call_args_list), 1)
        call = logger.error.call_args
        self.assertTrue(call.kwargs.get("exc_info"))
        self.assertIn("Not permitted: Sales Invoice", str(call.args[0]))
        mock_frappe.log_error.assert_called_once()


# ===========================================================================
# FIX A -- the shipping-address guard, through create_pos_invoice
# ===========================================================================

class TestShippingAddressGuard(unittest.TestCase):
    """Equivalence, not identity -- but a foreign address is still refused."""

    SURVIVOR = {
        "name": "ADDR-SURVIVOR",
        "address_line1": "12 Road",
        "address_line2": "",
        "city": "Giza",
    }
    RAW_ROWS = [
        {
            "name": "ADDR-LEGACY",
            "address_type": "Billing",
            "address_line1": "12 Road",
            "address_line2": "",
            "city": "Giza",
        },
        {
            "name": "ADDR-SURVIVOR",
            "address_type": "Shipping",
            "address_line1": "12 Road",
            "address_line2": "",
            "city": "Giza",
        },
    ]

    def _set_invoice_fields(self, invoice_doc, customer_doc, pos_profile, delivery_datetime, logger):
        invoice_doc.customer = customer_doc.name
        invoice_doc.customer_name = customer_doc.customer_name
        invoice_doc.company = pos_profile.company
        invoice_doc.pos_profile = pos_profile.name
        invoice_doc.territory = customer_doc.territory

    def _append_items(self, invoice_doc, processed_items, logger):
        for item_data in processed_items:
            row = invoice_doc.append("items", {})
            row.item_code = item_data["item_code"]
            row.qty = float(item_data.get("qty", 1) or 1)
            row.price_list_rate = float(
                item_data.get("price_list_rate", item_data.get("rate", 0)) or 0
            )
            row.discount_percentage = float(item_data.get("discount_percentage", 0) or 0)

    def _create(self, *, shipping_address_name, resolved, raw_rows):
        """Run create_pos_invoice with everything but the address logic stubbed."""
        inv = _InvoiceDocCapture()
        customer = _mock_customer(territory=None)
        pos_profile = _mock_pos_profile(name="Nasr city", company="Jarz Company")

        from jarz_pos.services.delivery_promotions import DeliveryPromotionDecision

        patches = [
            patch("jarz_pos.services.invoice_creation.validate_cart_data", return_value=[{"item_code": "ITEM-1"}]),
            patch("jarz_pos.services.invoice_creation._parse_delivery_charges", return_value=[]),
            patch("jarz_pos.services.invoice_creation.validate_delivery_datetime", return_value=None),
            patch("jarz_pos.services.invoice_creation.validate_customer", return_value=customer),
            patch("jarz_pos.services.invoice_creation.validate_pos_profile", return_value=pos_profile),
            patch("jarz_pos.services.invoice_creation._process_cart_items",
                return_value=[{"item_code": "ITEM-1", "qty": 1, "price_list_rate": 600.0}]),
            patch("jarz_pos.services.invoice_creation._create_invoice_document", return_value=inv),
            patch("jarz_pos.services.invoice_creation.set_invoice_fields", side_effect=self._set_invoice_fields),
            patch("jarz_pos.services.invoice_creation.resolve_customer_shipping_address", return_value=resolved),
            patch("jarz_pos.utils.customer_address_utils.get_linked_customer_addresses", return_value=raw_rows),
            patch("jarz_pos.services.invoice_creation.resolve_order_territory", return_value=None),
            patch("jarz_pos.services.invoice_creation.ensure_shipping_address"),
            patch("jarz_pos.services.invoice_creation.add_items_to_invoice", side_effect=self._append_items),
            patch("jarz_pos.services.invoice_creation._set_initial_state_for_sales_partner"),
            patch("jarz_pos.services.invoice_creation._validate_and_calculate_document"),
            patch("jarz_pos.services.invoice_creation._save_document"),
            patch("jarz_pos.services.invoice_creation._submit_document"),
            patch("jarz_pos.services.invoice_creation._maybe_register_online_payment_to_partner"),
            patch("jarz_pos.services.invoice_creation.add_delivery_charges_to_taxes"),
            patch("jarz_pos.services.invoice_creation._delivery_promotions.resolve_delivery_promotion",
                return_value=DeliveryPromotionDecision()),
            patch("jarz_pos.services.invoice_creation._delivery_promotions.apply_delivery_promotion_audit"),
            patch("jarz_pos.services.invoice_creation.frappe"),
        ]
        # 24 patches exceed CPython's 20-block limit for a chained `with`,
        # so they are entered through an ExitStack instead.
        with ExitStack() as stack:
            mock_frappe = [stack.enter_context(_p) for _p in patches][-1]
            mock_frappe.local.site = "test-site"
            mock_frappe.logger.return_value = MagicMock()
            mock_frappe.utils.now.return_value = "2026-05-05 12:00:00"
            # The POS Profile's price list must exist (otherwise pricing throws
            # long before the address guard); no Territory does, which keeps the
            # run on the shipping-address question alone.
            mock_frappe.db.exists.side_effect = (
                lambda doctype, *args, **kwargs: doctype == "Price List"
            )
            mock_frappe.get_all.return_value = []
            mock_frappe.get_roles.return_value = ["JARZ Manager"]
            mock_frappe.flags = {}
            # frappe.throw must actually raise here: with a bare Mock it returns
            # None and the guard's rejection would sail straight past unnoticed.
            mock_frappe.throw.side_effect = _throwing

            kwargs = {}
            if shipping_address_name is not None:
                kwargs["shipping_address_name"] = shipping_address_name

            invoice_creation.create_pos_invoice(
                cart_json="[]",
                customer_name=customer.name,
                pos_profile_name=pos_profile.name,
                **kwargs,
            )

        return inv

    def test_collapsed_duplicate_preference_is_accepted_and_stamps_the_survivor(self):
        # ADDR-LEGACY is a duplicate of ADDR-SURVIVOR (same
        # address_line1|address_line2|city), so the resolver answers with the
        # survivor by design. Pre-fix this threw and the order could not be placed.
        inv = self._create(
            shipping_address_name="ADDR-LEGACY",
            resolved=dict(self.SURVIVOR),
            raw_rows=list(self.RAW_ROWS),
        )

        self.assertEqual(inv.shipping_address_name, "ADDR-SURVIVOR")
        self.assertEqual(inv.customer_address, "ADDR-SURVIVOR")

    def test_exact_preference_is_accepted(self):
        inv = self._create(
            shipping_address_name="ADDR-SURVIVOR",
            resolved=dict(self.SURVIVOR),
            raw_rows=list(self.RAW_ROWS),
        )

        self.assertEqual(inv.shipping_address_name, "ADDR-SURVIVOR")

    def test_address_of_another_customer_is_still_refused(self):
        """LOAD-BEARING. The guard exists to stop this exact case.

        ADDR-FOREIGN is not among this customer's linked addresses, so the
        resolver's answer is a fallback, not an honoured preference. Accepting it
        would stamp the invoice with an address the caller never chose.
        """
        with self.assertRaises(_Thrown) as caught:
            self._create(
                shipping_address_name="ADDR-FOREIGN",
                resolved=dict(self.SURVIVOR),
                raw_rows=list(self.RAW_ROWS),
            )

        self.assertIn("no longer available for this customer", str(caught.exception))

    def test_nonexistent_address_is_still_refused(self):
        with self.assertRaises(_Thrown) as caught:
            self._create(
                shipping_address_name="ADDR-DELETED",
                resolved=dict(self.SURVIVOR),
                raw_rows=[],
            )

        self.assertIn("no longer available for this customer", str(caught.exception))

    def test_customer_with_no_addresses_at_all_is_still_refused(self):
        with self.assertRaises(_Thrown) as caught:
            self._create(
                shipping_address_name="ADDR-LEGACY",
                resolved=None,
                raw_rows=list(self.RAW_ROWS),
            )

        self.assertIn("no longer available for this customer", str(caught.exception))

    def test_a_different_address_of_the_same_customer_is_still_refused(self):
        # Both rows belong to the customer, but they are different places: the
        # resolver fell back rather than honouring the request.
        raw_rows = list(self.RAW_ROWS) + [
            {
                "name": "ADDR-OTHER-STREET",
                "address_type": "Shipping",
                "address_line1": "8 Nile St",
                "address_line2": "",
                "city": "Cairo",
            }
        ]

        with self.assertRaises(_Thrown) as caught:
            self._create(
                shipping_address_name="ADDR-OTHER-STREET",
                resolved=dict(self.SURVIVOR),
                raw_rows=raw_rows,
            )

        self.assertIn("no longer available for this customer", str(caught.exception))

    def test_no_preference_leaves_the_resolver_unchallenged(self):
        # No shipping_address_name at all: the guard must not fire, and whatever
        # the resolver picked is stamped. raw_rows is deliberately empty -- with no
        # preference the guard must not consult the address book at all.
        inv = self._create(
            shipping_address_name=None,
            resolved=dict(self.SURVIVOR),
            raw_rows=[],
        )

        self.assertEqual(inv.shipping_address_name, "ADDR-SURVIVOR")
