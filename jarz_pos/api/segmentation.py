import frappe
from frappe import _

from jarz_pos.constants import ROLES

#: Segments the nightly RFM job can write (``rfm_segmentation.classify_customer``)
#: and the ``Customer.customer_segment`` Select field accepts. "Regular" is NOT one
#: of them: it is a pseudo row in the summary (customers flagged
#: ``rfm_is_regular``), so pinning it would write a value the Select field does
#: not hold and the customer would vanish from every real segment.
REAL_SEGMENTS = (
	"Champion",
	"Loyal",
	"Potential Loyalist",
	"New Customer",
	"At Risk",
	"Can't Lose Them",
	"Lost",
	"One-Time",
)


def _ensure_jarz_manager() -> None:
	"""JARZ Manager or Administrator — the same gate as the analytics reports.

	Replaces ``frappe.only_for("JARZ Manager")``, which did not admit the
	Administrator role by name and so disagreed with every other report gate.
	"""
	roles = set(frappe.get_roles(frappe.session.user))
	if ROLES.JARZ_MANAGER not in roles and ROLES.ADMINISTRATOR not in roles:
		frappe.throw(_("Only JARZ Manager can access customer segments"), frappe.PermissionError)


def _is_truthy(value) -> bool:
	if isinstance(value, bool):
		return value
	return str(value or "").strip().lower() in {"1", "true", "yes", "y", "on"}


@frappe.whitelist()
def get_segment_summary():
	"""Returns count per segment for management dashboard."""
	_ensure_jarz_manager()
	from jarz_pos.services.rfm_segmentation import get_segment_summary as _get

	return _get()


@frappe.whitelist()
def run_segmentation_now():
	"""Manually trigger RFM recalculation. Manager-only."""
	_ensure_jarz_manager()
	from jarz_pos.services.rfm_segmentation import run_segmentation

	return run_segmentation()


@frappe.whitelist()
def export_segment(segment):
	"""Export a segment to a list for CSV download."""
	_ensure_jarz_manager()
	from jarz_pos.services.rfm_segmentation import export_segment_csv

	return export_segment_csv(segment)


@frappe.whitelist(methods=["POST"])
def set_segment_override(customer, override, manual_segment=None):
	"""Pin or unpin a customer's segment from automatic recalculation.

	``manual_segment`` must be one of :data:`REAL_SEGMENTS` when given. The
	pseudo segment "Regular" (and anything else the Select field does not hold)
	is refused with a clear error instead of being written. An unknown
	``customer`` raises ``DoesNotExistError`` (``db.set_value`` on a missing
	name is a silent no-op, so the call used to report "ok" for nothing).
	"""
	_ensure_jarz_manager()
	customer = str(customer or "").strip()
	if not customer or not frappe.db.exists("Customer", customer):
		frappe.throw(_("Customer {0} not found").format(customer or "''"), frappe.DoesNotExistError)
	pin = _is_truthy(override)
	segment = str(manual_segment or "").strip()
	if pin and segment and segment not in REAL_SEGMENTS:
		frappe.throw(
			_("'{0}' is not a segment a customer can be pinned to. Choose one of: {1}").format(
				segment, ", ".join(REAL_SEGMENTS)
			),
			frappe.ValidationError,
		)
	values = {"segment_override": 1 if pin else 0}
	if pin and segment:
		values["customer_segment"] = segment
	frappe.db.set_value("Customer", customer, values)
	frappe.db.commit()
	return {"status": "ok", "customer": customer}
