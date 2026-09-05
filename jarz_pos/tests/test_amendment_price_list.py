"""Price-list survival across a POS invoice amendment.

Regression cover for the production incident on order 17206 /
``ACC-SINV-2026-18128``: a cashier rang up a B2B order at 92 EGP per line using the
"B2B Selling" override, then amended it from delivery to pickup. Every line silently
reverted to the 160 EGP Standard Selling rate, and four further amendments produced
byte-identical 1120 EGP invoices — the customer was overcharged EGP 476 and the
operator had no way to get the agreed price back.

Two independent backend defects produced that, and both are pinned here.

Cause 1 — the override was never persisted.
    ``create_pos_invoice`` assigned ``selling_price_list`` before validation, but these
    are ``is_pos=1`` documents and ERPNext's own ``SalesInvoice.set_pos_fields``
    overwrites that column from ``customer.default_price_list ->
    customer_group.default_price_list -> pos_profile.selling_price_list``. The header
    therefore always lied, and the amendment draft loader (Flutter's
    ``pos_notifier.dart`` and, server-side, ``_resolve_amendment_price_list``) reads the
    header. ``TestSellingPriceListPersistedAfterSubmit`` pins the post-submit re-stamp.

Cause 2 — the amendment endpoint dropped the price list.
    ``submit_invoice_amendment`` had no ``price_list`` parameter, so Frappe silently
    discarded the one the client sends, and the downstream ``create_pos_invoice`` call
    omitted it too. Every replacement re-priced from the POS Profile default.
    ``TestAmendmentPriceListResolution`` and ``TestSubmitInvoiceAmendmentForwardsPriceList``
    pin the whole thread.

Everything here is mock-based and runs without a live site (CI logic gate).
"""

import json
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_PROFILE = "Nasr city"
_PROFILE_DEFAULT_PL = "Standard Selling"
_OVERRIDE_PL = "B2B Selling"


class _FakeSourceInvoice:
    """A submitted POS Sales Invoice, minimal but amendment-shaped."""

    def __init__(self, **overrides):
        self._data = {
            "name": "ACC-SINV-2026-18128",
            "docstatus": 1,
            "grand_total": 500.0,
            "is_return": 0,
            "custom_sales_invoice_state": "Received",
            "sales_invoice_state": "Received",
            "custom_delivery_date": None,
            "custom_delivery_time_from": None,
            "custom_delivery_duration": None,
            "custom_delivery_trip": None,
            "custom_is_pickup": False,
            "custom_payment_method": "Cash",
            "custom_kanban_profile": _PROFILE,
            "pos_profile": _PROFILE,
            "customer": "Test B2B Customer",
            "sales_partner": None,
            "woo_order_id": None,
            "territory": None,
            "selling_price_list": _OVERRIDE_PL,
            "custom_order_purpose": "B2B Supply",
            "custom_commercial_policy": None,
            "custom_policy_reason": None,
            "items": [],
        }
        self._data.update(overrides)
        self.name = self._data["name"]
        self.flags = SimpleNamespace(ignore_permissions=False, ignore_woo_outbound=False)

    def __getattr__(self, key):
        try:
            return self.__dict__["_data"][key]
        except KeyError:
            raise AttributeError(key)

    def get(self, key, default=None):
        return self._data.get(key, default)

    def cancel(self):
        self._data["docstatus"] = 2

    def reload(self):
        return None


def _mock_manager_frappe(profile_default_price_list=_PROFILE_DEFAULT_PL):
    """A ``frappe`` stand-in for ``jarz_pos.api.manager``.

    ``db.get_value`` is a real function rather than a bare MagicMock on purpose: the
    price-list resolver compares the invoice's list against the POS Profile default, and
    a MagicMock return value stringifies into a name that can never match, which would
    make every assertion here pass for the wrong reason.
    """
    mf = MagicMock()
    mf._ = lambda text: text
    mf.parse_json.side_effect = json.loads
    mf.db.sql.return_value = [[1]]  # advisory lock acquired
    mf.db.savepoint.return_value = None
    mf.session.user = "cashier@example.com"
    mf.local.site = "frontend"
    mf.logger.return_value = MagicMock()
    mf.utils.now.return_value = "2026-09-05 12:00:00"
    mf.utils.flt.side_effect = lambda value=0, precision=None: float(value or 0)

    def _get_value(*args, **kwargs):
        if args and args[0] == "POS Profile":
            return profile_default_price_list
        return None

    mf.db.get_value.side_effect = _get_value
    return mf


def _run_amendment(source_invoice, mock_frappe, **job_kwargs):
    """Drive ``_run_invoice_amendment_job`` to a successful replacement.

    Returns the ``_create_amendment_invoice`` mock so the caller can inspect exactly
    what the creation service was asked for.
    """
    creation = MagicMock(return_value={"invoice_name": "ACC-SINV-2026-18129"})
    cart = json.dumps([{"item_code": "MOLTEN-L", "qty": 2, "rate": 250}])

    with (
        patch("jarz_pos.api.manager.frappe", mock_frappe),
        patch("jarz_pos.api.manager._create_amendment_invoice", creation),
        patch("jarz_pos.api.manager._find_existing_amendment_invoice", return_value=None),
        patch("jarz_pos.api.manager.get_invoice_amendment_eligibility", return_value={"can_amend": True}),
        patch("jarz_pos.api.manager._find_submitted_payment_entries", return_value=[]),
        patch("jarz_pos.api.manager.frappe.get_doc", return_value=source_invoice),
        patch("jarz_pos.api.manager.assert_pos_profile_matches_territory", return_value=None),
        patch("jarz_pos.api.manager.resolve_order_territory", return_value=None),
        patch("jarz_pos.api.manager.read_invoice_shipping_income", return_value=0.0),
        patch("jarz_pos.api.manager._mark_source_invoice_as_amended", return_value=None),
        patch("jarz_pos.api.manager._add_invoice_audit_comment", return_value=None),
        patch("jarz_pos.api.manager._carry_over_invoice_notes", return_value=None),
        patch("jarz_pos.api.manager._build_invoice_amendment_response", return_value={"success": True}),
        patch("jarz_pos.api.manager._temporary_invoice_creation_form_context", MagicMock()),
    ):
        from jarz_pos.api.manager import _run_invoice_amendment_job

        result = _run_invoice_amendment_job(
            invoice_id=source_invoice.name,
            request_id="test-req-price-list",
            cart_json=cart,
            pos_profile_name=_PROFILE,
            **job_kwargs,
        )

    return creation, result


# ---------------------------------------------------------------------------
# 1. The resolver itself
# ---------------------------------------------------------------------------

class TestResolveAmendmentPriceList(unittest.TestCase):
    """Precedence: explicit argument -> the invoice's own override -> None."""

    def _resolve(self, requested, *, source_price_list, profile_default=_PROFILE_DEFAULT_PL):
        from jarz_pos.api.manager import _resolve_amendment_price_list

        inv = _FakeSourceInvoice(selling_price_list=source_price_list)
        with patch("jarz_pos.api.manager.frappe", _mock_manager_frappe(profile_default)):
            return _resolve_amendment_price_list(inv, requested, pos_profile_name=_PROFILE)

    def test_explicit_argument_wins_over_source_override(self):
        self.assertEqual(
            self._resolve("B2B Tier 2", source_price_list=_OVERRIDE_PL),
            "B2B Tier 2",
        )

    def test_source_override_is_carried_when_no_argument(self):
        self.assertEqual(self._resolve(None, source_price_list=_OVERRIDE_PL), _OVERRIDE_PL)

    def test_profile_default_is_not_an_override(self):
        """An ordinary retail order must resolve to None, i.e. today's behaviour."""
        self.assertIsNone(self._resolve(None, source_price_list=_PROFILE_DEFAULT_PL))

    def test_blank_source_price_list_resolves_to_none(self):
        self.assertIsNone(self._resolve(None, source_price_list=None))

    def test_whitespace_only_argument_falls_through_to_source(self):
        self.assertEqual(self._resolve("   ", source_price_list=_OVERRIDE_PL), _OVERRIDE_PL)


# ---------------------------------------------------------------------------
# 2. The job hands the resolved list to the creation service
# ---------------------------------------------------------------------------

class TestAmendmentPriceListResolution(unittest.TestCase):
    """``_run_invoice_amendment_job`` must forward the price list it resolved."""

    def test_override_survives_a_pickup_only_amendment(self):
        """(a) Changing delivery -> pickup must not reprice the order.

        This is the 17206 failure in one assertion: the only edit is the pickup flag,
        and the replacement must still be created against "B2B Selling".
        """
        source = _FakeSourceInvoice(selling_price_list=_OVERRIDE_PL)
        creation, _ = _run_amendment(source, _mock_manager_frappe(), pickup=True)

        self.assertTrue(creation.called, "The replacement invoice was never created")
        self.assertEqual(creation.call_args.kwargs.get("price_list"), _OVERRIDE_PL)
        # The pickup flip itself still happened (10th positional argument).
        self.assertTrue(creation.call_args.args[8])

    def test_explicit_price_list_argument_is_honoured(self):
        """(b) An operator switching lists on the amendment beats the stored one."""
        source = _FakeSourceInvoice(selling_price_list=_OVERRIDE_PL)
        creation, _ = _run_amendment(source, _mock_manager_frappe(), price_list="B2B Tier 2")

        self.assertEqual(creation.call_args.kwargs.get("price_list"), "B2B Tier 2")

    def test_order_without_override_forwards_none(self):
        """(c) A Standard/B2C order — the overwhelming majority — is untouched."""
        source = _FakeSourceInvoice(selling_price_list=_PROFILE_DEFAULT_PL)
        creation, _ = _run_amendment(source, _mock_manager_frappe())

        self.assertIsNone(creation.call_args.kwargs.get("price_list"))

    def test_order_purpose_still_carried_alongside_the_price_list(self):
        """The policy carry-over that already existed must not have been displaced."""
        source = _FakeSourceInvoice(selling_price_list=_OVERRIDE_PL)
        creation, _ = _run_amendment(source, _mock_manager_frappe())

        self.assertEqual(creation.call_args.kwargs.get("order_purpose"), "B2B Supply")
        self.assertEqual(creation.call_args.kwargs.get("price_list"), _OVERRIDE_PL)


# ---------------------------------------------------------------------------
# 3. The idempotency key
# ---------------------------------------------------------------------------

class TestAmendmentRequestId(unittest.TestCase):
    """A price-list change is a different request, not a duplicate of the last one."""

    def _request_id(self, **overrides):
        from jarz_pos.api.manager import _build_invoice_amendment_request_id

        kwargs = {
            "invoice_id": "ACC-SINV-2026-18128",
            "cart_json": json.dumps([{"item_code": "MOLTEN-L", "qty": 2, "rate": 250}]),
            "pos_profile_name": _PROFILE,
            "customer_name": "Test B2B Customer",
            "shipping_address_name": None,
            "required_delivery_datetime": None,
            "delivery_end_datetime": None,
            "sales_partner": None,
            "payment_type": None,
            "pickup": None,
            "payment_method": "Cash",
        }
        kwargs.update(overrides)
        with patch("jarz_pos.api.manager.frappe", _mock_manager_frappe()):
            return _build_invoice_amendment_request_id(**kwargs)

    def test_price_list_change_yields_a_distinct_request_id(self):
        """(d) The retry the operator kept attempting must not be swallowed.

        On 17206 the same cart was resubmitted four times; if the price list did not
        enter the digest, the corrected attempt would hash to the already-processed
        request id of the retail-priced one.
        """
        baseline = self._request_id()
        overridden = self._request_id(price_list=_OVERRIDE_PL)
        other = self._request_id(price_list="B2B Tier 2")

        self.assertNotEqual(baseline, overridden)
        self.assertNotEqual(overridden, other)

    def test_same_price_list_is_stable(self):
        self.assertEqual(
            self._request_id(price_list=_OVERRIDE_PL),
            self._request_id(price_list=_OVERRIDE_PL),
        )

    def test_absent_price_list_does_not_change_the_existing_id(self):
        """No-override payloads must keep the exact id they produced before this key."""
        self.assertEqual(self._request_id(), self._request_id(price_list=None))
        self.assertEqual(self._request_id(), self._request_id(price_list="  "))

    def test_caller_supplied_idempotency_key_still_wins(self):
        self.assertEqual(
            self._request_id(price_list=_OVERRIDE_PL, provided_idempotency_key="client-key-1"),
            "client-key-1",
        )


# ---------------------------------------------------------------------------
# 4. The whitelisted endpoint accepts and forwards the parameter
# ---------------------------------------------------------------------------

class TestSubmitInvoiceAmendmentForwardsPriceList(unittest.TestCase):
    """Frappe drops whitelisted kwargs the signature does not declare."""

    def _submit(self, **kwargs):
        from jarz_pos.api.manager import submit_invoice_amendment

        source = _FakeSourceInvoice()
        mf = _mock_manager_frappe()
        mf.get_doc.return_value = source
        mf.has_permission.return_value = True
        mf.get_roles.return_value = ["JARZ Manager"]
        mf.enqueue.return_value = {"success": True}

        with (
            patch("jarz_pos.api.manager.frappe", mf),
            patch("jarz_pos.api.manager._ensure_manager_dashboard_access"),
            patch("jarz_pos.api.manager._ensure_profile_scoped_invoice_access"),
            patch("jarz_pos.api.manager._find_existing_amendment_invoice", return_value=None),
            patch("jarz_pos.api.manager.get_invoice_amendment_eligibility", return_value={"can_amend": True}),
        ):
            submit_invoice_amendment(
                invoice_id=source.name,
                cart_json=json.dumps([{"item_code": "MOLTEN-L", "qty": 2, "rate": 250}]),
                **kwargs,
            )
        return mf.enqueue

    def test_price_list_reaches_the_queued_job(self):
        enqueue = self._submit(price_list=_OVERRIDE_PL)
        self.assertEqual(enqueue.call_args.kwargs["price_list"], _OVERRIDE_PL)

    def test_price_list_defaults_to_none(self):
        enqueue = self._submit()
        self.assertIsNone(enqueue.call_args.kwargs["price_list"])

    def test_price_list_changes_the_job_id(self):
        without = self._submit().call_args.kwargs["job_id"]
        with_pl = self._submit(price_list=_OVERRIDE_PL).call_args.kwargs["job_id"]
        self.assertNotEqual(without, with_pl)


# ---------------------------------------------------------------------------
# 5. Persistence: the header must survive ERPNext's set_pos_fields
# ---------------------------------------------------------------------------

class _InvoiceCapture:
    """Captures what ``create_pos_invoice`` writes onto the Sales Invoice."""

    def __init__(self):
        self.name = "ACC-SINV-2026-18128"
        self.selling_price_list = None
        self.remarks = ""
        self.custom_pos_audit_markers = ""
        self.items = []
        self.taxes = []
        self.payments = []
        self.docstatus = 0
        self.update_stock = 1
        self.status = "Draft"
        self.net_total = 0.0
        self.grand_total = 0.0
        self.db_set_calls = []

    def append(self, table, row=None):
        item = SimpleNamespace(**(row or {}))
        item.get = lambda key, default=None: getattr(item, key, default)
        getattr(self, table).append(item)
        return item

    def set(self, field, value):
        setattr(self, field, value)

    def get(self, field, default=None):
        return getattr(self, field, default)

    def save(self, **kwargs):
        pass

    def submit(self):
        self.docstatus = 1

    def run_method(self, method):
        pass

    def db_set(self, field, value, **kwargs):
        self.db_set_calls.append((field, value, kwargs))
        setattr(self, field, value)


class TestSellingPriceListPersistedAfterSubmit(unittest.TestCase):
    """``_persist_selling_price_list`` re-stamps the column ERPNext clobbered."""

    def _create(self, price_list, *, clobber_to):
        """Run create_pos_invoice, simulating set_pos_fields inside submit."""
        inv = _InvoiceCapture()
        customer = MagicMock(name="customer")
        customer.name = "Test B2B Customer"
        customer.customer_name = "Test B2B Customer"
        customer.territory = "Cairo"
        pos_profile = MagicMock()
        pos_profile.name = _PROFILE
        pos_profile.company = "Test Company"
        pos_profile.selling_price_list = _PROFILE_DEFAULT_PL
        pos_profile.currency = "EGP"

        def _clobber(invoice_doc, logger):
            # What erpnext/accounts/doctype/sales_invoice/sales_invoice.py does in
            # set_pos_fields for every is_pos=1 document.
            invoice_doc.selling_price_list = clobber_to
            invoice_doc.docstatus = 1

        with (
            patch("jarz_pos.services.invoice_creation.validate_cart_data",
                  return_value=[{"item_code": "MOLTEN-L", "qty": 2}]),
            patch("jarz_pos.services.invoice_creation._parse_delivery_charges", return_value=[]),
            patch("jarz_pos.services.invoice_creation.validate_delivery_datetime", return_value=None),
            patch("jarz_pos.services.invoice_creation.validate_customer", return_value=customer),
            patch("jarz_pos.services.invoice_creation.validate_pos_profile", return_value=pos_profile),
            patch("jarz_pos.services.invoice_creation._process_cart_items",
                  return_value=[{"item_code": "MOLTEN-L", "qty": 2, "rate": 92.0, "price_list_rate": 92.0}]),
            patch("jarz_pos.services.invoice_creation._create_invoice_document", return_value=inv),
            patch("jarz_pos.services.invoice_creation.set_invoice_fields"),
            patch("jarz_pos.services.invoice_creation.add_items_to_invoice"),
            patch("jarz_pos.services.invoice_creation._set_initial_state_for_sales_partner"),
            patch("jarz_pos.services.invoice_creation._validate_and_calculate_document"),
            patch("jarz_pos.services.invoice_creation._save_document"),
            patch("jarz_pos.services.invoice_creation._submit_document", side_effect=_clobber),
            patch("jarz_pos.services.invoice_creation._record_territory_exception"),
            patch("jarz_pos.services.invoice_creation._maybe_register_online_payment_to_partner"),
            patch("jarz_pos.services.invoice_creation._delivery_promotions.resolve_delivery_promotion") as promo,
            patch("jarz_pos.services.invoice_creation._delivery_promotions.apply_delivery_promotion_audit"),
            patch("jarz_pos.services.invoice_creation.frappe") as mf,
        ):
            from jarz_pos.services.delivery_promotions import DeliveryPromotionDecision

            promo.return_value = DeliveryPromotionDecision(
                matched=False,
                rule_name=None,
                rule_type=None,
                merchandise_subtotal=0.0,
                item_qty=0.0,
                suppress_shipping_income=False,
                suppress_legacy_delivery_charges=False,
            )
            mf.local.site = "test-site"
            mf.logger.return_value = MagicMock()
            mf.utils.now.return_value = "2026-09-05 12:00:00"
            mf.db.exists.return_value = True
            mf.get_roles.return_value = ["JARZ Manager"]
            mf.session.user = "manager@example.com"
            mf.get_all.return_value = []

            from jarz_pos.services.invoice_creation import create_pos_invoice

            create_pos_invoice(
                cart_json="[]",
                customer_name=customer.name,
                pos_profile_name=pos_profile.name,
                price_list=price_list,
                # Pickup mirrors the incident and short-circuits the shipping-income
                # block, which has no bearing on the price list under test.
                pickup=True,
            )

        return inv

    def test_override_is_restamped_after_erpnext_clobbers_it(self):
        """The exact ACC-SINV-2026-18128 shape: rates at 92, header reading retail."""
        inv = self._create(_OVERRIDE_PL, clobber_to=_PROFILE_DEFAULT_PL)

        self.assertEqual(
            inv.selling_price_list,
            _OVERRIDE_PL,
            "set_pos_fields' overwrite must not be the value that survives",
        )
        self.assertIn(
            ("selling_price_list", _OVERRIDE_PL),
            [(field, value) for field, value, _ in inv.db_set_calls],
            "The correction must be written to the DB, not only to the in-memory doc",
        )

    def test_restamp_is_a_post_submit_write(self):
        """db_set, not a pre-validation assignment — otherwise ERPNext wins again."""
        inv = self._create(_OVERRIDE_PL, clobber_to=_PROFILE_DEFAULT_PL)

        self.assertEqual(inv.docstatus, 1)
        self.assertTrue(inv.db_set_calls)
        # update_modified stays False so the post-submit steps that follow do not hit
        # a TimestampMismatchError on this same in-memory document.
        self.assertFalse(inv.db_set_calls[0][2].get("update_modified", True))

    def test_no_override_writes_nothing(self):
        """A Standard order must be byte-identical to today: no extra DB write."""
        inv = self._create(None, clobber_to=_PROFILE_DEFAULT_PL)

        self.assertEqual(inv.db_set_calls, [])
        self.assertEqual(inv.selling_price_list, _PROFILE_DEFAULT_PL)

    def test_matching_price_list_writes_nothing(self):
        """When ERPNext happens to land on the right list there is nothing to fix."""
        inv = self._create(_OVERRIDE_PL, clobber_to=_OVERRIDE_PL)

        self.assertEqual(inv.db_set_calls, [])
        self.assertEqual(inv.selling_price_list, _OVERRIDE_PL)


if __name__ == "__main__":
    unittest.main()
