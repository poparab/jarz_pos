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

Sent-out batches (v2): a side may also say only **what left** ("I sent out",
kind ``Dispatch``) without naming where it went. The receiving branches each
declare "I got this from X" as before. While X has a pending Dispatch, X's pending
receipts and its Dispatches form a **match group** (computed when needed, never
stored); X cannot answer a receipt 1:1 then, and a manager confirms the whole
group at once with :func:`confirm_match`, posting ONE Material Transfer that
splits what X sent across the receivers, only when every unit is accounted for.

API surface: ``jarz_pos.api.stock_handover`` (thin, whitelisted). Managers'
``api.transfer.submit_transfer`` routes branch-touching transfers here through
:func:`create_manager_handover`.
"""

from __future__ import annotations

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
_BATCH_MANAGER_EVENTS = (EVENT_BALANCED, EVENT_BATCH_REMINDER)

#: Quantities are whole units or fractions to this many decimals.
QTY_DP = 3
_QTY_EPS = 5e-4

_REJECT_NOT_IN_BATCH = "Not in {0}'s shipment (manager match)"
_DRIFT_MESSAGE = "Something changed in this batch. Reload and check again."


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


def _is_changed(row: Any, items: Sequence[Any]) -> bool:
	"""True once the other side changed the quantities (the flag, or any qty off its declaration)."""
	if cint(_g(row, "was_changed")):
		return True
	return any(abs(flt(_g(i, "qty")) - flt(_g(i, "declared_qty"))) > 1e-9 for i in items or [])


def _can_cancel(row: Any, items: Sequence[Any], actor: _Actor) -> bool:
	if _g(row, "status") != STATUS_PENDING:
		return False
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
	if _is_dispatch(row):
		# The only answer a Dispatch takes through ``respond`` is a manager's reject.
		return actor.is_manager
	if group_source:
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
	from_wh = _g(row, "from_warehouse")
	to_wh = _g(row, "to_warehouse") or None
	dispatch = _is_dispatch(row)
	group_source = _group_source(row, dispatch_sources)
	awaiting = _g(row, "awaiting_side")
	declared_by = _g(row, "declared_by")
	last_by = _g(row, "last_proposed_by")
	if dispatch or group_source:
		awaiting, awaiting_label = SIDE_MANAGER, MANAGER_LABEL
	else:
		awaiting_label = sides.label(_side_warehouse(row, awaiting))
	return {
		"name": _g(row, "name"),
		"status": _g(row, "status"),
		"kind": _kind(row),
		"group_source": group_source,
		"from_warehouse": from_wh,
		"from_label": sides.label(from_wh),
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
	"""
	pending = [r for r in rows or [] if _g(r, "status") == STATUS_PENDING]
	groups: dict[str, dict[str, list[Any]]] = {}
	for row in pending:
		if _is_dispatch(row) and _g(row, "from_warehouse"):
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
) -> Any:
	now = _now()
	dispatch = kind == DOC_KIND_DISPATCH
	doc = frappe.get_doc(
		{
			"doctype": DOCTYPE,
			"kind": kind,
			"company": _warehouse_company(from_warehouse, to_warehouse),
			"from_warehouse": from_warehouse,
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


def _post_or_explain(post: Any, shortage_doc: Any, sides: _Sides, subject: str) -> Any:
	"""Run ``post()`` inside a savepoint; on failure roll back and throw ONE clear message.

	A stock shortage is explained per line (item + what is available). Any other
	failure surfaces its real message. Either way the handover stays Pending.
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
		message = _shortage_message(shortage_doc, sides) if _looks_like_shortage(exc) else None
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

	return {"my_sides": my_sides, "counterparts": counterparts, "can_manage": actor.is_manager}


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
	my_warehouse: str, counterpart_warehouse: str | None = None, search: str | None = None
) -> dict[str, Any]:
	"""Items a side may hand over. With no counterpart: the "sent out" set (all jar groups)."""
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

	``direction`` "in" = I received from *counterpart_warehouse* (a one-to-one
	handover the counterpart confirms, or a manager matches when the counterpart
	has a sent-out batch open). "out" = I sent stock out: a Dispatch with no
	destination (*counterpart_warehouse* is ignored), matched later by a manager.

	*client_request_id* makes a repeated declare (a double tap) return the row the
	first one created.
	"""
	actor = _build_actor(pos_profile)
	sides = actor.sides

	direction = (direction or "").strip().lower()
	if direction not in ("in", "out"):
		frappe.throw(_("direction must be 'in' (I received) or 'out' (I sent out)"))

	counterpart = ""
	if direction == "in":
		my_warehouse, counterpart = _validate_pair(actor, my_warehouse, counterpart_warehouse or "")
	else:
		my_warehouse = _validate_side(actor, my_warehouse)

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
	if direction == "in":
		allowed = _allowed_item_codes(my_warehouse, counterpart, sides)
	else:
		allowed = _dispatch_item_codes(my_warehouse, sides)
	unknown = sorted(code for code in merged if code not in allowed)
	if unknown:
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
			from_warehouse=counterpart,
			to_warehouse=my_warehouse,
			declared_side=SIDE_TO,
			source=source,
			lines=merged,
			user=actor.user,
			pos_profile=used_profile,
			client_request_id=request_id,
		)
	else:
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
		)

	dispatch_sources = _pending_dispatch_sources()
	if direction == "out":
		# Nobody answers a Dispatch one-to-one; managers hear about it once it adds up.
		_maybe_push_balanced(my_warehouse, actor.user)
	elif counterpart in dispatch_sources:
		# The counterpart has a sent-out batch open: a manager matches this receipt.
		_maybe_push_balanced(counterpart, actor.user)
	else:
		_enqueue_push(doc.name, EVENT_DECLARED, actor.user)
	return {"ok": True, "handover": _serialize_doc(doc, actor, dispatch_sources)}


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
		"modified",
	]


def list_handovers(
	my_warehouse: str | None = None,
	pos_profile: str | None = None,
	limit: Any = 30,
) -> dict[str, Any]:
	actor = _build_actor(pos_profile)
	limit = max(1, min(cint(limit) or 30, 200))
	empty: dict[str, Any] = {"waiting_for_me": [], "waiting_for_them": [], "recent": [], "to_match": []}

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

	out: dict[str, Any] = {"waiting_for_me": [], "waiting_for_them": [], "recent": [], "to_match": []}
	for row in pending:
		if _group_source(row, dispatch_sources):
			# Answered by a manager's match, never one-to-one.
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
	else:
		_enqueue_push(doc.name, EVENT_CANCELLED, actor.user)
	return {"ok": True, "handover": _serialize_doc(doc, actor)}


def _maybe_push_balanced(source: str, actor_user: str | None) -> bool:
	"""Tell the managers when *source*'s batch now adds up. Never raises.

	No state is stored: every action that leaves the group balanced is one that made
	it so (adding to, or cancelling from, a balanced group always unbalances it).
	"""
	try:
		group = _load_group(source)
		if not group or not group.get("balanced"):
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
	if not dispatch_names or set(dispatch_names) | set(receipt_names) != set(member_names):
		frappe.throw(_(_DRIFT_MESSAGE))
	docs: dict[str, Any] = {}
	for name in sorted(dispatch_names) + sorted(receipt_names):
		try:
			docs[name] = _lock(name)
		except frappe.DoesNotExistError:
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
	from_label = sides.label(_g(doc, "from_warehouse"))
	dispatch = _is_dispatch(doc)
	to_label = "" if dispatch else sides.label(_g(doc, "to_warehouse"))
	n = _fmt_qty(sum(flt(_g(r, "qty")) for r in (_g(doc, "items") or [])))
	awaiting = _g(doc, "awaiting_side")
	proposer_label = sides.label(_side_warehouse(doc, _other_side(awaiting)))
	responder_label = (
		MANAGER_LABEL if awaiting == SIDE_MANAGER else sides.label(_side_warehouse(doc, awaiting))
	)
	route_ar = from_label if dispatch else f"{from_label} ← {to_label}"
	route_en = from_label if dispatch else f"{from_label} → {to_label}"
	title = "تسليم مخزون · Stock handover"

	if event == EVENT_BALANCED:
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
