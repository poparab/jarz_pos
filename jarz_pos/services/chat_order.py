"""Read the order a customer asked for out of their chat, for "Start order".

The chat's "Start order" opened the POS with the customer only, and staff
retyped what the customer had written. :func:`order_from_chat` asks Claude for
the items, bundles and their flavours, then checks every line against the POS
catalog, so the cart it fills holds only real codes at the POS's own prices:

* Item and bundle codes must be on the POS profile's menu; anything else is
  left out and named in ``warnings``. A paused flavour stays in, with a warning.
* A bundle's flavours are matched to its item groups; a bundle whose groups do
  not add up is returned with ``complete: false`` for staff to finish by hand.
* Address, phone and area are never rewritten by the model. It only points at
  the customer's message that holds them; the address is that message
  verbatim, the phone is read from it by :func:`chat_ingest.typed_phones`, and
  the area must be a delivery territory, with its fee from the Territory.

Nothing is saved: the result only fills the POS screen, and staff review it
before anything is billed.
"""

from __future__ import annotations

import time
from typing import Any

import frappe

from jarz_pos.services import chat_common as cc

HISTORY_MESSAGES = 40
MAX_TOKENS = 1500
EFFORT = "low"

SYSTEM_PROMPT = """You read a chat between Jarz (an Egyptian dessert brand: jars, cakes, bundles) \
and a customer, and write down the order the customer wants placed now, for the POS.

Rules:
- Only what the customer asked for in this order. Drop items they changed their mind about or \
replaced; apply their latest changes (another flavour, another size, one more, one less).
- Codes come only from the catalog below. A jar flavour without a size the customer stated or \
clearly agreed to is unclear: leave it out and say so in "unclear". Never guess a size or flavour.
- A bundle (box/offer) goes in "bundles", one entry per box, with the flavours chosen for it in \
"selections" (item codes from that bundle's groups, with how many of each). When they did not \
choose flavours, leave selections empty. Jars ordered outside a box go in "items".
- «زي المرة اللي فاتت» / «ع النفس» / "same as last time" means the last order in the profile: \
use its codes.
- address_message: the [number] of the customer's own message that gives a delivery address \
different from the one on file (street, building, floor, landmark, or a shared location). Null \
when they confirmed the address on file or gave none.
- phone_message: the [number] of the customer's message that gives a new phone number. Null otherwise.
- area: the delivery area they named, exactly as written in the delivery area list, only when it \
differs from the area on file. Null otherwise.
- pickup: true only when they said they will collect it from a branch.
- notes: short English notes for the kitchen or courier that the customer asked for (gift card \
text, "fresh batch", call before arriving, deliver after 9 pm). Never prices.
Always answer by calling submit_order."""

TOOL = {
	"name": "submit_order",
	"description": "Submit the order the customer wants placed now.",
	"input_schema": {
		"type": "object",
		"properties": {
			"items": {
				"type": "array",
				"items": {
					"type": "object",
					"properties": {"item_code": {"type": "string"}, "qty": {"type": "integer"}},
					"required": ["item_code", "qty"],
				},
			},
			"bundles": {
				"type": "array",
				"items": {
					"type": "object",
					"properties": {
						"bundle_code": {"type": "string"},
						"selections": {
							"type": "array",
							"items": {
								"type": "object",
								"properties": {
									"item_code": {"type": "string"},
									"qty": {"type": "integer"},
								},
								"required": ["item_code", "qty"],
							},
						},
					},
					"required": ["bundle_code"],
				},
			},
			"address_message": {"type": ["integer", "null"]},
			"phone_message": {"type": ["integer", "null"]},
			"area": {"type": ["string", "null"]},
			"pickup": {"type": "boolean"},
			"notes": {"type": ["string", "null"]},
			"unclear": {"type": ["string", "null"]},
		},
		"required": ["items", "bundles"],
	},
}


# ── pure ────────────────────────────────────────────────────────────────


def build_catalog(
	products: list[dict[str, Any]], bundles: list[dict[str, Any]], paused: list[dict[str, Any]]
) -> dict[str, Any]:
	"""The POS profile's menu as lookups. Pure.

	``{"items": {code: name}, "paused": {code}, "bundles": {erpnext_item: {...}}}``;
	each bundle keeps its groups as ``{"group_key", "quantity", "items": {code: name}}``.
	"""
	items = {}
	for item in [*products, *paused]:
		code = str(item.get("id") or "")
		if code:
			items[code] = str(item.get("name") or code)
	out_bundles = {}
	for bundle in bundles:
		code = str(bundle.get("erpnext_item") or bundle.get("parent_item_code") or "")
		if not code:
			continue
		groups = []
		for group in bundle.get("item_groups") or []:
			members = {
				str(i.get("id") or ""): str(i.get("name") or i.get("id") or "")
				for i in group.get("items") or []
				if i.get("id")
			}
			groups.append(
				{
					"group_key": str(group.get("group_key") or group.get("group_name") or ""),
					"label": str(group.get("group_name") or ""),
					"quantity": int(group.get("quantity") or 0),
					"items": members,
				}
			)
		out_bundles[code] = {
			"id": str(bundle.get("id") or ""),
			"name": str(bundle.get("name") or code),
			"groups": groups,
		}
	return {
		"items": items,
		"paused": {str(p.get("id") or "") for p in paused if p.get("id")},
		"bundles": out_bundles,
	}


def render_catalog(catalog: dict[str, Any]) -> str:
	"""The catalog block of the prompt: codes and names only, never prices. Pure."""
	lines = ["# Catalog", "## Items (ITEM_CODE | name)"]
	lines += [f"{code} | {name}" for code, name in catalog["items"].items() if code not in catalog["paused"]]
	if catalog["paused"]:
		lines.append("## Paused (out of stock; still list them if ordered)")
		lines += [f"{code} | {catalog['items'].get(code, code)}" for code in sorted(catalog["paused"])]
	lines.append("## Bundles (BUNDLE_CODE | name), then each group: how many to choose, from which codes")
	for code, bundle in catalog["bundles"].items():
		lines.append(f"{code} | {bundle['name']}")
		for group in bundle["groups"]:
			lines.append(f"  - choose {group['quantity']} from: " + ", ".join(group["items"]))
	return "\n".join(lines)


def _code(ref: Any, known: dict[str, str]) -> str | None:
	"""Exact code, then case-insensitive code, then name. Pure."""
	ref = str(ref or "").strip()
	if not ref:
		return None
	if ref in known:
		return ref
	low = ref.lower()
	for code, name in known.items():
		if code.lower() == low or str(name).lower() == low:
			return code
	return None


def _qty(value: Any) -> int:
	try:
		return max(int(float(value)), 0)
	except (TypeError, ValueError):
		return 0


def assign_selections(
	bundle: dict[str, Any], picks: list[dict[str, Any]]
) -> tuple[dict[str, list], bool, list[str]]:
	"""Spread the chosen flavours over the bundle's groups. Pure.

	Returns ``(selections, complete, warnings)``: ``selections`` maps group_key to
	``[{"item_code", "qty"}]``, and ``complete`` is true when every group gets
	exactly its quantity. A flavour that fits no group with room left is named.
	"""
	room = {g["group_key"]: g["quantity"] for g in bundle["groups"]}
	selections: dict[str, list[dict[str, Any]]] = {g["group_key"]: [] for g in bundle["groups"]}
	warnings: list[str] = []
	for pick in picks or []:
		qty = _qty(pick.get("qty"))
		ref = pick.get("item_code")
		for group in bundle["groups"]:
			if qty <= 0:
				break
			code = _code(ref, group["items"])
			if not code or room[group["group_key"]] <= 0:
				continue
			take = min(qty, room[group["group_key"]])
			selections[group["group_key"]].append({"item_code": code, "qty": take})
			room[group["group_key"]] -= take
			qty -= take
		if qty > 0:
			warnings.append(f"{bundle['name']}: {ref} x{qty} does not fit this bundle's choices")
	complete = bool(bundle["groups"]) and all(v == 0 for v in room.values())
	if not complete:
		warnings.append(f"{bundle['name']}: choose the flavours by hand")
	return {k: v for k, v in selections.items() if v}, complete, warnings


def validate_order(
	raw: dict[str, Any] | None, catalog: dict[str, Any]
) -> tuple[list[dict[str, Any]], list[str]]:
	"""The model's items and bundles, checked against the catalog. Pure.

	Returns ``(lines, warnings)``; see the module docstring for the line shape.
	"""
	raw = raw or {}
	lines: list[dict[str, Any]] = []
	warnings: list[str] = []
	merged: dict[str, int] = {}
	for row in raw.get("items") or []:
		code = _code(row.get("item_code"), catalog["items"])
		qty = _qty(row.get("qty"))
		if not code:
			warnings.append(f"Not on the menu: {row.get('item_code') or '?'}")
			continue
		if qty <= 0:
			continue
		merged[code] = merged.get(code, 0) + qty
	for code, qty in merged.items():
		if code in catalog["paused"]:
			warnings.append(f"{catalog['items'][code]} is paused (no stock at any branch)")
		lines.append({"kind": "item", "item_code": code, "item_name": catalog["items"][code], "qty": qty})
	bundle_names = {code: b["name"] for code, b in catalog["bundles"].items()}
	for row in raw.get("bundles") or []:
		code = _code(row.get("bundle_code"), bundle_names)
		if not code:
			warnings.append(f"Not a bundle on the menu: {row.get('bundle_code') or '?'}")
			continue
		bundle = catalog["bundles"][code]
		selections, complete, notes = assign_selections(bundle, row.get("selections") or [])
		warnings += notes
		for picks in selections.values():
			for pick in picks:
				if pick["item_code"] in catalog["paused"]:
					warnings.append(
						f"{catalog['items'].get(pick['item_code'], pick['item_code'])} is paused (no stock at any branch)"
					)
		lines.append(
			{
				"kind": "bundle",
				"bundle_id": bundle["id"],
				"item_code": code,
				"item_name": bundle["name"],
				"qty": 1,
				"selections": selections,
				"complete": complete,
			}
		)
	unclear = str(raw.get("unclear") or "").strip()
	if unclear:
		warnings.append(f"Unclear: {unclear[:300]}")
	return lines, list(dict.fromkeys(warnings))


def numbered_transcript(
	rows: list[dict[str, Any]], mask: bool, names: list[Any]
) -> tuple[list[str], dict[int, dict]]:
	"""``[n] [Thu 29 Oct 19:02] Customer: ...`` lines and the row behind each n. Pure."""
	from jarz_pos.services import chat_drafts as drafts

	lines, by_number = [], {}
	for row in rows:
		text = drafts.transcript_lines([row], mask, names)
		if not text:
			continue
		number = len(by_number) + 1
		by_number[number] = row
		lines.append(f"[{number}] {text[0]}")
	return lines, by_number


def _digits10(value: Any) -> str:
	digits = "".join(ch for ch in str(value or "") if ch.isdigit())
	return digits[-10:]


def contact_changes(
	raw: dict[str, Any], by_number: dict[int, dict], known_phones: list[Any]
) -> dict[str, Any]:
	"""The new address and phone the customer typed, from their own messages. Pure.

	Only an inbound message counts; a phone already on file is not "new".
	"""
	from jarz_pos.services.chat_ingest import typed_phones

	out: dict[str, Any] = {"address": None, "phone": None}
	row = by_number.get(_qty(raw.get("address_message")))
	if row and row.get("direction") == "Inbound" and str(row.get("body") or "").strip():
		out["address"] = {"text": str(row["body"]).strip()}
	row = by_number.get(_qty(raw.get("phone_message")))
	if row and row.get("direction") == "Inbound":
		known = {_digits10(p) for p in known_phones if p}
		fresh = [p for p in typed_phones(row.get("body")) if _digits10(p) not in known]
		if fresh:
			out["phone"] = "0" + _digits10(fresh[0])
	return out


def resolve_area(name: Any, territories: list[dict[str, Any]]) -> dict[str, Any] | None:
	"""The delivery territory *name* refers to (code or Arabic name), with its fee. Pure."""
	low = str(name or "").strip().lower()
	if not low:
		return None
	for row in territories:
		ar = str(row.get("custom_territory_name_ar") or "").strip()
		if low not in (str(row.get("name") or "").lower(), ar.lower()):
			continue
		return {
			"territory": row.get("name"),
			"label": ar or row.get("name"),
			"delivery_income": float(row.get("delivery_income") or 0),
		}
	return None


# ── data and call ───────────────────────────────────────────────────────


def _catalog(profile: str) -> dict[str, Any]:
	"""The retail menu: chat orders are never B2B, so no customer price list."""
	from jarz_pos.api import pos as pos_api
	from jarz_pos.services import chat_drafts as drafts

	with drafts._quiet():
		products = pos_api.get_profile_products(profile) or []
		bundles = pos_api.get_profile_bundles(profile) or []
	products, paused = drafts.split_by_stock(
		products, drafts._branch_stock(str(p.get("id") or "") for p in products)
	)
	return build_catalog(products, bundles, paused)


def _last_order_codes(customer: str | None) -> list[str]:
	if not customer:
		return []
	inv = frappe.get_all(
		"Sales Invoice",
		filters={"customer": customer, "docstatus": 1, "is_return": 0},
		pluck="name",
		order_by="posting_date desc, creation desc",
		limit_page_length=1,
		ignore_permissions=True,
	)
	if not inv:
		return []
	rows = frappe.get_all(
		"Sales Invoice Item",
		filters={"parent": inv[0], "parenttype": "Sales Invoice"},
		fields=["item_code", "qty"],
		order_by="idx asc",
		ignore_permissions=True,
	)
	return [f"{r.item_code} x{float(r.qty or 0):g}" for r in rows if r.item_code]


def _call(api_key: str, model: str, system_text: str, user_text: str) -> dict[str, Any] | None:
	import anthropic

	client = anthropic.Anthropic(api_key=api_key, timeout=40.0, max_retries=1)
	resp = client.messages.create(
		model=model,
		max_tokens=MAX_TOKENS,
		system=[{"type": "text", "text": SYSTEM_PROMPT}, {"type": "text", "text": system_text}],
		messages=[{"role": "user", "content": user_text}],
		tools=[TOOL],
		# Sonnet 5.5 and Opus 5.5 reject a forced tool with a 400.
		tool_choice={"type": "auto"},
		extra_body={"output_config": {"effort": EFFORT}},
	)
	for block in getattr(resp, "content", None) or []:
		if getattr(block, "type", None) == "tool_use" and getattr(block, "name", "") == "submit_order":
			return getattr(block, "input", None) or {}
	return None


def order_from_chat(conversation: str) -> dict[str, Any]:
	"""``{"order": {...} | None, "order_error": str | None}`` for *conversation*."""
	from jarz_pos.services import chat_drafts as drafts
	from jarz_pos.services.chat_settings import get_chat_settings

	settings = get_chat_settings()
	api_key = settings.get("anthropic_api_key")
	if not api_key:
		return {"order": None, "order_error": "The AI is not configured (Jarz Chat Settings)"}
	profile = settings.get("default_pos_profile")
	if not profile:
		return {"order": None, "order_error": "No POS profile is set in Jarz Chat Settings"}
	conv = frappe.db.get_value(
		cc.CONVERSATION, conversation, ["name", "contact_name", "customer", "phone"], as_dict=True
	)
	customer = conv.customer if conv else None
	customer_row = (
		frappe.db.get_value("Customer", customer, ["customer_name", "mobile_no"], as_dict=True)
		if customer
		else None
	) or {}
	rows = frappe.get_all(
		cc.MESSAGE,
		filters={"conversation": conversation},
		fields=["direction", "message_type", "body", "timestamp"],
		order_by="timestamp desc, creation desc",
		limit_page_length=HISTORY_MESSAGES,
		ignore_permissions=True,
	)
	rows.reverse()
	mask = bool(settings.get("mask_personal_data", True))
	lines, by_number = numbered_transcript(rows, mask, [conv.contact_name, customer_row.get("customer_name")])
	if not lines:
		return {"order": None, "order_error": "The chat has no messages to read"}

	catalog = _catalog(profile)
	cust_profile = drafts.customer_profile(customer) if customer else None
	territories = drafts._territories()
	area_names = sorted(
		{
			label
			for t in territories
			for label in (str(t.get("name") or ""), str(t.get("custom_territory_name_ar") or "").strip())
			if label
		}
	)
	system_text = "\n".join([render_catalog(catalog), "## Delivery areas", ", ".join(area_names) or "(none)"])
	last = _last_order_codes(customer)
	user_text = "\n\n".join(
		[
			drafts.render_customer_profile(cust_profile),
			"Last order: " + (", ".join(last) if last else "(none)"),
			"Conversation (oldest first, [number] before each message):",
			"\n".join(lines),
			"Write down the order the customer wants placed now, with submit_order.",
		]
	)
	model = settings.get("draft_model") or "claude-haiku-5-5"
	started = time.monotonic()
	try:
		raw = _call(api_key, model, system_text, user_text)
	except Exception as exc:
		cc.safe_log_error(
			"chat: order prefill failed",
			f"conversation={conversation} {type(exc).__name__}: {str(exc)[:300]}",
		)
		return {"order": None, "order_error": "The AI could not read the order; add it by hand"}
	if raw is None:
		return {"order": None, "order_error": "The AI did not return an order; add it by hand"}

	order_lines, warnings = validate_order(raw, catalog)
	changes = contact_changes(raw, by_number, [conv.phone, customer_row.get("mobile_no")])
	area = resolve_area(raw.get("area"), territories)
	if raw.get("area") and not area:
		warnings.append(f"Area named in the chat is not a delivery area: {str(raw.get('area'))[:80]}")
	elif area and area["territory"] == (cust_profile or {}).get("area"):
		area = None  # the area on file: nothing changed
	if not order_lines:
		warnings.append("No items found in the chat: add them by hand")
	notes = str(raw.get("notes") or "").strip()[:500] or None
	return {
		"order": {
			"lines": order_lines,
			"address": changes["address"],
			"area": area,
			"phone": changes["phone"],
			"contact_name": None,
			"pickup": bool(raw.get("pickup")),
			"notes": notes,
			"warnings": warnings,
			"latency_ms": int((time.monotonic() - started) * 1000),
		},
		"order_error": None,
	}
