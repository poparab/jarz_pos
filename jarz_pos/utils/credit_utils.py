"""One definition of "this order was taken ON ACCOUNT", shared by every reader.

WHY THIS MODULE EXISTS
----------------------
A credit order is identified in two different ways, and until this module they
were spelled differently in five places:

* ``Sales Invoice.custom_payment_method == "Credit"`` — what the cashier chose.
  **This column is MUTABLE after submit.** ``change_payment_collection_method``
  rewrites it on every collection-method change, and the ``unpaid_online_retarget``
  branch of that flow posts NO voucher — the outstanding stays exactly where it
  was. So a manager tapping "Change collection method -> Instapay" on a credit
  card used to make a real debt vanish from ``api/credit.get_credit_ledger``,
  free that much of the shop's limit, and leave ``record_credit_payment`` unable
  to allocate against it. The money never moved; only the label did.
* ``Sales Invoice.custom_credit_terms_days > 0`` — the FROZEN stamp written once
  by ``invoice_creation._apply_credit_terms``. Nothing rewrites it, ever. It is
  permanent provenance that this order was taken on credit.

Any query that has to find "the credit debts" must therefore match EITHER, and
every such query must match the same way. :func:`credit_invoice_or_filters` is
that one way; :func:`is_credit_payment_method` /
:func:`is_credit_intent_doc` are the same agreement at the value level, for the
gates that run before an invoice exists.

Deliberately dependency-free apart from ``frappe`` itself (imported lazily, only
where a query is built), so the two service modules that cannot import each
other — ``delivery_handling`` and ``settlement_strategies`` — can both use it
without an import cycle.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

#: Value of ``Sales Invoice.custom_payment_method`` that means "on account".
#: Matches the option added to ``Sales Invoice-custom_payment_method`` in
#: ``fixtures/custom_field.json``.
CREDIT_PAYMENT_METHOD = "Credit"

#: Fieldname of the frozen, IMMUTABLE stamp written at creation.
CREDIT_TERMS_FIELD = "custom_credit_terms_days"

#: Normalized tokens that mean "taken on account". ``onaccount`` is accepted
#: because it is how the same intent arrives from clients and imports that do
#: not use the Select option verbatim. Kept as a set so a synonym can be added
#: in exactly one place.
CREDIT_INTENT_TOKENS = frozenset({"credit", "onaccount"})


def normalize_payment_method_token(value: Any) -> str:
    """Fold a payment-method label to the token form the predicates compare on.

    Case, surrounding whitespace, inner spaces and underscores are all noise:
    ``"On Account"``, ``"on_account"`` and ``"ONACCOUNT"`` are the same choice.
    Deliberately does NOT route through
    ``delivery_handling._normalize_collection_method``, which throws on any
    method outside its four and would push "Invalid collection method" into the
    user's message log as a side effect of merely asking a question.
    """
    return str(value or "").strip().lower().replace(" ", "").replace("_", "")


def is_credit_payment_method(value: Any) -> bool:
    """True when this payment-method VALUE means "on account".

    Use this for gates that run before there is a document to inspect — most
    importantly the creation gate in ``invoice_creation.create_pos_invoice``,
    which used to compare ``== "Credit"`` exactly. An order created as
    ``"on account"`` then skipped ``_apply_credit_terms`` entirely (no permission
    check, no limit, no due date, no stamp) yet still routed to the credit
    handler at dispatch, because the dispatch predicates normalise and this one
    did not.
    """
    token = normalize_payment_method_token(value)
    if not token:
        return False
    return token in CREDIT_INTENT_TOKENS


def is_credit_intent_doc(inv: Any) -> bool:
    """True when this invoice DOCUMENT was taken on account.

    Self-contained and DB-free on purpose: it is read inside dispatch paths that
    unit tests drive with mocks.
    """
    try:
        raw = (
            inv.get("custom_payment_method")
            if hasattr(inv, "get")
            else getattr(inv, "custom_payment_method", None)
        )
    except Exception:
        raw = getattr(inv, "custom_payment_method", None)
    return is_credit_payment_method(raw)


def has_credit_terms_field(doctype: str = "Sales Invoice") -> bool:
    """Whether the frozen-stamp column exists on this bench.

    Seeded by ``setup/credit_terms.py`` + the fixture, so a site that has not
    migrated yet has no such column and a query naming it would raise. Every
    caller degrades to the payment-method-only match rather than breaking the
    screen it feeds.
    """
    import frappe

    try:
        return bool(frappe.get_meta(doctype).get_field(CREDIT_TERMS_FIELD))
    except Exception:
        return False


def credit_invoice_or_filters(doctype: str = "Sales Invoice") -> Optional[List[List[Any]]]:
    """``or_filters`` matching every invoice that WAS taken on credit.

    Pass alongside (never inside) the caller's own ``filters``::

        frappe.get_all(
            "Sales Invoice",
            filters={"docstatus": 1, "outstanding_amount": [">", 0.005]},
            or_filters=credit_invoice_or_filters(),
            ...
        )

    The OR is the whole point, and it is not defensive programming: the payment
    method is a MUTABLE column that a collection-method change rewrites without
    moving a single pound, while ``custom_credit_terms_days`` is a stamp written
    once at creation and never touched again. Matching only the method loses the
    debt the moment somebody relabels the card; matching only the stamp loses
    orders created before the stamp existed. Matching either is what makes
    ``api/credit``, ``api/kanban`` and ``invoice_creation`` agree on the same set
    of debts.

    Returns ``None`` when the stamp column is missing, which is the caller's
    signal to fall back to the payment-method filter on its own.
    """
    if not has_credit_terms_field(doctype):
        return None
    return [
        [doctype, "custom_payment_method", "=", CREDIT_PAYMENT_METHOD],
        [doctype, CREDIT_TERMS_FIELD, ">", 0],
    ]


def apply_credit_invoice_match(
    filters: Dict[str, Any], doctype: str = "Sales Invoice"
) -> Optional[List[List[Any]]]:
    """Add the credit match to *filters*, returning the ``or_filters`` to pass.

    Convenience for the common shape: when the stamp column exists the match is
    an OR (returned, so the caller hands it to ``get_all``); when it does not,
    the payment-method equality is written straight into *filters* and ``None``
    is returned. Mutates *filters* in place in the fallback case only.
    """
    or_filters = credit_invoice_or_filters(doctype)
    if or_filters is None:
        filters["custom_payment_method"] = CREDIT_PAYMENT_METHOD
    return or_filters
