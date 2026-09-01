"""Give the factory warehouses the count profiles the branches already have.

`list_items_for_count` restricts a count sheet to a warehouse's Warehouse Count
Profile -- and falls back to *every enabled item on the site* when there is no
profile. The three branches have one (Large + Medium, 20 rows). The four factory
warehouses never got one, so each of them loads 190 rows to show at most 60 with
stock, 83 of which are non-stock items that cannot be counted at all.

This seeds the four missing profiles off the item group tree, so each warehouse
lists what actually belongs in it:

    Raw Material - J      Materials       -> Raw Material, Packaging, Labels,
                                             Sub Assemblies      190 -> 67
    Consumables - J       Consumable                             190 -> 19
    Work In Progress - J  Sub Assemblies                         190 -> 10
    Finished Goods - J    Finished Goods  -> Medium, Large       190 -> 20

A profile is a *whitelist*, so the one real risk in adding one is hiding stock
that is already on hand somewhere it does not nominally belong -- two finished
jars sit in the raw store, for instance. So after building each profile this
checks every item currently holding stock in that warehouse and pins anything
the groups do not cover as an ``Include`` exception. Creating a profile can
therefore never make existing stock uncountable.

Note it cannot rescue stock held by a *disabled* item: both the profile path and
the main query filter ``disabled = 0``. The two Carrot cake labels (560 units)
are in that position by choice.

    python seed_warehouse_count_profiles.py            # dry run
    python seed_warehouse_count_profiles.py --apply
"""

import sys

import frappe

ABBR = "J"

#: warehouse -> the single root group whose subtree belongs in it.
PROFILES = (
    (f"Raw Material - {ABBR}", "Materials"),
    (f"Consumables - {ABBR}", "Consumable"),
    (f"Work In Progress - {ABBR}", "Sub Assemblies"),
    (f"Finished Goods - {ABBR}", "Finished Goods"),
)


def mark(k, v):
    print(f"MARKe {k} | {v}")


def _descendants(group):
    b = frappe.db.get_value("Item Group", group, ["lft", "rgt"], as_dict=True)
    if not b:
        return []
    return frappe.get_all(
        "Item Group",
        filters={"lft": [">=", b["lft"]], "rgt": ["<=", b["rgt"]], "is_group": 0},
        pluck="name",
    )


def _stock_here_outside(warehouse, groups):
    """Items holding stock in this warehouse that the groups would not show."""
    rows = frappe.db.sql(
        """SELECT b.item_code, i.item_group, b.actual_qty
           FROM `tabBin` b JOIN `tabItem` i ON i.name = b.item_code
           WHERE b.warehouse = %s AND b.actual_qty != 0 AND i.disabled = 0""",
        warehouse,
        as_dict=True,
    )
    return [r for r in rows if r["item_group"] not in groups]


def build(apply=False):
    mark("MODE", "APPLY - writing" if apply else "DRY RUN - no writes")
    changes = 0

    for warehouse, root in PROFILES:
        if not frappe.db.exists("Warehouse", warehouse):
            mark("WARN", f"no such warehouse: {warehouse}")
            continue
        if not frappe.db.exists("Item Group", root):
            mark("WARN", f"no such item group: {root}")
            continue

        leaves = _descendants(root)
        covered = set(leaves)
        strays = _stock_here_outside(warehouse, covered)
        shown = frappe.db.count(
            "Item", {"item_group": ["in", leaves], "disabled": 0, "has_variants": 0}
        ) + len(strays)

        if frappe.db.exists("Warehouse Count Profile", {"warehouse": warehouse}):
            mark("ALREADY", f"{warehouse} already has a count profile")
            continue

        mark(
            "WOULD" if not apply else "APPLY",
            f"{warehouse}: profile on {root} -> {sorted(leaves)}"
            f" | {shown} rows (was 190)",
        )
        for s in strays:
            mark(
                "PIN",
                f"{warehouse}: pinning {s['item_code']} ({s['item_group']}, qty {s['actual_qty']})"
                f" as an Include -- holds stock here but sits outside {root}",
            )
        changes += 1

        if not apply:
            continue

        doc = frappe.get_doc(
            {
                "doctype": "Warehouse Count Profile",
                "warehouse": warehouse,
                "company": frappe.db.get_value("Warehouse", warehouse, "company"),
                "enabled": 1,
                "include_child_groups": 1,
                "item_groups": [{"item_group": root, "enabled": 1}],
                "item_exceptions": [
                    {
                        "item_code": s["item_code"],
                        "action": "Include",
                        "enabled": 1,
                        "reason": f"Holds stock in {warehouse} but sits outside {root}",
                    }
                    for s in strays
                ],
            }
        )
        doc.flags.ignore_permissions = True
        doc.insert()
        mark("DOC", f"Warehouse Count Profile {doc.name} created")

    if apply:
        frappe.db.commit()
        frappe.clear_cache()
    mark("DONE", f"changes={changes}")


if __name__ == "__main__":
    build(apply="--apply" in sys.argv)
