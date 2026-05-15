from __future__ import annotations

import frappe
from frappe import _
from frappe.model.document import Document


class WarehouseCountProfile(Document):
    def validate(self) -> None:
        warehouse = self._validate_warehouse()
        self.company = warehouse.get("company")
        self._validate_item_groups()
        self._validate_item_exceptions()

    def _validate_warehouse(self) -> dict:
        warehouse = frappe.db.get_value(
            "Warehouse",
            self.warehouse,
            ["name", "company", "is_group"],
            as_dict=True,
        )
        if not warehouse:
            frappe.throw(_("Warehouse {0} does not exist.").format(self.warehouse))
        if int(warehouse.get("is_group") or 0):
            frappe.throw(
                _("Warehouse Count Profile can only target a leaf warehouse."),
            )
        return warehouse

    def _validate_item_groups(self) -> None:
        seen = {}
        duplicates = []
        for row in getattr(self, "item_groups", []) or []:
            if not int(getattr(row, "enabled", 1) or 0):
                continue
            item_group = (getattr(row, "item_group", "") or "").strip()
            if not item_group:
                continue
            key = item_group.casefold()
            if key in seen:
                duplicates.append(item_group)
                continue
            seen[key] = item_group
        if duplicates:
            frappe.throw(
                _("Duplicate item groups are not allowed: {0}").format(
                    ", ".join(sorted(set(duplicates))),
                ),
            )

    def _validate_item_exceptions(self) -> None:
        by_item = {}
        duplicate_rows = []
        for row in getattr(self, "item_exceptions", []) or []:
            if not int(getattr(row, "enabled", 1) or 0):
                continue
            item_code = (getattr(row, "item_code", "") or "").strip()
            action = (getattr(row, "action", "") or "").strip().title()
            if not item_code or not action:
                continue
            entry = by_item.setdefault(
                item_code.casefold(),
                {"label": item_code, "actions": set()},
            )
            if action in entry["actions"]:
                duplicate_rows.append(f"{item_code} ({action})")
                continue
            entry["actions"].add(action)

        if duplicate_rows:
            frappe.throw(
                _("Duplicate item exception rows are not allowed: {0}").format(
                    ", ".join(sorted(set(duplicate_rows))),
                ),
            )

        conflicts = [
            entry["label"]
            for entry in by_item.values()
            if {"Include", "Exclude"}.issubset(entry["actions"])
        ]
        if conflicts:
            frappe.throw(
                _(
                    "An item cannot be both included and excluded in the same warehouse profile: {0}"
                ).format(", ".join(sorted(conflicts))),
            )