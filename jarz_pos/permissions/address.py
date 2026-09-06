"""Least-privilege Address reads for the B2B ordering flow.

Frappe's controller permission hooks can narrow a role permission but cannot
grant one. ``setup.b2b_master_data`` therefore supplies read-only Address access
to the B2B Sales Rep role, while this module restricts that access to Addresses
directly linked to a Customer the same actor may read.
"""

from __future__ import annotations

from typing import Any

import frappe

from jarz_pos.constants import ROLES


B2B_ROLE = "B2B Sales Rep"
READ_TYPES = {"read", "select"}


def _is_scoped_b2b_user(user: str) -> bool:
    roles = set(frappe.get_roles(user) or [])
    manager_roles = ROLES.LINE_MANAGER_TIER | ROLES.MANAGER
    return B2B_ROLE in roles and not roles.intersection(manager_roles)


def _linked_customers(address_name: str) -> list[str]:
    if not address_name:
        return []
    return frappe.get_all(
        "Dynamic Link",
        filters={
            "parenttype": "Address",
            "parent": address_name,
            "link_doctype": "Customer",
        },
        pluck="link_name",
        limit_page_length=0,
    )


def has_permission(
    doc: Any,
    ptype: str = "read",
    user: str | None = None,
    debug: bool = False,
) -> bool:
    """Deny B2B-only reads unless a readable Customer owns the Address.

    Returning ``True`` does not grant access in Frappe; it only lets the normal
    role permission continue. Every other user therefore keeps their existing
    Address permissions unchanged.
    """
    resolved_user = user or frappe.session.user
    if ptype not in READ_TYPES or not _is_scoped_b2b_user(resolved_user):
        return True

    address_name = str(
        getattr(doc, "name", None)
        or (doc.get("name") if hasattr(doc, "get") else "")
        or ""
    ).strip()
    try:
        return any(
            frappe.has_permission(
                "Customer",
                ptype="read",
                doc=customer,
                user=resolved_user,
            )
            for customer in _linked_customers(address_name)
        )
    except Exception:
        # A permission lookup failure must not expose an Address.
        return False


def get_permission_query_conditions(
    user: str | None = None,
    doctype: str | None = None,
) -> str:
    """Keep Address lists inside the same Customer-readable boundary."""
    resolved_user = user or frappe.session.user
    if not _is_scoped_b2b_user(resolved_user):
        return ""

    from frappe.model.db_query import DatabaseQuery

    customer_conditions = DatabaseQuery(
        "Customer", user=resolved_user
    ).build_match_conditions()
    readable_customer_clause = (
        f" AND ({customer_conditions})" if customer_conditions else ""
    )
    return f"""
        EXISTS (
            SELECT 1
            FROM `tabDynamic Link` AS `jarz_b2b_address_link`
            INNER JOIN `tabCustomer`
                ON `tabCustomer`.`name` = `jarz_b2b_address_link`.`link_name`
            WHERE `jarz_b2b_address_link`.`parenttype` = 'Address'
              AND `jarz_b2b_address_link`.`parent` = `tabAddress`.`name`
              AND `jarz_b2b_address_link`.`link_doctype` = 'Customer'
              {readable_customer_clause}
        )
    """.strip()
