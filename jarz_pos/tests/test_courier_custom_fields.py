"""Schema guards for the courier app's Custom Fields (COURIER_CONTRACTS §2, §3).

Every assertion here exists because the corresponding mistake is silent in
production:

* a Sales Invoice outcome field without ``allow_on_submit`` cannot be written on
  a submitted invoice, and ``update_submitted_sales_invoice_fields`` filters it
  out and returns ``False`` **with no error** — a courier's "Delivered" tap
  vanishes;
* a field without ``no_copy`` carries a delivery outcome onto an amended
  invoice, so an amendment inherits somebody else's proof of delivery;
* a ``name`` that is not exactly ``"<DocType>-<fieldname>"`` is *deleted* by
  ``utils.cleanup.remove_colliding_custom_fields_for_fixtures`` on the next
  migrate — a typo here drops a live field on production;
* ``non_negative`` on latitude or longitude quietly clamps the southern
  hemisphere and everything west of Greenwich to zero.

The last class also checks the ``utils/cleanup`` seeders still mirror the
fixture. The two are belt and braces for each other, and belt-and-braces that
have drifted apart are worse than either alone.

Pure ``unittest`` — reads the fixture file off disk, no site required.
"""

import json
import os
import unittest
from unittest.mock import MagicMock, patch

import jarz_pos
from jarz_pos.utils import cleanup

FIXTURE_PATH = os.path.join(
    os.path.dirname(os.path.abspath(jarz_pos.__file__)), "fixtures", "custom_field.json"
)

#: fieldname -> (fieldtype, insert_after). The insert_after chain is contiguous:
#: each field anchors on the one above it, so the block stays together in the
#: form regardless of what any other app inserts nearby.
SALES_INVOICE_FIELDS = {
    "custom_delivery_sequence": ("Int", "custom_courier_party"),
    "custom_arrived_at": ("Datetime", "custom_delivery_sequence"),
    "custom_delivered_at": ("Datetime", "custom_arrived_at"),
    "custom_delivery_latitude": ("Float", "custom_delivered_at"),
    "custom_delivery_longitude": ("Float", "custom_delivery_latitude"),
    "custom_delivery_accuracy_m": ("Float", "custom_delivery_longitude"),
    "custom_delivery_attempt_no": ("Int", "custom_delivery_accuracy_m"),
    # Small Text, not Link. tabSales Invoice is at MariaDB's 65,535-byte row
    # limit (247 columns), so a varchar(140) ALTER is rejected outright with
    # error 1118. Small Text maps to TEXT, which costs ~20 inline bytes. See
    # utils/cleanup.ensure_courier_delivery_fields for the full reasoning.
    "custom_delivery_failure_reason": ("Small Text", "custom_delivery_attempt_no"),
}

ADDRESS_FIELDS = {
    "custom_latitude": ("Float", "gps_location"),
    "custom_longitude": ("Float", "custom_latitude"),
    "custom_geo_source": ("Select", "custom_longitude"),
    "custom_geo_confidence": ("Int", "custom_geo_source"),
    "custom_geo_accuracy_m": ("Float", "custom_geo_confidence"),
    "custom_geo_verified_on": ("Datetime", "custom_geo_accuracy_m"),
}

#: Frozen verbatim. Leading blank is Frappe's convention for an optional Select;
#: the order matches the confidence ladder but is NOT what enforces it.
GEO_SOURCE_OPTIONS = (
    "\nterritory_centroid\npos_link\ncustomer_pin\ncourier_verified\nmanual_override"
)


def _load_fixture():
    with open(FIXTURE_PATH, "r", encoding="utf-8") as handle:
        return json.load(handle)


def _index(entries, dt):
    return {e.get("fieldname"): e for e in entries if e.get("dt") == dt}


class TestFixtureIsWellFormed(unittest.TestCase):
    def test_fixture_is_a_list_of_custom_fields(self):
        data = _load_fixture()
        self.assertIsInstance(data, list)
        self.assertTrue(all(e.get("doctype") == "Custom Field" for e in data))

    def test_each_courier_field_appears_exactly_once(self):
        """A duplicated entry makes the fixture import non-deterministic."""
        data = _load_fixture()
        for dt, fields in (("Sales Invoice", SALES_INVOICE_FIELDS), ("Address", ADDRESS_FIELDS)):
            for fieldname in fields:
                matches = [
                    e for e in data if e.get("dt") == dt and e.get("fieldname") == fieldname
                ]
                self.assertEqual(
                    len(matches), 1, f"{dt}.{fieldname} appears {len(matches)} times"
                )


class TestSalesInvoiceOutcomeFields(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fields = _index(_load_fixture(), "Sales Invoice")

    def test_all_eight_fields_exist_with_the_contracted_type_and_anchor(self):
        for fieldname, (fieldtype, insert_after) in SALES_INVOICE_FIELDS.items():
            with self.subTest(field=fieldname):
                entry = self.fields.get(fieldname)
                self.assertIsNotNone(entry, f"{fieldname} missing from the fixture")
                self.assertEqual(entry["fieldtype"], fieldtype)
                self.assertEqual(entry["insert_after"], insert_after)

    def test_every_outcome_field_allows_writes_on_submit(self):
        """Without this the write is dropped and the caller is told it succeeded."""
        for fieldname in SALES_INVOICE_FIELDS:
            with self.subTest(field=fieldname):
                self.assertEqual(
                    self.fields[fieldname].get("allow_on_submit"),
                    1,
                    f"{fieldname} must carry allow_on_submit=1",
                )

    def test_every_outcome_field_is_no_copy(self):
        """A delivery outcome must not carry over onto an amended invoice."""
        for fieldname in SALES_INVOICE_FIELDS:
            with self.subTest(field=fieldname):
                self.assertEqual(
                    self.fields[fieldname].get("no_copy"),
                    1,
                    f"{fieldname} must carry no_copy=1",
                )

    def test_names_match_exactly_or_cleanup_will_delete_the_live_field(self):
        for fieldname in SALES_INVOICE_FIELDS:
            with self.subTest(field=fieldname):
                self.assertEqual(
                    self.fields[fieldname].get("name"), f"Sales Invoice-{fieldname}"
                )

    def test_module_is_declared(self):
        for fieldname in SALES_INVOICE_FIELDS:
            with self.subTest(field=fieldname):
                self.assertEqual(self.fields[fieldname].get("module"), "jarz pos")

    def test_coordinates_are_signed(self):
        """non_negative on a coordinate silently deletes half the planet."""
        for fieldname in ("custom_delivery_latitude", "custom_delivery_longitude"):
            with self.subTest(field=fieldname):
                self.assertEqual(self.fields[fieldname].get("non_negative"), 0)

    def test_coordinate_precision_is_six_places(self):
        """Six decimal places is ~0.1 m — anything coarser is not a door pin."""
        for fieldname in ("custom_delivery_latitude", "custom_delivery_longitude"):
            with self.subTest(field=fieldname):
                self.assertEqual(str(self.fields[fieldname].get("precision")), "6")

    def test_accuracy_precision_is_two_places(self):
        self.assertEqual(
            str(self.fields["custom_delivery_accuracy_m"].get("precision")), "2"
        )

    def test_attempt_counter_defaults_to_zero(self):
        self.assertEqual(str(self.fields["custom_delivery_attempt_no"].get("default")), "0")

    def test_failure_reason_carries_no_link_options(self):
        """The field stores a Delivery Failure Reason name without being a Link.

        It cannot be a Link: tabSales Invoice is at the InnoDB row-size limit and
        rejects any new varchar. So `options` must stay empty — a stale
        "Delivery Failure Reason" there would make Frappe treat a TEXT column as
        a link target and render a broken control in Desk.

        The integrity a Link would have enforced lives in
        services.courier_delivery._resolve_failure_reason, which resolves the
        stored value against the DocType and rejects anything missing or
        inactive.
        """
        self.assertFalse(self.fields["custom_delivery_failure_reason"].get("options"))


class TestAddressGeoFields(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fields = _index(_load_fixture(), "Address")

    def test_all_six_fields_exist_with_the_contracted_type_and_anchor(self):
        for fieldname, (fieldtype, insert_after) in ADDRESS_FIELDS.items():
            with self.subTest(field=fieldname):
                entry = self.fields.get(fieldname)
                self.assertIsNotNone(entry, f"{fieldname} missing from the fixture")
                self.assertEqual(entry["fieldtype"], fieldtype)
                self.assertEqual(entry["insert_after"], insert_after)

    def test_names_match_exactly_or_cleanup_will_delete_the_live_field(self):
        for fieldname in ADDRESS_FIELDS:
            with self.subTest(field=fieldname):
                self.assertEqual(self.fields[fieldname].get("name"), f"Address-{fieldname}")

    def test_module_is_declared(self):
        for fieldname in ADDRESS_FIELDS:
            with self.subTest(field=fieldname):
                self.assertEqual(self.fields[fieldname].get("module"), "jarz pos")

    def test_geo_source_options_are_the_frozen_ladder(self):
        self.assertEqual(
            self.fields["custom_geo_source"].get("options"), GEO_SOURCE_OPTIONS
        )

    def test_geo_source_options_match_the_confidence_ladder_exactly(self):
        from jarz_pos.utils.geo import CONFIDENCE_RANK

        options = [o for o in GEO_SOURCE_OPTIONS.split("\n") if o]
        self.assertEqual(set(options), set(CONFIDENCE_RANK))

    def test_derived_fields_are_read_only(self):
        """The rank is derived from the source; typing it in defeats the ladder."""
        for fieldname in ("custom_geo_confidence", "custom_geo_verified_on"):
            with self.subTest(field=fieldname):
                self.assertEqual(self.fields[fieldname].get("read_only"), 1)

    def test_coordinates_are_signed(self):
        for fieldname in ("custom_latitude", "custom_longitude"):
            with self.subTest(field=fieldname):
                self.assertEqual(self.fields[fieldname].get("non_negative"), 0)

    def test_coordinate_precision_is_six_places(self):
        for fieldname in ("custom_latitude", "custom_longitude"):
            with self.subTest(field=fieldname):
                self.assertEqual(str(self.fields[fieldname].get("precision")), "6")


class TestSeedersMirrorTheFixture(unittest.TestCase):
    """``before_migrate`` seeders and the fixture must not drift apart.

    The seeders exist because fixtures sync at the *end* of a migrate while code
    from the same release is already running. Two definitions of the same schema
    is only safe while they agree.
    """

    @staticmethod
    def _seeded_fields(seeder):
        """Run *seeder* against a fake frappe and collect what it would create.

        Returns ``{fieldname: merged_attrs}``. ``_ensure_custom_field`` puts the
        core attributes in the dict handed to ``frappe.get_doc`` and applies
        everything else through ``doc.set(...)``, so both are merged here to give
        one view of the field as it would actually be inserted.
        """
        created = []

        fake = MagicMock()
        fake.db.exists.return_value = False  # nothing exists ⇒ create everything
        fake.get_meta.return_value.get_field.return_value = {"fieldname": "gps_location"}

        def _capture(payload):
            doc = MagicMock()
            created.append((payload, doc))
            return doc

        fake.get_doc.side_effect = _capture

        with patch.object(cleanup, "frappe", fake):
            seeder()

        merged = {}
        for payload, doc in created:
            attrs = dict(payload)
            for call in doc.set.call_args_list:
                if len(call.args) == 2:
                    attrs[call.args[0]] = call.args[1]
            merged[payload["fieldname"]] = attrs
        return merged

    def test_sales_invoice_seeder_covers_every_contracted_field(self):
        seeded = self._seeded_fields(cleanup.ensure_courier_delivery_fields)
        self.assertEqual(set(seeded), set(SALES_INVOICE_FIELDS))

    def test_sales_invoice_seeder_sets_allow_on_submit_and_no_copy(self):
        """The seeder must not create a field the service cannot write to."""
        seeded = self._seeded_fields(cleanup.ensure_courier_delivery_fields)
        for fieldname, attrs in seeded.items():
            with self.subTest(field=fieldname):
                self.assertEqual(attrs["dt"], "Sales Invoice")
                self.assertEqual(attrs.get("allow_on_submit"), 1)
                self.assertEqual(attrs.get("no_copy"), 1)

    def test_seeded_coordinates_are_signed(self):
        seeded = self._seeded_fields(cleanup.ensure_courier_delivery_fields)
        for fieldname in ("custom_delivery_latitude", "custom_delivery_longitude"):
            with self.subTest(field=fieldname):
                self.assertEqual(seeded[fieldname].get("non_negative"), 0)

        seeded_addr = self._seeded_fields(cleanup.ensure_address_geo_fields)
        for fieldname in ("custom_latitude", "custom_longitude"):
            with self.subTest(field=fieldname):
                self.assertEqual(seeded_addr[fieldname].get("non_negative"), 0)

    def test_address_seeder_covers_every_contracted_field(self):
        seeded = self._seeded_fields(cleanup.ensure_address_geo_fields)
        self.assertEqual(set(seeded), set(ADDRESS_FIELDS))

    def test_address_seeder_uses_the_frozen_geo_source_options(self):
        seeded = self._seeded_fields(cleanup.ensure_address_geo_fields)
        self.assertEqual(seeded["custom_geo_source"]["options"], GEO_SOURCE_OPTIONS)

    def test_seeder_anchors_match_the_fixture_anchors(self):
        for seeder, expected in (
            (cleanup.ensure_courier_delivery_fields, SALES_INVOICE_FIELDS),
            (cleanup.ensure_address_geo_fields, ADDRESS_FIELDS),
        ):
            seeded = self._seeded_fields(seeder)
            for fieldname, (fieldtype, insert_after) in expected.items():
                with self.subTest(field=fieldname):
                    self.assertEqual(seeded[fieldname]["fieldtype"], fieldtype)
                    self.assertEqual(seeded[fieldname]["insert_after"], insert_after)

    def test_seeders_are_create_only(self):
        """An existing field is never rewritten — hand edits in Desk survive."""
        for seeder in (cleanup.ensure_courier_delivery_fields, cleanup.ensure_address_geo_fields):
            fake = MagicMock()
            fake.db.exists.return_value = True  # everything already exists
            fake.get_meta.return_value.get_field.return_value = {"fieldname": "gps_location"}
            with patch.object(cleanup, "frappe", fake):
                seeder()
            self.assertFalse(
                fake.get_doc.called, f"{seeder.__name__} rewrote an existing field"
            )

    def test_seeders_never_raise(self):
        """A raising before_migrate hook aborts the migrate for the whole bench."""
        for seeder in (cleanup.ensure_courier_delivery_fields, cleanup.ensure_address_geo_fields):
            fake = MagicMock()
            fake.db.exists.side_effect = Exception("database on fire")
            fake.get_meta.side_effect = Exception("database on fire")
            with patch.object(cleanup, "frappe", fake):
                seeder()  # must not raise


class TestHooksRegistration(unittest.TestCase):
    def test_both_seeders_run_after_the_collision_sweep(self):
        """Order matters: the sweep deletes fields whose name differs from the
        fixture's, so seeding before it would only hand it something to delete."""
        from jarz_pos import hooks

        before = hooks.before_migrate
        sweep = "jarz_pos.utils.cleanup.remove_colliding_custom_fields_for_fixtures"
        for seeder in (
            "jarz_pos.utils.cleanup.ensure_courier_delivery_fields",
            "jarz_pos.utils.cleanup.ensure_address_geo_fields",
        ):
            self.assertIn(seeder, before)
            self.assertGreater(before.index(seeder), before.index(sweep))

    def test_courier_seeder_runs_after_migrate(self):
        from jarz_pos import hooks

        self.assertIn(
            "jarz_pos.setup.courier_setup.ensure_courier_setup", hooks.after_migrate
        )

    def test_address_clamp_is_registered_on_before_save(self):
        from jarz_pos import hooks

        self.assertEqual(
            hooks.doc_events["Address"]["before_save"],
            "jarz_pos.events.address.clamp_geo_confidence",
        )


if __name__ == "__main__":
    unittest.main()
