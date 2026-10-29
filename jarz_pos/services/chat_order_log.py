"""Accuracy log for "Start order" in the chat inbox.

``chat_order.order_from_chat`` returns an AI-read order that staff review in the
POS. This module records every proposal (:func:`record_proposal`) and, a few
hours later, compares it with the Sales Invoice that was actually billed to the
same customer (:func:`score_pending`, daily), so the owner can see how often the
AI read the order right (:func:`accuracy_summary`).

How a bundle is stored on a submitted POS Sales Invoice (``invoice_creation`` /
``bundle_processing``): one parent row (``is_bundle_parent`` = 1, ``item_code`` =
the bundle's ERPNext item, ``bundle_code`` = the Jarz Bundle name, ``qty`` = how
many bundles) and one child row per chosen flavour (``is_bundle_child`` = 1,
``parent_bundle`` = the Jarz Bundle name, ``item_code`` = the flavour, ``qty`` =
flavours per bundle x bundles). Shipping income is a tax row, not an item row.

Comparison unit: a plain item is ``(item_code, qty)``; every bundle of one kind
is ``(bundle item_code, number of bundles, sorted (flavour, qty))``. Two bundles
of the same kind merge into one unit on both sides, because the invoice cannot
tell them apart either.

Everything that touches the database never raises into its caller.
"""

from __future__ import annotations

import datetime as _dt
import json
from collections import Counter
from typing import Any

import frappe

from jarz_pos.services import chat_common as cc

PROPOSAL = "Jarz Chat Order Proposal"
SCORE_HOURS = 6
SCORE_LIMIT = 500
#: Invoice item codes containing one of these are charges, not ordered lines.
NON_LINE_HINTS = ("shipping", "delivery fee", "delivery charge")
JSON_MAX_CHARS = 1000


# ── pure comparison ─────────────────────────────────────────────────────


def _num(value: Any) -> float:
	try:
		number = float(value or 0)
	except (TypeError, ValueError):
		return 0
	return int(number) if number == int(number) else round(number, 3)


def _flavours(counter: Counter) -> tuple:
	return tuple(sorted((code, _num(qty)) for code, qty in counter.items() if _num(qty) > 0))


def _finish(items: Counter, bundles: dict[str, dict]) -> dict[tuple, str]:
	"""Comparable-line key -> readable label."""
	out: dict[tuple, str] = {}
	for code, qty in items.items():
		qty = _num(qty)
		if qty > 0:
			out[("item", code, qty)] = f"{code} x{qty:g}"
	for code, data in bundles.items():
		count = _num(data["count"])
		flavours = _flavours(data["flavours"])
		if count > 0:
			label = ", ".join(f"{c} x{q:g}" for c, q in flavours) or "no flavours"
			out[("bundle", code, count, flavours)] = f"{code} x{count:g} [{label}]"
	return out


def normalise_proposal(lines: Any) -> dict[tuple, str]:
	items: Counter = Counter()
	bundles: dict[str, dict] = {}
	for line in lines or []:
		if not isinstance(line, dict):
			continue
		code = str(line.get("item_code") or "").strip()
		if not code:
			continue
		qty = _num(line.get("qty")) or 1
		if line.get("kind") == "bundle":
			slot = bundles.setdefault(code, {"count": 0, "flavours": Counter()})
			slot["count"] += qty
			for picks in (line.get("selections") or {}).values():
				for pick in picks or []:
					flavour = str(pick.get("item_code") or "").strip()
					if flavour:
						slot["flavours"][flavour] += _num(pick.get("qty")) or 1
		else:
			items[code] += qty
	return _finish(items, bundles)


def normalise_invoice(rows: Any) -> dict[tuple, str]:
	items: Counter = Counter()
	parents: dict[str, dict] = {}
	children: dict[str, Counter] = {}
	for row in rows or []:
		code = str(row.get("item_code") or "").strip()
		qty = _num(row.get("qty"))
		if not code or qty <= 0:
			continue
		if any(hint in code.lower() for hint in NON_LINE_HINTS):
			continue
		if row.get("is_bundle_parent"):
			key = str(row.get("bundle_code") or code)
			slot = parents.setdefault(key, {"item_code": code, "count": 0})
			slot["count"] += qty
		elif row.get("is_bundle_child") or row.get("parent_bundle"):
			key = str(row.get("parent_bundle") or row.get("bundle_code") or "")
			children.setdefault(key, Counter())[code] += qty
		else:
			items[code] += qty
	bundles: dict[str, dict] = {}
	for key, parent in parents.items():
		slot = bundles.setdefault(parent["item_code"], {"count": 0, "flavours": Counter()})
		slot["count"] += parent["count"]
		slot["flavours"].update(children.pop(key, Counter()))
	# Children whose parent row is missing stay visible as plain items.
	for counter in children.values():
		items.update(counter)
	return _finish(items, bundles)


def compare(proposal_lines: Any, invoice_rows: Any) -> dict[str, Any]:
	"""``{lines_billed, lines_matched, exact_match, missing, extra}`` (labels)."""
	proposed = normalise_proposal(proposal_lines)
	billed = normalise_invoice(invoice_rows)
	matched = set(proposed) & set(billed)
	missing = [label for key, label in billed.items() if key not in matched]
	extra = [label for key, label in proposed.items() if key not in matched]
	return {
		"lines_billed": len(billed),
		"lines_matched": len(matched),
		"exact_match": bool(billed) and not missing and not extra,
		"missing": missing,
		"extra": extra,
	}


def summarise(rows: Any) -> dict[str, Any]:
	"""Aggregate proposal rows (dicts of the stored fields) into the headline numbers."""
	rows = list(rows or [])
	with_invoice = [r for r in rows if r.get("sales_invoice") and r.get("scored_at")]
	exact = sum(1 for r in with_invoice if r.get("exact_match"))
	recalls = [
		int(r.get("lines_matched") or 0) / int(r["lines_billed"])
		for r in with_invoice
		if int(r.get("lines_billed") or 0) > 0
	]
	precisions = [
		int(r.get("lines_matched") or 0) / int(r["lines_proposed"])
		for r in with_invoice
		if int(r.get("lines_proposed") or 0) > 0
	]
	n = len(with_invoice)
	return {
		"proposals": len(rows),
		"scored": sum(1 for r in rows if r.get("scored_at")),
		"with_invoice": n,
		"exact_match": exact,
		"exact_match_rate": round(exact / n, 4) if n else None,
		"mean_line_recall": round(sum(recalls) / len(recalls), 4) if recalls else None,
		"mean_line_precision": round(sum(precisions) / len(precisions), 4) if precisions else None,
		"total_cost_usd": round(sum(float(r.get("cost_usd") or 0) for r in rows), 6),
	}


# ── recording ───────────────────────────────────────────────────────────


def record_proposal(
	conversation: str,
	customer: str | None,
	order: dict[str, Any],
	model: str,
	latency_ms: int,
	input_tokens: int,
	output_tokens: int,
) -> str | None:
	"""Store one proposal. Returns the row name, or None (never raises)."""
	try:
		from jarz_pos.services.chat_drafts import cost_usd

		order = order or {}
		doc = frappe.get_doc(
			{
				"doctype": PROPOSAL,
				"conversation": conversation,
				"customer": customer or None,
				"model": str(model or "")[:140],
				"latency_ms": int(latency_ms or 0),
				"input_tokens": int(input_tokens or 0),
				"output_tokens": int(output_tokens or 0),
				"cost_usd": cost_usd(model, input_tokens, output_tokens, 0, 0),
				"proposal_json": json.dumps(order, ensure_ascii=False, default=str),
				"lines_proposed": len(order.get("lines") or []),
				"warnings_count": len(order.get("warnings") or []),
			}
		)
		doc.insert(ignore_permissions=True)
		# The app reads get_order_prefill with GET, and Frappe commits a GET
		# request only when asked to: without this the row is rolled back.
		frappe.local.flags.commit = True
		return doc.name
	except Exception:
		cc.safe_log_error("Chat order proposal log failed")
		return None


# ── scoring ─────────────────────────────────────────────────────────────


def _short_json(labels: list[str]) -> str:
	text = json.dumps(labels, ensure_ascii=False)
	return text if len(text) <= JSON_MAX_CHARS else text[: JSON_MAX_CHARS - 1] + "]"


def _first_invoice(customer: str, after: Any, until: Any) -> str | None:
	rows = frappe.get_all(
		"Sales Invoice",
		filters={
			"customer": customer,
			"docstatus": 1,
			"is_return": 0,
			"creation": ["between", [after, until]],
		},
		fields=["name", "creation"],
		order_by="creation asc",
		limit_page_length=5,
		ignore_permissions=True,
	)
	for row in rows:
		if row.creation > after:
			return row.name
	return None


def score_one(name: str, hours: int = SCORE_HOURS) -> dict[str, Any]:
	"""The scoring fields of one proposal (not yet saved)."""
	row = frappe.db.get_value(PROPOSAL, name, ["customer", "creation", "proposal_json"], as_dict=True)
	values: dict[str, Any] = {"scored_at": frappe.utils.now_datetime()}
	if not row:
		return values
	if not row.customer:
		values["score_note"] = "no customer linked"
		return values
	until = row.creation + _dt.timedelta(hours=hours)
	invoice = _first_invoice(row.customer, row.creation, until)
	if not invoice:
		values["score_note"] = f"no invoice within {hours} h"
		return values
	rows = frappe.get_all(
		"Sales Invoice Item",
		filters={"parent": invoice},
		fields=["item_code", "qty", "is_bundle_parent", "is_bundle_child", "bundle_code", "parent_bundle"],
		order_by="idx asc",
		limit_page_length=500,
		ignore_permissions=True,
	)
	try:
		lines = (json.loads(row.proposal_json or "{}") or {}).get("lines") or []
	except ValueError:
		lines = []
	result = compare(lines, rows)
	values.update(
		{
			"sales_invoice": invoice,
			"lines_billed": result["lines_billed"],
			"lines_matched": result["lines_matched"],
			"exact_match": 1 if result["exact_match"] else 0,
			"missing_json": _short_json(result["missing"]),
			"extra_json": _short_json(result["extra"]),
		}
	)
	return values


def score_pending(hours: int = SCORE_HOURS, limit: int = SCORE_LIMIT) -> dict[str, int]:
	"""Score unscored proposals older than *hours*. Scheduler entry; never raises."""
	result = {"scored": 0, "with_invoice": 0, "failed": 0}
	try:
		hours = int(hours)
		cutoff = frappe.utils.add_to_date(frappe.utils.now_datetime(), hours=-hours)
		names = frappe.get_all(
			PROPOSAL,
			filters={"scored_at": ["is", "not set"], "creation": ["<", cutoff]},
			pluck="name",
			order_by="creation asc",
			limit_page_length=int(limit),
			ignore_permissions=True,
		)
		for name in names:
			try:
				values = score_one(name, hours)
				frappe.db.set_value(PROPOSAL, name, values, update_modified=False)
				result["scored"] += 1
				if values.get("sales_invoice"):
					result["with_invoice"] += 1
			except Exception:
				result["failed"] += 1
				cc.safe_log_error("Chat order proposal scoring failed", frappe.get_traceback())
		frappe.db.commit()  # nosemgrep: frappe-manual-commit -- scheduler job, one commit per batch
	except Exception:
		cc.safe_log_error("Chat order proposal scoring run failed")
	return result


# ── report ──────────────────────────────────────────────────────────────


def accuracy_summary(days: int = 30) -> dict[str, Any]:
	"""Headline accuracy numbers for proposals made in the last *days* days."""
	try:
		days = max(1, min(int(days), 365))
	except (TypeError, ValueError):
		days = 30
	since = frappe.utils.add_to_date(frappe.utils.now_datetime(), days=-days)
	rows = frappe.get_all(
		PROPOSAL,
		filters={"creation": [">=", since]},
		fields=[
			"sales_invoice",
			"scored_at",
			"exact_match",
			"lines_billed",
			"lines_matched",
			"lines_proposed",
			"cost_usd",
		],
		limit_page_length=0,
		ignore_permissions=True,
	)
	return {"days": days, **summarise(rows)}
