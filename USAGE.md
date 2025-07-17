# Jarz POS – Usage Guide

This guide covers day-to-day operation for sales staff and delivery team.

## 1. Launching POS
1. Login to ERPNext.
2. Navigate to **/app/custom-pos**.
3. Choose the correct POS Profile if prompted.

## 2. Adding Items
• Tap an item card to add a regular product.
• Tap a bundle card, fulfil required selections, then **Add to Cart**.

## 3. Customer & Delivery
1. Click the customer field.
2. Recent customers appear automatically; or type to search / click **+ New**.
3. Delivery charges load from the customer’s address city (configured under *Jarz POS › City*).

## 4. Cart Management
• Edit or remove bundles/items with the buttons next to each line.
• Click **Edit Expense** to adjust delivery cost if permitted.

## 5. Checkout
1. Review totals.
2. Press **Checkout** – a Sales Invoice is created & submitted.
3. POS auto-clears for the next sale after success message.

## 6. Courier Workflow
### Mark as Outstanding Courier
• On the Kanban board, click the red **Outstanding Courier** tag → select courier.
• The invoice is paid into *Courier Outstanding* and a *Courier Transaction* is logged.

### Settle Courier Balance (bulk)
• Sidebar › Couriers → choose courier → **Settle**.
• The system nets all unsettled rows and creates a Journal Entry between Cash-in-Hand and Courier Outstanding.

### Settle Single Invoice
• Click yellow **Settle** on an invoice card to pay/collect for just that invoice.

## 7. Mark Invoice Paid (online)
• Click **Mark Paid** on a card → choose payment mode (Instapay / Gateway / Wallet).
• A Payment Entry allocates the amount and turns the card green.

## 8. Troubleshooting Checklist
| Symptom | Action |
|---------|--------|
| Missing price | Ensure *Item Price* exists for selling price list |
| Delivery not applied | Check customer address city record & *City* doctype amounts |
| Bundle discount wrong | Verify bundle price < individual total & item prices correct |
| Cash account error | Check Cash-in-Hand ledger exists for POS Profile |

---
For installation & developer setup, see **README.md**.
