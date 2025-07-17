# Jarz POS – Backend API Reference

This document lists the HTTP/JS API endpoints exposed by the *headless* Jarz
POS backend.  All routes are **whitelisted** (`frappe.whitelist`) which means
they can be invoked from browser clients, mobile apps, or third-party systems
via `frappe.call` / `POST /api/method/<dot.path>`.

> **Note**
> The fully-fledged business logic is still implemented in
> `jarz_pos.jarz_pos.page.custom_pos.custom_pos`.  The dedicated *API* package
> (`jarz_pos.jarz_pos.api`) provides *thin wrappers* that forward the calls to
> that proven implementation while giving external clients a clear, stable
> import / REST path that is not coupled to the legacy “Page” controller.

---

## 1. Invoices – `jarz_pos.jarz_pos.api.invoices`

| Method | Description | Arguments |
|--------|-------------|-----------|
| `create_sales_invoice` | Create & submit a Sales Invoice from cart JSON | `cart_json`, `customer_name`, `pos_profile_name`, `delivery_charges_json?`, `required_delivery_datetime?` |
| `pay_invoice` | Record a full payment for an existing invoice | `invoice_name`, `payment_mode`, `pos_profile?` |

Endpoint format for REST calls:
```
POST /api/method/jarz_pos.jarz_pos.api.invoices.create_sales_invoice
```

---

## 2. Couriers – `jarz_pos.jarz_pos.api.couriers`

| Method | Description |
|--------|-------------|
| `mark_courier_outstanding` | Allocate invoice outstanding to *Courier Outstanding* ledger |
| `pay_delivery_expense` | Pay the courier’s delivery expense in cash (Journal Entry) |
| `courier_delivery_expense_only` | Record delivery expense without paying the invoice amount |
| `get_courier_balances` | Aggregate per-courier outstanding balances with details |
| `settle_courier` | Bulk settle *all* outstanding transactions for a courier |
| `settle_courier_for_invoice` | Settle courier outstanding for a single invoice |

REST prefix example:
```
POST /api/method/jarz_pos.jarz_pos.api.couriers.settle_courier
```

---

### Conventions

* All monetary values are returned as **floats** in the company currency.
* Timestamps use the site’s timezone unless stated otherwise.
* Errors are raised via `frappe.throw` → returned to the client as HTTP 417.

---

Generated 2025-07-17 