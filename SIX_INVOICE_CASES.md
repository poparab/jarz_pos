# Six Invoice Settlement Cases - Business Logic Documentation

This document explains the 6 comprehensive invoice settlement cases supported by Jarz POS, covering all business scenarios for Point of Sale operations with delivery management.

## Overview

The Jarz POS system supports 6 distinct invoice settlement cases that handle different combinations of payment timing, settlement timing, and special flows:

### Core 4 Cases (Standard Customer Flow)
1. **Unpaid + Settle Now** - Courier collects, immediate settlement
2. **Unpaid + Settle Later** - Courier collects, deferred settlement  
3. **Paid + Settle Now** - Branch collected, immediate courier fee
4. **Paid + Settle Later** - Branch collected, deferred courier fee

### Special 2 Cases
5. **Sales Partner Flow** - Online payment with partner routing
6. **Pickup Flow** - No delivery, no courier

---

## Case 1: Unpaid + Settle Now

**Business Scenario:** Customer hasn't paid at invoice creation. Courier collects full amount from customer. Branch settles with courier immediately (same day).

### Invoice Creation
- `is_pos = 1`
- `docstatus = 1` (submitted)
- `outstanding_amount = grand_total`
- No Payment Entry at creation
- Normal tax and delivery charges applied

### Settlement Flow (On Out For Delivery)
1. **Payment Entry Created:**
   - DR: Accounts Receivable (Customer)
   - CR: POS Cash Account
   - Amount: Full invoice total
   - Purpose: Record customer payment collection

2. **Journal Entry Created:**
   - DR: Freight and Forwarding Expense
   - CR: POS Cash Account
   - Amount: Shipping expense only
   - Purpose: Immediate courier settlement

3. **Courier Transaction:**
   - Status: Settled
   - Amount: Full order amount
   - Shipping: Expense amount
   - Type: Immediate settlement

4. **Delivery Note:**
   - Created and submitted
   - Links to Sales Invoice
   - Updates stock ledger

### Accounting Result
- Branch has net cash = (Order Total - Shipping Expense)
- Courier has been paid shipping fee
- Invoice fully settled
- Stock updated via Delivery Note

---

## Case 2: Unpaid + Settle Later

**Business Scenario:** Customer hasn't paid at invoice creation. Courier will collect full amount from customer. Branch will settle with courier later (future date).

### Invoice Creation
- `is_pos = 1`
- `docstatus = 1` (submitted)
- `outstanding_amount = grand_total`
- No Payment Entry at creation
- Normal tax and delivery charges applied

### Settlement Flow (On Out For Delivery)
1. **No Payment Entry** (invoice remains unpaid in system)

2. **No Journal Entry** (settlement deferred)

3. **Courier Transaction Created:**
   - Status: Unsettled
   - Amount: Full order amount
   - Shipping: Expense amount
   - Type: Outstanding collection

4. **Delivery Note:**
   - Created and submitted
   - Links to Sales Invoice
   - Updates stock ledger

### Later Settlement (settle_single_invoice_paid)
When branch settles with courier:

1. **Journal Entry Created:**
   - If Order Amount >= Shipping Expense:
     - DR: Cash (Order - Expense)
     - DR: Creditors (Expense)
     - CR: Courier Outstanding (Order)
   
   - If Shipping Expense > Order Amount:
     - DR: Creditors (Expense)
     - CR: Courier Outstanding (Order)
     - CR: Cash (Expense - Order)

2. **Courier Transaction Updated:**
   - Status: Settled

### Accounting Result
- Invoice tracked as outstanding until settlement
- Courier Outstanding account tracks liability
- Settlement reconciles all parties

---

## Case 3: Paid + Settle Now

**Business Scenario:** Customer paid at invoice creation (branch has cash). Courier delivers but doesn't collect. Branch pays courier shipping fee immediately.

### Invoice Creation
- `is_pos = 1`
- `docstatus = 1` (submitted)
- `outstanding_amount = 0`
- Payment Entry created at invoice time
- Normal tax and delivery charges applied

### Settlement Flow (On Out For Delivery)
1. **No New Payment Entry** (already paid)

2. **Journal Entry Created:**
   - DR: Freight and Forwarding Expense
   - CR: POS Cash Account
   - Amount: Shipping expense only
   - Purpose: Immediate courier fee payment

3. **Courier Transaction:**
   - Status: Settled
   - Amount: Shipping expense only (not order total)
   - Type: Fee-only settlement

4. **Delivery Note:**
   - Created and submitted
   - Links to Sales Invoice
   - Updates stock ledger

### Accounting Result
- Branch has cash from customer already
- Branch pays out shipping expense to courier
- Net profit = (Order Total - Shipping Expense)

---

## Case 4: Paid + Settle Later

**Business Scenario:** Customer paid at invoice creation (branch has cash). Courier delivers but doesn't collect. Branch will pay courier fee later.

### Invoice Creation
- `is_pos = 1`
- `docstatus = 1` (submitted)
- `outstanding_amount = 0`
- Payment Entry created at invoice time
- Normal tax and delivery charges applied

### Settlement Flow (On Out For Delivery)
1. **No New Payment Entry** (already paid)

2. **No Journal Entry** (settlement deferred)

3. **Courier Transaction Created:**
   - Status: Unsettled
   - Amount: Shipping expense only (not order total)
   - Type: Fee-only outstanding

4. **Delivery Note:**
   - Created and submitted
   - Links to Sales Invoice
   - Updates stock ledger

### Later Settlement (settle_single_invoice_paid)
When branch settles with courier:

1. **Journal Entry Created:**
   - DR: Creditors (Courier)
   - CR: Cash
   - Amount: Shipping expense

2. **Courier Transaction Updated:**
   - Status: Settled

### Accounting Result
- Branch holds customer payment
- Shipping expense accrued as liability
- Settlement pays courier fee only

---

## Case 5: Sales Partner Flow (Online Payment)

**Business Scenario:** Invoice created through Sales Partner (e.g., online marketplace, delivery app). Payment collected online. Special accounting and workflow.

### Invoice Creation
- `sales_partner` field set to partner name
- `payment_type = 'online'` (optional parameter)
- `custom_sales_invoice_state = 'In Progress'` (auto-set)
- `update_stock = 0` (stock update suppressed)
- `taxes = []` (all tax rows cleared - sales partner mode)
- No shipping income tax rows added
- No delivery charges added

### Special Business Rules
1. **Tax Suppression:**
   - All Sales Taxes and Charges rows removed
   - Partner handles their own invoicing
   - Keeps SI as accounting-only record

2. **Stock Update Suppression:**
   - `update_stock = 0` at invoice creation
   - Stock movement via Delivery Note (on Out For Delivery)
   - Separates accounting from inventory

3. **Payment Routing:**
   - If `payment_type = 'online'`:
     - Payment Entry created automatically
     - DR: Accounts Receivable
     - CR: Sales Partner Receivable Subaccount
   - Routes payment to partner account

### Kanban Flow (On Out For Delivery)
1. **Payment Entry Created** (if outstanding > 0):
   - DR: POS Cash Account (branch cash from rider)
   - CR: Accounts Receivable
   - Purpose: Branch collects cash from delivery rider

2. **Delivery Note Created:**
   - Updates stock ledger
   - Links to Sales Invoice
   - Status: Completed

### Accounting Result
- Partner receivable tracks amounts owed by/to partner
- Branch collects physical cash from rider
- Stock updated via DN, not SI

---

## Case 6: Pickup Flow (No Delivery)

**Business Scenario:** Customer picks up order from branch. No delivery, no courier involved. No shipping charges.

### Invoice Creation
- `pickup` flag set to `true` (or `custom_is_pickup = 1`)
- `is_pos = 1`
- Payment Entry created if paid at creation
- **No shipping income tax rows** (pickup suppresses delivery charges)
- **No delivery charges**
- Normal item taxes still apply

### Special Business Rules
1. **Shipping Suppression:**
   - Territory delivery_income not added
   - Territory delivery_expense not added
   - Customer handles own transportation

2. **No Delivery Note:**
   - Stock updated at invoice time (`update_stock = 1`)
   - No separate delivery workflow
   - Customer receives items immediately

3. **No Courier Transaction:**
   - No courier involved
   - No settlement needed
   - Simple cash/payment flow

### Settlement Flow
- No special settlement needed
- Simple customer payment (if not already paid)
- No courier-related accounting

### Accounting Result
- Clean sale without delivery costs
- Higher margin (no shipping expense)
- Immediate stock update

---

## Settlement Strategy Mapping

The system uses a strategy pattern to route to the correct handler:

```python
STRATEGY = {
    ("unpaid", "now"): handle_unpaid_settle_now,      # Case 1
    ("unpaid", "later"): handle_unpaid_settle_later,  # Case 2
    ("paid", "now"): handle_paid_settle_now,          # Case 3
    ("paid", "later"): handle_paid_settle_later,      # Case 4
}
```

**Dispatch Logic:**
```python
def dispatch_settlement(inv_name, mode, ...):
    inv = frappe.get_doc("Sales Invoice", inv_name)
    status = "unpaid" if _is_unpaid(inv) else "paid"
    key = (status, mode)  # e.g., ("paid", "now")
    handler = STRATEGY[key]
    return handler(inv, ...)
```

Cases 5 and 6 are handled through special logic paths:
- Case 5: Detected by `invoice.sales_partner` presence
- Case 6: Detected by `invoice.pickup` flag

---

## Integration Points

### 1. Invoice Creation → Settlement
- Invoice created with flags (sales_partner, pickup, payment_type)
- Flags determine which case applies
- Settlement strategy selected automatically

### 2. Kanban State Transitions
- Moving to "Out For Delivery" triggers settlement
- State transition calls appropriate handler
- Creates DN, PE, JE as needed

### 3. Manual Settlement
- Manager can trigger settlement manually
- Uses same dispatch_settlement function
- Respects invoice state (paid/unpaid)

### 4. Courier Settlement
- Bulk settlement via settle_delivery_party
- Individual via settle_single_invoice_paid
- Uses Courier Transaction tracking

---

## Key Accounts Used

### Standard Accounts
- **Accounts Receivable:** Customer payment tracking
- **POS Cash Account:** Branch cash (from POS Profile)
- **Freight and Forwarding Expense:** Delivery costs
- **Creditors:** Courier liability

### Special Accounts
- **Courier Outstanding:** Tracks unsettled courier collections
- **Sales Partner Receivable Subaccount:** Partner-specific tracking
- **Online Payment Account:** For online payment types

---

## Testing Coverage

Comprehensive tests exist in:
- `test_settlement_strategies.py` - Core 4 cases
- `test_kanban_state_transitions.py` - Kanban integration
- `test_invoice_creation_cases.py` - All 6 cases at invoice level

Each test validates:
- Correct accounting entries
- Proper status updates
- Idempotency (no duplicates)
- Error handling
- Integration between modules

---

## Future Enhancements

1. **Partial Settlements:**
   - Support partial courier payments
   - Track remaining balances

2. **Multi-Courier Orders:**
   - Split delivery between couriers
   - Proportional settlement

3. **Automated Reconciliation:**
   - Auto-match courier reports
   - Detect discrepancies

4. **Enhanced Partner Flows:**
   - Partner-specific pricing
   - Commission calculations
   - Automated payouts

---

**Document Version:** 1.0.0  
**Last Updated:** 2025-10-10  
**Related Files:**
- `jarz_pos/services/settlement_strategies.py`
- `jarz_pos/services/delivery_handling.py`
- `jarz_pos/services/invoice_creation.py`
- `jarz_pos/api/kanban.py`
