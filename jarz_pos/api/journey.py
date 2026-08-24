"""Journey notes API for Jarz POS (role-gated, B2B).

The rep's field diary. Every visit, call, WhatsApp reply or sample drop becomes
one dated ``Jarz Journey Note`` row hanging off a Lead, Opportunity or Customer,
carrying WHO was spoken to (``contact_person`` / ``contact_role``), WHAT was
said (``note``), and WHAT HAPPENS NEXT (``next_action`` on ``next_action_date``).

Design notes:
  - Gated by ``_ensure_b2b_access()`` -- the exact gate every other B2B endpoint
    uses. Editing and deleting are further restricted to the note's author or a
    manager; a rep may not rewrite a colleague's diary.
  - Every read is guarded on the DocType existing. Code ships before
    ``bench migrate`` runs, so the enrichment helpers below must degrade to an
    empty summary rather than raise "Unknown table" during that window -- but
    they LOG when the failure is anything other than a missing DocType, because
    a silent ``except: return {}`` is how a real bug hides for a month.
  - A ``next_action_date`` is not decoration: the DocType controller stamps it
    onto the referenced record's ``custom_next_followup_date`` so the existing
    daily reminder passes and the "My follow-ups" feed pick it up.
"""

import frappe

# Reuse the CRM access gate verbatim; never reinvent the B2B gating here.
from jarz_pos.api.crm import _ensure_b2b_access, _has_field, _manager_roles

JOURNEY_DOCTYPE = "Jarz Journey Note"

# Reference types a note may hang off. Kept in lockstep with the DocType's
# ``reference_doctype`` Select options.
REFERENCE_DOCTYPES = ("Lead", "Opportunity", "Customer")

# Fallback Select options, used when the live meta cannot be read (pre-migrate).
ENTRY_TYPES = [
    "Visit",
    "Call",
    "WhatsApp",
    "Sample Drop",
    "Meeting",
    "Email",
    "Other",
]

OUTCOMES = [
    "Interested",
    "Needs Follow-up",
    "Sample Requested",
    "Order Placed",
    "Not Now",
    "Rejected",
]

_NOTE_FIELDS = [
    "name",
    "reference_doctype",
    "reference_name",
    "entry_date",
    "entry_type",
    "note",
    "contact_person",
    "contact_role",
    "contact_phone",
    "next_action",
    "next_action_date",
    "outcome",
    "logged_by",
    "logged_by_name",
    "creation",
    "modified",
]

# Newest touch first. ``creation`` breaks ties so two notes logged on the same
# day still read in the order they were written.
_ORDER_BY = "entry_date desc, creation desc"

# How much of a note body the compact card summary carries.
_SNIPPET_CHARS = 160

# Rows the board summary reads at once. Enrichment is best-effort, so a
# pathological volume of notes must not turn one board load into a huge query.
_SUMMARY_LIMIT = 5000


# ---------------------------------------------------------------------------
# Guards / mapping
# ---------------------------------------------------------------------------
def journey_enabled():
    """Whether the site has migrated the journey DocType. Guarded -> False."""
    try:
        return bool(frappe.db.exists("DocType", JOURNEY_DOCTYPE))
    except Exception:
        return False


def _logger():
    return frappe.logger("jarz_journey", allow_site=True)


def _str_or_none(value):
    return str(value) if value else None


def _today():
    try:
        from frappe.utils import today

        return today()
    except Exception:
        return None


def _map_note(row):
    """Map a DocType row to the JSON shape the app consumes."""
    return {
        "name": row.get("name"),
        "reference_doctype": row.get("reference_doctype"),
        "reference_name": row.get("reference_name"),
        "entry_date": _str_or_none(row.get("entry_date")),
        "entry_type": row.get("entry_type") or "",
        "note": row.get("note") or "",
        "contact_person": row.get("contact_person") or "",
        "contact_role": row.get("contact_role") or "",
        "contact_phone": row.get("contact_phone") or "",
        "next_action": row.get("next_action") or "",
        "next_action_date": _str_or_none(row.get("next_action_date")),
        "outcome": row.get("outcome") or "",
        "logged_by": row.get("logged_by") or "",
        "logged_by_name": row.get("logged_by_name") or row.get("logged_by") or "",
        "creation": _str_or_none(row.get("creation")),
        "modified": _str_or_none(row.get("modified")),
        "can_edit": _can_edit_row(row),
    }


def _can_edit_row(row):
    """Author-or-manager. Managers may always edit; a rep only their own notes."""
    try:
        roles = set(frappe.get_roles(frappe.session.user) or [])
        if roles.intersection(_manager_roles()):
            return True
        return (row.get("logged_by") or "") == frappe.session.user
    except Exception:
        return False


def _ensure_reference(reference_doctype, reference_name):
    if reference_doctype not in REFERENCE_DOCTYPES:
        frappe.throw(
            "reference_doctype must be one of: " + ", ".join(REFERENCE_DOCTYPES)
        )
    if not reference_name:
        frappe.throw("reference_name is required.")
    if not frappe.db.exists(reference_doctype, reference_name):
        frappe.throw(f"{reference_doctype} '{reference_name}' not found.")


def _ensure_journey_doctype():
    if not journey_enabled():
        frappe.throw(
            "Journey notes are not available on this site yet "
            "(run `bench migrate` to install the Jarz Journey Note DocType)."
        )


def _select_options(fieldname, fallback):
    """Live Select options for a journey field, falling back to the constant."""
    try:
        field = frappe.get_meta(JOURNEY_DOCTYPE).get_field(fieldname)
        if field and field.options:
            opts = [o.strip() for o in field.options.split("\n") if o.strip()]
            if opts:
                return opts
    except Exception:
        pass
    return list(fallback)


# ---------------------------------------------------------------------------
# Read
# ---------------------------------------------------------------------------
@frappe.whitelist()
def get_journey_notes(reference_doctype, reference_name, limit=100):
    """Every journey note on one record, newest touch first.

    Returns: ``{"notes": [<mapped note>, ...], "count": <int>}``.
    """
    _ensure_b2b_access()
    _ensure_reference(reference_doctype, reference_name)

    notes = journey_notes_for(reference_doctype, reference_name, limit=limit)
    return {"notes": notes, "count": len(notes)}


@frappe.whitelist()
def get_journey_options():
    """Select options for the note editor: ``{"entry_types": [...], "outcomes": [...]}``."""
    _ensure_b2b_access()
    return {
        "entry_types": _select_options("entry_type", ENTRY_TYPES),
        # The DocType's first option is the empty "not set" choice; the app
        # renders that itself, so strip it here.
        "outcomes": [o for o in _select_options("outcome", OUTCOMES) if o],
    }


def journey_notes_for(reference_doctype, reference_name, limit=100):
    """Mapped notes for one record. Guarded -> [] (never raises)."""
    if not journey_enabled():
        return []
    try:
        rows = frappe.get_all(
            JOURNEY_DOCTYPE,
            filters={
                "reference_doctype": reference_doctype,
                "reference_name": reference_name,
            },
            fields=_NOTE_FIELDS,
            order_by=_ORDER_BY,
            limit_page_length=int(limit or 0) or 0,
        )
    except Exception:
        _logger().error(
            f"journey_notes_for({reference_doctype}, {reference_name}) failed",
            exc_info=True,
        )
        return []
    return [_map_note(r) for r in rows]


def journey_summaries(reference_doctype, reference_names=None):
    """Compact per-record journey summary, for boards and list screens.

    One query for the whole board rather than one per card. Returns a dict keyed
    by ``reference_name``; records with no notes are simply absent, so callers
    should ``.get(name)`` and treat a miss as "no journey yet".

    Each value:
        {
            "journey_count": int,
            "last_journey_date": str|None,
            "last_journey_type": str|None,
            "last_journey_note": str|None,      # truncated snippet
            "last_journey_contact": str|None,
            "next_action_date": str|None,
            "next_action": str|None,
        }

    ``next_action_date`` is "what's next": the EARLIEST pending action dated
    today or later, and when nothing is pending, the most recent overdue one --
    so a card shows the thing that actually needs doing, not whichever note was
    written last.
    """
    if not journey_enabled():
        return {}

    filters = {"reference_doctype": reference_doctype}
    names = [n for n in (reference_names or []) if n]
    if names:
        filters["reference_name"] = ["in", names]

    try:
        rows = frappe.get_all(
            JOURNEY_DOCTYPE,
            filters=filters,
            fields=[
                "reference_name",
                "entry_date",
                "entry_type",
                "note",
                "contact_person",
                "next_action",
                "next_action_date",
            ],
            order_by=_ORDER_BY,
            limit_page_length=_SUMMARY_LIMIT,
        )
    except Exception:
        _logger().error(
            f"journey_summaries({reference_doctype}) failed", exc_info=True
        )
        return {}

    grouped = {}
    for row in rows:
        key = row.get("reference_name")
        if key:
            grouped.setdefault(key, []).append(row)

    return {
        key: journey_summary_from_notes(group) for key, group in grouped.items()
    }


def journey_summary_from_notes(notes):
    """Build the compact summary from rows/notes ALREADY sorted newest-first.

    Shared by :func:`journey_summaries` (which queries) and by callers that have
    just loaded the full note list anyway (``leads.get_lead``), so the detail
    view derives its summary without a second query. Returns ``None`` for an
    empty list.
    """
    if not notes:
        return None

    today = _today()
    first = notes[0]
    # Newest-first ordering means the first row IS the last touch.
    entry = {
        "journey_count": 0,
        "last_journey_date": _str_or_none(first.get("entry_date")),
        "last_journey_type": first.get("entry_type") or "",
        "last_journey_note": _snippet(first.get("note")),
        "last_journey_contact": first.get("contact_person") or "",
        "next_action_date": None,
        "next_action": None,
    }
    for row in notes:
        entry["journey_count"] += 1
        _fold_next_action(entry, row, today)
    return entry


def _snippet(text):
    """First _SNIPPET_CHARS characters of a note body, single-spaced."""
    raw = " ".join(str(text or "").split())
    if not raw:
        return ""
    if len(raw) <= _SNIPPET_CHARS:
        return raw
    return raw[: _SNIPPET_CHARS - 1].rstrip() + "…"


def _fold_next_action(entry, row, today):
    """Keep the most relevant next action on ``entry`` (see journey_summaries)."""
    candidate = row.get("next_action_date")
    if not candidate:
        return
    candidate = str(candidate)
    current = entry.get("next_action_date")

    def _pending(date):
        return bool(today) and date >= today

    if current is None:
        keep = True
    elif _pending(candidate) and not _pending(current):
        # A pending action always beats an overdue one.
        keep = True
    elif _pending(candidate) and _pending(current):
        keep = candidate < current  # soonest pending wins
    elif not _pending(candidate) and not _pending(current):
        keep = candidate > current  # most recent overdue wins
    else:
        keep = False

    if keep:
        entry["next_action_date"] = candidate
        entry["next_action"] = row.get("next_action") or ""


# ---------------------------------------------------------------------------
# Write
# ---------------------------------------------------------------------------
@frappe.whitelist()
def add_journey_note(
    reference_doctype,
    reference_name,
    note,
    entry_date=None,
    entry_type=None,
    contact_person=None,
    contact_role=None,
    contact_phone=None,
    next_action=None,
    next_action_date=None,
    outcome=None,
):
    """Log one dated touch on a Lead/Opportunity/Customer.

    Supplying ``next_action_date`` also schedules the follow-up on the
    referenced record (see the DocType controller).

    Returns the mapped note.
    """
    _ensure_b2b_access()
    _ensure_journey_doctype()
    _ensure_reference(reference_doctype, reference_name)

    if not (note or "").strip():
        frappe.throw("note is required.")

    doc = frappe.get_doc(
        {
            "doctype": JOURNEY_DOCTYPE,
            "reference_doctype": reference_doctype,
            "reference_name": reference_name,
            "note": note,
            "entry_date": (entry_date or "").strip() or _today(),
            "entry_type": (entry_type or "").strip() or "Visit",
            "contact_person": (contact_person or "").strip() or None,
            "contact_role": (contact_role or "").strip() or None,
            "contact_phone": (contact_phone or "").strip() or None,
            "next_action": (next_action or "").strip() or None,
            "next_action_date": (next_action_date or "").strip() or None,
            "outcome": (outcome or "").strip() or None,
        }
    )
    doc.insert(ignore_permissions=True)

    return _map_note(doc.as_dict())


@frappe.whitelist()
def update_journey_note(
    name,
    note=None,
    entry_date=None,
    entry_type=None,
    contact_person=None,
    contact_role=None,
    contact_phone=None,
    next_action=None,
    next_action_date=None,
    outcome=None,
):
    """Patch one journey note. Only the keys actually supplied are written.

    A field is cleared by passing an empty string; omitting it (``None``) leaves
    it alone. ``note`` may never be blanked -- an empty diary entry is not an
    edit, it is a delete.

    Access: the note's author, or a manager.
    """
    _ensure_b2b_access()
    _ensure_journey_doctype()
    if not frappe.db.exists(JOURNEY_DOCTYPE, name):
        frappe.throw(f"Journey note '{name}' not found.")
    _ensure_can_edit(name)

    doc = frappe.get_doc(JOURNEY_DOCTYPE, name)

    if note is not None:
        if not str(note).strip():
            frappe.throw("note cannot be empty.")
        doc.note = note
    if entry_date is not None:
        doc.entry_date = str(entry_date).strip() or doc.entry_date
    if entry_type is not None:
        doc.entry_type = str(entry_type).strip() or "Visit"
    for field, value in (
        ("contact_person", contact_person),
        ("contact_role", contact_role),
        ("contact_phone", contact_phone),
        ("next_action", next_action),
        ("next_action_date", next_action_date),
        ("outcome", outcome),
    ):
        if value is not None:
            setattr(doc, field, str(value).strip() or None)

    doc.save(ignore_permissions=True)

    return _map_note(doc.as_dict())


@frappe.whitelist()
def delete_journey_note(name):
    """Delete one journey note. Access: the note's author, or a manager.

    Returns: ``{"ok": True}``.
    """
    _ensure_b2b_access()
    _ensure_journey_doctype()
    if not frappe.db.exists(JOURNEY_DOCTYPE, name):
        frappe.throw(f"Journey note '{name}' not found.")
    _ensure_can_edit(name)

    frappe.delete_doc(JOURNEY_DOCTYPE, name, ignore_permissions=True)
    return {"ok": True}


def _ensure_can_edit(name):
    """Raise unless the caller authored the note or is a manager."""
    try:
        logged_by = frappe.db.get_value(JOURNEY_DOCTYPE, name, "logged_by")
    except Exception:
        logged_by = None
    if not _can_edit_row({"logged_by": logged_by}):
        frappe.throw(
            "Not permitted: only the rep who logged this note (or a manager) "
            "may change it."
        )


# ---------------------------------------------------------------------------
# Contacts (who the note was spoken to)
# ---------------------------------------------------------------------------
# The editor's WHO box picks from the people already recorded on the account
# instead of retyping a name every visit. Those people live in exactly one
# place -- ``Lead.custom_contacts`` (see ``leads.save_lead_contacts``) -- so
# these endpoints resolve the backing Lead and then delegate to the leads
# helpers. They never own a second roster: a contact added from the diary is
# the same row the lead page's contacts section shows.

def _contacts_lead(reference_doctype, reference_name):
    """The Lead whose people back this record's contact picker. Guarded -> None.

    A note hangs off a Lead, an Opportunity or a Customer, but the venue's
    people only ever live on the Lead. An Opportunity made from a lead and a
    Customer converted from one both point back at it, so the roster follows
    the account down the funnel rather than resetting the moment it converts.
    """
    try:
        if reference_doctype == "Lead":
            return reference_name

        if reference_doctype == "Opportunity":
            row = frappe.db.get_value(
                "Opportunity",
                reference_name,
                ["opportunity_from", "party_name"],
                as_dict=True,
            )
            lead = (row or {}).get("party_name")
            if (row or {}).get("opportunity_from") == "Lead" and lead:
                return lead if frappe.db.exists("Lead", lead) else None
            return None

        if reference_doctype == "Customer" and _has_field("Customer", "lead_name"):
            lead = frappe.db.get_value("Customer", reference_name, "lead_name")
            if lead and frappe.db.exists("Lead", lead):
                return lead
    except Exception:
        _logger().error(
            f"_contacts_lead({reference_doctype}, {reference_name}) failed",
            exc_info=True,
        )
    return None


def _contacts_payload(lead):
    """``{"contacts": [...], "lead": str, "can_add": bool}`` for one lead.

    Guarded end to end: an account with no backing lead, or a site that has not
    migrated the child table yet, gets an empty list and ``can_add`` False --
    the editor then simply keeps its free-text boxes.
    """
    from jarz_pos.api.leads import _has_contacts_field, _lead_contacts

    if not lead or not _has_contacts_field():
        return {"contacts": [], "lead": lead or "", "can_add": False}

    try:
        rows = _lead_contacts(frappe.get_doc("Lead", lead))
    except Exception:
        _logger().error(f"_contacts_payload({lead}) failed", exc_info=True)
        return {"contacts": [], "lead": lead, "can_add": True}

    # Primary first; the rest keep the order the rep arranged on the lead page.
    rows.sort(key=lambda row: 0 if row.get("is_primary") else 1)
    return {"contacts": rows, "lead": lead, "can_add": True}


@frappe.whitelist()
def get_journey_contacts(reference_doctype, reference_name):
    """The people on this account, for the note editor's WHO picker.

    Returns: ``{"contacts": [<mapped contact>, ...], "lead": str, "can_add": bool}``.
    """
    _ensure_b2b_access()
    _ensure_reference(reference_doctype, reference_name)

    return _contacts_payload(_contacts_lead(reference_doctype, reference_name))


@frappe.whitelist()
def add_journey_contact(
    reference_doctype,
    reference_name,
    contact_name=None,
    role=None,
    phone=None,
    email=None,
    notes=None,
):
    """Record a new person on this account, straight from the note editor.

    Appends to the backing Lead's ``custom_contacts`` rather than replacing it
    (``save_lead_contacts`` sends the whole table, which would race a rep
    editing the lead page). A person whose phone -- or, lacking one, whose
    name+role -- already matches an existing row is NOT duplicated; that row is
    returned instead, so tapping "add" twice is harmless.

    Returns the same payload as :func:`get_journey_contacts`, plus ``added``:
    the mapped row the caller should select.
    """
    _ensure_b2b_access()
    _ensure_reference(reference_doctype, reference_name)

    from jarz_pos.api.leads import (
        _contact_key,
        _has_contacts_field,
        _map_contact_row,
    )

    lead = _contacts_lead(reference_doctype, reference_name)
    if not lead:
        frappe.throw(
            "This account has no lead record to hold people. Add the contact "
            "on the lead itself."
        )
    if not _has_contacts_field():
        frappe.throw(
            "This site has not migrated the lead contacts table yet. "
            "Run `bench migrate` and try again."
        )

    row = _map_contact_row(
        {
            "contact_name": contact_name,
            "role": role,
            "phone": phone,
            "email": email,
            "notes": notes,
        }
    )
    if not row["contact_name"] and not row["phone"]:
        frappe.throw("A contact needs at least a name or a phone number.")

    doc = frappe.get_doc("Lead", lead)
    existing = doc.get("custom_contacts") or []
    match = next(
        (r for r in existing if _contact_key(r.as_dict()) == _contact_key(row)),
        None,
    )
    if match is not None:
        payload = _contacts_payload(lead)
        payload["added"] = _map_contact_row(match.as_dict())
        return payload

    # First person on the record is the one to ring first.
    row["is_primary"] = not existing
    doc.append("custom_contacts", dict(row))

    # Same back-fill rule as ``leads._apply_contacts``: a lead whose only
    # number lives on a person row would otherwise still look unreachable.
    if row["phone"] and not str(doc.get("phone") or "").strip():
        doc.set("phone", row["phone"])

    doc.save(ignore_permissions=True)

    payload = _contacts_payload(lead)
    payload["added"] = row
    return payload
