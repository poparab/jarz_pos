"""Flavour availability for the AI chat menu, and the moderators' override of it.

The AI menu pauses a flavour when every branch together, plus the jars in
transit to them, holds fewer than ``chat_drafts.MENU_MIN_STOCK`` jars (a
branch's negative bin counts as zero). The system's stock still lags reality
(jars already in a branch but not moved in the system), so a
moderator may force a flavour **Available** (offered whatever the stock) or
**Paused** (never offered). Clearing the override (``auto``) goes back to the
stock. Overrides live in the ``flavour_overrides`` table of Jarz Chat Settings
and apply to the draft menu and to chat orders alike.
"""

from __future__ import annotations

from typing import Any

import frappe
from frappe import _
from frappe.utils import now_datetime

from jarz_pos.services import chat_drafts as d
from jarz_pos.services import chat_settings as cs

_PARENT = "Jarz Chat Settings"
_FIELD = "flavour_overrides"
_AUTO = "auto"
_CHOICES = {_AUTO: None, "available": d.AVAILABLE, "paused": d.PAUSED}


def describe(
	products: list[dict[str, Any]],
	stock: dict[str, float],
	overrides: dict[str, str],
	arabic_names: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
	"""One row per priced flavour, catalog order. Pure.

	``{item_code, name, name_ar, size, size_ar, stock, auto, override, effective}``:
	*auto* is the stock verdict, *override* ``auto|available|paused``, *effective*
	what the AI uses. ``stock`` is ``None`` for a non-stock item."""
	priced = []
	for item in products:
		try:
			if float(item.get("price") or 0) > 0 and item.get("id"):
				priced.append(item)
		except (TypeError, ValueError):
			continue
	available, _paused = d.split_by_stock(priced, stock)
	by_stock = {id(i) for i in available}
	names = arabic_names or {}
	rows = []
	for item in priced:
		code = str(item["id"])
		group = str(item.get("item_group") or "")
		flavour = d._flavour(str(item.get("name") or code), group)
		auto = "available" if id(item) in by_stock else "paused"
		forced = overrides.get(code)
		override = "available" if forced == d.AVAILABLE else "paused" if forced == d.PAUSED else _AUTO
		qty = stock.get(code)
		rows.append(
			{
				"item_code": code,
				"name": flavour,
				"name_ar": d.flavour_ar(flavour, names.get(code), group) or flavour,
				"size": d._size_label(group, False),
				"size_ar": d._size_label(group, True),
				"stock": None if qty is None else float(qty),
				"auto": auto,
				"override": override,
				"effective": auto if override == _AUTO else override,
			}
		)
	return rows


def _products() -> list[dict[str, Any]]:
	profile = cs.get_chat_settings().get("default_pos_profile") or ""
	if not profile:
		frappe.throw(_("Set a Default POS Profile in Jarz Chat Settings first."))
	from jarz_pos.api import pos as pos_api

	with d._quiet():
		return pos_api.get_profile_products(profile) or []


def list_availability() -> dict[str, Any]:
	products = _products()
	codes = [str(p.get("id") or "") for p in products]
	return {
		"items": describe(
			products, d._branch_stock(codes), d._flavour_overrides(), d._item_arabic_names(codes)
		),
		"min_stock": d.MENU_MIN_STOCK,
	}


def set_availability(item_code: str, availability: str) -> dict[str, Any]:
	"""Set or clear one flavour's override; returns its fresh row."""
	code = str(item_code or "").strip()
	choice = str(availability or "").strip().lower()
	if choice not in _CHOICES:
		frappe.throw(_("Availability must be auto, available or paused."))
	products = _products()
	if code not in {str(p.get("id") or "") for p in products}:
		frappe.throw(_("{0} is not on the chat menu.").format(code))

	existing = frappe.db.get_all(
		d.FLAVOUR_OVERRIDE_DOCTYPE,
		filters={"parent": _PARENT, "parentfield": _FIELD, "item_code": code},
		pluck="name",
	)
	value = _CHOICES[choice]
	if value is None:
		for name in existing:
			frappe.db.delete(d.FLAVOUR_OVERRIDE_DOCTYPE, {"name": name})
	elif existing:
		frappe.db.set_value(
			d.FLAVOUR_OVERRIDE_DOCTYPE,
			existing[0],
			{"availability": value, "set_by": frappe.session.user, "set_on": now_datetime()},
		)
		for name in existing[1:]:
			frappe.db.delete(d.FLAVOUR_OVERRIDE_DOCTYPE, {"name": name})
	else:
		idx = (
			frappe.db.sql(
				f"select coalesce(max(idx), 0) from `tab{d.FLAVOUR_OVERRIDE_DOCTYPE}` where parent=%s and parentfield=%s",
				(_PARENT, _FIELD),
			)[0][0]
			or 0
		)
		frappe.get_doc(
			{
				"doctype": d.FLAVOUR_OVERRIDE_DOCTYPE,
				"parent": _PARENT,
				"parenttype": _PARENT,
				"parentfield": _FIELD,
				"idx": int(idx) + 1,
				"item_code": code,
				"availability": value,
				"set_by": frappe.session.user,
				"set_on": now_datetime(),
			}
		).db_insert()

	frappe.clear_document_cache(_PARENT, _PARENT)
	cs.clear_cache()
	cs.clear_draft_context_cache()
	product = [p for p in products if str(p.get("id") or "") == code]
	codes = [str(p.get("id") or "") for p in products]
	rows = describe(products, d._branch_stock(codes), d._flavour_overrides(), d._item_arabic_names(codes))
	row = next((r for r in rows if r["item_code"] == code), None)
	if row is None and product:
		frappe.throw(_("{0} has no price on the chat menu.").format(code))
	return row or {}
