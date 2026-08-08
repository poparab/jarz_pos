"""Leads catalog API for Jarz POS (role-gated, B2B).

Whitelisted endpoints powering the Flutter "Leads catalog" experience. The
standard ERPNext ``Lead`` DocType is reused as the catalog store: rich catalog
metrics live on ``custom_*`` fields, per-branch detail lives in the
``custom_branches`` child table (Jarz Lead Branch), and the lead category is a
Link to the ``Jarz Lead Category`` master.

Design notes:
  - Every endpoint is gated by ``_ensure_b2b_access()`` (B2B Sales Rep OR
    manager), reusing the exact gate from ``jarz_pos.api.crm``.
  - Responses are plain JSON-serializable dicts/lists (Frappe wraps in
    ``{"message": ...}``).
  - JSON list fields (``custom_areas`` / ``custom_regions`` /
    ``custom_governorates``) store ``json.dumps([...])`` and are parsed back to
    Python lists on read (guarded -> ``[]`` for empty / non-JSON values).
  - Addresses are standard ERPNext ``Address`` records linked to the Lead via a
    ``Dynamic Link`` child row (``link_doctype="Lead", link_name=<lead>``).
  - "Not suitable" is the manual-inspection verdict: a rep who has looked at a
    prospect and judged it not worth pursuing marks it here. It is a separate
    axis from ``custom_b2b_stage`` (which tracks a live deal) and is written
    only through :func:`set_lead_suitability`, never through ``save_lead``.
"""

import json

import frappe

# Reuse the CRM access gate + field/option guards verbatim; never reinvent the
# B2B gating or the standard-CRM parity guards here.
from jarz_pos.api.crm import (
    _custom_lead_source_options,
    _ensure_b2b_access,
    _has_field,
)

DEFAULT_LEAD_CATEGORY = "Coffee"

# Canonical "why is this prospect not worth pursuing" reasons. Kept in lockstep
# with the ``custom_not_suitable_reason`` Select options in the custom_field
# fixture; served to the client by :func:`get_not_suitable_reasons` so the app
# never hard-codes its own copy.
NOT_SUITABLE_REASONS = [
    "Out of Business",
    "Wrong Category",
    "Too Small",
    "No Contact Info",
    "Unreachable",
    "Already Supplied",
    "Price Mismatch",
    "Outside Delivery Area",
    "Duplicate",
    "Not Interested",
    "Other",
]

# Stage a lead is parked at when it is marked not suitable, and the stage it is
# returned to when the verdict is reverted.
_NOT_SUITABLE_STAGE = "Lost/On-hold"
_DEFAULT_STAGE = "Lead"

# Flat DocType fields fetched for both list and detail responses.
_LEAD_FLAT_FIELDS = [
    "name",
    "custom_source_brand_id",
    "lead_name",
    "custom_lead_category",
    "custom_fit_score",
    "custom_fit_tier",
    "custom_branch_count",
    "custom_price_band",
    "custom_avg_rating",
    "custom_total_reviews",
    "custom_open_status",
    "custom_sahel_branches",
    "custom_is_specialty",
    "custom_primary_area",
    "custom_regions",
    "custom_governorates",
    "custom_areas",
    "phone",
    "mobile_no",
    "website",
    "custom_instagram",
    "custom_facebook",
    "custom_maps_url",
    "custom_confidence",
    "status",
    "custom_b2b_stage",
    "custom_last_verified",
    "custom_latitude",
    "custom_longitude",
    "custom_not_suitable",
    "custom_not_suitable_reason",
    "custom_not_suitable_notes",
    "custom_not_suitable_on",
    "custom_not_suitable_by",
]

# Fields that only exist once the not-suitable migration has run. Code is
# deployed before ``bench migrate`` completes, so a query naming them would fail
# with "Unknown column" during that window — every read filters through
# :func:`_lead_query_fields` and every filter through :func:`_has_verdict_field`.
_VERDICT_FIELDS = (
    "custom_not_suitable",
    "custom_not_suitable_reason",
    "custom_not_suitable_notes",
    "custom_not_suitable_on",
    "custom_not_suitable_by",
)


def _has_verdict_field():
    """Whether the site has migrated the not-suitable fields. Guarded -> False."""
    return _has_field("Lead", "custom_not_suitable")


def _lead_query_fields():
    """``_LEAD_FLAT_FIELDS`` minus anything this site has not migrated yet."""
    if _has_verdict_field():
        return _LEAD_FLAT_FIELDS
    return [f for f in _LEAD_FLAT_FIELDS if f not in _VERDICT_FIELDS]

# Child-row (Jarz Lead Branch) fields returned in lead detail.
_BRANCH_FIELDS = (
    "branch_name",
    "area",
    "region",
    "governorate",
    "rating",
    "reviews",
    "price",
    "status",
    "hours",
    "phone",
    "website",
    "maps_url",
    "address",
    "latitude",
    "longitude",
)


# ---------------------------------------------------------------------------
# Small parsing / coalescing helpers
# ---------------------------------------------------------------------------
def _json_list(value):
    """Parse a json.dumps([...]) list back to a Python list. Guarded -> []."""
    if not value:
        return []
    if isinstance(value, (list, tuple)):
        return list(value)
    try:
        parsed = json.loads(value)
    except (ValueError, TypeError):
        return []
    if isinstance(parsed, list):
        return parsed
    return []


def _int(value):
    try:
        return int(value or 0)
    except (ValueError, TypeError):
        return 0


def _float_or_none(value):
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (ValueError, TypeError):
        return None


def _bool(value):
    try:
        return bool(int(value or 0))
    except (ValueError, TypeError):
        return bool(value)


def _str_or_none(value):
    return str(value) if value not in (None, "") else None


def _map_lead_row(row):
    """Map a flat Lead row (dict) to the frozen catalog output shape."""
    return {
        "name": row.get("name"),
        "source_brand_id": row.get("custom_source_brand_id"),
        "lead_name": row.get("lead_name"),
        "category": row.get("custom_lead_category"),
        # Output key stays ``score`` (Flutter unchanged); source column is now the
        # catalog-owned custom_fit_score (custom_lead_score belongs to the CRM job).
        "score": _int(row.get("custom_fit_score")),
        "tier": row.get("custom_fit_tier"),
        "branch_count": _int(row.get("custom_branch_count")),
        "price_band": row.get("custom_price_band"),
        "avg_rating": _float_or_none(row.get("custom_avg_rating")),
        "total_reviews": _int(row.get("custom_total_reviews")),
        "open_status": row.get("custom_open_status"),
        "sahel_branches": _int(row.get("custom_sahel_branches")),
        "is_specialty": _bool(row.get("custom_is_specialty")),
        "primary_area": row.get("custom_primary_area"),
        "regions": _json_list(row.get("custom_regions")),
        "governorates": _json_list(row.get("custom_governorates")),
        "areas": _json_list(row.get("custom_areas")),
        "phone": row.get("phone") or row.get("mobile_no"),
        "website": row.get("website"),
        "instagram": row.get("custom_instagram"),
        "facebook": row.get("custom_facebook"),
        "maps_url": row.get("custom_maps_url"),
        "confidence": row.get("custom_confidence"),
        "status": row.get("status"),
        "b2b_stage": row.get("custom_b2b_stage"),
        "last_verified": _str_or_none(row.get("custom_last_verified")),
        "latitude": _float_or_none(row.get("custom_latitude")),
        "longitude": _float_or_none(row.get("custom_longitude")),
        # Manual-inspection verdict (see set_lead_suitability).
        "not_suitable": _bool(row.get("custom_not_suitable")),
        "not_suitable_reason": row.get("custom_not_suitable_reason") or "",
        "not_suitable_notes": row.get("custom_not_suitable_notes") or "",
        "not_suitable_on": _str_or_none(row.get("custom_not_suitable_on")),
        "not_suitable_by": row.get("custom_not_suitable_by") or "",
    }


# ---------------------------------------------------------------------------
# List
# ---------------------------------------------------------------------------
@frappe.whitelist()
def get_leads(category=None, status=None, not_suitable=None):
    """Return the whole leads catalog (coarse server-side filtering only).

    Optional coarse filters: ``category`` -> custom_lead_category,
    ``status`` -> status. Fine-grained filtering is client-side, so the full
    matching set is returned (no pagination).

    ``not_suitable`` is tri-state and defaults to "return everything" so the
    client keeps one complete cache and hides the rejected prospects itself:
      - ``None`` / "" -> both suitable and not-suitable leads (default)
      - falsy ("0"/0/False) -> only leads NOT marked not-suitable
      - truthy ("1"/1/True) -> only leads marked not-suitable

    Returns: ``{"leads": [<mapped row>, ...], "count": <int>}``.
    """
    _ensure_b2b_access()

    filters = {}
    if category:
        filters["custom_lead_category"] = category
    if status:
        filters["status"] = status
    if not_suitable not in (None, "") and _has_verdict_field():
        filters["custom_not_suitable"] = 1 if _bool(not_suitable) else 0

    rows = frappe.get_all(
        "Lead",
        filters=filters or None,
        fields=_lead_query_fields(),
        order_by="custom_fit_score desc",
        limit_page_length=0,
    )

    leads = [_map_lead_row(row) for row in rows]
    return {"leads": leads, "count": len(leads)}


# ---------------------------------------------------------------------------
# Detail
# ---------------------------------------------------------------------------
@frappe.whitelist()
def get_lead(name):
    """Full detail for one Lead: catalog fields + branches + addresses + notes.

    Returns all ``get_leads`` fields plus:
      - ``branches``: list of mapped Jarz Lead Branch child rows.
      - ``primary_address`` / ``shipping_address``: linked ERPNext Address
        records (or null).
      - ``notes``: ``custom_notes`` (str, default "").
    """
    _ensure_b2b_access()

    if not frappe.db.exists("Lead", name):
        frappe.throw(f"Lead '{name}' not found.")

    doc = frappe.get_doc("Lead", name)

    # Build the flat catalog shape from the loaded doc (reuse the row mapper by
    # feeding it a dict view of the doc fields).
    flat = {f: doc.get(f) for f in _lead_query_fields()}
    flat["name"] = doc.name
    result = _map_lead_row(flat)

    # Branches (custom_branches child table).
    branches = []
    for row in (doc.get("custom_branches") or []):
        branches.append(
            {
                "branch_name": row.get("branch_name"),
                "area": row.get("area"),
                "region": row.get("region"),
                "governorate": row.get("governorate"),
                "rating": _float_or_none(row.get("rating")),
                "reviews": _int(row.get("reviews")),
                "price": row.get("price"),
                "status": row.get("status"),
                "hours": row.get("hours"),
                "phone": row.get("phone"),
                "website": row.get("website"),
                "maps_url": row.get("maps_url"),
                "address": row.get("address"),
                "latitude": _float_or_none(row.get("latitude")),
                "longitude": _float_or_none(row.get("longitude")),
            }
        )
    result["branches"] = branches

    # Linked ERPNext Address records (via Dynamic Link).
    result["primary_address"] = _lead_address(name, "is_primary_address")
    result["shipping_address"] = _lead_address(name, "is_shipping_address")

    # Editable rep notes.
    result["notes"] = doc.get("custom_notes") or ""

    return result


def _linked_lead_address_names(name):
    """Address names linked to a Lead via Dynamic Link. Guarded -> []."""
    try:
        rows = frappe.get_all(
            "Dynamic Link",
            filters={
                "link_doctype": "Lead",
                "link_name": name,
                "parenttype": "Address",
            },
            fields=["parent"],
            limit_page_length=0,
        ) or []
    except Exception:
        return []
    seen = set()
    names = []
    for r in rows:
        parent = str(r.get("parent") or "").strip()
        if parent and parent not in seen:
            seen.add(parent)
            names.append(parent)
    return names


def _lead_address(name, flag_field):
    """Return the linked Address flagged by ``flag_field`` (mapped) or None."""
    address_names = _linked_lead_address_names(name)
    if not address_names:
        return None
    try:
        rows = frappe.get_all(
            "Address",
            filters={"name": ["in", address_names], flag_field: 1},
            fields=[
                "name",
                "address_line1",
                "address_line2",
                "city",
                "state",
                "country",
                "pincode",
                "phone",
            ],
            order_by="modified desc",
            limit_page_length=1,
        )
    except Exception:
        return None
    if not rows:
        return None
    a = rows[0]
    return {
        "name": a.get("name"),
        "address_line1": a.get("address_line1"),
        "address_line2": a.get("address_line2"),
        "city": a.get("city"),
        "state": a.get("state"),
        "country": a.get("country"),
        "pincode": a.get("pincode"),
        "phone": a.get("phone"),
    }


# ---------------------------------------------------------------------------
# Save (create / update)
# ---------------------------------------------------------------------------
# Payload key -> Lead fieldname for simple scalar assignments.
_SCALAR_FIELD_MAP = {
    "lead_name": "lead_name",
    "company_name": "company_name",
    "category": "custom_lead_category",
    "tier": "custom_fit_tier",
    # Catalog fit score writes to its own field; custom_lead_score is owned by
    # the nightly CRM job (compute_lead_scores) and must never be written here.
    "score": "custom_fit_score",
    "price_band": "custom_price_band",
    "phone": "phone",
    "mobile_no": "mobile_no",
    "email_id": "email_id",
    "website": "website",
    "instagram": "custom_instagram",
    "facebook": "custom_facebook",
    "maps_url": "custom_maps_url",
    "primary_area": "custom_primary_area",
    "is_specialty": "custom_is_specialty",
    "open_status": "custom_open_status",
    "confidence": "custom_confidence",
    "notes": "custom_notes",
    "latitude": "custom_latitude",
    "longitude": "custom_longitude",
    "branch_count": "custom_branch_count",
    "avg_rating": "custom_avg_rating",
    "total_reviews": "custom_total_reviews",
    "sahel_branches": "custom_sahel_branches",
    "last_verified": "custom_last_verified",
}

# Payload list keys -> Lead json.dumps fieldname.
_LIST_FIELD_MAP = {
    "areas": "custom_areas",
    "regions": "custom_regions",
    "governorates": "custom_governorates",
}


@frappe.whitelist()
def save_lead(payload, name=None):
    """Create (``name`` is None) or update a catalog Lead from a payload dict.

    Returns: ``{"name": <lead name>}``.
    """
    _ensure_b2b_access()

    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except (ValueError, TypeError):
            frappe.throw("payload must be a JSON object.")
    if not isinstance(payload, dict):
        frappe.throw("payload must be an object.")

    creating = not name
    if creating:
        doc = frappe.new_doc("Lead")
        doc.custom_b2b_stage = "Lead"
        doc.status = "Open"
        if not (payload.get("lead_name") or "").strip():
            frappe.throw("lead_name is required to create a Lead.")
    else:
        if not frappe.db.exists("Lead", name):
            frappe.throw(f"Lead '{name}' not found.")
        doc = frappe.get_doc("Lead", name)

    # Scalar fields.
    for key, field in _SCALAR_FIELD_MAP.items():
        if key in payload:
            value = payload.get(key)
            if field == "custom_is_specialty":
                value = 1 if _bool(value) else 0
            doc.set(field, value)

    # JSON list fields.
    for key, field in _LIST_FIELD_MAP.items():
        if key in payload:
            value = payload.get(key)
            if value is None:
                value = []
            if not isinstance(value, (list, tuple)):
                value = [value]
            doc.set(field, json.dumps(list(value)))

    # Standard-CRM parity (mirrors jarz_pos.api.crm.create_lead guards):
    # ``source`` / ``custom_lead_source`` / ``territory`` are set only when the
    # referenced master record or Select option actually exists, so an unknown
    # value is silently ignored rather than raising. ``email_id`` is handled via
    # ``_SCALAR_FIELD_MAP`` above. Applied on both create and update.
    source = payload.get("source")
    if source and _has_field("Lead", "source") and frappe.db.exists(
        "Lead Source", source
    ):
        doc.set("source", source)
    if source and _has_field("Lead", "custom_lead_source"):
        if source in _custom_lead_source_options():
            doc.set("custom_lead_source", source)
    territory = payload.get("territory")
    if (
        territory
        and _has_field("Lead", "territory")
        and frappe.db.exists("Territory", territory)
    ):
        doc.set("territory", territory)

    # Branches child table (replace wholesale when provided).
    if "branches" in payload and payload.get("branches") is not None:
        doc.set("custom_branches", [])
        for b in (payload.get("branches") or []):
            if not isinstance(b, dict):
                continue
            doc.append(
                "custom_branches",
                {f: b.get(f) for f in _BRANCH_FIELDS if f in b},
            )

    if creating:
        doc.insert(ignore_permissions=True)
        _assign_to_caller(doc.name)
    else:
        doc.save(ignore_permissions=True)

    return {"name": doc.name}


def _assign_to_caller(lead_name):
    """Assign the Lead to the calling user via standard Frappe assignment."""
    try:
        from frappe.desk.form.assign_to import add as _assign_add

        _assign_add(
            {
                "assign_to": [frappe.session.user],
                "doctype": "Lead",
                "name": lead_name,
            }
        )
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Not suitable (manual-inspection verdict)
# ---------------------------------------------------------------------------
@frappe.whitelist()
def get_not_suitable_reasons():
    """Return the canonical not-suitable reasons for the client dropdown.

    Prefers the live ``custom_not_suitable_reason`` Select options so a reason
    added in Desk shows up without an app release; falls back to the module
    constant when the field is missing (fresh site / pre-migrate).

    Returns: ``{"reasons": [<str>, ...]}``.
    """
    _ensure_b2b_access()
    return {"reasons": _not_suitable_reason_options()}


def _not_suitable_reason_options():
    """Live Select options for custom_not_suitable_reason. Guarded -> constant."""
    try:
        field = frappe.get_meta("Lead").get_field("custom_not_suitable_reason")
        options = [
            opt.strip()
            for opt in str((field.options if field else "") or "").split("\n")
            if opt.strip()
        ]
        if options:
            return options
    except Exception:
        pass
    return list(NOT_SUITABLE_REASONS)


@frappe.whitelist()
def set_lead_suitability(name, not_suitable=1, reason=None, notes=None):
    """Mark a Lead as not suitable after manual inspection, or clear the verdict.

    Marking (``not_suitable`` truthy) stamps the reason, the free-text notes,
    the timestamp and the acting user, and parks the lead at the
    ``Lost/On-hold`` B2B stage so it leaves the pipeline board. Clearing wipes
    all five fields and returns the lead to the ``Lead`` stage.

    ``reason`` is required when marking and must be one of the values served by
    :func:`get_not_suitable_reasons`.

    Returns the refreshed catalog row: ``{"lead": <mapped row>}``.
    """
    _ensure_b2b_access()

    if not _has_verdict_field():
        frappe.throw(
            "This site has not migrated the not-suitable fields yet. "
            "Run `bench migrate` and try again."
        )
    if not frappe.db.exists("Lead", name):
        frappe.throw(f"Lead '{name}' not found.")

    marking = _bool(not_suitable)
    reason = (reason or "").strip()
    notes = (notes or "").strip()

    if marking:
        if not reason:
            frappe.throw("A reason is required to mark a lead not suitable.")
        allowed = _not_suitable_reason_options()
        if reason not in allowed:
            frappe.throw(
                f"'{reason}' is not a valid reason. Expected one of: "
                + ", ".join(allowed)
            )

    doc = frappe.get_doc("Lead", name)

    if marking:
        doc.custom_not_suitable = 1
        doc.custom_not_suitable_reason = reason
        doc.custom_not_suitable_notes = notes or None
        doc.custom_not_suitable_on = frappe.utils.now()
        doc.custom_not_suitable_by = frappe.session.user
        doc.custom_b2b_stage = _NOT_SUITABLE_STAGE
    else:
        doc.custom_not_suitable = 0
        doc.custom_not_suitable_reason = None
        doc.custom_not_suitable_notes = None
        doc.custom_not_suitable_on = None
        doc.custom_not_suitable_by = None
        # Only pull the stage back if it is still parked where marking put it;
        # a lead someone has since moved on keeps whatever stage it now has.
        if (doc.get("custom_b2b_stage") or "") == _NOT_SUITABLE_STAGE:
            doc.custom_b2b_stage = _DEFAULT_STAGE

    doc.save(ignore_permissions=True)

    # A not-suitable lead should stop generating follow-up reminders; a revived
    # one starts clean rather than firing every missed reminder at once.
    _clear_followup(doc)

    flat = {f: doc.get(f) for f in _lead_query_fields()}
    flat["name"] = doc.name
    return {"lead": _map_lead_row(flat)}


def _clear_followup(doc):
    """Clear the pending follow-up date on a lead and close its open ToDos.

    Guarded: a missing field or a failing ToDo query must never fail the
    suitability write that already committed to the document.
    """
    try:
        if doc.meta.get_field("custom_next_followup_date") and doc.get(
            "custom_next_followup_date"
        ):
            frappe.db.set_value(
                "Lead", doc.name, "custom_next_followup_date", None,
                update_modified=False,
            )
    except Exception:
        frappe.log_error(
            title="leads.set_lead_suitability: clear follow-up date failed",
            message=frappe.get_traceback(),
        )
    try:
        from jarz_pos.api.crm import _close_open_todos

        _close_open_todos("Lead", doc.name)
    except Exception:
        frappe.log_error(
            title="leads.set_lead_suitability: close ToDos failed",
            message=frappe.get_traceback(),
        )


# ---------------------------------------------------------------------------
# Address (primary / shipping)
# ---------------------------------------------------------------------------
@frappe.whitelist()
def set_lead_address(name, kind, address):
    """Create or update the primary/shipping Address linked to a Lead.

    ``kind`` in {"primary", "shipping"}. ``address`` is a dict with
    address_line1/address_line2/city/state/country/pincode/phone.

    Returns: ``{"address": <address name>}``.
    """
    _ensure_b2b_access()

    if kind not in ("primary", "shipping"):
        frappe.throw("kind must be 'primary' or 'shipping'.")
    if not frappe.db.exists("Lead", name):
        frappe.throw(f"Lead '{name}' not found.")
    if isinstance(address, str):
        try:
            address = json.loads(address)
        except (ValueError, TypeError):
            frappe.throw("address must be a JSON object.")
    if not isinstance(address, dict):
        frappe.throw("address must be an object.")

    is_primary = kind == "primary"
    flag_field = "is_primary_address" if is_primary else "is_shipping_address"
    address_type = "Billing" if is_primary else "Shipping"

    # Find an existing linked Address already flagged for this kind.
    address_name = None
    for candidate in _linked_lead_address_names(name):
        if frappe.db.get_value("Address", candidate, flag_field):
            address_name = candidate
            break

    if address_name:
        doc = frappe.get_doc("Address", address_name)
    else:
        doc = frappe.new_doc("Address")
        doc.address_title = frappe.db.get_value("Lead", name, "lead_name") or name

    doc.address_type = address_type
    doc.address_line1 = address.get("address_line1") or doc.get("address_line1") or ""
    doc.address_line2 = address.get("address_line2")
    doc.city = address.get("city")
    doc.state = address.get("state")
    doc.country = address.get("country") or doc.get("country")
    doc.pincode = address.get("pincode")
    if address.get("phone"):
        doc.phone = address.get("phone")
    doc.set(flag_field, 1)

    # Ensure a Dynamic Link row to this Lead exists.
    has_link = any(
        (link.get("link_doctype") == "Lead" and link.get("link_name") == name)
        for link in (doc.get("links") or [])
    )
    if not has_link:
        doc.append("links", {"link_doctype": "Lead", "link_name": name})

    doc.save(ignore_permissions=True)
    return {"address": doc.name}


# ---------------------------------------------------------------------------
# Categories
# ---------------------------------------------------------------------------
@frappe.whitelist()
def get_lead_categories():
    """Return enabled Jarz Lead Category masters.

    Returns: ``{"categories": [{"name","category_name","color"}, ...]}``.
    """
    _ensure_b2b_access()

    rows = frappe.get_all(
        "Jarz Lead Category",
        filters={"disabled": 0},
        fields=["name", "category_name", "color"],
        order_by="category_name asc",
    )
    return {"categories": rows}


@frappe.whitelist()
def save_lead_category(category_name, color=None):
    """Idempotently create a Jarz Lead Category (or update its color).

    Returns: ``{"name": ..., "category_name": ...}``.
    """
    _ensure_b2b_access()

    category_name = (category_name or "").strip()
    if not category_name:
        frappe.throw("category_name is required.")

    if frappe.db.exists("Jarz Lead Category", category_name):
        if color is not None:
            frappe.db.set_value(
                "Jarz Lead Category", category_name, "color", color
            )
        doc_name = category_name
    else:
        doc = frappe.get_doc(
            {
                "doctype": "Jarz Lead Category",
                "category_name": category_name,
                "color": color,
            }
        )
        doc.insert(ignore_permissions=True)
        doc_name = doc.name

    return {"name": doc_name, "category_name": category_name}
