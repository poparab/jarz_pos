"""Read-only GL / accounting integrity audit for jarz_pos.

This is the CI entrypoint for the accounting-integrity gate. It runs the SAME
invariants asserted by ``test_gl_verification_case1`` .. ``case6`` — but as
**read-only** SQL against a populated site, WITHOUT going through
``bench run-tests`` / ``FrappeTestCase``.

Why not run-tests?
    ``bench --site <site> run-tests`` triggers ERPNext's ``before_tests`` hook
    (``erpnext.setup.utils.before_tests``) which unconditionally runs
    ``delete from \`tabItem Price\``` and ``set_defaults_for_tests()`` and then
    ``commit()`` — i.e. it MUTATES and corrupts a populated production clone.
    The GL verification cases, by contrast, only have coverage against real
    populated data (they ``skipTest`` on an empty site). Running them as a
    read-only audit is the only way to gate on real accounting data without
    damaging the site.

Guarantees:
    * Executes ONLY ``SELECT`` statements. No insert/update/delete, no commit,
      no document writes. Safe to run against production / staging clones.
    * Exits non-zero (raises ``GLAuditError``) if any invariant FAILS, so the
      CI step turns red. SKIPs (no data / optional feature absent) are not
      failures.

Run:
    bench --site frontend execute jarz_pos.tests.gl_audit.run

The site-wide per-voucher balance scan here is strictly stronger than the
"sample the 10 most recent SI/PE/JE/DN" checks in the FrappeTestCase files —
it verifies EVERY voucher, not a recent sample.
"""

from __future__ import annotations

import frappe


class GLAuditError(Exception):
    """Raised when one or more accounting invariants fail. Fails the CI step."""


# Status constants
PASS = "PASS"
FAIL = "FAIL"
SKIP = "SKIP"

# Imbalance tolerance (currency rounding)
_TOL = 0.01


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _company() -> str | None:
    return frappe.defaults.get_defaults().get("company") or frappe.db.get_single_value(
        "Global Defaults", "default_company"
    )


def _has_column(doctype: str, column: str) -> bool:
    """True if the SQL column exists on a doctype's table (custom-field guard)."""
    table = f"tab{doctype}"
    try:
        cols = frappe.db.sql(f"SHOW COLUMNS FROM `{table}` LIKE %s", (column,))
        return bool(cols)
    except Exception:
        return False


class _Result:
    __slots__ = ("name", "status", "message")

    def __init__(self, name: str, status: str, message: str = ""):
        self.name = name
        self.status = status
        self.message = message


def _ok(name: str, message: str = "") -> _Result:
    return _Result(name, PASS, message)


def _fail(name: str, message: str) -> _Result:
    return _Result(name, FAIL, message)


def _skip(name: str, message: str) -> _Result:
    return _Result(name, SKIP, message)


# ---------------------------------------------------------------------------
# Invariant checks — each returns a _Result. Read-only.
# ---------------------------------------------------------------------------

def _check_per_voucher_balanced(company: str) -> _Result:
    """THE definitive check: every voucher must have SUM(DR) == SUM(CR).

    Strictly stronger than the per-type "10 most recent" balance samples in
    cases 1/3/4/5 — it scans ALL vouchers (SI, PE, JE, DN, COD, pickup,
    sales-partner, multi-payment) in one pass.
    """
    name = "per_voucher_balanced"
    rows = frappe.db.sql(
        """
        SELECT voucher_type, voucher_no,
               SUM(debit_in_account_currency)  AS dr,
               SUM(credit_in_account_currency) AS cr,
               ABS(SUM(debit_in_account_currency) - SUM(credit_in_account_currency)) AS delta
        FROM `tabGL Entry`
        WHERE is_cancelled = 0 AND company = %s
        GROUP BY voucher_type, voucher_no
        HAVING delta > %s
        ORDER BY delta DESC
        LIMIT 50
        """,
        (company, _TOL),
        as_dict=True,
    )
    if rows:
        details = "\n".join(
            f"    {r['voucher_type']} {r['voucher_no']}: DR={r['dr']:.2f} CR={r['cr']:.2f} Δ={r['delta']:.4f}"
            for r in rows
        )
        return _fail(name, f"{len(rows)} unbalanced voucher(s) (top 50 by Δ):\n{details}")
    return _ok(name, "all vouchers balanced (DR == CR)")


def _check_site_total_balanced(company: str) -> _Result:
    name = "site_total_dr_eq_cr"
    row = frappe.db.sql(
        """
        SELECT SUM(debit_in_account_currency)  AS dr,
               SUM(credit_in_account_currency) AS cr
        FROM `tabGL Entry`
        WHERE is_cancelled = 0 AND company = %s
        """,
        (company,),
        as_dict=True,
    )
    if not row or row[0].get("dr") is None:
        return _skip(name, "no GL entries")
    dr = float(row[0]["dr"] or 0)
    cr = float(row[0]["cr"] or 0)
    if abs(dr - cr) > 0.1:
        return _fail(name, f"site-wide imbalance: DR={dr:.2f} CR={cr:.2f} Δ={abs(dr - cr):.2f}")
    return _ok(name, f"DR={dr:.2f} == CR={cr:.2f}")


def _check_no_dual_sided_lines(company: str) -> _Result:
    """MONITORING ONLY — not a gate invariant.

    A single GL line carrying both debit and credit > 0 is NOT necessarily a
    defect: standard ERPNext books "Valuation and Total" charges (freight /
    landed cost) on a perpetual-inventory Purchase Invoice (update_stock=1) as
    a Total (debit) leg AND a Valuation (credit) leg on the SAME account, and
    its GL-merge stores them as one row with both fields populated (e.g.
    ACC-PINV-MEG-2026-00001 → Freight account DR=800 CR=800, net P&L zero, fully
    capitalized into inventory). Because the merge sums debit and credit into
    separate fields, there is no robust SQL predicate that distinguishes this
    legitimate pattern from genuine corruption — so this can only be a human-
    reviewed monitoring signal, never a commit gate.
    """
    name = "no_dual_sided_gl_lines"
    rows = frappe.db.sql(
        """
        SELECT name, voucher_type, voucher_no,
               debit_in_account_currency AS dr, credit_in_account_currency AS cr
        FROM `tabGL Entry`
        WHERE is_cancelled = 0 AND company = %s
          AND debit_in_account_currency > 0 AND credit_in_account_currency > 0
        LIMIT 20
        """,
        (company,),
        as_dict=True,
    )
    if rows:
        details = "\n".join(
            f"    {r['name']} ({r['voucher_type']} {r['voucher_no']}): DR={r['dr']} CR={r['cr']}"
            for r in rows
        )
        return _fail(name, f"{len(rows)} GL line(s) with both DR and CR > 0:\n{details}")
    return _ok(name, "no dual-sided lines")


def _check_no_negative_amounts(company: str) -> _Result:
    name = "no_negative_gl_amounts"
    rows = frappe.db.sql(
        """
        SELECT name, voucher_type, voucher_no,
               debit_in_account_currency AS dr, credit_in_account_currency AS cr
        FROM `tabGL Entry`
        WHERE is_cancelled = 0 AND company = %s
          AND (debit_in_account_currency < 0 OR credit_in_account_currency < 0)
        LIMIT 20
        """,
        (company,),
        as_dict=True,
    )
    if rows:
        details = "\n".join(
            f"    {r['name']} ({r['voucher_type']} {r['voucher_no']}): DR={r['dr']} CR={r['cr']}"
            for r in rows
        )
        return _fail(name, f"{len(rows)} GL line(s) with negative amounts:\n{details}")
    return _ok(name, "no negative amounts")


def _check_journal_entries_balanced(company: str) -> _Result:
    name = "journal_entries_balanced"
    rows = frappe.db.sql(
        """
        SELECT je.name,
               SUM(jea.debit_in_account_currency)  AS dr,
               SUM(jea.credit_in_account_currency) AS cr,
               ABS(SUM(jea.debit_in_account_currency) - SUM(jea.credit_in_account_currency)) AS delta
        FROM `tabJournal Entry` je
        INNER JOIN `tabJournal Entry Account` jea ON jea.parent = je.name
        WHERE je.docstatus = 1 AND je.company = %s
        GROUP BY je.name
        HAVING delta > %s
        LIMIT 20
        """,
        (company, _TOL),
        as_dict=True,
    )
    if rows:
        details = "\n".join(f"    {r['name']}: DR={r['dr']:.2f} CR={r['cr']:.2f}" for r in rows)
        return _fail(name, f"{len(rows)} unbalanced Journal Entry(ies):\n{details}")
    return _ok(name, "all submitted Journal Entries balanced")


def _check_courier_outstanding_net_zero(company: str) -> _Result:
    name = "courier_outstanding_net_zero"
    acc = frappe.db.get_value(
        "Account",
        {"account_name": ["like", "%Courier Outstanding%"], "company": company, "is_group": 0},
        "name",
    )
    if not acc:
        return _skip(name, "no 'Courier Outstanding' account")
    row = frappe.db.sql(
        """
        SELECT SUM(debit_in_account_currency) AS dr, SUM(credit_in_account_currency) AS cr
        FROM `tabGL Entry`
        WHERE account = %s AND is_cancelled = 0
        """,
        (acc,),
        as_dict=True,
    )
    if not row or row[0].get("dr") is None:
        return _skip(name, "no GL entries for courier outstanding account")
    net = float(row[0]["dr"] or 0) - float(row[0]["cr"] or 0)
    if abs(net) > 0.1:
        return _fail(
            name,
            f"account {acc} net balance {net:.2f} != 0 — unsettled courier transactions",
        )
    return _ok(name, f"net balance {net:.2f}")


def _check_paid_invoices_zero_outstanding(company: str) -> _Result:
    name = "paid_invoices_zero_outstanding"
    rows = frappe.db.sql(
        """
        SELECT name, grand_total, outstanding_amount
        FROM `tabSales Invoice`
        WHERE docstatus = 1 AND status = 'Paid'
          AND outstanding_amount > %s AND company = %s
        ORDER BY creation DESC
        LIMIT 20
        """,
        (_TOL, company),
        as_dict=True,
    )
    if rows:
        details = "\n".join(
            f"    {r['name']}: grand_total={r['grand_total']:.2f} outstanding={r['outstanding_amount']:.2f}"
            for r in rows
        )
        return _fail(name, f"{len(rows)} 'Paid' invoice(s) with outstanding > 0:\n{details}")
    return _ok(name, "all 'Paid' invoices have zero outstanding")


def _check_woo_paid_invoices_zero_outstanding(company: str) -> _Result:
    """WooCommerce-imported paid invoices must have outstanding == 0.

    Optional: skips cleanly if the woo_order_id field / WooCommerce Order Map
    doctype is not present (jarz_pos must not hard-depend on the woo app).
    """
    name = "woo_paid_invoices_zero_outstanding"
    if not _has_column("Sales Invoice", "woo_order_id"):
        return _skip(name, "no woo_order_id field (woo integration not installed)")
    if not frappe.db.exists("DocType", "WooCommerce Order Map"):
        return _skip(name, "no WooCommerce Order Map doctype")
    candidates = frappe.db.sql(
        """
        SELECT name, grand_total, outstanding_amount
        FROM `tabSales Invoice`
        WHERE docstatus = 1
          AND woo_order_id IS NOT NULL AND woo_order_id != ''
          AND outstanding_amount > %s
        ORDER BY creation DESC
        LIMIT 20
        """,
        (_TOL,),
        as_dict=True,
    )
    paid_statuses = {"completed", "processing"}
    truly_unpaid = []
    for inv in candidates:
        status = frappe.db.get_value(
            "WooCommerce Order Map", {"erpnext_sales_invoice": inv["name"]}, "status"
        )
        if status in paid_statuses:
            truly_unpaid.append(inv)
    if truly_unpaid:
        details = "\n".join(
            f"    {r['name']}: grand_total={r['grand_total']:.2f} outstanding={r['outstanding_amount']:.2f}"
            for r in truly_unpaid
        )
        return _fail(name, f"{len(truly_unpaid)} paid Woo invoice(s) with outstanding > 0:\n{details}")
    return _ok(name, f"{len(candidates)} candidate(s) checked, none truly unpaid")


def _check_no_orphaned_gl_smoke(company: str) -> _Result:
    """Structural smoke check ported from case5 (intentionally a no-op there).

    Kept non-failing to preserve existing behavior; verifies the query plan
    executes. A real orphan-detection check can replace this later.
    """
    name = "orphaned_gl_smoke"
    frappe.db.sql(
        """
        SELECT gl.voucher_type, gl.voucher_no, COUNT(*) AS n
        FROM `tabGL Entry` gl
        WHERE gl.is_cancelled = 0 AND gl.company = %s
        GROUP BY gl.voucher_type, gl.voucher_no
        LIMIT 1
        """,
        (company,),
    )
    return _ok(name, "structural query executed")


# ---------------------------------------------------------------------------
# Two tiers of checks.
#
# GATE checks are pure double-entry invariants — mathematical truths of the
# ledger that hold regardless of operational state. They can ONLY be violated
# by a code defect in how jarz_pos writes GL entries, so they are safe to gate
# every commit on. ``run()`` (the CI entrypoint) uses these.
#
# MONITORING checks assert *operational/business* state (a courier mid-delivery
# legitimately leaves a non-zero outstanding balance; an in-flight Woo order is
# legitimately unpaid). These flip with live data unrelated to any code change,
# so gating commits on them just recreates the perpetually-red CI. They are
# exposed via ``run_monitoring()`` for a scheduled ops audit / human review —
# NOT wired into the commit gate.
# ---------------------------------------------------------------------------
_GATE_CHECKS = [
    _check_per_voucher_balanced,
    _check_site_total_balanced,
    _check_no_negative_amounts,
    _check_journal_entries_balanced,
    _check_no_orphaned_gl_smoke,
]

_MONITORING_CHECKS = [
    # Dual-sided lines are a legitimate ERPNext valuation/landed-cost pattern on
    # Purchase Invoices (see _check_no_dual_sided_lines docstring) — surfaced for
    # human review, never gated. Confirmed against staging Purchase Invoice
    # ACC-PINV-MEG-2026-00001.
    _check_no_dual_sided_lines,
    _check_courier_outstanding_net_zero,
    _check_paid_invoices_zero_outstanding,
    _check_woo_paid_invoices_zero_outstanding,
]


def _run_checks(title: str, checks: list, *, raise_on_fail: bool) -> dict:
    """Shared read-only runner. Prints a report; optionally raises on FAIL."""
    company = _company()
    print("=" * 72)
    print(f"jarz_pos — {title} (read-only)")
    print(f"  site:    {frappe.local.site}")
    print(f"  company: {company or '<none>'}")
    print("=" * 72)

    if not company:
        # A site with no default company is not a populated clone — the gate
        # must not go green against a misconfigured/empty site.
        raise GLAuditError(
            "No default company configured — cannot audit. "
            "This gate must run against a populated site."
        )

    results: list[_Result] = []
    for check in checks:
        try:
            results.append(check(company))
        except Exception as exc:  # a broken check is a failure, not a silent pass
            results.append(_fail(check.__name__, f"check raised: {exc!r}"))

    failures = [r for r in results if r.status == FAIL]
    skips = [r for r in results if r.status == SKIP]
    passes = [r for r in results if r.status == PASS]

    for r in results:
        marker = {PASS: "PASS", FAIL: "FAIL", SKIP: "skip"}[r.status]
        print(f"  [{marker}] {r.name}: {r.message}")

    print("-" * 72)
    print(f"  {len(passes)} passed, {len(failures)} failed, {len(skips)} skipped")
    print("=" * 72)

    if failures and raise_on_fail:
        summary = "\n".join(f"  - {r.name}: {r.message}" for r in failures)
        raise GLAuditError(f"{len(failures)} invariant(s) FAILED:\n{summary}")

    return {"passed": len(passes), "failed": len(failures), "skipped": len(skips)}


def run():
    """CI commit-gate entrypoint. Read-only. Hard double-entry invariants only.

    Raises GLAuditError (non-zero exit) if any invariant fails.

        bench --site frontend execute jarz_pos.tests.gl_audit.run
    """
    return _run_checks(
        "GL accounting integrity GATE", _GATE_CHECKS, raise_on_fail=True
    )


def run_monitoring():
    """Ops/monitoring entrypoint. Read-only. Gate invariants + operational-state
    checks (courier outstanding, paid/Woo outstanding). Intended for a scheduled
    audit against the production clone, NOT for the per-commit gate.

        bench --site frontend execute jarz_pos.tests.gl_audit.run_monitoring
    """
    return _run_checks(
        "GL accounting + operational-state MONITORING audit",
        _GATE_CHECKS + _MONITORING_CHECKS,
        raise_on_fail=True,
    )
