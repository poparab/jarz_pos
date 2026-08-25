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
    "custom_takeout",
    "custom_dine_in",
    "custom_serves_dessert",
    "custom_on_talabat",
    "custom_talabat_areas",
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
    "custom_merged_into",
    "custom_merged_on",
    "custom_merged_by",
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

# Same deal for the duplicate-merge bookkeeping.
_MERGE_FIELDS = (
    "custom_merged_into",
    "custom_merged_on",
    "custom_merged_by",
)

# ...and for the Google service signals (takeaway / dine-in / dessert).
#
# IMPORTANT: these are three-state in reality but stored as Check. The Places API
# only ever reports them when TRUE, so 1 == "Google confirms it" and 0 ==
# "unknown", NEVER "no". Filter on ``takeout=1`` to find confirmed-takeaway
# venues; do NOT filter on 0 to mean the venue has no takeaway.
_TAKEAWAY_FIELDS = (
    "custom_takeout",
    "custom_dine_in",
    "custom_serves_dessert",
)

# ...and for Talabat presence.
#
# IMPORTANT: unlike the Google signals above, this one IS two-state. It is
# sourced by reading Talabat's own per-area listings, so 0 means "not seen in
# any area we have checked" and filtering on 0 is meaningful (though it still
# only covers the areas actually swept -- see custom_talabat_areas).
_TALABAT_FIELDS = (
    "custom_on_talabat",
    "custom_talabat_areas",
)


def _has_verdict_field():
    """Whether the site has migrated the not-suitable fields. Guarded -> False."""
    return _has_field("Lead", "custom_not_suitable")


def _has_merge_field():
    """Whether the site has migrated the merge fields. Guarded -> False."""
    return _has_field("Lead", "custom_merged_into")


def _has_takeaway_field():
    """Whether the site has migrated the service-signal fields. Guarded -> False."""
    return _has_field("Lead", "custom_takeout")


def _has_talabat_field():
    """Whether the site has migrated the Talabat fields. Guarded -> False."""
    return _has_field("Lead", "custom_on_talabat")


def _has_contacts_field():
    """Whether the site has migrated the contacts child table. Guarded -> False."""
    return _has_field("Lead", "custom_contacts")


def _lead_query_fields():
    """``_LEAD_FLAT_FIELDS`` minus anything this site has not migrated yet."""
    skip = set()
    if not _has_verdict_field():
        skip.update(_VERDICT_FIELDS)
    if not _has_merge_field():
        skip.update(_MERGE_FIELDS)
    if not _has_takeaway_field():
        skip.update(_TAKEAWAY_FIELDS)
    if not _has_talabat_field():
        skip.update(_TALABAT_FIELDS)
    if not skip:
        return _LEAD_FLAT_FIELDS
    return [f for f in _LEAD_FLAT_FIELDS if f not in skip]

# Child-row (Jarz Lead Contact) fields. One row per PERSON at the venue: a
# lead is a business, and a rep who walks in meets whoever is on shift, so the
# owner, the manager, the shift manager and the barista all need to live on the
# record side by side. ``role`` is deliberately free text -- every venue names
# its own jobs -- and ``is_primary`` marks the one person to ring first.
_CONTACT_FIELDS = (
    "contact_name",
    "role",
    "phone",
    "email",
    "is_primary",
    "notes",
)

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
    "on_talabat",
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
        # Google service signals. True == confirmed by Google; False == UNKNOWN,
        # not "no" (see _TAKEAWAY_FIELDS).
        "takeout": _bool(row.get("custom_takeout")),
        "dine_in": _bool(row.get("custom_dine_in")),
        "serves_dessert": _bool(row.get("custom_serves_dessert")),
        # Talabat presence. Two-state (see _TALABAT_FIELDS): False really does
        # mean "not listed in any area we swept", and talabat_areas names which
        # delivery zones the listing was seen in.
        "on_talabat": _bool(row.get("custom_on_talabat")),
        "talabat_areas": _json_list(row.get("custom_talabat_areas")),
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
        # Duplicate-merge bookkeeping (see merge_leads).
        "merged_into": row.get("custom_merged_into") or "",
        "merged_on": _str_or_none(row.get("custom_merged_on")),
        "merged_by": row.get("custom_merged_by") or "",
    }


# ---------------------------------------------------------------------------
# Contacts (people at the venue)
# ---------------------------------------------------------------------------
def _map_contact_row(row):
    """Map a Jarz Lead Contact child row (dict or Document) to output shape."""
    return {
        "contact_name": str(row.get("contact_name") or "").strip(),
        "role": str(row.get("role") or "").strip(),
        "phone": str(row.get("phone") or "").strip(),
        "email": str(row.get("email") or "").strip(),
        "is_primary": _bool(row.get("is_primary")),
        "notes": str(row.get("notes") or "").strip(),
    }


def _normalize_contacts(value):
    """Parse + clean an inbound contacts list into storable rows.

    Accepts a list of dicts or the ``json.dumps`` of one (Frappe delivers list
    args as strings). Rows with neither a name nor a phone are dropped -- an
    empty row in the app's editor must not become an empty child row. Exactly
    one row ends up primary: the first one flagged, or the first row overall,
    so ``primary_contact`` is always answerable.
    """
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (ValueError, TypeError):
            frappe.throw("contacts must be a JSON list of contact objects.")
    if value is None:
        value = []
    if isinstance(value, dict):
        value = [value]
    if not isinstance(value, (list, tuple)):
        frappe.throw("contacts must be a list of contact objects.")

    rows = []
    primary_taken = False
    for raw in value:
        if not isinstance(raw, dict):
            continue
        row = _map_contact_row(raw)
        if not row["contact_name"] and not row["phone"]:
            continue
        if row["is_primary"] and primary_taken:
            row["is_primary"] = False
        elif row["is_primary"]:
            primary_taken = True
        rows.append(row)
    if rows and not primary_taken:
        rows[0]["is_primary"] = True
    return rows


def _contact_key(row):
    """Identity of a contact row for dedup: phone digits, else name+role."""
    phone = "".join(ch for ch in str(row.get("phone") or "") if ch.isdigit())
    if phone:
        return "tel|" + phone[-9:]
    name = " ".join(str(row.get("contact_name") or "").split()).lower()
    role = " ".join(str(row.get("role") or "").split()).lower()
    return f"who|{name}|{role}"


def _apply_contacts(doc, contacts):
    """Replace a Lead's contacts child table with ``contacts`` (normalised).

    Also back-fills the Lead's own ``phone`` from the primary contact when the
    Lead has none: the catalog card, the "has contact" filter and the journey
    note default all read that field, and a lead whose only number lives on a
    person row would otherwise still look unreachable.
    """
    rows = _normalize_contacts(contacts)
    doc.set("custom_contacts", [])
    for row in rows:
        doc.append("custom_contacts", dict(row))

    if not str(doc.get("phone") or "").strip():
        primary = next(
            (r for r in rows if r["is_primary"] and r["phone"]),
            next((r for r in rows if r["phone"]), None),
        )
        if primary:
            doc.set("phone", primary["phone"])
    return rows


def _lead_contacts(doc):
    """Mapped contact rows for a loaded Lead doc. Guarded -> []."""
    return [_map_contact_row(row) for row in (doc.get("custom_contacts") or [])]


def _attach_contacts(leads):
    """Merge each lead's contact rows into its catalog row.

    ONE query for the whole catalog, exactly like the journey summaries: the
    corpus runs to thousands of leads but only the worked ones carry people, so
    the child table is a small fraction of it and a per-lead query would be
    thousands of round trips. Best-effort -- a site that has not migrated the
    child table yet just gets empty lists.
    """
    for lead in leads:
        lead["contacts"] = []
    if not leads or not _has_contacts_field():
        return
    try:
        rows = frappe.get_all(
            "Jarz Lead Contact",
            filters={"parenttype": "Lead", "parentfield": "custom_contacts"},
            fields=["parent", "idx", *_CONTACT_FIELDS],
            order_by="parent asc, idx asc",
            limit_page_length=0,
        ) or []
    except Exception:
        frappe.log_error(
            title="leads: contacts lookup failed",
            message=frappe.get_traceback(),
        )
        return
    by_parent = {}
    for row in rows:
        by_parent.setdefault(row.get("parent"), []).append(_map_contact_row(row))
    for lead in leads:
        contacts = by_parent.get(lead.get("name"))
        if contacts:
            lead["contacts"] = contacts


# ---------------------------------------------------------------------------
# List
# ---------------------------------------------------------------------------
@frappe.whitelist()
def get_leads(category=None, status=None, not_suitable=None, include_merged=0,
              takeout=None, on_talabat=None):
    """Return the whole leads catalog (coarse server-side filtering only).

    Optional coarse filters: ``category`` -> custom_lead_category,
    ``status`` -> status. Fine-grained filtering is client-side, so the full
    matching set is returned (no pagination).

    ``not_suitable`` is tri-state and defaults to "return everything" so the
    client keeps one complete cache and hides the rejected prospects itself:
      - ``None`` / "" -> both suitable and not-suitable leads (default)
      - falsy ("0"/0/False) -> only leads NOT marked not-suitable
      - truthy ("1"/1/True) -> only leads marked not-suitable

    ``include_merged`` defaults to false: a Lead that was merged into another as
    a duplicate is not a prospect any more, it is an audit record, so it is
    excluded unless explicitly asked for. This one is NOT client-side like the
    not-suitable filter — a merged duplicate should never reach the cache at
    all, because showing it would re-offer the very row a rep just eliminated.

    ``on_talabat`` is an optional coarse filter over the Talabat-presence flag.
    Unlike ``takeout`` it is genuinely tri-state, because the flag is two-state:
      - ``None`` / "" -> everything (default)
      - truthy -> only brands listed on Talabat
      - falsy  -> only brands NOT listed on Talabat
    Clients normally filter this one themselves off the cached catalog.

    ``takeout`` is an optional coarse filter for the confirmed-takeaway segment
    (venues Google reports as doing takeaway, i.e. serving drinks in takeaway
    cups). Only ``takeout=1`` is meaningful: a 0 would mean "unknown", not "no",
    so passing a falsy value is ignored rather than silently returning a bogus
    "no takeaway" set. Clients normally filter this one themselves.

    Returns: ``{"leads": [<mapped row>, ...], "count": <int>}``.
    """
    _ensure_b2b_access()

    filters = {}
    if category:
        filters["custom_lead_category"] = category
    if status:
        filters["status"] = status
    if _bool(takeout) and _has_takeaway_field():
        filters["custom_takeout"] = 1
    if on_talabat not in (None, "") and _has_talabat_field():
        filters["custom_on_talabat"] = 1 if _bool(on_talabat) else 0
    if not_suitable not in (None, "") and _has_verdict_field():
        filters["custom_not_suitable"] = 1 if _bool(not_suitable) else 0
    if not _bool(include_merged) and _has_merge_field():
        filters["custom_merged_into"] = ["is", "not set"]

    rows = frappe.get_all(
        "Lead",
        filters=filters or None,
        fields=_lead_query_fields(),
        order_by="custom_fit_score desc",
        limit_page_length=0,
    )

    leads = [_map_lead_row(row) for row in rows]
    _attach_journey_summaries(leads)
    _attach_contacts(leads)
    return {"leads": leads, "count": len(leads)}


def _journey_summary_defaults():
    """The journey keys every catalog row carries, notes or not."""
    return {
        "journey_count": 0,
        "last_journey_date": None,
        "last_journey_type": None,
        "last_journey_note": None,
        "last_journey_contact": None,
        "next_action_date": None,
        "next_action": None,
    }


def _attach_journey_summaries(leads):
    """Merge each lead's journey summary (last touch + next action) into its row.

    ONE query for the whole catalog, not one per lead: the corpus runs to
    thousands of rows and the notes table is a fraction of that. Best-effort --
    a site that has not migrated the journey DocType just gets the defaults.
    """
    for lead in leads:
        lead.update(_journey_summary_defaults())
    if not leads:
        return
    try:
        from jarz_pos.api.journey import journey_summaries

        summaries = journey_summaries("Lead", [row.get("name") for row in leads])
    except Exception:
        frappe.log_error(
            title="leads: journey summaries failed",
            message=frappe.get_traceback(),
        )
        return
    if not summaries:
        return
    for lead in leads:
        summary = summaries.get(lead.get("name"))
        if summary:
            lead.update(summary)


# ---------------------------------------------------------------------------
# Detail
# ---------------------------------------------------------------------------
@frappe.whitelist()
def get_lead(name):
    """Full detail for one Lead: catalog fields + branches + addresses + notes.

    Returns all ``get_leads`` fields plus:
      - ``contacts``: list of mapped Jarz Lead Contact child rows (people).
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
                # Coerced, not passed through: a Frappe Check column reads back
                # as 0/1, and the client decodes this key as a real bool.
                "on_talabat": _bool(row.get("on_talabat")),
            }
        )
    result["branches"] = branches

    # People at the venue (custom_contacts child table).
    result["contacts"] = _lead_contacts(doc)

    # Linked ERPNext Address records (via Dynamic Link).
    result["primary_address"] = _lead_address(name, "is_primary_address")
    result["shipping_address"] = _lead_address(name, "is_shipping_address")

    # Editable rep notes.
    result["notes"] = doc.get("custom_notes") or ""

    # The dated field diary: every visit/call logged against this lead, newest
    # first, plus the same compact summary the catalog rows carry.
    result.update(_journey_summary_defaults())
    result["journey_notes"] = _journey_notes(name)
    _fold_detail_journey_summary(result)

    return result


def _journey_notes(name, limit=200):
    """Journey diary entries for a Lead. Guarded -> [] (never raises)."""
    try:
        from jarz_pos.api.journey import journey_notes_for

        return journey_notes_for("Lead", name, limit=limit)
    except Exception:
        frappe.log_error(
            title="leads: journey notes lookup failed",
            message=frappe.get_traceback(),
        )
        return []


def _fold_detail_journey_summary(result):
    """Derive the card summary from the notes already loaded (no second query)."""
    try:
        from jarz_pos.api.journey import journey_summary_from_notes

        summary = journey_summary_from_notes(result.get("journey_notes") or [])
    except Exception:
        return
    if summary:
        result.update(summary)


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
    "on_talabat": "custom_on_talabat",
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
    "talabat_areas": "custom_talabat_areas",
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
            if field in ("custom_is_specialty", "custom_on_talabat"):
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

    # Contacts child table (replace wholesale when provided). Omitting the key
    # leaves the existing people untouched, so the catalog importer and every
    # partial edit the app sends are both safe.
    if "contacts" in payload and _has_contacts_field():
        _apply_contacts(doc, payload.get("contacts"))

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


@frappe.whitelist()
def save_lead_contacts(name, contacts):
    """Replace the people recorded against a Lead. Returns the stored rows.

    Its own endpoint rather than a ``save_lead`` payload key so the app can add
    the barista it just met without re-sending (and racing) every other field
    on the lead.

    Returns: ``{"contacts": [<mapped row>, ...], "phone": <lead phone>}``.
    """
    _ensure_b2b_access()

    if not frappe.db.exists("Lead", name):
        frappe.throw(f"Lead '{name}' not found.")
    if not _has_contacts_field():
        frappe.throw(
            "This site has not migrated the lead contacts table yet. "
            "Run `bench migrate` and try again."
        )

    doc = frappe.get_doc("Lead", name)
    rows = _apply_contacts(doc, contacts)
    doc.save(ignore_permissions=True)

    return {"contacts": rows, "phone": doc.get("phone") or ""}


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
    row = _map_lead_row(flat)
    row["contacts"] = _lead_contacts(doc)
    return {"lead": row}


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
# Merge duplicates
# ---------------------------------------------------------------------------
# The catalog was scraped per-location, so one brand with branches in several
# areas can arrive as several Leads. Merging folds the duplicates' branches and
# contact details into one surviving Lead and parks the others.
#
# Sources are NEVER deleted. A Lead can be referenced by Opportunities, ToDos,
# Addresses and Contacts, so deletion would either fail or orphan them; and a
# merge is a judgement call someone may need to audit or undo. Branch rows are
# COPIED, not moved, so clearing custom_merged_into in Desk restores a usable
# source record.

_MERGE_CONTACT_FIELDS = (
    "phone",
    "mobile_no",
    "email_id",
    "website",
    "custom_instagram",
    "custom_facebook",
    "custom_maps_url",
    "custom_primary_area",
    "custom_price_band",
    "custom_confidence",
    "custom_open_status",
    "custom_latitude",
    "custom_longitude",
)


def _branch_key(row):
    """Identity of a branch row for dedup: name+area, else the maps URL."""
    def norm(value):
        return " ".join(str(value or "").split()).strip().lower()

    name = norm(row.get("branch_name"))
    area = norm(row.get("area"))
    if name or area:
        return f"{name}|{area}"
    return f"url|{norm(row.get('maps_url'))}"


def _branch_dict(row):
    """A Jarz Lead Branch row reduced to the fields we copy."""
    return {f: row.get(f) for f in _BRANCH_FIELDS}


@frappe.whitelist()
def get_merge_candidates(name, query=None, limit=25):
    """Leads that could be duplicates of ``name`` (or a free-text search).

    With no ``query`` this suggests likely duplicates, matched on the signals
    that actually identify a brand across scraped locations: an identical
    normalized brand name, or a shared phone / Instagram handle / website.
    With a ``query`` it is a plain name search, because the rep sometimes knows
    the duplicate the heuristics miss (a spelling variant, an Arabic name).

    Never returns the lead itself, anything already merged away, or anything
    that has other leads merged INTO it (merging a target into a third lead
    would strand the first merge's audit trail).

    Returns: ``{"candidates": [{name, lead_name, category, branch_count,
    primary_area, phone, instagram, score, reasons: [...]}, ...]}``.
    """
    _ensure_b2b_access()

    if not frappe.db.exists("Lead", name):
        frappe.throw(f"Lead '{name}' not found.")

    try:
        limit = max(1, min(int(limit or 25), 100))
    except (ValueError, TypeError):
        limit = 25

    fields = [
        "name",
        "lead_name",
        "custom_lead_category",
        "custom_branch_count",
        "custom_primary_area",
        "phone",
        "mobile_no",
        "website",
        "custom_instagram",
    ]

    base_filters = {"name": ["!=", name]}
    if _has_merge_field():
        base_filters["custom_merged_into"] = ["is", "not set"]

    query = (query or "").strip()
    if query:
        rows = frappe.get_all(
            "Lead",
            filters={**base_filters, "lead_name": ["like", f"%{query}%"]},
            fields=fields,
            order_by="lead_name asc",
            limit_page_length=limit,
        )
        candidates = [
            dict(_map_candidate(r), score=0, reasons=["Name search"]) for r in rows
        ]
        return {"candidates": _drop_merge_targets(candidates)}

    source = frappe.db.get_value(
        "Lead",
        name,
        ["lead_name", "phone", "mobile_no", "website", "custom_instagram"],
        as_dict=True,
    ) or {}

    # Each signal is queried separately and the results are unioned, so a lead
    # matching on two signals scores higher than one matching on either alone.
    signals = []
    lead_name = (source.get("lead_name") or "").strip()
    if lead_name:
        signals.append(("Same brand name", {"lead_name": lead_name}))
    for field, label in (
        ("phone", "Same phone"),
        ("mobile_no", "Same mobile"),
        ("custom_instagram", "Same Instagram"),
        ("website", "Same website"),
    ):
        value = (source.get(field) or "").strip()
        if value:
            signals.append((label, {field: value}))

    by_name = {}
    for label, extra in signals:
        try:
            rows = frappe.get_all(
                "Lead",
                filters={**base_filters, **extra},
                fields=fields,
                limit_page_length=limit,
            )
        except Exception:
            continue
        for row in rows:
            entry = by_name.setdefault(
                row["name"], dict(_map_candidate(row), score=0, reasons=[])
            )
            entry["score"] += 1
            entry["reasons"].append(label)

    candidates = sorted(
        by_name.values(),
        key=lambda c: (-c["score"], c.get("lead_name") or ""),
    )[:limit]
    return {"candidates": _drop_merge_targets(candidates)}


def _map_candidate(row):
    return {
        "name": row.get("name"),
        "lead_name": row.get("lead_name") or "",
        "category": row.get("custom_lead_category") or "",
        "branch_count": _int(row.get("custom_branch_count")),
        "primary_area": row.get("custom_primary_area") or "",
        "phone": row.get("phone") or row.get("mobile_no") or "",
        "instagram": row.get("custom_instagram") or "",
    }


def _drop_merge_targets(candidates):
    """Remove candidates that already have other leads merged into them."""
    if not candidates or not _has_merge_field():
        return candidates
    names = [c["name"] for c in candidates]
    try:
        taken = {
            r["custom_merged_into"]
            for r in frappe.get_all(
                "Lead",
                filters={"custom_merged_into": ["in", names]},
                fields=["custom_merged_into"],
                limit_page_length=0,
            )
        }
    except Exception:
        return candidates
    return [c for c in candidates if c["name"] not in taken]


@frappe.whitelist()
def merge_leads(target, sources):
    """Fold duplicate Leads into ``target``. Returns the refreshed target row.

    Copies every source branch and every source PERSON the target does not
    already have (contacts dedup on phone, else name+role), unions the
    area/region/governorate lists, fills BLANK target contact fields from the
    sources (never overwrites a value the target already has — the target is
    the record the rep chose to keep), appends source notes with attribution,
    and recomputes branch_count / total_reviews / avg_rating.

    Each source is then stamped with ``custom_merged_into`` and parked at the
    Lost/On-hold stage, which takes it out of the catalog and off the board.

    Returns: ``{"lead": <mapped target row>, "merged": [<source name>, ...]}``.
    """
    _ensure_b2b_access()

    if not _has_merge_field():
        frappe.throw(
            "This site has not migrated the merge fields yet. "
            "Run `bench migrate` and try again."
        )

    if isinstance(sources, str):
        try:
            parsed = json.loads(sources)
        except (ValueError, TypeError):
            parsed = [s.strip() for s in sources.split(",")]
        sources = parsed
    if isinstance(sources, str) or not isinstance(sources, (list, tuple)):
        frappe.throw("sources must be a list of Lead names.")

    sources = [str(s).strip() for s in sources if str(s or "").strip()]
    sources = list(dict.fromkeys(sources))  # de-dup, keep order
    if not sources:
        frappe.throw("Select at least one lead to merge.")
    if not frappe.db.exists("Lead", target):
        frappe.throw(f"Lead '{target}' not found.")
    if target in sources:
        frappe.throw("A lead cannot be merged into itself.")
    for source in sources:
        if not frappe.db.exists("Lead", source):
            frappe.throw(f"Lead '{source}' not found.")
        if frappe.db.get_value("Lead", source, "custom_merged_into"):
            frappe.throw(f"Lead '{source}' has already been merged.")
    # Merging INTO a lead that is itself a duplicate would hide the result.
    if frappe.db.get_value("Lead", target, "custom_merged_into"):
        frappe.throw(
            f"Lead '{target}' has itself been merged into another lead; "
            "merge into the surviving lead instead."
        )

    doc = frappe.get_doc("Lead", target)
    seen_branches = {
        _branch_key(row) for row in (doc.get("custom_branches") or [])
    }
    merging_contacts = _has_contacts_field()
    seen_contacts = (
        {_contact_key(row) for row in (doc.get("custom_contacts") or [])}
        if merging_contacts
        else set()
    )
    target_has_primary = any(
        _bool(row.get("is_primary")) for row in (doc.get("custom_contacts") or [])
    )
    areas = {v for v in _json_list(doc.get("custom_areas")) if v}
    regions = {v for v in _json_list(doc.get("custom_regions")) if v}
    governorates = {v for v in _json_list(doc.get("custom_governorates")) if v}
    note_blocks = []
    sahel = _int(doc.get("custom_sahel_branches"))
    # Seed the rating pool with the target's own aggregate so a target that has
    # no branch rows still contributes its rating to the merged average.
    rating_pool = _rating_pool_from_lead(doc)

    for source_name in sources:
        source = frappe.get_doc("Lead", source_name)

        for row in (source.get("custom_branches") or []):
            key = _branch_key(row)
            if key in seen_branches:
                continue
            seen_branches.add(key)
            doc.append("custom_branches", _branch_dict(row))

        if merging_contacts:
            for row in (source.get("custom_contacts") or []):
                mapped = _map_contact_row(row)
                if not mapped["contact_name"] and not mapped["phone"]:
                    continue
                key = _contact_key(mapped)
                if key in seen_contacts:
                    continue
                seen_contacts.add(key)
                # The surviving lead keeps its own primary; a merged-in person
                # joins as an ordinary contact.
                if target_has_primary:
                    mapped["is_primary"] = False
                elif mapped["is_primary"]:
                    target_has_primary = True
                doc.append("custom_contacts", mapped)

        areas.update(v for v in _json_list(source.get("custom_areas")) if v)
        regions.update(v for v in _json_list(source.get("custom_regions")) if v)
        governorates.update(
            v for v in _json_list(source.get("custom_governorates")) if v
        )
        sahel += _int(source.get("custom_sahel_branches"))
        rating_pool.extend(_rating_pool_from_lead(source))

        for field in _MERGE_CONTACT_FIELDS:
            if not doc.get(field) and source.get(field):
                doc.set(field, source.get(field))

        source_notes = (source.get("custom_notes") or "").strip()
        if source_notes:
            label = source.get("lead_name") or source_name
            note_blocks.append(f"[merged from {label}] {source_notes}")

    doc.set("custom_areas", json.dumps(sorted(areas)))
    doc.set("custom_regions", json.dumps(sorted(regions)))
    doc.set("custom_governorates", json.dumps(sorted(governorates)))
    doc.set("custom_sahel_branches", sahel)

    branches = doc.get("custom_branches") or []
    if branches:
        doc.set("custom_branch_count", len(branches))
        reviews = sum(_int(b.get("reviews")) for b in branches)
        if reviews:
            doc.set("custom_total_reviews", reviews)

    average = _weighted_average(rating_pool)
    if average is not None:
        doc.set("custom_avg_rating", average)

    if note_blocks:
        existing = (doc.get("custom_notes") or "").strip()
        doc.set(
            "custom_notes",
            "\n\n".join([b for b in [existing, *note_blocks] if b]),
        )

    doc.save(ignore_permissions=True)

    stamp = frappe.utils.now()
    for source_name in sources:
        values = {
            "custom_merged_into": target,
            "custom_merged_on": stamp,
            "custom_merged_by": frappe.session.user,
        }
        if _has_field("Lead", "custom_b2b_stage"):
            values["custom_b2b_stage"] = _NOT_SUITABLE_STAGE
        frappe.db.set_value("Lead", source_name, values, update_modified=True)
        _close_source_followups(source_name)

    flat = {f: doc.get(f) for f in _lead_query_fields()}
    flat["name"] = doc.name
    row = _map_lead_row(flat)
    row["contacts"] = _lead_contacts(doc)
    return {"lead": row, "merged": sources}


def _rating_pool_from_lead(doc):
    """(rating, weight) pairs for a lead's average-rating contribution.

    Prefers the branch rows, because after a merge the branches ARE the brand;
    falls back to the lead's own aggregate when it carries no branch ratings,
    so a lead imported without branch detail is not silently dropped from the
    average. Weight is the review count, or 1 when reviews are unknown, so an
    unreviewed location still counts for something but never dominates.
    """
    pool = []
    for row in (doc.get("custom_branches") or []):
        rating = _float_or_none(row.get("rating"))
        if rating is None:
            continue
        pool.append((rating, _int(row.get("reviews")) or 1))
    if pool:
        return pool
    rating = _float_or_none(doc.get("custom_avg_rating"))
    if rating is None:
        return []
    return [(rating, _int(doc.get("custom_total_reviews")) or 1)]


def _weighted_average(pool):
    """Review-weighted mean of (rating, weight) pairs, rounded to 2dp."""
    if not pool:
        return None
    weight = sum(w for _, w in pool)
    if weight <= 0:
        return None
    return round(sum(r * w for r, w in pool) / weight, 2)


def _close_source_followups(source_name):
    """A merged-away duplicate must stop generating its own reminders."""
    try:
        if _has_field("Lead", "custom_next_followup_date"):
            frappe.db.set_value(
                "Lead", source_name, "custom_next_followup_date", None,
                update_modified=False,
            )
    except Exception:
        frappe.log_error(
            title="leads.merge_leads: clear source follow-up date failed",
            message=frappe.get_traceback(),
        )
    try:
        from jarz_pos.api.crm import _close_open_todos

        _close_open_todos("Lead", source_name)
    except Exception:
        frappe.log_error(
            title="leads.merge_leads: close source ToDos failed",
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
