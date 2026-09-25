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

from jarz_pos.utils.invoice_utils import normalize_woo_order_id

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

# Stages a record lives in BEFORE a sample is requested.
#
# This used to gate which Leads reached the board, on the assumption that
# post-sample work continues on an Opportunity. Nothing in this app ever
# converts a Lead into an Opportunity, so that assumption was false: advancing
# a Lead to Sample (or Approved, Trial, Check-up, Active) simply deleted it
# from the board with nothing taking its place, while the stage editor happily
# offered all eight stages. Kept only for callers that genuinely mean
# "pre-sample"; get_b2b_pipeline no longer filters on it.
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


def _require_doc_permission(doctype, name=None, ptype="read"):
    """Apply normal Frappe document permissions after the B2B product gate."""
    if not frappe.has_permission(doctype, ptype=ptype, doc=name):
        frappe.throw(
            f"Not permitted: {ptype} access to {doctype} is required.",
            frappe.PermissionError,
        )


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


def _truthy(value):
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _can_manage_b2b_relationships():
    roles = set(frappe.get_roles(frappe.session.user) or [])
    return bool(roles.intersection(_manager_roles()))


def _customer_rows_for_lead(lead_name):
    """Customers created through ERPNext's standard Lead conversion link."""
    return frappe.get_all(
        "Customer",
        filters={"lead_name": lead_name},
        fields=["name", "disabled"],
        order_by="creation asc",
        limit_page_length=3,
    ) or []


def _resolve_lead_customer(lead_name, *, strict=True):
    """Resolve both standard Lead->Customer relationship directions.

    ``Lead.customer`` is ERPNext's "From Customer" link for an existing account;
    ``Customer.lead_name`` is written by the standard Lead conversion.  If legacy
    data contains two different answers, order taking must stop instead of choosing
    one and putting an invoice on the wrong account.
    """
    direct = frappe.db.get_value("Lead", lead_name, "customer") or None
    converted_rows = _customer_rows_for_lead(lead_name)
    converted_names = [str(row.get("name") or "").strip() for row in converted_rows]
    converted_names = [name for name in converted_names if name]

    if len(set(converted_names)) > 1:
        if strict:
            frappe.throw(
                "Lead relationship conflict: more than one Customer refers to this Lead. "
                "Ask a manager to repair the Customer.lead_name records before ordering."
            )
        return None

    converted = converted_names[0] if converted_names else None
    if direct and converted and direct != converted:
        if strict:
            frappe.throw(
                "Lead relationship conflict: the Lead and converted Customer point to "
                "different accounts. Ask a manager to repair the relationship before ordering."
            )
        return None
    return direct or converted


def _resolve_opportunity_customer(opportunity, *, strict=True):
    """Resolve an Opportunity without changing its historical party/origin."""
    if not opportunity:
        return None
    origin = str(opportunity.get("opportunity_from") or "").strip()
    party_name = str(opportunity.get("party_name") or "").strip()
    if origin == "Customer":
        return party_name if party_name and frappe.db.exists("Customer", party_name) else None
    if origin == "Lead":
        if not party_name or not frappe.db.exists("Lead", party_name):
            return None
        return _resolve_lead_customer(party_name, strict=strict)
    return None


def _assert_enabled_customer(customer):
    row = frappe.db.get_value(
        "Customer", customer, ["name", "disabled"], as_dict=True
    )
    if not row:
        frappe.throw(f"Customer '{customer}' not found.")
    if int(row.get("disabled") or 0):
        frappe.throw(
            f"Customer '{customer}' is disabled. Enable it before using it for a B2B order."
        )
    return customer


def _lead_customer_map(lead_rows):
    """Resolve a pipeline page in two queries, marking ambiguous links as None."""
    lead_names = [str(row.get("name") or "").strip() for row in lead_rows]
    lead_names = [name for name in lead_names if name]
    converted = {}
    if lead_names:
        rows = frappe.get_all(
            "Customer",
            filters={"lead_name": ["in", lead_names]},
            fields=["name", "lead_name"],
            limit_page_length=0,
        ) or []
        for row in rows:
            converted.setdefault(row.get("lead_name"), []).append(row.get("name"))

    result = {}
    for row in lead_rows:
        lead_name = row.get("name")
        direct = row.get("customer") or None
        converted_names = {name for name in converted.get(lead_name, []) if name}
        if len(converted_names) > 1:
            result[lead_name] = None
            continue
        converted_name = next(iter(converted_names), None)
        result[lead_name] = (
            None if direct and converted_name and direct != converted_name
            else direct or converted_name
        )
    return result


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
        for f in ("lead_name", "company_name", "custom_fit_score", "customer"):
            if _has_field("Lead", f):
                lead_fields.append(f)
        # Every stage, not just the pre-sample ones: a Lead is only ever a Lead
        # in this app, so whatever stage a rep sets is where its card belongs.
        lead_filters = {"custom_b2b_stage": ["is", "set"]}
        # Prospects a rep has manually judged unsuitable never belong on the
        # board. Guarded on the field so a pre-migrate site still returns cards.
        if _has_field("Lead", "custom_not_suitable"):
            lead_filters["custom_not_suitable"] = 0
        # Nor does a duplicate that has been merged into another lead — its
        # branches now live on the surviving card.
        if _has_field("Lead", "custom_merged_into"):
            lead_filters["custom_merged_into"] = ["is", "not set"]
        try:
            leads = frappe.get_all(
                "Lead",
                filters=lead_filters,
                fields=lead_fields,
                order_by=(
                    "custom_fit_score desc"
                    if _has_field("Lead", "custom_fit_score")
                    else None
                ),
                limit_page_length=0,
            )
        except Exception:
            leads = []
        lead_customer_map = _lead_customer_map(leads)
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
                # Output key stays ``lead_score`` (Flutter unchanged); the kanban
                # shows/sorts by the catalog fit score (custom_fit_score), not the
                # nightly CRM signal score (custom_lead_score).
                "lead_score": row.get("custom_fit_score"),
                "customer": lead_customer_map.get(row.get("name")),
                "last_activity": str(row.get("modified")) if row.get("modified") else None,
            }
            columns.setdefault(stage, []).append(card)

    # --- Opportunities (any B2B stage) ------------------------------------
    if _doctype_exists("Opportunity") and _has_field("Opportunity", "custom_b2b_stage"):
        opp_fields = ["name", "custom_b2b_stage", "owner", "modified"]
        for f in ("party_name", "customer_name", "opportunity_from"):
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
            linked_customer = _resolve_opportunity_customer(row, strict=False)
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

    # Order every column by lead score (highest first). Cards with no score —
    # all Opportunities and any Lead missing custom_fit_score — sort LAST.
    # Python's sort is stable, so equal scores keep their original query order.
    def _score_key(card):
        score = card.get("lead_score")
        return score if score is not None else -1

    for stage_cards in columns.values():
        stage_cards.sort(key=_score_key, reverse=True)

    # Fold in each card's journey diary summary (last touch + next action), so
    # the board itself shows when a prospect was last visited and what is due.
    _attach_journey_summaries(columns)

    # Stamp the printed-label warning on cards that resolve to a customer, so a
    # rep sees "this account cannot be packed" before promising a delivery.
    _attach_label_alerts(columns)

    return {"stages": stages, "columns": columns}


def _journey_card_defaults():
    """The journey keys every card carries, whether or not it has notes."""
    return {
        "journey_count": 0,
        "last_journey_date": None,
        "last_journey_type": None,
        "last_journey_note": None,
        "last_journey_contact": None,
        "next_action_date": None,
        "next_action": None,
    }


def _attach_journey_summaries(columns):
    """Merge the journey summary into every card, in ONE query per doctype.

    Best-effort: on a site that has not migrated the Jarz Journey Note DocType
    yet the cards simply carry the empty defaults. Imported lazily because
    ``jarz_pos.api.journey`` imports this module's access gate.
    """
    by_doctype = {}
    for stage_cards in columns.values():
        for card in stage_cards:
            card.update(_journey_card_defaults())
            by_doctype.setdefault(card.get("doctype"), []).append(card)

    try:
        from jarz_pos.api.journey import journey_summaries
    except Exception:
        frappe.log_error(
            title="crm.get_b2b_pipeline: journey import failed",
            message=frappe.get_traceback(),
        )
        return

    for doctype, cards in by_doctype.items():
        summaries = journey_summaries(doctype, [c.get("name") for c in cards])
        if not summaries:
            continue
        for card in cards:
            summary = summaries.get(card.get("name"))
            if summary:
                card.update(summary)


def _attach_label_alerts(columns):
    """Stamp ``label_alert`` (count of flavours needing printing) on each card.

    Only Opportunity cards can resolve to a Customer, and only customers with
    tracked labels get a non-zero count -- so the whole pass is two queries,
    not one per card. Best-effort: cards default to 0 and a failure changes
    nothing, because a wrong badge is worse than none.
    """
    with_customer = []
    for stage_cards in columns.values():
        for card in stage_cards:
            card.setdefault("label_alert", 0)
            if str(card.get("customer") or "").strip():
                with_customer.append(card)
    if not with_customer:
        return

    try:
        if not _doctype_exists("Jarz Customer Label"):
            return
        customers = sorted({str(c["customer"]).strip() for c in with_customer})
        rows = frappe.get_all(
            "Jarz Customer Label",
            filters={
                "customer": ["in", customers],
                "enabled": 1,
                "we_print": 1,
                # The cached status column: refreshed on every movement and by
                # the daily pass, which is fresh enough for a card badge. The
                # labels board itself always recomputes from the ledger.
                "status": ["in", ["Out of Stock", "Reorder Now", "Reorder Soon"]],
            },
            fields=["customer"],
            limit_page_length=0,
        )
        counts = {}
        for row in rows or []:
            counts[row["customer"]] = counts.get(row["customer"], 0) + 1
        if not counts:
            return
        for card in with_customer:
            card["label_alert"] = counts.get(str(card["customer"]).strip(), 0)
    except Exception:
        frappe.log_error(
            title="crm.get_b2b_pipeline: label alerts failed",
            message=frappe.get_traceback(),
        )


def _resolve_opp_customer(party_name):
    """Backward-compatible direct-Customer lookup used by older internal callers."""
    return _resolve_opportunity_customer(
        {"opportunity_from": "Customer", "party_name": party_name}, strict=False
    )


# ---------------------------------------------------------------------------
# Account detail
# ---------------------------------------------------------------------------
@frappe.whitelist()
def get_account(doctype, name):
    """Full detail for one B2B account (Lead, Opportunity, or Customer).

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
            "recent_invoices": [ {"name","woo_order_id","posting_date","grand_total","custom_order_purpose","status"} ],
            "open_todos": [ {"name","description","date"} ],
            "branch_lead": str | None,   # Lead whose Maps branches are paired in
            "branches": [ ... ],         # b2b_branches.unified_branches(); absent
            "unassigned_invoices": {...} | None,  # when neither customer nor lead
        }
    """
    _ensure_b2b_access()

    if doctype not in ("Lead", "Opportunity", "Customer"):
        frappe.throw("doctype must be 'Lead', 'Opportunity' or 'Customer'.")
    if not _doctype_exists(doctype) or not frappe.db.exists(doctype, name):
        frappe.throw(f"{doctype} '{name}' not found.")
    _require_doc_permission(doctype, name, "read")

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

    # Resolve both standard ERPNext Lead relationship directions. Opportunities
    # originating from a Lead follow that Lead without rewriting their history.
    if doctype == "Lead":
        customer = _resolve_lead_customer(name)
    elif doctype == "Opportunity":
        customer = _resolve_opportunity_customer(doc)
    else:
        customer = name
    if customer:
        _require_doc_permission("Customer", customer, "read")
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

    # One branch list: the customer's delivery Addresses paired with the
    # catalog Lead's Google Maps branches. A Lead with no customer yet still
    # shows its Maps doors.
    lead = _readable_branch_lead(doctype, name, doc, customer)
    result["branch_lead"] = lead
    if customer or lead:
        _attach_branches(result, customer, lead)

    # Open ToDos referencing this record.
    result["open_todos"] = _open_todos_for(doctype, name)

    # The rep's dated field diary for this account (newest touch first). Empty
    # on a site that has not migrated the journey DocType yet.
    result["journey_notes"] = _journey_notes(doctype, name)

    # Printed-label position, so a rep about to promise delivery can see the
    # labels are not there. Guarded -> None (never raises, absent pre-migrate).
    result["labels"] = _label_summary_for_customer(customer) if customer else None

    return result


def _branch_lead(doctype, name, doc=None, customer=None):
    """The catalog Lead whose Google Maps branches belong to this card, or None.

    Lead -> itself. Customer -> its one live Lead (two is an unresolved
    duplicate; neither is picked). Opportunity -> its Lead when it came from
    one, else its customer's one live Lead.
    """
    from jarz_pos.services import b2b_branches

    if doctype == "Lead":
        return name
    if doctype == "Customer":
        leads = b2b_branches._leads_for_customer(name)
        return leads[0] if len(leads) == 1 else None
    if doctype == "Opportunity":
        if doc is None:
            doc = frappe.get_doc("Opportunity", name)
        origin = str(doc.get("opportunity_from") or "").strip()
        party = str(doc.get("party_name") or "").strip()
        if origin == "Lead":
            return party if party and frappe.db.exists("Lead", party) else None
        if customer:
            leads = b2b_branches._leads_for_customer(customer)
            return leads[0] if len(leads) == 1 else None
    return None


def _readable_branch_lead(doctype, name, doc, customer):
    """``_branch_lead`` for the account screen: never raises, and drops a Lead
    the caller may not read (the screen then shows delivery branches only)."""
    try:
        lead = _branch_lead(doctype, name, doc, customer)
        if lead and lead != name and not frappe.has_permission("Lead", ptype="read", doc=lead):
            return None
        return lead
    except Exception:
        try:
            frappe.log_error(
                title=f"crm: branch lead lookup failed for {doctype} {name}",
                message=frappe.get_traceback(),
            )
        except Exception:
            pass
        return None


def _attach_branches(result, customer, lead=None):
    """Fold the account's branches -- each with its own invoices and balance,
    and paired with the Lead's Google Maps branch where one matches -- into an
    account payload, and label every recent invoice with its branch.
    Guarded: a failure here must never take the account screen down."""
    result["branches"] = []
    result["unassigned_invoices"] = None
    try:
        from jarz_pos.services import b2b_branches

        book = b2b_branches.unified_branches(customer, lead)
        result["branches"] = book["branches"]
        result["unassigned_invoices"] = book["unassigned"]
        index = b2b_branches._member_index(book["branches"])
        for invoice in result.get("recent_invoices") or []:
            address = frappe.db.get_value(
                "Sales Invoice",
                invoice.get("name"),
                ["shipping_address_name", "customer_address"],
                as_dict=True,
            ) or {}
            branch = b2b_branches._branch_for_invoice(address, index)
            invoice["branch_address"] = branch["address_name"] if branch else None
            invoice["branch_name"] = branch["branch_name"] if branch else None
    except Exception:
        frappe.log_error(
            title=f"crm: branch summary failed for {customer}",
            message=frappe.get_traceback(),
        )


def _account_customer(doctype, name):
    """The Customer behind a B2B card, after the normal access checks."""
    if doctype not in ("Lead", "Opportunity", "Customer"):
        frappe.throw("doctype must be 'Lead', 'Opportunity' or 'Customer'.")
    if not _doctype_exists(doctype) or not frappe.db.exists(doctype, name):
        frappe.throw(f"{doctype} '{name}' not found.")
    _require_doc_permission(doctype, name, "read")
    if doctype == "Lead":
        customer = _resolve_lead_customer(name)
    elif doctype == "Opportunity":
        customer = _resolve_opportunity_customer(frappe.get_doc("Opportunity", name))
    else:
        customer = name
    if not customer:
        frappe.throw("This account has no customer yet, so it has no invoices.")
    _require_doc_permission("Customer", customer, "read")
    return customer


@frappe.whitelist()
def get_account_invoices(doctype, name, branch=None, limit=100):
    """Every submitted invoice of a B2B account, optionally for ONE branch.

    ``branch`` is a branch's ``address_name`` from ``get_account().branches``,
    or ``"__unassigned__"`` for invoices that match no branch. ``summary`` is
    the branch's whole history, not just the page listed.
    """
    _ensure_b2b_access()
    customer = _account_customer(doctype, name)
    try:
        limit = max(1, min(int(limit or 100), 500))
    except (TypeError, ValueError):
        limit = 100
    from jarz_pos.services import b2b_branches

    return b2b_branches.account_invoices(customer, branch=branch, limit=limit)


@frappe.whitelist(methods=["POST"])
def link_branch(doctype, name, maps_row, address_name=None):
    """Pair one Google Maps branch of the account's Lead with a delivery Address,
    or (``address_name`` empty) unpair it for good.

    ``maps_row`` is ``branches[].maps.row`` from ``get_account``: a Jarz Lead
    Branch row name, or ``"__self__"`` for a branch-less Lead's own location
    (which is then saved as a real branch row so the pairing has a home).
    Linking clears any other row of the Lead paired with the same door;
    unlinking sets ``match_dismissed`` so auto-matching never pairs it again.
    Returns ``unified_branches(customer, lead)``: the refreshed branch list.
    """
    from jarz_pos.services import b2b_branches

    _ensure_b2b_access()
    if doctype not in ("Lead", "Opportunity", "Customer"):
        frappe.throw("doctype must be 'Lead', 'Opportunity' or 'Customer'.")
    if not _doctype_exists(doctype) or not frappe.db.exists(doctype, name):
        frappe.throw(f"{doctype} '{name}' not found.")
    _require_doc_permission(doctype, name, "read")

    doc = frappe.get_doc(doctype, name) if doctype == "Opportunity" else None
    if doctype == "Lead":
        customer = _resolve_lead_customer(name, strict=False)
    elif doctype == "Opportunity":
        customer = _resolve_opportunity_customer(doc, strict=False)
    else:
        customer = name
    lead = _branch_lead(doctype, name, doc, customer)
    if not lead:
        frappe.throw(
            "This account has no single catalog lead, so it has no Google Maps "
            "branches to link."
        )
    _require_doc_permission("Lead", lead, "write")
    if customer:
        _require_doc_permission("Customer", customer, "read")

    if not (
        b2b_branches._has_column("Jarz Lead Branch", "linked_address")
        and b2b_branches._has_column("Jarz Lead Branch", "match_dismissed")
    ):
        frappe.throw(
            "This site has not migrated the branch link fields yet. "
            "Run `bench migrate` and try again."
        )

    maps_row = str(maps_row or "").strip()
    address_name = str(address_name or "").strip() or None
    if not maps_row:
        frappe.throw("Pick the Google Maps branch to link.")

    # Validate the address BEFORE anything is written (a virtual self row is
    # materialized below, and must not be left behind by a refused request).
    if address_name:
        if not customer:
            frappe.throw(
                "This account has no customer yet, so it has no delivery branches to link to."
            )
        # Only an address in the delivery-branch book: a billing-only address
        # would be stored but never matched, so the link would silently fail.
        branch_index = b2b_branches._member_index(b2b_branches.customer_branches(customer))
        if address_name not in branch_index:
            frappe.throw("That address is not one of this customer's delivery branches.")

    if maps_row == b2b_branches.SELF_BRANCH_ROW:
        row_name = _materialize_self_branch(lead)
    else:
        row = frappe.db.get_value(
            "Jarz Lead Branch", maps_row, ["parenttype", "parent", "parentfield"], as_dict=True
        )
        if not row or (
            row.get("parenttype"),
            row.get("parent"),
            row.get("parentfield"),
        ) != ("Lead", lead, "custom_branches"):
            frappe.throw("That Google Maps branch does not belong to this account.")
        row_name = maps_row

    if address_name:
        branch = branch_index.get(address_name)
        members = list((branch or {}).get("member_address_names") or []) or [address_name]
        others = frappe.get_all(
            "Jarz Lead Branch",
            filters={
                "parenttype": "Lead",
                "parent": lead,
                "parentfield": "custom_branches",
                "linked_address": ["in", members],
                "name": ["!=", row_name],
            },
            pluck="name",
            limit_page_length=0,
        ) or []
        for other in others:
            frappe.db.set_value(
                "Jarz Lead Branch", other, "linked_address", None, update_modified=False
            )
        frappe.db.set_value(
            "Jarz Lead Branch",
            row_name,
            {"linked_address": address_name, "match_dismissed": 0},
            update_modified=False,
        )
    else:
        frappe.db.set_value(
            "Jarz Lead Branch",
            row_name,
            {"linked_address": None, "match_dismissed": 1},
            update_modified=False,
        )

    return b2b_branches.unified_branches(customer, lead)


def _materialize_self_branch(lead):
    """Save a branch-less Lead's own location as its first branch row."""
    from jarz_pos.api.leads import _lead_self_branch

    lead_doc = frappe.get_doc("Lead", lead)
    if lead_doc.get("custom_branches"):
        frappe.throw(
            "This lead already has Google Maps branches. Refresh the account and pick one."
        )
    own = _lead_self_branch(lead_doc)
    if not own:
        frappe.throw("This lead has no location to link.")
    row = lead_doc.append("custom_branches", own)
    lead_doc.flags.ignore_permissions = True
    lead_doc.save(ignore_permissions=True)
    return row.name


# ---------------------------------------------------------------------------
# Merge one account into another as a branch
# ---------------------------------------------------------------------------
@frappe.whitelist()
def search_merge_targets(doctype, name, query=None, limit=20):
    """Accounts the given card can be merged INTO as a branch.

    Returns live Leads (never a merged-away one) and Customers, dropping the
    card itself and anything that already resolves to the same account. A
    Customer that a returned Lead already stands for is not listed twice.
    """
    _ensure_b2b_access()
    from jarz_pos.services import b2b_branches

    if doctype not in ("Lead", "Customer") or not frappe.db.exists(doctype, name):
        frappe.throw("Only a Lead or a Customer account can be merged.")
    _require_doc_permission(doctype, name, "read")
    source = b2b_branches.resolve_party(doctype, name)
    query = str(query or "").strip()
    try:
        limit = max(1, min(int(limit or 20), 50))
    except (TypeError, ValueError):
        limit = 20

    candidates = []
    lead_filters = {"name": ["!=", source.get("lead") or ""]}
    if _has_field("Lead", "custom_merged_into"):
        lead_filters["custom_merged_into"] = ["is", "not set"]
    lead_or = None
    if query:
        lead_or = {
            "lead_name": ["like", f"%{query}%"],
            "company_name": ["like", f"%{query}%"],
            "mobile_no": ["like", f"%{query}%"],
            "name": ["like", f"%{query}%"],
        }
    lead_fields = ["name", "lead_name", "company_name", "mobile_no", "customer"]
    for optional in ("custom_b2b_stage", "custom_primary_area", "custom_branch_count"):
        if _has_field("Lead", optional):
            lead_fields.append(optional)
    leads = frappe.get_list(
        "Lead",
        filters=lead_filters,
        or_filters=lead_or,
        fields=lead_fields,
        order_by="modified desc",
        limit_page_length=limit,
    ) or []
    customer_map = _lead_customer_map(leads)
    seen_customers = set()
    for row in leads:
        customer = customer_map.get(row.get("name"))
        if source.get("customer") and customer == source.get("customer"):
            continue
        if customer:
            seen_customers.add(customer)
        candidates.append(
            {
                "doctype": "Lead",
                "name": row.get("name"),
                "title": row.get("lead_name") or row.get("company_name") or row.get("name"),
                "customer": customer,
                "stage": row.get("custom_b2b_stage"),
                "area": row.get("custom_primary_area"),
                "mobile_no": row.get("mobile_no"),
                "branch_count": int(row.get("custom_branch_count") or 0),
            }
        )

    if query:
        customers = frappe.get_list(
            "Customer",
            filters={"disabled": 0, "name": ["!=", source.get("customer") or ""]},
            or_filters={
                "name": ["like", f"%{query}%"],
                "customer_name": ["like", f"%{query}%"],
                "mobile_no": ["like", f"%{query}%"],
            },
            fields=["name", "customer_name", "mobile_no", "territory"],
            order_by="customer_name asc",
            limit_page_length=limit,
        ) or []
        for row in customers:
            if row.get("name") in seen_customers:
                continue
            candidates.append(
                {
                    "doctype": "Customer",
                    "name": row.get("name"),
                    "title": row.get("customer_name") or row.get("name"),
                    "customer": row.get("name"),
                    "stage": None,
                    "area": row.get("territory"),
                    "mobile_no": row.get("mobile_no"),
                    "branch_count": 0,
                }
            )
    return {"candidates": candidates}


def _merge_parties(source_doctype, source_name, target_doctype, target_name):
    from jarz_pos.services import b2b_branches

    for doctype, name in ((source_doctype, source_name), (target_doctype, target_name)):
        if doctype in ("Lead", "Customer") and frappe.db.exists(doctype, name):
            _require_doc_permission(doctype, name, "read")
    source = b2b_branches.resolve_party(source_doctype, source_name)
    target = b2b_branches.resolve_party(target_doctype, target_name)
    plan = b2b_branches.build_plan(source, target)
    # Linking a Lead's addresses onto a Customer rewrites that Customer's
    # address book, so it needs the same right as editing the Customer.
    if plan["customer_action"] == "link_lead_addresses":
        _require_doc_permission("Customer", target["customer"], "write")
    for party in (source, target):
        _require_doc_permission(party["doctype"], party["name"], "write")
        if party.get("lead") and party["lead"] != party["name"]:
            _require_doc_permission("Lead", party["lead"], "write")
        # Read, not write: the only step that rewrites a Customer (merging two
        # of them) is refused to anyone but a manager inside execute().
        if party.get("customer"):
            _require_doc_permission("Customer", party["customer"], "read")
    return source, target


@frappe.whitelist()
def preview_merge_as_branch(source_doctype, source_name, target_doctype, target_name):
    """What merging SOURCE into TARGET as a branch will do. Changes nothing."""
    _ensure_b2b_access()
    from jarz_pos.services import b2b_branches

    source, target = _merge_parties(source_doctype, source_name, target_doctype, target_name)
    return b2b_branches.preview(source, target)


@frappe.whitelist(methods=["POST"])
def merge_as_branch(source_doctype, source_name, target_doctype, target_name, branch_name=None):
    """Fold SOURCE into TARGET, keeping SOURCE's door as a branch of TARGET.

    Two customer accounts are merged with every invoice, payment and address
    moving to the target (manager only -- it cannot be undone); two catalog
    Leads are folded by ``leads.merge_leads``. See ``services.b2b_branches``.
    Returns the surviving card for the app to open next.
    """
    _ensure_b2b_access()
    from jarz_pos.services import b2b_branches

    source, target = _merge_parties(source_doctype, source_name, target_doctype, target_name)
    result = b2b_branches.execute(source, target, branch_name=branch_name)
    result.update(
        success=True,
        target_doctype=target["doctype"],
        target_name=target["name"],
    )
    return result


def _label_summary_for_customer(customer):
    """Compact label position for one customer, or None when unavailable.

    Small on purpose: the account screen needs "is this customer packable",
    not the whole board -- that lives one tap away on the labels screen.
    """
    try:
        if not _doctype_exists("Jarz Customer Label"):
            return None
        from jarz_pos.services import label_stock

        snapshots = label_stock.list_label_snapshots(customer=customer)
        if not snapshots:
            return None
        summary = label_stock.summarise(snapshots)
        return {
            "total": summary["total"],
            "needs_attention": summary["needs_attention"],
            "out_of_stock": summary["out_of_stock"],
            "reorder_now": summary["reorder_now"],
            "flavours": [
                {
                    "label": s["name"],
                    "title": s["label_title"],
                    "size": s.get("size"),
                    "on_hand_qty": s["on_hand_qty"],
                    "status": s["status"],
                }
                for s in snapshots
            ],
        }
    except Exception:
        frappe.log_error(frappe.get_traceback(), f"crm: label summary failed for {customer}")
        return None


def _journey_notes(doctype, name, limit=100):
    """Journey diary entries for a record. Guarded -> [] (never raises).

    Lazily imported: ``jarz_pos.api.journey`` imports this module's access gate,
    so a module-level import here would be circular.
    """
    try:
        from jarz_pos.api.journey import journey_notes_for

        return journey_notes_for(doctype, name, limit=limit)
    except Exception:
        frappe.log_error(
            title="crm: journey notes lookup failed",
            message=frappe.get_traceback(),
        )
        return []


def _str_or_none(value):
    return str(value) if value else None


def _recent_b2b_invoices(customer, limit=10):
    """Recent submitted B2B Sales Invoices for a customer. Never raises."""
    try:
        if not _doctype_exists("Sales Invoice"):
            return []
        fields = ["name", "posting_date", "grand_total", "status", "woo_order_id"]
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
                    "woo_order_id": normalize_woo_order_id(r.get("woo_order_id")),
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
def advance_stage(doctype, name, stage, reason=None, follow_up_date=None):
    """Set ``custom_b2b_stage`` on a Lead/Opportunity (manual advancement).

    Follow-up date handling:
      - When ``follow_up_date`` (ISO ``yyyy-mm-dd``) is supplied, stamp it on
        ``custom_next_followup_date`` and reset ``custom_followup_done = 0`` so the
        reminder passes pick the record up again. This works for any stage.
      - On move to "Lost/On-hold" WITHOUT an explicit date, keep the legacy
        behaviour: schedule a re-engage ToDo + stamp custom_next_followup_date 14
        days out. An explicit date on Lost/On-hold overrides that +14 default.

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

    follow_up_date = (follow_up_date or "").strip() or None

    frappe.db.set_value(
        doctype, name, "custom_b2b_stage", stage, update_modified=True
    )

    # Deliberately putting a Lead back into a live stage overrides an earlier
    # "not suitable" verdict — otherwise the lead would sit in a working stage
    # while still hidden from the catalog and the board. Guarded on the field.
    if (
        doctype == "Lead"
        and stage != _LOST_STAGE
        and _has_field("Lead", "custom_not_suitable")
    ):
        _clear_not_suitable(name)

    if follow_up_date:
        # Explicit date wins on every stage (including Lost/On-hold): stamp it and
        # re-open the follow-up loop so the daily reminders re-surface it.
        _stamp_followup_date(doctype, name, follow_up_date)
    elif stage == _LOST_STAGE:
        _schedule_reengage(doctype, name, reason)

    return {"doctype": doctype, "name": name, "stage": stage}


def _clear_not_suitable(name):
    """Wipe the not-suitable verdict on a Lead. No-op when it is not set."""
    try:
        if not frappe.db.get_value("Lead", name, "custom_not_suitable"):
            return
        frappe.db.set_value(
            "Lead",
            name,
            {
                "custom_not_suitable": 0,
                "custom_not_suitable_reason": None,
                "custom_not_suitable_notes": None,
                "custom_not_suitable_on": None,
                "custom_not_suitable_by": None,
            },
            update_modified=True,
        )
    except Exception:
        frappe.log_error(
            title="crm.advance_stage: clear not-suitable failed",
            message=frappe.get_traceback(),
        )


def _stamp_followup_date(doctype, name, follow_up_date):
    """Stamp custom_next_followup_date + reset custom_followup_done=0. Guarded."""
    try:
        if _has_field(doctype, "custom_next_followup_date"):
            frappe.db.set_value(
                doctype,
                name,
                "custom_next_followup_date",
                follow_up_date,
                update_modified=False,
            )
        if _has_field(doctype, "custom_followup_done"):
            frappe.db.set_value(
                doctype, name, "custom_followup_done", 0, update_modified=False
            )
    except Exception:
        # Stage advancement already happened; a follow-up hiccup must not fail it.
        pass


def _schedule_reengage(doctype, name, reason):
    """On Lost/On-hold: set re-engage ToDo + custom_next_followup_date. Guarded."""
    try:
        from jarz_pos.crm.follow_ups import KIND_REENGAGE, _ensure_todo

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
        _ensure_todo(
            doctype, name, owner, desc, date=followup_date, kind=KIND_REENGAGE
        )

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


@frappe.whitelist()
def complete_followup(doctype, name):
    """Mark a follow-up complete — the write path that STOPS daily regeneration.

    Sets ``custom_followup_done = 1``, clears ``custom_next_followup_date``, and closes
    any open ToDo referencing the record. Because the reminder passes only ever pick up
    records with ``custom_followup_done == 0`` (and re-open ToDos are deduped), completing
    the follow-up here is what breaks the loop where reminders were re-created every day.

    Access: a manager, or the B2B rep who owns / is assigned the record.

    Returns: {"ok": True}.
    """
    _ensure_b2b_access()

    if doctype not in ("Lead", "Opportunity"):
        frappe.throw("doctype must be 'Lead' or 'Opportunity'.")
    if not _doctype_exists(doctype) or not frappe.db.exists(doctype, name):
        frappe.throw(f"{doctype} '{name}' not found.")
    if not _can_complete_followup(doctype, name):
        frappe.throw("Not permitted: only a manager or the assigned rep may complete this follow-up.")

    if _has_field(doctype, "custom_followup_done"):
        frappe.db.set_value(
            doctype, name, "custom_followup_done", 1, update_modified=False
        )
    if _has_field(doctype, "custom_next_followup_date"):
        frappe.db.set_value(
            doctype, name, "custom_next_followup_date", None, update_modified=False
        )

    _close_open_todos(doctype, name)

    return {"ok": True}


def _can_complete_followup(doctype, name):
    """Managers may always complete; a B2B rep only for records they own/are assigned."""
    roles = set(frappe.get_roles(frappe.session.user) or [])
    if roles.intersection(_manager_roles()):
        return True
    if "B2B Sales Rep" not in roles:
        return False
    user = frappe.session.user
    try:
        if frappe.db.get_value(doctype, name, "owner") == user:
            return True
    except Exception:
        pass
    # Fall back to a standard Frappe assignment (open ToDo allocated to the caller).
    try:
        if _doctype_exists("ToDo") and frappe.db.exists(
            "ToDo",
            {
                "reference_type": doctype,
                "reference_name": name,
                "allocated_to": user,
                "status": "Open",
            },
        ):
            return True
    except Exception:
        pass
    return False


def _close_open_todos(reference_type, reference_name):
    """Close the reminders THIS APP opened on the record. Never raises.

    Deliberately NOT every open ToDo. On a Lead the Assignment Rule keeps its
    own open ToDo, and closing that as a side effect of "I've done this
    follow-up" quietly un-assigned the rep from their own lead. The helper
    matches only our ``[jarz:`` reminders.
    """
    try:
        from jarz_pos.crm.follow_ups import close_all_jarz_todos

        close_all_jarz_todos(reference_type, reference_name)
    except Exception:
        frappe.log_error(
            title="crm: closing follow-up ToDos failed",
            message=frappe.get_traceback(),
        )


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
    # This site has no ``Lead Source`` DocType; stamp the Select custom field
    # instead, but only when the passed value is a valid option (never raise).
    if source and _has_field("Lead", "custom_lead_source"):
        if source in _custom_lead_source_options():
            payload["custom_lead_source"] = source
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

    # 1. Standard ERPNext: a ``Lead Source`` DocType exists -> use its records.
    if _doctype_exists("Lead Source"):
        try:
            names = frappe.get_all("Lead Source", pluck="name")
        except Exception:
            return []
        return sorted(names)

    # 2. This site: no ``Lead Source`` DocType, but a ``custom_lead_source``
    #    Select field on Lead -> expose its options as the source list.
    if _has_field("Lead", "custom_lead_source"):
        return _custom_lead_source_options()

    # 3. Nothing to offer.
    return []


def _custom_lead_source_options(doctype="Lead"):
    """Non-empty Select options of ``custom_lead_source`` (guarded -> ``[]``)."""
    try:
        field = frappe.get_meta(doctype).get_field("custom_lead_source")
        if not field or not field.options:
            return []
        return [opt.strip() for opt in field.options.split("\n") if opt.strip()]
    except Exception:
        return []


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
def search_linkable_customers(query, limit=20):
    """Search enabled Customers that may be linked to a B2B account card.

    Deliberately searches every customer group/type. An existing retail Customer
    is still the same legal account and must remain linkable rather than being
    duplicated as a new Company/B2B record.
    """
    _ensure_b2b_access()
    _require_doc_permission("Customer", ptype="read")
    query = str(query or "").strip()
    if not query:
        return []
    try:
        limit = max(1, min(int(limit or 20), 50))
    except (TypeError, ValueError):
        limit = 20

    fields = [
        "name", "customer_name", "customer_type", "customer_group",
        "mobile_no", "territory", "disabled", "customer_primary_address",
    ]
    # get_list applies user permissions and match conditions. A global Customer
    # read bit is not permission to search accounts outside the caller's scope.
    rows = frappe.get_list(
        "Customer",
        filters={"disabled": 0},
        or_filters={
            "name": ["like", f"%{query}%"],
            "customer_name": ["like", f"%{query}%"],
            "mobile_no": ["like", f"%{query}%"],
        },
        fields=fields,
        order_by="customer_name asc",
        limit_page_length=limit,
    ) or []

    names = [row.get("name") for row in rows if row.get("name")]
    counts = {name: 0 for name in names}
    if names:
        links = frappe.get_all(
            "Dynamic Link",
            filters={
                "parenttype": "Address",
                "link_doctype": "Customer",
                "link_name": ["in", names],
            },
            fields=["link_name", "parent"],
            limit_page_length=0,
        ) or []
        seen = set()
        for link in links:
            key = (link.get("link_name"), link.get("parent"))
            if key in seen:
                continue
            seen.add(key)
            if key[0] in counts:
                counts[key[0]] += 1

    return [
        {
            "name": row.get("name"),
            "customer_name": row.get("customer_name"),
            "customer_type": row.get("customer_type"),
            "customer_group": row.get("customer_group"),
            "mobile_no": row.get("mobile_no"),
            "territory": row.get("territory"),
            "disabled": bool(row.get("disabled")),
            "address_count": counts.get(row.get("name"), 0),
            "primary_address": row.get("customer_primary_address"),
        }
        for row in rows
    ]


def _lock_relationship_row(doctype, name):
    """Serialize competing account-link requests for one Lead/Opportunity."""
    if doctype not in ("Lead", "Opportunity"):
        frappe.throw("party_doctype must be 'Lead' or 'Opportunity'.")
    frappe.db.sql(
        f"SELECT name FROM `tab{doctype}` WHERE name = %s FOR UPDATE",
        (name,),
    )


def _link_lead_customer(lead_name, customer, expected_customer=None, allow_relink=False):
    _lock_relationship_row("Lead", lead_name)
    converted_rows = _customer_rows_for_lead(lead_name)
    converted_names = {row.get("name") for row in converted_rows if row.get("name")}
    if len(converted_names) > 1:
        frappe.throw(
            "Lead relationship conflict: more than one Customer refers to this Lead. "
            "Ask a manager to repair the conversion records before linking."
        )
    converted = next(iter(converted_names), None)
    direct = frappe.db.get_value("Lead", lead_name, "customer") or None
    if direct and converted and direct != converted:
        frappe.throw(
            "Lead relationship conflict: the Lead and converted Customer point to "
            "different accounts. Ask a manager to repair the relationship before linking."
        )
    current = direct or converted
    expected = str(expected_customer or "").strip() or None

    if current == customer:
        return False
    if converted and converted != customer:
        frappe.throw(
            f"This Lead was already converted to Customer '{converted}'. Its conversion "
            "history must be repaired before it can be linked to another account."
        )
    if expected != current:
        frappe.throw(
            "The linked Customer changed while this account was open. Refresh the account "
            "and try again."
        )
    if current and (not allow_relink or not _can_manage_b2b_relationships()):
        frappe.throw(
            f"This Lead is already linked to Customer '{current}'. Only a manager may "
            "replace that link after reviewing the account history."
        )

    frappe.db.set_value("Lead", lead_name, "customer", customer, update_modified=True)
    return True


@frappe.whitelist()
def link_existing_customer(
    party_doctype,
    party_name,
    customer,
    expected_customer=None,
    allow_relink=0,
):
    """Atomically link an existing Customer to a Lead/Opportunity B2B card.

    Opportunities originating from a Lead update that Lead's standard Customer
    link and retain their original party fields. Customer-origin Opportunities
    are historical records and cannot be repointed by this endpoint.
    """
    _ensure_b2b_access()
    party_doctype = str(party_doctype or "").strip()
    party_name = str(party_name or "").strip()
    customer = str(customer or "").strip()
    if party_doctype not in ("Lead", "Opportunity"):
        frappe.throw("party_doctype must be 'Lead' or 'Opportunity'.")
    if not party_name or not frappe.db.exists(party_doctype, party_name):
        frappe.throw(f"{party_doctype} '{party_name}' not found.")
    _require_doc_permission(party_doctype, party_name, "write")
    _require_doc_permission("Customer", customer, "read")
    _assert_enabled_customer(customer)

    changed = False
    linked_via = party_doctype
    if party_doctype == "Lead":
        changed = _link_lead_customer(
            party_name,
            customer,
            expected_customer=expected_customer,
            allow_relink=_truthy(allow_relink),
        )
    else:
        _lock_relationship_row("Opportunity", party_name)
        opportunity = frappe.db.get_value(
            "Opportunity",
            party_name,
            ["opportunity_from", "party_name"],
            as_dict=True,
        ) or {}
        origin = str(opportunity.get("opportunity_from") or "").strip()
        current_party = str(opportunity.get("party_name") or "").strip()
        if origin == "Lead":
            if not current_party or not frappe.db.exists("Lead", current_party):
                frappe.throw("This Opportunity's source Lead no longer exists.")
            _require_doc_permission("Lead", current_party, "write")
            changed = _link_lead_customer(
                current_party,
                customer,
                expected_customer=expected_customer,
                allow_relink=_truthy(allow_relink),
            )
            linked_via = "Lead"
        elif origin == "Customer":
            if current_party != customer:
                frappe.throw(
                    f"This Opportunity is historically tied to Customer '{current_party}'. "
                    "It cannot be repointed from the B2B order flow."
                )
        else:
            frappe.throw(
                "Only Opportunities originating from a Lead or Customer can be linked."
            )

    if changed and linked_via == "Lead":
        # Settlement terms the rep agreed on the Lead follow it onto the linked
        # Customer (no Customer insert happens here, so the after_insert doc
        # event never fires). Only when the Customer has none. safe_carry_over
        # swallows everything except a deadlock / lock-wait timeout, which has
        # already rolled back this whole request and must propagate.
        from jarz_pos.services.settlement_lead_terms import safe_carry_over

        safe_carry_over(
            party_name if party_doctype == "Lead" else current_party,
            customer,
        )

    return {
        "success": True,
        "party_doctype": party_doctype,
        "party_name": party_name,
        "customer": customer,
        "changed": changed,
        "linked_via": linked_via,
    }


@frappe.whitelist()
def request_sample(
    party_doctype,
    party_name,
    customer_name=None,
    mobile_no=None,
    customer_primary_address=None,
    territory_id=None,
    customer_group=None,
    shipping_address_name=None,
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
        shipping_address_name=shipping_address_name,
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
    shipping_address_name=None,
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
        shipping_address_name=shipping_address_name,
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
    shipping_address_name=None,
):
    """Resolve the linked Customer (creating a Company customer if needed) and the
    commercial-policy price list for the given order purpose."""
    if party_doctype not in ("Lead", "Opportunity", "Customer"):
        frappe.throw("party_doctype must be 'Lead', 'Opportunity' or 'Customer'.")

    if not party_name or not frappe.db.exists(party_doctype, party_name):
        frappe.throw(f"{party_doctype} '{party_name}' not found.")

    _require_doc_permission(party_doctype, party_name, "read")

    customer = None
    source_lead = None

    if party_doctype == "Customer":
        customer = party_name
    elif party_doctype == "Lead":
        _lock_relationship_row("Lead", party_name)
        source_lead = party_name
        customer = _resolve_lead_customer(party_name)
    elif party_doctype == "Opportunity":
        _lock_relationship_row("Opportunity", party_name)
        opportunity = frappe.db.get_value(
            "Opportunity",
            party_name,
            ["opportunity_from", "party_name"],
            as_dict=True,
        )
        origin = str((opportunity or {}).get("opportunity_from") or "").strip()
        if origin == "Lead":
            source_lead = str((opportunity or {}).get("party_name") or "").strip()
            if not source_lead or not frappe.db.exists("Lead", source_lead):
                frappe.throw("This Opportunity's source Lead no longer exists.")
            _require_doc_permission("Lead", source_lead, "read")
            _lock_relationship_row("Lead", source_lead)
        elif origin == "Customer":
            direct_customer = str((opportunity or {}).get("party_name") or "").strip()
            if not direct_customer or not frappe.db.exists("Customer", direct_customer):
                frappe.throw(
                    "This Customer-origin Opportunity no longer points to a valid Customer. "
                    "Repair its CRM party before placing a B2B order."
                )
        else:
            frappe.throw(
                "This Opportunity is not linked to a Lead or Customer. Link its CRM party "
                "before placing a B2B order."
            )
        customer = _resolve_opportunity_customer(opportunity)

    # No linked Customer yet -> create a Company customer from supplied details.
    if not customer:
        if not (customer_name and mobile_no and customer_primary_address and territory_id):
            frappe.throw(
                "No linked Customer; supply customer_name, mobile_no, "
                "customer_primary_address and territory_id to create one."
            )
        from jarz_pos.api.customer import create_customer

        # When converting a Lead -> Customer, pass the lead so the create_customer
        # Contact-mobile guard ignores the Lead's own auto-created Contact.
        created = create_customer(
            customer_name=customer_name,
            mobile_no=mobile_no,
            customer_primary_address=customer_primary_address,
            territory_id=territory_id,
            customer_type="Company",
            customer_group=customer_group,
            source_lead=source_lead,
        )
        customer = (
            created.get("name")
            if isinstance(created, dict)
            else getattr(created, "name", None)
        )
        if not customer:
            frappe.throw("Failed to create Customer for B2B order.")

        # Customer.lead_name is the durable standard conversion link. Verify it
        # before returning so a repeated order cannot enter the create path again.
        if source_lead and _resolve_lead_customer(source_lead) != customer:
            frappe.throw("Customer was created but its source Lead was not linked.")

    _require_doc_permission("Customer", customer, "read")
    _assert_enabled_customer(customer)

    result = {
        "customer": customer,
        "order_purpose": order_purpose,
        "price_list": _policy_price_list(order_purpose),
    }
    result.update(_order_address_selection(customer, shipping_address_name))
    return result


def _order_address_selection(customer, shipping_address_name=None):
    """Address-book metadata for the thin binding response.

    A single address can be selected automatically. With zero or multiple
    addresses the binding stays valid, but the POS must create/select a branch
    before it submits the invoice.
    """
    from jarz_pos.api.customer import _build_customer_shipping_address_book
    from jarz_pos.utils.customer_address_utils import (
        preferred_address_was_honoured,
        resolve_customer_shipping_address,
    )
    from jarz_pos.utils.invoice_utils import (
        _territory_from_address_row,
        resolve_pos_profile_for_territory,
    )

    address_book = _build_customer_shipping_address_book(customer)
    branch_options = list(address_book.get("branch_options") or [])
    requested = str(shipping_address_name or "").strip()
    selected = None

    if requested:
        selected = resolve_customer_shipping_address(
            customer, preferred_address_name=requested
        )
        if not preferred_address_was_honoured(customer, requested, selected):
            frappe.throw("Selected shipping address does not belong to this customer.")
    elif len(branch_options) == 1:
        only_address_name = branch_options[0].get("address_name")
        selected = resolve_customer_shipping_address(
            customer, preferred_address_name=only_address_name
        )

    selected_name = str((selected or {}).get("name") or "").strip() or None
    effective_territory = None
    territory_pos_profile = None
    if selected_name:
        effective_territory = _territory_from_address_row(selected)
        if not effective_territory:
            if requested:
                frappe.throw(
                    "Selected branch has no valid territory. Edit the branch address and "
                    "choose its delivery territory before ordering."
                )
            selected = None
            selected_name = None
        if effective_territory:
            territory_pos_profile = resolve_pos_profile_for_territory(effective_territory)

    if selected_name:
        selection_error = None
    elif not branch_options:
        selection_error = "no_shipping_address"
    elif len(branch_options) > 1:
        selection_error = "selection_required"
    else:
        selection_error = "missing_territory"

    return {
        "address_book": address_book,
        "requires_shipping_address_selection": not bool(selected_name),
        "shipping_address_name": selected_name,
        "effective_territory": effective_territory,
        "territory_pos_profile": territory_pos_profile,
        "address_selection_error": selection_error,
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
