"""The Sales Invoice board-state options are frozen. This test is the lock.

``Sales Invoice.custom_sales_invoice_state`` is a Select whose options ARE the
Kanban columns — the board derives them at runtime — and whose stored values sit
in every historical row in production. 274 references across 44 files read them.

**"Recieved" is misspelled and stays misspelled.** Correcting it is a data
migration touching every invoice ever written, plus every consumer of the value,
not a typo fix. This project does not touch it, and this test asserts the
misspelling verbatim so nobody can "helpfully" clean it up in passing.

**No new state is added by the courier project.** A failed delivery stays
``Out for Delivery`` and is expressed as ``custom_delivery_failure_reason`` +
``custom_delivery_attempt_no``. Adding a ``Delivery Failed`` column would touch
the state machine and every consumer of it, so the test below asserts the option
count as well as the string.

Pure ``unittest`` — reads the fixture off disk, no site required.
"""

import json
import os
import unittest

import jarz_pos

FIXTURE_PATH = os.path.join(
    os.path.dirname(os.path.abspath(jarz_pos.__file__)), "fixtures", "custom_field.json"
)

STATE_FIELD = "custom_sales_invoice_state"

#: The exact, frozen value on ``main``. Seven options, including the misspelling.
FROZEN_OPTIONS = "Recieved\nIn Progress\nReady\nOut for Delivery\nDelivered\nCancelled\nReturned"


def _state_entries():
    with open(FIXTURE_PATH, "r", encoding="utf-8") as handle:
        data = json.load(handle)
    return [
        e
        for e in data
        if e.get("dt") == "Sales Invoice" and e.get("fieldname") == STATE_FIELD
    ]


class TestStateOptionsFrozen(unittest.TestCase):
    def setUp(self):
        entries = _state_entries()
        self.assertEqual(
            len(entries),
            1,
            f"Expected exactly one fixture entry for Sales Invoice.{STATE_FIELD}",
        )
        self.entry = entries[0]

    def test_options_string_is_exactly_the_frozen_value(self):
        self.assertEqual(self.entry.get("options"), FROZEN_OPTIONS)

    def test_there_are_exactly_seven_options(self):
        self.assertEqual(len(FROZEN_OPTIONS.split("\n")), 7)
        self.assertEqual(len(self.entry["options"].split("\n")), 7)

    def test_the_misspelling_is_preserved(self):
        """'Recieved' is live production data, not a typo waiting to be fixed."""
        options = self.entry["options"].split("\n")
        self.assertIn("Recieved", options)
        self.assertNotIn("Received", options)

    def test_options_are_in_the_frozen_order(self):
        self.assertEqual(
            self.entry["options"].split("\n"),
            [
                "Recieved",
                "In Progress",
                "Ready",
                "Out for Delivery",
                "Delivered",
                "Cancelled",
                "Returned",
            ],
        )

    def test_no_delivery_failed_state_was_added(self):
        """A failure is a reason code on an OFD order, never a new column."""
        options = {o.lower() for o in self.entry["options"].split("\n")}
        for forbidden in ("delivery failed", "failed", "delivery_failed", "arrived"):
            self.assertNotIn(forbidden, options)

    def test_default_is_the_first_option(self):
        self.assertEqual(self.entry.get("default"), "Recieved")

    def test_fixture_identity_is_unchanged(self):
        """A changed ``name`` would make cleanup delete the live field."""
        self.assertEqual(self.entry.get("name"), f"Sales Invoice-{STATE_FIELD}")
        self.assertEqual(self.entry.get("module"), "jarz pos")
        self.assertEqual(self.entry.get("fieldtype"), "Select")


class TestServiceConstantsAgreeWithTheFrozenOptions(unittest.TestCase):
    """Code that writes a state must write one the board can actually render."""

    def test_delivered_state_constant_is_a_valid_option(self):
        from jarz_pos.services.courier_delivery import DELIVERED_STATE

        self.assertIn(DELIVERED_STATE, FROZEN_OPTIONS.split("\n"))

    def test_returned_board_state_constant_is_a_valid_option(self):
        from jarz_pos.services.invoice_return import RETURNED_BOARD_STATE

        self.assertIn(RETURNED_BOARD_STATE, FROZEN_OPTIONS.split("\n"))


if __name__ == "__main__":
    unittest.main()
