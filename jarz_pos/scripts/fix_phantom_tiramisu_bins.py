"""Repair the two Bin rows orphaned by cancelled MAT-RECO-2026-00050.

The reconciliation was submitted 2026-05-22 00:39:11 and cancelled 00:40:22.
The cancel reversed both the stock ledger (balance back to 0) and the GL
(all four entries is_cancelled=1), but the Bin was never rewritten, so it
still claims 61 Tiramisu Medium + 44 Tiramisu Large in Raw Material - J.

GL-neutral: the books already say zero. This only makes the Bin agree.
Refuses any bin the ledger actually backs -- if the ledger does back it, a
Stock Reconciliation is the right tool, because that is a real stock
difference and needs real ledger and GL entries.

Run through ``scripts/remote_exec.ps1 -AllowWrite``. Idempotent: a second run
finds actual_qty already 0 and reports 0 repaired.

APPLIED 2026-09-18 to staging and production. Both sites then swept clean:
350 bins on production, 0 mismatched. Kept in the repo as the record of what
was run, and as the recipe for the next orphaned bin -- the sweep at the
bottom is the check worth repeating.

Found because the Final Products report grew a "Raw Material" column: the
report reads Bin, and Bin was the only one of the three records that the
cancel did not roll back. See the sibling fix in ``api/reports.py``.
"""
import frappe
from frappe.utils import flt
from erpnext.stock.stock_balance import repost_stock, get_balance_qty_from_sle

TARGETS = [
    ("Tiramisu Medium", "Raw Material - J"),
    ("Tiramisu Large", "Raw Material - J"),
]

out = []
def p(*a):
    out.append(" ".join(str(x) for x in a))

repaired = 0
for item, wh in TARGETS:
    before = frappe.db.get_value(
        "Bin", {"item_code": item, "warehouse": wh},
        ["name", "actual_qty", "projected_qty", "stock_value", "valuation_rate"],
        as_dict=True,
    )
    if not before:
        p(f"SKIP {item} @ {wh}: no Bin row")
        continue

    live = frappe.db.sql(
        """SELECT COUNT(*) FROM `tabStock Ledger Entry`
           WHERE item_code=%s AND warehouse=%s AND COALESCE(is_cancelled,0)=0""",
        (item, wh),
    )[0][0]
    bal = get_balance_qty_from_sle(item, wh)

    p(f"{item} @ {wh}")
    p(f"  before: actual={before.actual_qty} projected={before.projected_qty} "
      f"value={before.stock_value} rate={before.valuation_rate}")
    p(f"  ledger: live_sles={live} balance={bal}")

    if live != 0 or flt(bal) != 0:
        p("  REFUSED: the ledger backs this bin. A Stock Reconciliation is the")
        p("           right tool here, not a bin repair.")
        continue

    repost_stock(item, wh, only_bin=True)
    frappe.db.set_value("Bin", before.name, "stock_value", 0.0, update_modified=False)

    after = frappe.db.get_value(
        "Bin", before.name,
        ["actual_qty", "projected_qty", "stock_value", "valuation_rate"],
        as_dict=True,
    )
    p(f"  after : actual={after.actual_qty} projected={after.projected_qty} "
      f"value={after.stock_value} rate={after.valuation_rate}")
    repaired += 1

frappe.db.commit()
p("")
p(f"BINS REPAIRED: {repaired}")

p("")
p("=== re-sweep: every stock Bin vs its ledger balance ===")
rows = frappe.db.sql("""
    SELECT b.item_code, b.warehouse, i.item_group, b.actual_qty, b.stock_value,
           (SELECT sle.qty_after_transaction FROM `tabStock Ledger Entry` sle
             WHERE sle.item_code=b.item_code AND sle.warehouse=b.warehouse
               AND COALESCE(sle.is_cancelled,0)=0
             ORDER BY sle.posting_datetime DESC, sle.creation DESC LIMIT 1) AS ledger_qty
    FROM `tabBin` b JOIN `tabItem` i ON i.name=b.item_code
    WHERE i.is_stock_item = 1
""", as_dict=True)
bad = [r for r in rows if abs(flt(r["actual_qty"]) - flt(r["ledger_qty"] or 0)) > 0.001]
p(f"  bins={len(rows)} still mismatched={len(bad)}")
for r in bad:
    p(f"  {r['warehouse']:<22} {r['item_group']:<14} {r['item_code']:<30} "
      f"bin={flt(r['actual_qty'])} ledger={flt(r['ledger_qty'] or 0)} value={r['stock_value']}")

p("")
p("=== Medium/Large stock in Raw Material - J (should be empty) ===")
left = frappe.db.sql("""
    SELECT b.item_code, b.warehouse, b.actual_qty FROM `tabBin` b
    JOIN `tabItem` i ON i.name=b.item_code
    WHERE b.warehouse='Raw Material - J' AND b.actual_qty != 0
      AND i.item_group IN ('Medium','Meduim','Large')
""", as_dict=True)
p(f"  rows: {len(left)} {left}")

for _line in out:
    print("MARK " + _line)
