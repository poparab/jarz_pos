"""B2B CRM pipeline API for Jarz POS (role-gated).

Whitelisted endpoints powering the Flutter B2B sales-rep app: a unified
Lead/Opportunity pipeline board, per-account detail, manual stage advancement,
lead creation, activity logging, a "Today" follow-up feed, reorder-due
customers, and thin sample/order binding helpers.

Design notes:
  - Every endpoint is gated by ``_ensure_b2b_access()`` (B2B Sales Rep OR manager).
  - These endpoints are INTERACTIVE (user-triggered), so unlike the scheduled CRM
    modules they may raise (frappe.throw) on bad input / permission failures. They
    still guard every optional DocType/field access so a missing custom field never
    crashes the board — it just omits that datum.
  - Invoice creation is NEVER duplicated here: ``request_sample`` / ``place_b2b_order``
    only resolve the binding (customer + order_purpose + price_list) the app then
    feeds to the existing POS invoice endpoint.
  - Responses are plain JSON-serializable dicts/lists.
"""

import frappe

# Canonical B2B stage options (must match the custom_b2b_stage Select field).
B2B_STAGES = [
    "Lead",
    "Qualify",
    "Sample",
    "Approved",
    "Trial",
    "Check-up",
    "Active",
    "Lost/On-hold",
]

# Stages a record lives in BEFORE a sample is requested — used to decide which
# Leads still belong on the board (post-sample work happens on Opportunities).
_PRE_SAMPLE_STAGES = ("Lead", "Qualify")

_LOST_STAGE = "Lost/On-hold"

# Default commercial policy / price-list bindings for the thin order helpers.
_SAMPLE_ORDER_PURPOSE = "Sample - Courier"
_B2B_ORDER_PURPOSE = "B2B Supply"


# ---------------------------------------------------------------------------
# Guards / small helpers
# ---------------------------------------------------------------------------
def _manager_roles():
    return {
        "JARZ Manager",
        "jarz line manager",
        "JARZ line manager",
        "System Manager",
        "Administrator",
    }


def _can_access_b2b():
    roles = set(frappe.get_roles(frappe.session.user) or [])
    if "B2B Sales Rep" in roles:
        return True
    return bool(roles.intersection(_manager_roles()))


def _ensure_b2b_access():
    """Raise unless the caller is a B2B Sales Rep or a manager."""
    if not _can_access_b2b():
        frappe.throw("Not permitted: B2B sales access required.")


def _doctype_exists(name):
    try:
        return bool(frappe.db.exists("DocType", name))
    except Exception:
        return False


def _has_field(doctype, fieldname):
    try:
        return bool(frappe.get_meta(doctype).get_field(fieldname))
    except Exception:
        return False


def _stage_options(doctype="Lead"):
    """Valid Select options for ``custom_b2b_stage`` on ``doctype`` (fallback const)."""
    try:
        field = frappe.get_meta(doctype).get_field("custom_b2b_stage")
        if field and field.options:
            opts = [o.strip() for o in (field.options or "").split("\n") if o.strip()]
            if opts:
                return opts
    except Exception:
        pass
    return list(B2B_STAGES)


def _today():
    try:
        from frappe.utils import today

        return today()
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Pipeline board
# ---------------------------------------------------------------------------
@frappe.whitelist()
def get_b2b_pipeline():
    """Return the unified B2B board grouped by stage.

    Shape:
        {
            "stages": ["Lead", "Qualify", ...],
            "columns": {
                "<stage>": [ <card>, ... ],
                ...
            }
        }

    Each <card> dict:
        {
            "doctype": "Lead" | "Opportunity",
            "name": str,
            "title": str,
            "stage": str,
            "owner": str | None,
            "lead_score": int | None,   # only for Lead
            "customer": str | None,     # linked Customer name if any
            "last_activity": str | None # modified timestamp
        }
    """
    _ensure_b2b_access()

    stages = _stage_options("Lead")
    columns = {s: [] for s in stages}

    # --- Leads (pre-sample stages only) -----------------------------------
    if _doctype_exists("Lead") and _has_field("Lead", "custom_b2b_stage"):
        lead_fields = ["name", "custom_b2b_stage", "owner", "modified"]
        for f in ("lead_name", "company_name", "custom_lead_score"):
            if _has_field("Lead", f):
                lead_fields.append(f)
        try:
            leads = frappe.get_all(
                "Lead",
                filters={"custom_b2b_stage": ["in", list(_PRE_SAMPLE_STAGES)]},
                fields=lead_fields,
                limit_page_length=0,
            )
        except Exception:
            leads = []
        for row in leads:
            stage = row.get("custom_b2b_stage") or "Lead"
            card = {
                "doctype": "Lead",
                "name": row.get("name"),
                "title": row.get("lead_name")
                or row.get("company_name")
                or row.get("name"),
                "stage": stage,
                "owner": row.get("owner"),
                "lead_score": row.get("custom_lead_score"),
                "customer": None,
                "last_activity": str(row.get("modified")) if row.get("modified") else None,
            }
            columns.setdefault(stage, []).append(card)

    # --- Opportunities (any B2B stage) ------------------------------------
    if _doctype_exists("Opportunity") and _has_field("Opportunity", "custom_b2b_stage"):
        opp_fields = ["name", "custom_b2b_stage", "owner", "modified"]
        for f in ("party_name", "customer_name"):
            if _has_field("Opportunity", f):
                opp_fields.append(f)
        try:
            opps = frappe.get_all(
                "Opportunity",
                filters={"custom_b2b_stage": ["is", "set"]},
                fields=opp_fields,
                limit_page_length=0,
            )
        except Exception:
            opps = []
        for row in opps:
            stage = row.get("custom_b2b_stage")
            if not stage:
                continue
            linked_customer = _resolve_opp_customer(row.get("party_name"))
            card = {
                "doctype": "Opportunity",
                "name": row.get("name"),
                "title": row.get("customer_name")
                or row.get("party_name")
                or row.get("name"),
                "stage": stage,
                "owner": row.get("owner"),
                "lead_score": None,
                "customer": linked_customer,
                "last_activity": str(row.get("modified")) if row.get("modified") else None,
            }
            columns.setdefault(stage, []).append(card)

    return {"stages": stages, "columns": columns}


def _resolve_opp_customer(party_name):
    """Return the Customer name an Opportunity points at, else None. Never raises."""
    if not party_name:
        return None
    try:
        if frappe.db.exists("Customer", party_name):
            return party_name
    except Exception:
        pass
    return None


# ---------------------------------------------------------------------------
# Account detail
# ---------------------------------------------------------------------------
@frappe.whitelist()
def get_account(doctype, name):
    """Full detail for one pipeline card (Lead or Opportunity).

    Shape:
        {
            "doctype": str,
            "name": str,
            "title": str,
            "stage": str | None,
            "owner": str | None,
            "contact": {"mobile_no": str|None, "email_id": str|None, "phone": str|None},
            "customer": str | None,
            "predicted_next_order": str | None,
            "avg_order_cycle_days": float | None,
            "recent_invoices": [ {"name","posting_date","grand_total","custom_order_purpose","status"} ],
            "open_todos": [ {"name","description","date"} ]
        }
    """
    _ensure_b2b_access()

    if doctype not in ("Lead", "Opportunity"):
        frappe.throw("doctype must be 'Lead' or 'Opportunity'.")
    if not _doctype_exists(doctype) or not frappe.db.exists(doctype, name):
        frappe.throw(f"{doctype} '{name}' not found.")

    doc = frappe.get_doc(doctype, name)

    result = {
        "doctype": doctype,
        "name": name,
        "title": getattr(doc, "lead_name", None)
        or getattr(doc, "customer_name", None)
        or getattr(doc, "party_name", None)
        or name,
        "stage": getattr(doc, "custom_b2b_stage", None),
        "owner": getattr(doc, "owner", None),
        "contact": {
            "mobile_no": getattr(doc, "mobile_no", None),
            "email_id": getattr(doc, "email_id", None)
            or getattr(doc, "contact_email", None),
            "phone": getattr(doc, "phone", None) or getattr(doc, "contact_no", None),
        },
        "customer": None,
        "predicted_next_order": None,
        "avg_order_cycle_days": None,
        "recent_invoices": [],
        "open_todos": [],
    }

    # Resolve a linked Customer (Opportunity.party_name when party is a Customer).
    customer = None
    if doctype == "Opportunity":
        customer = _resolve_opp_customer(getattr(doc, "party_name", None))
    result["customer"] = customer

    # Customer forecast fields + recent B2B invoices.
    if customer:
        if _has_field("Customer", "custom_predicted_next_order"):
            result["predicted_next_order"] = _str_or_none(
                frappe.db.get_value("Customer", customer, "custom_predicted_next_order")
            )
        if _has_field("Customer", "custom_avg_order_cycle_days"):
            result["avg_order_cycle_days"] = frappe.db.get_value(
                "Customer", customer, "custom_avg_order_cycle_days"
            )
        result["recent_invoices"] = _recent_b2b_invoices(customer)

    # Open ToDos referencing this record.
    result["open_todos"] = _open_todos_for(doctype, name)

    return result


def _str_or_none(value):
    return str(value) if value else None


def _recent_b2b_invoices(customer, limit=10):
    """Recent submitted B2B Sales Invoices for a customer. Never raises."""
    try:
        if not _doctype_exists("Sales Invoice"):
            return []
        fields = ["name", "posting_date", "grand_total", "status"]
        if _has_field("Sales Invoice", "custom_order_purpose"):
            fields.append("custom_order_purpose")
        rows = frappe.get_all(
            "Sales Invoice",
            filters={"customer": customer, "docstatus": 1},
            fields=fields,
            order_by="posting_date desc",
            limit_page_length=limit,
        )
        out = []
        for r in rows:
            purpose = r.get("custom_order_purpose") or "Standard"
            # Keep only B2B-ish invoices when the field exists; if it doesn't,
            # include all (best-effort).
            if "custom_order_purpose" in fields and purpose in ("", "Standard"):
                continue
            out.append(
                {
                    "name": r.get("name"),
                    "posting_date": _str_or_none(r.get("posting_date")),
                    "grand_total": r.get("grand_total"),
                    "custom_order_purpose": purpose,
                    "status": r.get("status"),
                }
            )
        return out
    except Exception:
        return []


def _open_todos_for(reference_type, reference_name):
    """Open ToDos referencing a record. Never raises."""
    try:
        if not _doctype_exists("ToDo"):
            return []
        rows = frappe.get_all(
            "ToDo",
            filters={
                "reference_type": reference_type,
                "reference_name": reference_name,
                "status": "Open",
            },
            fields=["name", "description", "date"],
            order_by="date asc",
            limit_page_length=0,
        )
        return [
            {
                "name": r.get("name"),
                "description": r.get("description"),
                "date": _str_or_none(r.get("date")),
            }
            for r in rows
        ]
    except Exception:
        return []


# ---------------------------------------------------------------------------
# Stage advancement
# ---------------------------------------------------------------------------
@frappe.whitelist()
def advance_stage(doctype, name, stage, reason=None):
    """Set ``custom_b2b_stage`` on a Lead/Opportunity (manual advancement).

    On move to "Lost/On-hold", also schedules a re-engage follow-up ToDo and (if
    the field exists) stamps custom_next_followup_date a week out.

    Returns: {"doctype", "name", "stage"}.
    """
    _ensure_b2b_access()

    if doctype not in ("Lead", "Opportunity"):
        frappe.throw("doctype must be 'Lead' or 'Opportunity'.")
    if not _doctype_exists(doctype) or not frappe.db.exists(doctype, name):
        frappe.throw(f"{doctype} '{name}' not found.")
    if not _has_field(doctype, "custom_b2b_stage"):
        frappe.throw(f"{doctype} has no custom_b2b_stage field.")

    stage = (stage or "").strip()
    if stage not in _stage_options(doctype):
        frappe.throw(f"Invalid stage '{stage}'.")

    frappe.db.set_value(
        doctype, name, "custom_b2b_stage", stage, update_modified=True
    )

    if stage == _LOST_STAGE:
        _schedule_reengage(doctype, name, reason)

    return {"doctype": doctype, "name": name, "stage": stage}


def _schedule_reengage(doctype, name, reason):
    """On Lost/On-hold: set re-engage ToDo + custom_next_followup_date. Guarded."""
    try:
        from jarz_pos.crm.follow_ups import _ensure_todo

        followup_date = None
        try:
            from frappe.utils import add_days, today

            followup_date = add_days(today(), 14)
        except Exception:
            followup_date = None

        owner = frappe.db.get_value(doctype, name, "owner") or frappe.session.user
        desc = f"Re-engage {doctype.lower()} {name}"
        if reason:
            desc += f" (reason: {reason})"
        _ensure_todo(doctype, name, owner, desc, date=followup_date)

        if followup_date and _has_field(doctype, "custom_next_followup_date"):
            frappe.db.set_value(
                doctype,
                name,
                "custom_next_followup_date",
                followup_date,
                update_modified=False,
            )
    except Exception:
        # Advancement already happened; a follow-up hiccup must not fail the call.
        pass


# ---------------------------------------------------------------------------
# Lead creation
# ---------------------------------------------------------------------------
@frappe.whitelist()
def create_lead(
    lead_name,
    company_name=None,
    mobile_no=None,
    email_id=None,
    source=None,
    territory=None,
):
    """Create a native Lead at stage "Lead", assigned to the calling user.

    Returns: {"name": <lead name>}.
    """
    _ensure_b2b_access()

    if not (lead_name or "").strip():
        frappe.throw("lead_name is required.")

    payload = {"doctype": "Lead", "lead_name": lead_name.strip()}
    if company_name and _has_field("Lead", "company_name"):
        payload["company_name"] = company_name
    if mobile_no and _has_field("Lead", "mobile_no"):
        payload["mobile_no"] = mobile_no
    if email_id and _has_field("Lead", "email_id"):
        payload["email_id"] = email_id
    if source and _has_field("Lead", "source") and frappe.db.exists("Lead Source", source):
        payload["source"] = source
    if territory and _has_field("Lead", "territory") and frappe.db.exists(
        "Territory", territory
    ):
        payload["territory"] = territory
    if _has_field("Lead", "custom_b2b_stage"):
        payload["custom_b2b_stage"] = "Lead"

    doc = frappe.get_doc(payload)
    doc.insert(ignore_permissions=True)

    # Assign to the calling user via ToDo (standard Frappe assignment).
    try:
        from frappe.desk.form.assign_to import add as _assign_add

        _assign_add(
            {
                "assign_to": [frappe.session.user],
                "doctype": "Lead",
                "name": doc.name,
            }
        )
    except Exception:
        pass

    return {"name": doc.name}


@frappe.whitelist()
def get_lead_sources():
    """Return all Lead Source names (alphabetical) for the lead-source dropdown.

    Frozen contract: ``jarz_pos.api.crm.get_lead_sources`` -> ``[str, ...]``.
    Guarded so a site without the standard ``Lead Source`` DocType returns ``[]``
    instead of raising.
    """
    _ensure_b2b_access()

    if not _doctype_exists("Lead Source"):
        return []

    try:
        names = frappe.get_all("Lead Source", pluck="name")
    except Exception:
        return []

    return sorted(names)


# ---------------------------------------------------------------------------
# Activity logging
# ---------------------------------------------------------------------------
@frappe.whitelist()
def log_activity(doctype, name, note):
    """Append a timeline Comment on a Lead/Opportunity/Customer record.

    Returns: {"success": True}.
    """
    _ensure_b2b_access()

    if doctype not in ("Lead", "Opportunity", "Customer"):
        frappe.throw("doctype must be 'Lead', 'Opportunity' or 'Customer'.")
    if not (note or "").strip():
        frappe.throw("note is required.")
    if not _doctype_exists(doctype) or not frappe.db.exists(doctype, name):
        frappe.throw(f"{doctype} '{name}' not found.")

    try:
        doc = frappe.get_doc(doctype, name)
        doc.add_comment("Comment", note.strip())
    except Exception:
        frappe.throw("Could not log activity.")

    return {"success": True}


# ---------------------------------------------------------------------------
# Today / follow-up feed
# ---------------------------------------------------------------------------
@frappe.whitelist()
def get_my_followups():
    """Open ToDos for the current user (Lead/Opportunity/Customer) plus reorder-due
    customers, sorted by date.

    Shape:
        {
            "todos": [ {"name","reference_type","reference_name","description","date"} ],
            "reorder_due": [ <reorder card> ]   # see get_reorder_due
        }
    """
    _ensure_b2b_access()

    todos = []
    try:
        if _doctype_exists("ToDo"):
            rows = frappe.get_all(
                "ToDo",
                filters={
                    "allocated_to": frappe.session.user,
                    "status": "Open",
                    "reference_type": ["in", ["Lead", "Opportunity", "Customer"]],
                },
                fields=["name", "reference_type", "reference_name", "description", "date"],
                order_by="date asc",
                limit_page_length=0,
            )
            todos = [
                {
                    "name": r.get("name"),
                    "reference_type": r.get("reference_type"),
                    "reference_name": r.get("reference_name"),
                    "description": r.get("description"),
                    "date": _str_or_none(r.get("date")),
                }
                for r in rows
            ]
    except Exception:
        todos = []

    return {"todos": todos, "reorder_due": get_reorder_due()}


@frappe.whitelist()
def get_reorder_due():
    """Company customers predicted to be due to reorder (predicted_next <= today).

    Returns a list of:
        {"name","customer_name","last_order_date","avg_basket_value","predicted_next_order"}
    """
    _ensure_b2b_access()

    if not _doctype_exists("Customer"):
        return []
    if not _has_field("Customer", "custom_predicted_next_order"):
        return []

    today = _today()
    if not today:
        return []

    fields = ["name", "customer_name", "custom_predicted_next_order"]
    for f in ("custom_last_order_date", "custom_avg_basket_value"):
        if _has_field("Customer", f):
            fields.append(f)

    filters = {"custom_predicted_next_order": ["<=", today]}
    if _has_field("Customer", "customer_type"):
        filters["customer_type"] = "Company"

    try:
        rows = frappe.get_all(
            "Customer",
            filters=filters,
            fields=fields,
            order_by="custom_predicted_next_order asc",
            limit_page_length=0,
        )
    except Exception:
        return []

    return [
        {
            "name": r.get("name"),
            "customer_name": r.get("customer_name"),
            "last_order_date": _str_or_none(r.get("custom_last_order_date")),
            "avg_basket_value": r.get("custom_avg_basket_value"),
            "predicted_next_order": _str_or_none(r.get("custom_predicted_next_order")),
        }
        for r in rows
    ]


# ---------------------------------------------------------------------------
# Sample / order binding helpers (thin — DO NOT create invoices here)
# ---------------------------------------------------------------------------
@frappe.whitelist()
def request_sample(
    party_doctype,
    party_name,
    customer_name=None,
    mobile_no=None,
    customer_primary_address=None,
    territory_id=None,
    customer_group=None,
):
    """Resolve/create the Company Customer for a card and return the SAMPLE binding.

    The app then calls the existing POS invoice endpoint with the returned
    customer + order_purpose + price_list. No invoice is created here.

    Returns:
        {"customer", "order_purpose", "price_list"}
    """
    _ensure_b2b_access()
    return _resolve_order_binding(
        party_doctype,
        party_name,
        _SAMPLE_ORDER_PURPOSE,
        customer_name=customer_name,
        mobile_no=mobile_no,
        customer_primary_address=customer_primary_address,
        territory_id=territory_id,
        customer_group=customer_group,
    )


@frappe.whitelist()
def place_b2b_order(
    party_doctype,
    party_name,
    customer_name=None,
    mobile_no=None,
    customer_primary_address=None,
    territory_id=None,
    customer_group=None,
):
    """Resolve/create the Company Customer for a card and return the B2B order binding.

    Returns:
        {"customer", "order_purpose", "price_list"}
    """
    _ensure_b2b_access()
    return _resolve_order_binding(
        party_doctype,
        party_name,
        _B2B_ORDER_PURPOSE,
        customer_name=customer_name,
        mobile_no=mobile_no,
        customer_primary_address=customer_primary_address,
        territory_id=territory_id,
        customer_group=customer_group,
    )


def _resolve_order_binding(
    party_doctype,
    party_name,
    order_purpose,
    customer_name=None,
    mobile_no=None,
    customer_primary_address=None,
    territory_id=None,
    customer_group=None,
):
    """Resolve the linked Customer (creating a Company customer if needed) and the
    commercial-policy price list for the given order purpose."""
    if party_doctype not in ("Lead", "Opportunity", "Customer"):
        frappe.throw("party_doctype must be 'Lead', 'Opportunity' or 'Customer'.")

    customer = None

    if party_doctype == "Customer":
        if not frappe.db.exists("Customer", party_name):
            frappe.throw(f"Customer '{party_name}' not found.")
        customer = party_name
    elif party_doctype == "Opportunity":
        customer = _resolve_opp_customer(
            frappe.db.get_value("Opportunity", party_name, "party_name")
            if frappe.db.exists("Opportunity", party_name)
            else None
        )

    # No linked Customer yet -> create a Company customer from supplied details.
    if not customer:
        if not (customer_name and mobile_no and customer_primary_address and territory_id):
            frappe.throw(
                "No linked Customer; supply customer_name, mobile_no, "
                "customer_primary_address and territory_id to create one."
            )
        from jarz_pos.api.customer import create_customer

        created = create_customer(
            customer_name=customer_name,
            mobile_no=mobile_no,
            customer_primary_address=customer_primary_address,
            territory_id=territory_id,
            customer_type="Company",
            customer_group=customer_group,
        )
        customer = (
            created.get("name")
            if isinstance(created, dict)
            else getattr(created, "name", None)
        )
        if not customer:
            frappe.throw("Failed to create Customer for B2B order.")

    return {
        "customer": customer,
        "order_purpose": order_purpose,
        "price_list": _policy_price_list(order_purpose),
    }


def _policy_price_list(order_purpose):
    """Best-effort lookup of the price list bound to a commercial policy. None-safe."""
    try:
        if not _doctype_exists("Jarz Commercial Policy"):
            return None
        rows = frappe.get_all(
            "Jarz Commercial Policy",
            filters={"enabled": 1, "order_purpose": order_purpose},
            fields=["price_list"],
            order_by="priority asc, creation asc",
            limit_page_length=1,
        )
        if rows:
            return (rows[0].get("price_list") or "").strip() or None
    except Exception:
        pass
    return None
