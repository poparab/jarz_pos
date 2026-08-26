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

# The corpus stores the rating source lowercase; the Lead field is a Select whose
# options are title-cased. An unknown value maps to "" rather than throwing, so a
# future source name cannot fail a whole import.
_RATING_SOURCE = {"talabat": "Talabat", "google_maps": "Google Maps"}

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
    # Seed every category the export actually uses, not just the default, so a
    # new segment (e.g. tea/matcha/boba) lands in its own filterable category.
    _ensure_category(DEFAULT_LEAD_CATEGORY)
    for name in sorted({(l.get("category") or "").strip() for l in leads} - {""}):
        _ensure_category(name)

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

    promoted = _propagate_talabat_to_merge_targets()

    frappe.db.commit()

    summary = (
        f"import_leads_catalog: created {created}, updated {updated}, "
        f"failed {failed} (of {len(leads)}), talabat promoted {promoted}"
    )
    print(summary)
    return {
        "created": created,
        "updated": updated,
        "failed": failed,
        "total": len(leads),
        "talabat_promoted": promoted,
    }


def _propagate_talabat_to_merge_targets(only=None):
    """Move a merged duplicate's Talabat presence onto the lead that survived.

    The catalog is keyed on the Google-derived brand id, but a rep can merge two
    of those rows into one Lead when Google split a single business across branch
    names (PAO Boba & More is listed once per mall). The import then writes the
    flag onto the duplicate, which ``get_leads`` hides -- so the badge would never
    appear on the record anyone actually works.

    A merged duplicate IS the same business as its target, so its presence is the
    target's presence. Union rather than overwrite: the target may have been
    flagged from its own listing, and losing a zone would be a silent regression.

    ``only`` restricts the scan to the given Lead names. The import leaves it
    unset (every duplicate is fair game), but a TEST on a populated site MUST
    pass its own fixtures -- an unrestricted call from CI promotes real business
    records, which is how this suite once mutated live staging data.

    The count is rows actually mutated, so it does NOT drop to zero on a re-run
    of the full import: the merge target is itself a catalog row carrying
    ``onTalabat: false``, so each import resets it before this pass re-promotes
    it. The END STATE is what converges; the counter is not a no-op signal.

    Idempotent in effect, and a no-op on a site that has not migrated either
    field.
    """
    if not (_has_field("Lead", "custom_on_talabat")
            and _has_field("Lead", "custom_merged_into")):
        return 0

    filters = {"custom_on_talabat": 1, "custom_merged_into": ["is", "set"]}
    if only is not None:
        names = [only] if isinstance(only, str) else list(only)
        if not names:
            return 0
        filters["name"] = ["in", names]
    rows = frappe.get_all(
        "Lead",
        filters=filters,
        fields=["name", "custom_merged_into", "custom_talabat_areas"],
    )
    promoted = 0
    for row in rows:
        # Follow the chain: a duplicate can be merged into a lead that was itself
        # merged away. Bounded so a cycle can never hang the import.
        target, seen = row.custom_merged_into, {row.name}
        for _ in range(10):
            if not target or target in seen:
                target = None
                break
            seen.add(target)
            nxt = frappe.db.get_value("Lead", target, "custom_merged_into")
            if not nxt:
                break
            target = nxt
        if not target or not frappe.db.exists("Lead", target):
            continue

        current = frappe.db.get_value("Lead", target, "custom_talabat_areas")
        merged = sorted(set(_as_list_json(current)) | set(_as_list_json(row.custom_talabat_areas)))
        already = frappe.db.get_value("Lead", target, "custom_on_talabat")
        if int(already or 0) == 1 and merged == sorted(set(_as_list_json(current))):
            continue
        frappe.db.set_value(
            "Lead", target,
            {"custom_on_talabat": 1, "custom_talabat_areas": json.dumps(merged)},
            update_modified=False,
        )
        promoted += 1
    return promoted


def _as_list_json(value):
    """Parse a json.dumps([...]) column back to a list. Guarded -> []."""
    if not value:
        return []
    if isinstance(value, (list, tuple)):
        return [str(v) for v in value]
    try:
        parsed = json.loads(value)
    except (ValueError, TypeError):
        return []
    return [str(v) for v in parsed] if isinstance(parsed, list) else []


def _has_field(doctype, fieldname):
    """True if the site has this column yet (code ships before bench migrate)."""
    try:
        return bool(frappe.db.has_column(doctype, fieldname))
    except Exception:
        return False


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
    # Google service signals. Only ever set to 1 when Google positively confirms
    # it; a 0 means UNKNOWN, not "no" (the Places API omits these when false).
    # Guarded so this script still runs on a site that has not migrated yet.
    if _has_field("Lead", "custom_takeout"):
        doc.custom_takeout = 1 if lead.get("takeout") else 0
        doc.custom_dine_in = 1 if lead.get("dineIn") else 0
        doc.custom_serves_dessert = 1 if lead.get("servesDessert") else 0
    # Talabat presence is a CATALOG metric, not rep-owned: it comes from sweeping
    # the delivery app, so a later sweep must be able to refresh it. Set on both
    # create and update, like the Google signals above.
    if _has_field("Lead", "custom_on_talabat"):
        doc.custom_on_talabat = 1 if lead.get("onTalabat") else 0
        doc.custom_talabat_areas = json.dumps(_as_list(lead.get("talabatAreas")))
    if _has_field("Lead", "custom_talabat_rating"):
        doc.custom_talabat_rating = _float(lead.get("talabatRating"))
        doc.custom_talabat_reviews = _int(lead.get("talabatReviews"))
        doc.custom_talabat_rating_source = _RATING_SOURCE.get(
            (lead.get("talabatRatingSource") or "").lower(), ""
        )
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
            "on_talabat": 1 if b.get("onTalabat") else 0,
            "talabat_rating": _float(b.get("talabatRating")),
            "talabat_reviews": _int(b.get("talabatReviews")),
            "talabat_rating_source": b.get("talabatRatingSource") or "",
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
        doc.custom_lead_category = (lead.get("category") or "").strip() or DEFAULT_LEAD_CATEGORY
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
