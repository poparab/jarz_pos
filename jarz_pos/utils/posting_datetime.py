"""One place that turns an operator's chosen posting moment into document fields.

Every POS write that lets the operator pick a date used to do this by hand::

    if posting_date:
        doc.posting_date = posting_date
        doc.set_posting_time = 1

That is a date with no time, and ``set_posting_time = 1`` without a
``posting_time`` tells ERPNext "trust the fields I set" while leaving the time
field at whatever the framework defaulted it to — the wall clock of the moment
the document was typed.  A Purchase Invoice backdated to 2025-10-30 on staging
carries ``posting_time = 17:54:03``, which is when somebody keyed it in, not
when the goods arrived.  Stock valuation, the courier/shift cut-offs and every
"as of" balance read that time, so a backdated document lands in the right day
but in the wrong order inside it.

Two document shapes have to be served, and they are NOT interchangeable:

* **Stock-backed** — Stock Entry, Stock Reconciliation, Purchase Invoice,
  Purchase Receipt, Sales Invoice.  These carry real ``posting_time`` and
  ``set_posting_time`` columns, so the chosen time can be written where ERPNext
  itself reads it.  :func:`apply_stock_posting_datetime`.
* **Ledger-backed** — Journal Entry and Payment Entry.  These have NO
  ``posting_time`` field and no such column; ``je.set_posting_time = 1`` on a
  Journal Entry is a no-op that has been in this codebase long enough to look
  load-bearing.  The chosen time is therefore recorded in the Jarz custom field
  ``custom_jarz_posting_time`` so the information survives, even though GL
  Entry itself only has day resolution.  :func:`apply_ledger_posting_datetime`.

Wire contract (fixed; the mobile client is coded against it).  No endpoint grew
a new parameter — the EXISTING date strings simply accept an optional time::

    "2026-09-08"              -> ('2026-09-08', None)   # legacy, unchanged
    "2026-09-08 14:30:00"     -> ('2026-09-08', '14:30:00')
    "2026-09-08 14:30"        -> ('2026-09-08', '14:30:00')
    "2026-09-08T14:30:00.500" -> ('2026-09-08', '14:30:00.500000')

Backward compatibility is the reason for the ``None`` in the date-only case
rather than a ``"00:00:00"`` default: the backend deploys before the mobile
patch reaches devices, so for weeks every caller is a date-only caller, and
those requests must behave EXACTLY as they do today.  A caller that sends no
time gets no time written — not midnight, which would silently re-order every
backdated document to the top of its day.

A malformed value is refused rather than dropped.  Silently discarding the time
half of ``"2026-09-08 14:30:00"`` because the parser did not recognise it is
precisely the failure this module exists to remove, and it would be invisible:
the document would still post, on the right day, at the wrong time.
"""

from __future__ import annotations

import re
from datetime import date as _date_cls, datetime as _datetime_cls, time as _time_cls, timedelta as _timedelta_cls
from typing import Any, Optional, Tuple

import frappe
from frappe import _

# The Jarz custom field carrying the chosen time on doctypes that have no
# ``posting_time`` of their own.  Seeded by the before_migrate hook in
# ``jarz_pos.utils.cleanup``; every read of it here is guarded, because this
# code deploys as one commit and the field only exists after that deploy's
# migrate has run.
LEDGER_POSTING_TIME_FIELD = "custom_jarz_posting_time"

# ``YYYY-MM-DD`` optionally followed by a time, separated by a space or the ISO
# ``T``.  Seconds and fractional seconds are both optional; a trailing ``Z`` or
# a ``+HH:MM`` offset is deliberately NOT accepted — see _parse_text below.
_POSTING_DATETIME_RE = re.compile(
    r"^(?P<date>\d{4}-\d{2}-\d{2})"
    r"(?:[T ]\s*"
    r"(?P<hour>\d{1,2}):(?P<minute>\d{2})"
    r"(?::(?P<second>\d{2})(?P<fraction>\.\d{1,9})?)?"
    r")?$"
)

# The time half on its own, for values read back out of a Time column.
_TIME_RE = re.compile(
    r"^(?P<hour>\d{1,2}):(?P<minute>\d{2})"
    r"(?::(?P<second>\d{2})(?P<fraction>\.\d{1,9})?)?$"
)

_SECONDS_PER_DAY = 24 * 60 * 60


def _invalid(value: Any) -> None:
    """Refuse the value, naming it.

    ``frappe.throw`` and not a ``ValueError``: this is reached from whitelisted
    endpoints, so the operator has to be told which string was rejected. The
    raw value is echoed back because the client builds it by concatenation and
    the bug is nearly always visible in the string itself.
    """
    frappe.throw(_("Invalid posting date/time: {0}").format(value))


def _format_fraction(fraction: Optional[str]) -> str:
    """Normalise a ``.123`` / ``.123456789`` fraction to Frappe's 6 digits.

    An all-zero fraction returns ``""``.  Dart's ``toIso8601String()`` always
    emits ``.000`` for a whole second, and writing ``14:30:00.000000`` where
    ``14:30:00`` was meant makes two equal times compare unequal as strings —
    which is how the stock-ordering comparison in ``api/manufacturing`` reads
    them.
    """
    if not fraction:
        return ""
    digits = (fraction[1:] + "000000")[:6]
    if digits == "000000":
        return ""
    return "." + digits


def _compose_time(hour: int, minute: int, second: int, fraction: str) -> str:
    """Validate the clock reading and render it as ``HH:MM:SS[.ffffff]``.

    ``_time_cls`` does the range checking so ``25:00`` and ``12:61`` are refused
    by the standard library rather than by hand-rolled bounds.
    """
    _time_cls(hour, minute, second)  # raises ValueError on an impossible clock
    return f"{hour:02d}:{minute:02d}:{second:02d}{fraction}"


def _parse_text(text: str) -> Tuple[Optional[str], Optional[str]]:
    """Split an already-stripped, non-empty string. Throws if it is not ours."""
    match = _POSTING_DATETIME_RE.match(text)
    if not match:
        # This is where a ``Z``-suffixed or offset-bearing ISO instant lands,
        # and refusing it is deliberate.  Every posting field in this app is a
        # naive local wall clock; accepting ``...T14:30:00Z`` would store 14:30
        # local for a caller that meant 16:30 local, and no error would ever be
        # raised.  A client that has a UTC instant must convert it first.
        _invalid(text)
        return None, None  # pragma: no cover - frappe.throw does not return

    date_text = match.group("date")
    try:
        # Validates the calendar as well as the shape: ``2026-02-30`` matches
        # the regex and is still not a day.
        _date_cls.fromisoformat(date_text)
    except ValueError:
        _invalid(text)
        return None, None  # pragma: no cover

    if match.group("hour") is None:
        # Date-only: the legacy shape. NOT midnight — see the module docstring.
        return date_text, None

    try:
        time_text = _compose_time(
            int(match.group("hour")),
            int(match.group("minute")),
            int(match.group("second") or 0),
            _format_fraction(match.group("fraction")),
        )
    except ValueError:
        _invalid(text)
        return None, None  # pragma: no cover

    return date_text, time_text


def split_posting_datetime(value: Any) -> Tuple[Optional[str], Optional[str]]:
    """Split an operator-supplied posting moment into ``(date, time)`` strings.

    ``('2026-09-08 14:30:00')`` -> ``('2026-09-08', '14:30:00')``
    ``('2026-09-08')``          -> ``('2026-09-08', None)``
    ``(None / '')``             -> ``(None, None)``

    ``None`` for the time means "the caller named no time", which every caller
    here must treat as "leave the document's time alone" rather than as
    midnight.  Empty input yields ``(None, None)`` so a caller can keep its
    existing ``if not posting_date: ... today()`` fallback unchanged.

    ``date``/``datetime`` objects are accepted too: a value read back off a
    document comes out of the DB typed, not as the string the client sent.
    """
    if value is None:
        return None, None

    # Ordered before the ``date`` check: ``datetime`` is a subclass of ``date``,
    # so testing ``date`` first would throw every time component away.
    if isinstance(value, _datetime_cls):
        micro = int(getattr(value, "microsecond", 0) or 0)
        time_text = value.strftime("%H:%M:%S") + (f".{micro:06d}" if micro else "")
        return value.strftime("%Y-%m-%d"), time_text

    if isinstance(value, _date_cls):
        return value.strftime("%Y-%m-%d"), None

    text = str(value).strip()
    if not text:
        return None, None

    return _parse_text(text)


def format_time_value(value: Any) -> Optional[str]:
    """Render a Frappe ``Time`` field value as ``HH:MM:SS[.ffffff]``.

    Frappe hands a ``Time`` column back as a ``datetime.timedelta``, not a
    ``time`` and not a string — so a time read off a document cannot simply be
    concatenated onto a date.  Returns ``None`` for anything empty or outside a
    single day: a ``Time`` column here is a wall clock, and a value that is not
    one is not something to guess at.

    Unlike :func:`split_posting_datetime` this never throws.  Its inputs come
    from the database, not from a caller, so there is no operator to correct.
    """
    if value is None or value == "":
        return None

    if isinstance(value, _timedelta_cls):
        total = int(value.total_seconds())
        if total < 0 or total >= _SECONDS_PER_DAY:
            return None
        micro = int(value.microseconds or 0)
        text = f"{total // 3600:02d}:{(total % 3600) // 60:02d}:{total % 60:02d}"
        return text + (f".{micro:06d}" if micro else "")

    if isinstance(value, _time_cls):
        micro = int(getattr(value, "microsecond", 0) or 0)
        return value.strftime("%H:%M:%S") + (f".{micro:06d}" if micro else "")

    match = _TIME_RE.match(str(value).strip())
    if not match:
        return None
    try:
        return _compose_time(
            int(match.group("hour")),
            int(match.group("minute")),
            int(match.group("second") or 0),
            _format_fraction(match.group("fraction")),
        )
    except ValueError:
        return None


def join_posting_datetime(date_value: Any, time_value: Any) -> Optional[str]:
    """Rebuild a ``"YYYY-MM-DD HH:MM:SS"`` string from two stored halves.

    For the flows that persist the date on one document and the time on another
    field (Employee Advance, Jarz Expense Request) and have to hand both to
    :func:`apply_ledger_posting_datetime` later.  Returns just the date when no
    usable time is stored, and ``None`` when there is no date at all — so the
    caller can tell "no choice was recorded" from "a date-only choice".
    """
    date_text, embedded_time = split_posting_datetime(date_value)
    if not date_text:
        return None
    time_text = format_time_value(time_value) or embedded_time
    return f"{date_text} {time_text}" if time_text else date_text


def _set(doc: Any, fieldname: str, value: Any) -> None:
    """Write one field on either a Document or a plain dict.

    Both shapes turn up: endpoints that build the document with
    ``frappe.new_doc`` hand over a Document, while the ones that go through
    ``frappe.get_doc({...})`` or an ERPNext mapper build a dict first.
    ``frappe._dict`` is a dict subclass, so the ``dict`` branch covers it —
    which is why this tests for ``dict`` rather than for ``Document`` (the
    reference implementation, ``api/manufacturing._apply_posting_datetime``,
    tests for ``Document`` and therefore needs it importable).
    """
    if isinstance(doc, dict):
        doc[fieldname] = value
    else:
        setattr(doc, fieldname, value)


def apply_stock_posting_datetime(doc: Any, value: Any) -> None:
    """Set the posting moment on a doc that HAS ``posting_time``.

    Stock Entry, Stock Reconciliation, Purchase Invoice, Purchase Receipt and
    Sales Invoice — verified as real columns on this site.

    ``set_posting_time = 1`` goes on whenever a date is applied, time or no
    time, and that is NOT optional: ``TransactionBase.validate_posting_time``
    (erpnext/utilities/transaction_base.py) overwrites BOTH ``posting_date``
    and ``posting_time`` with ``now_datetime()`` for any document where the
    flag is falsy.  Leaving it off on the date-only path would therefore not
    "preserve the old behaviour", it would silently discard the backdate
    altogether — which is why every call site this replaced already set it
    alongside the date.  ``posting_time`` is the part that is conditional: it
    is written only when the caller actually named a time, so a date-only
    request keeps landing at the wall clock exactly as it does today.

    Does nothing at all when ``value`` is empty, so a caller can keep its own
    ``today()`` fallback.
    """
    date_text, time_text = split_posting_datetime(value)
    if not date_text:
        return

    _set(doc, "posting_date", date_text)
    _set(doc, "set_posting_time", 1)
    if time_text:
        _set(doc, "posting_time", time_text)


def _ledger_time_field_exists(doctype: str) -> bool:
    """True when *doctype* on THIS site carries ``custom_jarz_posting_time``.

    Guarded the same way ``api/pos.py`` guards ``custom_allow_delivery_partner``:
    the field arrives with a migrate, and the code that writes it ships in the
    same commit, so between deploy and migrate the field is legitimately absent.
    The broad ``except`` is scoped to this one meta lookup on purpose — it must
    never turn a cash transfer or a supplier payment into a 500 — and it cannot
    hide a parsing bug, because parsing has already happened by the time this
    is called.
    """
    if not doctype:
        return False
    try:
        return bool(frappe.get_meta(doctype).get_field(LEDGER_POSTING_TIME_FIELD))
    except Exception:
        return False


def _doctype_of(doc: Any) -> str:
    if isinstance(doc, dict):
        return str(doc.get("doctype") or "")
    return str(getattr(doc, "doctype", "") or "")


def _log_dropped_time(doc: Any, time_text: str) -> None:
    """Record a time we could not store, so it is not lost in silence."""
    try:
        frappe.log_error(
            f"{_doctype_of(doc) or 'ledger document'} has no {LEDGER_POSTING_TIME_FIELD}; "
            f"the chosen posting time {time_text} was not recorded. Run `bench migrate`.",
            "Jarz POS – posting time not recorded",
        )
    except Exception:
        # ``frappe.log_error`` itself can raise (a title/message over the column
        # limit, or no DB connection in a background context). Losing the note
        # is acceptable; losing the posting is not.
        pass


def apply_ledger_posting_datetime(doc: Any, value: Any) -> None:
    """Set the posting moment on a Journal Entry / Payment Entry.

    Neither doctype has a ``posting_time`` field or column — nor does GL Entry,
    so the general ledger genuinely has day resolution and no amount of writing
    can change that.  What this preserves is the operator's INTENT: the chosen
    time goes into ``custom_jarz_posting_time`` so the document can be ordered,
    reconciled and audited against the stock movement it pairs with.

    Never throws over the custom field being absent (see
    :func:`_ledger_time_field_exists`); a missing field is logged and the date
    is still applied.  A malformed *value* is still refused, because that is a
    caller bug rather than a schema state.
    """
    date_text, time_text = split_posting_datetime(value)
    if not date_text:
        return

    _set(doc, "posting_date", date_text)
    if not time_text:
        return

    if _ledger_time_field_exists(_doctype_of(doc)):
        _set(doc, LEDGER_POSTING_TIME_FIELD, time_text)
    else:
        _log_dropped_time(doc, time_text)
