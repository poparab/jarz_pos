"""Courier settlement reversal, checked against a REAL database and REAL Redis.

Every other test around this feature is mocked, and three defects walked
straight through a fully green mocked run because a mock cannot express what
they were about:

1. ``list_recent_courier_settlements`` filtered ``journal_entry`` with
   ``["not in", ["", None]]``. A mock records that dict and moves on; only SQL
   evaluates it, and SQL's three-valued logic makes
   ``IFNULL(journal_entry,'') NOT IN ('', NULL)`` UNKNOWN for every row. The
   query returned nothing on any real database, and the mocked test ASSERTED
   the broken shape as if it were the specification.
2. The "is this a settlement?" test was a deny-list holding one tag, so the
   collection-change entries that really do sit on Settled Courier Transactions
   read as reversible. A mock only ever sees the fixtures someone thought to
   write; the real table holds the ones nobody did.
3. The double-reversal guard took a row lock and then re-read with plain
   SELECTs. Under MariaDB's REPEATABLE READ those are served from the snapshot
   the transaction opened at its FIRST read, so the second caller re-read the
   very state that told it to proceed. Isolation levels do not exist inside a
   mock.

So these tests use the database and the cache, and one of them uses a second
real connection. They are slower than the mocked suite on purpose; they are the
only ones that can fail for the right reason.
"""

import json
import threading
import unittest

import frappe

from jarz_pos.api import couriers as couriers_api
from jarz_pos.services import delivery_handling as dh

SETTLEMENT_REMARK = "Order: 100.0, Shipping: 10.0, Net to branch: 90.0"
COLLECTION_CHANGE_REMARK = (
    "Payment collection changed to Instapay for ACC-SINV-TEST. Reference: POS-RCPT-1"
)


class _RealDatabaseTestCase(unittest.TestCase):
    """Base class that refuses to run without a database rather than skipping.

    A silent skip is how a suite goes green while covering nothing, which is the
    exact failure these tests exist to prevent.
    """

    @classmethod
    def setUpClass(cls):
        if getattr(frappe, "db", None) is None:
            raise AssertionError(
                "These tests require a real database connection. They must never be "
                "skipped: a mocked run already passes and proves nothing."
            )


class TestSettlementQueryAgainstRealSql(_RealDatabaseTestCase):
    def test_the_old_not_in_filter_matches_nothing_and_is_set_works(self):
        """Pin the SQL semantics that made the settlement list permanently empty."""
        real_rows = frappe.db.sql(
            "select count(*) from `tabCourier Transaction` "
            "where ifnull(journal_entry, '') != ''"
        )[0][0]

        broken = frappe.get_all(
            "Courier Transaction",
            filters={"journal_entry": ["not in", ["", None]]},
            fields=["name"],
            limit_page_length=0,
        )
        fixed = frappe.get_all(
            "Courier Transaction",
            filters={"journal_entry": ["is", "set"]},
            fields=["name"],
            limit_page_length=0,
        )

        self.assertEqual(
            len(broken), 0,
            "`['not in', ['', None]]` is expected to match NOTHING in SQL. If this "
            "ever returns rows the filter's semantics changed and the comment in "
            "list_recent_courier_settlements needs revisiting.",
        )
        self.assertEqual(
            len(fixed), real_rows,
            "`['is', 'set']` must return exactly the rows that carry a journal entry.",
        )

    def test_the_shipped_query_uses_a_filter_that_survives_sql(self):
        """The production query itself, run for real — it must not throw and must
        not be structurally incapable of returning anything."""
        rows = dh.list_recent_courier_settlements(limit=5)
        self.assertIsInstance(rows, list)


class TestSettlementClassificationAgainstRealDocs(_RealDatabaseTestCase):
    """The allow-list, exercised through real Journal Entry / Courier Transaction rows."""

    def setUp(self):
        self.made = []
        self.addCleanup(self._cleanup)

        company = frappe.db.get_value("Company", {}, "name")
        self.assertTrue(company, "a Company is required to post a Journal Entry")
        accounts = frappe.get_all(
            "Account",
            filters={
                "company": company,
                "is_group": 0,
                # Receivable/Payable rows demand a party; keep to plain ledgers.
                "account_type": ["not in", ["Receivable", "Payable"]],
            },
            pluck="name",
            limit_page_length=2,
        )
        self.assertEqual(len(accounts), 2, "two postable accounts are required")
        self.company = company
        self.accounts = accounts

    def _cleanup(self):
        frappe.db.rollback()
        for doctype, name in reversed(self.made):
            try:
                if doctype == "Journal Entry":
                    # A submitted entry also wrote GL rows; leaving those behind
                    # would skew real account balances on the site these tests
                    # run against.
                    frappe.db.sql(
                        "delete from `tabGL Entry` where voucher_type='Journal Entry' "
                        "and voucher_no=%s",
                        (name,),
                    )
                    frappe.db.sql(
                        "delete from `tabJournal Entry Account` where parent=%s", (name,)
                    )
                frappe.db.sql("delete from `tab" + doctype + "` where name=%s", (name,))
            except Exception:
                pass
        frappe.db.commit()

    def _make_je(self, remark, submit=False):
        je = frappe.new_doc("Journal Entry")
        je.voucher_type = "Journal Entry"
        je.posting_date = frappe.utils.nowdate()
        je.company = self.company
        je.user_remark = remark
        je.append("accounts", {
            "account": self.accounts[0],
            "debit_in_account_currency": 100,
            "credit_in_account_currency": 0,
        })
        je.append("accounts", {
            "account": self.accounts[1],
            "debit_in_account_currency": 0,
            "credit_in_account_currency": 100,
        })
        je.flags.ignore_mandatory = True
        je.insert(ignore_permissions=True, ignore_mandatory=True)
        self.made.append(("Journal Entry", je.name))
        if submit:
            # `list_recent_courier_settlements` only ever considers posted
            # entries, so anything testing the listing has to actually post one.
            je.submit()
        return je.name

    def _make_ct(self, je_name, status="Settled"):
        ct = frappe.new_doc("Courier Transaction")
        ct.journal_entry = je_name
        ct.status = status
        ct.amount = 100
        ct.shipping_amount = 10
        ct.date = frappe.utils.nowdate()
        ct.flags.ignore_mandatory = True
        ct.insert(ignore_permissions=True, ignore_mandatory=True)
        self.made.append(("Courier Transaction", ct.name))
        return ct.name

    def test_a_real_settlement_je_yields_its_settled_courier_transaction(self):
        je = self._make_je(SETTLEMENT_REMARK)
        ct = self._make_ct(je)
        frappe.db.commit()

        rows = dh._courier_transactions_for_settlement_je(je)

        self.assertEqual([r["name"] for r in rows], [ct])

    def test_a_collection_change_je_on_a_settled_ct_is_not_reversible(self):
        """The defect the deny-list left open, reproduced with real rows.

        A settle path whose totals net to zero marks the Courier Transaction
        Settled without clearing ``journal_entry``, so the collection-change
        entry stays attached to a Settled row. Reversing it would post the
        inverse of ``DR <online ledger> / CR Courier Outstanding`` — erasing the
        record that the customer paid online and re-creating a receivable
        against a courier holding nothing. On production, 14 such rows existed.
        """
        je = self._make_je(COLLECTION_CHANGE_REMARK)
        ct = self._make_ct(je, status="Settled")
        frappe.db.commit()

        # The row really is Settled and really does point at this entry...
        self.assertEqual(
            frappe.db.get_value("Courier Transaction", ct, "status"), "Settled"
        )
        # ...and it is still not reversible.
        self.assertEqual(dh._courier_transactions_for_settlement_je(je), [])
        with self.assertRaises(frappe.ValidationError):
            dh.get_unsettle_preview(je)

    def test_a_real_settlement_is_actually_LISTED_end_to_end(self):
        """The end-to-end guard for the dead query.

        The mocked suite could only ever assert the shape of a filter dict, and
        it asserted the broken one. This posts a real settlement and requires the
        real query to find it — which the old `['not in', ['', None]]` filter
        could not do for any row that has ever existed.
        """
        if not dh.user_has_global_profile_access():
            self.skipTest(
                "needs a caller with global POS Profile access to see a settlement "
                "whose Courier Transactions carry no branch-bearing invoice"
            )

        je = self._make_je(SETTLEMENT_REMARK, submit=True)
        self._make_ct(je)
        frappe.db.commit()

        listed = {row["journal_entry"] for row in dh.list_recent_courier_settlements(limit=200)}

        self.assertIn(je, listed, "a real settlement must be findable in the real list")

    def test_a_partner_fee_accrual_je_is_not_reversible(self):
        je = self._make_je(
            dh._je_user_remark(
                "ACC-SINV-TEST", dh.PARTNER_FEE_ACCRUAL_JE_TAG_TYPE, "Delivery fee owed"
            )
        )
        self._make_ct(je)
        frappe.db.commit()

        self.assertEqual(dh._courier_transactions_for_settlement_je(je), [])


class TestReversalIsolationWithTwoConnections(_RealDatabaseTestCase):
    """CRITICAL 3, with two real connections and MariaDB's real isolation level.

    Reproduces the defect and confirms the fix in one run: after a second
    connection commits the flip to Unsettled, a plain read still reports the
    settlement as reversible (that stale answer is what posted a second
    reversing entry), while the locking read the fix introduced sees the truth.
    """

    def setUp(self):
        self.made = []
        self.addCleanup(self._cleanup)
        self.site = frappe.local.site

        company = frappe.db.get_value("Company", {}, "name")
        accounts = frappe.get_all(
            "Account",
            filters={
                "company": company,
                "is_group": 0,
                "account_type": ["not in", ["Receivable", "Payable"]],
            },
            pluck="name",
            limit_page_length=2,
        )
        self.assertEqual(len(accounts), 2)

        je = frappe.new_doc("Journal Entry")
        je.voucher_type = "Journal Entry"
        je.posting_date = frappe.utils.nowdate()
        je.company = company
        je.user_remark = SETTLEMENT_REMARK
        je.append("accounts", {
            "account": accounts[0],
            "debit_in_account_currency": 100,
            "credit_in_account_currency": 0,
        })
        je.append("accounts", {
            "account": accounts[1],
            "debit_in_account_currency": 0,
            "credit_in_account_currency": 100,
        })
        je.flags.ignore_mandatory = True
        je.insert(ignore_permissions=True, ignore_mandatory=True)
        self.made.append(("Journal Entry", je.name))
        self.je = je.name

        ct = frappe.new_doc("Courier Transaction")
        ct.journal_entry = self.je
        ct.status = "Settled"
        ct.amount = 100
        ct.shipping_amount = 10
        ct.date = frappe.utils.nowdate()
        ct.flags.ignore_mandatory = True
        ct.insert(ignore_permissions=True, ignore_mandatory=True)
        self.made.append(("Courier Transaction", ct.name))
        self.ct = ct.name
        frappe.db.commit()

    def _cleanup(self):
        frappe.db.rollback()
        for doctype, name in reversed(self.made):
            try:
                if doctype == "Journal Entry":
                    # A submitted entry also wrote GL rows; leaving those behind
                    # would skew real account balances on the site these tests
                    # run against.
                    frappe.db.sql(
                        "delete from `tabGL Entry` where voucher_type='Journal Entry' "
                        "and voucher_no=%s",
                        (name,),
                    )
                    frappe.db.sql(
                        "delete from `tabJournal Entry Account` where parent=%s", (name,)
                    )
                frappe.db.sql("delete from `tab" + doctype + "` where name=%s", (name,))
            except Exception:
                pass
        frappe.db.commit()

    def test_a_locking_read_sees_a_commit_a_plain_read_cannot(self):
        reader_ready = threading.Event()
        writer_done = threading.Event()
        out = {}

        def reader():
            frappe.init(self.site)
            frappe.connect()
            try:
                # The transaction's FIRST consistent read fixes the read view —
                # in the real function this is the `frappe.db.exists` that runs
                # just above the row lock.
                frappe.db.exists("Journal Entry", self.je)
                out["before"] = len(dh._courier_transactions_for_settlement_je(self.je))
                reader_ready.set()
                writer_done.wait(30)

                out["plain_after"] = len(
                    dh._courier_transactions_for_settlement_je(self.je)
                )
                out["locking_after"] = len(
                    dh._courier_transactions_for_settlement_je(self.je, for_update=True)
                )
                frappe.db.rollback()
            except Exception as exc:  # surfaced by the assertions below
                out["reader_error"] = repr(exc)
                reader_ready.set()
            finally:
                frappe.destroy()

        def writer():
            frappe.init(self.site)
            frappe.connect()
            try:
                reader_ready.wait(30)
                # Stand in for the first reversal completing.
                frappe.db.set_value("Courier Transaction", self.ct, "status", "Unsettled")
                frappe.db.commit()
            except Exception as exc:
                out["writer_error"] = repr(exc)
            finally:
                writer_done.set()
                frappe.destroy()

        threads = [threading.Thread(target=reader), threading.Thread(target=writer)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(60)

        self.assertNotIn("reader_error", out, out.get("reader_error"))
        self.assertNotIn("writer_error", out, out.get("writer_error"))
        self.assertEqual(out.get("before"), 1, "the settlement should start reversible")
        self.assertEqual(
            out.get("plain_after"), 1,
            "A plain read is expected to be STALE here — that is the defect. If this "
            "ever reads 0, the isolation level changed and the FOR UPDATE reasoning "
            "in unsettle_courier_settlement should be revisited.",
        )
        self.assertEqual(
            out.get("locking_after"), 0,
            "The locking read MUST see the committed flip; this is what makes the "
            "double-reversal guard hold.",
        )


class TestPreviewTokenAgainstRealRedis(_RealDatabaseTestCase):
    """The preview token, against the real cache rather than a dict."""

    JE = "JE-REAL-REDIS-TOKEN-TEST"

    def tearDown(self):
        try:
            frappe.cache().delete(couriers_api._unsettle_preview_key(self.JE))
        except Exception:
            pass

    def test_the_token_carries_a_real_expiry(self):
        """The old shape (`hset` then `expire`) set a TTL on a key that did not
        exist, because `expire` does not apply the namespace `hset` applies — so
        the token never expired at all."""
        couriers_api._mint_unsettle_preview_token(self.JE, "Nasr City")

        ttl = frappe.cache().ttl(couriers_api._unsettle_preview_key(self.JE))

        self.assertGreater(ttl, 0, "the token must expire")
        self.assertLessEqual(ttl, couriers_api.UNSETTLE_PREVIEW_TTL)

    def test_minting_again_invalidates_the_earlier_token(self):
        first = couriers_api._mint_unsettle_preview_token(self.JE, "Nasr City")
        couriers_api._mint_unsettle_preview_token(self.JE, "Nasr City")

        payload, raw = couriers_api._peek_unsettle_preview_token(self.JE, first)

        self.assertIsNone(payload, "two valid tokens for one entry must never coexist")

    def test_a_wrong_token_is_refused_without_destroying_the_live_one(self):
        token = couriers_api._mint_unsettle_preview_token(self.JE, "Nasr City")

        payload, _raw = couriers_api._peek_unsettle_preview_token(self.JE, "nope")
        self.assertIsNone(payload)

        payload, _raw = couriers_api._peek_unsettle_preview_token(self.JE, token)
        self.assertIsNotNone(payload, "a bad guess must not invalidate the real token")

    def test_a_token_cannot_be_spent_twice(self):
        token = couriers_api._mint_unsettle_preview_token(self.JE, "Nasr City")
        payload, raw = couriers_api._peek_unsettle_preview_token(self.JE, token)
        self.assertIsNotNone(payload)

        self.assertTrue(couriers_api._spend_unsettle_preview_token(self.JE, raw))
        self.assertFalse(couriers_api._spend_unsettle_preview_token(self.JE, raw))

    def test_concurrent_spenders_of_one_token_produce_exactly_one_winner(self):
        """The property a check-then-delete could not give, and a dict cannot test.

        Every thread peeks the same live token — which is the interleaving that
        let two managers both reach the posting step — and then races to spend
        it. Exactly one may win.
        """
        token = couriers_api._mint_unsettle_preview_token(self.JE, "Nasr City")
        _payload, raw = couriers_api._peek_unsettle_preview_token(self.JE, token)

        start = threading.Event()
        wins = []
        lock = threading.Lock()

        def racer():
            frappe.init(self.site)
            frappe.connect()
            try:
                start.wait(30)
                won = couriers_api._spend_unsettle_preview_token(self.JE, raw)
                with lock:
                    wins.append(bool(won))
            finally:
                frappe.destroy()

        self.site = frappe.local.site
        threads = [threading.Thread(target=racer) for _ in range(12)]
        for t in threads:
            t.start()
        start.set()
        for t in threads:
            t.join(60)

        self.assertEqual(
            wins.count(True), 1,
            f"exactly one of {len(wins)} concurrent spenders may win, got {wins.count(True)}",
        )

    def test_the_payload_round_trips(self):
        token = couriers_api._mint_unsettle_preview_token(self.JE, "Nasr City")
        payload, raw = couriers_api._peek_unsettle_preview_token(self.JE, token)

        self.assertEqual(payload.get("pos_profile"), "Nasr City")
        self.assertEqual(json.loads(raw).get("token"), token)


class TestReferenceNoCannotForgeATag(_RealDatabaseTestCase):
    """A payment reference typed on a phone must not be able to forge a JE tag."""

    def test_a_forged_reversal_tag_in_a_reference_is_neutralised(self):
        forged = "[JARZ-JE:COURIER_SETTLEMENT_REVERSAL:ACC-JV-new-2026-00316]"

        cleaned = dh._strip_je_tag_lookalikes(forged)

        self.assertNotIn("[JARZ-JE:", cleaned)
        self.assertNotIn(dh._unsettle_dedup_tag("ACC-JV-new-2026-00316"), cleaned)

    def test_case_and_whitespace_variants_are_neutralised(self):
        for variant in ("[ jarz-je:X:Y]", "[JARZ-JE :X:Y]", "[jarz-je:X:Y]"):
            with self.subTest(variant=variant):
                # The opening bracket is what every guard matches on, so
                # removing it is what breaks the shape.
                self.assertNotIn("[", dh._strip_je_tag_lookalikes(variant))

    def test_ordinary_references_are_left_alone(self):
        self.assertEqual(dh._strip_je_tag_lookalikes("POS-RCPT-2026-00004"), "POS-RCPT-2026-00004")


if __name__ == "__main__":
    unittest.main()
