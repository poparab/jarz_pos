import datetime
import unittest
from unittest.mock import MagicMock, patch

# ``now_datetime`` resolves the site's timezone through the database, which no
# unit test has. Every stamp below is written with a fixed clock instead.
_FIXED_NOW = datetime.datetime(2026, 8, 28, 23, 45, 0)


def _make_mock_frappe():
	mock_frappe = MagicMock()
	mock_frappe.session.user = "staff@example.com"
	return mock_frappe


class TestCourierCarry(unittest.TestCase):
	def test_normalize_acknowledgement_accepts_a_json_string(self):
		"""Frappe delivers list arguments as JSON over HTTP."""
		from jarz_pos.services.courier_carry import normalize_acknowledgement

		self.assertEqual(
			normalize_acknowledgement('["CT-0001", "CT-0002"]'),
			["CT-0001", "CT-0002"],
		)

	def test_normalize_acknowledgement_accepts_row_dicts(self):
		from jarz_pos.services.courier_carry import normalize_acknowledgement

		self.assertEqual(
			normalize_acknowledgement(
				[{"courier_transaction": "CT-0001"}, {"name": "CT-0002"}]
			),
			["CT-0001", "CT-0002"],
		)

	def test_normalize_acknowledgement_drops_duplicates_and_blanks(self):
		from jarz_pos.services.courier_carry import normalize_acknowledgement

		self.assertEqual(
			normalize_acknowledgement(["CT-0001", "", "CT-0001", None, "CT-0002"]),
			["CT-0001", "CT-0002"],
		)

	def test_stamp_carried_keeps_the_first_shift_and_counts_the_recurrence(self):
		"""The shift that originally let the money walk is the auditable fact.

		A transaction carried three nights running must still point at night one,
		with a carry count of three — not at whichever shift touched it last.
		"""
		from jarz_pos.services.courier_carry import stamp_carried

		mock_frappe = _make_mock_frappe()
		rows = [
			{"courier_transaction": "CT-0001", "net_balance": 90, "carry_count": 0},
			{
				"courier_transaction": "CT-0002",
				"net_balance": 70,
				"carry_count": 2,
				"carried_from_shift": "POS-OPE-2026-00001",
			},
		]

		with patch("jarz_pos.services.courier_carry.frappe", mock_frappe), \
				 patch("jarz_pos.services.courier_carry.now_datetime", return_value=_FIXED_NOW):
			result = stamp_carried(
				rows, opening_entry="POS-OPE-2026-00003", user="staff@example.com"
			)

		self.assertEqual(result["count"], 2)
		self.assertEqual(result["net_balance"], 160)

		calls = mock_frappe.db.set_value.call_args_list
		first_values = calls[0].args[2]
		self.assertEqual(first_values["carried_from_shift"], "POS-OPE-2026-00003")
		self.assertEqual(first_values["carry_count"], 1)
		self.assertEqual(first_values["carried_by"], "staff@example.com")

		second_values = calls[1].args[2]
		# Already carried once: the original shift is preserved …
		self.assertNotIn("carried_from_shift", second_values)
		# … and the recurrence is what advances.
		self.assertEqual(second_values["carry_count"], 3)

	def test_stamp_carried_survives_one_unstampable_row(self):
		"""A close that has already submitted must not roll back over a stamp."""
		from jarz_pos.services.courier_carry import stamp_carried

		mock_frappe = _make_mock_frappe()
		mock_frappe.db.set_value.side_effect = [Exception("locked"), None]

		with patch("jarz_pos.services.courier_carry.frappe", mock_frappe), \
				 patch("jarz_pos.services.courier_carry.now_datetime", return_value=_FIXED_NOW):
			result = stamp_carried(
				[
					{"courier_transaction": "CT-0001", "net_balance": 90},
					{"courier_transaction": "CT-0002", "net_balance": 70},
				],
				opening_entry="POS-OPE-2026-00003",
			)

		self.assertEqual(result["transactions"], ["CT-0002"])
		self.assertEqual(result["net_balance"], 70)

	def test_mark_settled_records_the_shift_that_received_the_cash(self):
		from jarz_pos.services.courier_carry import mark_settled

		mock_frappe = _make_mock_frappe()

		with patch("jarz_pos.services.courier_carry.frappe", mock_frappe), \
				 patch("jarz_pos.services.courier_carry.now_datetime", return_value=_FIXED_NOW), \
				 patch(
					 "jarz_pos.utils.access_control.get_open_shift_for_profile",
					 return_value={"name": "POS-OPE-2026-00009"},
				 ):
			done = mark_settled(["CT-0001"], pos_profile="Dokki")

		self.assertEqual(done, ["CT-0001"])
		values = mock_frappe.db.set_value.call_args.args[2]
		self.assertEqual(values["status"], "Settled")
		self.assertEqual(values["settled_in_shift"], "POS-OPE-2026-00009")
		self.assertEqual(values["settled_by"], "staff@example.com")

	def test_mark_settled_still_settles_before_the_columns_exist(self):
		"""Code reaches a server before `bench migrate` does.

		A settlement in that window must flip the status and lose only the
		attribution — writing a column that is not there yet would 500.
		"""
		from jarz_pos.services.courier_carry import mark_settled

		mock_frappe = _make_mock_frappe()
		mock_frappe.db.has_column.return_value = False

		with patch("jarz_pos.services.courier_carry.frappe", mock_frappe), \
				 patch("jarz_pos.services.courier_carry.now_datetime", return_value=_FIXED_NOW):
			done = mark_settled(["CT-0001"], pos_profile="Dokki")

		self.assertEqual(done, ["CT-0001"])
		values = mock_frappe.db.set_value.call_args.args[2]
		self.assertEqual(values, {"status": "Settled"})

	def test_stamp_carried_is_a_no_op_before_the_columns_exist(self):
		from jarz_pos.services.courier_carry import stamp_carried

		mock_frappe = _make_mock_frappe()
		mock_frappe.db.has_column.return_value = False

		with patch("jarz_pos.services.courier_carry.frappe", mock_frappe), \
				 patch("jarz_pos.services.courier_carry.now_datetime", return_value=_FIXED_NOW):
			result = stamp_carried(
				[{"courier_transaction": "CT-0001", "net_balance": 90}],
				opening_entry="POS-OPE-2026-00003",
			)

		# The close still goes through; only the attribution is lost.
		self.assertEqual(result["count"], 0)
		self.assertTrue(result["unstamped"])
		mock_frappe.db.set_value.assert_not_called()

	def test_mark_settled_without_a_branch_still_settles(self):
		"""Partner settlements hit the bank, not a till — no shift to stamp."""
		from jarz_pos.services.courier_carry import mark_settled

		mock_frappe = _make_mock_frappe()

		with patch("jarz_pos.services.courier_carry.frappe", mock_frappe), \
				 patch("jarz_pos.services.courier_carry.now_datetime", return_value=_FIXED_NOW):
			done = mark_settled(["CT-0001"], extra={"journal_entry": "JE-0001"})

		self.assertEqual(done, ["CT-0001"])
		values = mock_frappe.db.set_value.call_args.args[2]
		self.assertEqual(values["status"], "Settled")
		self.assertNotIn("settled_in_shift", values)
		self.assertEqual(values["journal_entry"], "JE-0001")


if __name__ == "__main__":
	unittest.main()
