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

SOURCE_BRANCH = "Branch"
SOURCE_FACTORY = "Factory"
SOURCE_MANAGER = "Manager"

KIND_FACTORY = "factory"
KIND_BRANCH = "branch"

FACTORY_LABEL = "Factory"

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
	"""``{item_code: qty}`` -- zero/negative lines dropped, repeats summed.

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
		if qty <= 0:
			continue
		if not code:
			frappe.throw(_("A line with a quantity has no item"))
		merged[code] = round(merged.get(code, 0.0) + qty, 6)
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


def _side_warehouse(row: Any, side: str | None) -> str | None:
	return _g(row, "from_warehouse") if side == SIDE_FROM else _g(row, "to_warehouse")


def _other_side(side: str | None) -> str:
	return SIDE_TO if side == SIDE_FROM else SIDE_FROM


def _is_changed(row: Any, items: Sequence[Any]) -> bool:
	"""True once anyone but the declaration has touched the quantities."""
	if any(abs(flt(_g(i, "qty")) - flt(_g(i, "declared_qty"))) > 1e-9 for i in items or []):
		return True
	last = _g(row, "last_proposed_by")
	declared = _g(row, "declared_by")
	return bool(last and declared and last != declared)


def _can_cancel(row: Any, items: Sequence[Any], actor: _Actor) -> bool:
	if _g(row, "status") != STATUS_PENDING:
		return False
	declared_side = _g(row, "declared_side")
	if _g(row, "awaiting_side") == declared_side:
		return False
	if _is_changed(row, items):
		return False
	return actor.can_act_for(_side_warehouse(row, declared_side))


def _can_respond(row: Any, actor: _Actor) -> bool:
	if _g(row, "status") != STATUS_PENDING:
		return False
	if _g(row, "last_proposed_by") == actor.user:
		return False
	return actor.can_act_for(_side_warehouse(row, _g(row, "awaiting_side")))


def _serialize(row: Any, items: Sequence[Any], actor: _Actor, names: dict[str, str]) -> dict[str, Any]:
	sides = actor.sides
	from_wh = _g(row, "from_warehouse")
	to_wh = _g(row, "to_warehouse")
	awaiting = _g(row, "awaiting_side")
	declared_by = _g(row, "declared_by")
	last_by = _g(row, "last_proposed_by")
	return {
		"name": _g(row, "name"),
		"status": _g(row, "status"),
		"from_warehouse": from_wh,
		"from_label": sides.label(from_wh),
		"to_warehouse": to_wh,
		"to_label": sides.label(to_wh),
		"awaiting_side": awaiting,
		"awaiting_label": sides.label(_side_warehouse(row, awaiting)),
		"declared_by_name": names.get(declared_by, declared_by) if declared_by else None,
		"declared_at": _iso(_g(row, "declared_at")),
		"last_proposed_by_name": names.get(last_by, last_by) if last_by else None,
		"last_proposed_at": _iso(_g(row, "last_proposed_at")),
		"changed": _is_changed(row, items),
		"can_respond": _can_respond(row, actor),
		"can_cancel": _can_cancel(row, items, actor),
		"stock_entry": _g(row, "stock_entry") or None,
		"reject_reason": _g(row, "reject_reason") or None,
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


def _serialize_doc(doc: Any, actor: _Actor) -> dict[str, Any]:
	items = list(_g(doc, "items") or [])
	names = _user_names([_g(doc, "declared_by"), _g(doc, "last_proposed_by")])
	return _serialize(doc, items, actor, names)


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


def _item_child_rows(lines: dict[str, float], declared: dict[str, float] | None = None) -> list[dict[str, Any]]:
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


def _insert_handover(
	*,
	from_warehouse: str,
	to_warehouse: str,
	declared_side: str,
	source: str,
	lines: dict[str, float],
	user: str,
	pos_profile: str | None = None,
) -> Any:
	now = _now()
	doc = frappe.get_doc(
		{
			"doctype": DOCTYPE,
			"company": _warehouse_company(from_warehouse, to_warehouse),
			"from_warehouse": from_warehouse,
			"to_warehouse": to_warehouse,
			"status": STATUS_PENDING,
			"declared_side": declared_side,
			"awaiting_side": _other_side(declared_side),
			"source": source,
			"pos_profile": pos_profile or None,
			"declared_by": user,
			"declared_at": now,
			"last_proposed_by": user,
			"last_proposed_at": now,
			"items": _item_child_rows(lines),
		}
	)
	doc.insert(ignore_permissions=True)
	return doc


def touches_branch(source_warehouse: str, target_warehouse: str) -> bool:
	"""True when a manager transfer must become a pending handover.

	False before ``bench migrate`` has created the DocType, so the deploy window
	keeps the old immediate posting instead of failing every transfer.
	"""
	if not _doctype_ready():
		return False
	sides = _load_sides()
	return sides.is_branch(source_warehouse) or sides.is_branch(target_warehouse)


def create_manager_handover(source_warehouse: str, target_warehouse: str, lines: Sequence[dict[str, Any]]) -> str:
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
	savepoint = "jarz_stock_handover_confirm"
	try:
		frappe.db.savepoint(savepoint)
	except Exception:
		savepoint = None
	try:
		stock_entry = _post_stock_entry(doc)
	except Exception as exc:
		if savepoint:
			try:
				frappe.db.rollback(save_point=savepoint)
			except Exception:
				pass
		message = _shortage_message(doc, sides)
		if not message:
			detail = frappe.utils.strip_html(str(exc) or exc.__class__.__name__)[:300]
			message = _("Could not post the stock transfer for {0}: {1}. It is still pending.").format(
				_g(doc, "name"), detail
			)
		# ERPNext already queued its own message for the same failure; one clear
		# message is what the screen should show.
		try:
			frappe.local.message_log = []
		except Exception:
			pass
		frappe.throw(message, title=_("Transfer not posted"))
	now = _now()
	doc.status = STATUS_CONFIRMED
	doc.confirmed_by = user
	doc.confirmed_at = now
	doc.stock_entry = stock_entry
	doc.save(ignore_permissions=True)


def _lock(name: str) -> Any:
	"""The handover, parent AND child rows read with ``FOR UPDATE``.

	A locking read is always served from the latest committed rows, so the
	status / turn checks below cannot act on a stale snapshot.
	"""
	name = (name or "").strip()
	if not name:
		frappe.throw(_("Handover name is required"))
	return frappe.get_doc(DOCTYPE, name, for_update=True)


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
			my_sides.append({"warehouse": warehouse, "label": profile, "kind": KIND_BRANCH, "pos_profile": profile})

	if sides.factory and (actor.is_production or actor.is_manager):
		if not any(s["warehouse"] == sides.factory for s in my_sides):
			my_sides.append(
				{"warehouse": sides.factory, "label": FACTORY_LABEL, "kind": KIND_FACTORY, "pos_profile": None}
			)

	counterparts: list[dict[str, Any]] = []
	if sides.factory:
		counterparts.append({"warehouse": sides.factory, "label": FACTORY_LABEL, "kind": KIND_FACTORY})
	for warehouse in sides.branch_warehouses():
		counterparts.append({"warehouse": warehouse, "label": sides.label(warehouse), "kind": KIND_BRANCH})

	return {"my_sides": my_sides, "counterparts": counterparts, "can_manage": actor.is_manager}


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


def list_items(my_warehouse: str, counterpart_warehouse: str, search: str | None = None) -> dict[str, Any]:
	actor = _build_actor()
	my_warehouse, counterpart = _validate_pair(actor, my_warehouse, counterpart_warehouse)
	if not actor.can_act_for(my_warehouse):
		_refuse_side(actor, my_warehouse, _("record a stock handover"))
	codes = _allowed_item_codes(my_warehouse, counterpart, actor.sides)
	return {"items": _item_rows(codes, search)}


def declare(
	my_warehouse: str,
	counterpart_warehouse: str,
	direction: str,
	lines: Any,
	pos_profile: str | None = None,
) -> dict[str, Any]:
	actor = _build_actor(pos_profile)
	sides = actor.sides
	my_warehouse, counterpart = _validate_pair(actor, my_warehouse, counterpart_warehouse)

	direction = (direction or "").strip().lower()
	if direction not in ("in", "out"):
		frappe.throw(_("direction must be 'in' (I received) or 'out' (they took / I sent)"))

	used_profile: str | None = None
	if sides.is_factory(my_warehouse) and (actor.is_production or actor.is_manager):
		source = SOURCE_FACTORY if actor.is_production else SOURCE_MANAGER
	elif actor.pos_profile and not actor.is_manager:
		_require_branch_profile(actor, actor.pos_profile, my_warehouse)
		source, used_profile = SOURCE_BRANCH, actor.pos_profile
	elif actor.is_manager:
		if actor.pos_profile and actor.shift_warehouses.get(my_warehouse) == actor.pos_profile:
			source, used_profile = SOURCE_BRANCH, actor.pos_profile
		else:
			source = SOURCE_MANAGER
	elif sides.is_factory(my_warehouse):
		frappe.throw(
			_("Not permitted: only the production team can record a handover for the Factory."),
			frappe.PermissionError,
		)
	else:
		frappe.throw(
			_("Open Stock Transfer from your branch's active shift to record a handover."),
			ShiftRequiredError,
			title=_("Shift Required"),
		)

	merged = _normalize_lines(lines)
	allowed = _allowed_item_codes(my_warehouse, counterpart, sides)
	unknown = sorted(code for code in merged if code not in allowed)
	if unknown:
		frappe.throw(
			_("These items cannot be handed over between {0} and {1}: {2}").format(
				sides.label(my_warehouse), sides.label(counterpart), ", ".join(unknown)
			)
		)

	if direction == "in":
		from_wh, to_wh, declared_side = counterpart, my_warehouse, SIDE_TO
	else:
		from_wh, to_wh, declared_side = my_warehouse, counterpart, SIDE_FROM

	doc = _insert_handover(
		from_warehouse=from_wh,
		to_warehouse=to_wh,
		declared_side=declared_side,
		source=source,
		lines=merged,
		user=actor.user,
		pos_profile=used_profile,
	)
	_enqueue_push(doc.name, EVENT_DECLARED, actor.user)
	return {"ok": True, "handover": _serialize_doc(doc, actor)}


def list_handovers(
	my_warehouse: str | None = None,
	pos_profile: str | None = None,
	limit: Any = 30,
) -> dict[str, Any]:
	actor = _build_actor(pos_profile)
	limit = max(1, min(cint(limit) or 30, 200))
	empty: dict[str, Any] = {"waiting_for_me": [], "waiting_for_them": [], "recent": []}

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

	fields = [
		"name",
		"status",
		"from_warehouse",
		"to_warehouse",
		"awaiting_side",
		"declared_side",
		"declared_by",
		"declared_at",
		"last_proposed_by",
		"last_proposed_at",
		"stock_entry",
		"reject_reason",
		"modified",
	]
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

	rows = list(pending or []) + list(recent or [])
	names = [r.get("name") for r in rows if r.get("name")]
	items_by_parent: dict[str, list[dict[str, Any]]] = {n: [] for n in names}
	if names:
		for item in frappe.get_all(
			ITEM_DOCTYPE,
			filters={"parent": ["in", names], "parenttype": DOCTYPE},
			fields=["parent", "item_code", "item_name", "stock_uom", "qty", "declared_qty"],
			order_by="parent asc, idx asc",
			limit_page_length=0,
		):
			items_by_parent.setdefault(item.get("parent"), []).append(item)
	user_names = _user_names([r.get("declared_by") for r in rows] + [r.get("last_proposed_by") for r in rows])

	out = {"waiting_for_me": [], "waiting_for_them": [], "recent": []}
	for row in pending or []:
		awaiting_wh = _side_warehouse(row, row.get("awaiting_side"))
		in_scope = scope is None or awaiting_wh in scope
		mine = in_scope and actor.can_act_for(awaiting_wh) and row.get("last_proposed_by") != actor.user
		bucket = "waiting_for_me" if mine else "waiting_for_them"
		if len(out[bucket]) < limit:
			out[bucket].append(_serialize(row, items_by_parent.get(row.get("name"), []), actor, user_names))
	for row in recent or []:
		out["recent"].append(_serialize(row, items_by_parent.get(row.get("name"), []), actor, user_names))
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
	doc = _lock(name)

	if doc.status != STATUS_PENDING:
		frappe.throw(_("This handover is already {0}.").format(_(doc.status)))
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
		doc.save(ignore_permissions=True)
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
		doc.save(ignore_permissions=True)
		_enqueue_push(doc.name, EVENT_CHANGED, actor.user)

	return {"ok": True, "handover": _serialize_doc(doc, actor)}


def cancel(name: str, pos_profile: str | None = None) -> dict[str, Any]:
	actor = _build_actor(pos_profile)
	doc = _lock(name)
	if doc.status != STATUS_PENDING:
		frappe.throw(_("This handover is already {0}.").format(_(doc.status)))
	declared_wh = _side_warehouse(doc, doc.declared_side)
	if not actor.can_act_for(declared_wh):
		_refuse_side(actor, declared_wh, _("cancel this handover"))
	if doc.awaiting_side == doc.declared_side or _is_changed(doc, list(doc.items or [])):
		frappe.throw(
			_("The other side has already changed this handover; answer it instead of cancelling."),
		)
	doc.status = STATUS_CANCELLED
	doc.cancelled_by = actor.user
	doc.cancelled_at = _now()
	doc.save(ignore_permissions=True)
	_enqueue_push(doc.name, EVENT_CANCELLED, actor.user)
	return {"ok": True, "handover": _serialize_doc(doc, actor)}


# ---------------------------------------------------------------------------
# Push
# ---------------------------------------------------------------------------


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


def _push_text(doc: Any, event: str, sides: _Sides, actor_name: str | None) -> tuple[str, str]:
	"""(title, body) -- Arabic first, English second."""
	from_label = sides.label(_g(doc, "from_warehouse"))
	to_label = sides.label(_g(doc, "to_warehouse"))
	n = _fmt_qty(sum(flt(_g(r, "qty")) for r in (_g(doc, "items") or [])))
	awaiting = _g(doc, "awaiting_side")
	proposer_label = sides.label(_side_warehouse(doc, _other_side(awaiting)))
	responder_label = sides.label(_side_warehouse(doc, awaiting))
	title = "تسليم مخزون · Stock handover"

	if event == EVENT_DECLARED:
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
		ar = f"{proposer_label} عدّل الكميات ({n} برطمان، {from_label} ← {to_label}) — راجع وأكّد"
		en = f"{proposer_label} changed the quantities ({n} jars, {from_label} → {to_label}) — review"
	elif event == EVENT_CONFIRMED:
		ar = f"{responder_label} أكّد {n} برطمان ({from_label} ← {to_label})"
		en = f"{responder_label} confirmed {n} jars ({from_label} → {to_label})"
	elif event == EVENT_REJECTED:
		ar = f"{responder_label} رفض التسليم ({from_label} ← {to_label})"
		en = f"{responder_label} rejected the handover ({from_label} → {to_label})"
		reason = str(_g(doc, "reject_reason") or "").strip()
		if reason:
			en = f"{en}: {reason[:120]}"
	elif event == EVENT_CANCELLED:
		ar = f"{proposer_label} ألغى التسليم ({from_label} ← {to_label})"
		en = f"{proposer_label} cancelled the handover ({from_label} → {to_label})"
	else:  # reminder
		ar = f"مستني تأكيدك من أكتر من {REMIND_AFTER_HOURS} ساعة: {n} برطمان ({from_label} ← {to_label})"
		en = f"Waiting for your answer for over {REMIND_AFTER_HOURS}h: {n} jars ({from_label} → {to_label})"
	return title, f"{ar}\n{en}"


def _notify_side(doc: Any, event: str) -> str | None:
	"""The side a push for *event* goes to."""
	awaiting = _g(doc, "awaiting_side")
	if event in (EVENT_CONFIRMED, EVENT_REJECTED):
		# The responder is the awaiting side; tell the proposer.
		return _side_warehouse(doc, _other_side(awaiting))
	return _side_warehouse(doc, awaiting)


def notify_stock_handover(handover: Any, event: str, actor: str | None = None) -> dict[str, Any]:
	"""Push *event* about *handover* to the side that has to know. Never raises."""
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
		recipients = [u for u in _side_recipients(_notify_side(handover, event), sides) if u != actor]
		result["recipients"] = len(recipients)
		if not recipients:
			result["status"] = "skipped_no_recipients"
			return result

		actor_name = _user_names([actor]).get(actor) if actor else None
		title, body = _push_text(handover, event, sides, actor_name)
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
			vapid_result = {"ok": True, "status": "skipped_no_subscriptions", "success_count": 0, "failure_count": 0}
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


def remind_stale_handovers() -> dict[str, int]:
	"""Hourly: re-push handovers awaiting an answer for over 12h, at most once per 12h. Never raises."""
	summary = {"checked": 0, "sent": 0, "errors": 0}
	try:
		if not _doctype_ready():
			return summary
		cutoff = _now() - timedelta(hours=REMIND_AFTER_HOURS)
		rows = frappe.get_all(
			DOCTYPE,
			filters={"status": STATUS_PENDING, "last_proposed_at": ["<", cutoff]},
			fields=["name", "reminder_sent_at"],
			order_by="last_proposed_at asc",
			limit_page_length=200,
		)
		for row in rows or []:
			summary["checked"] += 1
			sent_at = row.get("reminder_sent_at")
			if sent_at and get_datetime(sent_at) > cutoff:
				continue
			try:
				doc = frappe.get_doc(DOCTYPE, row.get("name"))
				notify_stock_handover(doc, EVENT_REMINDER, None)
				frappe.db.set_value(DOCTYPE, row.get("name"), "reminder_sent_at", _now(), update_modified=False)
				summary["sent"] += 1
			except Exception:
				summary["errors"] += 1
				_safe_log("stock_handover: reminder failed")
		frappe.db.commit()
	except Exception:
		summary["errors"] += 1
		_safe_log("stock_handover: reminder sweep failed")
	return summary
