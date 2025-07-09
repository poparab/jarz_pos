# Delivery Integration Test Guide

## Overview
This guide tests the new address-based delivery system where delivery charges are determined from the customer's address city rather than a separate delivery selector.

## Setup Required

### 1. Create Test Cities
Go to `Jarz POS > City` and create:
- **City Name**: "Downtown"
- **Delivery Income**: 10.00
- **Delivery Expense**: 3.00

- **City Name**: "Suburbs" 
- **Delivery Income**: 15.00
- **Delivery Expense**: 5.00

### 2. Test Scenarios

#### Scenario 1: New Customer with Address
1. Open POS: `/app/custom-pos`
2. Add some items to cart
3. Click "+ New" customer button
4. Fill in customer details:
   - **Customer Name**: "John Doe"
   - **Mobile**: "123-456-7890"
   - **Address Line 1**: "123 Main St"
   - **City**: "Downtown" (select from dropdown)
5. Complete customer creation
6. Verify:
   - Customer is selected automatically
   - Cart shows delivery charges: $10.00
   - Delivery info displayed in cart

#### Scenario 2: Existing Customer Selection
1. Clear cart and customer
2. Search for existing customer with address
3. Select customer
4. Verify:
   - Delivery charges loaded automatically from address
   - Cart reflects delivery costs
   - No manual delivery selection needed

#### Scenario 3: Customer without Delivery City
1. Create/select customer with city not in delivery system
2. Verify:
   - No delivery charges shown
   - Cart works normally without delivery
   - Checkout proceeds without delivery costs

#### Scenario 4: Complete Checkout
1. Select customer with delivery address (Downtown)
2. Add items worth $50.00
3. Checkout
4. Verify invoice structure:
   ```
   Items Total: $50.00
   + Delivery Charge: $10.00 (Tax)
   = Subtotal: $60.00
   + Discount Amount: -$3.00 (Delivery Expense)
   = Grand Total: $57.00
   ```

## Expected Behavior

### ✅ Working Features
- No delivery selector in main POS screen
- City field in customer creation is dropdown linked to City doctype
- Delivery charges auto-load when customer selected
- Cart displays delivery info when applicable
- Invoice creation includes delivery accounting
- Customer clearing also clears delivery charges

### ❌ Removed Features
- Main screen delivery city selector
- Manual delivery selection during POS operation
- Delivery expense editing during sale (now fixed by city configuration)

## Troubleshooting

### Issue: Delivery charges not showing
**Solution**: 
- Check customer has address with city field
- Verify city name exactly matches City doctype city_name
- Ensure City record exists with delivery amounts

### Issue: City not saving in address
**Solution**:
- City field is Link field connected to City doctype
- Select city from dropdown (shows city_name, stores city ID)
- Ensure City records exist in City doctype before creating customers

### Issue: Invoice creation fails
**Solution**:
- Verify Chart of Accounts has freight/expense accounts
- Check POS Profile has payment methods configured
- Ensure customer address exists and is valid

## Notes
- Delivery charges are now completely address-driven
- City selection uses Link field to City doctype for data consistency
- System automatically finds appropriate accounting heads
- Delivery logic activates when customer has address with valid city link 