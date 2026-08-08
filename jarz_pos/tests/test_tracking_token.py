"""Tests for the customer tracking token: minting, idempotency and the URL helper.

The invariants here are the ones whose absence is a *security* problem rather
than a broken feature:

* **Opaque, never derived.** A token that encodes or hashes the invoice name lets
  one customer walk the order book. Asserted directly: the invoice name must not
  appear in the token, and two invoices must never produce the same one.
* **Minted once, never regenerated.** The customer already has the link. A second
  mint on a card dragged out → back → out would silently kill a link that is
  already in somebody's WhatsApp.
* **Written without re-entering the document lifecycle.** The mint runs *inside*
  ``on_update_after_submit``; a ``doc.save()`` there re-runs the whole hook chain.
  The write must be ``frappe.db.set_value(..., update_modified=False)``.
* **Cannot fail a dispatch.** A tracking link is a courtesy. Every failure path
  returns ``None`` and logs.

Pure ``unittest`` with mocks — no site, no fixtures.
"""

import json
import os
import unittest
from unittest.mock import MagicMock, patch

import jarz_pos
from jarz_pos.events import sales_invoice as si_events
from jarz_pos.services import tracking
from jarz_pos.utils import cleanup

INVOICE = "ACC-SINV-2026-00042"

FIXTURE_PATH = os.path.join(
    os.path.dirname(os.path.abspath(jarz_pos.__file__)), "fixtures", "custom_field.json"
)


class _FakeMeta:
    def __init__(self, fields=(tracking.TOKEN_FIELD,)):
        self._fields = set(fields)

    def get_field(self, fieldname):
        if fieldname not in self._fields:
            return None
        return {"fieldname": fieldname, "allow_on_submit": 1}


def _invoice(token=None, name=INVOICE):
    data = {tracking.TOKEN_FIELD: token}
    doc = MagicMock()
    doc.name = name
    doc.docstatus = 1
    doc._data = data
    doc.get.side_effect = lambda key, default=None: data.get(key, default)
    doc.set.side_effect = lambda key, value: data.__setitem__(key, value)
    return doc


class TokenGenerationTests(unittest.TestCase):
    def test_token_is_url_safe_and_long_enough(self):
        token = tracking.new_token()
        # 16 bytes of entropy base64url-encodes to 22 characters.
        self.assertGreaterEqual(len(token), 22)
        allowed = set(
            "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_"
        )
        self.assertTrue(set(token) <= allowed, token)

    def test_token_is_not_derived_from_anything(self):
        """The enumeration guard, asserted rather than assumed."""
        tokens = {tracking.new_token() for _ in range(200)}
        self.assertEqual(len(tokens), 200)
        for token in tokens:
            self.assertNotIn(INVOICE, token)
            self.assertNotIn("SINV", token)


class MintingTests(unittest.TestCase):
    def _mint(self, doc, *, stored=None, meta=None, set_value=None):
        with patch.object(tracking, "frappe") as mock_frappe:
            mock_frappe.get_meta.return_value = meta if meta is not None else _FakeMeta()
            mock_frappe.db.get_value.return_value = stored
            if set_value is not None:
                mock_frappe.db.set_value.side_effect = set_value
            token = tracking.ensure_tracking_token(doc)
            return token, mock_frappe

    def test_mints_when_the_invoice_has_none(self):
        doc = _invoice(token=None)
        token, mock_frappe = self._mint(doc)

        self.assertTrue(token)
        mock_frappe.db.set_value.assert_called_once()
        args, kwargs = mock_frappe.db.set_value.call_args
        self.assertEqual(args[0], "Sales Invoice")
        self.assertEqual(args[1], INVOICE)
        self.assertEqual(args[2], tracking.TOKEN_FIELD)
        self.assertEqual(args[3], token)
        # Leaving `modified` alone keeps any concurrently-loaded copy of the
        # invoice saveable rather than handing it a TimestampMismatchError.
        self.assertFalse(kwargs.get("update_modified", True))
        # And the in-memory doc is updated so the caller sees it too.
        self.assertEqual(doc._data[tracking.TOKEN_FIELD], token)

    def test_existing_token_is_returned_untouched(self):
        doc = _invoice(token="already-minted-token")
        token, mock_frappe = self._mint(doc)

        self.assertEqual(token, "already-minted-token")
        mock_frappe.db.set_value.assert_not_called()

    def test_token_written_by_another_worker_is_adopted_not_replaced(self):
        """The in-memory doc can predate a concurrent mint."""
        doc = _invoice(token=None)
        token, mock_frappe = self._mint(doc, stored="minted-elsewhere")

        self.assertEqual(token, "minted-elsewhere")
        mock_frappe.db.set_value.assert_not_called()

    def test_repeated_calls_never_change_the_token(self):
        doc = _invoice(token=None)
        first, _ = self._mint(doc)
        # Second pass sees the token the first one put on the doc.
        second, mock_frappe = self._mint(doc)

        self.assertEqual(first, second)
        mock_frappe.db.set_value.assert_not_called()

    def test_unmigrated_site_mints_nothing(self):
        """A deployment ahead of `bench migrate` must not write a phantom field."""
        doc = _invoice(token=None)
        token, mock_frappe = self._mint(doc, meta=_FakeMeta(fields=()))

        self.assertIsNone(token)
        mock_frappe.db.set_value.assert_not_called()

    def test_a_write_failure_never_propagates(self):
        """This runs inside the dispatch transaction; it must not break it."""
        doc = _invoice(token=None)
        token, mock_frappe = self._mint(doc, set_value=RuntimeError("db is on fire"))

        self.assertIsNone(token)
        self.assertTrue(mock_frappe.log_error.called)

    def test_invoice_with_no_name_is_ignored(self):
        doc = _invoice(token=None, name="")
        token, mock_frappe = self._mint(doc)

        self.assertIsNone(token)
        mock_frappe.db.set_value.assert_not_called()


class UrlHelperTests(unittest.TestCase):
    """``get_tracking_url`` is the seam the WooCommerce app consumes."""

    def test_builds_an_absolute_url_from_the_route_prefix(self):
        with patch.object(tracking, "frappe") as mock_frappe, patch.object(
            tracking, "get_url", return_value="https://erp.orderjarz.com/"
        ):
            mock_frappe.get_meta.return_value = _FakeMeta()
            mock_frappe.db.get_value.return_value = "tok3n-value"
            url = tracking.get_tracking_url(INVOICE)

        self.assertEqual(url, "https://erp.orderjarz.com/track/tok3n-value")
        self.assertTrue(url.startswith("https://"))
        # The order id must not be reconstructable from the link.
        self.assertNotIn(INVOICE, url)

    def test_returns_none_when_the_order_has_no_token(self):
        """None, not a broken URL: the caller can say "not dispatched yet"."""
        with patch.object(tracking, "frappe") as mock_frappe, patch.object(
            tracking, "get_url", return_value="https://erp.orderjarz.com"
        ):
            mock_frappe.get_meta.return_value = _FakeMeta()
            mock_frappe.db.get_value.return_value = None
            self.assertIsNone(tracking.get_tracking_url(INVOICE))

    def test_reading_a_url_never_mints_by_default(self):
        """A read stays a read; the mint point is the OFD transition."""
        with patch.object(tracking, "frappe") as mock_frappe, patch.object(
            tracking, "get_url", return_value="https://erp.orderjarz.com"
        ):
            mock_frappe.get_meta.return_value = _FakeMeta()
            mock_frappe.db.get_value.return_value = None
            tracking.get_tracking_url(INVOICE)

        mock_frappe.db.set_value.assert_not_called()

    def test_route_prefix_matches_the_hooks_rule(self):
        """The URL builder and hooks.website_route_rules must agree."""
        from jarz_pos import hooks

        rules = getattr(hooks, "website_route_rules", [])
        from_routes = {rule.get("from_route") for rule in rules}
        self.assertIn(f"{tracking.TRACKING_ROUTE_PREFIX}/<token>", from_routes)
        self.assertEqual(
            {rule.get("to_route") for rule in rules if rule.get("from_route", "").startswith(tracking.TRACKING_ROUTE_PREFIX)},
            {"track"},
        )


class SchemaTests(unittest.TestCase):
    """The Custom Field definition, in both places that define it.

    The fixture is canonical but syncs at the *end* of ``bench migrate``, while
    code from the same release is already running; the ``before_migrate`` seeder
    closes that window. Two definitions of one column are only safe while they
    agree, so both are asserted against the same expectations.
    """

    @classmethod
    def setUpClass(cls):
        with open(FIXTURE_PATH, "r", encoding="utf-8") as handle:
            entries = json.load(handle)
        cls.entry = next(
            e
            for e in entries
            if e.get("dt") == "Sales Invoice"
            and e.get("fieldname") == tracking.TOKEN_FIELD
        )
        cls.all_entries = entries

    def test_field_appears_exactly_once_in_the_fixture(self):
        matches = [
            e
            for e in self.all_entries
            if e.get("dt") == "Sales Invoice" and e.get("fieldname") == tracking.TOKEN_FIELD
        ]
        self.assertEqual(len(matches), 1)

    def test_field_is_small_text_not_data(self):
        """A varchar cannot exist on this table.

        ``tabSales Invoice`` carries 247 columns and sits at MariaDB's hard
        65,535-byte row limit (COURIER_CONTRACTS §2). A ``Data`` field is
        ``varchar(140)`` ≈ 560 inline bytes at utf8mb4 and the ALTER is rejected
        outright with "(1118) Row size too large". This assertion is the guard
        against somebody "fixing" the field type to make it indexable.
        """
        self.assertEqual(self.entry["fieldtype"], "Small Text")

    def test_field_allows_writes_on_submit(self):
        """It is minted on a submitted invoice; without this the write vanishes."""
        self.assertEqual(self.entry.get("allow_on_submit"), 1)

    def test_field_is_no_copy(self):
        """Two orders sharing one public URL is a cross-customer leak."""
        self.assertEqual(self.entry.get("no_copy"), 1)

    def test_field_is_read_only_and_not_printed(self):
        self.assertEqual(self.entry.get("read_only"), 1)
        self.assertEqual(self.entry.get("print_hide"), 1)

    def test_fixture_name_matches_exactly(self):
        """A mismatched name is DELETED by the collision sweep on next migrate."""
        self.assertEqual(self.entry.get("name"), f"Sales Invoice-{tracking.TOKEN_FIELD}")
        self.assertEqual(self.entry.get("module"), "jarz pos")

    def test_seeder_mirrors_the_fixture(self):
        created = []
        fake = MagicMock()
        fake.db.exists.return_value = False

        def _capture(payload):
            doc = MagicMock()
            created.append((payload, doc))
            return doc

        fake.get_doc.side_effect = _capture
        with patch.object(cleanup, "frappe", fake):
            cleanup.ensure_tracking_fields()

        self.assertEqual(len(created), 1)
        payload, doc = created[0]
        attrs = dict(payload)
        for call in doc.set.call_args_list:
            if len(call.args) == 2:
                attrs[call.args[0]] = call.args[1]

        self.assertEqual(attrs["dt"], "Sales Invoice")
        self.assertEqual(attrs["fieldname"], tracking.TOKEN_FIELD)
        self.assertEqual(attrs["fieldtype"], self.entry["fieldtype"])
        self.assertEqual(attrs["insert_after"], self.entry["insert_after"])
        self.assertEqual(attrs.get("allow_on_submit"), 1)
        self.assertEqual(attrs.get("no_copy"), 1)
        self.assertEqual(attrs.get("read_only"), 1)

    def test_seeder_is_create_only(self):
        fake = MagicMock()
        fake.db.exists.return_value = True
        with patch.object(cleanup, "frappe", fake):
            cleanup.ensure_tracking_fields()
        self.assertFalse(fake.get_doc.called)

    def test_seeder_never_raises(self):
        """A raising before_migrate hook aborts the migrate for the whole bench."""
        fake = MagicMock()
        fake.db.exists.side_effect = Exception("database on fire")
        with patch.object(cleanup, "frappe", fake):
            cleanup.ensure_tracking_fields()  # must not raise

    def test_seeder_runs_after_the_collision_sweep(self):
        from jarz_pos import hooks

        before = hooks.before_migrate
        sweep = "jarz_pos.utils.cleanup.remove_colliding_custom_fields_for_fixtures"
        seeder = "jarz_pos.utils.cleanup.ensure_tracking_fields"
        self.assertIn(seeder, before)
        self.assertGreater(before.index(seeder), before.index(sweep))

    def test_token_field_is_not_in_the_frozen_courier_block(self):
        """The eight contracted delivery-outcome fields stay eight.

        Bundling this ninth field into ``ensure_courier_delivery_fields`` would
        have forced ``test_courier_custom_fields`` to be loosened, and loosening a
        frozen-contract test to fit new work is how a contract stops meaning
        anything.
        """
        created = []
        fake = MagicMock()
        fake.db.exists.return_value = False
        fake.get_meta.return_value.get_field.return_value = {"fieldname": "gps_location"}
        fake.get_doc.side_effect = lambda payload: created.append(payload) or MagicMock()
        with patch.object(cleanup, "frappe", fake):
            cleanup.ensure_courier_delivery_fields()

        self.assertNotIn(
            tracking.TOKEN_FIELD, {payload["fieldname"] for payload in created}
        )


class OfdHookTests(unittest.TestCase):
    """The single mint point every dispatch path converges on."""

    def _run_hook(self, state):
        doc = _invoice(token=None)
        doc.custom_sales_invoice_state = state
        doc.sales_invoice_state = state
        with patch.object(tracking, "ensure_tracking_token") as ensure:
            si_events.mint_tracking_token_on_ofd(doc)
        return ensure

    def test_mints_on_out_for_delivery(self):
        self._run_hook("Out for Delivery").assert_called_once()

    def test_does_not_mint_on_any_other_state(self):
        # "Recieved" is the live production misspelling (COURIER_CONTRACTS §1).
        for state in ("Recieved", "In Progress", "Ready", "Delivered", "Cancelled", "Returned", ""):
            with self.subTest(state=state):
                self._run_hook(state).assert_not_called()

    def test_hook_never_raises(self):
        doc = _invoice(token=None)
        doc.custom_sales_invoice_state = "Out for Delivery"
        with patch.object(tracking, "ensure_tracking_token", side_effect=RuntimeError("boom")):
            with patch.object(si_events, "frappe") as mock_frappe:
                si_events.mint_tracking_token_on_ofd(doc)  # must not raise
        self.assertTrue(mock_frappe.log_error.called)

    def test_hook_is_registered_in_hooks_py(self):
        """A hook nobody wired up mints nothing, silently and forever."""
        from jarz_pos import hooks

        handlers = hooks.doc_events["Sales Invoice"]["on_update_after_submit"]
        self.assertIn(
            "jarz_pos.events.sales_invoice.mint_tracking_token_on_ofd", handlers
        )


if __name__ == "__main__":
    unittest.main()
