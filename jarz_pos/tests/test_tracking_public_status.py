"""Tests for the public tracking read — the app's only guest-callable surface.

Every assertion here is a security assertion, because the threat model is "the
link got forwarded":

* **The response is exactly an allow-list.** The expected key set is written out
  as a literal below rather than imported from the service, so a new key cannot
  be added to the payload and blessed by the same edit. That is the whole point:
  the risk on a public endpoint is not a deliberate leak, it is a convenient
  ``**invoice_dict`` added on a Friday.
* **No internal vocabulary escapes.** The raw board state is never echoed as the
  machine-readable code, and the dispatch-floor words — above all the live
  "Recieved" misspelling — never appear anywhere in the response. See
  :data:`LEAKY_INTERNAL_STATES` for where that line is drawn and why.
* **Wrong token and expired token are indistinguishable.** Asserted as literal
  equality of the two payloads, not just "both fail". A distinguishable
  "expired" confirms the token was once real.
* **Courier data is minimal and gated.** First name only, never the full name;
  the phone only when ``expose_courier_phone_to_customer`` is on; live
  coordinates only while the order is genuinely Out for Delivery.
* **The rate limiter runs before any lookup**, so it cannot be used as a timing
  oracle and cannot be sidestepped by a wrong token.

Pure ``unittest`` with mocks — no site, no fixtures.
"""

import json
import unittest
from unittest.mock import MagicMock, patch

import frappe
from frappe.utils import add_to_date, now_datetime

from jarz_pos.services import delivery_leg, tracking
from jarz_pos.utils import settings_utils

TOKEN = "Zx9-QbT1kLmN4pRs7wYvUA"
INVOICE = "ACC-SINV-2026-00042"

#: The complete public contract, written out by hand. If this list and
#: ``tracking.PUBLIC_STATUS_KEYS`` disagree, one of the two changed without the
#: other being reviewed — which is exactly the review this test is buying.
EXPECTED_KEYS = {
    "success",
    "status",
    "status_label_en",
    "status_label_ar",
    "courier_first_name",
    "courier_phone",
    "courier_latitude",
    "courier_longitude",
    "courier_updated_at",
    "destination_latitude",
    "destination_longitude",
    "item_count",
    "total_qty",
    "order_total",
    "currency",
    "poll_interval_sec",
}

#: The seven frozen board states (COURIER_CONTRACTS §1), including the live
#: misspelling. No raw state string may ever be echoed as the machine-readable
#: ``status`` code.
INTERNAL_STATES = (
    "Recieved",
    "In Progress",
    "Ready",
    "Out for Delivery",
    "Delivered",
    "Cancelled",
    "Returned",
)

#: The subset that is *internal vocabulary* and must not appear anywhere in the
#: response, not even inside a label.
#:
#: "Recieved" is the misspelling that is live production data — shipping it to
#: customers would put a typo on every order page. "In Progress" and "Out for
#: Delivery" are warehouse/dispatch words, not things a buyer says.
#:
#: "Delivered", "Cancelled", "Returned" and "Ready" are deliberately NOT in this
#: list: they are ordinary English and a customer-facing label legitimately uses
#: them. The rule being enforced is "no internal vocabulary and no raw state
#: echo", not "avoid words that also happen to be state names".
LEAKY_INTERNAL_STATES = ("Recieved", "In Progress", "Out for Delivery")

#: Every customer status code this endpoint may emit.
CUSTOMER_STATUS_CODES = {
    "received",
    "preparing",
    "ready",
    "on_the_way",
    "delivered",
    "cancelled",
    "returned",
    "processing",
}

DEFAULT_SETTINGS = {
    "enable_customer_tracking": 1,
    "expose_courier_phone_to_customer": 0,
    "tracking_link_ttl_hours": 24,
}


class _FakeMeta:
    def __init__(self, fields):
        self._fields = set(fields)

    def get_field(self, fieldname):
        if fieldname not in self._fields:
            return None
        return {"fieldname": fieldname}


def _row(**overrides):
    data = {
        "name": INVOICE,
        "docstatus": 1,
        "grand_total": 450.0,
        "currency": "EGP",
        "shipping_address_name": "ADDR-1",
        "customer_address": "ADDR-1",
        "custom_courier_party_type": "Employee",
        "custom_courier_party": "HR-EMP-0001",
        "custom_kanban_profile": "Branch A",
        "pos_profile": "Branch A",
        "custom_delivered_at": None,
        "modified": str(now_datetime()),
        "custom_sales_invoice_state": "Out for Delivery",
        tracking.TOKEN_FIELD: TOKEN,
    }
    data.update(overrides)
    return data


class _Harness:
    """Patches ``services.tracking.frappe`` with a router, not a bare MagicMock.

    Routing ``db.get_value`` by doctype means the real
    ``_resolve_token_to_invoice`` / ``_destination_pin`` / ``_courier_identity``
    code runs — including the token re-validation step, which is the guard
    against a stale cache entry rendering one order's data under another order's
    link.
    """

    def __init__(
        self,
        *,
        invoice=None,
        settings=None,
        address=None,
        items=None,
        employee=None,
        supplier=None,
        cache_values=None,
        meta_fields=(tracking.TOKEN_FIELD, "custom_sales_invoice_state"),
    ):
        self.invoice = invoice if invoice is not None else _row()
        self.settings = dict(DEFAULT_SETTINGS)
        self.settings.update(settings or {})
        self.address = (
            address
            if address is not None
            else {"custom_latitude": 30.0444, "custom_longitude": 31.2357}
        )
        self.items = items if items is not None else [{"qty": 2}, {"qty": 1}]
        self.employee = (
            employee
            if employee is not None
            else {"employee_name": "Mahmoud Abdelaziz Farouk", "cell_number": "01000000001"}
        )
        self.supplier = supplier
        self.cache_values = dict(cache_values or {})
        self.meta_fields = meta_fields
        self._patches = []
        self.frappe = None
        self.cache = None

    def __enter__(self):
        def _get_single_value(doctype, field):
            return self.settings.get(field)

        def _get_value(doctype, name, fields=None, as_dict=False, **kwargs):
            if doctype == "Sales Invoice":
                if not self.invoice:
                    return None
                if isinstance(name, dict):
                    wanted = name.get(tracking.TOKEN_FIELD)
                    if self.invoice.get(tracking.TOKEN_FIELD) == wanted:
                        return dict(self.invoice)
                    return None
                if self.invoice.get("name") == name:
                    return dict(self.invoice)
                return None
            if doctype == "Address":
                return dict(self.address) if self.address else None
            if doctype == "Employee":
                return dict(self.employee) if self.employee else None
            if doctype == "Supplier":
                return dict(self.supplier) if self.supplier else None
            return None

        def _get_all(doctype, filters=None, fields=None, **kwargs):
            if doctype == "Sales Invoice Item":
                return [dict(item) for item in self.items]
            return []

        def _sql(query, values=None, *args, **kwargs):
            """Serve the raw ``tabSingles`` read that ``settings_utils`` performs.

            ``tracking_link_ttl_hours`` is read through
            :func:`jarz_pos.utils.settings_utils.single_int`, which queries
            ``tabSingles`` directly rather than calling
            ``frappe.db.get_single_value``. It has to: ``get_single_value`` casts
            Int through ``cint()``, so a field nobody has ever written comes back
            as ``0`` — indistinguishable from an operator who deliberately chose
            0, which for this field is the meaningful value "never expire". The
            declared 24-hour default could therefore never apply.

            That means the ``get_single_value`` stub above no longer covers this
            setting, and a test that only stubs it silently loses control of the
            value: the real query runs against the test site, where the row
            happens to hold 0, and every expiry assertion passes vacuously
            because nothing ever expires. This stub is what keeps
            ``self.settings`` authoritative for both read paths.
            """
            text = " ".join(str(query).split()).lower()
            if "tabsingles" not in text:
                return []
            field = None
            if isinstance(values, (tuple, list)) and len(values) >= 2:
                field = values[1]
            elif isinstance(values, dict):
                field = values.get("field")
            if field is None or field not in self.settings:
                return []
            value = self.settings[field]
            # A stored NULL and an absent row both mean "never written".
            return [] if value is None else [(str(value),)]

        mock = MagicMock()
        mock.db.get_single_value.side_effect = _get_single_value
        mock.db.sql.side_effect = _sql
        mock.db.get_value.side_effect = _get_value
        mock.get_all.side_effect = _get_all
        mock.get_meta.return_value = _FakeMeta(self.meta_fields)

        cache = MagicMock()
        cache.make_key.side_effect = lambda key: key
        cache.incrby.return_value = 1
        cache.get_value.side_effect = lambda key, *a, **k: self.cache_values.get(key)
        cache.set_value.side_effect = lambda key, value, **k: self.cache_values.__setitem__(
            key, value
        )
        mock.cache.return_value = cache

        # Both modules, not just ``tracking``. ``settings_utils`` holds its own
        # module-level ``frappe`` reference, so patching only the caller leaves
        # the helper talking to the real database — which is exactly how the
        # expiry setting escaped this harness's control.
        self._patches = [
            patch.object(tracking, "frappe", mock),
            patch.object(settings_utils, "frappe", mock),
            # ``delivery_leg`` decides whether the live position may be released
            # at all, and holds its own module-level ``frappe``. Leaving it
            # unpatched would let the leg gate ask the real database whether the
            # leg fields exist — the same escape route the comment above
            # describes for the expiry setting.
            patch.object(delivery_leg, "frappe", mock),
        ]
        for started in self._patches:
            started.start()
        self.frappe = mock
        self.cache = cache
        return self

    def __exit__(self, *exc):
        for started in reversed(self._patches):
            started.stop()
        return False


def _resolve(**harness_kwargs):
    with _Harness(**harness_kwargs):
        return tracking.resolve_public_status(TOKEN)


class AllowListTests(unittest.TestCase):
    def test_declared_allow_list_matches_the_reviewed_literal(self):
        self.assertEqual(set(tracking.PUBLIC_STATUS_KEYS), EXPECTED_KEYS)

    def test_customer_status_vocabulary_is_closed(self):
        """Every emittable code has a label pair, and there are no others."""
        self.assertEqual(set(tracking.STATUS_LABELS), CUSTOMER_STATUS_CODES)
        self.assertTrue(set(tracking._STATE_TO_STATUS.values()) <= CUSTOMER_STATUS_CODES)
        self.assertTrue(tracking.TERMINAL_STATUSES <= CUSTOMER_STATUS_CODES)

    def test_response_keys_are_exactly_the_allow_list(self):
        result = _resolve()
        self.assertTrue(result["success"])
        self.assertEqual(set(result.keys()), EXPECTED_KEYS)

    def test_response_carries_no_internal_identifiers(self):
        """No invoice name, customer name, address text, item name or cost."""
        result = _resolve()
        blob = json.dumps(result, default=str)
        for forbidden in (INVOICE, "ADDR-1", "HR-EMP-0001", "Branch A", "SINV"):
            self.assertNotIn(forbidden, blob)

    def test_internal_state_is_mapped_never_echoed(self):
        for state, expected_status in (
            ("Recieved", "received"),
            ("In Progress", "preparing"),
            ("Ready", "ready"),
            ("Out for Delivery", "on_the_way"),
            ("Delivered", "delivered"),
            ("Cancelled", "cancelled"),
            ("Returned", "returned"),
        ):
            with self.subTest(state=state):
                result = _resolve(
                    invoice=_row(custom_sales_invoice_state=state),
                    settings={"tracking_link_ttl_hours": 0},
                )
                self.assertEqual(result["status"], expected_status)
                # The machine-readable code is never the raw state string.
                self.assertNotIn(result["status"], INTERNAL_STATES)
                self.assertIn(result["status"], CUSTOMER_STATUS_CODES)
                blob = json.dumps(result, default=str)
                for internal in LEAKY_INTERNAL_STATES:
                    self.assertNotIn(internal, blob)

    def test_the_live_misspelling_never_reaches_a_customer(self):
        """"Recieved" (sic) is live production data in every historical row."""
        result = _resolve(invoice=_row(custom_sales_invoice_state="Recieved"))
        self.assertEqual(result["status"], "received")
        self.assertNotIn("Recieved", json.dumps(result, default=str))
        self.assertNotIn("Recieved", result["status_label_en"])
        self.assertNotIn("Recieved", result["status_label_ar"])

    def test_both_language_labels_are_always_present(self):
        """The page shows both and lets the reader pick.

        There is no single request language to translate into for a guest whose
        phone might be set to either, so the pair is returned rather than one
        server-chosen string.
        """
        for state in ("Recieved", "In Progress", "Ready", "Out for Delivery"):
            with self.subTest(state=state):
                result = _resolve(invoice=_row(custom_sales_invoice_state=state))
                self.assertTrue(result["status_label_en"].strip())
                self.assertTrue(result["status_label_ar"].strip())
                self.assertNotEqual(
                    result["status_label_en"], result["status_label_ar"]
                )

    def test_unmapped_state_degrades_to_a_vague_status(self):
        """A new internal column must not become customer vocabulary."""
        result = _resolve(invoice=_row(custom_sales_invoice_state="Awaiting Recount"))
        self.assertEqual(result["status"], tracking.STATUS_PROCESSING)
        self.assertNotIn("Recount", json.dumps(result, default=str))


class PayloadContentTests(unittest.TestCase):
    def test_item_summary_is_counts_only(self):
        result = _resolve(items=[{"qty": 2}, {"qty": 1}, {"qty": 4}])
        self.assertEqual(result["item_count"], 3)
        self.assertEqual(result["total_qty"], 7.0)

    def test_order_total_and_currency_are_reported(self):
        result = _resolve(invoice=_row(grand_total=1234.5, currency="EGP"))
        self.assertEqual(result["order_total"], 1234.5)
        self.assertEqual(result["currency"], "EGP")

    def test_destination_pin_is_the_address_pin(self):
        result = _resolve()
        self.assertAlmostEqual(result["destination_latitude"], 30.0444, places=4)
        self.assertAlmostEqual(result["destination_longitude"], 31.2357, places=4)

    def test_unpinned_address_reports_null_not_null_island(self):
        """A Frappe Float is NOT NULL DEFAULT 0, so an unpinned address is (0, 0)."""
        result = _resolve(address={"custom_latitude": 0.0, "custom_longitude": 0.0})
        self.assertIsNone(result["destination_latitude"])
        self.assertIsNone(result["destination_longitude"])

    def test_missing_address_is_not_an_error(self):
        result = _resolve(
            invoice=_row(shipping_address_name=None, customer_address=None), address=None
        )
        self.assertTrue(result["success"])
        self.assertIsNone(result["destination_latitude"])

    def test_poll_interval_is_advertised_to_the_client(self):
        self.assertEqual(
            _resolve()["poll_interval_sec"], tracking.DEFAULT_POLL_INTERVAL_SEC
        )


class CourierPrivacyTests(unittest.TestCase):
    def test_only_the_first_name_is_exposed(self):
        result = _resolve()
        self.assertEqual(result["courier_first_name"], "Mahmoud")
        blob = json.dumps(result, default=str)
        self.assertNotIn("Abdelaziz", blob)
        self.assertNotIn("Farouk", blob)

    def test_phone_is_hidden_by_default(self):
        result = _resolve(settings={"expose_courier_phone_to_customer": 0})
        self.assertIsNone(result["courier_phone"])
        self.assertNotIn("01000000001", json.dumps(result, default=str))

    def test_phone_appears_only_when_the_flag_is_on(self):
        result = _resolve(settings={"expose_courier_phone_to_customer": 1})
        self.assertEqual(result["courier_phone"], "01000000001")

    def test_supplier_courier_resolves_too(self):
        result = _resolve(
            invoice=_row(custom_courier_party_type="Supplier", custom_courier_party="SUP-1"),
            supplier={"supplier_name": "Rami Bikes", "mobile_no": "01000000002"},
            settings={"expose_courier_phone_to_customer": 1},
        )
        self.assertEqual(result["courier_first_name"], "Rami")
        self.assertEqual(result["courier_phone"], "01000000002")

    def test_unassigned_order_reports_no_courier(self):
        result = _resolve(invoice=_row(custom_courier_party=None))
        self.assertIsNone(result["courier_first_name"])
        self.assertIsNone(result["courier_phone"])


class TripLegGateTests(unittest.TestCase):
    """The per-order gate. Out for Delivery alone must not release a position.

    Dispatch is bulk: ``api.trips.send_trip_for_delivery`` moves every invoice
    in a trip to Out for Delivery atomically, so several customers hold that
    state at once with the same courier on it. Before the leg existed, all of
    them were handed the same live coordinates and the same courier name — each
    one watching the courier drive to the others first.

    ``meta_fields`` includes the leg columns in every test here, because that is
    what makes the gate active; the suite's default omits them, which is how a
    site that has not migrated yet keeps working.
    """

    META = (
        tracking.TOKEN_FIELD,
        "custom_sales_invoice_state",
    ) + delivery_leg.LEG_FIELDS

    def _cache(self, branch="Branch A", party="HR-EMP-0001"):
        return {
            tracking.courier_location_key(branch, party): json.dumps(
                {"lat": 30.05, "lng": 31.24, "ts": "2026-08-26 18:10:00"}
            )
        }

    def _resolve_with_leg(self, **leg_fields):
        return _resolve(
            invoice=_row(**leg_fields),
            cache_values=self._cache(),
            meta_fields=self.META,
        )

    def test_out_for_delivery_without_a_leg_hides_the_courier(self):
        """The regression. Everything below is a variation on this one."""
        result = self._resolve_with_leg()
        self.assertEqual(result["status"], tracking.STATUS_ON_THE_WAY)
        self.assertIsNone(result["courier_latitude"])
        self.assertIsNone(result["courier_longitude"])
        self.assertIsNone(result["courier_updated_at"])

    def test_open_leg_releases_the_courier(self):
        result = self._resolve_with_leg(custom_leg_started_at="2026-08-26 18:04:00")
        self.assertAlmostEqual(result["courier_latitude"], 30.05, places=4)
        self.assertAlmostEqual(result["courier_longitude"], 31.24, places=4)

    def test_closed_leg_hides_the_courier_again(self):
        result = self._resolve_with_leg(
            custom_leg_started_at="2026-08-26 18:04:00",
            custom_leg_ended_at="2026-08-26 18:20:00",
        )
        self.assertIsNone(result["courier_latitude"])

    def test_reopened_leg_shows_the_courier_again(self):
        """A skipped stop the courier returns to. The map must reopen."""
        result = self._resolve_with_leg(
            custom_leg_started_at="2026-08-26 18:40:00",
            custom_leg_ended_at="2026-08-26 18:20:00",
        )
        self.assertAlmostEqual(result["courier_latitude"], 30.05, places=4)

    def test_the_name_and_phone_are_gated_with_the_marker(self):
        """The identity travels under the same gate as the coordinates.

        Naming the courier to a customer they are not on the way to discloses
        who is carrying somebody else's order — the same leak as the marker,
        and it was made on the same terms before the gate existed.
        """
        hidden = _resolve(
            invoice=_row(),
            settings={"expose_courier_phone_to_customer": 1},
            cache_values=self._cache(),
            meta_fields=self.META,
        )
        self.assertIsNone(hidden["courier_first_name"])
        self.assertIsNone(hidden["courier_phone"])

        shown = _resolve(
            invoice=_row(custom_leg_started_at="2026-08-26 18:04:00"),
            settings={"expose_courier_phone_to_customer": 1},
            cache_values=self._cache(),
            meta_fields=self.META,
        )
        self.assertEqual(shown["courier_first_name"], "Mahmoud")
        self.assertEqual(shown["courier_phone"], "01000000001")

    def test_payload_shape_is_unchanged_by_the_gate(self):
        """The allow-list still governs. A withheld field is null, not absent —
        a client that reads `courier_latitude` must not start seeing KeyError."""
        result = self._resolve_with_leg()
        self.assertEqual(set(result), EXPECTED_KEYS)

    def test_escape_hatch_restores_the_old_behaviour(self):
        result = _resolve(
            invoice=_row(),
            settings={"allow_live_map_without_leg": 1},
            cache_values=self._cache(),
            meta_fields=self.META,
        )
        self.assertAlmostEqual(result["courier_latitude"], 30.05, places=4)

    def test_delivered_order_stays_hidden_even_with_an_open_leg(self):
        """Status and leg are ANDed. A stale open leg on a delivered order —
        which the delivered transition should have closed — must not keep a
        public link streaming a courier's position."""
        result = _resolve(
            invoice=_row(
                custom_sales_invoice_state="Delivered",
                custom_leg_started_at="2026-08-26 18:04:00",
            ),
            settings={"tracking_link_ttl_hours": 0},
            cache_values=self._cache(),
            meta_fields=self.META,
        )
        self.assertIsNone(result["courier_latitude"])


class LivePositionTests(unittest.TestCase):
    def _cache(self, payload, branch="Branch A", party="HR-EMP-0001"):
        return {tracking.courier_location_key(branch, party): payload}

    def test_position_is_read_from_the_redis_key(self):
        result = _resolve(
            cache_values=self._cache(
                json.dumps({"lat": 30.05, "lng": 31.24, "ts": "2026-08-08 11:59:00"})
            )
        )
        self.assertAlmostEqual(result["courier_latitude"], 30.05, places=4)
        self.assertAlmostEqual(result["courier_longitude"], 31.24, places=4)
        self.assertEqual(result["courier_updated_at"], "2026-08-08 11:59:00")

    def test_position_is_suppressed_unless_out_for_delivery(self):
        """After delivery the courier is at somebody else's door.

        Streaming their position from a delivered order's link would turn a
        courtesy into a permanent staff tracker.
        """
        for state in ("Recieved", "In Progress", "Ready", "Delivered", "Cancelled"):
            with self.subTest(state=state):
                result = _resolve(
                    invoice=_row(custom_sales_invoice_state=state),
                    settings={"tracking_link_ttl_hours": 0},
                    cache_values=self._cache(json.dumps({"lat": 30.05, "lng": 31.24})),
                )
                self.assertIsNone(result["courier_latitude"])
                self.assertIsNone(result["courier_longitude"])

    def test_absent_key_degrades_to_null_coordinates(self):
        """Lane B7 does not exist yet, so this is today's normal path."""
        result = _resolve(cache_values={})
        self.assertTrue(result["success"])
        self.assertIsNone(result["courier_latitude"])
        self.assertIsNone(result["courier_longitude"])

    def test_bare_lat_lng_string_is_accepted(self):
        """Tolerant parsing, because the producer is not written yet."""
        result = _resolve(cache_values=self._cache("30.05,31.24"))
        self.assertAlmostEqual(result["courier_latitude"], 30.05, places=4)

    def test_latitude_longitude_aliases_are_accepted(self):
        result = _resolve(
            cache_values=self._cache({"latitude": 30.05, "longitude": 31.24})
        )
        self.assertAlmostEqual(result["courier_longitude"], 31.24, places=4)

    def test_garbage_in_the_key_is_not_an_error(self):
        for junk in ("", "not-a-position", json.dumps({"lat": 0, "lng": 0}), "[]"):
            with self.subTest(junk=junk):
                result = _resolve(cache_values=self._cache(junk))
                self.assertTrue(result["success"])
                self.assertIsNone(result["courier_latitude"])


class NotFoundAndExpiryTests(unittest.TestCase):
    def test_unknown_token_is_generic(self):
        with _Harness(invoice=_row(**{tracking.TOKEN_FIELD: "some-other-token"})):
            result = tracking.resolve_public_status(TOKEN)
        self.assertEqual(result, {"success": False, "error": "not found"})

    def test_empty_token_is_generic(self):
        with _Harness():
            for empty in (None, "", "   "):
                self.assertEqual(
                    tracking.resolve_public_status(empty),
                    {"success": False, "error": "not found"},
                )

    def test_expired_token_is_byte_identical_to_an_unknown_token(self):
        """The enumeration oracle this design exists to avoid."""
        delivered_long_ago = str(add_to_date(now_datetime(), hours=-72))
        expired = _resolve(
            invoice=_row(
                custom_sales_invoice_state="Delivered",
                custom_delivered_at=delivered_long_ago,
                modified=delivered_long_ago,
            )
        )
        with _Harness(invoice=_row(**{tracking.TOKEN_FIELD: "different"})):
            unknown = tracking.resolve_public_status(TOKEN)

        self.assertEqual(expired, unknown)
        self.assertEqual(expired, {"success": False, "error": "not found"})

    def test_recently_delivered_order_still_resolves(self):
        recent = str(add_to_date(now_datetime(), hours=-3))
        result = _resolve(
            invoice=_row(
                custom_sales_invoice_state="Delivered",
                custom_delivered_at=recent,
                modified=recent,
            )
        )
        self.assertTrue(result["success"])
        self.assertEqual(result["status"], "delivered")

    def test_ttl_zero_means_never_expire(self):
        long_ago = str(add_to_date(now_datetime(), hours=-24 * 400))
        result = _resolve(
            invoice=_row(
                custom_sales_invoice_state="Delivered",
                custom_delivered_at=long_ago,
                modified=long_ago,
            ),
            settings={"tracking_link_ttl_hours": 0},
        )
        self.assertTrue(result["success"])

    def test_a_live_order_never_expires_however_old(self):
        """The clock starts at a terminal state, not at creation."""
        long_ago = str(add_to_date(now_datetime(), hours=-24 * 30))
        result = _resolve(
            invoice=_row(custom_sales_invoice_state="Out for Delivery", modified=long_ago)
        )
        self.assertTrue(result["success"])

    def test_cancelled_and_returned_also_expire(self):
        long_ago = str(add_to_date(now_datetime(), hours=-72))
        for state in ("Cancelled", "Returned"):
            with self.subTest(state=state):
                result = _resolve(
                    invoice=_row(custom_sales_invoice_state=state, modified=long_ago)
                )
                self.assertEqual(result, {"success": False, "error": "not found"})

    def test_feature_switched_off_looks_like_a_bad_token(self):
        """The kill switch must not advertise that the order exists."""
        result = _resolve(settings={"enable_customer_tracking": 0})
        self.assertEqual(result, {"success": False, "error": "not found"})

    def test_unmigrated_site_looks_like_a_bad_token(self):
        result = _resolve(meta_fields=("custom_sales_invoice_state",))
        self.assertEqual(result, {"success": False, "error": "not found"})

    def test_lookup_failure_is_not_echoed_to_the_caller(self):
        """A traceback in a guest response leaks doctype and column names."""
        with _Harness() as harness:
            with patch.object(
                tracking, "_resolve_token_to_invoice", side_effect=RuntimeError("boom")
            ):
                result = tracking.resolve_public_status(TOKEN)
            self.assertTrue(harness.frappe.log_error.called)
        self.assertEqual(result, {"success": False, "error": "not found"})


class TokenResolutionTests(unittest.TestCase):
    def test_stale_cache_entry_cannot_render_another_order(self):
        """The reason the cache hit is re-validated against the DB.

        The index says this token belongs to ORDER-B; ORDER-B's stored token no
        longer matches. Serving it anyway would be a cross-customer leak
        manufactured by a cache.
        """
        other = _row(name="ACC-SINV-2026-00099", **{tracking.TOKEN_FIELD: "rotated"})
        with _Harness(
            invoice=other,
            cache_values={f"jarz_pos:track:tok:{TOKEN}": "ACC-SINV-2026-00099"},
        ):
            result = tracking.resolve_public_status(TOKEN)

        self.assertEqual(result, {"success": False, "error": "not found"})

    def test_successful_resolution_populates_the_index(self):
        with _Harness() as harness:
            tracking.resolve_public_status(TOKEN)
        self.assertEqual(
            harness.cache_values.get(f"jarz_pos:track:tok:{TOKEN}"), INVOICE
        )


class RateLimitTests(unittest.TestCase):
    def test_budget_is_spent_before_any_lookup(self):
        """So the limit cannot be used to tell a real token from a fake one."""
        with _Harness():
            with patch.object(tracking, "consume_rate_budget", return_value=False), patch.object(
                tracking, "_resolve_token_to_invoice"
            ) as resolver:
                result = tracking.resolve_public_status(TOKEN)

        self.assertEqual(result, {"success": False, "error": "too many requests"})
        resolver.assert_not_called()

    def test_counter_allows_up_to_the_limit_and_then_refuses(self):
        counter = {"n": 0}

        def _incrby(key, amount):
            counter["n"] += amount
            return counter["n"]

        with _Harness() as harness:
            harness.cache.incrby.side_effect = _incrby
            allowed = [
                tracking.consume_rate_budget(TOKEN)
                for _ in range(tracking.RATE_LIMIT_MAX_REQUESTS)
            ]
            refused = tracking.consume_rate_budget(TOKEN)
            # The window is set once, on the first request, so the bucket cannot
            # be held open forever by a steady trickle.
            harness.cache.expire.assert_called_once()

        self.assertTrue(all(allowed))
        self.assertFalse(refused)

    def test_cache_outage_fails_open(self):
        """Redis being down must not also take the tracking page down."""
        with _Harness() as harness:
            harness.cache.incrby.side_effect = RuntimeError("no redis")
            self.assertTrue(tracking.consume_rate_budget(TOKEN))


class ApiEnvelopeTests(unittest.TestCase):
    """The transport layer stays thin and never widens the contract."""

    def test_endpoint_returns_the_service_payload_unchanged(self):
        from jarz_pos.api import tracking as tracking_api

        payload = {"success": True, "status": "on_the_way"}
        with patch.object(tracking_api, "frappe") as mock_frappe, patch.object(
            tracking_api._tracking, "resolve_public_status", return_value=payload
        ):
            mock_frappe.local.response = {}
            result = tracking_api.get_public_status(token=TOKEN)

        self.assertEqual(result, payload)

    def test_endpoint_sets_a_429_for_a_throttled_token(self):
        from jarz_pos.api import tracking as tracking_api

        with patch.object(tracking_api, "frappe") as mock_frappe, patch.object(
            tracking_api._tracking, "resolve_public_status", return_value=tracking.rate_limited()
        ):
            response = {}
            mock_frappe.local.response = response
            tracking_api.get_public_status(token=TOKEN)

        self.assertEqual(response.get("http_status_code"), 429)

    def test_endpoint_never_echoes_an_exception_to_a_guest(self):
        from jarz_pos.api import tracking as tracking_api

        with patch.object(tracking_api, "frappe") as mock_frappe, patch.object(
            tracking_api._tracking,
            "resolve_public_status",
            side_effect=RuntimeError("table `tabSales Invoice` column x"),
        ):
            mock_frappe.local.response = {}
            # `except frappe.PermissionError` needs a real exception class; a bare
            # MagicMock there raises "catching classes that do not inherit from
            # BaseException".
            mock_frappe.PermissionError = frappe.PermissionError
            result = tracking_api.get_public_status(token=TOKEN)

        self.assertEqual(result, {"success": False, "error": "not found"})
        self.assertTrue(mock_frappe.log_error.called)

    def test_only_the_public_read_is_guest_callable(self):
        """A second guest endpoint must be a deliberate, reviewed act.

        ``frappe.whitelist`` records guest access in the module-level
        ``frappe.guest_methods`` set rather than as an attribute on the function,
        so that registry is what has to be asserted.
        """
        from jarz_pos.api import tracking as tracking_api

        self.assertIn(tracking_api.get_public_status, frappe.guest_methods)
        self.assertNotIn(tracking_api.get_tracking_link, frappe.guest_methods)
        self.assertIn(tracking_api.get_tracking_link, frappe.whitelisted)


if __name__ == "__main__":
    unittest.main()
