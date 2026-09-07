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

A fourth defect, same shape, was found on 2026-09-07 in the FORWARD half of the
pair — ``generate_settlement_preview`` / ``confirm_settlement``. Its token was
minted with ``hset`` followed by ``expire`` on the un-namespaced name, so it had
no expiry at all, and it was consumed with a ``hget`` … post … ``delete_value``
that two callers could both pass. A dict-backed fake cache has neither a
namespace nor a TTL nor concurrency, which is precisely why both survived a
green mocked suite. See ``TestSettlementPreviewTokenAgainstRealRedis``.

So these tests use the database and the cache, and two of them use additional
real connections. They are slower than the mocked suite on purpose; they are the
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

#: The provenance stamp a batch settlement posted today carries. `SETTLEMENT_REMARK`
#: alone is the LEGACY shape — an entry from before `custom_jarz_je_tag` existed —
#: and is now only trusted on entries older than the field itself.
BATCH_TAG = dh._je_dedup_tag("EMP-TEST-001", dh.BATCH_SETTLEMENT_JE_TAG_TYPE)


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
        """Roll back, then sweep — belt and braces on a site holding real data.

        Nothing in this class is ever committed: the rows only have to be
        visible to THIS connection, and one of these tests posts a real Journal
        Entry with real GL rows. So the rollback alone should undo everything
        and the deletes below should find nothing. They stay anyway, because a
        stray submitted entry left behind on the CI site would silently skew
        account balances, and that is not a risk worth carrying to save four
        lines.
        """
        frappe.db.rollback()
        for doctype, name in reversed(self.made):
            try:
                if doctype == "Journal Entry":
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

    def _make_je(self, remark, submit=False, stamp=None):
        """Post a Journal Entry. ``stamp`` fills ``custom_jarz_je_tag``.

        Since 2026-09-07 an entry is classified by that field, not by its
        remark, and only entries older than the field itself fall back to the
        remark — so a fixture standing in for something the app posted TODAY has
        to carry the stamp, exactly as `_tag_journal_entry` writes it. A fixture
        with only the remark now models a FOREIGN entry, which is a different
        test (see TestAForeignJournalEntryCannotSatisfyTheTagLookups).
        """
        je = frappe.new_doc("Journal Entry")
        je.voucher_type = "Journal Entry"
        je.posting_date = frappe.utils.nowdate()
        je.company = self.company
        je.user_remark = remark
        if stamp is not None and dh._je_tag_field_exists():
            je.set(dh._JE_TAG_FIELDNAME, stamp)
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
        je = self._make_je(SETTLEMENT_REMARK, stamp=BATCH_TAG)
        ct = self._make_ct(je)
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
        # Stamped with its OWN (collection-change) tag, which is what the app
        # really writes — so this pins that the allow-list refuses a genuine,
        # app-posted entry of the wrong KIND, not merely an unstamped one.
        je = self._make_je(
            COLLECTION_CHANGE_REMARK,
            stamp=dh._je_dedup_tag("ACC-SINV-TEST", "COLLECTION_CHANGE-a1b2c3d4e5f6"),
        )
        ct = self._make_ct(je, status="Settled")
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

        je = self._make_je(SETTLEMENT_REMARK, submit=True, stamp=BATCH_TAG)
        self._make_ct(je)
        listed = {row["journal_entry"] for row in dh.list_recent_courier_settlements(limit=200)}

        self.assertIn(je, listed, "a real settlement must be findable in the real list")

    def test_a_partner_fee_accrual_je_is_not_reversible(self):
        je = self._make_je(
            dh._je_user_remark(
                "ACC-SINV-TEST", dh.PARTNER_FEE_ACCRUAL_JE_TAG_TYPE, "Delivery fee owed"
            ),
            stamp=dh._je_dedup_tag("ACC-SINV-TEST", dh.PARTNER_FEE_ACCRUAL_JE_TAG_TYPE),
        )
        self._make_ct(je)
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
        if dh._je_tag_field_exists():
            je.set(dh._JE_TAG_FIELDNAME, BATCH_TAG)
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


class TestSettlementPreviewTokenAgainstRealRedis(_RealDatabaseTestCase):
    """The FORWARD preview token — the permit ``confirm_settlement`` posts on.

    The reversal's token was fixed in 446a0f1; the settlement preview that
    shipped alongside it kept the original broken shape for another two weeks,
    for exactly the reason this file exists: every test around it was mocked,
    and a dict-backed fake cache has no namespace and no TTL, so neither the
    dead ``expire`` nor the check-then-act consume could fail there.
    """

    def setUp(self):
        self.minted = []
        self.addCleanup(self._cleanup)

    def _mint(self, **overrides):
        data = {
            "invoice": "ACC-SINV-REDIS-TOKEN-TEST",
            "party_type": "Employee",
            "party": "HR-EMP-TEST",
            "mode": "pay_now",
            "order_amount": 100.5,
            "shipping_amount": 10.0,
            "net_amount": 90.5,
            "is_unpaid_effective": True,
            "last_payment_seconds": None,
            "is_partner_order": False,
            "delivery_partner": None,
            "partner_fee": 0.0,
            "is_online_unconfirmed": False,
        }
        data.update(overrides)
        token = couriers_api._mint_settle_preview_token(data)
        self.minted.append(token)
        return token

    def _cleanup(self):
        for token in self.minted:
            try:
                frappe.cache().delete(couriers_api._settle_preview_key(token))
            except Exception:
                pass

    def test_frappes_expire_really_does_not_namespace_the_key(self):
        """Pin the Redis semantics that made the old token immortal.

        ``hset`` pushes the name through ``make_key`` (``<db_name>|…``);
        ``expire`` is inherited raw from ``redis.Redis`` and does not. So
        ``hset(k, …)`` then ``expire(k, 180)`` sets a TTL on a key that does not
        exist — it returns False — and the real hash keeps no expiry at all.
        If this test ever fails, Frappe changed the behaviour and the comment on
        ``_mint_settle_preview_token`` needs revisiting. ``expire_key`` is the
        namespacing helper the old code should have reached for.
        """
        cache = frappe.cache()
        raw_name = "jarz_pos:settle_preview:OLD-SHAPE-PROBE"
        real_key = cache.make_key(raw_name)
        self.addCleanup(lambda: cache.delete(real_key))
        self.addCleanup(lambda: cache.delete(raw_name))

        cache.hset(raw_name, "data", {"invoice": "X"})
        expired = cache.expire(raw_name, 180)

        self.assertFalse(
            expired,
            "`expire` on the un-namespaced name must miss — it is the whole bug",
        )
        self.assertEqual(
            cache.ttl(real_key), -1,
            "the hash that actually holds the token must be shown to have NO expiry",
        )
        self.assertTrue(
            cache.expire_key(raw_name, 180),
            "`expire_key` namespaces and therefore hits the real key",
        )

    def test_an_old_shape_token_degrades_instead_of_raising(self):
        """The DEPLOY window, which is not the race window.

        The old shape stored this payload as a Redis HASH under the exact same
        key bytes the code now GETs, so a token minted seconds before the
        workers restart is a hash. `GET` against a hash raises
        `WRONGTYPE Operation against a key holding the wrong kind of value`,
        and unhandled that was an opaque 500 on the routine settle path for
        every cashier holding a dialog open across the restart.

        It must degrade to "no usable permit" — which the caller already turns
        into "reopen the dialog" — never propagate. Fails closed either way.
        """
        cache = frappe.cache()
        token = "DEPLOY-WINDOW-PROBE"
        real_key = couriers_api._settle_preview_key(token)
        self.addCleanup(lambda: cache.delete(real_key))

        # Exactly what the previous release left behind in Redis.
        cache.hset(f"jarz_pos:settle_preview:{token}", "data", {"invoice": "X"})

        payload, raw = couriers_api._peek_settle_preview_token(token)

        self.assertIsNone(payload, "an old-shape token must yield no payload")
        self.assertIsNone(raw, "an old-shape token must yield no raw value to spend")

    def test_the_token_carries_a_real_expiry(self):
        """The defect, stated as the property that was missing.

        Verified against real Redis on 2026-09-07: before the fix, `expire()`
        returned False and `TTL` on the real key was -1 — a permit to post money
        that never expired and could be replayed long after the state it
        described had changed.
        """
        token = self._mint()

        ttl = frappe.cache().ttl(couriers_api._settle_preview_key(token))

        self.assertGreater(ttl, 0, "the token must expire")
        self.assertLessEqual(ttl, couriers_api.SETTLE_PREVIEW_TTL)

    def test_the_payload_round_trips(self):
        """JSON replaced pickled hash fields; the values confirm reads must survive."""
        token = self._mint(partner_fee=12.25, is_partner_order=True, delivery_partner="DP-1")

        payload, raw = couriers_api._peek_settle_preview_token(token)

        self.assertEqual(payload.get("invoice"), "ACC-SINV-REDIS-TOKEN-TEST")
        self.assertEqual(payload.get("order_amount"), 100.5)
        self.assertEqual(payload.get("partner_fee"), 12.25)
        self.assertIs(payload.get("is_partner_order"), True)
        self.assertIs(payload.get("is_unpaid_effective"), True)
        self.assertIsNone(payload.get("last_payment_seconds"))
        self.assertEqual(json.loads(raw).get("invoice"), "ACC-SINV-REDIS-TOKEN-TEST")

    def test_peeking_does_not_spend(self):
        """The guards and the invoice check run between peek and spend; a caller
        refused there must still hold a usable token."""
        token = self._mint()

        self.assertIsNotNone(couriers_api._peek_settle_preview_token(token)[0])
        self.assertIsNotNone(couriers_api._peek_settle_preview_token(token)[0])

    def test_an_unknown_token_is_refused_and_disturbs_nothing(self):
        token = self._mint()

        payload, raw = couriers_api._peek_settle_preview_token("nope-not-a-token")
        self.assertIsNone(payload)
        self.assertIsNone(raw)

        self.assertIsNotNone(
            couriers_api._peek_settle_preview_token(token)[0],
            "a bad guess must not invalidate a live token",
        )

    def test_a_token_cannot_be_spent_twice(self):
        token = self._mint()
        payload, raw = couriers_api._peek_settle_preview_token(token)
        self.assertIsNotNone(payload)

        self.assertTrue(couriers_api._spend_settle_preview_token(token, raw))
        self.assertFalse(couriers_api._spend_settle_preview_token(token, raw))
        self.assertIsNone(
            couriers_api._peek_settle_preview_token(token)[0],
            "a spent token must not read back as live",
        )

    def test_concurrent_spenders_of_one_token_produce_exactly_one_winner(self):
        """The property the old check-then-act consume could not give.

        ``confirm_settlement`` used to ``hget`` the payload, post the money,
        commit, and only then ``delete_value`` the key. Every thread here peeks
        the same live token — the interleaving in which two clients both passed
        that read — and then races to spend it. Exactly one may win; the rest
        are refused before anything is posted.
        """
        token = self._mint()
        _payload, raw = couriers_api._peek_settle_preview_token(token)

        self.site = frappe.local.site
        start = threading.Event()
        wins = []
        lock = threading.Lock()

        def racer():
            frappe.init(self.site)
            frappe.connect()
            try:
                start.wait(30)
                won = couriers_api._spend_settle_preview_token(token, raw)
                with lock:
                    wins.append(bool(won))
            finally:
                frappe.destroy()

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

    def test_the_local_request_cache_cannot_resurrect_a_spent_token(self):
        """``hget`` consults ``frappe.local.cache`` before Redis, so the old shape
        could hand a token back inside the same request after it was consumed.
        The raw GET this fix uses talks to Redis only."""
        token = self._mint()
        _payload, raw = couriers_api._peek_settle_preview_token(token)
        self.assertTrue(couriers_api._spend_settle_preview_token(token, raw))

        self.assertIsNone(couriers_api._peek_settle_preview_token(token)[0])


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


class TestIdempotencyTokenCannotForgeATag(_RealDatabaseTestCase):
    """The sibling parameter that the `reference_no` fix did not cover.

    `change_payment_collection_method` builds its dedup tag as
    ``je_type = f"COLLECTION_CHANGE-{idempotency_token}"``, so unlike
    `reference_no` — which lands in free text AFTER the tag — this value lands
    INSIDE it. A caller who closes the tag and opens another controls what
    `_is_settlement_je_remark` sees, which is the function that decides what
    may be reversed.

    Two concrete abuses, both money-affecting:
      * forging COURIER_BATCH_SETTLEMENT makes a collection-change entry read
        as a reversible settlement; reversing one erases the record that a
        customer paid online and re-creates a receivable against a courier
        holding nothing;
      * forging COURIER_SETTLEMENT_REVERSAL marks a real, unreversed
        settlement as "already reversed", permanently.

    Guarded in `_je_dedup_tag` rather than only at the call site, because that
    is the single function every tag passes through.
    """

    def test_a_token_that_closes_the_tag_is_refused(self):
        forged = "A] [JARZ-JE:COURIER_BATCH_SETTLEMENT:X"

        with self.assertRaises(Exception):
            dh._je_dedup_tag("ACC-SINV-0001", f"COLLECTION_CHANGE-{forged}")

    def test_each_structural_character_is_refused(self):
        for ch in ("[", "]", ":"):
            with self.subTest(char=ch):
                with self.assertRaises(Exception):
                    dh._je_dedup_tag("ACC-SINV-0001", f"COLLECTION_CHANGE-A{ch}B")

    def test_a_forged_tag_never_reads_as_a_settlement(self):
        """The property that actually matters, stated end to end."""
        forged_remark = (
            "Payment collection changed to InstaPay "
            "[JARZ-JE:COLLECTION_CHANGE-A] "
            "[JARZ-JE:COURIER_BATCH_SETTLEMENT:X:ACC-SINV-0001]"
        )
        # If the tag above could ever be built, this is what would admit it.
        # The guard prevents its construction, so no such remark can exist —
        # but assert the shape is recognised as dangerous, so that removing the
        # guard fails loudly here rather than silently in production.
        self.assertTrue(
            dh._is_settlement_je_remark(forged_remark),
            "this remark WOULD read as a settlement — which is exactly why "
            "_je_dedup_tag must refuse to build it",
        )

    def test_ordinary_generated_tokens_still_work(self):
        tag = dh._je_dedup_tag("ACC-SINV-0001", "COLLECTION_CHANGE-a1b2c3d4e5f6")

        self.assertEqual(tag, "[JARZ-JE:COLLECTION_CHANGE-a1b2c3d4e5f6:ACC-SINV-0001]")

    def test_every_real_settlement_tag_type_still_builds(self):
        """The guard must not break any legitimate type."""
        for je_type in dh.SETTLEMENT_JE_TAG_TYPES:
            with self.subTest(je_type=je_type):
                self.assertIn(je_type, dh._je_dedup_tag("ACC-SINV-0001", je_type))


class TestAForeignJournalEntryCannotSatisfyTheTagLookups(_RealDatabaseTestCase):
    """The test the whole series was missing.

    Four consecutive adversarial review rounds each found a different
    caller-supplied string reaching a Journal Entry remark — the reversal
    `reason`, the payment `reference_no`, the `idempotency_token`, and finally
    `Jarz Expense Request.remarks` plus `cash_transfer.submit_transfer(remark=)`.
    Each was sanitised, and each fix bought exactly one round, because the write
    side is unbounded: every future writer is another chance to reintroduce the
    hole.

    None of those rounds asked the question this class asks. The three lookups
    were company-wide `user_remark LIKE '%[JARZ-JE:<type>:<key>]%'` queries, so
    the property that actually matters is not "can this particular parameter
    forge a tag" but "can ANY Journal Entry this app did not post satisfy these
    lookups". That property is testable directly, needs no knowledge of which
    writers exist, and does not go stale when a new one is added.

    So: post a REAL, submitted, foreign Journal Entry whose remark contains a
    perfectly well-formed tag — built by the app's own tag builder, so there is
    nothing malformed to catch — and require all three lookups to ignore it.

    The entry is real on purpose. A mock cannot tell you what
    `user_remark LIKE '%...%'` matches; only the database can, and matching is
    the entire subject.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # Create the provenance field if this site has not migrated yet, exactly
        # as `before_migrate` does. Deliberate, not a convenience: the CI job
        # that runs this module syncs branch code into staging and NEVER
        # migrates, so without this the test would pass vacuously on every run
        # before the first deploy — a green result that proves nothing is the
        # failure mode this whole module exists to prevent. The seeder is
        # create-only and idempotent, and the field is the one the next deploy
        # adds anyway.
        from jarz_pos.utils import cleanup

        cleanup.ensure_journal_entry_tag_field()
        frappe.clear_cache(doctype="Journal Entry")
        if not dh._je_tag_field_exists():
            raise AssertionError(
                "Journal Entry.custom_jarz_je_tag could not be created, so the tag "
                "lookups have nothing unforgeable to match on and this test cannot "
                "assert anything. Fix the field before trusting a green run."
            )

    def setUp(self):
        self.made = []
        self.addCleanup(self._cleanup)

        self.company = frappe.db.get_value("Company", {}, "name")
        self.assertTrue(self.company, "a Company is required to post a Journal Entry")
        self.accounts = frappe.get_all(
            "Account",
            filters={
                "company": self.company,
                "is_group": 0,
                "account_type": ["not in", ["Receivable", "Payable"]],
            },
            pluck="name",
            limit_page_length=2,
        )
        self.assertEqual(len(self.accounts), 2, "two postable accounts are required")

        # The three keys the three lookups are asked about.
        self.victim_je = "ACC-JV-FORGERY-VICTIM"
        self.victim_invoice = "ACC-SINV-FORGERY-VICTIM"
        self.partner = "Forgery Test Partner"
        self.token = "abc123def456"

    def _cleanup(self):
        frappe.db.rollback()
        for name in reversed(self.made):
            try:
                frappe.db.sql(
                    "delete from `tabGL Entry` where voucher_type='Journal Entry' "
                    "and voucher_no=%s",
                    (name,),
                )
                frappe.db.sql("delete from `tabJournal Entry Account` where parent=%s", (name,))
                frappe.db.sql("delete from `tabJournal Entry` where name=%s", (name,))
            except Exception:
                pass
        frappe.db.commit()

    def _post_je(self, *, remark, stamp=None):
        """Post and SUBMIT a Journal Entry. ``stamp`` fills the provenance field."""
        je = frappe.new_doc("Journal Entry")
        je.voucher_type = "Journal Entry"
        je.posting_date = frappe.utils.nowdate()
        je.company = self.company
        je.user_remark = remark
        if stamp is not None:
            je.set(dh._JE_TAG_FIELDNAME, stamp)
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
        self.made.append(je.name)
        # Submitted, because every one of the three lookups filters docstatus=1.
        # An unsubmitted forgery would be excluded for the wrong reason and the
        # test would pass without exercising anything.
        je.submit()
        return je.name

    def _forged_remark(self):
        """A remark carrying all three well-formed tags at once.

        Built with `_je_dedup_tag` itself, so these are byte-identical to what
        the app writes — there is no malformation for a validator to catch, and
        the only thing separating this entry from a real one is that the app did
        not post it. That is exactly the distinction the fix is about.
        """
        return " ".join([
            "Cash transfer to the safe. Ref:",
            dh._unsettle_dedup_tag(self.victim_je),
            dh._je_dedup_tag(self.victim_invoice, "OFD"),
            dh._partner_settlement_dedup_tag(self.partner, self.token),
        ])

    # -- the three lookups ----------------------------------------------------

    def test_find_settlement_reversal_je_ignores_a_foreign_entry(self):
        """A forged reversal tag marks a real settlement "already reversed" —
        permanently, because nothing ever clears that answer."""
        self._post_je(remark=self._forged_remark())

        self.assertIsNone(
            dh.find_settlement_reversal_je(self.company, self.victim_je),
            "a Journal Entry this app did not post must never answer "
            "'this settlement was already reversed'",
        )

    def test_find_existing_je_by_tag_ignores_a_foreign_entry(self):
        """A forged dedup tag makes the app believe an entry it never posted
        exists, so the real one is skipped — the freight for that order never
        reaches the ledger."""
        self._post_je(remark=self._forged_remark())

        self.assertIsNone(
            dh._find_existing_je_by_tag(self.company, self.victim_invoice, "OFD"),
            "a foreign entry must never satisfy an idempotency guard",
        )

    def test_find_partner_settlement_je_ignores_a_foreign_entry(self):
        """A forged partner-settlement tag makes the weekly bank transfer look
        already paid, so the partner is never paid at all."""
        self._post_je(remark=self._forged_remark())

        self.assertIsNone(
            dh._find_partner_settlement_je(self.company, self.partner, self.token),
            "a foreign entry must never answer 'this partner was already paid'",
        )

    # -- and the classifier the reversal path asks ----------------------------

    def test_a_foreign_entry_is_not_classified_as_a_reversible_settlement(self):
        forged = self._post_je(
            remark="Expense: office supplies "
            + dh._je_dedup_tag("EMP-001", dh.BATCH_SETTLEMENT_JE_TAG_TYPE)
        )

        self.assertFalse(
            dh._is_settlement_je(forged),
            "a foreign entry must not be reversible however its remark reads",
        )
        self.assertEqual(dh._courier_transactions_for_settlement_je(forged), [])

    def test_the_bulk_reversal_lookup_ignores_a_foreign_entry(self):
        """The batched sibling has to agree with the single lookup. If it did
        not, the reversal dialog and the reversal itself would disagree about
        whether a settlement had already been reversed."""
        self._post_je(remark=self._forged_remark())

        result = dh._find_settlement_reversal_jes_bulk(
            {self.victim_je: {"name": self.victim_je, "company": self.company}}
        )

        self.assertEqual(result, {})

    # -- the positive controls, so a green run cannot mean "lookups are dead" --

    def test_an_entry_this_app_stamped_IS_found_by_every_lookup(self):
        """Without this, every assertion above would pass on a build where the
        lookups simply return nothing."""
        reversal_tag = dh._unsettle_dedup_tag(self.victim_je)
        ofd_tag = dh._je_dedup_tag(self.victim_invoice, "OFD")
        partner_tag = dh._partner_settlement_dedup_tag(self.partner, self.token)

        rev = self._post_je(remark=f"Reversal {reversal_tag}", stamp=reversal_tag)
        ofd = self._post_je(remark=f"Out For Delivery {ofd_tag}", stamp=ofd_tag)
        partner = self._post_je(remark=f"Partner settlement {partner_tag}", stamp=partner_tag)

        self.assertEqual(dh.find_settlement_reversal_je(self.company, self.victim_je), rev)
        self.assertEqual(
            dh._find_existing_je_by_tag(self.company, self.victim_invoice, "OFD"), ofd
        )
        self.assertEqual(
            dh._find_partner_settlement_je(self.company, self.partner, self.token), partner
        )
        self.assertEqual(
            dh._find_settlement_reversal_jes_bulk(
                {self.victim_je: {"name": self.victim_je, "company": self.company}}
            ),
            {self.victim_je: rev},
        )

    def test_a_stamped_batch_settlement_is_still_reversible(self):
        tag = dh._je_dedup_tag("EMP-001", dh.BATCH_SETTLEMENT_JE_TAG_TYPE)
        je = self._post_je(remark=f"Order: 100.0, Shipping: 10.0, Net to branch: 90.0 {tag}",
                           stamp=tag)

        self.assertTrue(dh._is_settlement_je(je))

    def test_a_legacy_settlement_that_predates_the_field_stays_reversible(self):
        """The 2026-09-02 wrong-branch settlement, which is the case this whole
        feature exists for.

        It was posted long before `custom_jarz_je_tag` existed, so it carries no
        stamp — only the untagged `Order: ..., Shipping: ..., Net to branch: ...`
        remark `_create_settlement_journal_entry` has always written. Hardening
        the read side must not make it, or any of its five production siblings,
        permanently unreversable. `creation` is forced behind the field's own
        creation to model an entry from before the column existed; that is the
        boundary `_je_tag_legacy_cutoff` draws.
        """
        je = self._post_je(remark=SETTLEMENT_REMARK)
        cutoff = dh._je_tag_legacy_cutoff()
        self.assertIsNotNone(cutoff, "the field exists, so there must be a boundary")
        frappe.db.set_value(
            "Journal Entry", je, "creation",
            frappe.utils.add_to_date(cutoff, days=-30),
            update_modified=False,
        )

        self.assertTrue(
            dh._is_settlement_je(je),
            "a settlement posted before the field existed must stay reversible",
        )

    def test_the_same_remark_dated_AFTER_the_field_is_not_a_settlement(self):
        """The other side of that boundary, and why the legacy path is safe.

        Identical remark, identical shape — only the date differs. An entry
        created after the column existed and carrying no stamp is not something
        this app posted, so the legacy allowance must not reach it. Without this
        the fallback would be a permanently open door wearing a date on it.
        """
        je = self._post_je(remark=SETTLEMENT_REMARK)

        self.assertFalse(
            dh._is_settlement_je(je),
            "an unstamped entry created after the field existed is not ours",
        )


if __name__ == "__main__":
    unittest.main()
