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
  - ...and a promise can be KEPT. ``set_journey_action_done`` settles one
    action: it closes that note's reminder, then re-derives the record's
    follow-up fields from whatever is still pending, so a done action stops
    showing on the pipeline/lead cards while the note itself keeps reporting
    what was promised and when. ``crm.complete_followup`` is the record-level
    hammer; this is the per-action one.
  - ``get_action_calendar`` is the month view over both kinds of promise:
    journey next actions and record-level follow-ups, merged and deduped.
"""

import re

import frappe

# Reuse the CRM access gate verbatim; never reinvent the B2B gating here.
from jarz_pos.api.crm import _ensure_b2b_access, _has_field, _manager_roles

JOURNEY_DOCTYPE = "Jarz Journey Note"

# Reference types a note may hang off. Kept in lockstep with the DocType's
# ``reference_doctype`` Select options.
REFERENCE_DOCTYPES = ("Lead", "Opportunity", "Customer")

# Reference types that carry the CRM follow-up fields. A Customer has no
# ``custom_next_followup_date``, so a next action on a customer note is recorded
# and completable but drives no record-level reminder. Mirrors
# ``JarzJourneyNote._FOLLOWUP_DOCTYPES``.
FOLLOWUP_DOCTYPES = ("Lead", "Opportunity")

# The fields on each reference doctype that carry a human title, best first. One
# lookup per doctype resolves a whole calendar (see :func:`_attach_titles`).
#
# Opportunity lists ``customer_name`` ahead of ``party_name`` because
# ``party_name`` is a LINK: on a lead-sourced opportunity it holds
# "CRM-LEAD-2026-00042", which is not a title anybody can read. ERPNext fetches
# the human name into ``customer_name``, so that is preferred and party_name is
# only the fallback.
_TITLE_FIELDS = {
    "Lead": ("lead_name",),
    "Opportunity": ("customer_name", "party_name"),
    "Customer": ("customer_name",),
}

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
    "next_action_done",
    "next_action_done_on",
    "next_action_done_by",
    "outcome",
    "logged_by",
    "logged_by_name",
    "creation",
    "modified",
]

# The completion trio, which only exists after ``bench migrate``. Code always
# ships before the migration runs (and CI's logic gate runs entirely
# pre-migrate), so every SELECT that names these goes through
# :func:`_note_fields` / :func:`_has_done_fields` -- otherwise a whole note list
# would fail on "Unknown column" during that window.
_DONE_FIELDS = ("next_action_done", "next_action_done_on", "next_action_done_by")

# Newest touch first. ``creation`` breaks ties so two notes logged on the same
# day still read in the order they were written.
_ORDER_BY = "entry_date desc, creation desc"

# How much of a note body the compact card summary carries.
_SNIPPET_CHARS = 160

# Rows the board summary reads at once. Enrichment is best-effort, so a
# pathological volume of notes must not turn one board load into a huge query.
_SUMMARY_LIMIT = 5000

# Same guard rail for the calendar: a month of actions is tens of rows, but a
# caller asking for a decade must not be able to pull the whole table.
_CALENDAR_LIMIT = 5000

# Lazily built once per process by :func:`_journey_marker_parts` -- it reaches
# into ``crm.follow_ups`` and this module must import with no side effects.
_MARKER_PREFIX = ""
_MARKER_RE = None


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


def _bool(value):
    """Coerce a whitelisted-endpoint flag to a real bool.

    Frappe hands arguments over the wire as strings, so ``done="0"`` arrives
    here as the string ``"0"`` -- which is TRUTHY in Python. ``bool(value)``
    would therefore read "undo" as "complete". Mirrors ``leads._bool``, with the
    common word forms accepted too because the app is not the only caller.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        text = value.strip().lower()
        if text in ("", "0", "false", "no", "off"):
            return False
        if text in ("1", "true", "yes", "on"):
            return True
    try:
        return bool(int(value or 0))
    except (ValueError, TypeError):
        return bool(value)


def _today():
    try:
        from frappe.utils import today

        return today()
    except Exception:
        return None


def _has_done_fields():
    """Whether the site has migrated the next-action completion fields."""
    return _has_field(JOURNEY_DOCTYPE, "next_action_done")


def _note_fields():
    """``_NOTE_FIELDS``, minus anything this site has not migrated yet.

    Selecting a column that does not exist fails the WHOLE query, which would
    turn "the completion feature is not migrated yet" into "this account has no
    diary at all". Dropping the trio keeps every existing surface working during
    the deploy-then-migrate window; ``_map_note`` defaults them anyway.
    """
    if _has_done_fields():
        return list(_NOTE_FIELDS)
    return [f for f in _NOTE_FIELDS if f not in _DONE_FIELDS]


def _fullname(user):
    """Display name for a user id, memoised for the request. "" when unset.

    Memoised because ``can_complete``/owner names are resolved for every row of
    a note list or a month of calendar, and there are only ever a handful of
    distinct reps behind hundreds of rows.
    """
    if not user:
        return ""
    cache = getattr(frappe.local, "_jarz_journey_fullnames", None)
    if cache is None:
        cache = {}
        frappe.local._jarz_journey_fullnames = cache
    if user not in cache:
        try:
            cache[user] = frappe.utils.get_fullname(user) or user
        except Exception:
            _logger().error(f"_fullname({user}) failed", exc_info=True)
            cache[user] = user
    return cache[user]


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
        # A DONE note still reports its next action and its date: the promise
        # stays visible in the diary, it is just settled. Only the derived
        # surfaces (summaries, cards, reminders) drop it.
        "next_action_done": _bool(row.get("next_action_done")),
        "next_action_done_on": _str_or_none(row.get("next_action_done_on")),
        "next_action_done_by": row.get("next_action_done_by") or "",
        "next_action_done_by_name": _fullname(row.get("next_action_done_by")),
        "outcome": row.get("outcome") or "",
        "logged_by": row.get("logged_by") or "",
        "logged_by_name": row.get("logged_by_name") or row.get("logged_by") or "",
        "creation": _str_or_none(row.get("creation")),
        "modified": _str_or_none(row.get("modified")),
        "can_edit": _can_edit_row(row),
        "can_complete": _can_complete_row(row),
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


def _can_complete_row(row):
    """Whether the CALLER may tick this note's next action off.

    Deliberately WIDER than :func:`_can_edit_row`. Editing a colleague's diary
    rewrites what they reported; saying "this got done" is just reporting an
    outcome -- and the person who actually owes the action is whoever the
    reminder landed on, which is not always the author (the note's reminder can
    be re-owned, see ``JarzJourneyNote._reminder_owner``). So: author, manager,
    or the reminder's assignee.
    """
    try:
        user = frappe.session.user
        roles = set(frappe.get_roles(user) or [])
        if roles.intersection(_manager_roles()):
            return True
        if (row.get("logged_by") or "") == user:
            return True
        return (row.get("name") or "") in _my_reminder_notes()
    except Exception:
        return False


def _journey_marker_parts():
    """``(prefix, regex)`` for the tag a note's reminder ToDo carries.

    Derived from ``crm.follow_ups.todo_marker`` instead of hardcoding
    ``[jarz:journey:``, so the tag scheme keeps living in exactly one place --
    the same reason the DocType controller builds its marker from that helper.
    ``prefix`` is the SQL LIKE stem, ``regex`` pulls the note name back out.
    """
    global _MARKER_PREFIX, _MARKER_RE
    if _MARKER_RE is None:
        from jarz_pos.crm.follow_ups import todo_marker

        # A sentinel the marker text can never contain, so the template splits
        # cleanly into "[jarz:journey:" + <note> + "]".
        head, tail = todo_marker("journey:\x00").split("\x00")
        _MARKER_PREFIX = head
        _MARKER_RE = re.compile(re.escape(head) + r"([^\]\s]+)" + re.escape(tail))
    return _MARKER_PREFIX, _MARKER_RE


def _request_cache(attr, key):
    """Per-request memo dict for ``attr``; returns ``(cache, hit_or_None)``.

    Every permission memo below keys on the SESSION USER rather than caching one
    global answer, because ``frappe.set_user`` can switch identity inside a
    single context -- the test suite does it constantly -- and an access answer
    cached for the previous user is a quiet authorisation bug, not just a stale
    read.
    """
    cache = getattr(frappe.local, attr, None)
    if cache is None:
        cache = {}
        setattr(frappe.local, attr, cache)
    return cache, cache.get(key)


# Every per-request memo this module keeps, so they can be dropped together.
_REQUEST_CACHES = (
    "_jarz_journey_my_reminders",
    "_jarz_journey_assigned",
    "_jarz_journey_fullnames",
)


def clear_request_cache():
    """Drop the per-request memos (see :func:`_request_cache`).

    A web request is short and the memos are safe for its whole life, but the
    test runner, a bench console and a background job loop are ONE context for
    hours -- and this module answers permission questions out of those memos. So
    anything that changes ToDo allocation inside a context (notably
    :func:`set_journey_action_done`) invalidates them rather than trusting a
    snapshot taken before it wrote.
    """
    for attr in _REQUEST_CACHES:
        setattr(frappe.local, attr, None)


def _my_reminder_notes():
    """Journey notes whose reminder ToDo is allocated to the caller.

    ONE query per request, memoised: ``can_complete`` is computed for every row
    of a note list and every row of a month's calendar, so a per-row ToDo lookup
    would be hundreds of round trips on a board that used to cost one.

    Closed ToDos count too, on purpose. Completing an action closes its
    reminder, and if only OPEN ones counted the rep who just ticked it off would
    instantly lose the right to UNDO their own mistake.
    """
    cache, hit = _request_cache("_jarz_journey_my_reminders", frappe.session.user)
    if hit is not None:
        return hit

    names = set()
    try:
        prefix, pattern = _journey_marker_parts()
        rows = frappe.get_all(
            "ToDo",
            filters={
                "allocated_to": frappe.session.user,
                "description": ["like", f"%{prefix}%"],
            },
            pluck="description",
            limit_page_length=_CALENDAR_LIMIT,
        )
        for description in rows:
            match = pattern.search(description or "")
            if match:
                names.add(match.group(1))
    except Exception:
        # "Cannot tell" degrades to "not the assignee": the author and managers
        # still get through, so this never hands out access it should not.
        _logger().error("_my_reminder_notes failed", exc_info=True)

    cache[frappe.session.user] = names
    return names


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


def _ensure_done_fields():
    """Raise unless the completion fields are migrated on this site."""
    if not _has_done_fields():
        frappe.throw(
            "Next-action completion is not available on this site yet "
            "(run `bench migrate` to add the Jarz Journey Note completion "
            "fields)."
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
            fields=_note_fields(),
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
    written last. Actions already marked done are skipped entirely: a kept
    promise must stop advertising itself on the card.
    """
    if not journey_enabled():
        return {}

    filters = {"reference_doctype": reference_doctype}
    names = [n for n in (reference_names or []) if n]
    if names:
        filters["reference_name"] = ["in", names]

    # Explicit field list -- ``next_action_done`` has to be in it or the folding
    # below cannot tell a settled action from an outstanding one.
    fields = [
        "reference_name",
        "entry_date",
        "entry_type",
        "note",
        "contact_person",
        "next_action",
        "next_action_date",
    ]
    if _has_done_fields():
        fields.append("next_action_done")

    try:
        rows = frappe.get_all(
            JOURNEY_DOCTYPE,
            filters=filters,
            fields=fields,
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

    Works on either raw DocType rows or mapped notes, which is why the done flag
    is read through ``_bool`` in :func:`_fold_next_action` -- a raw row carries
    ``0``/``1``, a mapped note carries a real bool.
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
    """Keep the most relevant OUTSTANDING next action on ``entry``.

    See :func:`journey_summaries` for the ranking. A row flagged
    ``next_action_done`` is skipped: this is the single choke point that makes a
    completed promise vanish from every card, because both summary builders fold
    through here. ``journey_count`` is counted by the caller and is deliberately
    unaffected -- a done action is still a touch that happened.
    """
    if _bool(row.get("next_action_done")):
        return

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
# Completing a next action
# ---------------------------------------------------------------------------
@frappe.whitelist()
def set_journey_action_done(name, done=1):
    """Mark ONE note's next action done (or undo it).

    The gap this closes: before it existed the only ways out of a promise were
    deleting the note or wiping its date -- both of which destroy the record of
    what was agreed. ``crm.complete_followup`` is no help either: it settles the
    RECORD, so a rep with three open promises on one cafe could not tick off the
    one they actually did.

    What happens on done=1:
      1. the flag + an audit stamp (who, when) go on the note;
      2. this note's reminder ToDo is closed, so the feed stops nagging;
      3. the referenced record's follow-up fields are re-derived from whatever
         is STILL pending (:func:`_resync_record_followup`) -- which is what
         makes the settled action disappear from the pipeline and lead cards.

    done=0 reverses all three. The note keeps its ``next_action`` and
    ``next_action_date`` either way: the promise stays readable, it is just
    settled.

    Access: the note's author, a manager, or whoever the reminder is allocated
    to (see :func:`_can_complete_row`).

    Returns the mapped note.
    """
    _ensure_b2b_access()
    _ensure_journey_doctype()
    _ensure_done_fields()
    if not frappe.db.exists(JOURNEY_DOCTYPE, name):
        frappe.throw(f"Journey note '{name}' not found.")

    doc = frappe.get_doc(JOURNEY_DOCTYPE, name)
    if not _can_complete_row(doc.as_dict()):
        frappe.throw(
            "Not permitted: only the rep who logged this note, the rep it is "
            "assigned to, or a manager may complete its next action."
        )
    if not (doc.get("next_action") or doc.get("next_action_date")):
        frappe.throw("This note has no next action to complete.")

    flag = _bool(done)
    values = {
        "next_action_done": 1 if flag else 0,
        "next_action_done_on": frappe.utils.now() if flag else None,
        "next_action_done_by": frappe.session.user if flag else None,
    }
    # db.set_value, NOT doc.save(): saving re-runs the controller's
    # ``sync_followup``, which would immediately re-stamp the record and re-open
    # the very reminder this call exists to close -- the note still carries its
    # next_action_date, and that is deliberate.
    frappe.db.set_value(JOURNEY_DOCTYPE, name, values)
    # Re-read rather than patching the in-memory copy, so the mapped note the
    # app gets back carries the true ``modified`` the write just produced.
    doc.reload()

    _toggle_reminder(doc, flag)
    # The toggle just moved ToDos around, and can_complete is answered out of a
    # memo of exactly that state -- so the mapped note below must not read a
    # snapshot taken before this call wrote.
    clear_request_cache()
    # The note keeps its date, so pass it explicitly: it is how the resync knows
    # which booked follow-up this completion is entitled to clear.
    _resync_record_followup(
        doc.reference_doctype, doc.reference_name, doc.get("next_action_date")
    )

    return _map_note(doc.as_dict())


def _toggle_reminder(doc, done):
    """Retire or revive THIS note's reminder ToDo. Never raises.

    Delegates to the DocType controller's helpers so the ``[jarz:journey:<note>]``
    marker logic -- and the hard-won reason it does not use
    ``crm.follow_ups._ensure_todo`` -- stays in exactly one place.
    """
    try:
        if done:
            doc._close_reminder_todos()
            return
        next_date = doc.get("next_action_date")
        if not next_date:
            return
        # Revive the ToDo this note's completion closed rather than leaving a
        # Closed one behind per toggle; _ensure_reminder_todo then finds it open
        # and merely re-dates it.
        _reopen_reminder_todos(doc)
        doc._ensure_reminder_todo(next_date)
    except Exception:
        _logger().error(
            f"_toggle_reminder({doc.get('name')}, done={done}) failed", exc_info=True
        )


def _reopen_reminder_todos(doc):
    """Re-open the reminder ToDos completing this note closed. Never raises."""
    try:
        names = frappe.get_all(
            "ToDo",
            filters={
                "reference_type": doc.reference_doctype,
                "reference_name": doc.reference_name,
                "status": "Closed",
                "description": ["like", f"%{doc._todo_marker}%"],
            },
            pluck="name",
        )
        for todo in names:
            frappe.db.set_value("ToDo", todo, "status", "Open", update_modified=False)
    except Exception:
        _logger().error(
            f"_reopen_reminder_todos({doc.get('name')}) failed", exc_info=True
        )


def _resync_record_followup(reference_doctype, reference_name, settled_date=None):
    """Re-derive a record's follow-up fields from its PENDING journey actions.

    ``custom_next_followup_date`` is re-pointed at the EARLIEST journey action
    that is still outstanding; when nothing is outstanding the loop is closed
    (``custom_followup_done = 1``) so the daily reminder passes stop picking the
    record up. This is the half of completion the rep actually sees: it is what
    clears the "next action" line off the pipeline and lead cards.

    Note the asymmetry with the controller's ``sync_followup``, which only ever
    moves the date EARLIER (a new distant note must not delay a near reminder).
    Completion is the one moment it is correct to move the date LATER or drop it
    entirely, because the thing it pointed at has actually been done.

    BUT the diary is not the only writer of that field: ``crm.advance_stage``
    sets it too, from the stage editor. So this only rewrites a date the diary
    can claim -- one the record was pointing at because of the action just
    settled (``settled_date``), or none at all -- or when the remaining journey
    action is SOONER than what is already booked. A rep who scheduled a stage
    follow-up for the 20th must not lose it because they ticked off an unrelated
    note dated the 15th.

    Only Lead and Opportunity carry these fields -- a Customer note is settled
    but drives no record-level reminder. Guarded end to end: the toggle is
    already written, and a hiccup here must never undo or block it.
    """
    if reference_doctype not in FOLLOWUP_DOCTYPES:
        return
    if not _has_done_fields():
        return
    if not _has_field(reference_doctype, "custom_next_followup_date"):
        return
    try:
        pending = frappe.get_all(
            JOURNEY_DOCTYPE,
            filters={
                "reference_doctype": reference_doctype,
                "reference_name": reference_name,
                "next_action_date": ["is", "set"],
                "next_action_done": 0,
            },
            pluck="next_action_date",
            order_by="next_action_date asc",
            limit_page_length=1,
        )
        earliest = str(pending[0]) if pending else None

        current = frappe.db.get_value(
            reference_doctype, reference_name, "custom_next_followup_date"
        )
        if not _diary_may_rewrite(current, settled_date, earliest):
            return

        values = {"custom_next_followup_date": earliest}
        if _has_field(reference_doctype, "custom_followup_done"):
            values["custom_followup_done"] = 0 if earliest else 1
        frappe.db.set_value(
            reference_doctype, reference_name, values, update_modified=False
        )
    except Exception:
        _logger().error(
            f"_resync_record_followup({reference_doctype}, {reference_name}) failed",
            exc_info=True,
        )


def _diary_may_rewrite(current, settled_date, earliest):
    """Whether completion may move a record's follow-up date off ``current``.

    Yes when nothing is booked, when what is booked is exactly the action just
    settled (the diary put it there, so the diary may take it away), or when the
    earliest remaining journey action falls sooner than what is booked. No
    otherwise -- that date belongs to another writer, most likely the stage
    editor, and is not the diary's to drop.
    """
    if not current:
        return True
    if settled_date and _same_date(current, settled_date):
        return True
    return bool(earliest) and _is_before(earliest, current)


def _same_date(left, right):
    """Date equality across str/date/datetime. Guarded -> False."""
    try:
        return frappe.utils.getdate(left) == frappe.utils.getdate(right)
    except Exception:
        return False


def _is_before(left, right):
    """``left < right`` as dates. Guarded -> False."""
    try:
        return frappe.utils.getdate(left) < frappe.utils.getdate(right)
    except Exception:
        return False


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


# ---------------------------------------------------------------------------
# Action calendar
# ---------------------------------------------------------------------------
# The rep's month view of everything they promised. Two kinds of promise exist
# in this app and both have to show up or the calendar lies:
#   1. journey actions -- one note, one date, completable individually;
#   2. record-level follow-ups -- ``custom_next_followup_date`` on a
#      Lead/Opportunity, set by stage advancement or by the reminder passes.
# They overlap by design (a journey action STAMPS the record-level date), so the
# record-level row is dropped whenever a journey action already covers the same
# record on the same day -- otherwise every promise would render twice.


@frappe.whitelist()
def get_action_calendar(from_date=None, to_date=None, scope="mine", include_done=0):
    """Every promised action in a date range, for the calendar screen.

    Args:
        from_date/to_date: ISO ``yyyy-mm-dd``. Default to the current month.
        scope: ``"mine"`` (default) or ``"all"``. Anyone with B2B access may ask
            for either -- the pipeline board is already fully visible to reps,
            so hiding the calendar would be theatre, not security.
        include_done: falsy (default) omits completed actions; truthy includes
            them. ``counts.done`` reports what is in range either way, so the
            screen can offer "3 done" without fetching them.

    Returns::

        {
            "from_date": str, "to_date": str, "scope": str,
            "actions": [<action>, ...],
            "counts": {"pending": int, "overdue": int, "done": int},
        }

    ``pending`` is everything not done; ``overdue`` is the subset of those dated
    before today.
    """
    _ensure_b2b_access()

    start, end = _calendar_range(from_date, to_date)
    scope = "all" if str(scope or "").strip().lower() == "all" else "mine"
    show_done = _bool(include_done)
    user = frappe.session.user
    today = _today()

    # Roles once for the whole range; assignments are memoised the first time a
    # row asks for them. Nothing below costs a query per row.
    is_manager = _is_manager()

    actions = _calendar_journey_actions(start, end, scope, user, today, is_manager)

    # Dedup: a journey action already covers this record on this day, and the
    # record-level date is almost always its own echo.
    covered = {
        (a["reference_doctype"], a["reference_name"], a["date"]) for a in actions
    }
    for row in _calendar_followup_actions(start, end, scope, user, today, is_manager):
        if (row["reference_doctype"], row["reference_name"], row["date"]) in covered:
            continue
        actions.append(row)

    _attach_titles(actions)

    counts = {"pending": 0, "overdue": 0, "done": 0}
    for action in actions:
        if action["done"]:
            counts["done"] += 1
            continue
        counts["pending"] += 1
        if action["overdue"]:
            counts["overdue"] += 1

    if not show_done:
        actions = [a for a in actions if not a["done"]]

    actions.sort(key=lambda a: (a["date"], a["reference_name"]))

    return {
        "from_date": start,
        "to_date": end,
        "scope": scope,
        "actions": actions,
        "counts": counts,
    }


def _calendar_range(from_date, to_date):
    """``(start, end)`` ISO strings, defaulting to the current month."""
    start = str(from_date or "").strip()
    end = str(to_date or "").strip()
    if start and end:
        return start, end

    today = _today()
    try:
        from frappe.utils import get_first_day, get_last_day

        start = start or str(get_first_day(today))
        end = end or str(get_last_day(today))
    except Exception:
        # Never leave the range half-open: an unbounded scan of the whole table
        # is exactly what _CALENDAR_LIMIT exists to prevent.
        _logger().error("_calendar_range fallback engaged", exc_info=True)
        start = start or str(today or "")
        end = end or str(today or "")
    return start, end


def _is_manager():
    """Whether the caller holds a manager role. Guarded -> False."""
    try:
        return bool(set(frappe.get_roles(frappe.session.user) or []).intersection(
            _manager_roles()
        ))
    except Exception:
        return False


def _assigned_references(user):
    """``{(reference_type, reference_name)}`` the user has an open ToDo on.

    ONE query per request (memoised), so "is this mine?" and "may I complete
    this?" are set lookups for every row of the calendar rather than a round
    trip each.
    """
    cache, hit = _request_cache("_jarz_journey_assigned", user)
    if hit is not None:
        return hit

    refs = set()
    try:
        rows = frappe.get_all(
            "ToDo",
            filters={
                "allocated_to": user,
                "status": "Open",
                "reference_type": ["in", list(REFERENCE_DOCTYPES)],
            },
            fields=["reference_type", "reference_name"],
            limit_page_length=_CALENDAR_LIMIT,
        )
        refs = {
            (r.get("reference_type"), r.get("reference_name"))
            for r in rows
            if r.get("reference_name")
        }
    except Exception:
        _logger().error(f"_assigned_references({user}) failed", exc_info=True)

    cache[user] = refs
    return refs


def _calendar_journey_actions(start, end, scope, user, today, is_manager):
    """Journey next actions dated in range. Guarded -> [] (never raises).

    Guarded on the DocType existing because CI's logic gate runs pre-migrate:
    with no journey table the calendar still serves its follow-up half rather
    than 500-ing.
    """
    if not journey_enabled():
        return []

    fields = [
        "name",
        "reference_doctype",
        "reference_name",
        "next_action",
        "next_action_date",
        "contact_person",
        "entry_type",
        "logged_by",
    ]
    if _has_done_fields():
        fields.append("next_action_done")

    filters = {"next_action_date": ["between", [start, end]]}
    if scope == "mine":
        filters["logged_by"] = user

    try:
        rows = frappe.get_all(
            JOURNEY_DOCTYPE,
            filters=filters,
            fields=fields,
            order_by="next_action_date asc, reference_name asc",
            limit_page_length=_CALENDAR_LIMIT,
        )
    except Exception:
        _logger().error(
            f"_calendar_journey_actions({start}, {end}) failed", exc_info=True
        )
        return []

    out = []
    for row in rows:
        done = _bool(row.get("next_action_done"))
        date = str(row.get("next_action_date"))
        owner = row.get("logged_by") or ""
        out.append(
            {
                "source": "journey",
                "note": row.get("name"),
                "reference_doctype": row.get("reference_doctype"),
                "reference_name": row.get("reference_name"),
                "title": "",  # filled by _attach_titles, one query per doctype
                "date": date,
                "action": row.get("next_action") or "",
                "contact_person": row.get("contact_person") or "",
                "entry_type": row.get("entry_type") or "",
                "done": done,
                "overdue": bool(today and not done and date < today),
                "owner": owner,
                "owner_name": _fullname(owner),
                # Same rule as _can_complete_row, evaluated from the sets
                # already in hand so no row costs a query.
                "can_complete": bool(
                    is_manager
                    or owner == user
                    or (row.get("name") or "") in _my_reminder_notes()
                ),
            }
        )
    return out


def _calendar_followup_actions(start, end, scope, user, today, is_manager):
    """Record-level follow-ups dated in range. Guarded -> [] (never raises).

    Only Lead and Opportunity carry ``custom_next_followup_date``; a record
    already flagged ``custom_followup_done`` is settled and never appears, which
    is why every row from this source reports ``done`` False.
    """
    out = []
    for doctype in FOLLOWUP_DOCTYPES:
        if not _has_field(doctype, "custom_next_followup_date"):
            continue

        filters = {"custom_next_followup_date": ["between", [start, end]]}
        if _has_field(doctype, "custom_followup_done"):
            filters["custom_followup_done"] = 0
        # Mirror crm.follow_ups._pass_lead_followups: a lead judged not suitable,
        # or merged away into another record, produces no reminder -- so showing
        # its date on the calendar would promise a chase that never comes.
        if doctype == "Lead":
            if _has_field(doctype, "custom_not_suitable"):
                filters["custom_not_suitable"] = 0
            if _has_field(doctype, "custom_merged_into"):
                filters["custom_merged_into"] = ["in", ["", None]]

        try:
            rows = frappe.get_all(
                doctype,
                filters=filters,
                fields=["name", "owner", "custom_next_followup_date"],
                order_by="custom_next_followup_date asc, name asc",
                limit_page_length=_CALENDAR_LIMIT,
            )
        except Exception:
            _logger().error(
                f"_calendar_followup_actions({doctype}) failed", exc_info=True
            )
            continue

        for row in rows:
            name = row.get("name")
            owner = row.get("owner") or ""
            assigned = (doctype, name) in _assigned_references(user)
            if scope == "mine" and owner != user and not assigned:
                continue
            date = str(row.get("custom_next_followup_date"))
            out.append(
                {
                    "source": "followup",
                    "note": "",
                    "reference_doctype": doctype,
                    "reference_name": name,
                    "title": "",
                    "date": date,
                    # A record-level follow-up carries no text: it is a date the
                    # stage editor or a reminder pass set, not a sentence a rep
                    # wrote. The journey action is where the words live.
                    "action": "",
                    "contact_person": "",
                    "entry_type": "",
                    "done": False,
                    "overdue": bool(today and date < today),
                    "owner": owner,
                    "owner_name": _fullname(owner),
                    # Mirrors crm._can_complete_followup, computed from the sets
                    # already loaded instead of two queries per row.
                    "can_complete": bool(is_manager or owner == user or assigned),
                }
            )
    return out


def _attach_titles(actions):
    """Fill every action's ``title`` in ONE query per reference doctype.

    The corpus runs to ~2600 leads; resolving a title per row would turn one
    month view into hundreds of round trips. Same pattern as
    ``leads._attach_contacts`` and :func:`journey_summaries`. Best-effort: an
    unresolvable title falls back to the record name, which is never empty.
    """
    wanted = {}
    for action in actions:
        doctype = action.get("reference_doctype")
        name = action.get("reference_name")
        if doctype and name:
            wanted.setdefault(doctype, set()).add(name)

    titles = {}
    for doctype, names in wanted.items():
        candidates = [
            f for f in _TITLE_FIELDS.get(doctype, ()) if _has_field(doctype, f)
        ]
        if not candidates:
            continue
        try:
            rows = frappe.get_all(
                doctype,
                filters={"name": ["in", list(names)]},
                fields=["name", *candidates],
                limit_page_length=0,
            )
        except Exception:
            _logger().error(f"_attach_titles({doctype}) failed", exc_info=True)
            continue
        resolved = {}
        for row in rows:
            title = next((row.get(f) for f in candidates if row.get(f)), None)
            if title:
                resolved[row.get("name")] = title
        titles[doctype] = resolved

    for action in actions:
        by_name = titles.get(action.get("reference_doctype")) or {}
        action["title"] = (
            by_name.get(action.get("reference_name"))
            or action.get("reference_name")
            or ""
        )
