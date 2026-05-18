# Payment Collection Change After Dispatch

Date: 2026-05-18
Status: analysis and recommendation only
Scope: customer-unpaid courier orders after Out for Delivery or Delivered

## Executive Recommendation

This workflow must only handle orders where the customer had not actually paid before dispatch. If the customer already paid online before Out for Delivery, the customer payment method is final and must not be changed from this workflow.

Important distinction:

- ERP may show the Sales Invoice as paid after Out for Delivery because the receivable was moved from `Debtors - J` to `Courier Outstanding - J`.
- Business-wise, that order is still unpaid by the customer until the courier collects cash or the customer pays online at the door.
- The new workflow should treat this as customer-unpaid, courier-responsibility money.

Recommended business and technical direction:

1. Add a manager-only "Change collection method" workflow for customer-unpaid orders already in Out for Delivery or Delivered.
2. Block the workflow when a real customer payment already exists before dispatch, especially an online Payment Entry to Bank/Mobile Wallet.
3. Allow the workflow only before courier settlement is finalized.
4. Correct the courier responsibility and ledgers atomically before settlement.
5. Keep the invoice in its current operational state. Do not move Delivered back to Out for Delivery.

The main supported cases are:

- Customer was expected to pay online but had not paid yet, then pays cash to the courier.
- Customer was expected to pay cash/COD, then pays online when the courier reaches them.

The main blocked case is:

- Customer already paid online before Out for Delivery. No payment-method change is needed or allowed.

## Scope Rules

### In Scope

The workflow applies when all of these are true:

1. Sales Invoice is submitted.
2. Operational state is Out for Delivery or Delivered.
3. Customer was unpaid at dispatch time.
4. The money responsibility was moved to the courier through Courier Outstanding or an unsettled Courier Transaction.
5. Courier settlement is not finalized.
6. No real customer payment already exists to Cash, Bank, Mobile Wallet, Card, or similar customer payment account.

In code terms, Sales Invoice `outstanding_amount <= 0` is not enough to prove the customer paid. For COD settle-later, the invoice can look paid only because the receivable moved to Courier Outstanding. The workflow must inspect Courier Transaction, Courier Outstanding transfer artifacts, and actual Payment Entries.

### Out Of Scope / Blocked

The workflow must block when any of these are true:

1. A submitted customer Payment Entry already exists before OFD to Cash, Bank, Mobile Wallet, Card, InstaPay, or any real collection account.
2. A confirmed online payment is already accounted before OFD.
3. The related courier transaction is already settled.
4. The order is a sales partner or delivery partner special flow that has not been explicitly supported.
5. Finance is trying to reverse an accounting mistake after settlement. That needs a separate finance correction workflow, not a customer payment switch.

## Current Code Facts

### 1. Out for Delivery is an accounting boundary

Once an invoice enters Out for Delivery, code can create downstream artifacts:

- Delivery Note through `ensure_delivery_note_for_invoice()` in `jarz_pos/services/delivery_handling.py`.
- A permanent `custom_was_out_for_delivery` flag through `jarz_pos/events/sales_invoice.py`.
- Courier Transaction rows through `mark_courier_outstanding()`, `handle_out_for_delivery_paid()`, or trip OFD logic.
- Journal Entries for shipping expense and courier payables.

The amendment/cancel guard also treats Delivery Notes, Delivery Trips, Courier Transactions, Sales Partner Transactions, and Journal Entries as hard mutation blockers in `jarz_pos/api/manager.py`.

Implication: after OFD, this should be a controlled collection-responsibility workflow, not an invoice amendment.

### 2. ERP paid is not always customer paid

For COD settle-later, the system may move the receivable away from the customer:

```text
DR Courier Outstanding - J
CR Debtors - J
```

That can make the Sales Invoice outstanding amount become zero. This does not mean the customer paid. It means the courier is now expected to collect and return the money.

Implication: eligibility must be based on the real collection route, not only Sales Invoice status or outstanding amount.

### 3. `custom_payment_method` is not accounting

`custom_payment_method` is set during invoice creation and shown in Kanban/trips as the expected or selected method. It does not by itself create a Payment Entry, move money, clear Courier Outstanding, or update courier balances.

Accounting is created by these real artifacts:

- Payment Entry from `pay_invoice()` in `jarz_pos/api/invoices.py`.
- Journal Entry from courier settlement functions in `jarz_pos/services/delivery_handling.py`.
- Courier Transaction rows in the `Courier Transaction` doctype.

Implication: changing the label after dispatch without changing accounting will produce a visually correct order and financially wrong ledgers.

### 4. Real online payment before OFD is final

If the customer already paid online before Out for Delivery, then the order is already customer-paid. That payment method must not be changed by this workflow.

For those orders, the only remaining courier action may be shipping settlement, for example paying the courier shipping expense later. That is not a customer payment-method change.

Implication: paid-before-OFD online orders should be blocked from the change workflow and shown as "already paid" or "no collection change required".

### 5. Online receipts are evidence only today

`POS Payment Receipt` creation and confirmation in `jarz_pos/api/payment_receipts.py` records and confirms a receipt, but it does not create a Payment Entry or Journal Entry.

Implication: for an online payment made at the door on a customer-unpaid COD order, receipt evidence is not enough. The workflow must also clear Courier Outstanding to the online account or to a pending online clearing account.

### 6. Current settle-later accounting model

For normal courier orders, the system effectively supports these cases.

#### Paid + Settle Now

Customer already paid before OFD, and branch pays courier shipping immediately.

Expected artifacts:

- Delivery Note.
- Journal Entry: DR Freight Expense, CR Branch Cash.
- Courier Transaction: Settled.

Payment collection change applicability: blocked. Customer already paid.

#### Paid + Settle Later

Customer already paid before OFD, but courier shipping will be paid later.

Expected artifacts:

- Delivery Note.
- Journal Entry: DR Freight Expense, CR Creditors with courier party.
- Courier Transaction: amount `0`, shipping amount `S`, status `Unsettled`.
- Later settlement pays courier shipping: DR Creditors, CR Branch Cash.

Payment collection change applicability: blocked. Only shipping settlement remains.

#### Unpaid + Settle Now

Customer payment is collected before or at dispatch and courier shipping is paid immediately.

Expected artifacts:

- Payment Entry to branch cash for the customer amount.
- Delivery Note.
- Journal Entry: DR Freight Expense, CR Branch Cash.
- Courier Transaction: Settled.

Payment collection change applicability: normally blocked after payment is recorded, because the customer is no longer unpaid.

#### Unpaid + Settle Later / COD

Courier will collect customer money and settle later with the branch.

Expected artifacts:

- Courier Transaction: amount `GT`, shipping amount `S`, status `Unsettled`.
- Receivable transfer: DR Courier Outstanding, CR Debtors, usually via Journal Entry with Sales Invoice reference.
- Shipping accrual: DR Freight Expense, CR Creditors with courier party.
- Later settlement when courier returns:
  - If `GT >= S`: DR Branch Cash `(GT - S)`, DR Creditors `S`, CR Courier Outstanding `GT`.
  - If `S > GT`: DR Creditors `S`, CR Courier Outstanding `GT`, CR Branch Cash `(S - GT)`.

Payment collection change applicability: allowed before courier settlement. This is the core scope.

### 7. Current settlement uses Courier Transaction rows

Courier balances and settlement depend heavily on unsettled `Courier Transaction` rows:

- `get_courier_balances()` sums `amount - shipping_amount` for unsettled rows.
- `settle_single_invoice_paid()` has a special path when it finds an unsettled CT with `amount > 0`.
- `settle_courier_collected_payment()` finalizes a courier-collected order amount.
- `settle_delivery_party()` aggregates all unsettled CT rows for a courier party.

Implication: if a customer-unpaid COD order switches to online at the door, leaving the original CT as `amount = GT` will keep the courier owing branch money even though the customer paid online. That creates double-collection risk unless the CT is adjusted or replaced.

## The Two Supported Business Scenarios

## Scenario A: Expected online, but customer had not paid and then pays cash to courier

This scenario is only allowed when online was an intention or requested method, not an actual payment.

### A1. No actual online Payment Entry exists

This is supported.

If the invoice was still customer-unpaid when it went OFD, the system should treat it as a COD settle-later order:

- CT amount = order total `GT`.
- CT shipping = courier shipping expense `S`.
- Courier Outstanding holds the receivable.
- Courier should return cash minus shipping.

Recommended handling:

1. Keep the current COD courier settlement path.
2. Record actual collection method as Cash.
3. Do not create any Bank/Mobile Wallet correction because no online accounting happened.
4. Settle courier normally when they return.

Accounting at courier settlement:

```text
If GT >= S:
DR Branch Cash              GT - S
DR Creditors - J            S
CR Courier Outstanding - J  GT

If S > GT:
DR Creditors - J            S
CR Courier Outstanding - J  GT
CR Branch Cash              S - GT
```

### A2. A real online Payment Entry already exists before OFD

This is blocked.

Recommended handling:

1. Do not allow the customer/courier payment method switch.
2. Show that the order is already paid online.
3. Continue only with normal courier shipping settlement if needed.

If the customer also gives cash to the courier despite having already paid online, that is not a payment-method change. It is duplicate collection and must be handled outside this workflow through refund/customer credit rules.

Do not reverse online Payment Entries from this workflow. If the online Payment Entry was created by mistake, that is a finance correction, not an operational courier collection change.

## Scenario B: Expected cash/COD, but customer pays online at the door

This is supported only when the customer was unpaid at dispatch.

Typical state after OFD:

- Sales Invoice outstanding may be zero.
- Courier Outstanding has a debit for the order amount.
- Courier Transaction says the courier owes `GT` and is owed shipping `S`.
- Shipping payable to courier already exists in Creditors.

Calling `pay_invoice()` after this is not correct because the Sales Invoice is no longer outstanding. The payment is not from the customer to Debtors anymore; it clears the courier's responsibility into an online account or pending online clearing account.

Recommended handling before courier settlement:

1. Require online payment evidence or route to an online pending clearing account.
2. Create a Journal Entry:
   - DR Online Account or Online Pending Clearing: `GT`
   - CR Courier Outstanding with courier party: `GT`
3. Mark the original order-amount CT as resolved by payment-method change.
4. Create or keep a shipping-only unsettled CT:
   - amount `0`
   - shipping amount `S`
   - status `Unsettled`
   - note: payment changed from cash/COD to online after dispatch
5. Later courier settlement should only pay courier shipping:
   - DR Creditors `S`
   - CR Branch Cash `S`

This ensures the courier is no longer asked to bring order cash, but still gets the delivery expense.

If the branch pays courier shipping immediately at return, steps 4 and 5 can happen in one atomic operation.

## What To Do About Settlement Timing

### Recommended rule

For customer-unpaid orders, payment collection change must happen before courier settlement whenever possible.

The sequence should be:

1. Order reaches customer.
2. Actual collection method differs from the original requested method.
3. Manager records payment collection change.
4. System adjusts ledgers and CT responsibility.
5. Courier settlement uses the corrected CT state.

### If courier is not yet settled

This is the normal supported path.

- Cash/COD -> Online: clear Courier Outstanding to online or pending account first, then settle only shipping payable.
- Online intent -> Cash: if no actual online payment exists, settle as normal COD courier cash.

### If courier is already settled

Normal payment collection change should be blocked.

If the business discovers an error after settlement, use a separate finance correction workflow. That correction must link the original settlement JE and preserve the original CT history. It should not be presented as a customer payment-method change.

## Operational State: OFD vs Delivered

Recommended behavior:

- Allow the change in both Out for Delivery and Delivered when the order is customer-unpaid and courier settlement is not finalized.
- Prefer leaving Delivered as Delivered if the order is physically delivered.
- Do not move Delivered back to Out for Delivery to perform the correction.
- Do not call the existing OFD endpoints from this workflow because they may create/reuse Delivery Notes, create CT rows, and run state hooks.

The real gate should be collection/accounting state, not kanban state:

- There must be an existing courier party.
- There must be an unsettled CT with order amount or equivalent Courier Outstanding responsibility.
- There must be no real pre-existing customer payment to cash/bank/wallet/card.
- There must be no submitted final settlement JE for the courier/order.

Delivered is operationally safer for UI because fewer normal actions happen there, but the backend must still validate artifacts.

## Implementation Approaches

## Approach 1: Manual accounting correction only

Description:

- No new product workflow.
- Finance manually creates JEs, updates CT rows, and edits labels as needed.

Pros:

- No development work.
- Useful for rare emergencies.

Cons:

- High risk of double collection.
- Easy to leave CT and ledgers inconsistent.
- Easy to accidentally change an already-paid online order, which should be blocked.
- Requires deep ERP/accounting knowledge each time.

Recommendation: not recommended except as a temporary admin-only workaround.

## Approach 2: Minimal manager workflow using existing artifacts

Description:

- Add one backend endpoint for customer-unpaid, pre-settlement collection switches.
- It blocks any order with a real pre-dispatch customer payment.
- It creates the required JE and updates/replaces CT rows.
- It updates requested/actual payment display fields.
- It blocks after courier settlement.

Pros:

- Fastest safe implementation.
- Reuses existing settlement functions.
- Solves the common unpaid cases.

Cons:

- Current CT doctype has only `Settled` and `Unsettled`, not `Adjusted` or `Replaced`.
- Mutating or closing CT rows needs careful notes to preserve auditability.
- No dedicated document for approval/history unless added.

Recommendation: acceptable as phase 1 if paired with a change log and strict validation.

## Approach 3: Explicit Payment Collection Change document and service

Description:

Create a new auditable workflow, for example `Payment Collection Change`, with fields:

- Sales Invoice
- Delivery Trip
- Courier party type and party
- Customer unpaid at dispatch: yes/no
- Old requested payment method
- New actual collection method
- Reason
- Evidence or reference number
- Amount
- Shipping amount
- Source CT
- Created JE / receipt links
- Status: Draft, Applied, Reversed
- Applied by / applied on

The service would apply the switch atomically:

- Validate invoice is OFD or Delivered.
- Validate the order is customer-unpaid, even if ERP outstanding is now zero.
- Validate there is no real pre-dispatch customer Payment Entry.
- Validate CT state and settlement state.
- Lock the invoice and CT rows.
- Create the reclassification JE when needed.
- Update or close original CT.
- Create shipping-only CT if needed.
- Update actual payment method fields.
- Publish realtime update.

Pros:

- Strong audit trail.
- Clear block/no-op behavior for already-paid online orders.
- Cleaner future reports.
- Safer for finance.

Cons:

- More development work.
- Needs tests for all supported and blocked cases.

Recommendation: best long-term approach.

## Approach 4: Cancel/amend/recreate the invoice after OFD

Description:

- Cancel or amend the Sales Invoice and create a replacement with the new payment method.

Pros:

- The invoice label becomes clean.

Cons:

- Conflicts with the current hard-mutation blocker design.
- Submitted Delivery Notes, CTs, JEs, and trips already exist.
- High risk of stock, payment, and courier settlement corruption.
- Does not solve the real issue, which is courier responsibility for unpaid customer money.

Recommendation: do not use this for post-dispatch payment collection changes.

## Recommended Target Workflow

### Common validation

Before applying any switch:

1. Sales Invoice must be submitted.
2. Invoice state must be Out for Delivery or Delivered.
3. A courier party must be known from CT or Delivery Trip.
4. The order must be customer-unpaid at dispatch.
5. Sales Invoice `outstanding_amount <= 0` must not be treated as proof of customer payment if Courier Outstanding transfer exists.
6. There must be an unsettled source CT with amount `GT`, or equivalent unsettled Courier Outstanding responsibility.
7. There must be no submitted customer Payment Entry to Cash, Bank, Mobile Wallet, Card, InstaPay, or similar real collection account.
8. Source CT must be unsettled.
9. No submitted final settlement JE should exist for the same invoice and courier.
10. Amount must match Sales Invoice grand total unless explicitly approved.
11. Shipping amount must come from CT first, then Sales Invoice `custom_shipping_expense`, then territory fallback.
12. Online-at-door payment must have a reference, receipt, or pending-clearing destination.
13. User must be manager/accounts role.
14. Every operation must run inside one transaction/savepoint.

### Expected online -> actual cash before courier settlement

Use when: customer was expected to pay online, did not actually pay online, and pays cash to courier.

Accounting:

```text
No online correction is required.
Use normal COD courier settlement.
```

Courier settlement:

```text
If GT >= S:
DR Branch Cash              GT - S
DR Creditors - J            S
CR Courier Outstanding - J  GT

If S > GT:
DR Creditors - J            S
CR Courier Outstanding - J  GT
CR Branch Cash              S - GT
```

Blocked variant:

```text
If a real online Payment Entry already exists, do not allow this workflow.
Show: already paid online / no payment collection change required.
```

### Expected cash/COD -> actual online before courier settlement

Use when: courier expected to collect cash, but customer pays InstaPay/Wallet/Card at the door.

Accounting:

```text
DR Online Account or Online Pending Clearing   GT
CR Courier Outstanding - J                     GT
```

Courier tracking:

```text
Original CT: order obligation resolved by payment switch
New/current CT: amount = 0, shipping_amount = S, status = Unsettled
```

Later courier settlement:

```text
DR Creditors - J   S
CR Branch Cash     S
```

If shipping is paid immediately during the same flow, create that settlement JE and mark the shipping CT settled.

### After courier settlement

Normal flow should be blocked.

Finance correction, if needed, should:

1. Link original settlement JE.
2. Reverse the wrong settlement effect.
3. Create the correct accounting effect.
4. Preserve original CT history.
5. Add reason and approval.

This is separate from the operational payment collection change workflow.

## Data Model Changes Recommended

### Sales Invoice fields

Current `custom_payment_method` is ambiguous. Recommended split:

- `custom_requested_payment_method`: what the customer/order originally selected.
- `custom_actual_collection_method`: how money was actually collected for customer-unpaid dispatched orders.
- `custom_customer_unpaid_at_dispatch`: check.
- `custom_payment_method_changed_after_dispatch`: check.
- `custom_payment_collection_change`: link to latest Payment Collection Change document.

If we want a smaller phase 1, keep `custom_payment_method` as requested/display, add only `custom_actual_collection_method` and `custom_customer_unpaid_at_dispatch`, and show actual method when present.

### Courier Transaction fields

Current status only supports `Settled` and `Unsettled`. Recommended additions:

- Status options: `Unsettled`, `Settled`, `Adjusted`, `Replaced`.
- `adjusted_by_transaction`: Link to replacement CT or change document.
- `payment_collection_change`: Link to change document.
- `resolution_reason`: Data/Small Text.

Avoid negative adjustment CT rows unless the settlement algorithms are updated to aggregate net amounts correctly. Current code paths often look for the first CT with `amount > 0`, so negative adjustment rows could be ignored and cause wrong settlement.

### New document

Recommended new doctype: `Payment Collection Change`.

This gives operations and finance a real audit object instead of hiding the change in CT notes.

## Risks

### Already-paid online order changed by mistake

If a real online payment already exists and the workflow allows a cash switch, the system may create duplicate collection or false cash movement.

Mitigation: block any order with a real pre-dispatch customer Payment Entry to Cash/Bank/Wallet/Card. Paid online before OFD is final for this workflow.

### ERP paid status misunderstood

COD settle-later can make Sales Invoice outstanding zero even though the customer has not paid.

Mitigation: determine customer-unpaid status from CT/Courier Outstanding responsibility, not only invoice outstanding.

### Double collection

If online-at-door payment is recorded while CT still says courier owes cash, both bank and courier may be collected.

Mitigation: clear or replace the order-amount CT in the same transaction as the online JE.

### False bank balance

If a customer says they paid online but the money is unconfirmed, posting directly to Bank/Mobile Wallet may overstate assets.

Mitigation: require receipt confirmation or post to an Online Pending Clearing account first.

### Shift balance distortion

Cash corrections affect branch cash accounts. If posted after a shift closes, the correction may appear in the wrong shift/day cash balance.

Mitigation: use current posting date by default, show the active/closed shift warning, and require finance approval for backdating.

### Courier balance mismatch

Courier balances are based on unsettled CT rows. Any payment switch must update CT state so balances match real responsibility.

Mitigation: source all courier settlement from the corrected CT rows, not from `custom_payment_method`.

### Settlement after the fact

Changing collection after courier settlement requires reversing already submitted JEs.

Mitigation: block normal switch after settlement; route to finance correction.

### Payment receipt misunderstanding

Receipt confirmation does not currently create accounting.

Mitigation: connect the receipt to the new workflow, then create the clearing JE as part of the workflow. If receipt confirmation remains evidence-only, UI copy should say so clearly.

### Delivery partner / sales partner special cases

Partner orders have different settlement assumptions, including full-order collection or no cash exchange depending on partner mode.

Mitigation: first implementation should either block partner orders or route them to a partner-specific payment change workflow.

## Specific Questions And Recommended Answers

### Q1. Can the customer change payment method if they already paid online before OFD?

Recommended answer: no. If a real online customer payment already exists, the order is paid. The workflow should block and show that no collection change is required.

### Q2. What does "unpaid" mean after OFD if the Sales Invoice outstanding is zero?

Recommended answer: unpaid means unpaid by the customer. After OFD, the invoice may look paid because the receivable moved from Debtors to Courier Outstanding. If CT/Courier Outstanding says the courier still owes the order amount, it is still customer-unpaid for this workflow.

### Q3. Should we settle first, then change payment method?

Recommended answer: no. For customer-unpaid orders, change the collection responsibility first, then settle the courier. Settlement should always reflect the final reality of who collected the money.

Choices:

- Change first, settle after: recommended.
- Settle first, then create correction JEs: only for finance correction, not normal operations.

### Q4. Can this be done while the order is Delivered?

Recommended answer: yes, if the order is customer-unpaid and courier settlement is not finalized. Delivered is fine operationally. The backend should not depend on moving the invoice back to OFD.

Choices:

- Allow in Out for Delivery only: too restrictive.
- Allow in Out for Delivery and Delivered before settlement: recommended.
- Allow anytime: not recommended.

### Q5. Should the workflow call existing OFD endpoints again?

Recommended answer: no. OFD endpoints create/reuse Delivery Notes and may create CT/JEs. A payment switch needs its own endpoint/service that only corrects collection responsibility.

### Q6. Is changing `custom_payment_method` enough?

Recommended answer: no. It is only a label. Use a separate actual collection method plus accounting artifacts.

### Q7. What should happen when expected Cash/COD becomes Online before courier settlement?

Recommended answer: clear Courier Outstanding into the online or pending account, resolve the order-amount CT, and leave only shipping payable to courier.

### Q8. What should happen when expected Online becomes Cash before courier settlement?

Recommended answer: if online was only an unpaid intention, settle as normal COD cash. If an online Payment Entry exists, block the workflow because the order is already paid.

### Q9. Should online payment at the door create a Payment Entry or Journal Entry?

Recommended answer: if the invoice is still genuinely outstanding in Debtors, Payment Entry can be used. If OFD settle-later already transferred the receivable to Courier Outstanding, use Journal Entry from online account/clearing to Courier Outstanding because there is no Sales Invoice outstanding to allocate.

### Q10. Should we use a pending online account?

Recommended answer: yes unless the business wants to require confirmed receipt/reference before applying the switch.

Choices:

- Require confirmed online proof before posting to bank/wallet: safest.
- Post to Online Pending Clearing first, then clear later: good operational compromise.
- Post directly to bank/wallet based only on courier/customer statement: not recommended.

### Q11. Should the courier keep/deduct shipping when customer pays online at the door?

Recommended answer: no order cash should be expected from courier, but shipping remains payable to courier. The courier should receive shipping from branch, either immediately or through shipping-only settlement.

### Q12. How should shipping greater than order total behave?

Recommended answer: keep the existing settlement logic. It already handles `S > GT` by paying the courier the excess from branch cash while clearing the available order amount.

### Q13. Should we mutate the original Courier Transaction?

Recommended answer: long term, no silent mutation. Use a Payment Collection Change log and CT statuses like Adjusted/Replaced. Short term, if we need phase 1, mark the original CT as resolved with explicit notes/link and create a replacement shipping-only CT.

### Q14. Who should be allowed to do this?

Recommended answer: branch manager and above for pre-settlement customer-unpaid switches; accounts/finance role for post-settlement corrections.

### Q15. What should reports show?

Recommended answer: show both requested and actual collection method. Finance reports should use actual accounting route, not requested payment method.

### Q16. What should happen to POS Payment Receipt?

Recommended answer: keep it as evidence, but connect it to the new Payment Collection Change. Receipt confirmation should either trigger the clearing JE through the workflow or explicitly remain proof-only with clear UI wording.

## Proposed Phase Plan

### Phase 1: Safe pre-settlement switch for customer-unpaid orders

Build only these flows:

- COD/Cash -> Online before courier settlement.
- Online intent -> Cash before courier settlement when no real online Payment Entry exists.

Block:

- Any real customer Payment Entry before OFD.
- Already paid online orders.
- Already settled CT.
- Partner/sales partner orders unless explicitly handled.

### Phase 2: Audit document and receipt integration

Add `Payment Collection Change` doctype and link it to receipts, JEs, CTs, and Sales Invoice actual method fields.

### Phase 3: Separate finance corrections

Add reversal/correction workflows for already-settled couriers or mistaken Payment Entries, with stricter permissions and approval reason. Keep this separate from the operational customer-unpaid switch.

## Final Recommendation

Implement Approach 3 as the target design, possibly delivered in phases using Approach 2 for the first release.

The key rule is simple:

This workflow is only for money the customer had not actually paid before dispatch. If the customer already paid online before OFD, payment method change is blocked and no collection correction is needed.

For customer-unpaid orders after OFD, treat the apparent paid Sales Invoice carefully. It may only be paid because customer receivable moved to Courier Outstanding. The workflow should correct who actually collected the customer money, then let courier settlement use the corrected responsibility.

So the safest workflow is:

1. Keep the invoice Delivered or OFD as-is.
2. Confirm the order is customer-unpaid despite any ERP paid status caused by Courier Outstanding transfer.
3. Block if a real pre-dispatch customer payment already exists.
4. Before courier settlement, record the actual collection method.
5. Reclassify the money responsibility with a JE or PE depending on where the receivable currently lives.
6. Adjust courier transaction responsibility so settlement asks for exactly what the courier really owes or is owed.
7. Settle after the correction.
8. If settlement already happened, use a separate finance correction path.