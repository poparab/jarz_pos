"""Tests for the trip leg — the gate that decides who may watch a courier move.

The leg exists because dispatch is a bulk action. ``send_trip_for_delivery``
puts three or four orders from one slot into ``Out for Delivery`` in the same
instant, all carrying the same courier, and the public tracking read used to
release that courier's live coordinates on the strength of the board state
alone. Every customer in the slot watched the courier drive to the other
addresses first, in visiting order, with the courier's first name beside the
marker.

So the assertions here are privacy assertions, and two of them are about the
*default*:

* an order with **no leg** releases nothing (:class:`GateTests`);
* an order whose leg has been **closed** releases nothing, including when the
  close and the start land in the same second;
* the escape hatch is phrased ``allow_live_map_without_leg`` and is read through
  the unset-reads-as-zero path on purpose — a ``require_...`` flag defaulting to
  1 reads as 0 on every already-populated Single, which would leave the leak in
  place on exactly the sites that already have it.

Pure ``unittest`` with mocks — no site, no fixtures.
"""

import unittest
from unittest.mock import MagicMock, patch

import frappe  # noqa: F401  (binds frappe.local / frappe._ for the imports below)

from jarz_pos.services import delivery_leg

EARLY = "2026-08-26 18:00:00"
LATER = "2026-08-26 18:30:00"


class _FakeMeta:
    def __init__(self, fields):
        self._fields = set(fields)

    def get_field(self, fieldname):
        return {"fieldname": fieldname} if fieldname in self._fields else None


def _patched(*, meta_fields=delivery_leg.LEG_FIELDS, settings=None, rows=None):
    """Patch ``delivery_leg.frappe`` with a router rather than a bare mock."""
    mock = MagicMock()
    mock.get_meta.return_value = _FakeMeta(meta_fields)
    mock.db.get_single_value.side_effect = lambda dt, field: (settings or {}).get(field)
    mock.get_all.return_value = list(rows or [])
    return patch.object(delivery_leg, "frappe", mock), mock


class IsLegOpenTests(unittest.TestCase):
    """The whole open/closed truth table, in one place."""

    def test_no_start_is_closed(self):
        self.assertFalse(delivery_leg.is_leg_open({}))
        self.assertFalse(delivery_leg.is_leg_open({delivery_leg.LEG_STARTED_FIELD: None}))

    def test_start_without_end_is_open(self):
        self.assertTrue(
            delivery_leg.is_leg_open({delivery_leg.LEG_STARTED_FIELD: EARLY})
        )

    def test_end_after_start_is_closed(self):
        self.assertFalse(
            delivery_leg.is_leg_open(
                {
                    delivery_leg.LEG_STARTED_FIELD: EARLY,
                    delivery_leg.LEG_ENDED_FIELD: LATER,
                }
            )
        )

    def test_restarted_after_an_end_is_open_again(self):
        """A skipped stop the courier comes back to must reopen the map."""
        self.assertTrue(
            delivery_leg.is_leg_open(
                {
                    delivery_leg.LEG_STARTED_FIELD: LATER,
                    delivery_leg.LEG_ENDED_FIELD: EARLY,
                }
            )
        )

    def test_same_timestamp_counts_as_closed(self):
        """A tie resolves to closed. This gate guards a live location, so the
        safe direction is to withhold it."""
        self.assertFalse(
            delivery_leg.is_leg_open(
                {
                    delivery_leg.LEG_STARTED_FIELD: EARLY,
                    delivery_leg.LEG_ENDED_FIELD: EARLY,
                }
            )
        )

    def test_none_row_is_closed(self):
        self.assertFalse(delivery_leg.is_leg_open(None))

    def test_unparseable_timestamps_do_not_raise(self):
        self.assertFalse(
            delivery_leg.is_leg_open({delivery_leg.LEG_STARTED_FIELD: "not a date"})
        )

    def test_reads_a_document_as_well_as_a_dict(self):
        class _Doc:
            pass

        doc = _Doc()
        setattr(doc, delivery_leg.LEG_STARTED_FIELD, EARLY)
        setattr(doc, delivery_leg.LEG_ENDED_FIELD, None)
        self.assertTrue(delivery_leg.is_leg_open(doc))


class GateTests(unittest.TestCase):
    """``live_map_gate_open`` — the policy layered on the pure rule above."""

    def test_closed_leg_withholds_the_position(self):
        started, _ = _patched()
        with started:
            self.assertFalse(
                delivery_leg.live_map_gate_open(
                    {
                        delivery_leg.LEG_STARTED_FIELD: EARLY,
                        delivery_leg.LEG_ENDED_FIELD: LATER,
                    }
                )
            )

    def test_no_leg_at_all_withholds_the_position(self):
        """The regression this module exists for: Out for Delivery is not enough."""
        started, _ = _patched()
        with started:
            self.assertFalse(delivery_leg.live_map_gate_open({}))

    def test_open_leg_releases_the_position(self):
        started, _ = _patched()
        with started:
            self.assertTrue(
                delivery_leg.live_map_gate_open(
                    {delivery_leg.LEG_STARTED_FIELD: EARLY}
                )
            )

    def test_unmigrated_site_degrades_to_the_old_behaviour(self):
        """A site without the columns cannot express a leg, so gating on one
        would kill live tracking outright on any deployment a commit behind.
        The deploy asserts the fields landed; this is the fallback, not a plan."""
        started, _ = _patched(meta_fields=())
        with started:
            self.assertTrue(delivery_leg.live_map_gate_open({}))

    def test_escape_hatch_reopens_it_for_everyone(self):
        started, _ = _patched(settings={"allow_live_map_without_leg": 1})
        with started:
            self.assertTrue(delivery_leg.live_map_gate_open({}))

    def test_unset_flag_reads_as_gate_on(self):
        """The polarity check. ``get_single_value`` returns None for a field no
        one has written, and None must mean "gate on", not "gate off"."""
        started, _ = _patched(settings={"allow_live_map_without_leg": None})
        with started:
            self.assertFalse(delivery_leg.allow_live_map_without_leg())
            self.assertFalse(delivery_leg.live_map_gate_open({}))


class OpenLegsForCourierTests(unittest.TestCase):
    """The sweep that keeps one courier to one open leg."""

    def test_filters_closed_legs_out_of_the_sql_result(self):
        rows = [
            {"name": "SI-1", delivery_leg.LEG_STARTED_FIELD: LATER,
             delivery_leg.LEG_ENDED_FIELD: None},
            {"name": "SI-2", delivery_leg.LEG_STARTED_FIELD: EARLY,
             delivery_leg.LEG_ENDED_FIELD: LATER},
        ]
        started, _ = _patched(rows=rows)
        with started:
            self.assertEqual(
                delivery_leg.open_legs_for_courier("Employee", "HR-EMP-0001"),
                ["SI-1"],
            )

    def test_excludes_the_invoice_being_started(self):
        rows = [
            {"name": "SI-1", delivery_leg.LEG_STARTED_FIELD: LATER,
             delivery_leg.LEG_ENDED_FIELD: None},
        ]
        started, _ = _patched(rows=rows)
        with started:
            self.assertEqual(
                delivery_leg.open_legs_for_courier(
                    "Employee", "HR-EMP-0001", exclude_invoice="SI-1"
                ),
                [],
            )

    def test_no_courier_returns_empty_without_querying(self):
        started, mock = _patched()
        with started:
            self.assertEqual(delivery_leg.open_legs_for_courier("", ""), [])
            mock.get_all.assert_not_called()

    def test_a_failing_query_returns_empty_rather_than_raising(self):
        started, mock = _patched()
        with started:
            mock.get_all.side_effect = RuntimeError("db gone")
            self.assertEqual(
                delivery_leg.open_legs_for_courier("Employee", "HR-EMP-0001"), []
            )


class UpdateShapeTests(unittest.TestCase):
    def test_start_clears_the_end(self):
        """Half the operation. Reopening a leg that only sets the start leaves
        the old end in place and :func:`is_leg_open` still reads it as closed."""
        updates = delivery_leg.start_updates(EARLY)
        self.assertEqual(updates[delivery_leg.LEG_STARTED_FIELD], EARLY)
        self.assertIsNone(updates[delivery_leg.LEG_ENDED_FIELD])

    def test_end_touches_only_the_end(self):
        updates = delivery_leg.end_updates(LATER)
        self.assertEqual(updates, {delivery_leg.LEG_ENDED_FIELD: LATER})

    def test_start_then_end_round_trips_to_closed(self):
        row = dict(delivery_leg.start_updates(EARLY))
        self.assertTrue(delivery_leg.is_leg_open(row))
        row.update(delivery_leg.end_updates(LATER))
        self.assertFalse(delivery_leg.is_leg_open(row))


if __name__ == "__main__":
    unittest.main()
