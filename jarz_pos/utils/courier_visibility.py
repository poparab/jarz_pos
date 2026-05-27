from __future__ import annotations

from typing import Iterable, Mapping, Sequence

import frappe
from frappe import _

from jarz_pos.constants import ROLES
from jarz_pos.utils.validation_utils import assert_pos_profile_enabled


def _clean(value: object | None) -> str:
	return str(value or "").strip()


def _coerce_bool(value: object | None) -> bool:
	if isinstance(value, bool):
		return value
	if value is None:
		return False
	if isinstance(value, (int, float)):
		return value != 0
	text = _clean(value).lower()
	return text in {"1", "true", "yes", "y", "on"}


def _row_value(row: Mapping[str, object] | object, key: str) -> object | None:
	if isinstance(row, Mapping):
		return row.get(key)
	getter = getattr(row, "get", None)
	if callable(getter):
		try:
			value = getter(key, None)
		except TypeError:
			value = getter(key)
		except Exception:
			value = None
		if value is not None:
			return value
	return getattr(row, key, None)


def _normalize_profiles(profiles: Iterable[object] | None) -> list[str]:
	seen: set[str] = set()
	result: list[str] = []
	for profile in profiles or []:
		cleaned = _clean(profile)
		if not cleaned or cleaned in seen:
			continue
		seen.add(cleaned)
		result.append(cleaned)
	return result


def _get_user_roles(user: str | None = None) -> set[str]:
	current_user = _clean(user or getattr(frappe.session, "user", None))
	try:
		roles = frappe.get_roles(current_user) if current_user else frappe.get_roles()
	except TypeError:
		roles = frappe.get_roles() if not current_user else frappe.get_roles(current_user)
	except Exception:
		if not current_user:
			return set()
		try:
			roles = frappe.get_all("Has Role", filters={"parent": current_user}, pluck="role") or []
		except Exception:
			roles = []
	return {_clean(role) for role in roles if _clean(role)}


def user_has_global_profile_access(user: str | None = None) -> bool:
	current_user = _clean(user or getattr(frappe.session, "user", None))
	if current_user == ROLES.ADMINISTRATOR:
		return True
	return bool(_get_user_roles(current_user).intersection(ROLES.ADMIN))


def get_allowed_pos_profiles_for_user(user: str | None = None) -> list[str]:
	current_user = _clean(user or getattr(frappe.session, "user", None))
	if not current_user:
		return []

	if user_has_global_profile_access(current_user):
		return _normalize_profiles(
			frappe.get_all("POS Profile", filters={"disabled": 0}, pluck="name") or []
		)

	linked_profiles = frappe.get_all("POS Profile User", filters={"user": current_user}, pluck="parent") or []
	if not linked_profiles:
		return []

	return _normalize_profiles(
		frappe.get_all(
			"POS Profile",
			filters={"name": ["in", list(linked_profiles)], "disabled": 0},
			pluck="name",
		)
		or []
	)


def assert_user_can_access_pos_profile(pos_profile: str, user: str | None = None) -> str:
	requested = _clean(pos_profile)
	if not requested:
		frappe.throw(_("POS Profile is required"))

	assert_pos_profile_enabled(requested)
	if user_has_global_profile_access(user):
		return requested

	allowed_profiles = set(get_allowed_pos_profiles_for_user(user))
	if not allowed_profiles:
		frappe.throw(
			_("Not permitted: no assigned POS Profile was found for this user."),
			frappe.PermissionError,
		)
	if requested not in allowed_profiles:
		frappe.throw(
			_("You are not allowed to use POS Profile {0}").format(requested),
			frappe.PermissionError,
		)
	return requested


def get_visible_pos_profiles(requested_pos_profile: str | None = None, user: str | None = None) -> list[str]:
	requested = _clean(requested_pos_profile)
	allowed_profiles = get_allowed_pos_profiles_for_user(user)

	if requested:
		return [assert_user_can_access_pos_profile(requested, user)]

	return allowed_profiles


def resolve_invoice_pos_profile(invoice: Mapping[str, object] | object | str) -> str:
	invoice_row = invoice
	if isinstance(invoice, str):
		try:
			invoice_row = frappe.get_doc("Sales Invoice", invoice)
		except Exception:
			return ""
	return _clean(_row_value(invoice_row, "custom_kanban_profile") or _row_value(invoice_row, "pos_profile"))


def resolve_assignment_pos_profile(
	invoice: Mapping[str, object] | object | str,
	*,
	requested_pos_profile: str | None = None,
	user: str | None = None,
) -> str:
	invoice_label = _clean(invoice if isinstance(invoice, str) else _row_value(invoice, "name")) or _("the invoice")
	resolved_requested = _clean(requested_pos_profile)
	if resolved_requested:
		resolved_requested = assert_user_can_access_pos_profile(resolved_requested, user)

	invoice_profile = resolve_invoice_pos_profile(invoice)
	if invoice_profile:
		assert_user_can_access_pos_profile(invoice_profile, user)

	if resolved_requested and invoice_profile and resolved_requested != invoice_profile:
		frappe.throw(
			_("Sales Invoice {0} belongs to POS Profile {1}, not {2}.").format(
				invoice_label,
				invoice_profile,
				resolved_requested,
			),
			frappe.PermissionError,
		)

	resolved_profile = invoice_profile or resolved_requested
	if not resolved_profile:
		frappe.throw(_("Sales Invoice {0} has no operational POS Profile.").format(invoice_label))
	return resolved_profile


def resolve_courier_branch(
	party_type: str,
	party: str,
	*,
	row: Mapping[str, object] | object | None = None,
) -> str:
	branch = _clean(_row_value(row, "branch")) if row is not None else ""
	if branch:
		return branch
	if not party_type or not party:
		return ""
	try:
		return _clean(frappe.get_cached_value(party_type, party, "branch"))
	except Exception:
		return ""


def resolve_courier_delivery_partner(
	party_type: str,
	party: str,
	*,
	row: Mapping[str, object] | object | None = None,
) -> str:
	delivery_partner = ""
	if row is not None:
		delivery_partner = _clean(_row_value(row, "delivery_partner") or _row_value(row, "custom_delivery_partner"))
	if delivery_partner:
		return delivery_partner
	if not party_type or not party:
		return ""
	try:
		return _clean(frappe.get_cached_value(party_type, party, "custom_delivery_partner"))
	except Exception:
		return ""


def is_courier_record_active(
	party_type: str,
	party: str,
	*,
	row: Mapping[str, object] | object | None = None,
) -> bool:
	clean_party_type = _clean(party_type)
	clean_party = _clean(party)
	if not clean_party_type or not clean_party:
		return False

	if clean_party_type == "Employee":
		status = _clean(_row_value(row, "status")) if row is not None else ""
		if not status:
			try:
				status = _clean(frappe.get_cached_value("Employee", clean_party, "status"))
			except Exception:
				status = ""
		if status and status != "Active":
			return False
	elif clean_party_type == "Supplier":
		disabled = _row_value(row, "disabled") if row is not None else None
		if disabled is None:
			try:
				disabled = frappe.get_cached_value("Supplier", clean_party, "disabled")
			except Exception:
				disabled = None
		if _coerce_bool(disabled):
			return False

	delivery_partner = resolve_courier_delivery_partner(clean_party_type, clean_party, row=row)
	if delivery_partner:
		try:
			partner_active = frappe.get_cached_value("Delivery Partner", delivery_partner, "is_active")
		except Exception:
			partner_active = None
		if partner_active is not None and not _coerce_bool(partner_active):
			return False

	return True


def assert_courier_matches_pos_profile(
	party_type: str,
	party: str,
	pos_profile: str,
	*,
	require_active: bool = True,
) -> dict[str, str]:
	clean_party_type = _clean(party_type)
	clean_party = _clean(party)
	clean_pos_profile = _clean(pos_profile)

	if clean_party_type not in {"Employee", "Supplier"}:
		frappe.throw(_("Courier party type must be Employee or Supplier"))
	if not clean_party:
		frappe.throw(_("Courier party is required"))
	if not clean_pos_profile:
		frappe.throw(_("POS Profile is required"))
	if not frappe.db.exists(clean_party_type, clean_party):
		frappe.throw(_("{0} '{1}' not found").format(clean_party_type, clean_party))

	branch = resolve_courier_branch(clean_party_type, clean_party)
	if not branch:
		frappe.throw(_("Courier {0} has no branch and cannot be assigned.").format(clean_party))
	if branch != clean_pos_profile:
		frappe.throw(
			_("Courier {0} belongs to POS Profile {1}, not {2}.").format(
				clean_party,
				branch,
				clean_pos_profile,
			),
			frappe.PermissionError,
		)
	if require_active and not is_courier_record_active(clean_party_type, clean_party):
		frappe.throw(_("Courier {0} is inactive and cannot be assigned.").format(clean_party))

	delivery_partner = resolve_courier_delivery_partner(clean_party_type, clean_party)
	return {"branch": branch, "delivery_partner": delivery_partner}


def assert_invoices_share_pos_profile(
	invoices: Sequence[Mapping[str, object] | object] | None,
	*,
	requested_pos_profile: str | None = None,
	user: str | None = None,
) -> str:
	invoice_rows = list(invoices or [])
	if not invoice_rows:
		frappe.throw(_("At least one invoice is required to create a trip"))

	profiles: dict[str, list[str]] = {}
	for invoice in invoice_rows:
		invoice_name = _clean(_row_value(invoice, "name")) or _("an invoice")
		invoice_profile = resolve_invoice_pos_profile(invoice)
		if not invoice_profile:
			frappe.throw(_("Sales Invoice {0} has no operational POS Profile.").format(invoice_name))
		profiles.setdefault(invoice_profile, []).append(invoice_name)

	if len(profiles) > 1:
		frappe.throw(
			_("All invoices in a trip must belong to the same POS Profile. Found: {0}").format(
				", ".join(sorted(profiles))
			)
		)

	return resolve_assignment_pos_profile(
		invoice_rows[0],
		requested_pos_profile=requested_pos_profile,
		user=user,
	)


def filter_available_couriers(
	records: Sequence[Mapping[str, object] | object] | None,
	*,
	visible_profiles: Sequence[object] | None,
) -> list[dict[str, object]]:
	allowed_profiles = set(_normalize_profiles(visible_profiles))
	if not allowed_profiles:
		return []

	result: list[dict[str, object]] = []
	seen: set[tuple[str, str]] = set()

	for record in records or []:
		party_type = _clean(_row_value(record, "party_type"))
		party = _clean(_row_value(record, "party"))
		if not party_type or not party:
			continue

		branch = resolve_courier_branch(party_type, party, row=record)
		if not branch or branch not in allowed_profiles:
			continue

		if not is_courier_record_active(party_type, party, row=record):
			continue

		key = (party_type, party)
		if key in seen:
			continue
		seen.add(key)

		display_name = _clean(_row_value(record, "display_name")) or party
		delivery_partner = resolve_courier_delivery_partner(party_type, party, row=record)

		row_out: dict[str, object] = {
			"party_type": party_type,
			"party": party,
			"display_name": display_name,
			"branch": branch,
		}
		if delivery_partner:
			row_out["delivery_partner"] = delivery_partner
		result.append(row_out)

	return result