"""Amount history for ``Jarz Recurring Expense`` — pure helpers, no DB access.

A rent raise happens at a point in time; the months before it must keep the
amount that was actually due then. The registry therefore keeps a dated list of
rates (child table ``Jarz Recurring Expense Rate``) and every screen asks
``amount_for_month`` instead of reading the single ``amount`` field.

Rules (one definition, shared by the DocType, both APIs and the backfill patch):

* ``effective_from`` is always the FIRST day of a month.
* The amount in force for a month is the last row whose ``effective_from`` is on
  or before that month's last day.
* A month before the first row uses the first row's amount.
* No history at all falls back to the document's own ``amount``.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from datetime import date
from typing import Any

from frappe.utils import flt, getdate

_NUMBER = re.compile(r"\d[\d,]*(?:\.\d+)?")
_YEAR_MONTH = re.compile(r"^\s*(\d{4})-(\d{1,2})\s*$")


def first_of_month(value: Any) -> date:
	d = getdate(value)
	return date(d.year, d.month, 1)


def month_end_of(value: Any) -> date:
	import calendar

	d = getdate(value)
	return date(d.year, d.month, calendar.monthrange(d.year, d.month)[1])


def normalize_effective_from(value: Any) -> date:
	"""``'YYYY-MM'`` or any date → the first day of that month. Raises ``ValueError``."""
	if value in (None, ""):
		raise ValueError("Effective month is required.")
	if isinstance(value, str):
		match = _YEAR_MONTH.match(value)
		if match:
			year, month = int(match.group(1)), int(match.group(2))
			if not 1 <= month <= 12:
				raise ValueError(f"Invalid effective month: {value}")
			return date(year, month, 1)
	try:
		return first_of_month(value)
	except Exception:
		raise ValueError(f"Invalid effective month: {value}")


def _row_pairs(history: Iterable[Any] | None) -> list[tuple[date, float]]:
	pairs: list[tuple[date, float]] = []
	for row in history or []:
		get = row.get if hasattr(row, "get") else (lambda k, d=None: getattr(row, k, d))
		eff = get("effective_from")
		if not eff:
			continue
		pairs.append((getdate(eff), flt(get("amount"))))
	pairs.sort(key=lambda p: p[0])
	return pairs


def history_payload(raw: dict[str, Any]) -> list[dict[str, Any]]:
	"""``[{effective_from: 'YYYY-MM-DD', amount}]``, ascending."""
	return [
		{"effective_from": eff.isoformat(), "amount": amt}
		for eff, amt in _row_pairs(raw.get("amount_history"))
	]


def amount_for_month(raw: dict[str, Any], month_start: date, month_end: date | None = None) -> float:
	"""The amount a registry item owes for the month ``month_start``..``month_end``."""
	pairs = _row_pairs(raw.get("amount_history"))
	if not pairs:
		return flt(raw.get("amount"))
	limit = getdate(month_end) if month_end else month_end_of(month_start)
	in_force = pairs[0][1]
	for eff, amt in pairs:
		if eff <= limit:
			in_force = amt
		else:
			break
	return in_force


def attach_amount_history(registry: Sequence[dict[str, Any]], rate_rows: Iterable[dict[str, Any]]) -> None:
	"""Attach ``amount_history`` to each registry dict from child rows (``parent`` keyed)."""
	by_parent: dict[str, list[dict[str, Any]]] = {}
	for r in rate_rows:
		by_parent.setdefault(r.get("parent"), []).append(
			{"effective_from": r.get("effective_from"), "amount": r.get("amount")}
		)
	for raw in registry:
		raw["amount_history"] = by_parent.get(raw.get("name"), [])


def parse_version_amount(text: Any) -> float | None:
	"""``'£ or ج.م 5,500.00'`` → 5500.0 (digits and the dot only). ``None`` if no digits."""
	if text is None:
		return None
	if isinstance(text, (int, float)):
		return float(text)
	# The currency symbol can itself contain a dot ("ج.م"), so take the first
	# number in the text rather than stripping everything that is not a digit.
	match = _NUMBER.search(str(text))
	if not match:
		return None
	return float(match.group(0).replace(",", ""))


def plan_history_change(
	rows: Sequence[dict[str, Any]],
	*,
	is_new: bool,
	old_amount: Any,
	new_amount: Any,
	start_date: Any,
	effective_from: Any = None,
	today_date: Any = None,
	tolerance: float = 0.005,
) -> tuple[list[dict[str, Any]], float]:
	"""Compute the history rows and the current ``amount`` a save should leave.

	``rows`` are the document's present history rows (dicts with ``effective_from``
	and ``amount``). Returns ``(rows, amount_today)``; ``rows`` ascending, every
	``effective_from`` a first-of-month ``date``. Raises ``ValueError`` on a
	non-positive amount or two rows in one month.
	"""
	start_month = first_of_month(start_date)
	today_month = first_of_month(today_date or date.today())
	effective = normalize_effective_from(effective_from) if effective_from else today_month

	# Normalise (first of month, clamp to the start month), keeping order stable.
	normalised: list[tuple[date, float]] = []
	for eff, amt in _row_pairs(rows):
		if amt <= 0:
			raise ValueError("Every amount in the history must be greater than zero.")
		normalised.append((max(first_of_month(eff), start_month), amt))
	seen: set[date] = set()
	for eff, _amt in normalised:
		if eff in seen:
			raise ValueError(f"Two amounts are set for {eff.strftime('%Y-%m')}. Keep one per month.")
		seen.add(eff)

	if is_new:
		if not normalised:
			normalised = [(start_month, flt(new_amount))]
	else:
		changed = abs(flt(old_amount) - flt(new_amount)) > tolerance
		if not normalised:
			# Legacy row with no history: what it owed so far is the OLD amount.
			normalised = [(start_month, flt(old_amount) if changed else flt(new_amount))]
		if changed:
			effective = max(effective, start_month)
			normalised = [p for p in normalised if p[0] != effective]
			normalised.append((effective, flt(new_amount)))
			normalised.sort(key=lambda p: p[0])

	out = [{"effective_from": eff, "amount": amt} for eff, amt in normalised]
	today_amount = amount_for_month({"amount": new_amount, "amount_history": out}, today_month)
	return out, today_amount


def history_from_versions(
	start_date: Any,
	current_amount: Any,
	versions: Sequence[dict[str, Any]],
) -> list[dict[str, Any]]:
	"""Rebuild a history from ``Version`` data (backfill).

	``versions`` ascending by creation: ``{"creation": datetime|str, "changed": [[field, old, new], ...]}``.
	Seed = start month at the FIRST amount change's old value (or the current amount
	when the amount never changed); then each amount change adds its creation
	month at the NEW value (same month overwrites).
	"""
	start_month = first_of_month(start_date)
	changes: list[tuple[date, float | None, float | None]] = []
	for v in versions:
		for entry in v.get("changed") or []:
			if len(entry) >= 3 and entry[0] == "amount":
				changes.append(
					(
						first_of_month(v.get("creation")),
						parse_version_amount(entry[1]),
						parse_version_amount(entry[2]),
					)
				)
	if not changes:
		return [{"effective_from": start_month, "amount": flt(current_amount)}]

	seed = changes[0][1]
	if seed is None:
		seed = flt(current_amount)
	history: dict[date, float] = {start_month: seed}
	for month, _old, new in changes:
		if new is None:
			continue
		history[max(month, start_month)] = new
	return [{"effective_from": k, "amount": v} for k, v in sorted(history.items())]
