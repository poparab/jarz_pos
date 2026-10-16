"""The kanban transition matrix after the Ready stage was removed (2026-10-13).

Flow: Received (stored "Recieved") -> In Progress -> Out for Delivery -> Delivered.
No backward moves of any kind. A legacy "Ready" row not yet converted by the
``kanban_drop_ready_state`` patch is read as In Progress so it can still go out.

Pure ``unittest`` - no site, no DB, no ``erpnext.tests.utils``.
"""

import unittest
from unittest.mock import MagicMock, patch

from jarz_pos.api import kanban


def block(old, new):
	return kanban._transition_block_reason(old, new)


class TestStateSequence(unittest.TestCase):
	def test_sequence_has_no_ready_stage(self):
		self.assertEqual(
			kanban._STATE_SEQUENCE,
			["recieved", "in_progress", "out_for_delivery", "delivered"],
		)
		self.assertNotIn("ready", kanban._STATE_LABELS)

	def test_legacy_ready_is_an_alias_of_in_progress(self):
		self.assertEqual(kanban._STATE_ALIASES.get("ready"), "in_progress")
		self.assertEqual(kanban._sequence_index("Ready"), kanban._sequence_index("In Progress"))


class TestForwardMoves(unittest.TestCase):
	def test_each_single_forward_step_is_allowed(self):
		self.assertIsNone(block("Recieved", "In Progress"))
		self.assertIsNone(block("In Progress", "Out for Delivery"))
		self.assertIsNone(block("Out for Delivery", "Delivered"))

	def test_both_spellings_of_received_work(self):
		self.assertIsNone(block("Received", "In Progress"))
		self.assertIsNone(block("Recieved", "In Progress"))

	def test_empty_state_counts_as_received(self):
		self.assertIsNone(block("", "In Progress"))
		self.assertIsNone(block(None, "In Progress"))

	def test_legacy_ready_row_can_still_be_dispatched(self):
		self.assertIsNone(block("Ready", "Out for Delivery"))

	def test_legacy_ready_row_may_be_dragged_to_in_progress(self):
		# Same stage under another spelling - not a backward move.
		self.assertIsNone(block("Ready", "In Progress"))


class TestSkippedStagesAreRefused(unittest.TestCase):
	def test_received_cannot_jump_to_out_for_delivery(self):
		reason = block("Recieved", "Out for Delivery")
		self.assertIsNotNone(reason)
		self.assertIn("one stage at a time", reason)

	def test_in_progress_cannot_jump_to_delivered(self):
		self.assertIn("one stage at a time", block("In Progress", "Delivered") or "")

	def test_received_cannot_jump_to_delivered(self):
		self.assertIsNotNone(block("Recieved", "Delivered"))


class TestNoBackwardMoves(unittest.TestCase):
	BACKWARD = [
		("In Progress", "Recieved"),
		("In Progress", "Received"),
		("Out for Delivery", "In Progress"),
		("Out for Delivery", "Recieved"),
		("Delivered", "Out for Delivery"),
		("Delivered", "In Progress"),
		("Delivered", "Recieved"),
		("Ready", "Recieved"),
	]

	def test_every_backward_move_is_refused(self):
		for old, new in self.BACKWARD:
			with self.subTest(old=old, new=new):
				reason = block(old, new)
				self.assertIsNotNone(reason)
				self.assertIn("backward", reason)

	def test_the_old_ready_exception_is_gone(self):
		# Out for Delivery -> In Progress used to be reachable via Ready.
		self.assertIn("backward", block("Out for Delivery", "In Progress") or "")

	def test_ready_as_a_target_is_the_in_progress_stage(self):
		# "Ready" as a destination resolves to the In Progress stage, so moving
		# an Out for Delivery card there is a backward move like any other.
		self.assertIn("backward", block("Out for Delivery", "Ready") or "")


class TestNonDragTargets(unittest.TestCase):
	def test_cancelled_and_returned_are_not_drag_targets(self):
		self.assertIn("Cancel Order", block("In Progress", "Cancelled") or "")
		self.assertIn("Return Order", block("Delivered", "Returned") or "")

	def test_unknown_labels_are_left_to_other_checks(self):
		self.assertIsNone(block("In Progress", "Something Else"))
		self.assertIsNone(block("Something Else", "Delivered"))


class TestOldClientSendsReady(unittest.TestCase):
	"""A client on the pre-2026-10-13 build, or an offline move queued before
	the update, still sends "Ready"; it must mean In Progress, not be refused."""

	def test_ready_request_passes_state_validation(self):
		stop = RuntimeError("reached get_doc")
		allowed = ["Recieved", "In Progress", "Out for Delivery", "Delivered"]
		with (
			patch.object(kanban, "_get_allowed_states", return_value=allowed),
			patch.object(kanban.frappe, "has_permission", return_value=True),
			patch.object(kanban.frappe, "logger", MagicMock()),
			patch.object(kanban.frappe.utils, "now", return_value="2026-10-13 12:00:00"),
			patch.object(kanban.frappe, "get_doc", side_effect=stop) as get_doc,
			patch.object(kanban.frappe, "log_error", MagicMock()),
		):
			result = kanban.update_invoice_state("SINV-1", "Ready")
		get_doc.assert_called_once_with("Sales Invoice", "SINV-1")
		self.assertNotIn("not a valid state", str(result))


if __name__ == "__main__":
	unittest.main()
