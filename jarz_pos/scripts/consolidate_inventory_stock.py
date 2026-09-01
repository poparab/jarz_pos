"""Second half of the 2026-09 reshelving: move the stock the tree could not.

`reorganize_inventory.py` changed how items are filed. This moves the stock
itself, on decisions the owner made item by item after seeing the numbers:

* Ten raw-material rows nothing consumes are gone from the shelf -> reconcile
  to zero. EGP 8,841.85 to Stock Adjustment.
* Sub-assembly balances stranded in Goods In Transit -> zero. Perishable mixes
  whose last movement was May; the negative Cheesecake Mix goes with them.
  EGP 3,304.49 net to Stock Adjustment.
* The two Puratos Blueberry rows are one product -> combine, no write-off.
* The consumables sitting in the raw store -> move to the consumables store,
  and repoint their defaults so the next purchase follows.
* `Carrot cake Jar Label` 212/330 -> disabled, stock kept. Might come back.
* `assets` -> `Assets`.

Safe to re-run: every step reads the current state first.

    python consolidate_inventory_stock.py            # dry run
    python consolidate_inventory_stock.py --apply
"""

import sys

import frappe

COMPANY_ABBR = "J"
RAW_STORE = f"Raw Material - {COMPANY_ABBR}"
CONSUMABLE_STORE = f"Consumables - {COMPANY_ABBR}"
TRANSIT = f"Goods In Transit - {COMPANY_ABBR}"

#: Gone from the shelf. Owner-confirmed 2026-09-01 ("doesn't exist right now").
#: Their disabled flag is left exactly as found -- five of these were already
#: disabled and stay so; the rest stay active because "no stock" is not the
#: same statement as "never buy this again".
WRITE_OFF = (
    "Lotus Biscuit",
    "Roasted Kunafa",
    "Dr baker blueberry",
    "Kunafa Lotus Jar Label 212",
    "Kunafa Lotus Jar Label 330",
    "Kunafa Nutella Jar Label 212",
    "Kunafa Nutella Jar Label 330",
    "Dr baker Strawberry",
    "pistachio chusco",
    "Vanilla sponge mix",
)

#: Discontinued for now, stock deliberately retained.
DISABLE_KEEP_STOCK = ("Carrot cake Jar Label 212", "Carrot cake Jar Label 330")

#: Two item codes, one product. Same item_name, same supplier, same purchase
#: UOM (Box) and conversion factor (5 Kg), same price history. The only
#: difference is stock_uom -- Nos on the dead one, Kg on the live one -- and
#: the dead one carries a `Kg = 1.0` conversion, so its "Nos" IS a Kg.
#:
#: rename_doc(merge=True) is refused: ERPNext blocks a merge across differing
#: stock_uom, and stock_uom itself cannot be edited once stock ledger entries
#: exist. So the value is carried across with a Repack -- total in equals total
#: out, nothing is written off -- and the dead code is then disabled.
MERGE_FROM = "Puratos Blueberry"
MERGE_INTO = "Puratos Blueberry KG"


class Runner:
    def __init__(self, apply):
        self.apply = apply
        self.changes = 0

    def mark(self, key, msg):
        print(f"MARKe {key} | {msg}")

    def did(self, msg):
        self.changes += 1
        self.mark("APPLY" if self.apply else "WOULD", msg)

    def already(self, msg):
        self.mark("ALREADY", msg)

    def warn(self, msg):
        self.mark("WARN", msg)


def _company():
    return frappe.db.get_value("Company", {}, "name")


def _bins(item_code, warehouse=None, positive_only=False):
    sql = "SELECT warehouse, actual_qty, stock_value FROM `tabBin` WHERE item_code = %s"
    args = [item_code]
    if warehouse:
        sql += " AND warehouse = %s"
        args.append(warehouse)
    sql += " AND actual_qty > 0" if positive_only else " AND (actual_qty != 0 OR stock_value != 0)"
    return frappe.db.sql(sql, args, as_dict=True)


def _submit(r, doc, label):
    """Insert+submit inside a savepoint so one refusal cannot unwind the rest."""
    try:
        frappe.db.savepoint("stock_doc")
        doc.flags.ignore_permissions = True
        doc.insert()
        doc.submit()
        r.mark("DOC", f"{label}: {doc.doctype} {doc.name} submitted")
        return doc
    except Exception as exc:
        frappe.db.rollback(save_point="stock_doc")
        r.warn(f"{label} failed: {type(exc).__name__}: {exc}")
        return None


def _reconcile_to_zero(r, rows, label):
    """One Stock Reconciliation zeroing every (item, warehouse) in rows.

    ERPNext refuses to transact a disabled item, and five of the write-off rows
    are already disabled — so they are re-enabled for the duration and put back
    exactly as they were afterwards.
    """
    if not rows:
        r.already(f"{label}: nothing to zero")
        return
    total = sum(float(x["stock_value"] or 0) for x in rows)
    for x in rows:
        r.did(f"{label}: {x['item_code']} @ {x['warehouse']} {x['actual_qty']} -> 0 ({x['stock_value']})")
    r.mark("SUBTOTAL", f"{label}: {len(rows)} rows, {round(total, 2)} to Stock Adjustment")
    if not r.apply:
        return

    codes = sorted({x["item_code"] for x in rows})
    was_disabled = [c for c in codes if frappe.db.get_value("Item", c, "disabled")]
    for c in was_disabled:
        frappe.db.set_value("Item", c, "disabled", 0, update_modified=False)
    try:
        doc = frappe.get_doc(
            {
                "doctype": "Stock Reconciliation",
                "purpose": "Stock Reconciliation",
                "company": _company(),
                "items": [
                    {
                        "item_code": x["item_code"],
                        "warehouse": x["warehouse"],
                        "qty": 0,
                        "valuation_rate": 0,
                    }
                    for x in rows
                ],
            }
        )
        _submit(r, doc, label)
    finally:
        for c in was_disabled:
            frappe.db.set_value("Item", c, "disabled", 1, update_modified=False)
            r.mark("RESTORED", f"{c} left disabled, as found")


def step_write_off(r):
    r.mark("PHASE", "4a - write off stock that is not on the shelf")
    rows = []
    for code in WRITE_OFF:
        if not frappe.db.exists("Item", code):
            r.warn(f"missing item {code}")
            continue
        for b in _bins(code):
            rows.append(dict(b, item_code=code))
    _reconcile_to_zero(r, rows, "write-off")


def step_disable_keep_stock(r):
    r.mark("PHASE", "4b - discontinue, stock retained")
    for code in DISABLE_KEEP_STOCK:
        if not frappe.db.exists("Item", code):
            r.warn(f"missing item {code}")
            continue
        if frappe.db.get_value("Item", code, "disabled"):
            r.already(f"{code} already disabled")
            continue
        held = sum(float(b["actual_qty"] or 0) for b in _bins(code))
        r.did(f"disable {code} (keeping {held} in stock)")
        if r.apply:
            frappe.db.set_value("Item", code, "disabled", 1, update_modified=False)


def step_merge_puratos(r):
    r.mark("PHASE", "4c - combine the two Puratos Blueberry rows")
    for code in (MERGE_FROM, MERGE_INTO):
        if not frappe.db.exists("Item", code):
            r.warn(f"missing item {code}")
            return
    src = _bins(MERGE_FROM, positive_only=True)
    if not src:
        r.already(f"{MERGE_FROM} holds no stock to move")
    for b in src:
        r.did(
            f"repack {b['actual_qty']} {MERGE_FROM} @ {b['warehouse']} "
            f"-> {MERGE_INTO} (carrying {b['stock_value']}, no write-off)"
        )
        if r.apply:
            doc = frappe.get_doc(
                {
                    "doctype": "Stock Entry",
                    "stock_entry_type": "Repack",
                    "company": _company(),
                    "items": [
                        {
                            "item_code": MERGE_FROM,
                            "qty": b["actual_qty"],
                            "s_warehouse": b["warehouse"],
                        },
                        {
                            "item_code": MERGE_INTO,
                            "qty": b["actual_qty"],
                            "t_warehouse": b["warehouse"],
                        },
                    ],
                }
            )
            _submit(r, doc, "puratos repack")

    if frappe.db.get_value("Item", MERGE_FROM, "disabled"):
        r.already(f"{MERGE_FROM} already disabled")
        return
    remaining = sum(float(b["actual_qty"] or 0) for b in _bins(MERGE_FROM))
    if r.apply and remaining:
        r.warn(f"not disabling {MERGE_FROM}: {remaining} still on hand")
        return
    r.did(f"disable {MERGE_FROM} (superseded by {MERGE_INTO})")
    if r.apply:
        frappe.db.set_value("Item", MERGE_FROM, "disabled", 1, update_modified=False)


def step_move_consumables(r):
    r.mark("PHASE", "5 - consumables to the consumables store")
    rows = frappe.db.sql(
        """SELECT b.item_code, b.actual_qty, b.stock_value
           FROM `tabBin` b JOIN `tabItem` i ON i.name = b.item_code
           WHERE i.item_group = 'Consumable' AND b.warehouse = %s
             AND b.actual_qty > 0 AND i.disabled = 0
           ORDER BY b.stock_value DESC""",
        RAW_STORE,
        as_dict=True,
    )
    if rows:
        total = sum(float(x["stock_value"] or 0) for x in rows)
        for x in rows:
            r.did(f"transfer {x['actual_qty']} {x['item_code']} {RAW_STORE} -> {CONSUMABLE_STORE}")
        r.mark("SUBTOTAL", f"consumables: {len(rows)} items, {round(total, 2)} moved (same account, no P&L)")
        if r.apply:
            doc = frappe.get_doc(
                {
                    "doctype": "Stock Entry",
                    "stock_entry_type": "Material Transfer",
                    "company": _company(),
                    "items": [
                        {
                            "item_code": x["item_code"],
                            "qty": x["actual_qty"],
                            "s_warehouse": RAW_STORE,
                            "t_warehouse": CONSUMABLE_STORE,
                        }
                        for x in rows
                    ],
                }
            )
            _submit(r, doc, "consumable transfer")
    else:
        r.already("no consumable stock left in the raw store")

    # Without this the next purchase lands back in the raw store: the Item
    # Default beats the item group route in resolve_purchase_warehouse.
    defaults = frappe.db.sql(
        """SELECT d.name, d.parent FROM `tabItem Default` d
           JOIN `tabItem` i ON i.name = d.parent
           WHERE i.item_group = 'Consumable' AND d.default_warehouse = %s""",
        RAW_STORE,
        as_dict=True,
    )
    if not defaults:
        r.already("no consumable still defaults to the raw store")
    for d in defaults:
        r.did(f"{d['parent']}: default_warehouse {RAW_STORE} -> {CONSUMABLE_STORE}")
        if r.apply:
            frappe.db.set_value(
                "Item Default", d["name"], "default_warehouse", CONSUMABLE_STORE, update_modified=False
            )


def step_zero_transit_subassemblies(r):
    """Two vehicles, because a Stock Reconciliation cannot lift a negative bin.

    Reconciling Cheesecake Mix from -14.64 to 0 is an *increase*, but ERPNext
    still refuses it with NegativeStockError: the reco reverses the existing
    balance before setting the new one, and that reversal issues stock the
    warehouse does not have. So the negative row is cleared with a Material
    Receipt first (which only ever adds), and the positives are reconciled
    afterwards.
    """
    r.mark("PHASE", "6 - zero the sub-assembly balances stranded in transit")
    rows = frappe.db.sql(
        """SELECT b.item_code, b.warehouse, b.actual_qty, b.stock_value, b.valuation_rate
           FROM `tabBin` b JOIN `tabItem` i ON i.name = b.item_code
           WHERE i.item_group = 'Sub Assemblies' AND b.warehouse = %s
             AND (b.actual_qty != 0 OR b.stock_value != 0)
           ORDER BY b.stock_value DESC""",
        TRANSIT,
        as_dict=True,
    )
    negatives = [x for x in rows if float(x["actual_qty"] or 0) < 0]
    positives = [x for x in rows if float(x["actual_qty"] or 0) > 0]

    for x in negatives:
        qty = abs(float(x["actual_qty"]))
        rate = abs(float(x["stock_value"] or 0)) / qty if qty else 0
        r.did(f"receipt {qty} {x['item_code']} into {TRANSIT} to clear {x['actual_qty']} ({x['stock_value']})")
        if r.apply:
            doc = frappe.get_doc(
                {
                    "doctype": "Stock Entry",
                    "stock_entry_type": "Material Receipt",
                    "company": _company(),
                    "items": [
                        {
                            "item_code": x["item_code"],
                            "qty": qty,
                            "t_warehouse": TRANSIT,
                            "basic_rate": rate,
                            "allow_zero_valuation_rate": 1 if not rate else 0,
                        }
                    ],
                }
            )
            _submit(r, doc, f"clear negative {x['item_code']}")

    _reconcile_to_zero(r, positives, "transit sub-assemblies")


def step_rename_assets(r):
    r.mark("PHASE", "7 - assets -> Assets")
    # frappe.db.exists collates case-insensitively on MariaDB, so it answers
    # True for "assets" even after the row has been renamed to "Assets" --
    # which made a second run try the rename again and fail on "already
    # exists". Compare the stored spelling in Python instead.
    stored = [row[0] for row in frappe.db.sql(
        "SELECT name FROM `tabItem Group` WHERE name IN ('assets', 'Assets')"
    )]
    if "Assets" in stored:
        r.already("item group already named Assets")
        return
    if "assets" not in stored:
        r.already("no lowercase assets group")
        return
    n = frappe.db.count("Item", {"item_group": "assets"})
    r.did(f"rename item group assets -> Assets ({n} items follow the link)")
    if r.apply:
        try:
            frappe.db.savepoint("rename_assets")
            frappe.rename_doc("Item Group", "assets", "Assets", force=True, show_alert=False)
        except Exception as exc:
            frappe.db.rollback(save_point="rename_assets")
            # MariaDB collates case-insensitively, so a case-only rename is the
            # one that can fail here. Cosmetic; not worth failing the run.
            r.warn(f"rename refused: {type(exc).__name__}: {exc}")


def main(apply=False):
    r = Runner(apply)
    r.mark("MODE", "APPLY - writing" if apply else "DRY RUN - no writes")
    for step in (
        step_write_off,
        step_disable_keep_stock,
        step_merge_puratos,
        step_move_consumables,
        step_zero_transit_subassemblies,
        step_rename_assets,
    ):
        try:
            step(r)
            if apply:
                frappe.db.commit()
        except Exception as exc:
            if apply:
                frappe.db.rollback()
            r.warn(f"{step.__name__} aborted: {type(exc).__name__}: {exc}")
    if apply:
        frappe.clear_cache()
    r.mark("DONE", f"changes={r.changes}")
    return r


if __name__ == "__main__":
    main(apply="--apply" in sys.argv)
