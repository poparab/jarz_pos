"""API endpoints for POS Payment Receipt management."""

from __future__ import annotations
import frappe
import base64
import os
from dataclasses import dataclass
from typing import List, Dict, Any
from frappe import _
from frappe.exceptions import PermissionError as FrappePermissionError
from frappe.exceptions import ValidationError as FrappeValidationError

from jarz_pos.constants import ROLES
from jarz_pos.utils.invoice_utils import normalize_woo_order_id


RECEIPT_STATUS_UNCONFIRMED = "Unconfirmed"
RECEIPT_STATUS_CONFIRMED = "Confirmed"
RECEIPT_STATUS_CHANGED = "Changed"
# A manager looked at the proof of transfer and did not accept it. Kept
# listable, unlike "Changed", so the person who uploaded it finds out why
# instead of watching the receipt sit unconfirmed for ever.
RECEIPT_STATUS_REJECTED = "Rejected"


#: The ``payment_method`` Select on POS Payment Receipt accepts exactly these two
#: labels, while a Sales Invoice's ``custom_payment_method`` carries the
#: operator-facing spelling ("Instapay", "Mobile Wallet"). Writing the invoice's
#: spelling straight through fails Select validation, so every automatic writer
#: must map through :func:`receipt_method_label` first.
RECEIPT_METHOD_LABELS = {"instapay": "InstaPay", "wallet": "Wallet"}


def _normalize_receipt_method(method: str | None) -> str:
    normalized = str(method or "").strip().lower().replace(" ", "").replace("_", "")
    if normalized in {"instapay", "insta", "bank", "bankaccount"}:
        return "instapay"
    if normalized in {"wallet", "mobilewallet"}:
        return "wallet"
    if normalized in {"paymentgateway", "gateway", "card"}:
        return "payment_gateway"
    if normalized in {"cash", "cod", "cashondelivery"}:
        return "cash"
    return normalized


def receipt_method_label(method: str | None) -> str | None:
    """The DocType Select label for *method*, or ``None`` if it has no receipt.

    Only the two transfer methods take a screenshot. Cash needs no proof, and a
    card/gateway payment is captured by the gateway itself — returning ``None``
    for those is what keeps :func:`ensure_pending_payment_receipt` from filing a
    receipt nobody can ever satisfy.
    """
    return RECEIPT_METHOD_LABELS.get(_normalize_receipt_method(method))


def get_live_transfer_receipt(sales_invoice: str | None) -> dict | None:
    """The transfer screenshot already on file for *sales_invoice*, if any.

    "On file" means a receipt that is still live -- Unconfirmed or Confirmed,
    never ``Changed`` (retired by a collection change) or ``Rejected`` (a
    manager looked and did not accept it) -- and that actually carries an image.
    A receipt with no image is a placeholder filed by the dispatch path asking
    for proof, not proof itself.

    This is the load-bearing fact behind the cash/online decision, and it is a
    statement about INTENT, not about money having arrived. The upload flow is
    only reachable from the InstaPay / Wallet payment actions, so a screenshot
    on an order is the floor saying "the customer is paying by transfer" --
    whatever the Woo-declared ``custom_payment_method`` still says. Woo sends
    ``cod`` for every order placed without an online gateway, including the ones
    where the customer then transfers instead, so the invoice field is the
    weaker signal of the two.

    Returns the newest matching row (``name``, ``payment_method``) or ``None``.
    Never raises: every caller is on a dispatch or confirmation path where an
    exception would be worse than the answer "no receipt".
    """
    invoice_name = str(sales_invoice or "").strip()
    if not invoice_name:
        return None
    try:
        rows = frappe.get_all(
            "POS Payment Receipt",
            filters={
                "sales_invoice": invoice_name,
                "status": ["in", [RECEIPT_STATUS_UNCONFIRMED, RECEIPT_STATUS_CONFIRMED]],
                # The Select only offers these two today, but the callers act on
                # this answer by rerouting an order to the transfer flow -- and
                # that flow can only ever confirm a receipt whose method matches
                # the invoice. A row for any other method would strand the order
                # Awaiting Payment with no way to confirm it.
                "payment_method": ["in", list(RECEIPT_METHOD_LABELS.values())],
            },
            fields=["name", "payment_method", "amount", "status", "receipt_image", "receipt_image_url"],
            order_by="creation desc",
            limit_page_length=20,
        )
    except Exception:
        return None
    for row in rows:
        if str(row.get("receipt_image_url") or row.get("receipt_image") or "").strip():
            return row
    return None


@dataclass(frozen=True)
class PendingReceipt:
    """What :func:`ensure_pending_payment_receipt` actually did.

    A bare receipt name could not tell "I filed this row" from "this row was
    already here", and the hourly reconciler reported both as work done: on
    2026-09-09 a steady-state sweep of production logged 23 of 23 orders
    "reconciled" while creating nothing, so the one line meant to say whether
    the backlog moved said the same thing every hour whatever happened.

    ``name`` is the receipt for this invoice and method, or ``None`` when none
    could be filed. ``changed`` is the only thing a caller should report on.
    """

    name: str | None = None
    created: bool = False
    amount_synced: bool = False

    @property
    def changed(self) -> bool:
        """Whether this call wrote anything. A reused, in-step row did not."""
        return self.created or self.amount_synced

    def __bool__(self) -> bool:
        """Truthy means *a receipt exists*, not that this call made one.

        Kept deliberately different from :attr:`changed`: the callers that test
        this are asking "is the order visible on the receipts list", which a
        reused row answers just as well as a new one.
        """
        return bool(self.name)


def ensure_pending_payment_receipt(
    sales_invoice: str,
    *,
    payment_method: str | None,
    amount: float,
    pos_profile: str | None,
) -> PendingReceipt:
    """Create — or refresh — the imageless receipt an awaiting-payment order needs.

    An unpaid InstaPay/Wallet order moved Out for Delivery used to create no
    ``POS Payment Receipt`` row at all: the row was only ever born client-side, at
    the moment somebody attached a screenshot. So the list whose entire purpose is
    "orders still owing a transfer" showed only the orders that had already been
    dealt with, and on 2026-09-09 production held 20 awaiting orders worth
    10,780 EGP that appeared on no receipt screen. Filing the row up front makes
    the queue match reality.

    Two properties matter more than the creation itself:

    * **It never raises.** This runs as a side effect of dispatch. A receipt that
      cannot be filed is a reporting gap; an exception here would be a courier
      standing at the door with an order that will not go out. Same reasoning as
      the courier-attribution params being optional.
    * **It refreshes a stale amount, but only while no screenshot is attached.**
      ``ensure_uploaded_payment_receipt`` refuses a receipt whose amount has
      drifted from the invoice, so a post-submit re-rate (shipping recalculated
      on an address change) would otherwise leave an order that can never be
      confirmed. An imageless row carries a TARGET copied off the invoice, so
      re-syncing it loses nothing. Once an image is attached the amount is a
      CLAIM about that screenshot, and it is the only automated check that the
      proof matches the order — rewriting it would silently turn a 500 EGP
      transfer against a 1,000 EGP order into a full collection.

    Returns a :class:`PendingReceipt` rather than the name alone, so a caller
    that reports on its own work -- the hourly reconciler and the v1_9 patch --
    can tell a row it filed from one that was already there.
    """
    invoice_name = str(sales_invoice or "").strip()
    method_label = receipt_method_label(payment_method)
    profile = str(pos_profile or "").strip()
    if not invoice_name or not method_label or not profile:
        return PendingReceipt()

    try:
        rows = frappe.get_all(
            "POS Payment Receipt",
            filters={
                "sales_invoice": invoice_name,
                "status": ["!=", RECEIPT_STATUS_CHANGED],
            },
            fields=[
                "name", "payment_method", "status", "amount",
                "receipt_image", "receipt_image_url",
            ],
            order_by="creation desc",
            limit_page_length=20,
        )
        existing = next(
            (
                row for row in rows
                if _normalize_receipt_method(row.get("payment_method"))
                == _normalize_receipt_method(method_label)
            ),
            None,
        )

        if existing:
            has_image = bool(
                str(
                    existing.get("receipt_image_url")
                    or existing.get("receipt_image")
                    or ""
                ).strip()
            )
            needs_resync = (
                not has_image
                and str(existing.get("status") or "").strip() != RECEIPT_STATUS_CONFIRMED
                and abs(float(existing.get("amount") or 0) - float(amount or 0)) > 0.01
            )
            if needs_resync:
                frappe.db.set_value(
                    "POS Payment Receipt",
                    existing["name"],
                    "amount",
                    float(amount or 0),
                    update_modified=False,
                )
            return PendingReceipt(
                name=existing["name"], created=False, amount_synced=needs_resync
            )

        receipt = frappe.get_doc({
            "doctype": "POS Payment Receipt",
            "sales_invoice": invoice_name,
            "payment_method": method_label,
            "amount": float(amount or 0),
            "pos_profile": profile,
            "status": RECEIPT_STATUS_UNCONFIRMED,
        })
        receipt.insert(ignore_permissions=True)
        return PendingReceipt(name=receipt.name, created=True)
    except Exception as exc:  # pragma: no cover - must never block a dispatch
        frappe.logger().error(
            f"Failed to file pending payment receipt for {invoice_name}: {exc}"
        )
        return PendingReceipt()


def mark_payment_receipts_changed_for_invoice(
    sales_invoice: str,
    *,
    payment_methods: list[str] | tuple[str, ...] | set[str] | None = None,
    receipt_name: str | None = None,
) -> list[str]:
    invoice_name = str(sales_invoice or "").strip()
    if not invoice_name:
        return []

    filters: dict[str, object] = {
        "sales_invoice": invoice_name,
        "status": ["!=", RECEIPT_STATUS_CHANGED],
    }
    if receipt_name:
        filters["name"] = str(receipt_name).strip()

    rows = frappe.get_all(
        "POS Payment Receipt",
        filters=filters,
        fields=["name", "payment_method"],
        order_by="creation desc",
    )
    if payment_methods:
        allowed_methods = {
            _normalize_receipt_method(method)
            for method in payment_methods
            if str(method or "").strip()
        }
        rows = [
            row for row in rows
            if _normalize_receipt_method(row.get("payment_method")) in allowed_methods
        ]

    changed_receipts: list[str] = []
    for row in rows:
        receipt = frappe.get_doc("POS Payment Receipt", row.get("name"))
        receipt.status = RECEIPT_STATUS_CHANGED
        receipt.save(ignore_permissions=True)
        changed_receipts.append(receipt.name)

    return changed_receipts


def retire_pending_payment_receipts(sales_invoice: str) -> list[str]:
    """Mark the still-pending receipts on *sales_invoice* as no longer applicable.

    Used when the money arrived by some other route, so nobody is owed a
    transfer screenshot any more. Unlike
    :func:`mark_payment_receipts_changed_for_invoice` this deliberately spares a
    CONFIRMED row: that one is evidence a manager actually looked at, and
    rewriting it to "Changed" would destroy the audit trail for a payment that
    really did happen.
    """
    invoice_name = str(sales_invoice or "").strip()
    if not invoice_name:
        return []

    rows = frappe.get_all(
        "POS Payment Receipt",
        filters={
            "sales_invoice": invoice_name,
            "status": ["in", [RECEIPT_STATUS_UNCONFIRMED, RECEIPT_STATUS_REJECTED]],
        },
        pluck="name",
    )
    retired: list[str] = []
    for name in rows:
        try:
            receipt = frappe.get_doc("POS Payment Receipt", name)
            receipt.status = RECEIPT_STATUS_CHANGED
            receipt.save(ignore_permissions=True)
            retired.append(name)
        except Exception as exc:  # pragma: no cover - housekeeping must not raise
            frappe.logger().error(f"Failed to retire payment receipt {name}: {exc}")
    return retired


def ensure_uploaded_payment_receipt(
    receipt_name: str,
    *,
    sales_invoice: str,
    payment_method: str,
    amount: float,
) -> dict[str, Any]:
    normalized_name = str(receipt_name or "").strip()
    if not normalized_name:
        frappe.throw("Payment receipt is required")
    if not frappe.db.exists("POS Payment Receipt", normalized_name):
        frappe.throw("Payment receipt was not found")

    receipt = frappe.get_doc("POS Payment Receipt", normalized_name)
    if str(getattr(receipt, "sales_invoice", "") or "").strip() != str(sales_invoice or "").strip():
        frappe.throw("Payment receipt does not belong to this invoice")
    if str(getattr(receipt, "status", "") or "").strip() == RECEIPT_STATUS_CHANGED:
        frappe.throw("Changed payment receipts cannot be used")
    if _normalize_receipt_method(getattr(receipt, "payment_method", None)) != _normalize_receipt_method(payment_method):
        frappe.throw("Payment receipt method does not match the selected collection method")
    receipt_amount = float(getattr(receipt, "amount", 0) or 0)
    if abs(receipt_amount - float(amount or 0)) > 0.01:
        # Deliberately still a refusal. The posted Payment Entry takes its
        # amount from the invoice's outstanding, never from this field, so this
        # comparison is the only automated check that the screenshot somebody
        # uploaded is for this order's money. Re-syncing it here would turn a
        # 500 EGP transfer against a 1,000 EGP order into a full collection with
        # no trace of the 500 claim. The stale-amount case this used to guard
        # against is healed earlier and more safely, by
        # ensure_pending_payment_receipt, which re-syncs only while the row
        # still has no image attached.
        frappe.throw("Payment receipt amount does not match the order amount")

    image_url = str(
        getattr(receipt, "receipt_image_url", None)
        or getattr(receipt, "receipt_image", None)
        or ""
    ).strip()
    if not image_url:
        frappe.throw("Payment receipt must have an uploaded image")

    return {
        "name": receipt.name,
        "sales_invoice": str(getattr(receipt, "sales_invoice", "") or "").strip(),
        "payment_method": str(getattr(receipt, "payment_method", "") or "").strip(),
        "amount": receipt_amount,
        "status": str(getattr(receipt, "status", "") or "").strip(),
        "receipt_image_url": image_url,
    }


def _ensure_receipt_image_editable(receipt) -> None:
    """Allow image changes while the receipt is still Unconfirmed or Rejected.

    ``Confirmed`` is evidence and ``Changed`` is audit history, so both are
    frozen. ``Rejected`` deliberately is not: a rejection asks the branch for a
    better screenshot, and freezing it would leave them nothing to do about it.
    """
    status = str(getattr(receipt, "status", "") or "").strip()
    if status == RECEIPT_STATUS_CONFIRMED:
        frappe.throw(_("Confirmed payment receipts cannot be changed."))
    if status == RECEIPT_STATUS_CHANGED:
        frappe.throw(_("Changed payment receipts cannot be edited."))


def _receipt_image_file_names(receipt_name: str, *image_urls: str | None) -> list[str]:
    """File docs holding the receipt image, matched by attachment or by URL."""
    urls = {
        str(url or "").strip()
        for url in image_urls
        if str(url or "").strip()
    }
    rows = frappe.get_all(
        "File",
        filters={
            "attached_to_doctype": "POS Payment Receipt",
            "attached_to_name": str(receipt_name or "").strip(),
        },
        fields=["name", "file_url", "attached_to_field"],
    )
    names = []
    for row in rows:
        field = str(row.get("attached_to_field") or "").strip()
        file_url = str(row.get("file_url") or "").strip()
        if field == "receipt_image" or (file_url and file_url in urls):
            names.append(row.get("name"))
    return names


def _delete_receipt_image_files(file_names: list[str]) -> None:
    """Best-effort removal of superseded receipt image files."""
    for file_name in file_names:
        try:
            frappe.delete_doc("File", file_name, ignore_permissions=True, force=True)
        except Exception as exc:  # pragma: no cover - storage cleanup must never block
            frappe.logger().error(
                f"Failed to delete receipt image file {file_name}: {exc}"
            )


def _current_user_roles() -> set[str]:
    return {
        str(role or "").strip()
        for role in (frappe.get_roles(frappe.session.user) or [])
        if str(role or "").strip()
    }


def _has_payment_receipt_confirm_access(pos_profile: str | None = None) -> bool:
    roles = _current_user_roles()

    # Admin tier confirms anywhere; the rest of the line-manager tier is scoped
    # to the profiles they are actually assigned to.
    if ROLES.ADMIN.intersection(roles) or ROLES.JARZ_MANAGER in roles:
        return True

    if roles.isdisjoint(ROLES.LINE_MANAGER_TIER):
        return False

    if not pos_profile:
        return True

    from jarz_pos.api.manager import _current_user_allowed_profiles

    allowed_profiles = {
        str(profile or "").strip()
        for profile in (_current_user_allowed_profiles() or [])
        if str(profile or "").strip()
    }
    if not allowed_profiles:
        return False

    return str(pos_profile or "").strip() in allowed_profiles


def _allowed_pos_profiles() -> list[str]:
    """POS Profiles the session user is assigned to.

    Wrapped rather than imported at module scope on purpose: ``api.manager``
    pulls in the real ``frappe`` package, and this module's unit tests run
    site-less against a stub. Patch this name to scope a test.
    """
    from jarz_pos.api.manager import _current_user_allowed_profiles

    return list(_current_user_allowed_profiles() or [])


def _receipt_branch_scope() -> list[str]:
    """The caller's assigned POS Profiles, cleaned: the one branch scope for receipts.

    Every receipt read and write resolves the caller's branches through here,
    so the list filter and the per-receipt check cannot come to disagree about
    what "assigned" means again.

    An EMPTY result means "assigned to no branch", never "unrestricted".
    ``get_user_pos_profiles`` already hands ``Administrator`` every enabled
    profile, so no legitimate all-branches user reaches here with nothing. The
    read endpoints used to treat empty as "apply no filter", which showed a user
    with no branch at all every receipt of every branch -- customer names,
    invoice ids and transfer screenshots -- while the write endpoints refused
    that same user everything.
    """
    seen: list[str] = []
    for allowed in _allowed_pos_profiles():
        profile = str(allowed or "").strip()
        if profile and profile not in seen:
            seen.append(profile)
    return seen


def _has_receipt_branch_access(pos_profile: str | None) -> bool:
    """Whether the caller may read or write a receipt filed against *pos_profile*.

    The write endpoints once scoped nothing, so any ``Sales User`` who knew a
    receipt name could replace or drop another branch's proof of transfer; the
    read endpoints then turned out to be looser than the writes (an explicit
    ``pos_profile`` was trusted as given, and an empty branch list meant no
    filter). Both now answer through this one rule, which mirrors the scoping
    half of :func:`_has_payment_receipt_confirm_access`, so seeing and editing
    agree.

    ``Administrator`` passes because ``get_user_pos_profiles`` hands the
    unrestricted user every enabled profile.
    """
    profile = str(pos_profile or "").strip()
    if not profile:
        # ``pos_profile`` is reqd on the DocType, so an empty one can only be a
        # row that predates the field. There is nothing to scope against, and
        # refusing here would brick it for everyone.
        return True

    return profile in _receipt_branch_scope()


def _ensure_receipt_branch_access(pos_profile: str | None) -> None:
    if _has_receipt_branch_access(pos_profile):
        return

    frappe.throw(
        _("This payment receipt belongs to a branch you are not assigned to."),
        FrappePermissionError,
    )


def _ensure_payment_receipt_confirm_access(pos_profile: str | None = None) -> None:
    if _has_payment_receipt_confirm_access(pos_profile):
        return

    frappe.throw(
        _("Only branch managers and above can confirm payment receipts."),
        FrappePermissionError,
    )


#: The POS Payment Receipt columns every receipt read returns. Shared by
#: :func:`list_payment_receipts` and :func:`get_payment_receipt` so the single
#: read cannot drift from the list the app already parses.
RECEIPT_ROW_FIELDS = (
    'name',
    'sales_invoice',
    'payment_method',
    'amount',
    'pos_profile',
    'status',
    'receipt_image',
    'receipt_image_url',
    'uploaded_by',
    'upload_date',
    'confirmed_by',
    'confirmed_date',
    'rejected_by',
    'rejected_date',
    'rejection_reason',
    'creation',
    'modified',
)


def _decorate_receipt_row(receipt: dict) -> dict:
    """Add the invoice-derived fields and ``can_confirm`` to a receipt row, in place.

    One builder for both read endpoints: the transfer-proof sheet re-reads a
    single receipt through :func:`get_payment_receipt` and hands it to the same
    client model the list feeds, so a key added here reaches both or neither.
    """
    try:
        invoice = frappe.get_doc('Sales Invoice', receipt['sales_invoice'])
        receipt['customer_name'] = invoice.customer_name
        receipt['invoice_id'] = invoice.name
        receipt['woo_order_id'] = normalize_woo_order_id(invoice.get('woo_order_id'))
    except Exception:
        receipt['customer_name'] = 'Unknown'
        receipt['invoice_id'] = receipt['sales_invoice']
        receipt['woo_order_id'] = None

    receipt['can_confirm'] = _has_payment_receipt_confirm_access(
        receipt.get('pos_profile')
    )
    return receipt


def _does_not_exist_error() -> type:
    """``frappe.DoesNotExistError``, resolved lazily.

    The site-less test harness stubs ``frappe.exceptions`` with only the two
    classes this module imports at the top, so a module-level import of a third
    would break every test in it. ``DoesNotExistError`` subclasses
    ``ValidationError``, which makes that the faithful fallback.
    """
    try:
        from frappe.exceptions import DoesNotExistError
    except ImportError:
        return FrappeValidationError
    return DoesNotExistError


@frappe.whitelist()
def get_payment_receipt(receipt_name: str):
    """One payment receipt, in exactly the shape a ``list_payment_receipts`` row has.

    The InstaPay transfer-proof sheet re-reads the receipt it is about to
    confirm. Its only way to do that was ``list_payment_receipts(pos_profile)``,
    which returns every non-Changed receipt of the branch with no limit and
    loads a Sales Invoice for each one — to look at a single row.

    Scoping is :func:`_has_receipt_branch_access`, the rule every write endpoint
    enforces: the receipt must be filed against a POS Profile the caller is
    assigned to. A caller assigned to no branch is refused rather than let
    through -- this read used to copy the list's old "empty allowed list means
    unrestricted", which handed a branchless user any receipt by name. A legacy
    row with no ``pos_profile`` stays readable, exactly as it stays writable.
    Like the list it reads with ``frappe.get_all``, which does NOT apply DocPerm
    read rules, so the branch check is the only gate.

    Unlike the list, ``Changed`` receipts are returned: the caller asked for this
    row by name, and its ``status`` is what tells the sheet it was superseded.

    Args:
        receipt_name: POS Payment Receipt name.

    Returns:
        dict: ``{"success": True, "receipt": row}``.
    """
    name = str(receipt_name or '').strip()
    if not name:
        frappe.throw(_("Payment receipt name is required."), FrappeValidationError)

    rows = frappe.get_all(
        'POS Payment Receipt',
        filters={'name': name},
        fields=list(RECEIPT_ROW_FIELDS),
        limit_page_length=1,
    )
    if not rows:
        frappe.throw(
            _("Payment receipt {0} was not found.").format(name),
            _does_not_exist_error(),
        )
    receipt = rows[0]

    # Refused before the invoice is loaded, so nothing of the order leaks.
    _ensure_receipt_branch_access(receipt.get('pos_profile'))

    return {'success': True, 'receipt': _decorate_receipt_row(receipt)}


@frappe.whitelist()
def list_payment_receipts(pos_profile: str = None, status: str = None):
    """List payment receipts filtered by POS profile and status.

    Branch scoping is the same rule the write endpoints enforce
    (:func:`_has_receipt_branch_access`), because the rows carry customer names,
    invoice ids and transfer screenshots:

    * An explicit ``pos_profile`` the caller is not assigned to is refused with a
      ``PermissionError``. It used to be trusted as given, so anyone logged in
      could read any branch's receipts by naming the branch.
    * With no ``pos_profile``, the list is limited to the caller's branches, and
      a caller assigned to no branch gets an empty list. Empty used to mean "no
      filter" -- every receipt of every branch. This case returns ``[]`` rather
      than throwing because the Payment Receipts screen and the kanban badge
      call it with no profile, and "nothing for you" is the true answer there.

    Args:
        pos_profile: Filter by POS profile (optional)
        status: Filter by status: Unconfirmed/Confirmed/Changed (optional)

    Returns:
        list: List of payment receipt records
    """
    try:
        filters = {}

        if pos_profile:
            _ensure_receipt_branch_access(pos_profile)
            filters['pos_profile'] = pos_profile

        if status:
            filters['status'] = status
        else:
            filters['status'] = ['!=', RECEIPT_STATUS_CHANGED]

        # If pos_profile not specified, filter by the caller's own branches.
        if not pos_profile:
            accessible_profiles = _receipt_branch_scope()
            if not accessible_profiles:
                return []
            filters['pos_profile'] = ['in', accessible_profiles]

        receipts = frappe.get_all(
            'POS Payment Receipt',
            filters=filters,
            fields=list(RECEIPT_ROW_FIELDS),
            order_by='creation desc'
        )

        # Get invoice details for each receipt
        for receipt in receipts:
            _decorate_receipt_row(receipt)

        frappe.logger().info(f"Retrieved {len(receipts)} payment receipts")
        
        return receipts

    except FrappePermissionError:
        # A branch refusal is a 403 and says so; the catch-all below would
        # relabel it "Failed to list payment receipts", which reads as a bug.
        raise
    except Exception as e:
        frappe.logger().error(f"Failed to list payment receipts: {str(e)}")
        frappe.throw(f"Failed to list payment receipts: {str(e)}")


@frappe.whitelist()
def create_payment_receipt(sales_invoice: str, payment_method: str, amount: float, pos_profile: str):
    """Create a new payment receipt record.
    
    Args:
        sales_invoice: Sales Invoice name
        payment_method: Instapay or Mobile Wallet
        amount: Payment amount
        pos_profile: POS Profile name
    
    Returns:
        dict: Created receipt details
    """
    try:
        frappe.logger().info(f"Creating payment receipt for invoice {sales_invoice}")

        # Filing a receipt against a branch the caller is not assigned to is the
        # same hole as editing one; close it at the door.
        _ensure_receipt_branch_access(pos_profile)
        
        # Reuse only active receipts; changed receipts are audit history and should not block recreation.
        existing = frappe.get_all(
            'POS Payment Receipt',
            filters={
                'sales_invoice': sales_invoice,
                'status': ['!=', RECEIPT_STATUS_CHANGED],
            },
            fields=['name', 'payment_method'],
            order_by='creation desc',
            limit_page_length=20,
        )
        existing_name = next(
            (
                row.get('name')
                for row in existing
                if _normalize_receipt_method(row.get('payment_method')) == _normalize_receipt_method(payment_method)
            ),
            None,
        )

        if existing_name:
            frappe.logger().info(f"Receipt already exists: {existing_name}")
            return {
                'success': True,
                'receipt_name': existing_name,
                'message': 'Receipt already exists'
            }
        
        # Create new receipt
        receipt = frappe.get_doc({
            'doctype': 'POS Payment Receipt',
            'sales_invoice': sales_invoice,
            'payment_method': payment_method,
            'amount': amount,
            'pos_profile': pos_profile,
            'status': RECEIPT_STATUS_UNCONFIRMED,
            'uploaded_by': frappe.session.user
        })
        
        receipt.insert()
        frappe.db.commit()
        
        frappe.logger().info(f"Created payment receipt: {receipt.name}")
        
        return {
            'success': True,
            'receipt_name': receipt.name,
            'message': 'Receipt created successfully'
        }
    
    except FrappePermissionError:
        # A branch refusal is a 403 and says so. Letting it fall into the
        # catch-all below would relabel it "Failed to create payment receipt",
        # which reads as a bug rather than a permission boundary.
        raise
    except Exception as e:
        frappe.logger().error(f"Failed to create payment receipt: {str(e)}")
        frappe.throw(f"Failed to create payment receipt: {str(e)}")


@frappe.whitelist()
def upload_receipt_image(receipt_name: str, image_data: str, filename: str):
    """Upload (or replace) the receipt image for a payment receipt.

    Re-uploading is allowed while the receipt is still Unconfirmed; the
    superseded image file is deleted so only the current screenshot is kept.

    Args:
        receipt_name: POS Payment Receipt name
        image_data: Base64 encoded image data
        filename: Original filename

    Returns:
        dict: Upload result with file URL
    """
    try:
        frappe.logger().info(f"Uploading receipt image for {receipt_name}")
        
        # Get the receipt document
        receipt = frappe.get_doc('POS Payment Receipt', receipt_name)
        _ensure_receipt_branch_access(getattr(receipt, 'pos_profile', None))
        _ensure_receipt_image_editable(receipt)
        was_rejected = (
            str(getattr(receipt, 'status', '') or '').strip() == RECEIPT_STATUS_REJECTED
        )

        # Remember the previous image so it can be cleaned up after the swap.
        previous_files = _receipt_image_file_names(
            receipt.name,
            getattr(receipt, 'receipt_image', None),
            getattr(receipt, 'receipt_image_url', None),
        )

        # Decode base64 image
        if ',' in image_data:
            # Remove data:image/...;base64, prefix if present
            image_data = image_data.split(',')[1]
        
        image_bytes = base64.b64decode(image_data)
        
        # Create file document
        file_doc = frappe.get_doc({
            'doctype': 'File',
            'file_name': filename,
            'is_private': 0,
            'content': image_bytes,
            'attached_to_doctype': 'POS Payment Receipt',
            'attached_to_name': receipt_name,
            'attached_to_field': 'receipt_image'
        })
        file_doc.save()
        
        # Update receipt with image
        receipt.receipt_image = file_doc.file_url
        receipt.receipt_image_url = file_doc.file_url
        receipt.upload_date = frappe.utils.now()
        receipt.uploaded_by = frappe.session.user
        if was_rejected:
            # A rejection is a request for a better screenshot, not a dead end.
            # Leaving the row Rejected after a fresh upload keeps it out of the
            # manager's queue for ever, so the corrected proof re-enters it.
            receipt.status = RECEIPT_STATUS_UNCONFIRMED
            receipt.rejected_by = None
            receipt.rejected_date = None
            receipt.rejection_reason = None
        receipt.save()

        # The receipt no longer points at the old file, so it is safe to drop.
        _delete_receipt_image_files(
            [name for name in previous_files if name != file_doc.name]
        )

        frappe.db.commit()
        
        frappe.logger().info(f"Receipt image uploaded: {file_doc.file_url}")
        
        return {
            'success': True,
            'file_url': file_doc.file_url,
            'replaced': bool(previous_files),
            'status': str(getattr(receipt, 'status', '') or '').strip(),
            'reopened': was_rejected,
            'message': 'Image uploaded successfully'
        }
    
    except (FrappeValidationError, FrappePermissionError):
        raise
    except Exception as e:
        frappe.logger().error(f"Failed to upload receipt image: {str(e)}")
        frappe.throw(f"Failed to upload receipt image: {str(e)}")


@frappe.whitelist()
def remove_receipt_image(receipt_name: str):
    """Remove the uploaded image from a payment receipt.

    Only allowed while the receipt is Unconfirmed — once a manager has
    confirmed it, the screenshot is evidence and stays put. The receipt record
    itself is kept so the same row can be re-used for a fresh upload.

    Args:
        receipt_name: POS Payment Receipt name

    Returns:
        dict: Removal result
    """
    try:
        normalized_name = str(receipt_name or '').strip()
        if not normalized_name:
            frappe.throw(_('Payment receipt is required'))
        if not frappe.db.exists('POS Payment Receipt', normalized_name):
            frappe.throw(_('Payment receipt was not found'))

        receipt = frappe.get_doc('POS Payment Receipt', normalized_name)
        _ensure_receipt_branch_access(getattr(receipt, 'pos_profile', None))
        _ensure_receipt_image_editable(receipt)

        file_names = _receipt_image_file_names(
            receipt.name,
            getattr(receipt, 'receipt_image', None),
            getattr(receipt, 'receipt_image_url', None),
        )

        # Clear the doc first so nothing still links to the file being deleted.
        receipt.receipt_image = None
        receipt.receipt_image_url = None
        receipt.upload_date = None
        receipt.save()

        _delete_receipt_image_files(file_names)

        frappe.db.commit()

        frappe.logger().info(f"Receipt image removed: {normalized_name}")

        return {
            'success': True,
            'receipt_name': normalized_name,
            'message': 'Image removed successfully'
        }

    except (FrappeValidationError, FrappePermissionError):
        raise
    except Exception as e:
        frappe.logger().error(f"Failed to remove receipt image: {str(e)}")
        frappe.throw(f"Failed to remove receipt image: {str(e)}")


#: Shapes returned by ``services.delivery_handling.classify_receipt_collection``.
#: Spelled here as well so this module's site-less unit tests do not have to
#: import the service (which pulls in erpnext) just to name a branch.
_RECEIPT_COLLECTION_NONE = "none"
_RECEIPT_COLLECTION_COURIER_CASH = "courier_cash"
_RECEIPT_COLLECTION_SETTLED_CASH = "settled_cash"


def _classify_receipt_collection(invoice_name: str | None) -> dict:
    """Where this invoice's money sits, per ``services.delivery_handling``.

    Wrapped rather than imported at module scope for the same two reasons as
    :func:`_allowed_pos_profiles`: the service imports this module (so a
    top-level import here would be circular), and patching this one name is how
    a site-less test picks the shape it wants to exercise.
    """
    from jarz_pos.services.delivery_handling import classify_receipt_collection

    return classify_receipt_collection(invoice_name)


def _plan_receipt_collection(receipt) -> dict | None:
    """What confirming *receipt* still has to do about the money, or ``None``.

    ``None`` means a plain stamp is the whole job: the order is already paid
    into a real ledger, its collection was converted to cash, or this is not a
    transfer receipt at all.

    Only InstaPay and Wallet receipts collect, which is the whole of the rule
    the owner stated: a screenshot is uploaded only because somebody chose to
    pay by transfer, so a confirmed one means the money is in the bank.

    Degrades to ``None`` on any failure, exactly as the narrower probe it
    replaced did. Confirming is the last step of a real payment, and failing it
    because a classifier could not read the ledger would be worse than the
    stamp-only behaviour this whole change exists to improve on.
    """
    if _normalize_receipt_method(getattr(receipt, "payment_method", None)) not in (
        "instapay",
        "wallet",
    ):
        return None
    try:
        plan = _classify_receipt_collection(getattr(receipt, "sales_invoice", None))
    except Exception:
        return None
    if not plan or str(plan.get("shape") or _RECEIPT_COLLECTION_NONE) == _RECEIPT_COLLECTION_NONE:
        return None
    return plan


def _confirm_receipt_record(receipt) -> None:
    """Stamp a receipt Confirmed. Moves no money -- see :func:`confirm_receipt`."""
    # Confirming a previously rejected receipt is deliberately allowed: it
    # is the only way back from a rejection made in error, and the
    # alternative -- refusing -- would strand the receipt in a state with no
    # exit and push the branch to upload a duplicate image instead.
    # The stale rejection stamp is cleared so the record does not read as
    # both rejected and confirmed.
    if receipt.status == RECEIPT_STATUS_REJECTED:
        receipt.rejected_by = None
        receipt.rejected_date = None
        receipt.rejection_reason = None

    receipt.status = RECEIPT_STATUS_CONFIRMED
    receipt.confirmed_by = frappe.session.user
    receipt.confirmed_date = frappe.utils.now()
    receipt.save()


@frappe.whitelist()
def confirm_receipt(receipt_name: str):
    """Confirm a payment receipt -- and, when one is owed, collect the money.

    Confirming used to be a pure stamp: the row went Confirmed and nothing else
    happened, because the Payment Entry was posted only by
    ``confirm_online_payment`` on the reconciliation screen. Two screens, one
    verb, and the receipts list was the one staff actually used. On 2026-09-09
    production carried four orders -- 17987, 18039, 18048, 18053, 2,460 EGP --
    whose proof of transfer a manager had looked at and confirmed while the
    invoice stayed fully unpaid. Nobody was told; the order simply read
    "confirmed" on one screen and "awaiting payment" on the other.

    So confirming finishes the job. Which job depends on where the money
    currently sits, which :func:`_plan_receipt_collection` decides:

    * still owed on Debtors (awaiting a transfer, or simply not yet dispatched)
      -> ``confirm_online_payment`` validates the screenshot, posts
      ``DR Bank/Instapay . CR Debtors`` and flips the invoice to Payment
      Confirmed;
    * already dispatched as cash, rider not settled ->
      ``change_payment_collection_method`` moves it off Courier Outstanding into
      the bank, so the courier is no longer recorded as holding it;
    * already settled as cash -> refused, because the branch till has counted
      that money and only a manual correction can move it;
    * anything else -- paid at the counter, cancelled, converted to cash, or a
      receipt for a method that takes no transfer -- keeps the plain stamp.

    The second case is the 2026-09-15 one (orders 17450 and 17453). A Woo order
    arrives declared Cash whatever the customer later does, so an order paid by
    transfer and dispatched before a manager confirmed it had its whole
    receivable moved onto the rider. Confirming the screenshot then stamped a
    row and moved nothing, leaving the courier settlement asking him for money
    the customer had already sent to the bank.

    Args:
        receipt_name: POS Payment Receipt name

    Returns:
        dict: Confirmation result
    """
    try:
        frappe.logger().info(f"Confirming receipt {receipt_name}")

        receipt = frappe.get_doc('POS Payment Receipt', receipt_name)
        _ensure_payment_receipt_confirm_access(getattr(receipt, 'pos_profile', None))

        if receipt.status == RECEIPT_STATUS_CHANGED:
            frappe.throw('Changed payment receipts cannot be confirmed')

        # Deliberately BEFORE the already-confirmed short circuit. A receipt that
        # is Confirmed while its invoice's money is still uncollected is
        # precisely the stuck state described above, and returning "already
        # confirmed" there is what made it permanent -- pressing the button
        # again has to be the way out.
        plan = _plan_receipt_collection(receipt)
        if plan:
            image_url = str(
                getattr(receipt, 'receipt_image_url', None)
                or getattr(receipt, 'receipt_image', None)
                or ''
            ).strip()
            if not image_url:
                # Confirming now moves money, so it needs the proof first. The
                # receipt stays Unconfirmed and listed, which is the honest
                # state: nobody has evidenced this transfer yet.
                frappe.throw(
                    "Upload the transfer screenshot before confirming - confirming "
                    "this receipt records the payment against the invoice."
                )

            invoice_name = plan.get('invoice')
            # The receipt's own profile first, then the INVOICE's branch. A
            # legacy receipt may carry no profile at all -- branch access
            # deliberately lets those through (see _has_receipt_branch_access) --
            # and the guarded endpoints below both refuse an empty one.
            profile = str(
                getattr(receipt, 'pos_profile', None) or plan.get('pos_profile') or ''
            ).strip()
            shape = plan.get('shape')

            # Both branches call the GUARDED endpoints, never the services
            # underneath them: those wrappers add branch scope on the INVOICE
            # plus an open shift. Calling a service directly would make this a
            # second, weaker door to the same money -- bookable outside any
            # shift, on another branch's order, by whoever filed the receipt.
            if shape == _RECEIPT_COLLECTION_SETTLED_CASH:
                # The rider already settled, so the branch till was debited with
                # cash nobody handed over. Nothing here can undo that safely:
                # the money has been counted at a shift close. Refuse loudly
                # rather than stamp the receipt and leave the books wrong.
                frappe.throw(
                    "This order was settled with the courier as cash, so the branch "
                    "till already counted this money. Confirming cannot move it - "
                    "post a correction from the bank to the branch cash account, "
                    "then confirm."
                )

            if shape == _RECEIPT_COLLECTION_COURIER_CASH:
                # Dispatched down the cash path before anyone confirmed the
                # transfer: the receivable sits on Courier Outstanding against
                # the rider. Moving it to the bank is exactly what a collection
                # method change does, so use that rather than a second Payment
                # Entry -- which would credit Debtors twice.
                from jarz_pos.api.couriers import change_payment_collection_method

                result = change_payment_collection_method(
                    invoice_name,
                    str(getattr(receipt, 'payment_method', None) or '').strip(),
                    profile,
                    receipt_name=receipt.name,
                    notes='Confirmed transfer receipt {0}'.format(receipt.name),
                    # DERIVED FROM THE RECEIPT, never left to the service to
                    # mint. Both replay guards key on this token -- the stored
                    # one on the Courier Transaction and the Journal Entry title
                    # dedup -- so a random token disarms both, and two confirms
                    # racing on one receipt post the bank/Courier-Outstanding
                    # entry twice. The invoice row lock does not save us: the
                    # loser re-reads the courier row from its own REPEATABLE
                    # READ snapshot and still sees the money unmoved.
                    idempotency_token='RCPT-{0}'.format(receipt.name),
                ) or {}
                # The whitelisted wrapper nests the service's answer under
                # ``data``; the bare service returns it flat.
                result = result.get('data') or result
                # The collection change validates the receipt but does not stamp
                # it; this is still a confirmation, so record who confirmed.
                _confirm_receipt_record(
                    frappe.get_doc('POS Payment Receipt', receipt.name)
                )
                frappe.db.commit()
                return {
                    # "payment recorded" is load-bearing wording, not prose: the
                    # app's ``confirmRecordsPayment`` reads it (or a
                    # ``payment_entry``) to tell a confirmation that moved money
                    # from one that only stamped a row. This branch moves money
                    # with a Journal Entry rather than a Payment Entry, so
                    # without it an already-shipped client would report the
                    # collection as unconfirmed. ``payment_recorded`` is the
                    # explicit flag for clients from here on.
                    'success': True,
                    'payment_recorded': True,
                    'message': 'Receipt confirmed and payment recorded from the courier',
                    'invoice': invoice_name,
                    'journal_entry': result.get('journal_entry'),
                    'courier_transaction': result.get('courier_transaction'),
                    'collection_change_mode': result.get('collection_change_mode'),
                }

            from jarz_pos.api.couriers import confirm_online_payment

            result = confirm_online_payment(
                invoice_name,
                profile,
                receipt_name=receipt.name,
            ) or {}
            return {
                'success': True,
                'payment_recorded': True,
                'message': 'Receipt confirmed and payment recorded',
                'invoice': invoice_name,
                'payment_entry': result.get('payment_entry'),
                'payment_confirmation_status': result.get('payment_confirmation_status'),
            }

        if receipt.status == RECEIPT_STATUS_CONFIRMED:
            return {
                'success': True,
                'message': 'Receipt already confirmed'
            }

        _confirm_receipt_record(receipt)

        frappe.db.commit()
        
        frappe.logger().info(f"Receipt confirmed: {receipt_name}")
        
        return {
            'success': True,
            'message': 'Receipt confirmed successfully'
        }
    
    except FrappePermissionError:
        raise
    except Exception as e:
        frappe.logger().error(f"Failed to confirm receipt: {str(e)}")
        frappe.throw(f"Failed to confirm receipt: {str(e)}")


@frappe.whitelist()
def get_accessible_pos_profiles():
    """Get list of POS profiles accessible to current user.
    
    Returns:
        list: List of POS profile names
    """
    try:
        from jarz_pos.api.manager import _current_user_allowed_profiles
        
        profile_names = _current_user_allowed_profiles()
        
        return profile_names
    
    except Exception as e:
        frappe.logger().error(f"Failed to get accessible profiles: {str(e)}")
        frappe.throw(f"Failed to get accessible profiles: {str(e)}")


@frappe.whitelist()
def reject_receipt(receipt_name: str, reason: str):
    """Turn down a payment receipt, recording who rejected it and why.

    The counterpart :func:`confirm_receipt` shipped without. A receipt whose
    screenshot showed the wrong amount, a different order, or nothing at all had
    exactly one available action — Confirm — so the only way to register "this
    is not proof of payment" was to leave it alone. That reads identically to
    "nobody has looked yet", which is the state a branch chases the customer
    over.

    A rejected receipt stays visible rather than being deleted or marked
    ``Changed``: the uploader has to see the reason to send a correct one, and a
    receipt that vanishes teaches people to re-upload the same image.

    Rejecting moves no money. ``confirm_receipt`` does not post the Payment
    Entry either — :func:`jarz_pos.api.couriers.confirm_online_payment` does,
    against a confirmed receipt — so the invoice simply stays unpaid, which is
    the true state of the world.
    """
    receipt_name = str(receipt_name or "").strip()
    if not receipt_name:
        frappe.throw(_("Receipt name is required."))

    reason = str(reason or "").strip()
    if not reason:
        frappe.throw(_("A reason is required to reject a payment receipt."))

    receipt = frappe.get_doc("POS Payment Receipt", receipt_name)
    _ensure_payment_receipt_confirm_access(getattr(receipt, "pos_profile", None))

    if receipt.status == RECEIPT_STATUS_CONFIRMED:
        frappe.throw(
            _(
                "Receipt {0} is already confirmed. If the payment did not arrive, reverse "
                "the payment entry on the invoice instead."
            ).format(receipt_name)
        )
    if receipt.status == RECEIPT_STATUS_REJECTED:
        return {
            "success": True,
            "message": _("Receipt already rejected"),
            "status": RECEIPT_STATUS_REJECTED,
        }

    receipt.status = RECEIPT_STATUS_REJECTED
    receipt.rejected_by = frappe.session.user
    receipt.rejected_date = frappe.utils.now()
    receipt.rejection_reason = reason
    receipt.save(ignore_permissions=True)

    return {
        "success": True,
        "message": _("Receipt rejected"),
        "status": RECEIPT_STATUS_REJECTED,
        "reason": reason,
    }
