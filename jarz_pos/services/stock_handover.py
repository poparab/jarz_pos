"""Stock Handover: a stock movement is official only once the other side confirms it.

Branches and the factory entered their transfers late and wrong, because one
person typed both halves of a movement. Here one side DECLARES ("I received 12
jars from Factory" / "Factory took 12 jars from me") and the other side answers:

* **confirm** -- posts the Material Transfer Stock Entry, at that moment;
* **change**  -- proposes other quantities and hands it back to the first side;
* **reject**  -- closes it, nothing posted.

The declaring side may **cancel** while it is pending and nobody has changed it.
The user who made the latest proposal (declaration or change) can never confirm
it, so one person can never move stock on their own say-so.

Sides are warehouses:

* the **factory** is the Company's ``default_fg_warehouse``; the production team
  (``ROLES.PRODUCTION_EXECUTE``) and managers (``ROLES.STOCK_TRANSFER``) act for it;
* a **branch** is any warehouse an enabled POS Profile sells from; a user acts
  for it with their OWN open shift (POS Opening Entry) on a POS Profile they are
  assigned to whose warehouse it is -- or as a manager.

Branch users never send their own warehouse as a free choice nor a date: the
branch side is derived from ``pos_profile`` + the open shift and checked against
``my_warehouse``, and a confirmation always posts at "now".

Transit model (v3, Feature: stock-transit-matching). Each side answers only for
its own half of a movement it cannot see the other end of:

* **send** ("I sent out", kind ``Dispatch``): names no destination and posts AT
  ONCE a Material Transfer sender -> the company's transit warehouse
  (``transit_entry``). The stock is then "in transit", nobody's shelf.
* **receipt** ("I received", a ``Handover`` declared by the To side): names the
  sender, or leaves ``from_warehouse`` blank ("I don't know who sent it").
* A **pool receipt** is a pending receipt that is unsourced, or names a sender
  with an open transit send. A manager approves it on the transit board
  (:func:`get_transit_board` / :func:`approve_receipt`): ONE Material Transfer
  transit -> receiver per send allocation (sender -> receiver for a "sender never
  declared it" direct allocation), a ``Jarz Stock Transit Match`` row per
  allocation, and ``matched_qty`` bumped on both sides. The pure engine in
  ``jarz_pos.services.transit_match`` recommends the allocation with reasons.
* What is left of a send stays in transit, aged, until it is matched,
  :func:`return_to_sender` (transit -> sender) or :func:`write_off_transit`
  (Material Issue from transit). A send closes Confirmed once every line's
  ``remaining = qty - matched - written_off - returned`` is 0; its declarer or a
  manager may :func:`cancel` it (the transit entry is cancelled) only while
  nothing on it has moved on.
* A receipt naming a sender with NO open send keeps the one-to-one flow above.

Locks: Dispatches before receipts, each group in name order (the one global
order; see :func:`_lock_dispatches`), then status is re-checked, so a receipt is
never posted twice.

Legacy (v2) sent-out batches without ``transit_entry`` still form per-sender
**match groups** confirmed by :func:`confirm_match`; it refuses any group holding
a transit send ("Update the app to match transit sends.").

API surface: ``jarz_pos.api.stock_handover`` (thin, whitelisted). Managers'
``api.transfer.submit_transfer`` routes branch-touching transfers here through
:func:`create_manager_handover`.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Iterable, Sequence
from datetime import timedelta
from typing import Any

import frappe
from frappe import _
from frappe.utils import cint, flt, get_datetime, now_datetime

from jarz_pos.api import transfer as transfer_api
from jarz_pos.constants import DEFAULT_UOM, ROLES
from jarz_pos.services import transit_match
from jarz_pos.utils.access_control import (
	BranchAccessError,
	ShiftRequiredError,
	get_user_pos_profiles,
	get_users_for_pos_profiles,
)

DOCTYPE = "Jarz Stock Handover"
ITEM_DOCTYPE = "Jarz Stock Handover Item"

STATUS_PENDING = "Pending"
STATUS_CONFIRMED = "Confirmed"
STATUS_REJECTED = "Rejected"
STATUS_CANCELLED = "Cancelled"
CLOSED_STATUSES = (STATUS_CONFIRMED, STATUS_REJECTED, STATUS_CANCELLED)

SIDE_FROM = "From"
SIDE_TO = "To"
#: ``awaiting_side`` of a Dispatch (and, in effect, of every receipt in its group).
SIDE_MANAGER = "Manager"

#: ``kind`` of a row: a normal one-to-one handover, or a "sent out" declaration.
DOC_KIND_HANDOVER = "Handover"
DOC_KIND_DISPATCH = "Dispatch"

SOURCE_BRANCH = "Branch"
SOURCE_FACTORY = "Factory"
SOURCE_MANAGER = "Manager"

KIND_FACTORY = "factory"
KIND_BRANCH = "branch"

FACTORY_LABEL = "Factory"
MANAGER_LABEL = "Manager"

NOTIFICATION_TYPE = "stock_handover"
NOTIFICATION_ROUTE = "/stock-transfer"

#: A pending handover nobody has answered for this long is pushed again, and at
#: most once per this window.
REMIND_AFTER_HOURS = 12
#: How far back ``list_handovers`` shows closed handovers.
RECENT_DAYS = 7
#: Upper bound on pending rows read per list call (pending ones are few).
_PENDING_SCAN_LIMIT = 500

EVENT_DECLARED = "declared"
EVENT_CHANGED = "changed"
EVENT_CONFIRMED = "confirmed"
EVENT_REJECTED = "rejected"
EVENT_CANCELLED = "cancelled"
EVENT_REMINDER = "reminder"
#: Match-group events (managers / declarers, not a warehouse side).
EVENT_BALANCED = "batch_balanced"
EVENT_BATCH_CONFIRMED = "batch_confirmed"
EVENT_BATCH_REMINDER = "batch_reminder"
#: Something new to match on the transit board (a send, or a pool receipt). Managers.
EVENT_TRANSIT = "transit_to_match"
_BATCH_MANAGER_EVENTS = (EVENT_BALANCED, EVENT_BATCH_REMINDER, EVENT_TRANSIT)

#: Quantities are whole units or fractions to this many decimals.
QTY_DP = 3
_QTY_EPS = 5e-4

_REJECT_NOT_IN_BATCH = "Not in {0}'s shipment (manager match)"
_DRIFT_MESSAGE = "Something changed in this batch. Reload and check again."

TRANSIT_MATCH_DOCTYPE = "Jarz Stock Transit Match"
UNKNOWN_SENDER_LABEL = "Unknown sender"
#: A send open longer than this is "overdue" on the transit board.
TRANSIT_OVERDUE_HOURS = 24
#: History window of the "usual supplier" affinity.
AFFINITY_DAYS = 60
#: Managers hear about new transit work at most once per this window.
TRANSIT_PUSH_DEBOUNCE_SEC = 300
_TRANSIT_PUSH_CACHE_KEY = "jarz_pos:stock_transit_push"
#: ``match_note`` prefix of a receipt a manager answered on the transit board.
_TRANSIT_NOTE_PREFIX = "Transit: "
_TRANSIT_UPDATE_APP = "Update the app to match transit sends."
_CONFIDENCES = ("high", "medium", "low")


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def _safe_log(title: str) -> None:
	"""``frappe.log_error`` that can never become the failure (it can raise)."""
	try:
		frappe.log_error(frappe.get_traceback(), title[:140], defer_insert=True)
	except TypeError:
		try:
			frappe.log_error(frappe.get_traceback(), title[:140])
		except Exception:
			pass
	except Exception:
		pass


def _suppressed(context: str) -> bool:
	"""True during a test run (CI runs on live staging). A failed import counts as suppressed."""
	try:
		from jarz_pos.api.notifications import outbound_alerts_suppressed

		return bool(outbound_alerts_suppressed(context))
	except Exception:
		_safe_log("stock_handover: suppression check failed")
		return True


def _g(row: Any, key: str, default: Any = None) -> Any:
	"""Read ``key`` off a Document, a dict or a namespace alike."""
	if row is None:
		return default
	if isinstance(row, dict):
		return row.get(key, default)
	getter = getattr(row, "get", None)
	if callable(getter):
		try:
			value = getter(key)
		except Exception:
			value = None
		if value is not None:
			return value
	return getattr(row, key, default)


def _iso(value: Any) -> str | None:
	if not value:
		return None
	try:
		return get_datetime(value).isoformat(sep=" ")
	except Exception:
		return str(value)


def _fmt_qty(value: Any) -> str:
	qty = flt(value)
	return str(int(qty)) if qty == int(qty) else f"{qty:g}"


def _now() -> Any:
	return now_datetime()


def _doctype_ready() -> bool:
	"""False between deploy and ``bench migrate`` (the DocType is not there yet)."""
	try:
		return bool(frappe.db.exists("DocType", DOCTYPE))
	except Exception:
		return False


def _parse_lines(lines: Any) -> list[Any]:
	if lines is None or lines == "":
		return []
	if isinstance(lines, str):
		try:
			lines = json.loads(lines)
		except Exception:
			frappe.throw(_("Invalid JSON for lines"))
	if not isinstance(lines, list):
		frappe.throw(_("lines must be a list of {item_code, qty}"))
	return lines


def _normalize_lines(lines: Any) -> dict[str, float]:
	"""``{item_code: qty}`` -- quantities rounded to 3 dp, then zero/negative lines dropped, repeats summed.

	Refuses a non-numeric or non-finite qty, a positive qty with no item, and a
	request with no positive line at all.
	"""
	merged: dict[str, float] = {}
	for ln in _parse_lines(lines):
		if not isinstance(ln, dict):
			frappe.throw(_("Each line must be an object with item_code and qty"))
		code = str(ln.get("item_code") or ln.get("item") or "").strip()
		raw = ln.get("qty")
		try:
			qty = float(raw if raw not in (None, "") else 0)
		except (TypeError, ValueError):
			frappe.throw(_("Invalid quantity for {0}").format(code or _("an item")))
		if not math.isfinite(qty):
			frappe.throw(_("Invalid quantity for {0}").format(code or _("an item")))
		qty = round(qty, QTY_DP)
		if qty <= 0:
			continue
		if not code:
			frappe.throw(_("A line with a quantity has no item"))
		merged[code] = round(merged.get(code, 0.0) + qty, QTY_DP)
	if not merged:
		frappe.throw(_("Enter a quantity above zero for at least one item."))
	return merged


# ---------------------------------------------------------------------------
# Sides (factory + branches) and the acting user
# ---------------------------------------------------------------------------


class _Sides:
	"""The factory warehouse and every branch warehouse, with their labels."""

	def __init__(self, factory: str | None, profiles: Sequence[dict[str, Any]]):
		self.factory = (factory or "").strip() or None
		self.branch_profiles: dict[str, list[str]] = {}
		self.profile_warehouse: dict[str, str] = {}
		for row in profiles or []:
			name = str(_g(row, "name") or "").strip()
			warehouse = str(_g(row, "warehouse") or "").strip()
			if not name or not warehouse:
				continue
			self.profile_warehouse[name] = warehouse
			self.branch_profiles.setdefault(warehouse, []).append(name)
		for names in self.branch_profiles.values():
			names.sort(key=lambda n: n.lower())

	def is_factory(self, warehouse: str | None) -> bool:
		return bool(warehouse) and warehouse == self.factory

	def is_branch(self, warehouse: str | None) -> bool:
		return bool(warehouse) and warehouse in self.branch_profiles

	def is_known(self, warehouse: str | None) -> bool:
		return self.is_factory(warehouse) or self.is_branch(warehouse)

	def kind(self, warehouse: str | None) -> str | None:
		if self.is_factory(warehouse):
			return KIND_FACTORY
		if self.is_branch(warehouse):
			return KIND_BRANCH
		return None

	def label(self, warehouse: str | None) -> str:
		if self.is_factory(warehouse):
			return FACTORY_LABEL
		if self.is_branch(warehouse):
			return self.branch_profiles[warehouse][0]
		return str(warehouse or "")

	def branch_warehouses(self) -> list[str]:
		return sorted(
			(w for w in self.branch_profiles if w != self.factory),
			key=lambda w: self.label(w).lower(),
		)


def _load_sides() -> _Sides:
	profiles = frappe.get_all(
		"POS Profile",
		filters={"disabled": 0},
		fields=["name", "warehouse"],
		order_by="name asc",
	)
	return _Sides(transfer_api._get_default_fg_warehouse(), profiles or [])


def _open_shift_profiles(user: str) -> set[str]:
	"""POS Profiles on which *user themself* has an open, submitted POS Opening Entry."""
	if not user or user == "Guest":
		return set()
	rows = frappe.get_all(
		"POS Opening Entry",
		filters={"user": user, "status": "Open", "docstatus": 1},
		pluck="pos_profile",
	)
	return {str(p).strip() for p in rows or [] if str(p or "").strip()}


class _Actor:
	"""Who the current user may act for, resolved once per request."""

	def __init__(
		self,
		*,
		user: str,
		roles: Iterable[str],
		sides: _Sides,
		assigned_profiles: Iterable[str],
		open_profiles: Iterable[str],
		pos_profile: str | None = None,
	):
		self.user = user
		self.roles = set(roles or [])
		self.sides = sides
		self.is_manager = bool(self.roles & set(ROLES.STOCK_TRANSFER))
		self.is_production = bool(self.roles & set(ROLES.PRODUCTION_EXECUTE))
		# One-step stock power: the same set ``api.transfer.can_post_direct`` checks,
		# read off the roles already resolved here. A line manager (STOCK_TRANSFER
		# only) approves against declared sends but cannot conjure stock directly.
		self.can_direct = bool(self.roles & set(ROLES.STOCK_TRANSFER_DIRECT))
		self.assigned_profiles = set(assigned_profiles or [])
		self.open_profiles = set(open_profiles or [])
		self.pos_profile = (pos_profile or "").strip() or None
		# warehouse -> the POS Profile the user's own open shift covers it with.
		# With ``pos_profile`` given only that profile counts; without it, every
		# profile the user has an open shift on.
		self.shift_warehouses: dict[str, str] = {}
		candidates = [self.pos_profile] if self.pos_profile else sorted(self.open_profiles)
		for profile in candidates:
			if profile in self.assigned_profiles and profile in self.open_profiles:
				warehouse = sides.profile_warehouse.get(profile)
				if warehouse:
					self.shift_warehouses.setdefault(warehouse, profile)

	def can_act_for(self, warehouse: str | None) -> bool:
		if not warehouse:
			return False
		if self.is_manager:
			return True
		if self.is_production and self.sides.is_factory(warehouse):
			return True
		return warehouse in self.shift_warehouses

	def my_scope(self) -> set[str] | None:
		"""The warehouses this user acts for; ``None`` = every side (manager)."""
		if self.is_manager:
			return None
		scope = set(self.shift_warehouses)
		if self.is_production and self.sides.factory:
			scope.add(self.sides.factory)
		return scope


def _build_actor(pos_profile: str | None = None) -> _Actor:
	user = frappe.session.user
	sides = _load_sides()
	return _Actor(
		user=user,
		roles=frappe.get_roles(user),
		sides=sides,
		assigned_profiles=get_user_pos_profiles(user),
		open_profiles=_open_shift_profiles(user),
		pos_profile=pos_profile,
	)


def _require_branch_profile(actor: _Actor, pos_profile: str, my_warehouse: str) -> None:
	"""Refuse unless *pos_profile* is the user's, has THEIR open shift and stocks *my_warehouse*."""
	profile = (pos_profile or "").strip()
	if profile not in actor.sides.profile_warehouse:
		frappe.throw(_("Unknown or disabled branch {0}.").format(profile), BranchAccessError)
	if profile not in actor.assigned_profiles:
		frappe.throw(
			_("You are not assigned to branch {0}.").format(profile),
			BranchAccessError,
			title=_("Wrong Branch"),
		)
	if profile not in actor.open_profiles:
		frappe.throw(
			_("Start your shift on {0} first: stock handovers are recorded from an open shift.").format(
				profile
			),
			ShiftRequiredError,
			title=_("Shift Required"),
		)
	if actor.sides.profile_warehouse.get(profile) != my_warehouse:
		frappe.throw(
			_("Branch {0} does not stock from warehouse {1}.").format(profile, my_warehouse),
			BranchAccessError,
			title=_("Wrong Branch"),
		)


def _refuse_side(actor: _Actor, warehouse: str, what: str) -> None:
	"""Throw the clearest reason *actor* may not act for *warehouse*."""
	label = actor.sides.label(warehouse)
	branch_profiles = set(actor.sides.branch_profiles.get(warehouse, []))
	if (
		not actor.sides.is_factory(warehouse)
		and branch_profiles
		and branch_profiles & actor.assigned_profiles
	):
		# Their own branch, just no open shift: the client routes to Start Shift.
		frappe.throw(
			_("Start your shift on {0} to {1}.").format(label, what),
			ShiftRequiredError,
			title=_("Shift Required"),
		)
	frappe.throw(_("Not permitted: only {0} can {1}.").format(label, what), frappe.PermissionError)


# ---------------------------------------------------------------------------
# Items offered (jars only in v1)
# ---------------------------------------------------------------------------


def _branch_side_for_items(my_warehouse: str, counterpart: str, sides: _Sides) -> str:
	"""Whose Warehouse Count Profile scopes the items: the branch side.

	Branch <-> branch uses the caller's own side.
	"""

	def pure_branch(w: str) -> bool:
		return sides.is_branch(w) and not sides.is_factory(w)

	if pure_branch(my_warehouse):
		return my_warehouse
	if pure_branch(counterpart):
		return counterpart
	return my_warehouse


def _items_with_bin(warehouses: Sequence[str]) -> list[str]:
	wanted = [w for w in warehouses if w]
	if not wanted:
		return []
	codes = frappe.get_all(
		"Bin",
		filters={"warehouse": ["in", wanted], "actual_qty": ["!=", 0]},
		pluck="item_code",
		limit_page_length=0,
	)
	return sorted({str(c) for c in codes or [] if c})


def _allowed_item_codes(my_warehouse: str, counterpart: str, sides: _Sides) -> set[str]:
	"""The item codes that may be handed over between the two sides.

	The enabled stock items of the branch side's Warehouse Count Profile (on
	production: the Large / Medium / Small jar groups). With no profile there,
	any enabled stock item with a non-zero bin in either warehouse.
	"""
	from jarz_pos.api.inventory_count import _resolve_count_item_codes

	branch = _branch_side_for_items(my_warehouse, counterpart, sides)
	codes = _resolve_count_item_codes(branch)
	if codes is None:
		codes = _items_with_bin([my_warehouse, counterpart])
	return {str(c) for c in codes or [] if c}


def _dispatch_item_codes(my_warehouse: str, sides: _Sides) -> set[str]:
	"""The item codes a side may declare as "sent out" (it names no destination).

	The union of every branch's Warehouse Count Profile items (the jar groups).
	With no branch profile at all, any item with a non-zero bin in the sender.
	"""
	from jarz_pos.api.inventory_count import _resolve_count_item_codes

	codes: set[str] = set()
	found = False
	for warehouse in sides.branch_warehouses():
		resolved = _resolve_count_item_codes(warehouse)
		if resolved is None:
			continue
		found = True
		codes.update(str(c) for c in resolved or [] if c)
	if not found:
		codes = set(_items_with_bin([my_warehouse]))
	return codes


def _unsourced_receipt_item_codes(my_warehouse: str, sides: _Sides) -> set[str]:
	"""The item codes a side may declare as received from an unknown sender.

	The union of what any known side may hand to *my_warehouse*, plus everything a
	side may send out (anything sent can arrive anywhere), so the list the app
	shows for "I don't know who sent it" never refuses what it offered.
	"""
	codes: set[str] = set()
	others = ([sides.factory] if sides.factory else []) + sides.branch_warehouses()
	for counterpart in dict.fromkeys(others):
		if counterpart and counterpart != my_warehouse:
			codes |= _allowed_item_codes(my_warehouse, counterpart, sides)
	return codes | _dispatch_item_codes(my_warehouse, sides)


def _item_rows(codes: Iterable[str], search: str | None = None) -> list[dict[str, Any]]:
	wanted = sorted({c for c in codes if c})
	if not wanted:
		return []
	or_filters: list[Any] = []
	text = (search or "").strip()
	if text:
		like = f"%{text}%"
		or_filters = [["Item", "name", "like", like], ["Item", "item_name", "like", like]]
	rows = frappe.get_all(
		"Item",
		filters={"name": ["in", wanted], "disabled": 0, "has_variants": 0, "is_stock_item": 1},
		or_filters=or_filters,
		fields=["name as item_code", "item_name", "item_group", "stock_uom"],
		order_by="item_group asc, item_name asc",
		limit_page_length=0,
	)
	out = [
		{
			"item_code": r.get("item_code"),
			"item_name": r.get("item_name") or r.get("item_code"),
			"item_group": r.get("item_group"),
			"stock_uom": r.get("stock_uom") or DEFAULT_UOM,
		}
		for r in rows or []
	]
	out.sort(key=lambda r: (str(r.get("item_group") or "").lower(), str(r.get("item_name") or "").lower()))
	return out


def _item_meta(codes: Iterable[str]) -> dict[str, dict[str, Any]]:
	wanted = sorted({c for c in codes if c})
	if not wanted:
		return {}
	rows = frappe.get_all(
		"Item",
		filters={"name": ["in", wanted]},
		fields=["name", "item_name", "stock_uom"],
		limit_page_length=0,
	)
	return {str(r.get("name")): r for r in rows or []}


# ---------------------------------------------------------------------------
# Serialization
# ---------------------------------------------------------------------------


def _user_names(users: Iterable[str]) -> dict[str, str]:
	wanted = sorted({u for u in users if u})
	if not wanted:
		return {}
	try:
		rows = frappe.get_all("User", filters={"name": ["in", wanted]}, fields=["name", "full_name"])
	except Exception:
		return {u: u for u in wanted}
	names = {u: u for u in wanted}
	for r in rows or []:
		names[r.get("name")] = r.get("full_name") or r.get("name")
	return names


def _kind(row: Any) -> str:
	"""``Dispatch`` or ``Handover`` (a row with no ``kind`` is a one-to-one handover)."""
	return DOC_KIND_DISPATCH if _g(row, "kind") == DOC_KIND_DISPATCH else DOC_KIND_HANDOVER


def _is_dispatch(row: Any) -> bool:
	return _kind(row) == DOC_KIND_DISPATCH


def _side_warehouse(row: Any, side: str | None) -> str | None:
	"""The warehouse of a side; ``None`` for the Manager side (a Dispatch has no To)."""
	if side == SIDE_FROM:
		return _g(row, "from_warehouse")
	if side == SIDE_TO:
		return _g(row, "to_warehouse")
	return None


def _other_side(side: str | None) -> str:
	return SIDE_TO if side == SIDE_FROM else SIDE_FROM


def _group_source(row: Any, dispatch_sources: Iterable[str]) -> str | None:
	"""The sender warehouse when *row* sits in a match group, else ``None``.

	A pending Dispatch is always in its sender's group. A pending receipt (a
	one-to-one handover declared by the receiving side) joins the group of the
	warehouse it came from only while that warehouse has a pending Dispatch.
	"""
	if _g(row, "status") != STATUS_PENDING:
		return None
	source = _g(row, "from_warehouse")
	if not source:
		return None
	if _is_dispatch(row):
		return source
	if _g(row, "declared_side") == SIDE_TO and source in set(dispatch_sources or ()):
		return source
	return None


def _is_transit_send(row: Any) -> bool:
	"""A Dispatch whose stock already sits in transit (posted at declaration)."""
	return _is_dispatch(row) and bool(_g(row, "transit_entry"))


def _is_receipt(row: Any) -> bool:
	return not _is_dispatch(row) and _g(row, "declared_side") == SIDE_TO


def _is_pool_receipt(row: Any, transit_sources: Iterable[str]) -> bool:
	"""A pending receipt a manager approves on the transit board.

	Unsourced ("I don't know who sent it"), or naming a sender that has an open
	transit send. A receipt naming a sender with no open send stays one-to-one.
	"""
	if _g(row, "status") != STATUS_PENDING or not _is_receipt(row):
		return False
	source = _g(row, "from_warehouse")
	return not source or source in set(transit_sources or ())


def _line_remaining(item: Any) -> float:
	"""A send line's ``qty - matched - written off - returned`` (never below 0)."""
	left = flt(_g(item, "qty")) - flt(_g(item, "matched_qty")) - flt(_g(item, "written_off_qty"))
	left -= flt(_g(item, "returned_qty"))
	return round(max(left, 0.0), QTY_DP)


def _transit_moved(items: Sequence[Any]) -> bool:
	"""True once any line of a send was matched, written off or returned."""
	return any(
		flt(_g(i, "matched_qty")) > _QTY_EPS
		or flt(_g(i, "written_off_qty")) > _QTY_EPS
		or flt(_g(i, "returned_qty")) > _QTY_EPS
		for i in items or []
	)


def _is_changed(row: Any, items: Sequence[Any]) -> bool:
	"""True once the other side changed the quantities (the flag, or any qty off its declaration)."""
	if cint(_g(row, "was_changed")):
		return True
	return any(abs(flt(_g(i, "qty")) - flt(_g(i, "declared_qty"))) > 1e-9 for i in items or [])


def _can_cancel(row: Any, items: Sequence[Any], actor: _Actor) -> bool:
	if _g(row, "status") != STATUS_PENDING:
		return False
	if _is_transit_send(row):
		# Its declarer or a manager, only while nothing on it has moved on.
		if _transit_moved(items):
			return False
		return actor.is_manager or _g(row, "declared_by") == actor.user
	declared_side = _g(row, "declared_side")
	if _g(row, "awaiting_side") == declared_side:
		return False
	if _is_changed(row, items):
		return False
	return actor.can_act_for(_side_warehouse(row, declared_side))


def _can_respond(row: Any, actor: _Actor, group_source: str | None = None) -> bool:
	if _g(row, "status") != STATUS_PENDING:
		return False
	if _g(row, "last_proposed_by") == actor.user:
		return False
	if _is_transit_send(row):
		# Its stock is in transit: the transit board returns or writes it off.
		return False
	if _is_dispatch(row):
		# The only answer a Dispatch takes through ``respond`` is a manager's reject.
		return actor.is_manager
	if group_source:
		return False
	if _is_receipt(row) and not _g(row, "from_warehouse"):
		# Unsourced: a manager matches it on the transit board.
		return False
	return actor.can_act_for(_side_warehouse(row, _g(row, "awaiting_side")))


def _serialize(
	row: Any,
	items: Sequence[Any],
	actor: _Actor,
	names: dict[str, str],
	dispatch_sources: Iterable[str] = (),
) -> dict[str, Any]:
	sides = actor.sides
	from_wh = _g(row, "from_warehouse") or None
	to_wh = _g(row, "to_warehouse") or None
	dispatch = _is_dispatch(row)
	group_source = _group_source(row, dispatch_sources)
	awaiting = _g(row, "awaiting_side")
	declared_by = _g(row, "declared_by")
	last_by = _g(row, "last_proposed_by")
	unsourced = _is_receipt(row) and not from_wh
	if dispatch or group_source or (unsourced and _g(row, "status") == STATUS_PENDING):
		awaiting, awaiting_label = SIDE_MANAGER, MANAGER_LABEL
	else:
		awaiting_label = sides.label(_side_warehouse(row, awaiting))
	return {
		"name": _g(row, "name"),
		"status": _g(row, "status"),
		"kind": _kind(row),
		"group_source": group_source,
		"from_warehouse": from_wh,
		"from_label": sides.label(from_wh) if from_wh else UNKNOWN_SENDER_LABEL,
		"transit_entry": _g(row, "transit_entry") or None,
		"transit_warehouse": _g(row, "transit_warehouse") or None,
		"to_warehouse": to_wh,
		"to_label": sides.label(to_wh) if to_wh else None,
		"awaiting_side": awaiting,
		"awaiting_label": awaiting_label,
		"declared_by_name": names.get(declared_by, declared_by) if declared_by else None,
		"declared_at": _iso(_g(row, "declared_at")),
		"last_proposed_by_name": names.get(last_by, last_by) if last_by else None,
		"last_proposed_at": _iso(_g(row, "last_proposed_at")),
		"changed": _is_changed(row, items),
		"can_respond": _can_respond(row, actor, group_source),
		"can_cancel": _can_cancel(row, items, actor),
		"stock_entry": _g(row, "stock_entry") or None,
		"reject_reason": _g(row, "reject_reason") or None,
		"match_note": _g(row, "match_note") or None,
		"items": [
			{
				"item_code": _g(i, "item_code"),
				"item_name": _g(i, "item_name") or _g(i, "item_code"),
				"stock_uom": _g(i, "stock_uom") or DEFAULT_UOM,
				"qty": flt(_g(i, "qty")),
				"declared_qty": flt(_g(i, "declared_qty")),
			}
			for i in items or []
		],
	}


def _serialize_doc(doc: Any, actor: _Actor, dispatch_sources: Iterable[str] | None = None) -> dict[str, Any]:
	items = list(_g(doc, "items") or [])
	names = _user_names([_g(doc, "declared_by"), _g(doc, "last_proposed_by")])
	if dispatch_sources is None:
		dispatch_sources = _pending_dispatch_sources()
	return _serialize(doc, items, actor, names, dispatch_sources)


# ---------------------------------------------------------------------------
# Match groups (a sender's Dispatches + the receipts that name it as source)
# ---------------------------------------------------------------------------

_GROUP_FIELDS = [
	"name",
	"status",
	"kind",
	"from_warehouse",
	"to_warehouse",
	"declared_side",
	"awaiting_side",
	"declared_by",
	"declared_at",
	"last_proposed_by",
	"last_proposed_at",
	"reminder_sent_at",
	"transit_entry",
]


def _pending_dispatch_sources() -> set[str]:
	"""Warehouses with at least one pending Dispatch (their receipts are in a match group).

	An unlocked read (display, reminders). A request that acts on the answer locks the
	one source it cares about with :func:`_lock_dispatches` instead; never lock this
	table-wide.
	"""
	rows = frappe.get_all(
		DOCTYPE,
		filters={"status": STATUS_PENDING, "kind": DOC_KIND_DISPATCH},
		pluck="from_warehouse",
		limit_page_length=0,
	)
	return {str(w) for w in rows or [] if w}


def _transit_sources() -> set[str]:
	"""Warehouses with an open transit send (their pending receipts are pool receipts). Unlocked."""
	rows = frappe.get_all(
		DOCTYPE,
		filters={"status": STATUS_PENDING, "kind": DOC_KIND_DISPATCH, "transit_entry": ["is", "set"]},
		pluck="from_warehouse",
		limit_page_length=0,
	)
	return {w for w in rows or [] if isinstance(w, str) and w}


def _locked_names(filters: dict[str, Any]) -> list[dict[str, Any]]:
	"""``name`` + ``declared_at`` of the matching rows, read ``FOR UPDATE`` in name order.

	``db.get_values`` because v16's ``get_all`` / ``get_list`` have no ``for_update``
	(see api/delivery_partners.py). Name order is the one global lock order.
	"""
	return (
		frappe.db.get_values(
			DOCTYPE,
			filters,
			["name", "declared_at"],
			as_dict=True,
			order_by="name asc",
			for_update=True,
		)
		or []
	)


def _lock_dispatches(source: str) -> list[str]:
	"""Lock *source*'s pending Dispatches (name order) and return their names.

	Step 2 of the lock order every request follows: (1) read the row's source without
	a lock, (2) this, (3) the receipt(s) sorted by name, (4) re-check status and
	membership. A non-empty result means *source*'s receipts are in a match group.
	"""
	if not source:
		return []
	rows = _locked_names({"status": STATUS_PENDING, "kind": DOC_KIND_DISPATCH, "from_warehouse": source})
	return [str(r.get("name")) for r in rows]


def _declared_order(rows: Sequence[dict[str, Any]]) -> list[str]:
	"""Names oldest first (declared_at, then name)."""
	ordered = sorted(rows, key=lambda r: (str(r.get("declared_at") or ""), str(r.get("name"))))
	return [str(r.get("name")) for r in ordered]


def _current_group_names(source: str, for_update: bool = False) -> tuple[list[str], list[str]]:
	"""``(dispatch names, receipt names)`` of the pending match group of *source*, oldest first.

	With *for_update* the Dispatches are locked first, then the receipts, each in name
	order (the same order :func:`_lock_dispatches` and ``respond`` use).
	"""
	dispatch_filters = {"status": STATUS_PENDING, "kind": DOC_KIND_DISPATCH, "from_warehouse": source}
	receipt_filters = {
		"status": STATUS_PENDING,
		"kind": DOC_KIND_HANDOVER,
		"declared_side": SIDE_TO,
		"from_warehouse": source,
	}
	if for_update:
		dispatches = _declared_order(_locked_names(dispatch_filters))
		if not dispatches:
			return [], []
		return dispatches, _declared_order(_locked_names(receipt_filters))
	dispatches = frappe.get_all(
		DOCTYPE,
		filters=dispatch_filters,
		fields=["name", "declared_at"],
		limit_page_length=0,
	)
	if not dispatches:
		return [], []
	receipts = frappe.get_all(
		DOCTYPE,
		filters=receipt_filters,
		fields=["name", "declared_at"],
		limit_page_length=0,
	)
	return _declared_order(dispatches), _declared_order(receipts or [])


def _load_items(names: Sequence[str]) -> dict[str, list[dict[str, Any]]]:
	"""Child rows of the given handovers, ``{parent: [row...]}`` in line order."""
	wanted = [n for n in names if n]
	out: dict[str, list[dict[str, Any]]] = {n: [] for n in wanted}
	if not wanted:
		return out
	for item in frappe.get_all(
		ITEM_DOCTYPE,
		filters={"parent": ["in", wanted], "parenttype": DOCTYPE},
		fields=["parent", "item_code", "item_name", "stock_uom", "qty", "declared_qty"],
		order_by="parent asc, idx asc",
		limit_page_length=0,
	):
		out.setdefault(item.get("parent"), []).append(item)
	return out


def _member_order(row: Any) -> tuple[str, str]:
	return (str(_iso(_g(row, "declared_at")) or ""), str(_g(row, "name") or ""))


def _match_groups(rows: Iterable[Any]) -> dict[str, dict[str, list[Any]]]:
	"""``{source: {"dispatches": [...], "receipts": [...]}}`` from pending rows, oldest first.

	Pure: a group exists for every sender with a pending Dispatch; receipts are the
	pending ones declared by the receiving side that name the sender as source.
	A sender with an open transit send has no group: its receipts are pool
	receipts, approved on the transit board.
	"""
	pending = [r for r in rows or [] if _g(r, "status") == STATUS_PENDING]
	transit = {_g(r, "from_warehouse") for r in pending if _is_transit_send(r)}
	groups: dict[str, dict[str, list[Any]]] = {}
	for row in pending:
		if _is_dispatch(row) and _g(row, "from_warehouse") and _g(row, "from_warehouse") not in transit:
			groups.setdefault(_g(row, "from_warehouse"), {"dispatches": [], "receipts": []})[
				"dispatches"
			].append(row)
	for row in pending:
		source = _g(row, "from_warehouse")
		if not _is_dispatch(row) and _g(row, "declared_side") == SIDE_TO and source in groups:
			groups[source]["receipts"].append(row)
	for group in groups.values():
		group["dispatches"].sort(key=_member_order)
		group["receipts"].sort(key=_member_order)
	return groups


def _group_summary(
	source: str,
	dispatches: Sequence[Any],
	receipts: Sequence[Any],
	items_by_parent: dict[str, Sequence[Any]],
) -> dict[str, Any]:
	"""What was sent, what each receiver claims and what is left, per item. Pure.

	``remaining`` = sent - sum(claims); a group is ``balanced`` when every item's
	remaining is 0 and something was sent.
	"""
	sent: dict[str, float] = {}
	claims: dict[str, dict[str, float]] = {}
	meta: dict[str, Any] = {}
	destinations: dict[str, list[str]] = {}
	for dispatch in dispatches:
		for item in items_by_parent.get(_g(dispatch, "name"), []) or []:
			code = _g(item, "item_code")
			sent[code] = round(sent.get(code, 0.0) + flt(_g(item, "qty")), QTY_DP)
			meta.setdefault(code, item)
	for receipt in receipts:
		warehouse = _g(receipt, "to_warehouse")
		destinations.setdefault(warehouse, []).append(_g(receipt, "name"))
		for item in items_by_parent.get(_g(receipt, "name"), []) or []:
			code = _g(item, "item_code")
			per = claims.setdefault(code, {})
			per[warehouse] = round(per.get(warehouse, 0.0) + flt(_g(item, "qty")), QTY_DP)
			meta.setdefault(code, item)
	codes = sorted(set(sent) | set(claims), key=lambda c: (str(_g(meta[c], "item_name") or c).lower(), c))
	items_out: list[dict[str, Any]] = []
	for code in codes:
		total_sent = round(sent.get(code, 0.0), QTY_DP)
		per = claims.get(code, {})
		remaining = round(total_sent - sum(per.values()), QTY_DP)
		items_out.append(
			{
				"item_code": code,
				"item_name": _g(meta[code], "item_name") or code,
				"stock_uom": _g(meta[code], "stock_uom") or DEFAULT_UOM,
				"sent": total_sent,
				"claims": [{"warehouse": w, "qty": q} for w, q in per.items()],
				"remaining": remaining,
			}
		)
	balanced = bool(items_out) and all(
		abs(i["remaining"]) <= _QTY_EPS and i["sent"] > _QTY_EPS for i in items_out
	)
	members = list(dispatches) + list(receipts)
	oldest = min((_g(m, "declared_at") for m in members if _g(m, "declared_at")), default=None)
	newest = max(members, key=_member_order) if members else None
	newest_dispatch = max(dispatches, key=_member_order) if dispatches else None
	return {
		"source_warehouse": source,
		"newest": _g(newest, "name") if newest is not None else None,
		"newest_dispatch": _g(newest_dispatch, "name") if newest_dispatch is not None else None,
		"dispatches": [_g(d, "name") for d in dispatches],
		"receipts": [_g(r, "name") for r in receipts],
		"destinations": [{"warehouse": w, "handovers": names} for w, names in destinations.items()],
		"items": items_out,
		"balanced": balanced,
		"transit": any(_is_transit_send(d) for d in dispatches),
		"oldest_at": oldest,
		"declarers": sorted({_g(m, "declared_by") for m in members if _g(m, "declared_by")}),
		"unmatched_units": round(sum(abs(i["remaining"]) for i in items_out), QTY_DP),
	}


def _serialize_group(summary: dict[str, Any], actor: _Actor, names: dict[str, str]) -> dict[str, Any]:
	sides = actor.sides
	source = summary["source_warehouse"]
	declarers = summary.get("declarers") or []
	return {
		"source_warehouse": source,
		"source_label": sides.label(source),
		"dispatches": list(summary["dispatches"]),
		"receipts": list(summary["receipts"]),
		"destinations": [
			{
				"warehouse": d["warehouse"],
				"label": sides.label(d["warehouse"]),
				"handovers": list(d["handovers"]),
			}
			for d in summary["destinations"]
		],
		"items": [
			{
				**{k: v for k, v in i.items() if k != "claims"},
				"claims": [{"warehouse": c["warehouse"], "qty": c["qty"]} for c in i["claims"]],
			}
			for i in summary["items"]
		],
		"balanced": summary["balanced"],
		"oldest_at": _iso(summary.get("oldest_at")),
		"declared_by_names": [names.get(u, u) for u in declarers],
		"can_confirm": bool(actor.is_manager and actor.user not in declarers),
	}


def _load_group(source: str) -> dict[str, Any] | None:
	"""The current summary of *source*'s match group, or ``None`` when it has no pending Dispatch."""
	dispatch_names, receipt_names = _current_group_names(source)
	if not dispatch_names:
		return None
	rows = frappe.get_all(
		DOCTYPE,
		filters={"name": ["in", dispatch_names + receipt_names]},
		fields=_GROUP_FIELDS,
		limit_page_length=0,
	)
	by_name = {r.get("name"): r for r in rows or []}
	dispatches = [by_name[n] for n in dispatch_names if n in by_name]
	receipts = [by_name[n] for n in receipt_names if n in by_name]
	items = _load_items(dispatch_names + receipt_names)
	return _group_summary(source, dispatches, receipts, items)


# ---------------------------------------------------------------------------
# Writing handovers
# ---------------------------------------------------------------------------


def _warehouse_company(*warehouses: str) -> str | None:
	for warehouse in warehouses:
		if not warehouse:
			continue
		company = frappe.db.get_value("Warehouse", warehouse, "company")
		if company:
			return company
	return None


def _item_child_rows(
	lines: dict[str, float], declared: dict[str, float] | None = None
) -> list[dict[str, Any]]:
	meta = _item_meta(lines.keys())
	missing = sorted(code for code in lines if code not in meta)
	if missing:
		frappe.throw(_("Unknown item: {0}").format(", ".join(missing)))
	rows = []
	for code, qty in lines.items():
		item = meta[code]
		rows.append(
			{
				"item_code": code,
				"item_name": item.get("item_name") or code,
				"stock_uom": item.get("stock_uom") or DEFAULT_UOM,
				"qty": qty,
				"declared_qty": qty if declared is None else flt(declared.get(code, 0)),
			}
		)
	return rows


def _save(doc: Any) -> None:
	"""Save *doc* as the service (the DocType is read-only in Desk and guards closed rows)."""
	try:
		doc.flags.from_handover_service = True
	except Exception:
		pass
	doc.save(ignore_permissions=True)


def _insert_handover(
	*,
	from_warehouse: str,
	to_warehouse: str | None,
	declared_side: str,
	source: str,
	lines: dict[str, float],
	user: str,
	pos_profile: str | None = None,
	kind: str = DOC_KIND_HANDOVER,
	client_request_id: str | None = None,
	transit_warehouse: str | None = None,
) -> Any:
	now = _now()
	dispatch = kind == DOC_KIND_DISPATCH
	payload: dict[str, Any] = {}
	if transit_warehouse:
		payload["transit_warehouse"] = transit_warehouse
	doc = frappe.get_doc(
		{
			**payload,
			"doctype": DOCTYPE,
			"kind": kind,
			"company": _warehouse_company(from_warehouse, to_warehouse),
			"from_warehouse": from_warehouse or None,
			"to_warehouse": None if dispatch else to_warehouse,
			"status": STATUS_PENDING,
			"declared_side": declared_side,
			"awaiting_side": SIDE_MANAGER if dispatch else _other_side(declared_side),
			"source": source,
			"pos_profile": pos_profile or None,
			"client_request_id": (client_request_id or None),
			"declared_by": user,
			"declared_at": now,
			"last_proposed_by": user,
			"last_proposed_at": now,
			"items": _item_child_rows(lines),
		}
	)
	doc.flags.from_handover_service = True
	doc.insert(ignore_permissions=True)
	return doc


def touches_branch(source_warehouse: str, target_warehouse: str) -> bool:
	"""True when a manager transfer must become a pending handover.

	Only when BOTH sides are known (the factory or a branch) and at least one is a
	branch: a transfer between a branch and Stores / Waste / WIP posts immediately.
	False before ``bench migrate`` has created the DocType, so the deploy window
	keeps the old immediate posting instead of failing every transfer.
	"""
	if not _doctype_ready():
		return False
	sides = _load_sides()
	if not (sides.is_known(source_warehouse) and sides.is_known(target_warehouse)):
		return False
	return sides.is_branch(source_warehouse) or sides.is_branch(target_warehouse)


def create_manager_handover(
	source_warehouse: str, target_warehouse: str, lines: Sequence[dict[str, Any]]
) -> str:
	"""The pending handover behind a manager's branch-touching ``submit_transfer``.

	Declared by the source ("sender") side, awaiting the target. ``lines`` were
	already validated by ``transfer._clean_transfer_lines``; repeats are summed.
	"""
	merged = _normalize_lines(list(lines or []))
	doc = _insert_handover(
		from_warehouse=source_warehouse,
		to_warehouse=target_warehouse,
		declared_side=SIDE_FROM,
		source=SOURCE_MANAGER,
		lines=merged,
		user=frappe.session.user,
	)
	_enqueue_push(doc.name, EVENT_DECLARED, frappe.session.user)
	return doc.name


def _shortage_message(doc: Any, sides: _Sides) -> str | None:
	"""Name every line the source warehouse cannot cover right now, or None."""
	from_wh = _g(doc, "from_warehouse")
	parts: list[str] = []
	for row in _g(doc, "items") or []:
		qty = flt(_g(row, "qty"))
		try:
			available = flt(
				frappe.db.get_value(
					"Bin", {"item_code": _g(row, "item_code"), "warehouse": from_wh}, "actual_qty"
				)
			)
		except Exception:
			continue
		if qty > available + 1e-9:
			parts.append(
				_("{0}: {1} to move, {2} available").format(
					_g(row, "item_name") or _g(row, "item_code"), _fmt_qty(qty), _fmt_qty(available)
				)
			)
	if not parts:
		return None
	return _(
		"Not enough stock in {0} to confirm this handover. {1}. It is still pending: change the "
		"quantities, or ask {0} to check its stock."
	).format(sides.label(from_wh), "; ".join(parts))


def _looks_like_shortage(exc: BaseException) -> bool:
	"""True when *exc* is ERPNext refusing a transfer for lack of stock."""
	if any(klass.__name__ == "NegativeStockError" for klass in type(exc).__mro__):
		return True
	text = str(exc).lower()
	return "insufficient" in text or "not enough" in text


def _shortage_messages(shortage_doc: Any, sides: _Sides) -> str | None:
	"""``_shortage_message`` for one doc, or for each of a list/tuple of docs (joined)."""
	if shortage_doc is None:
		return None
	docs = list(shortage_doc) if isinstance(shortage_doc, (list, tuple)) else [shortage_doc]
	parts = [m for m in (_shortage_message(d, sides) for d in docs if d is not None) if m]
	return " ".join(parts) if parts else None


def _post_or_explain(post: Any, shortage_doc: Any, sides: _Sides, subject: str) -> Any:
	"""Run ``post()`` inside a savepoint; on failure roll back and throw ONE clear message.

	A stock shortage is explained per line (item + what is available); *shortage_doc*
	may be one doc, a list of docs (one per source warehouse) or ``None``. Any other
	failure surfaces its real message. Either way nothing is recorded.
	"""
	savepoint = "jarz_stock_handover_confirm"
	try:
		frappe.db.savepoint(savepoint)
	except Exception:
		savepoint = None
	try:
		return post()
	except Exception as exc:
		if savepoint:
			try:
				frappe.db.rollback(save_point=savepoint)
			except Exception:
				pass
		_safe_log("stock_handover: confirm failed")
		message = _shortage_messages(shortage_doc, sides) if _looks_like_shortage(exc) else None
		if not message:
			detail = frappe.utils.strip_html(str(exc) or exc.__class__.__name__)[:300]
			message = _("Could not post the stock transfer for {0}: {1}. It is still pending.").format(
				subject, detail
			)
		# ERPNext already queued its own message for the same failure; one clear
		# message is what the screen should show.
		try:
			frappe.local.message_log = []
		except Exception:
			pass
		frappe.throw(message, title=_("Transfer not posted"))


def _post_stock_entry(doc: Any) -> str:
	"""Post the Material Transfer for *doc*'s current quantities, now. Returns its name."""
	lines = [
		{"item_code": _g(row, "item_code"), "qty": flt(_g(row, "qty"))}
		for row in (_g(doc, "items") or [])
		if flt(_g(row, "qty")) > 0
	]
	# No posting_date: with ``set_posting_time`` off, ERPNext stamps the entry
	# with now_datetime() itself -- the moment of confirmation, never a date the
	# client chose.
	se = transfer_api._build_material_transfer(
		_g(doc, "from_warehouse"),
		_g(doc, "to_warehouse"),
		lines,
		posting_date=None,
		remarks=_("Stock Handover {0}").format(_g(doc, "name")),
	)
	transfer_api._insert_and_submit_stock_entry(se)
	return se.name


def _confirm(doc: Any, user: str, sides: _Sides) -> None:
	"""Post the Stock Entry and close *doc* as Confirmed.

	If posting fails the handover is left exactly as it was (Pending) and the
	user gets one clear message naming the short item(s) and what is there.
	"""
	stock_entry = _post_or_explain(lambda: _post_stock_entry(doc), doc, sides, _g(doc, "name"))
	now = _now()
	doc.status = STATUS_CONFIRMED
	doc.confirmed_by = user
	doc.confirmed_at = now
	doc.stock_entry = stock_entry
	_save(doc)


def _resolve_transit_warehouse(company: str | None) -> str:
	"""The company's transit warehouse, or throw.

	The Company's ``default_in_transit_warehouse`` when set; else the single enabled,
	non-group Warehouse of that company with ``warehouse_type = "Transit"``.
	"""
	if not company:
		frappe.throw(_("Could not tell which company this warehouse belongs to."))
	configured = frappe.db.get_value("Company", company, "default_in_transit_warehouse")
	if configured:
		return str(configured)
	found = frappe.get_all(
		"Warehouse",
		filters={"company": company, "warehouse_type": "Transit", "is_group": 0, "disabled": 0},
		pluck="name",
		order_by="name asc",
		limit_page_length=0,
	)
	found = [str(w) for w in found or [] if w]
	if len(found) == 1:
		return found[0]
	if not found:
		frappe.throw(_("No transit warehouse for {0}").format(company))
	frappe.throw(
		_("{0} has several transit warehouses ({1}). Set the Company's Default In-Transit Warehouse.").format(
			company, ", ".join(found)
		)
	)
	raise AssertionError("unreachable")  # frappe.throw raises


def _post_transit_entry(doc: Any, transit_warehouse: str, sides: _Sides) -> str:
	"""Post *doc*'s lines sender -> transit now (a send). Returns the Stock Entry name.

	A shortage throws the per-line explanation and the caller's transaction is rolled
	back to before the send existed: nothing is recorded.
	"""
	lines = [
		{"item_code": _g(row, "item_code"), "qty": flt(_g(row, "qty"))}
		for row in (_g(doc, "items") or [])
		if flt(_g(row, "qty")) > 0
	]

	def post() -> str:
		se = transfer_api._build_material_transfer(
			_g(doc, "from_warehouse"),
			transit_warehouse,
			lines,
			posting_date=None,
			remarks=_("Stock sent out {0} (in transit)").format(_g(doc, "name")),
		)
		transfer_api._insert_and_submit_stock_entry(se)
		return se.name

	return _post_or_explain(post, doc, sides, _g(doc, "name"))


def _cancel_stock_entry(name: str) -> None:
	se = frappe.get_doc("Stock Entry", name)
	if cint(se.docstatus) != 1:
		return
	se.flags.ignore_permissions = True
	# Lets :func:`guard_transit_entry_cancel` through: this IS the send's own cancel path.
	se.flags.from_handover_service = True
	se.cancel()


def _live_transit_entries(names: Iterable[str]) -> set[str]:
	"""Which of these transit Stock Entries are still submitted (docstatus 1)."""
	wanted = sorted({str(n) for n in names or [] if n})
	if not wanted:
		return set()
	rows = frappe.get_all(
		"Stock Entry",
		filters={"name": ["in", wanted], "docstatus": 1},
		pluck="name",
		limit_page_length=0,
	)
	return {str(r) for r in rows or [] if r}


def _require_live_transit(send: Any) -> None:
	"""Refuse a send whose transit Stock Entry was cancelled outside the app."""
	entry = _g(send, "transit_entry")
	if entry and entry not in _live_transit_entries([entry]):
		frappe.throw(
			_(
				"{0} cannot be used: its transit Stock Entry {1} is no longer submitted (it was "
				"cancelled outside the app), so nothing of it is in transit."
			).format(_g(send, "name"), entry)
		)


def guard_transit_entry_cancel(doc: Any, method: str | None = None) -> None:
	"""``Stock Entry.before_cancel``: never cancel the transit entry of a pending send.

	Cancelling it in Desk would leave the send "in transit" with nothing behind it.
	The send's own cancel (:func:`_cancel_stock_entry`) flags the entry and passes.
	Fails open before ``bench migrate`` has added the ``transit_entry`` column.
	"""
	flags = getattr(doc, "flags", None)
	if flags is not None and getattr(flags, "from_handover_service", False):
		return
	name = _g(doc, "name")
	if not name:
		return
	try:
		send = frappe.db.get_value(
			DOCTYPE,
			{"transit_entry": name, "status": STATUS_PENDING, "kind": DOC_KIND_DISPATCH},
			"name",
		)
	except Exception:
		_safe_log("stock_handover: transit entry cancel guard lookup failed")
		return
	if send:
		frappe.throw(
			_(
				"{0} carries the stock of pending send {1} into transit. Cancel the send in the app "
				"(or settle it on the transit board) instead of cancelling this entry."
			).format(name, send),
			title=_("Stock in transit"),
		)


def _lock(name: str) -> Any:
	"""The handover, parent AND child rows read with ``FOR UPDATE``.

	A locking read is always served from the latest committed rows, so the
	status / turn checks below cannot act on a stale snapshot.
	"""
	name = (name or "").strip()
	if not name:
		frappe.throw(_("Handover name is required"))
	return frappe.get_doc(DOCTYPE, name, for_update=True)


def _lock_with_group(name: str) -> tuple[Any, bool]:
	"""Lock a handover in the one global order; return ``(doc, source has a pending Dispatch)``.

	(1) Read its source without a lock, (2) lock that source's pending Dispatches only,
	(3) lock the row itself, (4) the caller re-checks status after this returns.
	"""
	name = (name or "").strip()
	if not name:
		frappe.throw(_("Handover name is required"))
	source = frappe.db.get_value(DOCTYPE, name, "from_warehouse")
	grouped = bool(_lock_dispatches(str(source or "")))
	return _lock(name), grouped


# ---------------------------------------------------------------------------
# Public service functions (called by jarz_pos.api.stock_handover)
# ---------------------------------------------------------------------------


def get_context(pos_profile: str | None = None) -> dict[str, Any]:
	actor = _build_actor(pos_profile)
	sides = actor.sides
	my_sides: list[dict[str, Any]] = []

	profile = actor.pos_profile
	if profile:
		warehouse = sides.profile_warehouse.get(profile)
		if warehouse and (actor.is_manager or actor.shift_warehouses.get(warehouse) == profile):
			my_sides.append(
				{"warehouse": warehouse, "label": profile, "kind": KIND_BRANCH, "pos_profile": profile}
			)

	if sides.factory and (actor.is_production or actor.is_manager):
		if not any(s["warehouse"] == sides.factory for s in my_sides):
			my_sides.append(
				{
					"warehouse": sides.factory,
					"label": FACTORY_LABEL,
					"kind": KIND_FACTORY,
					"pos_profile": None,
				}
			)

	counterparts: list[dict[str, Any]] = []
	if sides.factory:
		counterparts.append({"warehouse": sides.factory, "label": FACTORY_LABEL, "kind": KIND_FACTORY})
	for warehouse in sides.branch_warehouses():
		counterparts.append({"warehouse": warehouse, "label": sides.label(warehouse), "kind": KIND_BRANCH})

	return {
		"my_sides": my_sides,
		"counterparts": counterparts,
		"can_manage": actor.is_manager,
		# Same set ``transfer.can_post_direct`` checks; the app shows the
		# "post now" switch from this flag.
		"can_post_direct": bool(set(actor.roles) & ROLES.STOCK_TRANSFER_DIRECT),
		# Managers approve receipts against transit sends on the transit board.
		"can_match_transit": actor.is_manager,
	}


def _validate_side(actor: _Actor, warehouse: str) -> str:
	warehouse = (warehouse or "").strip()
	if not warehouse:
		frappe.throw(_("my_warehouse is required"))
	if not actor.sides.is_known(warehouse):
		frappe.throw(_("{0} is neither the Factory nor a branch.").format(warehouse))
	return warehouse


def _validate_pair(actor: _Actor, my_warehouse: str, counterpart: str) -> tuple[str, str]:
	my_warehouse = (my_warehouse or "").strip()
	counterpart = (counterpart or "").strip()
	if not my_warehouse or not counterpart:
		frappe.throw(_("Both my_warehouse and counterpart_warehouse are required"))
	if my_warehouse == counterpart:
		frappe.throw(_("Pick a different side to hand stock over with."))
	for warehouse in (my_warehouse, counterpart):
		if not actor.sides.is_known(warehouse):
			frappe.throw(_("{0} is neither the Factory nor a branch.").format(warehouse))
	return my_warehouse, counterpart


def list_items(
	my_warehouse: str,
	counterpart_warehouse: str | None = None,
	search: str | None = None,
	direction: str | None = None,
) -> dict[str, Any]:
	"""Items a side may hand over.

	With no counterpart: the "sent out" set (all jar groups); with ``direction="in"``
	also everything any known side may hand to *my_warehouse* (an unsourced receipt
	accepts exactly this union, see :func:`declare`).
	"""
	actor = _build_actor()
	counterpart = (counterpart_warehouse or "").strip()
	if counterpart:
		my_warehouse, counterpart = _validate_pair(actor, my_warehouse, counterpart)
	else:
		my_warehouse = _validate_side(actor, my_warehouse)
	if not actor.can_act_for(my_warehouse):
		_refuse_side(actor, my_warehouse, _("record a stock handover"))
	if counterpart:
		codes = _allowed_item_codes(my_warehouse, counterpart, actor.sides)
	elif (direction or "").strip().lower() == "in":
		codes = _unsourced_receipt_item_codes(my_warehouse, actor.sides)
	else:
		codes = _dispatch_item_codes(my_warehouse, actor.sides)
	return {"items": _item_rows(codes, search)}


def _declaring_source(actor: _Actor, my_warehouse: str) -> tuple[str, str | None]:
	"""``(source, pos_profile)`` for a declaration made by *actor* for *my_warehouse*, or throw."""
	sides = actor.sides
	if sides.is_factory(my_warehouse) and (actor.is_production or actor.is_manager):
		return (SOURCE_FACTORY if actor.is_production else SOURCE_MANAGER), None
	if actor.pos_profile and not actor.is_manager:
		_require_branch_profile(actor, actor.pos_profile, my_warehouse)
		return SOURCE_BRANCH, actor.pos_profile
	if actor.is_manager:
		if actor.pos_profile and actor.shift_warehouses.get(my_warehouse) == actor.pos_profile:
			return SOURCE_BRANCH, actor.pos_profile
		return SOURCE_MANAGER, None
	if sides.is_factory(my_warehouse):
		frappe.throw(
			_("Not permitted: only the production team can record a handover for the Factory."),
			frappe.PermissionError,
		)
	frappe.throw(
		_("Open Stock Transfer from your branch's active shift to record a handover."),
		ShiftRequiredError,
		title=_("Shift Required"),
	)
	raise AssertionError("unreachable")  # frappe.throw raises


def declare(
	my_warehouse: str,
	counterpart_warehouse: str | None = None,
	direction: str = "",
	lines: Any = None,
	pos_profile: str | None = None,
	client_request_id: str | None = None,
) -> dict[str, Any]:
	"""Declare a movement.

	``direction`` "in" = I received from *counterpart_warehouse*: a one-to-one
	handover the counterpart confirms, or a pool receipt a manager approves on the
	transit board when the counterpart has an open transit send. A blank
	*counterpart_warehouse* = "I don't know who sent it" (an unsourced receipt,
	always a pool receipt). "out" = I sent stock out: a Dispatch with no
	destination (*counterpart_warehouse* is ignored) whose stock is moved sender ->
	transit in this same request (``transit_entry``); a shortage records nothing.

	*client_request_id* makes a repeated declare (a double tap) return the row the
	first one created.
	"""
	actor = _build_actor(pos_profile)
	sides = actor.sides

	direction = (direction or "").strip().lower()
	if direction not in ("in", "out"):
		frappe.throw(_("direction must be 'in' (I received) or 'out' (I sent out)"))

	counterpart = ""
	if direction == "in" and (counterpart_warehouse or "").strip():
		my_warehouse, counterpart = _validate_pair(actor, my_warehouse, counterpart_warehouse or "")
	else:
		my_warehouse = _validate_side(actor, my_warehouse)
	unsourced = direction == "in" and not counterpart

	source, used_profile = _declaring_source(actor, my_warehouse)

	request_id = (str(client_request_id or "").strip())[:64] or None
	if request_id:
		# Serialize this user's declares so two taps with one id cannot both insert.
		frappe.db.get_value("User", actor.user, "name", for_update=True)
		existing = frappe.get_all(
			DOCTYPE,
			filters={"client_request_id": request_id, "declared_by": actor.user},
			pluck="name",
			limit_page_length=1,
		)
		if existing:
			return {"ok": True, "handover": _serialize_doc(frappe.get_doc(DOCTYPE, existing[0]), actor)}

	merged = _normalize_lines(lines)
	if unsourced:
		allowed = _unsourced_receipt_item_codes(my_warehouse, sides)
	elif direction == "in":
		allowed = _allowed_item_codes(my_warehouse, counterpart, sides)
	else:
		allowed = _dispatch_item_codes(my_warehouse, sides)
	unknown = sorted(code for code in merged if code not in allowed)
	if unknown:
		if unsourced:
			frappe.throw(
				_("These items cannot be received at {0}: {1}").format(
					sides.label(my_warehouse), ", ".join(unknown)
				)
			)
		if direction == "in":
			frappe.throw(
				_("These items cannot be handed over between {0} and {1}: {2}").format(
					sides.label(my_warehouse), sides.label(counterpart), ", ".join(unknown)
				)
			)
		frappe.throw(
			_("These items cannot be sent out from {0}: {1}").format(
				sides.label(my_warehouse), ", ".join(unknown)
			)
		)

	if direction == "in":
		doc = _insert_handover(
			from_warehouse=counterpart or None,
			to_warehouse=my_warehouse,
			declared_side=SIDE_TO,
			source=source,
			lines=merged,
			user=actor.user,
			pos_profile=used_profile,
			client_request_id=request_id,
		)
	else:
		# Resolve the transit warehouse BEFORE inserting: no transit, no send.
		transit = _resolve_transit_warehouse(_warehouse_company(my_warehouse))
		doc = _insert_handover(
			from_warehouse=my_warehouse,
			to_warehouse=None,
			declared_side=SIDE_FROM,
			source=source,
			lines=merged,
			user=actor.user,
			pos_profile=used_profile,
			kind=DOC_KIND_DISPATCH,
			client_request_id=request_id,
			transit_warehouse=transit,
		)
		# Sender -> transit, now. A failure throws and the whole request (the
		# Dispatch insert included) is rolled back: nothing is recorded.
		doc.transit_entry = _post_transit_entry(doc, transit, sides)
		doc.transit_warehouse = transit
		_save(doc)

	dispatch_sources = _pending_dispatch_sources()
	if direction == "out":
		# Nobody answers a send one-to-one: the managers have something to match.
		_notify_transit_managers(doc.name, actor.user)
	elif unsourced or counterpart in _transit_sources():
		# A pool receipt: a manager approves it on the transit board.
		_notify_transit_managers(doc.name, actor.user)
	elif counterpart in dispatch_sources:
		# The counterpart has a (legacy) sent-out batch open: a manager matches this receipt.
		_maybe_push_balanced(counterpart, actor.user)
	else:
		_enqueue_push(doc.name, EVENT_DECLARED, actor.user)
	return {"ok": True, "handover": _serialize_doc(doc, actor, dispatch_sources)}


def _notify_transit_managers(handover_name: str, actor_user: str | None) -> bool:
	"""Tell the managers there is something to match, at most once per 5 minutes. Never raises.

	A burst of sends / receipts (a morning round) becomes one push. The debounce lives
	in the cache; when the cache fails the push goes out anyway.
	"""
	try:
		if _suppressed(f"stock_handover:{EVENT_TRANSIT}"):
			return False
		try:
			cache = frappe.cache()
			if cache.get_value(_TRANSIT_PUSH_CACHE_KEY):
				return False
			cache.set_value(_TRANSIT_PUSH_CACHE_KEY, 1, expires_in_sec=TRANSIT_PUSH_DEBOUNCE_SEC)
		except Exception:
			_safe_log("stock_handover: transit debounce failed")
		return _enqueue_push(handover_name, EVENT_TRANSIT, actor_user)
	except Exception:
		_safe_log("stock_handover: transit push failed")
		return False


def _list_fields() -> list[str]:
	return [
		"name",
		"status",
		"kind",
		"from_warehouse",
		"to_warehouse",
		"awaiting_side",
		"declared_side",
		"declared_by",
		"declared_at",
		"last_proposed_by",
		"last_proposed_at",
		"reminder_sent_at",
		"was_changed",
		"stock_entry",
		"reject_reason",
		"match_note",
		"transit_entry",
		"transit_warehouse",
		"modified",
	]


def list_handovers(
	my_warehouse: str | None = None,
	pos_profile: str | None = None,
	limit: Any = 30,
) -> dict[str, Any]:
	actor = _build_actor(pos_profile)
	limit = max(1, min(cint(limit) or 30, 200))
	empty: dict[str, Any] = {
		"waiting_for_me": [],
		"waiting_for_them": [],
		"recent": [],
		"to_match": [],
		"transit_to_match": 0,
	}

	my_warehouse = (my_warehouse or "").strip() or None
	scope: set[str] | None
	if my_warehouse:
		if not actor.can_act_for(my_warehouse):
			_refuse_side(actor, my_warehouse, _("see its stock handovers"))
		scope = {my_warehouse}
	else:
		scope = actor.my_scope()
		if scope is not None and not scope:
			return empty

	fields = _list_fields()
	or_filters: list[Any] = []
	if scope is not None:
		wanted = sorted(scope)
		or_filters = [
			[DOCTYPE, "from_warehouse", "in", wanted],
			[DOCTYPE, "to_warehouse", "in", wanted],
		]

	pending = frappe.get_all(
		DOCTYPE,
		filters={"status": STATUS_PENDING},
		or_filters=or_filters,
		fields=fields,
		order_by="last_proposed_at desc",
		limit_page_length=_PENDING_SCAN_LIMIT,
	)
	recent = frappe.get_all(
		DOCTYPE,
		filters={
			"status": ["in", list(CLOSED_STATUSES)],
			"modified": [">=", _now() - timedelta(days=RECENT_DAYS)],
		},
		or_filters=or_filters,
		fields=fields,
		order_by="modified desc",
		limit_page_length=limit,
	)
	pending = list(pending or [])
	recent = list(recent or [])

	# Managers match whole groups, whichever side of the shipment a row touches.
	group_rows: list[Any] = []
	if actor.is_manager:
		if scope is None:
			group_rows = pending
		else:
			group_rows = list(
				frappe.get_all(
					DOCTYPE,
					filters={"status": STATUS_PENDING},
					fields=fields,
					order_by="last_proposed_at desc",
					limit_page_length=_PENDING_SCAN_LIMIT,
				)
				or []
			)

	rows = pending + recent
	names = [r.get("name") for r in rows + group_rows if r.get("name")]
	items_by_parent = _load_items(list(dict.fromkeys(names)))
	user_names = _user_names(
		[r.get("declared_by") for r in rows + group_rows] + [r.get("last_proposed_by") for r in rows]
	)
	dispatch_sources = _pending_dispatch_sources()

	out: dict[str, Any] = {
		"waiting_for_me": [],
		"waiting_for_them": [],
		"recent": [],
		"to_match": [],
		"transit_to_match": 0,
	}
	for row in pending:
		if _group_source(row, dispatch_sources) or (_is_receipt(row) and not row.get("from_warehouse")):
			# Answered by a manager's match (batch or transit board), never one-to-one.
			bucket = "waiting_for_them"
		else:
			awaiting_wh = _side_warehouse(row, row.get("awaiting_side"))
			in_scope = scope is None or awaiting_wh in scope
			mine = in_scope and actor.can_act_for(awaiting_wh) and row.get("last_proposed_by") != actor.user
			bucket = "waiting_for_me" if mine else "waiting_for_them"
		if len(out[bucket]) < limit:
			out[bucket].append(
				_serialize(row, items_by_parent.get(row.get("name"), []), actor, user_names, dispatch_sources)
			)
	for row in recent:
		out["recent"].append(_serialize(row, items_by_parent.get(row.get("name"), []), actor, user_names))

	if actor.is_manager:
		summaries = [
			_group_summary(source, group["dispatches"], group["receipts"], items_by_parent)
			for source, group in _match_groups(group_rows).items()
		]
		summaries.sort(key=lambda s: str(_iso(s.get("oldest_at")) or ""))
		out["to_match"] = [_serialize_group(s, actor, user_names) for s in summaries]
		transit_sources = {
			_g(r, "from_warehouse") for r in group_rows if _is_transit_send(r) and _g(r, "from_warehouse")
		}
		out["transit_to_match"] = sum(1 for r in group_rows if _is_pool_receipt(r, transit_sources))
	return out


def respond(
	name: str,
	action: str,
	lines: Any = None,
	reason: str | None = None,
	pos_profile: str | None = None,
) -> dict[str, Any]:
	action = (action or "").strip().lower()
	if action not in ("confirm", "change", "reject"):
		frappe.throw(_("action must be confirm, change or reject"))

	actor = _build_actor(pos_profile)
	sides = actor.sides
	doc, group_locked = _lock_with_group(name)

	if doc.status != STATUS_PENDING:
		frappe.throw(_("This handover is already {0}.").format(_(doc.status)))

	if _is_dispatch(doc):
		return _respond_dispatch(doc, actor, action, reason)

	source = doc.from_warehouse
	if doc.declared_side == SIDE_TO and not source:
		frappe.throw(_("A manager matches this receipt to a send."), frappe.PermissionError)
	if doc.declared_side == SIDE_TO and group_locked:
		frappe.throw(
			_("This is part of {0}'s sent-out batch. A manager matches it.").format(sides.label(source)),
			frappe.PermissionError,
		)

	awaiting_side = doc.awaiting_side
	awaiting_wh = _side_warehouse(doc, awaiting_side)
	if not actor.can_act_for(awaiting_wh):
		_refuse_side(actor, awaiting_wh, _("answer this handover"))
	if doc.last_proposed_by == actor.user:
		frappe.throw(
			_("You made the latest proposal on this handover, so the other side has to answer it."),
			frappe.PermissionError,
		)

	if action == "confirm":
		_confirm(doc, actor.user, sides)
		_enqueue_push(doc.name, EVENT_CONFIRMED, actor.user)
	elif action == "reject":
		now = _now()
		doc.status = STATUS_REJECTED
		doc.rejected_by = actor.user
		doc.rejected_at = now
		doc.reject_reason = (str(reason or "").strip()[:1000]) or None
		_save(doc)
		_enqueue_push(doc.name, EVENT_REJECTED, actor.user)
	else:
		merged = _normalize_lines(lines)
		existing = {row.item_code: row for row in doc.items}
		other_wh = _side_warehouse(doc, _other_side(awaiting_side))
		allowed = _allowed_item_codes(awaiting_wh, other_wh, sides) | set(existing)
		unknown = sorted(code for code in merged if code not in allowed)
		if unknown:
			frappe.throw(
				_("These items cannot be handed over on this handover: {0}").format(", ".join(unknown))
			)
		current = {code: flt(row.qty) for code, row in existing.items()}
		if set(current) == set(merged) and all(abs(current[c] - merged[c]) <= 1e-9 for c in merged):
			frappe.throw(_("These are the same quantities. Confirm the handover instead."))
		declared = {code: flt(row.declared_qty) for code, row in existing.items()}
		doc.set("items", [])
		for child in _item_child_rows(merged, declared=declared):
			doc.append("items", child)
		doc.awaiting_side = _other_side(awaiting_side)
		doc.last_proposed_by = actor.user
		doc.last_proposed_at = _now()
		doc.reminder_sent_at = None
		doc.was_changed = 1
		_save(doc)
		_enqueue_push(doc.name, EVENT_CHANGED, actor.user)

	return {"ok": True, "handover": _serialize_doc(doc, actor)}


def _respond_dispatch(doc: Any, actor: _Actor, action: str, reason: str | None) -> dict[str, Any]:
	"""A Dispatch has no counterpart to answer it: only a manager may reject it."""
	if _is_transit_send(doc):
		# Its stock already sits in transit: rejecting would strand it there.
		frappe.throw(_("Use the transit board: return it to the sender or write it off."))
	if action != "reject":
		frappe.throw(_("Use the manager match."))
	if not actor.is_manager:
		frappe.throw(_("Only a manager can reject a sent-out batch."), frappe.PermissionError)
	if doc.last_proposed_by == actor.user:
		frappe.throw(
			_("You declared this batch, so cancel it instead of rejecting it."),
			frappe.PermissionError,
		)
	doc.status = STATUS_REJECTED
	doc.rejected_by = actor.user
	doc.rejected_at = _now()
	doc.reject_reason = (str(reason or "").strip()[:1000]) or None
	_save(doc)
	_enqueue_push(doc.name, EVENT_REJECTED, actor.user)
	_after_dispatch_closed(doc.from_warehouse, actor.user)
	return {"ok": True, "handover": _serialize_doc(doc, actor)}


def cancel(name: str, pos_profile: str | None = None) -> dict[str, Any]:
	actor = _build_actor(pos_profile)
	doc, group_locked = _lock_with_group(name)
	if doc.status != STATUS_PENDING:
		frappe.throw(_("This handover is already {0}.").format(_(doc.status)))
	if _is_transit_send(doc):
		return _cancel_transit_send(doc, actor)
	declared_wh = _side_warehouse(doc, doc.declared_side)
	if not actor.can_act_for(declared_wh):
		_refuse_side(actor, declared_wh, _("cancel this handover"))
	if doc.awaiting_side == doc.declared_side or _is_changed(doc, list(doc.items or [])):
		frappe.throw(
			_("The other side has already changed this handover; answer it instead of cancelling."),
		)
	source = doc.from_warehouse
	dispatch = _is_dispatch(doc)
	grouped = bool(_group_source(doc, {source} if group_locked else set()))
	doc.status = STATUS_CANCELLED
	doc.cancelled_by = actor.user
	doc.cancelled_at = _now()
	_save(doc)
	if dispatch:
		_after_dispatch_closed(source, actor.user)
	elif grouped:
		_maybe_push_balanced(source, actor.user)
	elif source:
		_enqueue_push(doc.name, EVENT_CANCELLED, actor.user)
	# An unsourced receipt had nobody waiting on it: no push.
	return {"ok": True, "handover": _serialize_doc(doc, actor)}


def _cancel_transit_send(doc: Any, actor: _Actor) -> dict[str, Any]:
	"""Cancel a send (its declarer or a manager) while nothing on it has moved on.

	The sender -> transit Stock Entry is cancelled (docstatus 2) in the same request;
	if ERPNext refuses, nothing changes.
	"""
	if not (actor.is_manager or doc.declared_by == actor.user):
		frappe.throw(
			_("Only whoever declared this send, or a manager, can cancel it."), frappe.PermissionError
		)
	if _transit_moved(list(doc.items or [])):
		frappe.throw(
			_("Part of this send was already matched, returned or written off, so it cannot be cancelled.")
		)
	sides = actor.sides
	entry = doc.transit_entry
	_post_or_explain(lambda: _cancel_stock_entry(entry), None, sides, doc.name)
	doc.status = STATUS_CANCELLED
	doc.cancelled_by = actor.user
	doc.cancelled_at = _now()
	_save(doc)
	_after_dispatch_closed(doc.from_warehouse, actor.user)
	return {"ok": True, "handover": _serialize_doc(doc, actor)}


def _maybe_push_balanced(source: str, actor_user: str | None) -> bool:
	"""Tell the managers when *source*'s batch now adds up. Never raises.

	No state is stored: every action that leaves the group balanced is one that made
	it so (adding to, or cancelling from, a balanced group always unbalances it).
	"""
	try:
		group = _load_group(source)
		if not group or not group.get("balanced") or group.get("transit"):
			# A transit sender's receipts are approved on the transit board instead.
			return False
		return _enqueue_push(group["newest"], EVENT_BALANCED, actor_user)
	except Exception:
		_safe_log("stock_handover: balanced check failed")
		return False


def _after_dispatch_closed(source: str, actor_user: str | None) -> None:
	"""A Dispatch was cancelled or rejected: re-announce what that changed. Never raises.

	With another Dispatch still open the group may now add up (managers hear it).
	With none left, the receipts are one-to-one again and *source* is asked to confirm them.
	"""
	try:
		if source in _pending_dispatch_sources():
			_maybe_push_balanced(source, actor_user)
			return
		for name in frappe.get_all(
			DOCTYPE,
			filters={
				"status": STATUS_PENDING,
				"kind": DOC_KIND_HANDOVER,
				"declared_side": SIDE_TO,
				"from_warehouse": source,
			},
			pluck="name",
			order_by="declared_at asc, name asc",
			limit_page_length=50,
		):
			_enqueue_push(name, EVENT_DECLARED, actor_user)
	except Exception:
		_safe_log("stock_handover: dispatch fallback failed")


# ---------------------------------------------------------------------------
# Manager match
# ---------------------------------------------------------------------------


def _parse_members(members: Any) -> list[str]:
	if isinstance(members, str):
		try:
			members = json.loads(members)
		except Exception:
			frappe.throw(_("Invalid JSON for members"))
	if not isinstance(members, list):
		frappe.throw(_("members must be a list of handover names"))
	names = list(dict.fromkeys(str(m).strip() for m in members if str(m or "").strip()))
	if not names:
		frappe.throw(_("members is required"))
	return names


def _match_qty(raw: Any, label: str) -> float:
	try:
		qty = float(raw if raw not in (None, "") else 0)
	except (TypeError, ValueError):
		frappe.throw(_("Invalid quantity for {0}").format(label))
	if not math.isfinite(qty) or qty < 0:
		frappe.throw(_("Invalid quantity for {0}").format(label))
	return round(qty, QTY_DP)


def _normalize_match_lines(lines: Any) -> dict[str, dict[str, Any]]:
	"""``{item_code: {"sent": q, "splits": {warehouse: q}}}`` -- finite, >= 0, 3 dp, repeats summed."""
	out: dict[str, dict[str, Any]] = {}
	for ln in _parse_lines(lines):
		if not isinstance(ln, dict):
			frappe.throw(_("Each line must be an object with item_code, sent and splits"))
		code = str(ln.get("item_code") or "").strip()
		if not code:
			frappe.throw(_("A line has no item"))
		entry = out.setdefault(code, {"sent": 0.0, "splits": {}})
		entry["sent"] = round(entry["sent"] + _match_qty(ln.get("sent"), code), QTY_DP)
		splits = ln.get("splits") or []
		if not isinstance(splits, list):
			frappe.throw(_("splits must be a list of {warehouse, qty}"))
		for split in splits:
			if not isinstance(split, dict):
				frappe.throw(_("Each split must be an object with warehouse and qty"))
			warehouse = str(split.get("warehouse") or "").strip()
			if not warehouse:
				frappe.throw(_("A split has no warehouse"))
			qty = _match_qty(split.get("qty"), code)
			entry["splits"][warehouse] = round(entry["splits"].get(warehouse, 0.0) + qty, QTY_DP)
	if not out:
		frappe.throw(_("Enter a quantity above zero for at least one item."))
	return out


def _assign_splits(receipts: Sequence[dict[str, float]], splits: dict[str, float]) -> list[dict[str, float]]:
	"""Spread one destination's final quantities over its receipts. Pure.

	*receipts* are the declared ``{item: qty}`` of each receipt, oldest first. Per
	item the oldest is filled up to what it declared, and the newest takes the
	remainder (so more than was claimed lands on the newest). A receipt that ends
	with nothing is the caller's to reject.
	"""
	assigned: list[dict[str, float]] = [{} for _r in receipts]
	if not receipts:
		return assigned
	last = len(receipts) - 1
	for code, total in splits.items():
		remaining = round(max(flt(total), 0.0), QTY_DP)
		for index, claimed in enumerate(receipts):
			take = remaining if index == last else min(remaining, round(flt(claimed.get(code, 0)), QTY_DP))
			take = round(max(take, 0.0), QTY_DP)
			assigned[index][code] = take
			remaining = round(remaining - take, QTY_DP)
	return assigned


def _set_item_qtys(doc: Any, qtys: dict[str, float]) -> None:
	"""Set each line of *doc* to ``qtys[item]`` (0 when absent); append lines for new items."""
	seen: set[str] = set()
	for row in doc.items:
		code = row.item_code
		seen.add(code)
		row.qty = flt(qtys.get(code, 0))
	fresh = {code: qty for code, qty in qtys.items() if code not in seen and qty > 0}
	if fresh:
		for child in _item_child_rows(fresh, declared={code: 0.0 for code in fresh}):
			doc.append("items", child)


def _post_match_stock_entry(
	source: str,
	by_destination: dict[str, dict[str, float]],
	meta: dict[str, Any],
	remarks: str,
) -> str:
	"""ONE Material Transfer ``source -> each destination``, one row per (item, destination)."""
	destinations = list(by_destination)
	first = destinations[0]
	se = transfer_api._build_material_transfer(
		source,
		first,
		[{"item_code": code, "qty": qty} for code, qty in by_destination[first].items()],
		posting_date=None,
		remarks=remarks,
	)
	for destination in destinations[1:]:
		for code, qty in by_destination[destination].items():
			se.append(
				"items",
				{
					"item_code": code,
					"uom": (_g(meta.get(code), "stock_uom") or DEFAULT_UOM),
					"qty": qty,
					"s_warehouse": source,
					"t_warehouse": destination,
				},
			)
	transfer_api._insert_and_submit_stock_entry(se)
	return se.name


def _match_note(
	sides: _Sides,
	item_names: dict[str, str],
	declared_sent: dict[str, float],
	final_sent: dict[str, float],
	claims: dict[str, dict[str, float]],
	final: dict[str, dict[str, float]],
	added: Sequence[str],
	rejected: Sequence[str],
	several_dispatches: bool,
	note: str | None,
) -> str:
	"""A short text of what the manager changed against the declarations."""

	def label(code: str) -> str:
		return item_names.get(code) or code

	parts: list[str] = []
	for code in sorted(final_sent, key=lambda c: label(c).lower()):
		if abs(final_sent[code] - declared_sent.get(code, 0.0)) > _QTY_EPS:
			parts.append(
				f"{label(code)}: sent {_fmt_qty(declared_sent.get(code, 0.0))} -> {_fmt_qty(final_sent[code])}"
			)
	claimed_by = {w for per in claims.values() for w in per}
	for warehouse in [w for w in final if w in claimed_by] + [w for w in claimed_by if w not in final]:
		for code in sorted(final_sent, key=lambda c: label(c).lower()):
			was = claims.get(code, {}).get(warehouse, 0.0)
			now = final.get(warehouse, {}).get(code, 0.0)
			if abs(now - was) > _QTY_EPS:
				parts.append(f"{sides.label(warehouse)}: {label(code)} {_fmt_qty(was)} -> {_fmt_qty(now)}")
	for warehouse in added:
		detail = ", ".join(
			f"{label(code)} {_fmt_qty(qty)}" for code, qty in final.get(warehouse, {}).items() if qty > 0
		)
		parts.append(f"Added {sides.label(warehouse)}: {detail}")
	for warehouse in rejected:
		parts.append(f"{sides.label(warehouse)} was not in the shipment")
	text = "; ".join(parts) if parts else "Matched as declared."
	if several_dispatches:
		lines = []
		for code in sorted(final_sent, key=lambda c: label(c).lower()):
			split = ", ".join(
				f"{sides.label(w)} {_fmt_qty(per[code])}"
				for w, per in final.items()
				if per.get(code, 0.0) > _QTY_EPS
			)
			lines.append(f"{label(code)} {_fmt_qty(final_sent[code])} ({split})")
		text += " Final: " + "; ".join(lines) + "."
	extra = str(note or "").strip()
	if extra:
		text += f" Note: {extra}"
	return text[:2000]


def confirm_match(
	source_warehouse: str,
	members: Any,
	lines: Any,
	note: str | None = None,
	pos_profile: str | None = None,
) -> dict[str, Any]:
	"""A manager approves a sender's whole batch: ONE Material Transfer splitting it.

	*members* is exactly the list of Dispatch + receipt names the manager was shown.
	Nothing is posted unless, per item, ``sent`` equals the sum of its splits; the
	manager may edit ``sent``, any receiver's quantity, or add a destination.
	"""
	actor = _build_actor(pos_profile)
	sides = actor.sides
	if not actor.is_manager:
		frappe.throw(_("Only a manager can match a sent-out batch."), frappe.PermissionError)

	source = (source_warehouse or "").strip()
	if not sides.is_known(source):
		frappe.throw(_("{0} is neither the Factory nor a branch.").format(source or _("A sender")))
	member_names = _parse_members(members)
	match_lines = _normalize_match_lines(lines)
	for ln in match_lines.values():
		for warehouse in ln["splits"]:
			if not sides.is_known(warehouse):
				frappe.throw(_("{0} is neither the Factory nor a branch.").format(warehouse))
			if warehouse == source:
				frappe.throw(_("A batch cannot be sent back to its sender."))

	# 1. Lock the source's Dispatches, then its receipts (name order), check nobody
	# added to / removed from the group meanwhile, then load every member.
	dispatch_names, receipt_names = _current_group_names(source, for_update=True)
	if not dispatch_names:
		frappe.throw(_(_DRIFT_MESSAGE))
	docs: dict[str, Any] = {}
	for name in sorted(dispatch_names) + sorted(receipt_names):
		try:
			docs[name] = _lock(name)
		except frappe.DoesNotExistError:
			frappe.throw(_(_DRIFT_MESSAGE))
	# A transit send's stock already left; this per-source batch would post it twice.
	if any(_is_transit_send(docs[n]) for n in dispatch_names):
		frappe.throw(_(_TRANSIT_UPDATE_APP))
	if set(dispatch_names) | set(receipt_names) != set(member_names):
		frappe.throw(_(_DRIFT_MESSAGE))
	for doc in docs.values():
		if doc.status != STATUS_PENDING or doc.from_warehouse != source:
			frappe.throw(_(_DRIFT_MESSAGE))
	dispatches = [docs[n] for n in dispatch_names]
	receipts = [docs[n] for n in receipt_names]

	# 2. Nobody approves their own declaration.
	for doc in dispatches + receipts:
		if actor.user in (doc.declared_by, doc.last_proposed_by):
			frappe.throw(
				_("You declared part of this batch, so another manager has to approve it."),
				frappe.PermissionError,
			)

	# 3. Every unit sent must go somewhere.
	declared_sent: dict[str, float] = {}
	claims: dict[str, dict[str, float]] = {}
	names_by_code: dict[str, str] = {}
	for doc in dispatches:
		for row in doc.items:
			declared_sent[row.item_code] = round(declared_sent.get(row.item_code, 0.0) + flt(row.qty), QTY_DP)
			names_by_code.setdefault(row.item_code, row.item_name or row.item_code)
	for doc in receipts:
		for row in doc.items:
			per = claims.setdefault(row.item_code, {})
			per[doc.to_warehouse] = round(per.get(doc.to_warehouse, 0.0) + flt(row.qty), QTY_DP)
			names_by_code.setdefault(row.item_code, row.item_name or row.item_code)
	group_codes = set(declared_sent) | set(claims)
	if set(match_lines) != group_codes:
		missing = sorted(group_codes - set(match_lines))
		extra = sorted(set(match_lines) - group_codes)
		if missing:
			frappe.throw(
				_("Every item in the batch needs a line (set it to 0 to drop it): {0}").format(
					", ".join(names_by_code.get(c, c) for c in missing)
				)
			)
		frappe.throw(_("These items are not part of this batch: {0}").format(", ".join(extra)))

	final_sent: dict[str, float] = {}
	final: dict[str, dict[str, float]] = {}
	for code, ln in match_lines.items():
		sent = round(ln["sent"], QTY_DP)
		split_total = round(sum(ln["splits"].values()), QTY_DP)
		if abs(sent - split_total) > _QTY_EPS:
			frappe.throw(
				_("{0}: sent {1}, split {2}. Every unit must go somewhere.").format(
					names_by_code.get(code, code), _fmt_qty(sent), _fmt_qty(split_total)
				)
			)
		final_sent[code] = sent
		for warehouse, qty in ln["splits"].items():
			final.setdefault(warehouse, {})[code] = qty
	if not any(qty > _QTY_EPS for qty in final_sent.values()):
		frappe.throw(_("Enter a quantity above zero for at least one item."))

	# 4. ONE Material Transfer, a row per (item, destination), posted now.
	receipt_warehouses = list(dict.fromkeys(doc.to_warehouse for doc in receipts))
	added = [w for w in final if w not in receipt_warehouses and sum(final[w].values()) > _QTY_EPS]
	by_destination: dict[str, dict[str, float]] = {}
	for warehouse in receipt_warehouses + added:
		rows = {code: qty for code, qty in final.get(warehouse, {}).items() if qty > _QTY_EPS}
		if rows:
			by_destination[warehouse] = rows
	meta = _item_meta(group_codes)
	for code in group_codes:
		if code in meta:
			names_by_code[code] = _g(meta[code], "item_name") or names_by_code.get(code, code)
	shortage_doc = {
		"from_warehouse": source,
		"items": [
			{"item_code": c, "item_name": names_by_code.get(c, c), "qty": q}
			for c, q in final_sent.items()
			if q > _QTY_EPS
		],
	}
	remarks = _("Stock Handover batch {0}: {1}").format(
		sides.label(source), ", ".join(d.name for d in dispatches + receipts)
	)
	stock_entry = _post_or_explain(
		lambda: _post_match_stock_entry(source, by_destination, meta, remarks),
		shortage_doc,
		sides,
		sides.label(source),
	)

	# 5. Close the members.
	now = _now()
	rejected_warehouses: list[str] = []
	assigned_by_receipt: dict[str, dict[str, float]] = {}
	for warehouse in receipt_warehouses:
		group = [d for d in receipts if d.to_warehouse == warehouse]
		assigned = _assign_splits(
			[{r.item_code: flt(r.qty) for r in d.items} for d in group],
			{code: final.get(warehouse, {}).get(code, 0.0) for code in group_codes},
		)
		for doc, qtys in zip(group, assigned, strict=True):
			assigned_by_receipt[doc.name] = qtys
		if all(sum(q.values()) <= _QTY_EPS for q in assigned):
			rejected_warehouses.append(warehouse)

	note_text = _match_note(
		sides,
		names_by_code,
		declared_sent,
		final_sent,
		claims,
		final,
		added,
		rejected_warehouses,
		len(dispatches) > 1,
		note,
	)
	touched: list[str] = []
	for doc in dispatches:
		if len(dispatches) == 1:
			_set_item_qtys(doc, final_sent)
		_close_member(doc, STATUS_CONFIRMED, actor.user, now, stock_entry, note_text)
		touched.append(doc.name)
	for doc in receipts:
		qtys = assigned_by_receipt[doc.name]
		_set_item_qtys(doc, qtys)
		if sum(qtys.values()) <= _QTY_EPS:
			doc.reject_reason = _REJECT_NOT_IN_BATCH.format(sides.label(source))
			_close_member(doc, STATUS_REJECTED, actor.user, now, None, note_text)
		else:
			_close_member(doc, STATUS_CONFIRMED, actor.user, now, stock_entry, note_text)
		touched.append(doc.name)

	# 6. A destination the manager added gets a record of its own.
	for warehouse in added:
		record = frappe.get_doc(
			{
				"doctype": DOCTYPE,
				"kind": DOC_KIND_HANDOVER,
				"company": _warehouse_company(source, warehouse),
				"from_warehouse": source,
				"to_warehouse": warehouse,
				"status": STATUS_CONFIRMED,
				"declared_side": SIDE_FROM,
				"awaiting_side": SIDE_TO,
				"source": SOURCE_MANAGER,
				"declared_by": actor.user,
				"declared_at": now,
				"last_proposed_by": actor.user,
				"last_proposed_at": now,
				"confirmed_by": actor.user,
				"confirmed_at": now,
				"stock_entry": stock_entry,
				"match_note": note_text,
				"items": _item_child_rows(
					{code: qty for code, qty in final[warehouse].items() if qty > _QTY_EPS}
				),
			}
		)
		record.flags.from_handover_service = True
		record.insert(ignore_permissions=True)
		touched.append(record.name)

	# 7. Best effort: tell each declarer once.
	notified: set[str] = set()
	for doc in dispatches + receipts:
		declarer = doc.declared_by
		if declarer and declarer != actor.user and declarer not in notified:
			notified.add(declarer)
			_enqueue_push(doc.name, EVENT_BATCH_CONFIRMED, actor.user)
	return {"ok": True, "stock_entry": stock_entry, "handovers": touched}


def _close_member(doc: Any, status: str, user: str, now: Any, stock_entry: str | None, note: str) -> None:
	doc.status = status
	if status == STATUS_CONFIRMED:
		doc.confirmed_by = user
		doc.confirmed_at = now
		doc.stock_entry = stock_entry
	else:
		doc.rejected_by = user
		doc.rejected_at = now
	doc.match_note = note
	_save(doc)


# ---------------------------------------------------------------------------
# Transit board (sends in transit + pool receipts, matched by a manager)
# ---------------------------------------------------------------------------

_TRANSIT_SEND_FIELDS = [
	"name",
	"status",
	"kind",
	"from_warehouse",
	"declared_by",
	"declared_at",
	"transit_entry",
	"transit_warehouse",
	"modified",
]
_TRANSIT_RECEIPT_FIELDS = [
	"name",
	"status",
	"kind",
	"from_warehouse",
	"to_warehouse",
	"declared_side",
	"declared_by",
	"declared_at",
	"modified",
]


def _require_transit_manager(actor: _Actor) -> None:
	if not actor.is_manager:
		frappe.throw(_("Only a manager can match stock in transit."), frappe.PermissionError)


def _load_transit_pool() -> tuple[list[Any], list[Any]]:
	"""``(open sends, pool receipts)``, each oldest first. Unlocked reads.

	A send whose transit Stock Entry is no longer submitted (cancelled outside the
	app) is left out: nothing of it is in transit. Its sender still counts as a
	transit source for receipt membership, the same as :func:`_transit_sources`.
	"""
	sends = frappe.get_all(
		DOCTYPE,
		filters={"status": STATUS_PENDING, "kind": DOC_KIND_DISPATCH, "transit_entry": ["is", "set"]},
		fields=_TRANSIT_SEND_FIELDS,
		order_by="declared_at asc, name asc",
		limit_page_length=_PENDING_SCAN_LIMIT,
	)
	sends = [s for s in sends or [] if _is_transit_send(s)]
	transit_sources = {_g(s, "from_warehouse") for s in sends if _g(s, "from_warehouse")}
	live = _live_transit_entries(_g(s, "transit_entry") for s in sends)
	sends = [s for s in sends if _g(s, "transit_entry") in live]
	receipts = frappe.get_all(
		DOCTYPE,
		filters={"status": STATUS_PENDING, "kind": DOC_KIND_HANDOVER, "declared_side": SIDE_TO},
		fields=_TRANSIT_RECEIPT_FIELDS,
		order_by="declared_at asc, name asc",
		limit_page_length=_PENDING_SCAN_LIMIT,
	)
	receipts = [r for r in receipts or [] if _is_pool_receipt(r, transit_sources)]
	return list(sends), receipts


def _version_of(rows: Iterable[Any]) -> str:
	"""sha1 of the sorted ``name|modified`` of every board member."""
	parts = sorted(f"{_g(r, 'name')}|{_iso(_g(r, 'modified')) or ''}" for r in rows or [])
	return hashlib.sha1("\n".join(parts).encode("utf-8")).hexdigest()


def _current_board_version() -> str:
	sends, receipts = _load_transit_pool()
	return _version_of(list(sends) + list(receipts))


def _load_transit_items(names: Sequence[str]) -> dict[str, list[dict[str, Any]]]:
	"""Child rows with the transit counters, ``{parent: [row...]}`` in line order."""
	wanted = [n for n in names if n]
	out: dict[str, list[dict[str, Any]]] = {n: [] for n in wanted}
	if not wanted:
		return out
	for item in frappe.get_all(
		ITEM_DOCTYPE,
		filters={"parent": ["in", wanted], "parenttype": DOCTYPE},
		fields=[
			"parent",
			"item_code",
			"item_name",
			"stock_uom",
			"qty",
			"declared_qty",
			"matched_qty",
			"written_off_qty",
			"returned_qty",
		],
		order_by="parent asc, idx asc",
		limit_page_length=0,
	):
		out.setdefault(item.get("parent"), []).append(item)
	return out


def _transit_affinity(targets: Iterable[str], sides: _Sides) -> dict[tuple[str, str], float]:
	"""``{(source, target): share}`` of each target's inbound transfers, last 60 days. Never raises.

	Direct Material Transfers between known sides, plus approved transit matches
	(those post transit -> receiver, so the Stock Entry alone does not show the sender).
	"""
	wanted = sorted({t for t in targets or [] if t})
	known = sorted({w for w in ([sides.factory] if sides.factory else []) + list(sides.branch_profiles) if w})
	if not wanted or not known:
		return {}
	try:
		since = (_now() - timedelta(days=AFFINITY_DAYS)).date()
		rows = frappe.db.sql(
			"""
			select sed.s_warehouse, sed.t_warehouse, sum(sed.transfer_qty)
			from `tabStock Entry Detail` sed
			inner join `tabStock Entry` se on se.name = sed.parent
			where se.docstatus = 1
				and se.purpose = 'Material Transfer'
				and se.posting_date >= %(since)s
				and sed.s_warehouse in %(known)s
				and sed.t_warehouse in %(targets)s
			group by sed.s_warehouse, sed.t_warehouse
			""",
			{"since": since, "known": tuple(known), "targets": tuple(wanted)},
		)
		totals: list[tuple[str, str, float]] = [(r[0], r[1], flt(r[2])) for r in rows or []]
		matches = frappe.get_all(
			TRANSIT_MATCH_DOCTYPE,
			filters={
				"approved_at": [">=", since],
				"dispatch": ["is", "set"],
				"target_warehouse": ["in", wanted],
			},
			fields=["source_warehouse", "target_warehouse", "qty"],
			limit_page_length=0,
		)
		totals += [
			(m.get("source_warehouse"), m.get("target_warehouse"), flt(m.get("qty"))) for m in matches or []
		]
		return transit_match.affinity_from_totals(totals)
	except Exception:
		_safe_log("stock_handover: transit affinity failed")
		return {}


def get_transit_board(pos_profile: str | None = None) -> dict[str, Any]:
	"""Every open send and every pool receipt, with the matcher's suggestions. Managers only."""
	actor = _build_actor(pos_profile)
	sides = actor.sides
	_require_transit_manager(actor)

	sends, receipts = _load_transit_pool()
	names = [_g(r, "name") for r in list(sends) + list(receipts)]
	items = _load_transit_items(names)
	users = _user_names([_g(r, "declared_by") for r in list(sends) + list(receipts)])
	now = _now()

	sends_out: list[dict[str, Any]] = []
	engine_sends: list[dict[str, Any]] = []
	units = 0.0
	overdue_count = 0
	for send in sends:
		name = _g(send, "name")
		declared_by = _g(send, "declared_by")
		declared_at = _g(send, "declared_at")
		lines: list[dict[str, Any]] = []
		open_lines: dict[str, float] = {}
		for item in items.get(name, []):
			code = _g(item, "item_code")
			remaining = _line_remaining(item)
			lines.append(
				{
					"item_code": code,
					"item_name": _g(item, "item_name") or code,
					"stock_uom": _g(item, "stock_uom") or DEFAULT_UOM,
					"qty": flt(_g(item, "qty")),
					"matched_qty": flt(_g(item, "matched_qty")),
					"written_off_qty": flt(_g(item, "written_off_qty")),
					"returned_qty": flt(_g(item, "returned_qty")),
					"remaining": remaining,
				}
			)
			if remaining > _QTY_EPS:
				open_lines[code] = round(open_lines.get(code, 0.0) + remaining, QTY_DP)
		units += sum(open_lines.values())
		age_hours = 0.0
		if declared_at:
			try:
				age_hours = round(max((now - get_datetime(declared_at)).total_seconds(), 0.0) / 3600.0, 1)
			except Exception:
				age_hours = 0.0
		overdue = age_hours > TRANSIT_OVERDUE_HOURS
		overdue_count += 1 if overdue else 0
		source = _g(send, "from_warehouse")
		sends_out.append(
			{
				"name": name,
				"source_warehouse": source,
				"source_label": sides.label(source),
				"declared_by": declared_by,
				"declared_by_name": users.get(declared_by, declared_by) if declared_by else None,
				"declared_at": _iso(declared_at),
				"age_hours": age_hours,
				"transit_entry": _g(send, "transit_entry"),
				"overdue": overdue,
				"can_act": bool(actor.is_manager and declared_by != actor.user),
				"lines": lines,
			}
		)
		engine_sends.append({"name": name, "source": source, "declared_at": declared_at, "lines": open_lines})

	engine_receipts: list[dict[str, Any]] = []
	for receipt in receipts:
		lines_in: dict[str, float] = {}
		for item in items.get(_g(receipt, "name"), []):
			code = _g(item, "item_code")
			lines_in[code] = round(lines_in.get(code, 0.0) + flt(_g(item, "qty")), QTY_DP)
		engine_receipts.append(
			{
				"name": _g(receipt, "name"),
				"target": _g(receipt, "to_warehouse"),
				"declared_at": _g(receipt, "declared_at"),
				"named_source": _g(receipt, "from_warehouse") or None,
				"lines": lines_in,
			}
		)
	affinity = _transit_affinity([_g(r, "to_warehouse") for r in receipts], sides)
	suggested = transit_match.recommend(engine_sends, engine_receipts, affinity, default_source=sides.factory)

	receipts_out: list[dict[str, Any]] = []
	for receipt in receipts:
		name = _g(receipt, "name")
		meta = {_g(i, "item_code"): i for i in items.get(name, [])}
		target = _g(receipt, "to_warehouse")
		named = _g(receipt, "from_warehouse") or None
		declared_by = _g(receipt, "declared_by")
		lines_out = []
		for line in suggested.get(name, []):
			code = line["item_code"]
			item = meta.get(code)
			lines_out.append(
				{
					"item_code": code,
					"item_name": _g(item, "item_name") or code,
					"stock_uom": _g(item, "stock_uom") or DEFAULT_UOM,
					"qty": line["qty"],
					"suggestions": [
						{
							"dispatch": s.get("dispatch"),
							"source_warehouse": s.get("source_warehouse"),
							"source_label": sides.label(s.get("source_warehouse")),
							"qty": s.get("qty"),
							"confidence": s.get("confidence"),
							"reasons": list(s.get("reasons") or []),
						}
						for s in line["suggestions"]
					],
					"unresolved_qty": line["unresolved_qty"],
				}
			)
		receipts_out.append(
			{
				"name": name,
				"target_warehouse": target,
				"target_label": sides.label(target),
				"named_source": named,
				"named_source_label": sides.label(named) if named else None,
				"declared_by": declared_by,
				"declared_by_name": users.get(declared_by, declared_by) if declared_by else None,
				"declared_at": _iso(_g(receipt, "declared_at")),
				"can_approve": bool(actor.is_manager and declared_by != actor.user),
				"lines": lines_out,
			}
		)

	senders = []
	if sides.factory:
		senders.append({"warehouse": sides.factory, "label": FACTORY_LABEL})
	for warehouse in sides.branch_warehouses():
		senders.append({"warehouse": warehouse, "label": sides.label(warehouse)})

	return {
		"version": _version_of(list(sends) + list(receipts)),
		"summary": {
			"to_approve": len(receipts_out),
			"units_in_transit": round(units, QTY_DP),
			"overdue_sends": overdue_count,
		},
		"sends": sends_out,
		"receipts": receipts_out,
		"senders": senders,
		# A direct ("sender never declared it") allocation and a received correction
		# above the declared qty need ROLES.STOCK_TRANSFER_DIRECT.
		"can_direct": actor.can_direct,
	}


def _parse_json(value: Any, label: str, kind: type) -> Any:
	if isinstance(value, str):
		if not value.strip():
			return None
		try:
			value = json.loads(value)
		except Exception:
			frappe.throw(_("Invalid JSON for {0}").format(label))
	if value is not None and not isinstance(value, kind):
		frappe.throw(_("{0} has the wrong shape").format(label))
	return value


def _normalize_allocations(allocations: Any) -> list[dict[str, Any]]:
	"""Finite, > 0, 3 dp allocations; a zero line is dropped."""
	out: list[dict[str, Any]] = []
	for alloc in _parse_json(allocations, "allocations", list) or []:
		if not isinstance(alloc, dict):
			frappe.throw(_("Each allocation must be an object with item_code, dispatch, source_warehouse and qty"))
		code = str(alloc.get("item_code") or "").strip()
		if not code:
			frappe.throw(_("An allocation has no item"))
		qty = _match_qty(alloc.get("qty"), code)
		if qty <= _QTY_EPS:
			continue
		dispatch = str(alloc.get("dispatch") or "").strip() or None
		source = str(alloc.get("source_warehouse") or "").strip() or None
		if not dispatch and not source:
			frappe.throw(_("An allocation of {0} names no send and no sender.").format(code))
		confidence = str(alloc.get("confidence") or "").strip().lower()
		out.append(
			{
				"item_code": code,
				"dispatch": dispatch,
				"source_warehouse": source,
				"qty": qty,
				"was_recommended": 1 if cint(alloc.get("was_recommended")) else 0,
				"confidence": confidence if confidence in _CONFIDENCES else "",
			}
		)
	return out


def _send_transit_warehouse(send: Any) -> str:
	transit = _g(send, "transit_warehouse")
	if transit:
		return str(transit)
	return _resolve_transit_warehouse(_g(send, "company") or _warehouse_company(_g(send, "from_warehouse")))


def _post_transfer_rows(
	by_source: dict[str, dict[str, float]], target: str, meta: dict[str, Any], remarks: str
) -> str:
	"""ONE Material Transfer, a row per (source, item) into *target*."""
	sources = list(by_source)
	first = sources[0]
	se = transfer_api._build_material_transfer(
		first,
		target,
		[{"item_code": code, "qty": qty} for code, qty in by_source[first].items()],
		posting_date=None,
		remarks=remarks,
	)
	for source in sources[1:]:
		for code, qty in by_source[source].items():
			se.append(
				"items",
				{
					"item_code": code,
					"uom": (_g(meta.get(code), "stock_uom") or DEFAULT_UOM),
					"qty": qty,
					"s_warehouse": source,
					"t_warehouse": target,
				},
			)
	transfer_api._insert_and_submit_stock_entry(se)
	return se.name


def _close_sends(sends: Iterable[Any], user: str, now: Any) -> list[str]:
	"""Mark every send whose lines are all at 0 remaining Confirmed. Returns their names."""
	closed: list[str] = []
	for send in sends:
		if all(_line_remaining(r) <= _QTY_EPS for r in (send.items or [])):
			send.status = STATUS_CONFIRMED
			send.confirmed_by = user
			send.confirmed_at = now
			closed.append(send.name)
	return closed


def approve_receipt(
	receipt: str,
	allocations: Any,
	received: Any = None,
	note: str | None = None,
	version: str | None = None,
	pos_profile: str | None = None,
) -> dict[str, Any]:
	"""A manager approves a pool receipt: ONE Material Transfer into the receiver.

	Rows: transit -> receiver per send allocation, sender -> receiver per direct
	allocation. Lock order: the referenced sends (name order), then the receipt.
	"""
	actor = _build_actor(pos_profile)
	sides = actor.sides
	_require_transit_manager(actor)
	receipt_name = str(receipt or "").strip()
	if not receipt_name:
		frappe.throw(_("receipt is required"))
	allocs = _normalize_allocations(allocations)
	received_map = _parse_json(received, "received", dict)

	# 1. Lock: sends first (name order), then the receipt; re-check everything after.
	sends: dict[str, Any] = {}
	for name in sorted({a["dispatch"] for a in allocs if a["dispatch"]}):
		try:
			sends[name] = _lock(name)
		except frappe.DoesNotExistError:
			frappe.throw(_(_DRIFT_MESSAGE))
	try:
		doc = _lock(receipt_name)
	except frappe.DoesNotExistError:
		frappe.throw(_(_DRIFT_MESSAGE))
	if doc.status != STATUS_PENDING:
		frappe.throw(_("This receipt is already {0}.").format(_(doc.status)))
	if not _is_receipt(doc):
		frappe.throw(_("{0} is not a receipt.").format(doc.name))
	if doc.from_warehouse and doc.from_warehouse not in _transit_sources():
		frappe.throw(
			_("{0} has nothing in transit, so {0} confirms this receipt itself.").format(
				sides.label(doc.from_warehouse)
			)
		)
	if version and str(version).strip() and str(version).strip() != _current_board_version():
		frappe.throw(_(_DRIFT_MESSAGE))

	# 2. Nobody approves their own declaration (the receipt, or a send it uses).
	if actor.user == doc.declared_by:
		frappe.throw(_("You declared this receipt, so another manager has to approve it."), frappe.PermissionError)
	for send in sends.values():
		if actor.user == send.declared_by:
			frappe.throw(
				_("You declared {0}, so another manager has to approve this receipt.").format(send.name),
				frappe.PermissionError,
			)

	# 3. What actually arrived (the manager may correct it; the declaration stays).
	target = doc.to_warehouse
	names_by_code = {row.item_code: (row.item_name or row.item_code) for row in doc.items}
	final = {row.item_code: round(flt(row.qty), QTY_DP) for row in doc.items}
	declared_by_code: dict[str, float] = {}
	for row in doc.items:
		base = _g(row, "declared_qty")
		declared_by_code[row.item_code] = round(
			declared_by_code.get(row.item_code, 0.0) + flt(row.qty if base in (None, "") else base), QTY_DP
		)
	for code, raw in (received_map or {}).items():
		code = str(code or "").strip()
		if code not in final:
			frappe.throw(_("{0} is not on this receipt.").format(code))
		final[code] = _match_qty(raw, names_by_code.get(code, code))
		# Correcting DOWN is any manager's call; UP adds stock nobody declared.
		if final[code] > declared_by_code.get(code, 0.0) + _QTY_EPS and not actor.can_direct:
			frappe.throw(
				_("{0}: only a senior manager can record more than the {1} declared.").format(
					names_by_code.get(code, code), _fmt_qty(declared_by_code.get(code, 0.0))
				),
				frappe.PermissionError,
			)
	if not any(q > _QTY_EPS for q in final.values()):
		frappe.throw(_("Nothing was received: reject the receipt instead."))

	# 4. Every unit received comes from exactly one place.
	per_item: dict[str, float] = {}
	for alloc in allocs:
		if alloc["item_code"] not in final:
			frappe.throw(_("{0} is not on this receipt.").format(alloc["item_code"]))
		per_item[alloc["item_code"]] = round(per_item.get(alloc["item_code"], 0.0) + alloc["qty"], QTY_DP)
	for code, qty in final.items():
		assigned = per_item.get(code, 0.0)
		if abs(assigned - qty) > _QTY_EPS:
			frappe.throw(
				_("{0}: received {1}, assigned {2}. Every unit must come from somewhere.").format(
					names_by_code.get(code, code), _fmt_qty(qty), _fmt_qty(assigned)
				)
			)

	use: dict[tuple[str, str], float] = {}
	for alloc in allocs:
		name = alloc["dispatch"]
		if name:
			send = sends[name]
			if send.status != STATUS_PENDING or not _is_transit_send(send):
				frappe.throw(_(_DRIFT_MESSAGE))
			_require_live_transit(send)
			if alloc["source_warehouse"] and alloc["source_warehouse"] != send.from_warehouse:
				frappe.throw(
					_("{0} was sent by {1}, not {2}.").format(
						name, sides.label(send.from_warehouse), sides.label(alloc["source_warehouse"])
					)
				)
			if send.from_warehouse == target:
				frappe.throw(_("{0} cannot supply its own sender.").format(name))
			alloc["source_warehouse"] = send.from_warehouse
			key = (name, alloc["item_code"])
			use[key] = round(use.get(key, 0.0) + alloc["qty"], QTY_DP)
		else:
			if not actor.can_direct:
				frappe.throw(
					_(
						"Only a senior manager can take stock straight from a sender that never "
						"declared sending it. Match this receipt to a send instead."
					),
					frappe.PermissionError,
				)
			source = alloc["source_warehouse"]
			if not sides.is_known(source):
				frappe.throw(_("{0} is neither the Factory nor a branch.").format(source))
			if source == target:
				frappe.throw(_("A receipt cannot come from its own warehouse."))
	stale_check = bool(version and str(version).strip())
	for (name, code), qty in use.items():
		line = next((r for r in sends[name].items if r.item_code == code), None)
		left = _line_remaining(line) if line is not None else 0.0
		if qty > left + _QTY_EPS:
			if stale_check:
				# The board the manager saw promised more: somebody used it meanwhile.
				frappe.throw(_(_DRIFT_MESSAGE))
			frappe.throw(
				_("{0}: only {1} of {2} left in transit, {3} assigned.").format(
					name, _fmt_qty(left), names_by_code.get(code, code), _fmt_qty(qty)
				)
			)

	# 5. ONE Material Transfer into the receiver.
	by_source: dict[str, dict[str, float]] = {}
	for alloc in allocs:
		s_wh = _send_transit_warehouse(sends[alloc["dispatch"]]) if alloc["dispatch"] else alloc["source_warehouse"]
		per = by_source.setdefault(s_wh, {})
		per[alloc["item_code"]] = round(per.get(alloc["item_code"], 0.0) + alloc["qty"], QTY_DP)
	meta = _item_meta(final)
	shortage_docs = [
		{
			"from_warehouse": s_wh,
			"items": [
				{"item_code": c, "item_name": names_by_code.get(c, c), "qty": q} for c, q in per.items()
			],
		}
		for s_wh, per in by_source.items()
	]
	remarks = _("Transit receipt {0}: {1}").format(
		doc.name, ", ".join(sorted({a["dispatch"] for a in allocs if a["dispatch"]})) or _("direct")
	)
	stock_entry = _post_or_explain(
		lambda: _post_transfer_rows(by_source, target, meta, remarks), shortage_docs, sides, doc.name
	)

	# 6. Close the receipt, bump the sends, write the trail.
	now = _now()
	changed = False
	for row in doc.items:
		qty = final.get(row.item_code, 0.0)
		if abs(flt(row.qty) - qty) > _QTY_EPS:
			changed = True
		row.qty = qty
		row.matched_qty = qty
	parts = []
	for alloc in allocs:
		origin = alloc["dispatch"] or _("direct")
		parts.append(
			f"{sides.label(alloc['source_warehouse'])} {_fmt_qty(alloc['qty'])} "
			f"{names_by_code.get(alloc['item_code'], alloc['item_code'])} ({origin})"
		)
	note_text = _TRANSIT_NOTE_PREFIX + "; ".join(parts)
	if changed:
		doc.was_changed = 1
		note_text += ". Received corrected by the manager"
	named = doc.from_warehouse
	used = list(dict.fromkeys(a["source_warehouse"] for a in allocs if a["source_warehouse"]))
	if named and any(s != named for s in used):
		# The receiver named a sender; the manager took (some of) it from elsewhere.
		note_text += ". Sender overridden by {0}: receipt named {1}, used {2}".format(
			actor.user, sides.label(named), ", ".join(sides.label(s) for s in used)
		)
	extra = str(note or "").strip()
	if extra:
		note_text += f". Note: {extra}"
	doc.status = STATUS_CONFIRMED
	doc.confirmed_by = actor.user
	doc.confirmed_at = now
	doc.stock_entry = stock_entry
	doc.match_note = note_text[:2000]
	_save(doc)

	for (name, code), qty in use.items():
		for row in sends[name].items:
			if row.item_code == code:
				row.matched_qty = round(flt(_g(row, "matched_qty")) + qty, QTY_DP)
				break
	closed = _close_sends([sends[n] for n in sorted(sends)], actor.user, now)
	for name in sorted(sends):
		_save(sends[name])

	for alloc in allocs:
		record = frappe.get_doc(
			{
				"doctype": TRANSIT_MATCH_DOCTYPE,
				"receipt": doc.name,
				"dispatch": alloc["dispatch"],
				"item_code": alloc["item_code"],
				"qty": alloc["qty"],
				"source_warehouse": alloc["source_warehouse"],
				"target_warehouse": target,
				"stock_entry": stock_entry,
				"approved_by": actor.user,
				"approved_at": now,
				"was_recommended": alloc["was_recommended"],
				"confidence": alloc["confidence"],
			}
		)
		record.flags.from_handover_service = True
		record.insert(ignore_permissions=True)

	_enqueue_push(doc.name, EVENT_CONFIRMED, actor.user)
	for source in dict.fromkeys(sends[n].from_warehouse for n in closed):
		_after_dispatch_closed(source, actor.user)
	return {"ok": True, "stock_entry": stock_entry, "receipt": doc.name, "closed_sends": closed}


def reject_receipt(receipt: str, reason: str | None = None, pos_profile: str | None = None) -> dict[str, Any]:
	"""A manager rejects a pool receipt (nothing arrived from any send). Nothing posts."""
	actor = _build_actor(pos_profile)
	_require_transit_manager(actor)
	name = str(receipt or "").strip()
	if not name:
		frappe.throw(_("receipt is required"))
	doc, _grouped = _lock_with_group(name)
	if doc.status != STATUS_PENDING:
		frappe.throw(_("This receipt is already {0}.").format(_(doc.status)))
	if not _is_receipt(doc):
		frappe.throw(_("{0} is not a receipt.").format(doc.name))
	if doc.from_warehouse and doc.from_warehouse not in _transit_sources():
		frappe.throw(
			_("{0} has nothing in transit, so {0} answers this receipt itself.").format(
				actor.sides.label(doc.from_warehouse)
			)
		)
	if actor.user == doc.declared_by:
		frappe.throw(_("You declared this receipt, so another manager has to reject it."), frappe.PermissionError)
	text = str(reason or "").strip()[:1000]
	doc.status = STATUS_REJECTED
	doc.rejected_by = actor.user
	doc.rejected_at = _now()
	doc.reject_reason = text or None
	doc.match_note = (_TRANSIT_NOTE_PREFIX + (text or "rejected by a manager"))[:2000]
	_save(doc)
	_enqueue_push(doc.name, EVENT_REJECTED, actor.user)
	return {"ok": True, "handover": _serialize_doc(doc, actor)}


def _settle_send(
	dispatch: str, lines: Any, reason: str | None, mode: str, pos_profile: str | None
) -> dict[str, Any]:
	"""Write off (``mode="write_off"``) or return (``"return"``) part of a send's remaining stock."""
	actor = _build_actor(pos_profile)
	sides = actor.sides
	_require_transit_manager(actor)
	reason_text = str(reason or "").strip()[:1000]
	write_off = mode == "write_off"
	if write_off and not reason_text:
		frappe.throw(_("Give a reason for the write-off."))
	merged = _normalize_lines(lines)

	send = _lock(dispatch)
	if send.status != STATUS_PENDING:
		frappe.throw(_("This send is already {0}.").format(_(send.status)))
	if not _is_transit_send(send):
		frappe.throw(_("{0} is not a send in transit.").format(send.name))
	if actor.user == send.declared_by:
		frappe.throw(
			_("You declared this send, so another manager has to settle it."), frappe.PermissionError
		)
	_require_live_transit(send)
	by_code = {row.item_code: row for row in send.items}
	for code, qty in merged.items():
		row = by_code.get(code)
		if row is None:
			frappe.throw(_("{0} is not on this send.").format(code))
		left = _line_remaining(row)
		if qty > left + _QTY_EPS:
			frappe.throw(
				_("{0}: only {1} left in transit, {2} asked.").format(
					row.item_name or code, _fmt_qty(left), _fmt_qty(qty)
				)
			)

	transit = _send_transit_warehouse(send)
	company = _g(send, "company") or _warehouse_company(transit, send.from_warehouse)
	shortage = {
		"from_warehouse": transit,
		"items": [
			{"item_code": c, "item_name": by_code[c].item_name or c, "qty": q} for c, q in merged.items()
		],
	}
	if write_off:
		account = frappe.db.get_value("Company", company, "stock_adjustment_account") if company else None
		if not account:
			frappe.throw(
				_("Set the Stock Adjustment Account on {0} to write off stock in transit.").format(
					company or _("the company")
				)
			)
		# No existing warehouse -> POS Profile cost-centre helper: the Company's default.
		cost_center = frappe.db.get_value("Company", company, "cost_center") if company else None
		remarks = _("Written off from transit, send {0}: {1}").format(send.name, reason_text)
		entry = _post_or_explain(
			lambda: _post_write_off(company, transit, merged, account, remarks, cost_center),
			shortage,
			sides,
			send.name,
		)
		field = "written_off_qty"
	else:
		remarks = _("Returned from transit to the sender, send {0}").format(send.name)
		if reason_text:
			remarks += f": {reason_text}"
		entry = _post_or_explain(
			lambda: _post_transfer_rows({transit: merged}, send.from_warehouse, _item_meta(merged), remarks),
			shortage,
			sides,
			send.name,
		)
		field = "returned_qty"

	for code, qty in merged.items():
		row = by_code[code]
		setattr(row, field, round(flt(_g(row, field)) + qty, QTY_DP))
	detail = ", ".join(f"{by_code[c].item_name or c} {_fmt_qty(q)}" for c, q in merged.items())
	if write_off:
		line = f"Written off {detail} ({entry}): {reason_text}"
	else:
		line = f"Returned to {sides.label(send.from_warehouse)} {detail} ({entry})"
		if reason_text:
			line += f": {reason_text}"
	send.match_note = (f"{send.match_note}; {line}" if send.match_note else line)[:2000]
	now = _now()
	closed = bool(_close_sends([send], actor.user, now))
	_save(send)
	if closed:
		_after_dispatch_closed(send.from_warehouse, actor.user)
	return {"ok": True, "stock_entry": entry, "dispatch": send.name, "closed": closed}


def _post_write_off(
	company: str,
	transit: str,
	lines: dict[str, float],
	account: str,
	remarks: str,
	cost_center: str | None = None,
) -> str:
	"""A Material Issue out of *transit*, charged to the Stock Adjustment Account."""
	meta = _item_meta(lines)
	se = frappe.new_doc("Stock Entry")
	se.stock_entry_type = "Material Issue"
	se.purpose = "Material Issue"
	se.company = company
	se.remarks = remarks
	for code, qty in lines.items():
		row = {
			"item_code": code,
			"uom": (_g(meta.get(code), "stock_uom") or DEFAULT_UOM),
			"qty": qty,
			"s_warehouse": transit,
			"expense_account": account,
		}
		if cost_center:
			row["cost_center"] = cost_center
		se.append("items", row)
	transfer_api._insert_and_submit_stock_entry(se)
	return se.name


def write_off_transit(
	dispatch: str, lines: Any, reason: str | None = None, pos_profile: str | None = None
) -> dict[str, Any]:
	"""Write off part of a send as lost (Material Issue from transit). Reason required."""
	return _settle_send(dispatch, lines, reason, "write_off", pos_profile)


def return_to_sender(
	dispatch: str, lines: Any, reason: str | None = None, pos_profile: str | None = None
) -> dict[str, Any]:
	"""Move part of a send back from transit to its sender (Material Transfer)."""
	return _settle_send(dispatch, lines, reason, "return", pos_profile)


# ---------------------------------------------------------------------------
# Push
# ---------------------------------------------------------------------------


def _manager_recipients() -> list[str]:
	"""Everyone who may match a batch (the stock-transfer roles)."""
	from jarz_pos.api.notifications import _get_users_with_roles

	return [u for u in _get_users_with_roles(sorted(ROLES.STOCK_TRANSFER)) if u and u != "Guest"]


def _side_recipients(warehouse: str | None, sides: _Sides) -> list[str]:
	"""Branch: users of the POS Profiles stocking from it. Factory: the production team."""
	from jarz_pos.api.notifications import _get_users_with_roles

	users: list[str] = []
	if sides.is_branch(warehouse):
		users.extend(get_users_for_pos_profiles(sides.branch_profiles.get(warehouse, [])))
	if sides.is_factory(warehouse):
		users.extend(_get_users_with_roles(sorted(ROLES.PRODUCTION_EXECUTE)))
	if not users and warehouse and not sides.is_known(warehouse):
		users.extend(_get_users_with_roles(sorted(ROLES.STOCK_TRANSFER)))
	seen: set[str] = set()
	out: list[str] = []
	for user in users:
		if user and user != "Guest" and user not in seen:
			seen.add(user)
			out.append(user)
	return out


def _push_text(
	doc: Any,
	event: str,
	sides: _Sides,
	actor_name: str | None,
	extra: dict[str, Any] | None = None,
) -> tuple[str, str]:
	"""(title, body) -- Arabic first, English second."""
	extra = extra or {}
	from_wh = _g(doc, "from_warehouse")
	from_label = sides.label(from_wh) if from_wh else UNKNOWN_SENDER_LABEL
	dispatch = _is_dispatch(doc)
	to_label = "" if dispatch else sides.label(_g(doc, "to_warehouse"))
	n = _fmt_qty(sum(flt(_g(r, "qty")) for r in (_g(doc, "items") or [])))
	awaiting = _g(doc, "awaiting_side")
	proposer_label = sides.label(_side_warehouse(doc, _other_side(awaiting)))
	transit_answer = str(_g(doc, "match_note") or "").startswith(_TRANSIT_NOTE_PREFIX) or (
		_is_receipt(doc) and not from_wh
	)
	responder_label = (
		MANAGER_LABEL
		if awaiting == SIDE_MANAGER or transit_answer
		else sides.label(_side_warehouse(doc, awaiting))
	)
	route_ar = from_label if dispatch else f"{from_label} ← {to_label}"
	route_en = from_label if dispatch else f"{from_label} → {to_label}"
	title = "تسليم مخزون · Stock handover"

	if event == EVENT_TRANSIT:
		if dispatch:
			ar = f"{from_label} بعت {n} برطمان (في الطريق) — طابقها"
			en = f"{from_label} sent out {n} jars (in transit) — match it"
		else:
			ar = f"{to_label} استلم {n} برطمان — طابق الاستلام"
			en = f"{to_label} received {n} jars — match the receipt"
	elif event == EVENT_BALANCED:
		sent = _fmt_qty(extra.get("sent", 0))
		ar = f"شحنة {from_label} اتطابقت: {sent} برطمان — راجع وأكّد"
		en = f"{from_label}'s sent-out batch adds up ({sent} jars) — match it"
	elif event == EVENT_BATCH_CONFIRMED:
		who = actor_name or MANAGER_LABEL
		if _g(doc, "status") == STATUS_REJECTED:
			ar = f"{who} طابق شحنة {from_label} — استلامك مش ضمن الشحنة"
			en = f"{who} matched {from_label}'s shipment — your receipt was not part of it"
		else:
			ar = f"{who} طابق شحنة {from_label} وأكّد {n} برطمان"
			en = f"{who} matched {from_label}'s shipment and confirmed {n} jars"
		note = str(_g(doc, "match_note") or "").strip()
		if note:
			en = f"{en}. {note[:160]}"
	elif event == EVENT_BATCH_REMINDER:
		hours = extra.get("hours", REMIND_AFTER_HOURS)
		unmatched = _fmt_qty(extra.get("unmatched", 0))
		ar = f"شحنة {from_label} مستنية مطابقة من {hours} ساعة ({unmatched} برطمان مش متطابق)"
		en = f"{from_label}'s sent-out batch has waited {hours}h for a manager match ({unmatched} jars unmatched)"
	elif event == EVENT_DECLARED:
		if _g(doc, "source") == SOURCE_MANAGER:
			who = actor_name or "Manager"
			ar = f"{who} سجّل نقل {n} برطمان من {from_label} إلى {to_label} — أكّد"
			en = f"{who} recorded {n} jars from {from_label} to {to_label} — confirm"
		elif _g(doc, "declared_side") == SIDE_TO:
			ar = f"{to_label} سجّل استلام {n} برطمان من {from_label} — أكّد"
			en = f"{to_label} says it received {n} jars from {from_label} — confirm"
		else:
			ar = f"{from_label} سجّل تسليم {n} برطمان إلى {to_label} — أكّد"
			en = f"{from_label} says {to_label} took {n} jars — confirm"
	elif event == EVENT_CHANGED:
		ar = f"{proposer_label} عدّل الكميات ({n} برطمان، {route_ar}) — راجع وأكّد"
		en = f"{proposer_label} changed the quantities ({n} jars, {route_en}) — review"
	elif event == EVENT_CONFIRMED:
		ar = f"{responder_label} أكّد {n} برطمان ({route_ar})"
		en = f"{responder_label} confirmed {n} jars ({route_en})"
	elif event == EVENT_REJECTED:
		ar = f"{responder_label} رفض التسليم ({route_ar})"
		en = f"{responder_label} rejected the handover ({route_en})"
		reason = str(_g(doc, "reject_reason") or "").strip()
		if reason:
			en = f"{en}: {reason[:120]}"
	elif event == EVENT_CANCELLED:
		ar = f"{proposer_label} ألغى التسليم ({route_ar})"
		en = f"{proposer_label} cancelled the handover ({route_en})"
	else:  # reminder
		ar = f"مستني تأكيدك من أكتر من {REMIND_AFTER_HOURS} ساعة: {n} برطمان ({route_ar})"
		en = f"Waiting for your answer for over {REMIND_AFTER_HOURS}h: {n} jars ({route_en})"
	return title, f"{ar}\n{en}"


def _notify_side(doc: Any, event: str) -> str | None:
	"""The side a push for *event* goes to."""
	awaiting = _g(doc, "awaiting_side")
	if event in (EVENT_CONFIRMED, EVENT_REJECTED):
		# The responder is the awaiting side; tell the proposer.
		return _side_warehouse(doc, _other_side(awaiting))
	return _side_warehouse(doc, awaiting)


def _recipients_for(handover: Any, event: str, sides: _Sides) -> list[str]:
	if event in _BATCH_MANAGER_EVENTS:
		return _manager_recipients()
	if event == EVENT_BATCH_CONFIRMED:
		declarer = _g(handover, "declared_by")
		return [declarer] if declarer else []
	return _side_recipients(_notify_side(handover, event), sides)


def notify_stock_handover(
	handover: Any, event: str, actor: str | None = None, extra: dict[str, Any] | None = None
) -> dict[str, Any]:
	"""Push *event* about *handover* to whoever has to know. Never raises.

	Batch events go to the managers (balanced / reminder) or to the declarer
	(batch confirmed); everything else goes to the side that has to answer.
	"""
	result: dict[str, Any] = {"ok": True, "status": "skipped", "recipients": 0}
	try:
		if _suppressed(f"stock_handover:{event}"):
			result["status"] = "suppressed_test_run"
			return result
		name = str(_g(handover, "name") or "")
		if not name:
			result["status"] = "skipped_no_handover"
			return result

		from jarz_pos.api import notifications as n

		sides = _load_sides()
		recipients = [u for u in _recipients_for(handover, event, sides) if u != actor]
		result["recipients"] = len(recipients)
		if not recipients:
			result["status"] = "skipped_no_recipients"
			return result

		if event == EVENT_BALANCED and extra is None:
			group = _load_group(_g(handover, "from_warehouse"))
			extra = {"sent": sum(i["sent"] for i in group["items"])} if group else {}

		actor_name = _user_names([actor]).get(actor) if actor else None
		title, body = _push_text(handover, event, sides, actor_name, extra)
		data = {
			"type": NOTIFICATION_TYPE,
			"title": title,
			"body": body,
			"notification_id": name,
			"route": NOTIFICATION_ROUTE,
			"handover": name,
			"event": str(event or ""),
			"status": str(_g(handover, "status") or ""),
			"timestamp": _now().isoformat(),
		}

		tokens, token_platforms = n._get_token_targets_for_users(recipients)
		vapid_subs = n._get_vapid_subscriptions_for_users(recipients)
		if tokens:
			fcm_result = n._send_fcm_notifications(tokens, data, platforms=token_platforms)
		else:
			fcm_result = n._new_fcm_send_result(tokens, "skipped_no_tokens")
			fcm_result["ok"] = True
		if vapid_subs:
			vapid_result = n._send_vapid_notifications(vapid_subs, data)
		else:
			vapid_result = {
				"ok": True,
				"status": "skipped_no_subscriptions",
				"success_count": 0,
				"failure_count": 0,
			}
		if not tokens and not vapid_subs:
			n._log_notification_gap(
				"Stock handover push reached no device",
				(
					f"Stock handover {name} ({event}) resolved {len(recipients)} recipient(s) but none "
					f"has an enabled push token or web-push subscription. Recipients: {', '.join(recipients)}."
				),
				throttle_key="stock_handover:notokens",
			)
		result.update(
			{
				"ok": bool(fcm_result.get("ok")) or bool(vapid_result.get("ok")),
				"status": fcm_result.get("status"),
				"success_count": fcm_result.get("success_count", 0) + vapid_result.get("success_count", 0),
				"failure_count": fcm_result.get("failure_count", 0) + vapid_result.get("failure_count", 0),
			}
		)
		return result
	except Exception:
		_safe_log("stock_handover: push failed")
		result["ok"] = False
		result["status"] = "failed_exception"
		return result


def send_stock_handover_push(handover_name: str, kind: str, actor_user: str | None = None) -> dict[str, Any]:
	"""Background-job entry point (takes the NAME: RQ serialises its kwargs). Never raises."""
	try:
		doc = frappe.get_doc(DOCTYPE, handover_name)
	except Exception:
		_safe_log("stock_handover: push load failed")
		return {"ok": False, "status": "failed_load"}
	return notify_stock_handover(doc, kind, actor_user or None)


def _enqueue_push(handover_name: str, kind: str, actor_user: str | None) -> bool:
	"""Queue the push for after the current transaction commits. Never raises.

	The kwargs avoid ``event`` / ``job_name`` / ``timeout``: those are
	``frappe.enqueue``'s own parameters and would be swallowed by it.
	"""
	try:
		if _suppressed(f"stock_handover:{kind}"):
			return False
		frappe.enqueue(
			"jarz_pos.services.stock_handover.send_stock_handover_push",
			queue="short",
			enqueue_after_commit=True,
			handover_name=str(handover_name),
			kind=str(kind),
			actor_user=str(actor_user or ""),
		)
		return True
	except Exception:
		_safe_log("stock_handover: enqueue failed")
		return False


def _remind_one_to_one(summary: dict[str, int], cutoff: Any, sides: _Sides) -> None:
	"""Re-push one-to-one handovers waiting over 12h (never a Dispatch, never a grouped receipt)."""
	dispatch_sources = _pending_dispatch_sources()
	# Dispatches and grouped receipts are skipped below; leave them out of the query too
	# so they cannot use up the 200-row window every hour.
	filters: dict[str, Any] = {
		"status": STATUS_PENDING,
		"kind": DOC_KIND_HANDOVER,
		"last_proposed_at": ["<", cutoff],
	}
	if dispatch_sources:
		filters["from_warehouse"] = ["not in", sorted(dispatch_sources)]
	rows = frappe.get_all(
		DOCTYPE,
		filters=filters,
		or_filters=[
			[DOCTYPE, "reminder_sent_at", "is", "not set"],
			[DOCTYPE, "reminder_sent_at", "<", cutoff],
		],
		fields=[
			"name",
			"status",
			"kind",
			"from_warehouse",
			"to_warehouse",
			"declared_side",
			"awaiting_side",
		],
		order_by="last_proposed_at asc",
		limit_page_length=200,
	)
	for row in rows or []:
		summary["checked"] += 1
		if _group_source(row, dispatch_sources):
			continue
		if not sides.is_known(_side_warehouse(row, row.get("awaiting_side"))):
			# A disabled profile or removed warehouse: nobody to ask.
			continue
		try:
			doc = frappe.get_doc(DOCTYPE, row.get("name"))
			notify_stock_handover(doc, EVENT_REMINDER, None)
			frappe.db.set_value(DOCTYPE, row.get("name"), "reminder_sent_at", _now(), update_modified=False)
			frappe.db.commit()
			summary["sent"] += 1
		except Exception:
			summary["errors"] += 1
			_safe_log("stock_handover: reminder failed")


def _remind_groups(summary: dict[str, int], cutoff: Any) -> None:
	"""Re-push each match group whose oldest member is over 12h old, at most once per 12h."""
	rows = frappe.get_all(
		DOCTYPE,
		filters={"status": STATUS_PENDING},
		fields=_GROUP_FIELDS,
		order_by="declared_at asc",
		limit_page_length=_PENDING_SCAN_LIMIT,
	)
	for source, group in _match_groups(rows or []).items():
		members = group["dispatches"] + group["receipts"]
		summary["checked"] += 1
		declared = [get_datetime(_g(m, "declared_at")) for m in members if _g(m, "declared_at")]
		if not declared or min(declared) > cutoff:
			continue
		reminded = [get_datetime(_g(m, "reminder_sent_at")) for m in members if _g(m, "reminder_sent_at")]
		if reminded and max(reminded) > cutoff:
			continue
		try:
			names = [_g(m, "name") for m in members]
			info = _group_summary(source, group["dispatches"], group["receipts"], _load_items(names))
			target = frappe.get_doc(DOCTYPE, info["newest_dispatch"])
			hours = int((_now() - min(declared)).total_seconds() // 3600)
			notify_stock_handover(
				target,
				EVENT_BATCH_REMINDER,
				None,
				extra={"hours": hours, "unmatched": info["unmatched_units"]},
			)
			frappe.db.set_value(DOCTYPE, target.name, "reminder_sent_at", _now(), update_modified=False)
			frappe.db.commit()
			summary["sent"] += 1
		except Exception:
			summary["errors"] += 1
			_safe_log("stock_handover: batch reminder failed")


def remind_stale_handovers() -> dict[str, int]:
	"""Hourly: re-push handovers and sent-out batches waiting over 12h, at most once per 12h. Never raises."""
	summary = {"checked": 0, "sent": 0, "errors": 0}
	try:
		if not _doctype_ready():
			return summary
		cutoff = _now() - timedelta(hours=REMIND_AFTER_HOURS)
		sides = _load_sides()
		try:
			_remind_one_to_one(summary, cutoff, sides)
		except Exception:
			summary["errors"] += 1
			_safe_log("stock_handover: reminder sweep failed")
		try:
			_remind_groups(summary, cutoff)
		except Exception:
			summary["errors"] += 1
			_safe_log("stock_handover: batch reminder sweep failed")
	except Exception:
		summary["errors"] += 1
		_safe_log("stock_handover: reminder sweep failed")
	return summary
