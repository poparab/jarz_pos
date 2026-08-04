# Courier App — Frozen Contracts (W0)

**Status:** NORMATIVE. Frozen 2026-08-05.
**Companion to:** `COURIER_APP_SPEC.md` (scope) and the parallel build plan (execution).

Every lane codes against this document, not against another lane's code. Renaming anything here
after `bench migrate` has run on staging costs a patch file, not an edit — so changes require
re-freezing and notifying every lane.

---

## 1. DO NOT EDIT — the Sales Invoice state options

Settled 2026-08-05. The "Returned Kanban column" work landed, so the committed value on `main` is
now **seven** options and `test_state_options_frozen.py` asserts exactly this string:

```
Recieved\nIn Progress\nReady\nOut for Delivery\nDelivered\nCancelled\nReturned
```

Stored on `Sales Invoice.custom_sales_invoice_state` as a Select.

**The misspelling "Recieved" is live production data.** It is the stored value in every historical
row, Kanban columns are derived from these options at runtime, and 274 references across 44 files
read them. Correcting the spelling is a data migration, not a typo fix. **This project does not
touch it.**

**No new state is added by this project.** A failed delivery stays `Out for Delivery` and is
expressed through `custom_delivery_failure_reason` + `custom_delivery_attempt_no`.

Guard: `jarz_pos/tests/test_state_options_frozen.py` asserts the exact 7-option string.
The string `custom_sales_invoice_state` must not appear in any diff to
`fixtures/custom_field.json` for the duration of this project.

---

## 2. Sales Invoice — delivery outcome fields (A1)

Anchor chain begins after the existing `custom_courier_party`. Each field's `insert_after` is the
one above it, so the block stays contiguous.

| # | fieldname | fieldtype | insert_after | notes |
|---|---|---|---|---|
| 1 | `custom_delivery_sequence` | Int | `custom_courier_party` | Stop order within the courier's run; 0 = unsequenced |
| 2 | `custom_arrived_at` | Datetime | `custom_delivery_sequence` | |
| 3 | `custom_delivered_at` | Datetime | `custom_arrived_at` | |
| 4 | `custom_delivery_latitude` | Float (precision 6) | `custom_delivered_at` | Where the courier stood at POD |
| 5 | `custom_delivery_longitude` | Float (precision 6) | `custom_delivery_latitude` | |
| 6 | `custom_delivery_accuracy_m` | Float (precision 2) | `custom_delivery_longitude` | GPS accuracy radius in metres |
| 7 | `custom_delivery_attempt_no` | Int, default `0` | `custom_delivery_accuracy_m` | Incremented on every failure |
| 8 | `custom_delivery_failure_reason` | Link → `Delivery Failure Reason` | `custom_delivery_attempt_no` | Cleared on successful delivery |

**Every one of these MUST carry `"allow_on_submit": 1`.** They are written on submitted invoices;
without it, Frappe rejects the write and `update_submitted_sales_invoice_fields` filters the field
out and returns `False` **with no error** — a courier's "Delivered" tap would vanish silently.

**Every one of these MUST carry `"no_copy": 1`.** A delivery outcome must not carry over to an
amended invoice.

Fixture entries additionally require `"module": "jarz pos"` and
`"name": "Sales Invoice-<fieldname>"`. A `name` that does not match exactly will be **deleted** by
`cleanup.remove_colliding_custom_fields_for_fixtures` on the next migrate.

---

## 3. Address — geo fields (A4)

Anchor chain begins after the core Frappe field `gps_location` (a free-text `Data` field, last in
the Address layout; we do not use it — it is unstructured and not numerically queryable).

| # | fieldname | fieldtype | insert_after | notes |
|---|---|---|---|---|
| 1 | `custom_latitude` | Float (precision 6) | `gps_location` | |
| 2 | `custom_longitude` | Float (precision 6) | `custom_latitude` | |
| 3 | `custom_geo_source` | Select (see §4) | `custom_longitude` | The label |
| 4 | `custom_geo_confidence` | Int, read-only | `custom_geo_source` | The rank — derived, never written independently |
| 5 | `custom_geo_accuracy_m` | Float (precision 2) | `custom_geo_confidence` | |
| 6 | `custom_geo_verified_on` | Datetime, read-only | `custom_geo_accuracy_m` | Set when source reaches `courier_verified` |

Fixture entries require `"module": "jarz pos"` and `"name": "Address-<fieldname>"`.

**`services/geo_resolution.py` is the sole writer of all six.** Nothing else may write them —
not the Woo sync, not the Desk form, not a patch. `custom_geo_confidence` is always written in the
same call as `custom_geo_source`; a test asserts the two never disagree.

### Never rewrite `address_line2`

Maps links live in `address_line2` today as `"Location: <url>"` (`api/customer.py:720`). The
backfill **reads** it and **copies** coordinates out. It must never move, clear, or reformat it —
that field is part of the Woo address-dedup signature (forks duplicate Addresses) and the Woo
outbound-push trigger set (fans out a customer + invoice sync per record).

The `custom_*` fields above are safe by construction: Woo's Address outbound hooks are field-gated
on a list containing no `custom_*` field, so a geo-only save never reaches WooCommerce. **That
safety disappears the moment a text edit is bundled into the same save.**

---

## 4. Confidence ladder — integer ranks

```python
CONFIDENCE_RANK = {
    "territory_centroid": 10,
    "pos_link":           20,
    "customer_pin":       30,
    "courier_verified":   40,
    "manual_override":    50,
}
```

`custom_geo_source` Select options (leading blank is intentional — Frappe convention for optional):

```
\nterritory_centroid\npos_link\ncustomer_pin\ncourier_verified\nmanual_override
```

**Ranks, never string comparison.** Alphabetical ordering puts `courier_verified` below
`customer_pin` and `pos_link` above `manual_override` — the "never downgrade" rule would silently
invert.

**The rule:** a write is accepted only when `incoming_rank >= current_rank`. Equal ranks are
accepted (a fresher pin of the same class wins). Lower ranks are silently ignored, not an error —
a routine Woo re-sync must not fail because the address already has a better pin.

`manual_override` sits highest deliberately: an authorised human with context must be able to fix a
bad pin and have it stick against consensus. It requires a manager role and is logged.

---

## 5. Service signatures (A3)

All three live in `jarz_pos/services/courier_delivery.py`. All return the
`{"success": bool, ...}` envelope and never raise to the caller except `frappe.PermissionError`.

```python
def mark_invoice_arrived(
    invoice_id: str,
    *,
    latitude: float | None = None,
    longitude: float | None = None,
    accuracy_m: float | None = None,
    request_id: str | None = None,
) -> dict: ...

def mark_invoice_delivered(
    invoice_id: str,
    *,
    latitude: float | None = None,
    longitude: float | None = None,
    accuracy_m: float | None = None,
    collected_amount: float | None = None,
    recipient_name: str | None = None,
    is_mocked: bool = False,
    request_id: str | None = None,
) -> dict: ...

def mark_invoice_failed(
    invoice_id: str,
    *,
    failure_reason: str,          # Delivery Failure Reason.name — required
    latitude: float | None = None,
    longitude: float | None = None,
    accuracy_m: float | None = None,
    notes: str | None = None,
    request_id: str | None = None,
) -> dict: ...
```

### Mandatory invariants

1. **Meta assertion first.** Each function asserts its target fields exist via
   `frappe.get_meta("Sales Invoice").get_field(...)` and `frappe.throw`s if not. Without this a
   pre-migration deployment loses writes silently.
2. **All writes go through `delivery_handling.update_submitted_sales_invoice_fields`.** Never
   `frappe.db.set_value` on a submitted invoice.
3. **Dual idempotency.** A deterministic composite token `f"{invoice_id}::delivered"` (immune to
   device reinstall) **plus** the caller-supplied `request_id` replay guard (handles offline
   retry of the same tap). Both are required; `request_id` alone dies when a courier reinstalls.
4. **Failure never changes the state string.** It sets `custom_delivery_failure_reason` and
   increments `custom_delivery_attempt_no`. A test asserts the state field is untouched.
5. **Access gate, in order:** `frappe.has_permission("Sales Invoice", ptype="write", throw=True)`
   → `ensure_profile_scoped_invoice_access(...)` → `ensure_open_shift_for_invoice(...)`.
6. **Feature flag read in the service, not the API** — mirroring
   `services/invoice_return.returns_enabled()`.
7. **Realtime via `utils/realtime.publish_invoice_event` only.** Never `frappe.publish_realtime`
   directly — a bare call broadcasts site-wide.

---

## 6. Delivery Failure Reason — seed set

Doctype `Delivery Failure Reason`, seeded create-only by `setup/courier_setup.py` on
`after_migrate`. Fields: `code` (Data, unique), `label_en`, `label_ar`, `next_action`
(Select: `Reschedule\nReturn\nCancel`), `is_active` (Check, default 1).

| code | label_en | label_ar | next_action |
|---|---|---|---|
| `CUSTOMER_UNREACHABLE` | Customer unreachable | العميل لا يرد | Reschedule |
| `CUSTOMER_REFUSED` | Customer refused the order | العميل رفض الطلب | Return |
| `WRONG_ADDRESS` | Wrong or incomplete address | العنوان خطأ أو ناقص | Reschedule |
| `POSTPONED_BY_CUSTOMER` | Customer asked to postpone | العميل طلب التأجيل | Reschedule |
| `AREA_INACCESSIBLE` | Area inaccessible | المنطقة يصعب الوصول إليها | Reschedule |
| `PAYMENT_UNAVAILABLE` | Customer could not pay | العميل لا يستطيع الدفع | Return |

Seeder must be create-only (`frappe.db.exists` before insert) and must swallow exceptions — it runs
inside the shared `bench migrate` that `jarz_pos` also depends on.

---

## 7. WebSocket events

Appended to `jarz_pos/constants.py` `WS_EVENTS`, then **that file is frozen** for this project.

```python
COURIER_STOP_ARRIVED     = "jarz_pos_courier_stop_arrived"
COURIER_STOP_DELIVERED   = "jarz_pos_courier_stop_delivered"
COURIER_STOP_FAILED      = "jarz_pos_courier_stop_failed"
COURIER_DUTY_CHANGED     = "jarz_pos_courier_duty_changed"
COURIER_DEPOSIT_DECLARED = "jarz_pos_courier_deposit_declared"
ADDRESS_PIN_UPDATED      = "jarz_pos_address_pin_updated"
```

Mirrored in `jarz_pos_mobile/jarz_pos/lib/src/core/constants/ws_events.dart`.

**The invariant is Python ⊆ Dart for events Flutter listens to — not set equality.** The two sets
already differ today (`TRIP_CREATED`/`TRIP_OFD`/`TRIP_COMPLETED` and the `CUSTOM_SHIPPING_*` family
have no Dart counterpart; `newPosInvoice`/`posProfileUpdate`/`itemStockUpdate`/`roomPosUpdates` have
no Python counterpart). Do not write an equality test — it fails on day one.

---

## 8. API conventions

Template: `jarz_pos/api/returns.py`. Every endpoint module follows it exactly.

- `from __future__ import annotations`, full type hints, module docstring explaining the *why*.
- A module-private `_ensure_<feature>_permission()` as the **first statement** of every endpoint.
- `@frappe.whitelist(allow_guest=False)` explicit on every endpoint.
- Return `{"success": True, ...}` / `{"success": False, "error": str}`.
- `except frappe.PermissionError: raise` **before** the generic handler, so permission failures
  surface as real 403s rather than being flattened into a success envelope.
- Feature trio where applicable: **preview** (read-only) / **execute** (write) / **dry-run**
  (resolve and balance, write nothing) — the dry-run exists so staging harnesses can sweep
  production clones.
- Business logic lives in `services/`; the API layer stays thin transport.

---

## 9. `jarz_courier` boundary

- `required_apps = ["jarz_pos"]`. One-way: `jarz_courier` may import `jarz_pos`; `jarz_pos` and
  `jarz_woocommerce_integration` must never import `jarz_courier`.
- **Never writes GL, never creates a Journal Entry, never inserts a `Courier Transaction`.**
  Reads freely; writes through `jarz_pos` service functions. Enforced by
  `jarz_courier/tests/test_no_gl_writes.py`, which AST-greps the app and must be listed in the
  `MODULES` array of its CI workflow.
- **Declares zero Custom Fields on any doctype `jarz_pos` touches.** `jarz_pos`'s collision cleanup
  deletes fields whose `name` differs; whichever app migrates second would win and silently drop
  the other's field. All shared schema is owned by lane P1, exclusively.
- Every migrate hook swallows exceptions — a raising seeder aborts the shared `bench migrate`.

---

## 10. Release-classification trap for the POS Flutter app

`classify_mobile_release.ps1` excludes `lib/src/core/*`, `lib/src/data/*`, `lib/src/domain/*`,
`lib/src/services/*` and `lib/main.dart` from Shorebird patch candidates. Anything under those
paths is a mobile runtime path that is *not* patchable, so it falls through to **`full_apk`** —
a full APK build plus Firebase App Distribution to the POS tester list.

`lib/src/core/constants/ws_events.dart` is in that set. A push to `main` touching only that file
ships a full APK to POS staff for a constants-only change.

**Rule for the courier lanes:** never push a `lib/src/core/*` change on its own. Batch it with the
feature that actually consumes it, so one release carries something real.
