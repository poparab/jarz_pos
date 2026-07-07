"""Bench-run importer for the Jarz Leads catalog.

Upserts the standard ERPNext ``Lead`` DocType from a ``leads.json`` catalog
export, keyed on ``custom_source_brand_id`` (the source ``id``).

Run:
    bench execute jarz_pos.scripts.import_leads_catalog.run \
        --kwargs "{'json_path': '/path/leads.json'}"

leads.json shape:
    {"generated": <str>, "count": <int>, "leads": [ {...}, ... ]}

Idempotency contract (preserve rep-owned work):
  - On INITIAL CREATE only: seed ``status="Open"``, ``custom_b2b_stage="Lead"``,
    ``custom_notes`` (from JSON notes), ``custom_lead_category="Coffee"``, and
    create the primary Address from the primary branch address.
  - On UPDATE of an existing Lead: refresh catalog METRICS only. NEVER overwrite
    ``status``, ``custom_b2b_stage``, ``custom_notes``, ``custom_lead_category``,
    or addresses (all rep-owned).

This script is bench-run only. It is NOT whitelisted and NOT called from the app.
"""

import json
from urllib.parse import urlsplit, urlunsplit

import frappe

DEFAULT_LEAD_CATEGORY = "Coffee"

# Child (Jarz Lead Branch) fields we accept from each JSON branch object,
# excluding the special-cased branch_name / latitude / longitude / maps_url.
_BRANCH_PASSTHROUGH_FIELDS = (
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
    "address",
)

# Frappe Data fields cap at 140 chars; guard values so no record fails on length.
_MAX_DATA_LEN = 140
_BRANCH_DATA_FIELDS = ("area", "region", "governorate", "price", "status", "phone")


def _cap(value, n=_MAX_DATA_LEN):
    """Truncate an over-long value to fit a Data(140) field; pass None through."""
    if value is None:
        return None
    s = str(value)
    return s if len(s) <= n else s[:n]


def _fit_website(value):
    """Fit a website/URL into Data(140) without corrupting it: drop the
    query/fragment (usually UTM/tracking junk) first, then hard-truncate."""
    if value is None:
        return None
    s = str(value).strip()
    if len(s) <= _MAX_DATA_LEN:
        return s
    try:
        p = urlsplit(s)
        s = urlunsplit((p.scheme, p.netloc, p.path, "", ""))
    except Exception:
        pass
    return s if len(s) <= _MAX_DATA_LEN else s[:_MAX_DATA_LEN]


def run(json_path):
    """Entry point. Read the catalog JSON and upsert every lead."""
    with open(json_path, "r", encoding="utf-8") as fh:
        data = json.load(fh)

    leads = data.get("leads") or []
    _ensure_category(DEFAULT_LEAD_CATEGORY)

    created = 0
    updated = 0
    failed = 0

    for lead in leads:
        try:
            was_created = _upsert_lead(lead)
            if was_created:
                created += 1
            else:
                updated += 1
        except Exception:
            failed += 1
            frappe.log_error(
                title="import_leads_catalog: record failed",
                message=frappe.get_traceback(),
            )

    frappe.db.commit()

    summary = (
        f"import_leads_catalog: created {created}, updated {updated}, "
        f"failed {failed} (of {len(leads)})"
    )
    print(summary)
    return {
        "created": created,
        "updated": updated,
        "failed": failed,
        "total": len(leads),
    }


def _ensure_category(name):
    """Create-only guard for the Jarz Lead Category master."""
    if frappe.db.exists("Jarz Lead Category", name):
        return
    doc = frappe.get_doc({"doctype": "Jarz Lead Category", "category_name": name})
    doc.insert(ignore_permissions=True)


def _upsert_lead(lead):
    """Create or update one Lead from a JSON object. Returns True if created."""
    source_id = lead.get("id")
    existing_name = None
    if source_id is not None:
        existing_name = frappe.db.get_value(
            "Lead", {"custom_source_brand_id": source_id}, "name"
        )

    creating = not existing_name
    if creating:
        doc = frappe.new_doc("Lead")
    else:
        doc = frappe.get_doc("Lead", existing_name)

    # --- Catalog metrics (set on BOTH create and update) ------------------
    doc.custom_source_brand_id = source_id
    doc.lead_name = lead.get("name")
    doc.company_name = lead.get("name")
    # Catalog fit score lives on its OWN field so the nightly CRM job
    # (compute_lead_scores) can keep exclusive ownership of custom_lead_score
    # without clobbering the imported fit score. Refreshed on create AND update.
    doc.custom_fit_score = _int(lead.get("score"))
    doc.custom_fit_tier = lead.get("tier")
    doc.custom_branch_count = _int(lead.get("branchCount"))
    doc.custom_price_band = lead.get("price")
    doc.custom_avg_rating = _float(lead.get("rating"))
    doc.custom_total_reviews = _int(lead.get("reviews"))
    doc.custom_open_status = lead.get("openStatus")
    doc.custom_regions = json.dumps(_as_list(lead.get("regions")))
    doc.custom_sahel_branches = _int(lead.get("sahelBranches"))
    doc.custom_is_specialty = 1 if lead.get("isSpecialty") else 0
    doc.custom_primary_area = lead.get("primaryArea")
    doc.custom_areas = json.dumps(_as_list(lead.get("areas")))
    doc.custom_governorates = json.dumps(_as_list(lead.get("governorates")))
    doc.phone = lead.get("phone")
    doc.mobile_no = lead.get("phone")
    doc.website = _fit_website(lead.get("website"))
    doc.custom_instagram = lead.get("instagram")
    doc.custom_facebook = lead.get("facebook")
    doc.custom_maps_url = lead.get("mapsUrl")
    doc.custom_confidence = lead.get("confidence")
    doc.custom_last_verified = lead.get("lastVerified")

    # Branches child table (metrics: always refreshed).
    branches = lead.get("branches") or []
    doc.set("custom_branches", [])
    for b in branches:
        if not isinstance(b, dict):
            continue
        row = {
            "branch_name": _cap(b.get("name")),
            "latitude": _float(b.get("lat")),
            "longitude": _float(b.get("lng")),
            "maps_url": b.get("mapsUrl"),
        }
        for f in _BRANCH_PASSTHROUGH_FIELDS:
            if f in b:
                if f == "website":
                    row[f] = _fit_website(b.get(f))
                elif f in _BRANCH_DATA_FIELDS:
                    row[f] = _cap(b.get(f))
                else:
                    row[f] = b.get(f)
        doc.append("custom_branches", row)

    # Geo on the Lead from the primary branch (metric: always refreshed).
    primary_branch = _pick_primary_branch(branches, lead.get("primaryArea"))
    if primary_branch:
        doc.custom_latitude = _float(primary_branch.get("lat"))
        doc.custom_longitude = _float(primary_branch.get("lng"))

    # --- Rep-owned fields: seed on CREATE ONLY ----------------------------
    if creating:
        doc.status = "Open"
        doc.custom_b2b_stage = "Lead"
        doc.custom_notes = lead.get("notes") or ""
        doc.custom_lead_category = DEFAULT_LEAD_CATEGORY
        doc.insert(ignore_permissions=True)
        # Primary Address from the primary branch (create-only).
        if primary_branch:
            _create_primary_address(doc.name, doc.lead_name, primary_branch)
    else:
        doc.save(ignore_permissions=True)

    return creating


def _pick_primary_branch(branches, primary_area):
    """Return the primary branch dict: one matching primaryArea, else the first."""
    if not branches:
        return None
    if primary_area:
        for b in branches:
            if isinstance(b, dict) and b.get("area") == primary_area:
                return b
    first = branches[0]
    return first if isinstance(first, dict) else None


def _create_primary_address(lead_name, title, branch):
    """Create a primary Billing Address linked to the Lead from a branch dict."""
    address_line1 = str(branch.get("address") or "").strip()
    if not address_line1:
        # Fall back to the branch name so the mandatory line1 is populated.
        address_line1 = str(branch.get("name") or title or lead_name).strip()
    if not address_line1:
        return

    doc = frappe.new_doc("Address")
    doc.address_title = title or lead_name
    doc.address_type = "Billing"
    doc.address_line1 = address_line1
    doc.city = branch.get("area") or branch.get("region")
    doc.state = branch.get("governorate")
    if branch.get("phone"):
        doc.phone = branch.get("phone")
    doc.is_primary_address = 1
    doc.append("links", {"link_doctype": "Lead", "link_name": lead_name})
    doc.insert(ignore_permissions=True)


def _as_list(value):
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return list(value)
    return [value]


def _int(value):
    try:
        return int(value or 0)
    except (ValueError, TypeError):
        return 0


def _float(value):
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (ValueError, TypeError):
        return None
