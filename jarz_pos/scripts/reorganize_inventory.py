"""Reshelve the item catalogue: one Item Group tree, and warehouses that match it.

Background
----------
``Raw Material`` on production held 65 items, of which only 37 were raw
material. The other 28 were printed jar labels (24) and glassware (4), filed
there because ``Raw Material`` was one of only two groups with a purchase
warehouse route — so anything that had to land in the raw store had to wear
that group. Meanwhile ``kamina vanilla``, a component of 34 BOMs, sat under
``Consumable``.

Item group is not cosmetic in this app. ``resolve_purchase_warehouse`` reads it
on every purchase line, B2B category pricing reads it, and the POS profile
catalogue reads it. So this script builds a real tree — ``Materials`` carrying
the warehouse route, with ``Raw Material`` / ``Packaging`` / ``Labels`` /
``Sub Assemblies`` beneath it — rather than adding more sibling leaves and more
hand-maintained route rows.

What it deliberately does NOT do
--------------------------------
* No write-off. Dead stock and the Water placeholder are P&L decisions.
* No item merges. ``rename_doc(merge=True)`` is irreversible and these
  duplicates carry transaction history; the dead side gets disabled instead,
  which is reversible.
* No ``stock_uom`` changes — ERPNext blocks those once stock ledger entries
  exist, and every candidate here has them.
* ``assets`` is not renamed to ``Assets``. A case-only rename on MariaDB is
  flaky for no functional gain.

Safe to re-run. Every step checks the current state first and reports
``already`` rather than writing again.

Usage (inside the backend container, from the bench root)::

    python reorganize_inventory.py            # dry run, writes nothing
    python reorganize_inventory.py --apply
"""

import sys

import frappe

COMPANY_ABBR = "J"
RAW_STORE = f"Raw Material - {COMPANY_ABBR}"
WIP = f"Work In Progress - {COMPANY_ABBR}"
WAREHOUSE_ROOT = f"All Warehouses - {COMPANY_ABBR}"

#: Groups whose items are internal — never sellable. Historic invoices keep
#: their lines; this only stops the rows appearing in future sales pickers.
INTERNAL_GROUPS = ("Raw Material", "Consumable", "Sub Assemblies", "assets", "Misc")

#: New tree. (name, parent, is_group)
NEW_GROUPS = (
    ("Finished Goods", "All Item Groups", 1),
    ("Materials", "All Item Groups", 1),
    ("Packaging", "Materials", 0),
    ("Labels", "Materials", 0),
)

#: Existing groups that move under a new parent.
REPARENT_GROUPS = (
    ("Medium", "Finished Goods"),
    ("Large", "Finished Goods"),
    ("Raw Material", "Materials"),
    ("Sub Assemblies", "Materials"),
)

#: Jar glass and lids — packaging, not raw material.
CONTAINERS = ("Glass Jar", "Glass Jar 330", "Jar Lid", "Jar Lid 330")

#: A real ingredient that was filed as a consumable. 34 BOMs consume it.
INGREDIENTS_FROM_CONSUMABLE = ("kamina vanilla",)

#: Recurring costs typed as Misc. They are services, and they are already
#: non-stock, so this is a filing change with no stock consequence.
EXPENSES_FROM_MISC = (
    "Rent",
    "Talabat Fees",
    "gaurd bribe",
    "company registration",
    "Monthly Sim Subscription",
    "cartoon design",
)

#: Empty groups with no items and no references.
DELETE_GROUPS = ("Products", "Selling Products")

#: The placeholder: 1,999,957.75 units at a valuation rate of zero, seeded by
#: MAT-STE-2024-00043 in Aug 2024 so BOMs consuming water never block on stock.
#: It cannot become a non-stock item (ERPNext refuses once stock ledger entries
#: exist), so the quantity is reconciled away instead. Rate is already zero, so
#: this posts nothing to the P&L.
WATER_ITEM = "Water"


class Runner:
    def __init__(self, apply: bool):
        self.apply = apply
        self.changes = 0
        self.skips = 0

    def mark(self, key, msg):
        print(f"MARKe {key} | {msg}")

    def did(self, msg):
        self.changes += 1
        self.mark("APPLY" if self.apply else "WOULD", msg)

    def already(self, msg):
        self.skips += 1
        self.mark("ALREADY", msg)

    def warn(self, msg):
        self.mark("WARN", msg)


def _set_item_field(r: Runner, item_code: str, field: str, value):
    current = frappe.db.get_value("Item", item_code, field)
    if current == value:
        return False
    r.did(f"{item_code}: {field} {current!r} -> {value!r}")
    if r.apply:
        frappe.db.set_value("Item", item_code, field, value, update_modified=False)
    return True


# ── Phase 1 ──────────────────────────────────────────────────────────────────
def phase1_flags(r: Runner):
    """Clear is_sales_item on internal stock, and file misplaced fixed assets."""
    r.mark("PHASE", "1 - flags and misfiled fixed assets")

    rows = frappe.get_all(
        "Item",
        filters={"item_group": ["in", INTERNAL_GROUPS], "is_sales_item": 1},
        fields=["name", "item_group"],
    )
    for row in rows:
        _set_item_field(r, row["name"], "is_sales_item", 0)
    if not rows:
        r.already("no internal item is flagged sellable")

    # Fixed assets filed as consumables or miscellany. The `assets` group is
    # exactly the is_fixed_asset population, so this is a definitional move.
    misfiled = frappe.get_all(
        "Item",
        filters={"is_fixed_asset": 1, "item_group": ["in", ("Consumable", "Misc")]},
        fields=["name", "item_group"],
    )
    for row in misfiled:
        _set_item_field(r, row["name"], "item_group", "assets")
    if not misfiled:
        r.already("no fixed asset sits outside the assets group")


# ── Phase 2 ──────────────────────────────────────────────────────────────────
def phase2_tree(r: Runner):
    """Build the group tree, reshelve the items, retire the empty groups."""
    r.mark("PHASE", "2 - item group tree")

    for name, parent, is_group in NEW_GROUPS:
        if frappe.db.exists("Item Group", name):
            r.already(f"item group {name} exists")
            continue
        r.did(f"create item group {name} (parent={parent}, is_group={is_group})")
        if r.apply:
            frappe.get_doc(
                {
                    "doctype": "Item Group",
                    "item_group_name": name,
                    "parent_item_group": parent,
                    "is_group": is_group,
                }
            ).insert(ignore_permissions=True)

    for name, parent in REPARENT_GROUPS:
        current = frappe.db.get_value("Item Group", name, "parent_item_group")
        if current == parent:
            r.already(f"{name} already under {parent}")
            continue
        if not frappe.db.exists("Item Group", parent):
            r.warn(f"cannot reparent {name}: {parent} missing (dry run?)")
            continue
        r.did(f"reparent item group {name}: {current} -> {parent}")
        if r.apply:
            doc = frappe.get_doc("Item Group", name)
            doc.parent_item_group = parent
            doc.save(ignore_permissions=True)

    # Printed jar labels. Matched on the name pattern rather than a hard-coded
    # list so a flavour added since the audit is not left behind.
    labels = frappe.get_all(
        "Item",
        filters={"item_group": "Raw Material", "item_name": ["like", "%Jar Label%"]},
        pluck="name",
    ) + frappe.get_all(
        "Item",
        filters={"item_group": "Raw Material", "item_name": ["like", "%Jar label%"]},
        pluck="name",
    )
    for code in sorted(set(labels)):
        _set_item_field(r, code, "item_group", "Labels")

    for code in CONTAINERS:
        if frappe.db.exists("Item", code):
            _set_item_field(r, code, "item_group", "Packaging")
        else:
            r.warn(f"container item missing: {code}")

    for code in INGREDIENTS_FROM_CONSUMABLE:
        if frappe.db.exists("Item", code):
            _set_item_field(r, code, "item_group", "Raw Material")

    for code in EXPENSES_FROM_MISC:
        if frappe.db.exists("Item", code):
            _set_item_field(r, code, "item_group", "Services")

    for name in DELETE_GROUPS:
        if not frappe.db.exists("Item Group", name):
            r.already(f"item group {name} already gone")
            continue
        used = frappe.db.count("Item", {"item_group": name})
        if used:
            r.warn(f"not deleting {name}: {used} items still reference it")
            continue
        # Child tables cannot go through frappe.get_all without a parent.
        linked = frappe.db.sql(
            "SELECT COUNT(*) FROM `tabPOS Item Group` WHERE item_group = %s", name
        )[0][0]
        if linked:
            r.warn(f"not deleting {name}: referenced by {linked} POS profile rows")
            continue
        # "Products" holds no items but all three Talabat pricing rules scope to
        # it. Deleting it would silently narrow their scope, which is a pricing
        # decision, not a cleanup — so report the link and leave the group.
        rules = frappe.db.sql(
            "SELECT DISTINCT parent FROM `tabPricing Rule Item Group` WHERE item_group = %s",
            name,
        )
        if rules:
            r.warn(
                f"not deleting {name}: scoped by pricing rule(s) "
                + ", ".join(row[0] for row in rules)
            )
            continue
        r.did(f"delete empty item group {name}")
        if r.apply:
            # A link from somewhere this script does not know about must not
            # take the rest of the run down with it — and the undo has to be a
            # savepoint, not frappe.db.rollback(). A bare rollback discards the
            # whole uncommitted phase: on the first staging run a Pricing Rule
            # link on "Products" silently reverted the four new groups and every
            # item move made alongside them.
            try:
                frappe.db.savepoint("delete_item_group")
                frappe.delete_doc("Item Group", name, ignore_permissions=True, force=False)
            except Exception as exc:
                frappe.db.rollback(save_point="delete_item_group")
                r.warn(f"could not delete {name}: {type(exc).__name__}: {exc}")


def phase2_route(r: Runner):
    """One route on Materials replaces the per-leaf rows.

    ``_routed_warehouse`` walks the group ancestry deepest-first, so Labels and
    Packaging — which have no route of their own — resolve through Materials.
    """
    r.mark("PHASE", "2b - purchase warehouse route")
    settings = frappe.get_single("Jarz POS Settings")
    existing = {(row.item_group or "").strip() for row in (settings.purchase_warehouse_routes or [])}
    if "Materials" in existing:
        r.already("route Materials -> ... already present")
        return
    if not frappe.db.exists("Item Group", "Materials"):
        r.warn("cannot add route: item group Materials missing (dry run?)")
        return
    r.did(f"add purchase route Materials -> {RAW_STORE}")
    if r.apply:
        settings.append("purchase_warehouse_routes", {"item_group": "Materials", "warehouse": RAW_STORE})
        settings.flags.ignore_permissions = True
        # Saving this Single revalidates every link on it, and production
        # carries a dangling cash_over_short_account. Same guard as
        # purchase_setup.ensure_purchase_setup.
        settings.flags.ignore_links = True
        settings.save()


# ── Phase 3 ──────────────────────────────────────────────────────────────────
def phase3_warehouse_tree(r: Runner):
    """Reattach the orphans. Four of the seven warehouses holding stock — the
    raw store and all three branches — hang outside the root, so any report
    that aggregates by warehouse group silently misses them."""
    r.mark("PHASE", "3a - warehouse tree")
    orphans = [
        row[0]
        for row in frappe.db.sql(
            "SELECT name FROM `tabWarehouse` "
            "WHERE is_group = 0 AND (parent_warehouse IS NULL OR parent_warehouse = '') "
            "AND name != %s",
            WAREHOUSE_ROOT,
        )
    ]
    if not orphans:
        r.already("no orphan warehouses")
        return
    for name in orphans:
        r.did(f"reparent warehouse {name} -> {WAREHOUSE_ROOT}")
        if r.apply:
            doc = frappe.get_doc("Warehouse", name)
            doc.parent_warehouse = WAREHOUSE_ROOT
            doc.save(ignore_permissions=True)


def _repoint_defaults(r: Runner, current_wh: str, target_wh, only_group=None):
    # Item Default is a child table, so it has to be read with SQL rather than
    # frappe.get_all, which refuses a child doctype with no parent context.
    sql = (
        "SELECT d.name, d.parent FROM `tabItem Default` d "
        "JOIN `tabItem` i ON i.name = d.parent WHERE d.default_warehouse = %s"
    )
    args = [current_wh]
    if only_group:
        sql += " AND i.item_group = %s"
        args.append(only_group)
    rows = frappe.db.sql(sql, args, as_dict=True)
    if not rows:
        r.already(f"no Item Default points at {current_wh}" + (f" in {only_group}" if only_group else ""))
        return
    for row in rows:
        is_stock = frappe.db.get_value("Item", row["parent"], "is_stock_item")
        # A default warehouse on a non-stock item is inert; blank it rather
        # than pointing it somewhere equally meaningless.
        new = target_wh if is_stock else None
        r.did(f"{row['parent']}: default_warehouse {current_wh} -> {new!r}")
        if r.apply:
            frappe.db.set_value("Item Default", row["name"], "default_warehouse", new, update_modified=False)


def phase3_defaults(r: Runner):
    r.mark("PHASE", "3b - item default warehouses")

    # Stores - J is disabled; anything defaulting to it cannot transact.
    disabled_store = f"Stores - {COMPANY_ABBR}"
    if frappe.db.get_value("Warehouse", disabled_store, "disabled"):
        _repoint_defaults(r, disabled_store, RAW_STORE)
    else:
        r.already(f"{disabled_store} is not disabled; leaving its defaults alone")

    # Sub-assemblies default to Goods In Transit, which is why Cheesecake Mix
    # went negative there. In-transit is not a production warehouse.
    _repoint_defaults(
        r,
        f"Goods In Transit - {COMPANY_ABBR}",
        WIP,
        only_group="Sub Assemblies",
    )


def phase3_water(r: Runner):
    """Reconcile the 2,000,000-unit placeholder away. Valuation rate is zero,
    so this moves no money — it only stops every raw-material total being
    meaningless."""
    r.mark("PHASE", "3c - water placeholder")
    bins = frappe.get_all(
        "Bin",
        filters={"item_code": WATER_ITEM, "actual_qty": [">", 0]},
        fields=["warehouse", "actual_qty", "valuation_rate", "stock_value"],
    )
    if not bins:
        r.already(f"{WATER_ITEM} holds no positive stock")
        return
    for b in bins:
        if float(b["stock_value"] or 0) != 0:
            r.warn(
                f"{WATER_ITEM} in {b['warehouse']} carries value "
                f"{b['stock_value']} - NOT reconciling, this would hit the P&L"
            )
            continue
        r.did(f"reconcile {WATER_ITEM} in {b['warehouse']}: {b['actual_qty']} -> 0 (rate 0, no GL impact)")
        if r.apply:
            try:
                frappe.db.savepoint("water_reco")
                doc = frappe.get_doc(
                    {
                        "doctype": "Stock Reconciliation",
                        "purpose": "Stock Reconciliation",
                        "company": frappe.db.get_value("Warehouse", b["warehouse"], "company"),
                        "items": [
                            {
                                "item_code": WATER_ITEM,
                                "warehouse": b["warehouse"],
                                "qty": 0,
                                "valuation_rate": 0,
                            }
                        ],
                    }
                )
                doc.flags.ignore_permissions = True
                doc.insert()
                doc.submit()
                r.mark("DOC", f"Stock Reconciliation {doc.name} submitted")
            except Exception as exc:
                frappe.db.rollback(save_point="water_reco")
                r.warn(f"water reconciliation failed: {type(exc).__name__}: {exc}")


def main(apply: bool = False):
    r = Runner(apply)
    r.mark("MODE", "APPLY - writing" if apply else "DRY RUN - no writes")
    steps = (
        phase1_flags,
        phase2_tree,
        phase2_route,
        phase3_warehouse_tree,
        phase3_defaults,
        phase3_water,
    )
    # Committed step by step. The phases are independent, so a failure in a
    # later one must not roll back the earlier work and leave the catalogue
    # half-reshelved with no record of which half.
    for step in steps:
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
    r.mark("DONE", f"changes={r.changes} already_correct={r.skips}")
    return r


if __name__ == "__main__":
    main(apply="--apply" in sys.argv)
