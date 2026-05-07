import unittest
from unittest.mock import MagicMock, patch


class TestPOSClosingEntryOverride(unittest.TestCase):
	def test_update_opening_entry_saves_with_ignore_permissions(self):
		from jarz_pos.overrides.pos_closing_entry import POSClosingEntry

		mock_frappe = MagicMock()
		opening_entry = MagicMock()
		mock_frappe.get_doc.return_value = opening_entry

		closing_entry = POSClosingEntry.__new__(POSClosingEntry)
		closing_entry.name = "POS-CLO-2026-00002"
		closing_entry.pos_opening_entry = "POS-OPE-2026-00001"

		with patch("jarz_pos.overrides.pos_closing_entry.frappe", mock_frappe):
			POSClosingEntry.update_opening_entry(closing_entry)

		self.assertEqual(opening_entry.pos_closing_entry, "POS-CLO-2026-00002")
		self.assertTrue(opening_entry.flags.ignore_permissions)
		opening_entry.set_status.assert_called_once_with()
		opening_entry.save.assert_called_once_with(ignore_permissions=True)