"""Backfill bundle_group_key and bundle_group_name on existing Sales Invoice Item rows.

Covers two populations:
  1. Rows created BEFORE the v1_5 patch (fields didn't exist) — fields are now empty strings.
  2. Rows created AFTER the v1_5 patch but before the flag_field loop was extended (same issue).

Uses frappe.db.set_value so no hooks / submit-state checks fire.  Safe to re-run (idempotent).
"""
import frappe


def execute():
    """Populate bundle_group_key / bundle_group_name on bundle-child invoice rows."""
    if not frappe.db.has_column("Sales Invoice Item", "bundle_group_key"):
        # Fields not yet added — v1_5 patch hasn't run; nothing to backfill.
        return

    # Collect distinct (parent_bundle, item_code) pairs that need backfill.
    rows = frappe.db.sql(
        """
        SELECT name, item_code, parent_bundle
        FROM `tabSales Invoice Item`
        WHERE is_bundle_child = 1
          AND (bundle_group_key IS NULL OR bundle_group_key = '')
          AND parent_bundle IS NOT NULL
          AND parent_bundle != ''
        """,
        as_dict=True,
    )

    if not rows:
        frappe.logger("jarz_pos.backfill").info(
            "v1_6 backfill: no bundle-child rows require group metadata update."
        )
        return

    # Build a cache: bundle_code -> {item_code -> {key, name}}
    cache: dict = {}
    updated = 0
    unresolvable = 0

    for row in rows:
        bundle_code = row["parent_bundle"]
        item_code = row["item_code"]

        if bundle_code not in cache:
            bundle_map: dict = {}
            try:
                bundle_doc = frappe.get_doc("Jarz Bundle", bundle_code)
                for group_row in bundle_doc.items:
                    group_key = str(getattr(group_row, "name", "") or "")
                    group_name = str(group_row.item_group or "")
                    items_in_group = frappe.get_all(
                        "Item",
                        filters={"item_group": group_name, "disabled": 0, "has_variants": 0},
                        fields=["name"],
                        limit=0,
                    )
                    for item_row in items_in_group:
                        bundle_map[item_row["name"]] = {
                            "key": group_key,
                            "name": group_name,
                        }
            except Exception as exc:
                frappe.logger("jarz_pos.backfill").warning(
                    f"v1_6 backfill: bundle '{bundle_code}' could not be loaded: {exc}"
                )
            cache[bundle_code] = bundle_map

        entry = cache.get(bundle_code, {}).get(item_code)
        if entry:
            frappe.db.set_value(
                "Sales Invoice Item",
                row["name"],
                {
                    "bundle_group_key": entry["key"],
                    "bundle_group_name": entry["name"],
                },
            )
            updated += 1
        else:
            unresolvable += 1

    frappe.db.commit()
    frappe.logger("jarz_pos.backfill").info(
        f"v1_6 backfill complete — scanned: {len(rows)}, updated: {updated}, "
        f"unresolvable (bundle/item not found): {unresolvable}"
    )
