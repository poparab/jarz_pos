"""B2B settlement schedules: when a shop is expected to pay its credit balance.

WHAT THIS IS FOR
----------------
B2B shops settle their on-account balance on loose, informal rhythms (owner,
2026-09-25): "invoice after invoice" (the next delivery collects the previous
one), weekly on a fixed day, twice a month (the 15th and the last day), or
simply cash at the door. The owner wants REMINDERS and a "collections due"
view, never enforcement: nothing here blocks an order.

This module is the whole rule book, and it is deliberately PURE: no ``frappe``
import, no database, no clock. Every function takes ``today`` explicitly and
works on plain dicts, so the schedule arithmetic (fortnights counted from an
anchor, the 31st clamping to a 30-day month, February, ...) is pinned by
``jarz_pos/tests/test_settlement_schedule.py`` without a site. The API
(``api/settlement_terms.py``), the DocType controller and the daily reminder
pass (``services/settlement_reminders.py``) only read rows, call in here, and
write results.

THE ONE RULE THAT MATTERS
-------------------------
For a date-based cycle an invoice is **due at the first schedule date strictly
AFTER its posting date**. A shop that pays on Thursdays and takes an order on a
Thursday is not expected to pay for it that same Thursday; it pays next week.
"""

from __future__ import annotations

import calendar
import datetime
import json
import re
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

# ---------------------------------------------------------------------------
# Vocabulary (wire contract with the DocType Select options and the client)
# ---------------------------------------------------------------------------

CYCLE_ON_DELIVERY = "On Delivery"
CYCLE_INVOICE_AFTER_INVOICE = "Invoice after Invoice"
CYCLE_WEEKLY = "Weekly"
CYCLE_DAYS_OF_MONTH = "Days of Month"
CYCLE_EVERY_N_DAYS = "Every N Days"

#: Exactly the DocType's Select options, in the same order.
CYCLES: Tuple[str, ...] = (
    CYCLE_ON_DELIVERY,
    CYCLE_INVOICE_AFTER_INVOICE,
    CYCLE_WEEKLY,
    CYCLE_DAYS_OF_MONTH,
    CYCLE_EVERY_N_DAYS,
)

#: Cycles that produce calendar due dates.
DATE_CYCLES: Tuple[str, ...] = (CYCLE_WEEKLY, CYCLE_DAYS_OF_MONTH, CYCLE_EVERY_N_DAYS)

STATE_OVERDUE = "overdue"
STATE_DUE_TODAY = "due_today"
STATE_DUE_SOON = "due_soon"
STATE_OK = "ok"
STATE_NONE = "none"
#: A customer with an open credit balance and no usable schedule.
STATE_UNSCHEDULED = "unscheduled"

#: Collections list order (``none`` is never listed).
STATE_ORDER: Tuple[str, ...] = (
    STATE_OVERDUE,
    STATE_DUE_TODAY,
    STATE_DUE_SOON,
    STATE_UNSCHEDULED,
    STATE_OK,
)

#: Reminder kinds written to ``last_reminder_kind`` and sent as ``data.kind``.
KIND_DUE_SOON = "due_soon"
KIND_DUE_TODAY = "due_today"
KIND_OVERDUE = "overdue"
#: The on-submit "collect the previous invoice(s) with this delivery" push.
KIND_COLLECT_ON_DELIVERY = "collect_on_delivery"

WEEKDAY_CODES: Tuple[str, ...] = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
_WEEKDAY_NAMES_EN = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
_WEEKDAY_NAMES_AR = ("الاثنين", "الثلاثاء", "الأربعاء", "الخميس", "الجمعة", "السبت", "الأحد")
_WEEKDAY_LOOKUP = {code.lower(): idx for idx, code in enumerate(WEEKDAY_CODES)}

MONTH_LAST = "last"
_MONTH_LAST_TOKENS = frozenset({"last", "end", "eom"})

#: Fallback grid origin when a record carries no anchor (and no creation date).
#: A Monday, so a fortnightly grid without an anchor is still deterministic.
DEFAULT_ANCHOR = datetime.date(2024, 1, 1)

#: Amounts at or below this are zero (same epsilon api/credit uses).
MONEY_EPSILON = 0.005

#: How many future due dates ``upcoming_dates`` carries.
UPCOMING_COUNT = 3

#: Hard ceiling on a day-by-day scan, so a malformed range cannot spin.
_MAX_SCAN_DAYS = 3700


class SettlementTermsError(ValueError):
    """A terms value that cannot be stored. The message is user-facing."""


# ---------------------------------------------------------------------------
# Small coercions
# ---------------------------------------------------------------------------


def _get(row: Any, key: str, default: Any = None) -> Any:
    if row is None:
        return default
    if isinstance(row, dict):
        return row.get(key, default)
    getter = getattr(row, "get", None)
    if callable(getter):
        try:
            return getter(key, default)
        except TypeError:
            pass
    return getattr(row, key, default)


def to_date(value: Any) -> Optional[datetime.date]:
    """``date`` / ``datetime`` / ISO string -> ``date``; empty -> ``None``.

    Raises :class:`SettlementTermsError` for a non-empty value that is not a date.
    """
    if value is None or value == "":
        return None
    if isinstance(value, datetime.datetime):
        return value.date()
    if isinstance(value, datetime.date):
        return value
    text = str(value).strip()
    if not text:
        return None
    try:
        return datetime.date.fromisoformat(text[:10])
    except ValueError:
        raise SettlementTermsError(f"'{value}' is not a valid date (use YYYY-MM-DD).")


def _to_date_lenient(value: Any) -> Optional[datetime.date]:
    try:
        return to_date(value)
    except SettlementTermsError:
        return None


def _iso(value: Optional[datetime.date]) -> Optional[str]:
    return value.isoformat() if value else None


def _money(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _int_lenient(value: Any, default: Optional[int]) -> Optional[int]:
    if value is None or value == "":
        return default
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _int_strict(value: Any, default: Optional[int], label: str, minimum: Optional[int] = None) -> Optional[int]:
    if value is None or (isinstance(value, str) and not value.strip()):
        return default
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise SettlementTermsError(f"{label} must be a whole number.")
    if number != int(number):
        raise SettlementTermsError(f"{label} must be a whole number.")
    result = int(number)
    if minimum is not None and result < minimum:
        raise SettlementTermsError(f"{label} must be at least {minimum}.")
    return result


def _truthy(value: Any, default: bool = True) -> bool:
    if value is None or value == "":
        return default
    if isinstance(value, str):
        return value.strip().lower() not in ("0", "false", "no", "off")
    try:
        return bool(int(value))
    except (TypeError, ValueError):
        return bool(value)


def _split_tokens(value: Any) -> List[str]:
    """Comma string, JSON list string, or a real list -> list of tokens."""
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        return [str(v).strip() for v in value if str(v if v is not None else "").strip()]
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return [str(int(value))]
    text = str(value).strip()
    if not text:
        return []
    if text.startswith("["):
        try:
            parsed = json.loads(text)
        except ValueError:
            parsed = None
        if isinstance(parsed, list):
            return _split_tokens(parsed)
    return [tok for tok in re.split(r"[,;\s]+", text) if tok]


def canonical_cycle(value: Any) -> Optional[str]:
    """The Select option this value names (case/space-insensitive), else None."""
    key = re.sub(r"\s+", " ", str(value or "").strip()).lower()
    if not key:
        return None
    for cycle in CYCLES:
        if cycle.lower() == key:
            return cycle
    return None


# ---------------------------------------------------------------------------
# Weekday / month-day lists
# ---------------------------------------------------------------------------


def parse_weekdays(value: Any, strict: bool = True) -> List[int]:
    """``"Thu"`` / ``"Mon,Thu"`` / ``["thursday"]`` -> sorted weekday indexes (Mon=0)."""
    days = set()
    for token in _split_tokens(value):
        idx = _WEEKDAY_LOOKUP.get(token.strip().lower()[:3])
        if idx is None:
            if strict:
                raise SettlementTermsError(
                    f"'{token}' is not a weekday. Use {', '.join(WEEKDAY_CODES)}."
                )
            continue
        days.add(idx)
    return sorted(days)


def format_weekdays(days: Iterable[int]) -> str:
    return ",".join(WEEKDAY_CODES[d] for d in sorted(set(days)))


def parse_month_days(value: Any, strict: bool = True) -> Tuple[List[int], bool]:
    """``"15,last"`` -> ``([15], True)``. Days are 1..31; ``last`` is the month end."""
    days = set()
    last = False
    for token in _split_tokens(value):
        key = token.strip().lower()
        if key in _MONTH_LAST_TOKENS:
            last = True
            continue
        try:
            number = float(key)
            if number != int(number):
                raise ValueError
            day = int(number)
        except ValueError:
            if strict:
                raise SettlementTermsError(
                    f"'{token}' is not a day of the month. Use 1-31 or 'last'."
                )
            continue
        if not 1 <= day <= 31:
            if strict:
                raise SettlementTermsError(f"Day of month {day} is out of range (1-31).")
            continue
        days.add(day)
    return sorted(days), last


def format_month_days(days: Iterable[int], last: bool) -> str:
    parts = [str(d) for d in sorted(set(days))]
    if last:
        parts.append(MONTH_LAST)
    return ",".join(parts)


# ---------------------------------------------------------------------------
# Storage normalisation (strict) and runtime parsing (lenient)
# ---------------------------------------------------------------------------


def normalize_terms_input(values: Dict[str, Any]) -> Dict[str, Any]:
    """Validate a terms record and return the values to STORE.

    Shared by ``save_settlement_terms`` and the DocType controller, so the API
    and a Desk edit refuse exactly the same things. Fields that do not belong
    to the chosen cycle are cleared, so a record never carries a stale
    ``weekdays`` under a monthly cycle that a later reader could misread.

    Raises :class:`SettlementTermsError` with a user-facing message.
    ``anchor_date`` is returned as a ``date`` (or ``None``: the caller decides
    the default, because only it knows "today").
    """
    cycle = canonical_cycle(values.get("cycle"))
    if not cycle:
        raise SettlementTermsError(
            "Choose a settlement cycle: " + ", ".join(CYCLES) + "."
        )

    out: Dict[str, Any] = {
        "cycle": cycle,
        "enabled": 1 if _truthy(values.get("enabled"), True) else 0,
        "weekdays": None,
        "week_interval": 1,
        "month_days": None,
        "interval_days": None,
        "anchor_date": to_date(values.get("anchor_date")),
        "remind_days_before": _int_strict(
            values.get("remind_days_before"), 1, "Remind days before", minimum=0
        ),
        "overdue_repeat_days": _int_strict(
            values.get("overdue_repeat_days"), 2, "Repeat overdue reminder every", minimum=1
        ),
        "responsible_user": (str(values.get("responsible_user") or "").strip() or None),
        "notes": (str(values.get("notes") or "").strip() or None),
    }

    if cycle == CYCLE_WEEKLY:
        days = parse_weekdays(values.get("weekdays"), strict=True)
        if not days:
            raise SettlementTermsError("Pick at least one weekday for a weekly schedule.")
        out["weekdays"] = format_weekdays(days)
        out["week_interval"] = _int_strict(
            values.get("week_interval"), 1, "Every N weeks", minimum=1
        )
    elif cycle == CYCLE_DAYS_OF_MONTH:
        days, last = parse_month_days(values.get("month_days"), strict=True)
        if not days and not last:
            raise SettlementTermsError(
                "Pick at least one day of the month (1-31 or 'last')."
            )
        out["month_days"] = format_month_days(days, last)
    elif cycle == CYCLE_EVERY_N_DAYS:
        interval = _int_strict(values.get("interval_days"), None, "Every N days", minimum=1)
        if interval is None:
            raise SettlementTermsError("Enter how many days apart the payments are (1 or more).")
        out["interval_days"] = interval

    return out


def needs_anchor(normalized: Dict[str, Any]) -> bool:
    """True when the cycle counts from ``anchor_date`` (so a default must be set)."""
    cycle = normalized.get("cycle")
    if cycle == CYCLE_EVERY_N_DAYS:
        return True
    return cycle == CYCLE_WEEKLY and int(normalized.get("week_interval") or 1) > 1


def parse_terms(row: Any) -> Optional[Dict[str, Any]]:
    """A stored terms row (dict or Document) -> normalized dict. Never raises.

    Lenient on purpose: this runs inside the daily reminder pass and the
    collections list, where one bad record must degrade to "no schedule", not
    take the whole pass down. The strict gate is :func:`normalize_terms_input`.
    """
    if row is None:
        return None
    raw_cycle = _get(row, "cycle")
    cycle = canonical_cycle(raw_cycle) or (str(raw_cycle).strip() if raw_cycle else None)
    month_days, month_last = parse_month_days(_get(row, "month_days"), strict=False)
    interval_days = _int_lenient(_get(row, "interval_days"), None)
    if interval_days is not None and interval_days < 1:
        interval_days = None
    anchor = _to_date_lenient(_get(row, "anchor_date")) or _to_date_lenient(_get(row, "creation"))
    return {
        "name": _get(row, "name"),
        "customer": _get(row, "customer"),
        "enabled": _truthy(_get(row, "enabled"), True),
        "cycle": cycle,
        "weekdays": parse_weekdays(_get(row, "weekdays"), strict=False),
        "week_interval": max(1, _int_lenient(_get(row, "week_interval"), 1) or 1),
        "month_days": month_days,
        "month_last": month_last,
        "interval_days": interval_days,
        "anchor_date": anchor,
        "remind_days_before": max(0, _int_lenient(_get(row, "remind_days_before"), 1) or 0),
        "overdue_repeat_days": max(1, _int_lenient(_get(row, "overdue_repeat_days"), 2) or 1),
        "responsible_user": (str(_get(row, "responsible_user") or "").strip() or None),
        "notes": (str(_get(row, "notes") or "").strip() or None),
        "last_reminder_on": _to_date_lenient(_get(row, "last_reminder_on")),
        "last_reminder_kind": (str(_get(row, "last_reminder_kind") or "").strip() or None),
    }


# ---------------------------------------------------------------------------
# Schedule arithmetic
# ---------------------------------------------------------------------------


def has_schedule(terms: Optional[Dict[str, Any]]) -> bool:
    """True when *terms* produce calendar due dates."""
    if not terms:
        return False
    cycle = terms.get("cycle")
    if cycle == CYCLE_WEEKLY:
        return bool(terms.get("weekdays"))
    if cycle == CYCLE_DAYS_OF_MONTH:
        return bool(terms.get("month_days")) or bool(terms.get("month_last"))
    if cycle == CYCLE_EVERY_N_DAYS:
        return bool(terms.get("interval_days")) and int(terms["interval_days"]) >= 1
    return False


def _anchor(terms: Dict[str, Any]) -> datetime.date:
    return terms.get("anchor_date") or DEFAULT_ANCHOR


def _monday(value: datetime.date) -> datetime.date:
    return value - datetime.timedelta(days=value.weekday())


def _is_due(terms: Dict[str, Any], day: datetime.date) -> bool:
    cycle = terms.get("cycle")
    if cycle == CYCLE_WEEKLY:
        if day.weekday() not in (terms.get("weekdays") or []):
            return False
        interval = max(1, int(terms.get("week_interval") or 1))
        if interval == 1:
            return True
        weeks = (_monday(day) - _monday(_anchor(terms))).days // 7
        return weeks % interval == 0
    if cycle == CYCLE_DAYS_OF_MONTH:
        last_day = calendar.monthrange(day.year, day.month)[1]
        if terms.get("month_last") and day.day == last_day:
            return True
        # A day past the month's length clamps to its last day: "31" on a
        # 30-day month is the 30th, "30" in February is the 28th/29th.
        return any(min(int(n), last_day) == day.day for n in (terms.get("month_days") or []))
    if cycle == CYCLE_EVERY_N_DAYS:
        interval = int(terms.get("interval_days") or 0)
        if interval < 1:
            return False
        anchor = _anchor(terms)
        if day < anchor:
            return False
        return (day - anchor).days % interval == 0
    return False


def _horizon(terms: Dict[str, Any]) -> int:
    cycle = terms.get("cycle")
    if cycle == CYCLE_WEEKLY:
        return 7 * max(1, int(terms.get("week_interval") or 1)) + 7
    if cycle == CYCLE_DAYS_OF_MONTH:
        return 62
    if cycle == CYCLE_EVERY_N_DAYS:
        return int(terms.get("interval_days") or 1) + 1
    return 0


def next_due_date(terms: Optional[Dict[str, Any]], today: Any) -> Optional[datetime.date]:
    """First due date on or after *today* (today counts). ``None`` for
    On Delivery / Invoice after Invoice or a schedule with no dates."""
    if not has_schedule(terms):
        return None
    start = to_date(today)
    if start is None:
        return None
    if terms["cycle"] == CYCLE_EVERY_N_DAYS:
        interval = int(terms["interval_days"])
        anchor = _anchor(terms)
        if start <= anchor:
            return anchor
        steps = -(-(start - anchor).days // interval)  # ceil division
        return anchor + datetime.timedelta(days=steps * interval)
    for offset in range(_horizon(terms) + 1):
        day = start + datetime.timedelta(days=offset)
        if _is_due(terms, day):
            return day
    return None


def previous_due_date(terms: Optional[Dict[str, Any]], today: Any) -> Optional[datetime.date]:
    """Last due date strictly BEFORE *today*, or ``None``."""
    if not has_schedule(terms):
        return None
    start = to_date(today)
    if start is None:
        return None
    if terms["cycle"] == CYCLE_EVERY_N_DAYS:
        interval = int(terms["interval_days"])
        anchor = _anchor(terms)
        if start <= anchor:
            return None
        steps = ((start - anchor).days - 1) // interval
        return anchor + datetime.timedelta(days=steps * interval)
    for offset in range(1, _horizon(terms) + 1):
        day = start - datetime.timedelta(days=offset)
        if _is_due(terms, day):
            return day
    return None


def due_dates_between(terms: Optional[Dict[str, Any]], start: Any, end: Any) -> List[datetime.date]:
    """Every due date in ``[start, end]`` (inclusive), ascending."""
    first = to_date(start)
    last = to_date(end)
    if not has_schedule(terms) or first is None or last is None or first > last:
        return []
    if (last - first).days > _MAX_SCAN_DAYS:
        last = first + datetime.timedelta(days=_MAX_SCAN_DAYS)
    if terms["cycle"] == CYCLE_EVERY_N_DAYS:
        out: List[datetime.date] = []
        day = next_due_date(terms, first)
        step = datetime.timedelta(days=int(terms["interval_days"]))
        while day is not None and day <= last:
            out.append(day)
            day = day + step
        return out
    out = []
    day = first
    one = datetime.timedelta(days=1)
    while day <= last:
        if _is_due(terms, day):
            out.append(day)
        day += one
    return out


def upcoming_due_dates(terms: Optional[Dict[str, Any]], today: Any, count: int = UPCOMING_COUNT) -> List[datetime.date]:
    """The next *count* due dates, today included."""
    out: List[datetime.date] = []
    day = next_due_date(terms, today)
    while day is not None and len(out) < count:
        out.append(day)
        day = next_due_date(terms, day + datetime.timedelta(days=1))
    return out


# ---------------------------------------------------------------------------
# Human description
# ---------------------------------------------------------------------------


def _ordinal(n: int) -> str:
    if 10 <= n % 100 <= 20:
        suffix = "th"
    else:
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def _join_en(parts: Sequence[str]) -> str:
    parts = list(parts)
    if not parts:
        return ""
    if len(parts) == 1:
        return parts[0]
    return ", ".join(parts[:-1]) + " and " + parts[-1]


def _join_ar(parts: Sequence[str]) -> str:
    return " و".join(parts)


def _ar_count(n: int, one: str, two: str, few: str, many: str) -> str:
    if n == 1:
        return one
    if n == 2:
        return two
    if 3 <= n <= 10:
        return f"{n} {few}"
    return f"{n} {many}"


def describe(terms: Optional[Dict[str, Any]], lang: str = "en") -> str:
    """One line a person reads: "Every Thursday", "Every 10 days", ..."""
    arabic = str(lang or "en").lower().startswith("ar")
    if not terms or not terms.get("cycle"):
        return "لا يوجد جدول سداد" if arabic else "No settlement schedule"
    cycle = terms["cycle"]

    if cycle == CYCLE_ON_DELIVERY:
        return "يدفع عند الاستلام" if arabic else "Pays on delivery"
    if cycle == CYCLE_INVOICE_AFTER_INVOICE:
        return (
            "يدفع الفاتورة السابقة مع كل توصيل"
            if arabic
            else "Pays the previous invoice on each delivery"
        )
    if cycle == CYCLE_WEEKLY:
        days = terms.get("weekdays") or []
        interval = max(1, int(terms.get("week_interval") or 1))
        if arabic:
            if not days:
                return "أسبوعيًا (لم يُحدد يوم)"
            names = _join_ar([_WEEKDAY_NAMES_AR[d] for d in days])
            if interval == 1:
                return f"أسبوعيًا: {names}"
            return "كل " + _ar_count(interval, "أسبوع", "أسبوعين", "أسابيع", "أسبوعًا") + f": {names}"
        if not days:
            return "Weekly (no day chosen)"
        names = _join_en([_WEEKDAY_NAMES_EN[d] for d in days])
        if interval == 1:
            return f"Every {names}"
        return f"Every {interval} weeks on {names}"
    if cycle == CYCLE_DAYS_OF_MONTH:
        days = terms.get("month_days") or []
        last = bool(terms.get("month_last"))
        if arabic:
            parts = [f"يوم {d}" for d in days] + (["آخر يوم"] if last else [])
            if not parts:
                return "شهريًا (لم يُحدد يوم)"
            return f"شهريًا: {_join_ar(parts)} من كل شهر"
        parts = [_ordinal(d) for d in days] + (["the last day"] if last else [])
        if not parts:
            return "Monthly (no day chosen)"
        prefix = "On " if parts[0].startswith("the ") else "On the "
        return f"{prefix}{_join_en(parts)} of each month"
    if cycle == CYCLE_EVERY_N_DAYS:
        interval = terms.get("interval_days")
        if not interval:
            return "كل عدة أيام (لم تُحدد المدة)" if arabic else "Every N days (interval not set)"
        interval = int(interval)
        if arabic:
            return "كل " + _ar_count(interval, "يوم", "يومين", "أيام", "يومًا")
        return "Every day" if interval == 1 else f"Every {interval} days"
    return str(cycle)


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------


def _invoice_rows(open_invoices: Optional[Iterable[Any]], today: datetime.date) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for index, inv in enumerate(open_invoices or []):
        amount = _money(_get(inv, "outstanding_amount"))
        if amount <= MONEY_EPSILON:
            continue
        # A row with no readable date is still money owed; it is dated today
        # rather than dropped, because dropping it would understate the debt.
        posting = _to_date_lenient(_get(inv, "posting_date")) or today
        rows.append({"name": _get(inv, "name"), "posting_date": posting, "amount": amount, "_i": index})
    # Stable: equal dates keep the caller's order (the ledger query's creation asc).
    rows.sort(key=lambda r: (r["posting_date"], r["_i"]))
    return rows


def _round(value: float) -> float:
    return round(float(value or 0.0) + 0.0, 2)


def compute_status(
    terms: Optional[Dict[str, Any]],
    open_invoices: Optional[Iterable[Any]],
    today: Any,
    soon_days: Optional[int] = None,
) -> Dict[str, Any]:
    """Where a customer stands against their schedule on *today*.

    *terms* is a :func:`parse_terms` dict (or ``None`` = no terms record);
    *open_invoices* are the customer's OPEN credit invoices as
    ``{name, posting_date, outstanding_amount}``. *soon_days* optionally widens
    the "due soon" window beyond ``remind_days_before`` (the collections list's
    ``days_ahead``); the reminder decision never passes it.

    Every date in the result is an ISO string (or ``None``) and every amount a
    float rounded to 2 places, so the dict goes on the wire unchanged.
    """
    day = to_date(today)
    rows = _invoice_rows(open_invoices, day)
    balance = sum(r["amount"] for r in rows)
    cycle = (terms or {}).get("cycle")
    dated = has_schedule(terms)

    result: Dict[str, Any] = {
        "state": STATE_NONE,
        "cycle": cycle,
        "next_due_date": None,
        "next_due_amount": 0.0,
        "due_now_amount": 0.0,
        "overdue_amount": 0.0,
        "open_balance": _round(balance),
        "oldest_overdue_date": None,
        "upcoming_dates": [_iso(d) for d in upcoming_due_dates(terms, day)] if dated else [],
        "collect_on_next_delivery": 0.0,
        "invoice_count": len(rows),
        "invoices": [],
    }
    if dated:
        result["next_due_date"] = _iso(next_due_date(terms, day))

    def _emit(row: Dict[str, Any], due: Optional[datetime.date], overdue: bool) -> None:
        result["invoices"].append(
            {
                "name": row["name"],
                "posting_date": _iso(row["posting_date"]),
                "outstanding_amount": _round(row["amount"]),
                "due_date": _iso(due),
                "overdue": bool(overdue),
            }
        )

    if balance <= MONEY_EPSILON:
        return result

    if terms is None or not cycle or (cycle in DATE_CYCLES and not dated) or (
        cycle not in CYCLES
    ):
        for row in rows:
            _emit(row, None, False)
        result["state"] = STATE_UNSCHEDULED
        return result

    due_now = 0.0
    overdue = 0.0
    oldest_overdue: Optional[datetime.date] = None

    if cycle == CYCLE_ON_DELIVERY:
        # A cash shop should owe nothing: anything open was due at the door.
        for row in rows:
            _emit(row, row["posting_date"], True)
        due_now = overdue = balance
        oldest_overdue = rows[0]["posting_date"]
        result["state"] = STATE_OVERDUE

    elif cycle == CYCLE_INVOICE_AFTER_INVOICE:
        newest = rows[-1]
        for row in rows[:-1]:
            is_late = row["posting_date"] < newest["posting_date"]
            due_now += row["amount"]
            if is_late:
                overdue += row["amount"]
            _emit(row, newest["posting_date"], is_late)
        _emit(newest, None, False)
        result["collect_on_next_delivery"] = _round(newest["amount"])
        if overdue > MONEY_EPSILON:
            oldest_overdue = newest["posting_date"]
            result["state"] = STATE_OVERDUE
        elif due_now > MONEY_EPSILON:
            result["state"] = STATE_DUE_TODAY
        else:
            result["state"] = STATE_OK

    else:
        dues: List[Tuple[Dict[str, Any], Optional[datetime.date]]] = []
        for row in rows:
            due = next_due_date(terms, row["posting_date"] + datetime.timedelta(days=1))
            is_late = due is not None and due < day
            if due is not None and due <= day:
                due_now += row["amount"]
            if is_late:
                overdue += row["amount"]
                if oldest_overdue is None or due < oldest_overdue:
                    oldest_overdue = due
            dues.append((row, due))
            _emit(row, due, is_late)
        # The next COLLECTION: the earliest due date (today included) that
        # money actually falls due on. A payment day on which nothing is owed
        # yet (the only invoice was delivered that same day) is not it --
        # reporting it with 0 would hide the real one a week later. With
        # nothing pending, it is simply the next schedule date.
        pending = [due for _row, due in dues if due is not None and due >= day]
        upcoming = min(pending) if pending else next_due_date(terms, day)
        next_amount = sum(r["amount"] for r, due in dues if due is not None and due == upcoming)
        result["next_due_date"] = _iso(upcoming)
        result["next_due_amount"] = _round(next_amount)
        window = max(int(terms.get("remind_days_before") or 0), int(soon_days or 0))
        if overdue > MONEY_EPSILON:
            result["state"] = STATE_OVERDUE
        elif due_now > MONEY_EPSILON:
            result["state"] = STATE_DUE_TODAY
        elif (
            upcoming is not None
            and next_amount > MONEY_EPSILON
            and (upcoming - day).days <= window
        ):
            result["state"] = STATE_DUE_SOON
        else:
            result["state"] = STATE_OK

    result["due_now_amount"] = _round(due_now)
    result["overdue_amount"] = _round(overdue)
    result["oldest_overdue_date"] = _iso(oldest_overdue)
    return result


# ---------------------------------------------------------------------------
# Reminder decision
# ---------------------------------------------------------------------------


def plan_reminder(terms: Optional[Dict[str, Any]], status: Dict[str, Any], today: Any) -> Optional[str]:
    """Which reminder (if any) the daily pass sends for this customer today.

    * never twice in one day (``last_reminder_on == today``);
    * ``overdue`` while anything is overdue, repeated every
      ``overdue_repeat_days`` (the first one goes out at once);
    * ``due_today`` when something falls due exactly today;
    * ``due_soon`` exactly ``remind_days_before`` days ahead of the next due
      date (never when ``remind_days_before`` is 0).
    """
    if not terms or not terms.get("enabled", True):
        return None
    if _money(status.get("open_balance")) <= MONEY_EPSILON:
        return None
    day = to_date(today)
    last_on = terms.get("last_reminder_on")
    if not isinstance(last_on, datetime.date):
        last_on = _to_date_lenient(last_on)
    last_kind = terms.get("last_reminder_kind")
    if last_on is not None and last_on == day:
        return None
    repeat = max(1, int(terms.get("overdue_repeat_days") or 1))

    def _repeat_ok(kind: str) -> bool:
        return last_kind != kind or last_on is None or (day - last_on).days >= repeat

    overdue = _money(status.get("overdue_amount"))
    due_now = _money(status.get("due_now_amount"))
    if overdue > MONEY_EPSILON and _repeat_ok(KIND_OVERDUE):
        return KIND_OVERDUE
    if due_now - overdue > MONEY_EPSILON and _repeat_ok(KIND_DUE_TODAY):
        return KIND_DUE_TODAY
    ahead = int(terms.get("remind_days_before") or 0)
    upcoming = _to_date_lenient(status.get("next_due_date"))
    if (
        ahead > 0
        and upcoming is not None
        and _money(status.get("next_due_amount")) > MONEY_EPSILON
        and (upcoming - day).days == ahead
    ):
        return KIND_DUE_SOON
    return None


def reminder_amount(kind: str, status: Dict[str, Any]) -> float:
    """The figure a reminder of *kind* asks the manager to collect."""
    if kind == KIND_DUE_SOON:
        return _round(_money(status.get("next_due_amount")))
    if kind == KIND_COLLECT_ON_DELIVERY:
        return _round(_money(status.get("due_now_amount")))
    return _round(_money(status.get("due_now_amount")))


def reminder_due_date(kind: str, status: Dict[str, Any], today: Any) -> Optional[str]:
    if kind == KIND_OVERDUE:
        return status.get("oldest_overdue_date") or _iso(to_date(today))
    if kind == KIND_DUE_SOON:
        return status.get("next_due_date")
    return _iso(to_date(today))


def reminder_text(
    kind: str,
    customer_name: str,
    amount: float,
    currency: str,
    due_date: Optional[str],
    description: Optional[str] = None,
) -> Tuple[str, str]:
    """(title, body) for a settlement push. English, like every other push."""
    money = f"{amount:,.2f} {currency or ''}".strip()
    who = customer_name or "customer"
    if kind == KIND_OVERDUE:
        title = f"Overdue: collect {money} from {who}"
        body = f"Payment overdue since {due_date}." if due_date else "Payment overdue."
    elif kind == KIND_DUE_TODAY:
        title = f"Due today: collect {money} from {who}"
        body = "Payment is due today."
    elif kind == KIND_DUE_SOON:
        title = f"Due {due_date}: {money} from {who}" if due_date else f"Due soon: {money} from {who}"
        body = "Settlement coming up."
    elif kind == KIND_COLLECT_ON_DELIVERY:
        title = f"Collect {money} from {who}"
        body = "Collect for the previous invoice(s) with this delivery."
    else:
        title = f"Settlement reminder: {who}"
        body = money
    if description:
        body = f"{body} Terms: {description}."
    return title, body


def todo_date(status: Dict[str, Any], today: Any) -> Optional[str]:
    """Date the customer's settlement ToDo should carry, or ``None`` to close it.

    Open while something is due, overdue or coming due; closed once nothing is
    ("the balance due is 0") -- a balance that is merely not due yet is ``ok``.
    """
    state = status.get("state")
    if state == STATE_OVERDUE:
        return status.get("oldest_overdue_date") or _iso(to_date(today))
    if state == STATE_DUE_TODAY:
        return _iso(to_date(today))
    if state == STATE_DUE_SOON:
        return status.get("next_due_date")
    return None


# ---------------------------------------------------------------------------
# Collections list
# ---------------------------------------------------------------------------


def _sort_key(row: Dict[str, Any]) -> Tuple[Any, ...]:
    state = row.get("state")
    rank = STATE_ORDER.index(state) if state in STATE_ORDER else len(STATE_ORDER)
    far = "9999-12-31"
    if state == STATE_OVERDUE:
        date_key, amount = row.get("oldest_overdue_date") or far, row.get("overdue_amount")
    elif state == STATE_DUE_TODAY:
        date_key, amount = "", row.get("due_now_amount")
    elif state == STATE_DUE_SOON:
        date_key, amount = row.get("next_due_date") or far, row.get("next_due_amount")
    elif state == STATE_UNSCHEDULED:
        date_key, amount = "", row.get("open_balance")
    else:
        date_key, amount = row.get("next_due_date") or far, row.get("open_balance")
    name = str(row.get("customer_name") or row.get("customer") or "").lower()
    return (rank, date_key, -_money(amount), name, str(row.get("customer") or ""))


def build_collection_rows(
    entries: Iterable[Dict[str, Any]],
    today: Any,
    soon_days: Optional[int] = None,
) -> Tuple[List[Dict[str, Any]], Dict[str, int]]:
    """Rows + counts for the collections view. Pure.

    *entries*: ``{customer, customer_name, terms (parse_terms dict|None),
    invoices, responsible_user}`` per customer. A customer in state ``none``
    (nothing open) is left out. Sorted overdue -> due_today -> due_soon ->
    unscheduled -> ok; within a state the most urgent first (oldest overdue,
    nearest date), then the biggest amount, then the name.
    """
    rows: List[Dict[str, Any]] = []
    counts = {STATE_OVERDUE: 0, STATE_DUE_TODAY: 0, STATE_DUE_SOON: 0, STATE_UNSCHEDULED: 0}
    for entry in entries or []:
        terms = entry.get("terms")
        status = compute_status(terms, entry.get("invoices"), today, soon_days=soon_days)
        if status["state"] == STATE_NONE:
            continue
        if status["state"] in counts:
            counts[status["state"]] += 1
        rows.append(
            {
                "customer": entry.get("customer"),
                "customer_name": entry.get("customer_name") or entry.get("customer"),
                "cycle": (terms or {}).get("cycle") if terms else None,
                "description": describe(terms) if terms else None,
                "description_ar": describe(terms, "ar") if terms else None,
                "enabled": bool((terms or {}).get("enabled", True)) if terms else None,
                "state": status["state"],
                "next_due_date": status["next_due_date"],
                "next_due_amount": status["next_due_amount"],
                "due_now_amount": status["due_now_amount"],
                "overdue_amount": status["overdue_amount"],
                "open_balance": status["open_balance"],
                "oldest_overdue_date": status["oldest_overdue_date"],
                "collect_on_next_delivery": status["collect_on_next_delivery"],
                "invoice_count": status["invoice_count"],
                "responsible_user": (terms or {}).get("responsible_user") if terms else None,
            }
        )
    rows.sort(key=_sort_key)
    return rows, counts


def needs_attention(state: Optional[str]) -> bool:
    """States the approvals badge counts."""
    return state in (STATE_OVERDUE, STATE_DUE_TODAY)
