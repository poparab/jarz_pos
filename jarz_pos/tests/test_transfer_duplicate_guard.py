"""submit_transfer's duplicate-tap guard, against the site's real Redis.

On 2026-10-27, while production was throttled, one direct Finished Goods ->
Nasr City transfer was posted twice 16 s apart. What is pinned:

* A second identical request (same user, warehouses, lines) inside the window
  is refused, and the message names what the first one posted.
* Line order does not matter; a different qty, warehouse or user is a
  different transfer and goes through.
* A failed post releases the claim, so the user can retry at once.
* The pending-handover path is guarded too, and records the handover name.
* Two requests racing in threads: exactly one gets the claim (SET NX).

Stock posting itself is patched out; only the claim touches Redis, under keys
unique to each test run.
"""

import threading
import unittest
import uuid
from unittest.mock import patch

import frappe

from jarz_pos.api import transfer as transfer_api
from jarz_pos.services import stock_handover as svc


class _SE:
	def __init__(self, name):
		self.name = name


class TestTransferDuplicateGuard(unittest.TestCase):
	def setUp(self):
		# Unique warehouses per test, so keys never collide with a previous run.
		tag = uuid.uuid4().hex[:8]
		self.src, self.tgt = f"SRC-{tag}", f"TGT-{tag}"
		self.lines = [{"item_code": "JAR-L", "qty": 5}, {"item_code": "JAR-M", "qty": 2}]
		self.posted = []
		for p in (
			patch.object(transfer_api, "_ensure_transfer_access"),
			patch.object(svc, "touches_branch", return_value=False),
			patch.object(transfer_api, "_build_material_transfer", side_effect=self._build),
			patch.object(transfer_api, "_insert_and_submit_stock_entry"),
			patch.object(transfer_api.frappe.db, "commit"),
		):
			p.start()
			self.addCleanup(p.stop)

	def _build(self, *args, **kwargs):
		se = _SE(f"MAT-STE-TEST-{len(self.posted) + 1}")
		self.posted.append(se.name)
		return se

	def _send(self, lines=None, src=None, tgt=None):
		return transfer_api.submit_transfer(
			source_warehouse=src or self.src, target_warehouse=tgt or self.tgt, lines=lines or self.lines
		)

	def test_repeat_is_refused_and_names_the_first_entry(self):
		first = self._send()
		self.assertEqual(first["stock_entry"], "MAT-STE-TEST-1")
		with self.assertRaises(frappe.ValidationError) as ctx:
			self._send(lines=list(reversed(self.lines)))
		self.assertIn("MAT-STE-TEST-1", str(ctx.exception))
		self.assertEqual(self.posted, ["MAT-STE-TEST-1"])

	def test_different_transfers_are_not_duplicates(self):
		self._send()
		self._send(lines=[{"item_code": "JAR-L", "qty": 6}, {"item_code": "JAR-M", "qty": 2}])
		self._send(tgt=self.tgt + "-2")
		with patch.dict(frappe.session, {"user": "someone.else@example.com"}):
			self._send()
		self.assertEqual(len(self.posted), 4)

	def test_failed_post_releases_the_claim(self):
		with patch.object(transfer_api, "_insert_and_submit_stock_entry", side_effect=RuntimeError("boom")):
			with self.assertRaises(RuntimeError):
				self._send()
		self.assertEqual(self._send()["stock_entry"], "MAT-STE-TEST-2")

	def test_pending_handover_is_guarded(self):
		with (
			patch.object(svc, "touches_branch", return_value=True),
			patch.object(svc, "create_manager_handover", return_value="HND-TEST-1") as create,
		):
			self.assertEqual(self._send()["handover"], "HND-TEST-1")
			with self.assertRaises(frappe.ValidationError) as ctx:
				self._send()
		self.assertIn("HND-TEST-1", str(ctx.exception))
		create.assert_called_once()

	def test_concurrent_claims_only_one_wins(self):
		results = []
		barrier = threading.Barrier(2)
		site, user = frappe.local.site, frappe.session.user

		def claim():
			frappe.init(site=site)
			frappe.connect()
			frappe.set_user(user)
			barrier.wait(timeout=10)
			try:
				results.append(
					transfer_api._claim_transfer(
						self.src, self.tgt, transfer_api._clean_transfer_lines(self.lines)
					)
				)
			except frappe.ValidationError:
				results.append("refused")
			finally:
				frappe.destroy()

		threads = [threading.Thread(target=claim) for _ in range(2)]
		[t.start() for t in threads]
		[t.join() for t in threads]
		self.assertEqual(results.count("refused"), 1)
		self.assertEqual(len([r for r in results if r not in ("refused", None)]), 1)


if __name__ == "__main__":
	unittest.main()
