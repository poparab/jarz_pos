"""B2B account branches: per-branch invoice separation, and merge-as-branch.

A B2B shop is ONE Customer (one legal account, one receivable) with several
branches. A branch is a shipping Address linked to that Customer -- the same
read model ``api.customer._build_customer_branch_options`` already feeds the
order flow, which folds legacy duplicate Address rows for one door into one
selectable branch and lists them in ``member_address_names``.

Every Sales Invoice already records the door it went to in
``shipping_address_name`` (``customer_address`` on older rows), so separating
invoices per branch is a READ over data that exists. Nothing new is written to
the Sales Invoice, which is at the MariaDB row limit anyway.

Merge-as-branch is the other half: the catalog, and the order flow before it,
could record one brand as two accounts -- "Orbt speciality coffee" and
"Orbt speciality Coffee - 1" are two doors of one brand, each with its own
invoices. Merging folds the source account into the target as a branch:

* both have a Customer  -> ``rename_doc(merge=True)`` moves every invoice,
  payment, GL/PLE row, address and contact onto the target. The source's
  addresses come across with it, so its invoices stay attributable to the
  source's door -- which is what makes them a *branch* of the target and not a
  blur in its history. Totals are snapshotted before and after; any drift
  raises and the whole request rolls back.
* both have a Lead      -> ``leads.merge_leads`` folds the catalog record
  (branches, contacts, notes) and parks the source off the board.
* only one side has a Customer -> the surviving card is linked to it, and the
  source Lead's own addresses become branches of the target Customer.

Merging two Customers is irreversible, so it is a manager action; folding two
catalog Leads is not, and stays open to a B2B rep exactly as ``merge_leads`` is.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import frappe

UNASSIGNED_BRANCH = "__unassigned__"

_MANAGER_ROLES = {"JARZ Manager", "System Manager", "Administrator"}


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------
def _has_column(doctype: str, fieldname: str) -> bool:
    try:
        return bool(frappe.db.has_column(doctype, fieldname))
    except Exception:
        return False


def _is_manager() -> bool:
    roles = set(frappe.get_roles(frappe.session.user) or [])
    return bool(roles.intersection(_MANAGER_ROLES))


def _flt(value: Any) -> float:
    try:
        return round(float(value or 0), 2)
    except (TypeError, ValueError):
        return 0.0


def _str_or_none(value: Any) -> Optional[str]:
    return str(value) if value else None


def _norm(value: Any) -> str:
    return " ".join(str(value or "").split()).strip()


# ---------------------------------------------------------------------------
# Branch book
# ---------------------------------------------------------------------------
def customer_branches(customer: str) -> List[Dict[str, Any]]:
    """The customer's physical branches, as the order flow sees them."""
    from jarz_pos.api.customer import _build_customer_shipping_address_book

    book = _build_customer_shipping_address_book(customer)
    options = list(book.get("branch_options") or [])
    coords = _address_coordinates(
        [o.get("address_name") for o in options if o.get("address_name")]
    )
    branches = []
    for option in options:
        address_name = option.get("address_name")
        lat, lng = coords.get(address_name, (None, None))
        branches.append(
            {
                "address_name": address_name,
                "branch_name": option.get("branch_name") or address_name,
                "address_line1": option.get("address_line1"),
                "address_line2": option.get("address_line2"),
                "city": option.get("city"),
                "phone": option.get("phone"),
                "territory": option.get("effective_territory"),
                "territory_missing": bool(option.get("territory_missing")),
                "is_primary_address": bool(option.get("is_primary_address")),
                "latitude": lat,
                "longitude": lng,
                "member_address_names": list(option.get("member_address_names") or [address_name]),
            }
        )
    return branches


def _address_coordinates(names: List[str]) -> Dict[str, tuple]:
    if not names or not (
        _has_column("Address", "custom_latitude") and _has_column("Address", "custom_longitude")
    ):
        return {}
    rows = frappe.get_all(
        "Address",
        filters={"name": ["in", names]},
        fields=["name", "custom_latitude", "custom_longitude"],
        limit_page_length=0,
    ) or []
    out = {}
    for row in rows:
        lat = row.get("custom_latitude")
        lng = row.get("custom_longitude")
        if lat or lng:
            out[row.get("name")] = (lat, lng)
    return out


def _member_index(branches: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """Address docname -> the branch it belongs to."""
    index = {}
    for branch in branches:
        for member in branch.get("member_address_names") or []:
            if member:
                index[member] = branch
    return index


def _invoice_address(row: Dict[str, Any]) -> str:
    return str(row.get("shipping_address_name") or row.get("customer_address") or "").strip()


def _branch_for_invoice(row, index):
    return index.get(_invoice_address(row))


# ---------------------------------------------------------------------------
# Per-branch invoice separation
# ---------------------------------------------------------------------------
def _invoice_fields() -> List[str]:
    fields = [
        "name",
        "posting_date",
        "grand_total",
        "outstanding_amount",
        "status",
        "is_return",
        "shipping_address_name",
        "customer_address",
    ]
    for optional in ("woo_order_id", "custom_order_purpose", "custom_payment_method"):
        if _has_column("Sales Invoice", optional):
            fields.append(optional)
    return fields


def _submitted_invoices(customer: str, limit: Optional[int] = None) -> List[Dict[str, Any]]:
    return frappe.get_all(
        "Sales Invoice",
        filters={"customer": customer, "docstatus": 1},
        fields=_invoice_fields(),
        order_by="posting_date desc, creation desc",
        limit_page_length=limit or 0,
    ) or []


def _empty_stats() -> Dict[str, Any]:
    return {"invoice_count": 0, "total_billed": 0.0, "outstanding": 0.0, "last_order_date": None}


def _add_to_stats(stats: Dict[str, Any], row: Dict[str, Any]) -> None:
    stats["invoice_count"] += 1
    stats["total_billed"] = _flt(stats["total_billed"] + _flt(row.get("grand_total")))
    stats["outstanding"] = _flt(stats["outstanding"] + _flt(row.get("outstanding_amount")))
    posted = _str_or_none(row.get("posting_date"))
    if posted and (not stats["last_order_date"] or posted > stats["last_order_date"]):
        stats["last_order_date"] = posted


def _invoice_totals_by_address(customer: str) -> List[Dict[str, Any]]:
    """Submitted-invoice totals per address the invoice was sent to."""
    return frappe.db.sql(
        """SELECT COALESCE(NULLIF(shipping_address_name, ''), customer_address) AS address,
                  COUNT(*) AS invoice_count,
                  COALESCE(SUM(grand_total), 0) AS total_billed,
                  COALESCE(SUM(outstanding_amount), 0) AS outstanding,
                  MAX(posting_date) AS last_order_date
           FROM `tabSales Invoice`
           WHERE customer = %s AND docstatus = 1
           GROUP BY COALESCE(NULLIF(shipping_address_name, ''), customer_address)""",
        (customer,),
        as_dict=True,
    ) or []


def account_branches(customer: str) -> Dict[str, Any]:
    """Branches with their own invoice totals and outstanding balance.

    ``unassigned`` holds invoices whose address is none of the branches (no
    address at all, or an Address no longer linked to this customer). It is
    reported rather than folded into a branch: guessing would put a debt on
    the wrong door.
    """
    branches = customer_branches(customer)
    index = _member_index(branches)
    stats = {b["address_name"]: _empty_stats() for b in branches}
    unassigned = _empty_stats()
    for group in _invoice_totals_by_address(customer):
        branch = index.get(str(group.get("address") or "").strip())
        target = stats[branch["address_name"]] if branch else unassigned
        target["invoice_count"] += int(group.get("invoice_count") or 0)
        target["total_billed"] = _flt(target["total_billed"] + _flt(group.get("total_billed")))
        target["outstanding"] = _flt(target["outstanding"] + _flt(group.get("outstanding")))
        posted = _str_or_none(group.get("last_order_date"))
        if posted and (not target["last_order_date"] or posted > target["last_order_date"]):
            target["last_order_date"] = posted
    for branch in branches:
        branch.update(stats[branch["address_name"]])
    return {
        "branches": branches,
        "unassigned": unassigned if unassigned["invoice_count"] else None,
    }


def map_invoice(row: Dict[str, Any], index: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    from jarz_pos.utils.invoice_utils import normalize_woo_order_id

    branch = _branch_for_invoice(row, index)
    return {
        "name": row.get("name"),
        "woo_order_id": normalize_woo_order_id(row.get("woo_order_id")),
        "posting_date": _str_or_none(row.get("posting_date")),
        "grand_total": row.get("grand_total"),
        "outstanding_amount": _flt(row.get("outstanding_amount")),
        "custom_order_purpose": row.get("custom_order_purpose") or "Standard",
        "custom_payment_method": row.get("custom_payment_method"),
        "status": row.get("status"),
        "is_return": bool(row.get("is_return")),
        "branch_address": branch["address_name"] if branch else None,
        "branch_name": branch["branch_name"] if branch else None,
    }


def account_invoices(customer: str, branch: Optional[str] = None, limit: int = 100) -> Dict[str, Any]:
    """Every submitted invoice of the customer, optionally for one branch.

    All order purposes are listed: a shop's history before it became a B2B
    account (Standard orders) is still that door's history. ``summary`` is the
    whole branch, never just the listed page.
    """
    branches = customer_branches(customer)
    index = _member_index(branches)
    branch = str(branch or "").strip() or None
    if branch and branch != UNASSIGNED_BRANCH and branch not in {b["address_name"] for b in branches}:
        frappe.throw("That branch does not belong to this customer.")

    rows = []
    summary = _empty_stats()
    for row in _submitted_invoices(customer):
        owner = _branch_for_invoice(row, index)
        if branch == UNASSIGNED_BRANCH and owner is not None:
            continue
        if branch and branch != UNASSIGNED_BRANCH and (owner or {}).get("address_name") != branch:
            continue
        _add_to_stats(summary, row)
        rows.append(row)

    listed = rows[:limit]
    return {
        "customer": customer,
        "branch": branch,
        "invoices": [map_invoice(row, index) for row in listed],
        "summary": summary,
        "truncated": len(rows) > len(listed),
    }


# ---------------------------------------------------------------------------
# Merge-as-branch: party resolution and plan
# ---------------------------------------------------------------------------
def _merged_into(lead: str) -> Optional[str]:
    if not _has_column("Lead", "custom_merged_into"):
        return None
    return frappe.db.get_value("Lead", lead, "custom_merged_into") or None


def _leads_for_customer(customer: str) -> List[str]:
    """Live (not merged-away) Leads that resolve to this Customer."""
    names = set(
        frappe.get_all("Lead", filters={"customer": customer}, pluck="name") or []
    )
    converted = frappe.db.get_value("Customer", customer, "lead_name")
    if converted and frappe.db.exists("Lead", converted):
        names.add(converted)
    return sorted(n for n in names if not _merged_into(n))


def resolve_party(doctype: str, name: str) -> Dict[str, Any]:
    """{"doctype","name","title","lead","customer"} for a Lead or Customer card."""
    from jarz_pos.api.crm import _resolve_lead_customer

    doctype = str(doctype or "").strip()
    name = str(name or "").strip()
    if doctype not in ("Lead", "Customer"):
        frappe.throw("Only a Lead or a Customer account can be merged.")
    if not name or not frappe.db.exists(doctype, name):
        frappe.throw(f"{doctype} '{name}' not found.")

    if doctype == "Lead":
        merged = _merged_into(name)
        if merged:
            frappe.throw(f"Lead '{name}' has already been merged into '{merged}'.")
        row = frappe.db.get_value("Lead", name, ["lead_name", "company_name"], as_dict=True) or {}
        return {
            "doctype": "Lead",
            "name": name,
            "title": row.get("lead_name") or row.get("company_name") or name,
            "lead": name,
            "customer": _resolve_lead_customer(name),
        }

    row = frappe.db.get_value("Customer", name, ["customer_name", "disabled"], as_dict=True) or {}
    if int(row.get("disabled") or 0):
        frappe.throw(f"Customer '{name}' is disabled.")
    leads = _leads_for_customer(name)
    return {
        "doctype": "Customer",
        "name": name,
        "title": row.get("customer_name") or name,
        # Two live Leads on one Customer is an existing duplicate; the merge
        # does not pick one of them for the user.
        "lead": leads[0] if len(leads) == 1 else None,
        "customer": name,
    }


def build_plan(source: Dict[str, Any], target: Dict[str, Any]) -> Dict[str, Any]:
    """Decide what a merge will do, without doing any of it."""
    s_cust, t_cust = source.get("customer"), target.get("customer")
    s_lead, t_lead = source.get("lead"), target.get("lead")

    if (source["doctype"], source["name"]) == (target["doctype"], target["name"]):
        frappe.throw("An account cannot be merged into itself.")
    same_customer = bool(s_cust and t_cust and s_cust == t_cust)
    two_customers = bool(s_cust and t_cust and s_cust != t_cust)
    same_lead = bool(s_lead and t_lead and s_lead == t_lead)
    if same_customer and not (s_lead and t_lead and not same_lead):
        frappe.throw("These two cards are already the same account.")
    if same_lead and not two_customers:
        frappe.throw("These two cards are already the same account.")

    customer_action = None
    if s_cust and t_cust and s_cust != t_cust:
        customer_action = "merge_customers"
    elif s_cust and not t_cust:
        customer_action = "adopt_source_customer"
    elif t_cust and not s_cust and s_lead:
        customer_action = "link_lead_addresses"

    lead_action = None
    if s_lead and t_lead and not same_lead:
        lead_action = "merge_leads"
    elif s_lead and not t_lead:
        # Target is a bare Customer: the source Lead stays the board card and
        # follows the account it now belongs to.
        lead_action = "relink_source_lead"

    return {
        "customer_action": customer_action,
        "lead_action": lead_action,
        "requires_manager": customer_action == "merge_customers",
        "final_customer": t_cust or s_cust,
    }


def _structural_use(customer: str) -> Optional[str]:
    """Why this Customer must never be merged away or absorb another, or None.

    ``rename_doc`` rewrites EVERY link to the source, including a POS
    Profile's walk-in customer and settings defaults: merging such a record
    into a shop would book every later walk-in sale to that shop's receivable.
    """
    if frappe.get_all("POS Profile", filters={"customer": customer}, limit_page_length=1):
        return "it is the default customer of a POS Profile"
    if frappe.db.sql(
        """SELECT 1 FROM `tabSingles` s
           JOIN `tabDocField` f ON f.parent = s.doctype AND f.fieldname = s.field
           WHERE f.fieldtype = 'Link' AND f.options = 'Customer' AND s.value = %s
           UNION
           SELECT 1 FROM `tabSingles` s
           JOIN `tabCustom Field` f ON f.dt = s.doctype AND f.fieldname = s.field
           WHERE f.fieldtype = 'Link' AND f.options = 'Customer' AND s.value = %s
           LIMIT 1""",
        (customer, customer),
    ):
        return "it is a default in a settings page"
    if _has_column("Customer", "custom_employee") and frappe.db.get_value(
        "Customer", customer, "custom_employee"
    ):
        return "it is an employee's staff account"
    return None


def _label_clash(source: str, target: str) -> List[str]:
    """Flavours both customers track a printed label for (one ledger each)."""
    try:
        if not frappe.db.table_exists("Jarz Customer Label"):
            return []
        return [
            row[0]
            for row in frappe.db.sql(
                """SELECT DISTINCT s.item FROM `tabJarz Customer Label` s
                   JOIN `tabJarz Customer Label` t ON t.item = s.item
                   WHERE s.customer = %s AND t.customer = %s AND IFNULL(s.item, '') != ''""",
                (source, target),
            )
        ]
    except Exception:
        return []


def _woo_id(customer: str) -> Optional[str]:
    if not _has_column("Customer", "woo_customer_id"):
        return None
    value = frappe.db.get_value("Customer", customer, "woo_customer_id")
    return str(value).strip() if value not in (None, "", 0, "0") else None


def customer_merge_blockers(source: str, target: str) -> List[str]:
    """Reasons a customer merge must not run. Empty means it may."""
    blockers = []
    for role, customer in (("source", source), ("target", target)):
        reason = _structural_use(customer)
        if reason:
            blockers.append(f"'{customer}' cannot be merged: {reason}.")
    clash = _label_clash(source, target)
    if clash:
        blockers.append(
            "Both accounts track printed labels for the same flavour ("
            + ", ".join(clash[:5])
            + "). Combine those label records first."
        )
    s_woo, t_woo = _woo_id(source), _woo_id(target)
    if s_woo and t_woo and s_woo != t_woo:
        blockers.append(
            "Both accounts are linked to different WooCommerce customers; one link "
            "would be lost. Resolve that in the Woo integration first."
        )
    return blockers


def _customer_summary(customer: Optional[str]) -> Optional[Dict[str, Any]]:
    if not customer:
        return None
    fields = ["name", "customer_name", "customer_type", "customer_group", "mobile_no"]
    for optional in ("custom_credit_allowed", "custom_credit_days", "custom_credit_limit_amount", "woo_customer_id"):
        if _has_column("Customer", optional):
            fields.append(optional)
    row = frappe.db.get_value("Customer", customer, fields, as_dict=True) or {}
    totals = frappe.db.sql(
        """SELECT COUNT(*) AS invoice_count,
                  COALESCE(SUM(grand_total), 0) AS total_billed,
                  COALESCE(SUM(outstanding_amount), 0) AS outstanding
           FROM `tabSales Invoice` WHERE customer = %s AND docstatus = 1""",
        (customer,),
        as_dict=True,
    )[0]
    from jarz_pos.utils.customer_address_utils import get_linked_customer_address_names

    return {
        "name": customer,
        "customer_name": row.get("customer_name"),
        "customer_type": row.get("customer_type"),
        "mobile_no": row.get("mobile_no"),
        "credit_allowed": bool(row.get("custom_credit_allowed")),
        "credit_days": row.get("custom_credit_days"),
        "credit_limit": row.get("custom_credit_limit_amount"),
        "woo_customer_id": row.get("woo_customer_id"),
        "invoice_count": int(totals.get("invoice_count") or 0),
        "total_billed": _flt(totals.get("total_billed")),
        "outstanding": _flt(totals.get("outstanding")),
        "address_count": len(get_linked_customer_address_names(customer) or []),
    }


def preview(source: Dict[str, Any], target: Dict[str, Any]) -> Dict[str, Any]:
    plan = build_plan(source, target)
    s_sum = _customer_summary(source.get("customer"))
    t_sum = _customer_summary(target.get("customer"))
    warnings = []
    blockers = []
    if plan["customer_action"] == "merge_customers":
        warnings.append("irreversible_customer_merge")
        if s_sum and s_sum.get("credit_allowed") and not (t_sum or {}).get("credit_allowed"):
            warnings.append("credit_terms_carried_over")
        blockers = customer_merge_blockers(source["customer"], target["customer"])
    return {
        "source": {k: source.get(k) for k in ("doctype", "name", "title", "lead", "customer")},
        "target": {k: target.get(k) for k in ("doctype", "name", "title", "lead", "customer")},
        "plan": plan,
        "source_customer": s_sum,
        "target_customer": t_sum,
        "can_execute": (_is_manager() or not plan["requires_manager"]) and not blockers,
        "warnings": warnings,
        "blockers": blockers,
    }


# ---------------------------------------------------------------------------
# Merge-as-branch: execution
# ---------------------------------------------------------------------------
def _snapshot(names: List[str]) -> Dict[str, float]:
    """Money and row totals that a merge must move but never change."""
    placeholders = ", ".join(["%s"] * len(names))
    si = frappe.db.sql(
        f"""SELECT COUNT(*) AS n,
                   COALESCE(SUM(CASE WHEN docstatus = 1 THEN grand_total END), 0) AS billed,
                   COALESCE(SUM(CASE WHEN docstatus = 1 THEN outstanding_amount END), 0) AS outstanding
            FROM `tabSales Invoice` WHERE customer IN ({placeholders})""",
        tuple(names),
        as_dict=True,
    )[0]
    gl = frappe.db.sql(
        f"""SELECT COUNT(*) AS n, COALESCE(SUM(debit), 0) AS debit, COALESCE(SUM(credit), 0) AS credit
            FROM `tabGL Entry`
            WHERE party_type = 'Customer' AND party IN ({placeholders}) AND is_cancelled = 0""",
        tuple(names),
        as_dict=True,
    )[0]
    pe = frappe.db.sql(
        f"""SELECT COUNT(*) AS n, COALESCE(SUM(CASE WHEN docstatus = 1 THEN paid_amount END), 0) AS paid
            FROM `tabPayment Entry` WHERE party_type = 'Customer' AND party IN ({placeholders})""",
        tuple(names),
        as_dict=True,
    )[0]
    ple = frappe.db.sql(
        f"""SELECT COALESCE(SUM(amount), 0) AS amount FROM `tabPayment Ledger Entry`
            WHERE party_type = 'Customer' AND party IN ({placeholders}) AND delinked = 0""",
        tuple(names),
        as_dict=True,
    )[0]
    return {
        "ple_amount": _flt(ple.get("amount")),
        "invoices": int(si.get("n") or 0),
        "billed": _flt(si.get("billed")),
        "outstanding": _flt(si.get("outstanding")),
        "gl_rows": int(gl.get("n") or 0),
        "gl_debit": _flt(gl.get("debit")),
        "gl_credit": _flt(gl.get("credit")),
        "payments": int(pe.get("n") or 0),
        "paid": _flt(pe.get("paid")),
    }


def _stamp_unaddressed_invoices(source: str) -> int:
    """Give the source's address-less invoices the source's own door.

    After the merge they sit on the target's account, where an invoice with no
    address could no longer be told apart from the target's own. Only rows with
    NO address at all are touched, and only when the source has exactly one
    unambiguous door to give them.
    """
    from jarz_pos.utils.customer_address_utils import get_linked_customer_address_names

    addresses = list(get_linked_customer_address_names(source) or [])
    if len(addresses) != 1:
        return 0
    door = addresses[0]
    rows = frappe.get_all(
        "Sales Invoice",
        filters={
            "customer": source,
            "docstatus": ["<", 2],
            "shipping_address_name": ["is", "not set"],
            "customer_address": ["is", "not set"],
        },
        pluck="name",
        limit_page_length=0,
    ) or []
    for name in rows:
        frappe.db.set_value("Sales Invoice", name, "shipping_address_name", door, update_modified=False)
    return len(rows)


def _title_source_addresses(source: str, branch_name: Optional[str]) -> int:
    branch_name = _norm(branch_name)
    if not branch_name:
        return 0
    from jarz_pos.utils.customer_address_utils import get_linked_customer_address_names

    names = list(get_linked_customer_address_names(source) or [])
    # One door, one name. A multi-door source keeps its own titles, and an
    # Address some other party also uses is never renamed from here.
    if len(names) != 1:
        return 0
    shared = frappe.db.sql(
        """SELECT COUNT(*) FROM `tabDynamic Link`
           WHERE parenttype = 'Address' AND parent = %s
             AND NOT (link_doctype = 'Customer' AND link_name = %s)""",
        (names[0], source),
    )[0][0]
    if shared:
        return 0
    frappe.db.set_value("Address", names[0], "address_title", branch_name[:140], update_modified=False)
    return 1


def _carry_credit_terms(source: str, target: str) -> bool:
    """A shop allowed on account stays allowed after absorbing its twin."""
    if not _has_column("Customer", "custom_credit_allowed"):
        return False
    s = frappe.db.get_value(
        "Customer", source,
        ["custom_credit_allowed", "custom_credit_days", "custom_credit_limit_amount"],
        as_dict=True,
    ) or {}
    t_allowed = frappe.db.get_value("Customer", target, "custom_credit_allowed")
    if not int(s.get("custom_credit_allowed") or 0) or int(t_allowed or 0):
        return False
    frappe.db.set_value(
        "Customer", target,
        {
            "custom_credit_allowed": 1,
            "custom_credit_days": s.get("custom_credit_days"),
            "custom_credit_limit_amount": s.get("custom_credit_limit_amount"),
        },
        update_modified=False,
    )
    return True


def merge_customers(source: str, target: str, branch_name: Optional[str] = None) -> Dict[str, Any]:
    """Fold Customer ``source`` into ``target`` as one of its branches."""
    from frappe.model.rename_doc import rename_doc

    blockers = customer_merge_blockers(source, target)
    if blockers:
        frappe.throw(" ".join(blockers))

    savepoint = "jarz_b2b_merge_as_branch"
    frappe.db.savepoint(savepoint)
    try:
        return _merge_customers_in_savepoint(rename_doc, source, target, branch_name)
    except Exception:
        # Outside a POST request nothing else would undo a half-done merge.
        frappe.db.rollback(save_point=savepoint)
        raise


def _merge_customers_in_savepoint(rename_doc, source, target, branch_name):
    before = _snapshot([source, target])
    woo_carried = None
    s_woo, t_woo = _woo_id(source), _woo_id(target)
    stamped = _stamp_unaddressed_invoices(source)
    titled = _title_source_addresses(source, branch_name)
    credit_carried = _carry_credit_terms(source, target)

    # Customer.on_trash (run by the merge's delete of the source) resets the
    # source's converted Lead to "Interested", and Customer.after_rename
    # overwrites the target's display name with its docname under
    # "naming by Customer Name". Neither is part of a merge; put both back.
    target_display = frappe.db.get_value("Customer", target, "customer_name")
    source_converted = frappe.db.get_value("Customer", source, "lead_name")
    lead_names = set(_leads_for_customer(source)) | set(
        frappe.get_all("Lead", filters={"customer": source}, pluck="name") or []
    )
    if source_converted:
        lead_names.add(source_converted)
    lead_statuses = {
        name: frappe.db.get_value("Lead", name, "status") for name in lead_names
    }

    rename_doc(
        "Customer", source, target,
        merge=True, force=True, ignore_permissions=True,
        show_alert=False, rebuild_search=False,
    )

    frappe.db.set_value("Customer", target, "customer_name", target_display, update_modified=False)
    for name, status in lead_statuses.items():
        if status:
            frappe.db.set_value("Lead", name, "status", status, update_modified=False)
    # A Lead tied to the source only through Customer.lead_name now points at a
    # deleted record; give it the account it belongs to.
    from jarz_pos.api.crm import _resolve_lead_customer

    for name in lead_names:
        if (
            frappe.db.exists("Lead", name)
            and not frappe.db.get_value("Lead", name, "customer")
            and not _resolve_lead_customer(name, strict=False)
        ):
            frappe.db.set_value("Lead", name, "customer", target, update_modified=False)
    # The source's Woo binding would vanish with it, and the next Woo order
    # would recreate the duplicate this merge just removed.
    if s_woo and not t_woo:
        frappe.db.set_value("Customer", target, "woo_customer_id", s_woo, update_modified=False)
        woo_carried = s_woo

    after = _snapshot([target])
    drift = [key for key in before if before[key] != after[key]]
    if frappe.db.exists("Customer", source):
        drift.append("source_still_present")
    if (s_woo or t_woo) and not _woo_id(target):
        drift.append("woo_customer_id")
    if drift:
        frappe.throw(
            "Merge stopped: the account totals did not survive the merge unchanged "
            f"({', '.join(drift)}). Nothing was changed."
        )
    return {
        "stamped_invoices": stamped,
        "titled_addresses": titled,
        "credit_terms_carried": credit_carried,
        "woo_customer_id_carried": woo_carried,
        "totals": after,
    }


def _link_lead_addresses(lead: str, customer: str) -> int:
    """Attach a Lead's own Addresses to a Customer so they become its branches."""
    names = frappe.get_all(
        "Dynamic Link",
        filters={"parenttype": "Address", "link_doctype": "Lead", "link_name": lead},
        pluck="parent",
        limit_page_length=0,
    ) or []
    linked = 0
    for name in dict.fromkeys(names):
        if not frappe.db.exists("Address", name):
            continue
        address = frappe.get_doc("Address", name)
        if any(
            l.link_doctype == "Customer" and l.link_name == customer for l in (address.links or [])
        ):
            continue
        address.append("links", {"link_doctype": "Customer", "link_name": customer})
        if not address.is_shipping_address:
            address.is_shipping_address = 1
        address.flags.ignore_permissions = True
        address.save(ignore_permissions=True)
        linked += 1
    return linked


def execute(source: Dict[str, Any], target: Dict[str, Any], branch_name: Optional[str] = None) -> Dict[str, Any]:
    """Run the plan. Raises (and so rolls the request back) on any failure."""
    plan = build_plan(source, target)
    if plan["requires_manager"] and not _is_manager():
        frappe.throw(
            "Only a manager can merge two customer accounts: it moves every invoice "
            "and payment and cannot be undone."
        )

    s_cust, t_cust = source.get("customer"), target.get("customer")
    s_lead, t_lead = source.get("lead"), target.get("lead")
    result: Dict[str, Any] = {
        "plan": plan,
        "customer": plan["final_customer"],
        "merged_customer": None,
        "moved_invoices": 0,
        "stamped_invoices": 0,
        "linked_addresses": 0,
        "credit_terms_carried": False,
        "merged_lead": None,
    }

    previous_flag = getattr(frappe.flags, "ignore_woo_outbound", False)
    # A merge rewrites the Customer link on every moved row; none of that is a
    # change the Woo store needs to hear about.
    frappe.flags.ignore_woo_outbound = True
    try:
        action = plan["customer_action"]
        if action == "merge_customers":
            target_own = int(
                frappe.db.count("Sales Invoice", {"customer": t_cust, "docstatus": 1}) or 0
            )
            source_own = int(
                frappe.db.count("Sales Invoice", {"customer": s_cust, "docstatus": 1}) or 0
            )
            merged = merge_customers(s_cust, t_cust, branch_name)
            result.update(
                merged_customer=s_cust,
                moved_invoices=source_own,
                stamped_invoices=merged["stamped_invoices"],
                credit_terms_carried=merged["credit_terms_carried"],
                totals=merged["totals"],
                target_invoices_before=target_own,
            )
        elif action == "adopt_source_customer":
            frappe.db.set_value("Lead", t_lead, "customer", s_cust, update_modified=True)
        elif action == "link_lead_addresses":
            result["linked_addresses"] = _link_lead_addresses(s_lead, t_cust)

        lead_action = plan["lead_action"]
        if lead_action == "merge_leads":
            from jarz_pos.api.leads import merge_leads

            merge_leads(t_lead, [s_lead])
            result["merged_lead"] = s_lead
            # The surviving card must resolve to the surviving account.
            if plan["final_customer"] and not frappe.db.get_value("Lead", t_lead, "customer"):
                from jarz_pos.api.crm import _resolve_lead_customer

                if _resolve_lead_customer(t_lead, strict=False) != plan["final_customer"]:
                    frappe.db.set_value(
                        "Lead", t_lead, "customer", plan["final_customer"], update_modified=True
                    )
        elif lead_action == "relink_source_lead":
            frappe.db.set_value("Lead", s_lead, "customer", t_cust, update_modified=True)
    finally:
        frappe.flags.ignore_woo_outbound = previous_flag

    return result
