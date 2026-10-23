"""Branch Weekly Count: every branch counts everything it holds, once a week.

The contract with the Flutter client is ``.scratch/branch-weekly-count/CONTRACT.md``;
field names and shapes here are part of it.

How it works
------------
* A **week** runs Saturday to Friday (site time, Cairo). One count per branch
  (POS Profile) per week; the Friday is the due date.
* The count is **blind**. The counter gets a sheet of item names and units, no
  quantities, and cannot choose what to count: the submit must carry a qty for
  every sheet item. Only an approver ever sees ``system_qty`` / ``difference``.
* **Who approves.** An approver is Administrator or anyone holding a role in
  ``LINE_MANAGER_TIER | STOCK | ADMIN``. When an approver submits, the count is
  approved on the spot and the Stock Reconciliation is posted. When anyone
  else submits it waits as ``Pending Approval`` and moves no stock until an
  approver *with access to that branch* approves it.
* **Posting.** Both paths post through ``inventory_count._reconcile_counts``,
  with the posting datetime set to ``counted_at`` (the moment the stock was
  counted), so a late approval still lands on the right day.
* **B2B labels.** The customer labels kept at the branch (``Jarz Customer
  Label`` whose ``storage_location`` is the branch warehouse) are counted on
  the same sheet, one section per customer. ``label_lines`` is optional so an
  older client still submits; when it is sent it must cover every label. On
  approval each label's difference against its ledger at ``counted_at`` is
  posted as an ``Adjustment`` movement referencing the count.

Every refusal is a ``frappe.throw`` whose message starts with ``[error_code]``
so the client can map it to its own (Arabic) text.
"""

from __future__ import annotations

import json
import math
import re
from datetime import date, datetime, timedelta
from typing import Any

import frappe
from frappe import _

from jarz_pos.api.inventory_count import (
	_get_active_warehouse_count_profile_name,
	_get_bin_qty_map,
	_get_enabled_profile_item_exceptions,
	_reconcile_counts,
	_resolve_count_item_codes,
)
from jarz_pos.constants import ROLES, STATUS
from jarz_pos.services import label_stock
from jarz_pos.utils.courier_visibility import (
	get_allowed_pos_profiles_for_user,
	user_has_global_profile_access,
)

DOCTYPE = "Branch Weekly Count"

_SETTINGS = "Jarz POS Settings"
_APPROVER_TABLE = "Jarz Weekly Count Approver"
_APPROVER_FIELD = "weekly_count_approvers"

#: Week boundary: Python's ``date.weekday()`` is Monday=0 ... Saturday=5, Sunday=6.
_SATURDAY = 5

#: Section order on the sheet: these first, in this order, then the rest A-Z.
_SIZE_ORDER = ("large", "medium", "small")
_SIZE_WORDS = re.compile(r"\b(large|medium|small)\b", re.IGNORECASE)

_STATE_DUE = "due"
_STATE_PENDING = "pending_approval"
_STATE_DONE = "done"
_STATE_REJECTED = "rejected"

_LIST_LIMIT_MAX = 200

_HEADER_FIELDS = [
	"name",
	"pos_profile",
	"week_start",
	"status",
	"counted_by",
	"counted_by_name",
	"counted_at",
	"approved_by_name",
	"approved_at",
	"rejection_reason",
	"total_items",
	"differences_count",
	"stock_reconciliation",
]


# ── Small helpers ────────────────────────────────────────────────────────────


def _refuse(code: str, message: str, exc: type[Exception] | None = None) -> None:
	"""Raise with the machine-readable ``[code]`` prefix the client maps."""
	frappe.throw(f"[{code}] {message}", exc or frappe.ValidationError)


def _now() -> datetime:
	from frappe.utils import now_datetime

	return now_datetime()


def _today() -> date:
	return _now().date()


def _as_date(value: Any) -> date | None:
	if value is None or value == "":
		return None
	if isinstance(value, datetime):
		return value.date()
	if isinstance(value, date):
		return value
	return date.fromisoformat(str(value)[:10])


def _fmt_date(value: Any) -> str | None:
	parsed = _as_date(value)
	return parsed.isoformat() if parsed else None


def _fmt_dt(value: Any) -> str | None:
	"""``YYYY-MM-DD HH:MM:SS`` (no microseconds), the shape the client and the posting code expect."""
	if value is None or value == "":
		return None
	if isinstance(value, datetime):
		return value.strftime("%Y-%m-%d %H:%M:%S")
	if isinstance(value, date):
		return value.strftime("%Y-%m-%d 00:00:00")
	return str(value).replace("T", " ")[:19]


def week_start_for(day: date) -> date:
	"""The Saturday that starts the Saturday-to-Friday week containing *day*."""
	return day - timedelta(days=(day.weekday() - _SATURDAY) % 7)


def _week_bounds(day: date) -> tuple[date, date]:
	start = week_start_for(day)
	return start, start + timedelta(days=6)


def _approver_roles() -> set[str]:
	return set(ROLES.LINE_MANAGER_TIER) | set(ROLES.STOCK) | set(ROLES.ADMIN)


def listed_count_approvers() -> set[str] | None:
	"""The enabled users on Jarz POS Settings -> Weekly Count Approvers.

	``None`` when the list is empty (or the table is not migrated yet): approval
	then falls back to the roles in ``_approver_roles``. Once anyone is listed,
	the list alone decides, whatever roles other people hold.
	"""
	try:
		users = frappe.get_all(
			_APPROVER_TABLE,
			filters={"parenttype": _SETTINGS, "parentfield": _APPROVER_FIELD},
			pluck="user",
		)
		users = [u for u in users or [] if u]
		if not users:
			return None
		return set(frappe.get_all("User", filters={"name": ["in", users], "enabled": 1}, pluck="name") or [])
	except Exception:
		try:
			frappe.log_error(frappe.get_traceback(), "branch_count_approver_list_failed", defer_insert=True)
		except Exception:
			pass
		return None


def is_count_approver(user: str | None = None) -> bool:
	"""Administrator, or a listed approver (or, with no list, a line-manager-tier, stock or admin role)."""
	user = user or frappe.session.user
	if user == ROLES.ADMINISTRATOR:
		return True
	listed = listed_count_approvers()
	if listed is not None:
		return user in listed
	return bool(set(frappe.get_roles(user) or []) & _approver_roles())


def count_scope(user: str | None = None) -> list[str]:
	"""Branches the caller may count, see and decide on.

	A listed approver covers every enabled branch; anyone else keeps the usual
	POS Profile scope.
	"""
	user = user or frappe.session.user
	listed = listed_count_approvers()
	if listed and user in listed:
		return list(frappe.get_all("POS Profile", filters={"disabled": 0}, pluck="name") or [])
	return get_allowed_pos_profiles_for_user(user)


def count_pending_in_scope(user: str | None = None) -> int:
	"""Pending Approval counts in the caller's branch scope (an empty scope is zero, never all)."""
	scope = count_scope(user)
	if not scope:
		return 0
	return int(
		frappe.db.count(DOCTYPE, filters={"status": STATUS.PENDING_APPROVAL, "pos_profile": ["in", scope]})
		or 0
	)


def _section_rank(group: str | None) -> tuple[int, int, str]:
	"""Large, Medium, Small first, then every other group alphabetically."""
	name = str(group or "").strip().casefold()
	if name in _SIZE_ORDER:
		return (0, _SIZE_ORDER.index(name), "")
	return (1, 0, name)


def _flavour_key(item_name: str | None) -> str:
	"""Item name without its size word, so flavours line up across sections."""
	return " ".join(_SIZE_WORDS.sub(" ", str(item_name or "")).split()).casefold()


def _full_name(user: str) -> str:
	try:
		return str(frappe.db.get_value("User", user, "full_name") or user)
	except Exception:
		return user


# ── Branch and count lookups ─────────────────────────────────────────────────


def _branch_rows(profiles: list[str]) -> list[dict[str, Any]]:
	"""Enabled POS Profiles (from *profiles*) that have a warehouse, by name."""
	if not profiles:
		return []
	rows = frappe.get_all(
		"POS Profile",
		filters={"name": ["in", list(profiles)], "disabled": 0},
		fields=["name", "warehouse", "company"],
		order_by="name asc",
	)
	return [r for r in rows or [] if r.get("warehouse")]


def _member_profiles(user: str, profiles: list[str]) -> set[str]:
	"""Which of *profiles* the user is actually a POS Profile User of."""
	if not profiles:
		return set()
	if not user_has_global_profile_access(user) and user not in (listed_count_approvers() or ()):
		# Their allowed list *is* their memberships.
		return set(profiles)
	return set(
		frappe.get_all(
			"POS Profile User",
			filters={"user": user, "parent": ["in", list(profiles)]},
			pluck="parent",
		)
		or []
	)


def _latest_counts(profiles: list[str], week_start: date) -> dict[str, dict[str, Any]]:
	"""This week's latest count per branch (a Rejected one only counts if nothing newer exists)."""
	if not profiles:
		return {}
	rows = frappe.get_all(
		DOCTYPE,
		filters={"pos_profile": ["in", list(profiles)], "week_start": week_start.isoformat()},
		fields=[
			"name",
			"pos_profile",
			"status",
			"counted_by",
			"counted_by_name",
			"counted_at",
			"approved_by_name",
			"rejection_reason",
		],
		order_by="creation desc",
	)
	latest: dict[str, dict[str, Any]] = {}
	for row in rows or []:
		latest.setdefault(row.get("pos_profile"), row)
	return latest


def _profiles_approved_in_week(profiles: list[str], week_start: date) -> set[str]:
	if not profiles:
		return set()
	return set(
		frappe.get_all(
			DOCTYPE,
			filters={
				"pos_profile": ["in", list(profiles)],
				"week_start": week_start.isoformat(),
				"status": STATUS.APPROVED,
			},
			pluck="pos_profile",
		)
		or []
	)


def _counting_live_by(week_start: date) -> bool:
	"""Had any branch counted in or before the week starting *week_start*?

	``missed_last_week`` paints the home banner red. Without this guard every
	branch would show red in the launch week, for a count nobody could have
	done because the feature did not exist yet.
	"""
	return bool(frappe.db.exists(DOCTYPE, {"week_start": ["<=", week_start.isoformat()]}))


def _state_of(count: dict[str, Any] | None) -> str:
	status = (count or {}).get("status")
	if status == STATUS.PENDING_APPROVAL:
		return _STATE_PENDING
	if status == STATUS.APPROVED:
		return _STATE_DONE
	if status == STATUS.REJECTED:
		return _STATE_REJECTED
	return _STATE_DUE


def _count_summary(count: dict[str, Any] | None) -> dict[str, Any] | None:
	if not count:
		return None
	return {
		"name": count.get("name"),
		"status": count.get("status"),
		"counted_by": count.get("counted_by"),
		"counted_by_name": count.get("counted_by_name"),
		"counted_at": _fmt_dt(count.get("counted_at")),
		"approved_by_name": count.get("approved_by_name"),
		"rejection_reason": count.get("rejection_reason"),
	}


def _require_branch(pos_profile: str, user: str | None = None) -> dict[str, Any]:
	"""The POS Profile row (name, warehouse, company) if the caller may use it, else a refusal."""
	profile = str(pos_profile or "").strip()
	if not profile or profile not in set(count_scope(user)):
		_refuse("no_access", _("You are not allowed to count for this branch"), frappe.PermissionError)
	row = frappe.db.get_value("POS Profile", profile, ["name", "warehouse", "company"], as_dict=True)
	if not row or not row.get("warehouse"):
		_refuse("no_warehouse", _("This branch has no warehouse"))
	return row


# ── The sheet ────────────────────────────────────────────────────────────────


def _excluded_item_codes(warehouse: str) -> set[str]:
	"""Items the warehouse's Count Profile explicitly excludes."""
	profile_name = _get_active_warehouse_count_profile_name(warehouse)
	if not profile_name:
		return set()
	profile_doc = frappe.get_cached_doc("Warehouse Count Profile", profile_name)
	return set(_get_enabled_profile_item_exceptions(profile_doc)[1])


def _sheet_items(warehouse: str) -> list[dict[str, Any]]:
	"""Everything the branch must count.

	The Warehouse Count Profile's items plus every item with a Bin row in the
	warehouse, limited to live stock items (not templates), minus the profile's
	Exclude exceptions.
	"""
	codes = set(_resolve_count_item_codes(warehouse) or [])
	codes.update(
		c for c in (frappe.get_all("Bin", filters={"warehouse": warehouse}, pluck="item_code") or []) if c
	)
	codes.difference_update(_excluded_item_codes(warehouse))
	if not codes:
		return []
	rows = frappe.get_all(
		"Item",
		filters={
			"name": ["in", sorted(codes)],
			"is_stock_item": 1,
			"disabled": 0,
			"has_variants": 0,
		},
		fields=["name", "item_name", "item_group", "stock_uom", "image"],
	)
	return [
		{
			"item_code": r.get("name"),
			"item_name": r.get("item_name") or r.get("name"),
			"item_group": r.get("item_group") or "",
			"stock_uom": r.get("stock_uom"),
			"image": r.get("image") or None,
		}
		for r in rows or []
	]


def _build_sections(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
	by_group: dict[str, list[dict[str, Any]]] = {}
	for item in items:
		by_group.setdefault(item["item_group"], []).append(item)
	sections = []
	for group in sorted(by_group, key=_section_rank):
		ordered = sorted(
			by_group[group],
			key=lambda i: (_flavour_key(i["item_name"]), str(i["item_code"])),
		)
		sections.append(
			{
				"key": group,
				"title": group,
				"items": [
					{
						"item_code": i["item_code"],
						"item_name": i["item_name"],
						"stock_uom": i["stock_uom"],
						"image": i["image"],
					}
					for i in ordered
				],
			}
		)
	return sections


def _sheet_labels(warehouse: str) -> list[dict[str, Any]]:
	"""Enabled B2B customer labels stored at *warehouse*."""
	if not warehouse or not frappe.db.exists("DocType", label_stock.LABEL_DOCTYPE):
		return []
	rows = frappe.get_all(
		label_stock.LABEL_DOCTYPE,
		filters={"storage_location": warehouse, "enabled": 1},
		fields=["name", "customer", "customer_name", "label_title", "item", "size"],
		limit_page_length=0,
	)
	codes = sorted({r.get("item") for r in rows or [] if r.get("item")})
	items: dict[str, Any] = {}
	if codes:
		items = {
			i["name"]: i
			for i in frappe.get_all(
				"Item", filters={"name": ["in", codes]}, fields=["name", "item_name", "item_group", "image"]
			)
			or []
		}
	out = []
	for r in rows or []:
		item = items.get(r.get("item")) or {}
		item_name = item.get("item_name") or r.get("item") or ""
		out.append(
			{
				"label": r.get("name"),
				"customer": r.get("customer") or "",
				"customer_name": r.get("customer_name") or r.get("customer") or "",
				"title": r.get("label_title") or item_name or r.get("name"),
				"item_code": r.get("item") or None,
				"item_name": item_name,
				"size": r.get("size") or item.get("item_group") or "",
				"image": item.get("image") or None,
			}
		)
	return out


def _label_sort_key(label: dict[str, Any]) -> tuple:
	return (_section_rank(label.get("size")), _flavour_key(label.get("item_name")), str(label.get("label")))


def _build_label_sections(labels: list[dict[str, Any]]) -> list[dict[str, Any]]:
	"""One section per customer, customers A-Z, labels by size then flavour."""
	by_customer: dict[str, list[dict[str, Any]]] = {}
	for label in labels:
		by_customer.setdefault(label["customer"], []).append(label)
	sections = []
	for customer in sorted(by_customer, key=lambda c: (by_customer[c][0]["customer_name"].casefold(), c)):
		ordered = sorted(by_customer[customer], key=_label_sort_key)
		sections.append(
			{
				"key": f"label:{customer}",
				"customer": customer,
				"title": ordered[0]["customer_name"],
				"labels": [
					{
						"label": lb["label"],
						"title": lb["title"],
						"item_code": lb["item_code"],
						"item_name": lb["item_name"],
						"size": lb["size"],
						"image": lb["image"],
					}
					for lb in ordered
				],
			}
		)
	return sections


def _label_on_hand_at(label: str, at: Any) -> int:
	"""A label's ledger quantity as it stood at *at* (movements created up to then)."""
	rows = frappe.get_all(
		label_stock.MOVEMENT_DOCTYPE,
		filters={"label": label, "creation": ["<=", at]},
		pluck="qty",
		limit_page_length=0,
	)
	return int(sum(int(q or 0) for q in rows or []))


# Movements that set a label's position from a physical count.
_RECOUNT_TYPES = ["Adjustment", "Opening"]


def _label_recounted_after(label: str, at: Any) -> bool:
	"""True when a newer count (or an opening) was posted for *label* after *at*."""
	return bool(
		frappe.get_all(
			label_stock.MOVEMENT_DOCTYPE,
			filters={"label": label, "movement_type": ["in", _RECOUNT_TYPES], "creation": [">", at]},
			pluck="name",
			limit_page_length=1,
		)
	)


def _post_label_counts(doc) -> None:
	"""Post each counted label's difference against its ledger at ``counted_at``.

	Movements after the count (sales consuming labels while the count waited
	for approval) stay in the ledger: only what the count found different at
	that moment is adjusted. A label adjustment is a delta, not an absolute
	set, so a label recounted after this count (a newer weekly count or a
	manual count) is skipped: stacking this older delta on it would
	double-correct.
	"""
	cut = doc.counted_at
	posting_date = (_fmt_dt(cut) or "").split(" ")[0] or None
	for row in doc.get("labels") or []:
		label_stock._lock_label(row.label)
		if _label_recounted_after(row.label, cut):
			continue
		counted = int(row.counted_qty or 0)
		before = _label_on_hand_at(row.label, cut)
		delta = counted - before
		if delta:
			movement = label_stock.post_movement(
				label=row.label,
				movement_type="Adjustment",
				qty=delta,
				posting_date=posting_date,
				reference_doctype=DOCTYPE,
				reference_name=doc.name,
				remarks=f"Branch weekly count {doc.name}: {counted} (was {before})",
			)
			if movement:
				row.db_set("movement", movement, update_modified=False)
		frappe.db.set_value(
			label_stock.LABEL_DOCTYPE, row.label, "last_counted_on", posting_date, update_modified=False
		)
		if not delta:
			label_stock.refresh_label(row.label)


# ── Reads ────────────────────────────────────────────────────────────────────


@frappe.whitelist(methods=["GET", "POST"])
def get_weekly_count_status() -> dict[str, Any]:
	"""Where each of the caller's branches stands this week (drives the home banner)."""
	user = frappe.session.user
	today = _today()
	week_start, due_date = _week_bounds(today)

	approver = is_count_approver(user)
	rows = _branch_rows(count_scope(user))
	names = [r["name"] for r in rows]
	members = _member_profiles(user, names)
	this_week = _latest_counts(names, week_start)
	last_week_start = week_start - timedelta(days=7)
	approved_last_week = _profiles_approved_in_week(names, last_week_start)
	counting_was_live = _counting_live_by(last_week_start)

	branches = []
	for row in rows:
		count = this_week.get(row["name"])
		state = _state_of(count)
		branches.append(
			{
				"pos_profile": row["name"],
				"warehouse": row["warehouse"],
				"is_member": row["name"] in members,
				"state": state,
				"last_day": today == due_date and state in (_STATE_DUE, _STATE_REJECTED),
				"missed_last_week": counting_was_live and row["name"] not in approved_last_week,
				"count": _count_summary(count),
			}
		)

	return {
		"week_start": week_start.isoformat(),
		"due_date": due_date.isoformat(),
		"today": today.isoformat(),
		"days_left": (due_date - today).days,
		"is_approver": approver,
		"pending_approval_count": count_pending_in_scope(user) if approver else 0,
		"branches": branches,
	}


@frappe.whitelist(methods=["GET", "POST"])
def get_weekly_count_sheet(pos_profile: str) -> dict[str, Any]:
	"""The blind count sheet for a branch: item names and units, never quantities."""
	user = frappe.session.user
	branch = _require_branch(pos_profile, user)
	today = _today()
	week_start, due_date = _week_bounds(today)

	items = _sheet_items(branch["warehouse"])
	labels = _sheet_labels(branch["warehouse"])
	count = _latest_counts([branch["name"]], week_start).get(branch["name"])
	return {
		"pos_profile": branch["name"],
		"warehouse": branch["warehouse"],
		"week_start": week_start.isoformat(),
		"due_date": due_date.isoformat(),
		"is_approver": is_count_approver(user),
		"state": _state_of(count),
		"count": _count_summary(count),
		"total_items": len(items),
		"sections": _build_sections(items),
		"total_labels": len(labels),
		"label_sections": _build_label_sections(labels),
	}


def _header(row: dict[str, Any], approver: bool) -> dict[str, Any]:
	out = {key: row.get(key) for key in _HEADER_FIELDS}
	out["week_start"] = _fmt_date(row.get("week_start"))
	out["counted_at"] = _fmt_dt(row.get("counted_at"))
	out["approved_at"] = _fmt_dt(row.get("approved_at"))
	out["total_items"] = int(row.get("total_items") or 0)
	# Blind count: even the number of mismatches tells a counter how they did.
	out["differences_count"] = int(row.get("differences_count") or 0) if approver else None
	return out


@frappe.whitelist(methods=["GET", "POST"])
def list_weekly_counts(status: str | None = None, pos_profile: str | None = None, limit: int = 50) -> dict:
	"""Counts in the caller's branch scope, newest first (a non-approver sees only their own)."""
	user = frappe.session.user
	approver = is_count_approver(user)
	scope = count_scope(user)
	if pos_profile:
		scope = [p for p in scope if p == pos_profile]
	if not scope:
		return {"counts": []}

	filters: dict[str, Any] = {"pos_profile": ["in", scope]}
	if status:
		filters["status"] = status
	if not approver:
		filters["counted_by"] = user
	try:
		limit = max(1, min(int(limit), _LIST_LIMIT_MAX))
	except (TypeError, ValueError):
		limit = 50

	rows = frappe.get_all(
		DOCTYPE,
		filters=filters,
		fields=_HEADER_FIELDS + ["approved_at"],
		order_by="creation desc",
		limit=limit,
	)
	return {"counts": [_header(r, approver) for r in rows or []]}


def _load_visible_count(name: str, approver: bool, user: str):
	"""The count document if the caller may see it, else a refusal."""
	if not name or not frappe.db.exists(DOCTYPE, name):
		_refuse("not_found", _("Count not found"), frappe.DoesNotExistError)
	doc = frappe.get_doc(DOCTYPE, name)
	scope = set(count_scope(user))
	visible = doc.pos_profile in scope and (approver or doc.counted_by == user)
	if not visible:
		_refuse("no_access", _("You are not allowed to view this count"), frappe.PermissionError)
	return doc


@frappe.whitelist(methods=["GET", "POST"])
def get_weekly_count(name: str) -> dict[str, Any]:
	"""One count with its lines. Quantities the system holds are shown to approvers only."""
	user = frappe.session.user
	approver = is_count_approver(user)
	doc = _load_visible_count(name, approver, user)

	lines = []
	for row in doc.items or []:
		lines.append(
			{
				"item_code": row.item_code,
				"item_name": row.item_name,
				"item_group": row.item_group,
				"stock_uom": row.stock_uom,
				"counted_qty": row.counted_qty,
				"system_qty": row.system_qty if approver else None,
				"difference": row.difference if approver else None,
			}
		)
	if approver:
		lines.sort(
			key=lambda ln: (
				-abs(float(ln["difference"] or 0)),
				_section_rank(ln["item_group"]),
				_flavour_key(ln["item_name"]),
			)
		)
	else:
		lines.sort(key=lambda ln: (_section_rank(ln["item_group"]), _flavour_key(ln["item_name"])))

	label_lines = [
		{
			"label": row.label,
			"customer": row.customer,
			"customer_name": row.customer_name,
			"item_code": row.item_code,
			"item_name": row.item_name,
			"counted_qty": row.counted_qty,
			"system_qty": row.system_qty if approver else None,
			"difference": row.difference if approver else None,
		}
		for row in doc.get("labels") or []
	]
	label_lines.sort(
		key=lambda ln: (
			str(ln["customer_name"] or "").casefold(),
			-abs(int(ln["difference"] or 0)) if approver else 0,
			_flavour_key(ln["item_name"]),
		)
	)

	result = _header({key: doc.get(key) for key in _HEADER_FIELDS + ["approved_at"]}, approver)
	result["can_approve"] = bool(
		approver
		and doc.status == STATUS.PENDING_APPROVAL
		and doc.counted_by != user
		and doc.pos_profile in set(count_scope(user))
	)
	result["lines"] = lines
	result["label_lines"] = label_lines
	return result


# ── Submit ───────────────────────────────────────────────────────────────────


def _parse_lines(lines: Any) -> dict[str, float]:
	"""``[{"item_code", "qty"}, ...]`` (JSON string or list) -> ``{item_code: qty}``."""
	if isinstance(lines, str):
		try:
			lines = json.loads(lines or "[]")
		except ValueError:
			_refuse("invalid_qty", _("The count lines could not be read"))
	if not isinstance(lines, list):
		_refuse("invalid_qty", _("The count lines must be a list"))

	counted: dict[str, float] = {}
	for line in lines:
		if not isinstance(line, dict):
			_refuse("invalid_qty", _("Each count line must be an object"))
		code = str(line.get("item_code") or "").strip()
		if not code:
			_refuse("unknown_items", _("A count line has no item_code"))
		raw = line.get("qty")
		if raw is None:
			raw = line.get("counted_qty")
		try:
			if isinstance(raw, bool) or raw is None or str(raw).strip() == "":
				raise ValueError
			qty = float(raw)
		except (TypeError, ValueError):
			_refuse("invalid_qty", _("Quantity for {0} is not a number").format(code))
		if not math.isfinite(qty) or qty < 0:
			_refuse("invalid_qty", _("Quantity for {0} must be zero or more").format(code))
		if code in counted:
			_refuse("invalid_qty", _("{0} appears twice in the count").format(code))
		counted[code] = qty
	return counted


_MAX_LABEL_QTY = 1_000_000  # the count fields are 32-bit Int; refuse a typo instead of a 500


def _parse_label_lines(label_lines: Any) -> dict[str, int] | None:
	"""``[{"label", "qty"}, ...]`` -> ``{label: qty}``; None when the client sent none."""
	if label_lines is None:
		return None
	if isinstance(label_lines, str):
		if not label_lines.strip():
			return None
		try:
			label_lines = json.loads(label_lines)
		except ValueError:
			_refuse("invalid_qty", _("The label count lines could not be read"))
	if not isinstance(label_lines, list):
		_refuse("invalid_qty", _("The label count lines must be a list"))

	counted: dict[str, int] = {}
	for line in label_lines:
		if not isinstance(line, dict):
			_refuse("invalid_qty", _("Each label count line must be an object"))
		name = str(line.get("label") or "").strip()
		if not name:
			_refuse("unknown_items", _("A label count line has no label"))
		raw = line.get("qty")
		if raw is None:
			raw = line.get("counted_qty")
		try:
			if isinstance(raw, bool) or raw is None or str(raw).strip() == "":
				raise ValueError
			qty = float(raw)
		except (TypeError, ValueError):
			_refuse("invalid_qty", _("Quantity for label {0} is not a number").format(name))
		if not math.isfinite(qty) or qty < 0 or qty != int(qty) or qty > _MAX_LABEL_QTY:
			_refuse("invalid_qty", _("Quantity for label {0} must be a whole number, zero or more").format(name))
		if name in counted:
			_refuse("invalid_qty", _("Label {0} appears twice in the count").format(name))
		counted[name] = int(qty)
	return counted


def _submit_response(row: dict[str, Any], approver: bool) -> dict[str, Any]:
	return {
		"ok": True,
		"name": row.get("name"),
		"status": row.get("status"),
		"auto_approved": bool(row.get("auto_approved")),
		"stock_reconciliation": row.get("stock_reconciliation") or None,
		"differences_count": int(row.get("differences_count") or 0) if approver else None,
		"total_items": int(row.get("total_items") or 0),
	}


def _enqueue_push(method: str, count_name: str, context: str) -> bool:
	"""Queue a push for after commit. Never raises: a count must not fail over a notification."""
	try:
		from jarz_pos.api.notifications import outbound_alerts_suppressed

		if outbound_alerts_suppressed(context):
			return False
		frappe.enqueue(
			f"jarz_pos.api.notifications.{method}",
			queue="short",
			enqueue_after_commit=True,
			count_name=count_name,
		)
		return True
	except Exception:
		try:
			frappe.log_error(frappe.get_traceback(), "branch_count_push_enqueue_failed", defer_insert=True)
		except Exception:
			pass
		return False


@frappe.whitelist(methods=["POST"])
def submit_weekly_count(
	pos_profile: str,
	lines: Any,
	client_request_id: str | None = None,
	label_lines: Any = None,
) -> dict[str, Any]:
	"""Record this week's count for a branch.

	An approver (see ``is_count_approver``) posts immediately (auto-approved);
	anyone else leaves it ``Pending Approval`` and the approvers are pushed.
	``label_lines`` (optional) carries the B2B label counts; when present it
	must cover every label stored at the branch.
	"""
	user = frappe.session.user
	branch = _require_branch(pos_profile, user)
	approver = is_count_approver(user)
	warehouse = branch["warehouse"]
	week_start, _due = _week_bounds(_today())
	crid = str(client_request_id or "").strip() or None

	# Serialise submits per branch: two phones double-tapping, or a retry racing
	# its original, queue here and the second one sees the first one's row.
	frappe.db.sql("SELECT name FROM `tabPOS Profile` WHERE name = %s FOR UPDATE", (branch["name"],))

	if crid:
		# A Rejected count is never "this submit again": a draft whose first
		# submit was rejected must record a new count, not echo the old one.
		earlier = frappe.db.get_value(
			DOCTYPE,
			{"client_request_id": crid, "counted_by": user, "status": ["!=", STATUS.REJECTED]},
			["name", "status", "auto_approved", "stock_reconciliation", "differences_count", "total_items"],
			as_dict=True,
		)
		if earlier:
			return _submit_response(earlier, approver)

	existing = frappe.get_all(
		DOCTYPE,
		filters={
			"pos_profile": branch["name"],
			"week_start": week_start.isoformat(),
			"status": ["in", [STATUS.PENDING_APPROVAL, STATUS.APPROVED]],
		},
		fields=["name", "status"],
		limit=1,
	)
	if existing:
		if existing[0].get("status") == STATUS.APPROVED:
			_refuse("already_done", _("This branch's count for the week is already done"))
		_refuse("already_pending", _("This branch's count is already waiting for approval"))

	counted = _parse_lines(lines)
	sheet = {item["item_code"]: item for item in _sheet_items(warehouse)}
	if not sheet:
		_refuse("no_items", _("This branch has no items to count"))
	unknown = sorted(code for code in counted if code not in sheet)
	if unknown:
		_refuse("unknown_items", _("Not on this branch's count sheet: {0}").format(", ".join(unknown)))
	missing = [sheet[code]["item_name"] for code in sheet if code not in counted]
	if missing:
		_refuse("missing_items", _("Count every item. Missing: {0}").format(", ".join(sorted(missing))))

	label_counted = _parse_label_lines(label_lines)
	label_rows = []
	if label_counted is not None:
		label_sheet = {lb["label"]: lb for lb in _sheet_labels(warehouse)}
		unknown = sorted(name for name in label_counted if name not in label_sheet)
		if unknown:
			_refuse("unknown_items", _("Not on this branch's label sheet: {0}").format(", ".join(unknown)))
		missing = [
			f"{lb['customer_name']} - {lb['item_name']}" for name, lb in label_sheet.items() if name not in label_counted
		]
		if missing:
			_refuse("missing_items", _("Count every label. Missing: {0}").format(", ".join(sorted(missing))))
		ordered = sorted(label_sheet.items(), key=lambda kv: (kv[1]["customer_name"].casefold(), _label_sort_key(kv[1])))
		for name, lb in ordered:
			system = int(label_stock.get_on_hand(name))
			label_rows.append(
				{
					"label": name,
					"customer": lb["customer"] or None,
					"customer_name": lb["customer_name"],
					"item_code": lb["item_code"],
					"item_name": lb["item_name"],
					"counted_qty": label_counted[name],
					"system_qty": system,
					"difference": label_counted[name] - system,
				}
			)

	system_qty = _get_bin_qty_map(warehouse, list(sheet))
	rows = []
	differences = sum(1 for r in label_rows if r["difference"])
	for code, item in sheet.items():
		system = float(system_qty.get(code, 0.0))
		difference = counted[code] - system
		if abs(difference) > 1e-9:
			differences += 1
		rows.append(
			{
				"item_code": code,
				"item_name": item["item_name"],
				"item_group": item["item_group"],
				"stock_uom": item["stock_uom"],
				"counted_qty": counted[code],
				"system_qty": system,
				"difference": difference,
			}
		)

	now = _now()
	counted_at = _fmt_dt(now)
	values: dict[str, Any] = {
		"doctype": DOCTYPE,
		"pos_profile": branch["name"],
		"warehouse": warehouse,
		"company": branch.get("company"),
		"week_start": week_start.isoformat(),
		"status": STATUS.APPROVED if approver else STATUS.PENDING_APPROVAL,
		"counted_by": user,
		"counted_by_name": _full_name(user),
		"counted_at": counted_at,
		"total_items": len(rows),
		"differences_count": differences,
		"client_request_id": crid,
		"auto_approved": 1 if approver else 0,
		"items": rows,
		"labels": label_rows,
	}

	if approver:
		posted = _reconcile_counts(
			warehouse,
			counted_at,
			[{"item_code": r["item_code"], "qty": r["counted_qty"]} for r in rows],
			qty_as_of_posting=True,
		)
		if not posted.get("ok", True):
			_refuse("post_failed", str(posted.get("error") or _("The stock could not be updated")))
		values.update(
			{
				"approved_by": user,
				"approved_by_name": values["counted_by_name"],
				"approved_at": counted_at,
				"stock_reconciliation": posted.get("stock_reconciliation"),
			}
		)

	doc = frappe.get_doc(values)
	doc.insert(ignore_permissions=True)
	if approver:
		_post_label_counts(doc)
	else:
		_enqueue_push("notify_branch_count_approval_required", doc.name, "branch_count_approval")
	frappe.db.commit()
	return _submit_response(
		{
			"name": doc.name,
			"status": values["status"],
			"auto_approved": values["auto_approved"],
			"stock_reconciliation": values.get("stock_reconciliation"),
			"differences_count": differences,
			"total_items": len(rows),
		},
		approver,
	)


# ── Decide ───────────────────────────────────────────────────────────────────


def _load_pending_for_decision(name: str, user: str):
	"""Row-locked count the caller may decide on, else a refusal."""
	if not is_count_approver(user):
		_refuse("no_access", _("Only a weekly count approver can decide on a count"), frappe.PermissionError)
	if not name or not frappe.db.exists(DOCTYPE, name):
		_refuse("not_found", _("Count not found"), frappe.DoesNotExistError)
	doc = frappe.get_doc(DOCTYPE, name, for_update=True)
	if doc.pos_profile not in set(count_scope(user)):
		_refuse("no_access", _("You do not have access to this branch"), frappe.PermissionError)
	if doc.status != STATUS.PENDING_APPROVAL:
		_refuse("not_pending", _("This count is not waiting for approval"))
	if doc.counted_by == user:
		_refuse("own_count", _("Someone else must decide on your own count"), frappe.PermissionError)
	return doc


@frappe.whitelist(methods=["POST"])
def approve_weekly_count(name: str) -> dict[str, Any]:
	"""Approve a pending count: post its Stock Reconciliation at the moment it was counted."""
	user = frappe.session.user
	doc = _load_pending_for_decision(name, user)

	# Judged against the stock at counted_at, not today's Bin: sales since the
	# count must neither hide a shortage nor fake a change.
	posted = _reconcile_counts(
		doc.warehouse,
		_fmt_dt(doc.counted_at),
		[{"item_code": r.item_code, "qty": r.counted_qty} for r in doc.items or []],
		qty_as_of_posting=True,
	)
	if not posted.get("ok", True):
		_refuse("post_failed", str(posted.get("error") or _("The stock could not be updated")))
	_post_label_counts(doc)

	doc.status = STATUS.APPROVED
	doc.approved_by = user
	doc.approved_by_name = _full_name(user)
	doc.approved_at = _fmt_dt(_now())
	doc.stock_reconciliation = posted.get("stock_reconciliation")
	doc.save(ignore_permissions=True)
	frappe.db.commit()
	return {
		"ok": True,
		"name": doc.name,
		"status": STATUS.APPROVED,
		"stock_reconciliation": doc.stock_reconciliation or None,
	}


@frappe.whitelist(methods=["POST"])
def reject_weekly_count(name: str, reason: str) -> dict[str, Any]:
	"""Send a pending count back: the branch stays due and the counter is told to recount."""
	user = frappe.session.user
	if not is_count_approver(user):
		_refuse("no_access", _("Only a weekly count approver can decide on a count"), frappe.PermissionError)
	reason = str(reason or "").strip()
	if not reason:
		_refuse("reason_required", _("Say why the count is rejected"))
	doc = _load_pending_for_decision(name, user)

	doc.status = STATUS.REJECTED
	doc.rejection_reason = reason
	doc.save(ignore_permissions=True)
	_enqueue_push("notify_branch_count_rejected", doc.name, "branch_count_rejected")
	frappe.db.commit()
	return {"ok": True, "name": doc.name, "status": STATUS.REJECTED}
