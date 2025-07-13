# Bundle Pricing Testing Guide

## 🎯 Overview

This guide explains how to test the new **ERPNext-compliant bundle pricing implementation** with comprehensive debug output to track all parameters, calculations, and results.

## 🚀 Quick Test Steps

### 1. **Backend Debug Output** (Server Console)
The backend now has comprehensive logging that shows:
- ✅ All incoming parameters (cart_json, customer_name, pos_profile_name)
- ✅ Parsed cart data with all bundle items
- ✅ POS profile settings (company, price list, currency)
- ✅ Each item processing (bundle vs regular)
- ✅ Price lookups and calculations
- ✅ Discount amount calculations
- ✅ Items before and after ERPNext processing
- ✅ Final invoice totals and verification

### 2. **Frontend Debug Test** (Browser Console)
We've created a debug test script that you can run from the browser console:

```javascript
// Load the debug script first (already included in the page)
// Then run the test
testBundlePricingDebug();
```

### 3. **Manual Testing via Custom POS Page**
1. Go to your Custom POS page
2. Add items to cart (including bundles)
3. Create sales invoice
4. Check server logs for detailed debug output

## 📋 Test Parameters You Can Verify

### Input Parameters
```json
{
  "cart_json": "[{\"is_bundle\": true, \"item_code\": \"BUNDLE_PARENT\", \"bundle_name\": \"Test Bundle\", \"price\": 150.00, \"items\": [{\"item_code\": \"SKU006\", \"qty\": 1}, {\"item_code\": \"SKU007\", \"qty\": 1}]}]",
  "customer_name": "Walk-In Customer",
  "pos_profile_name": "Main POS Profile"
}
```

### Debug Output Sections
1. **📍 File Location**: Shows exactly which file is being executed
2. **📋 Parameters**: All input parameters logged
3. **🛒 Cart Data**: Parsed cart with all items
4. **⚙️ POS Settings**: Company, price list, currency
5. **🎁 Bundle Processing**: Each bundle item with calculations
6. **💰 Price Lookups**: Item prices from database
7. **📊 Calculations**: Discount amounts and totals
8. **📋 Items Before/After**: ERPNext processing comparison
9. **💾 Save/Submit**: Success confirmation

## 🔧 How to Monitor Debug Output

### Option 1: Docker Logs (Recommended)
```bash
# In your terminal, run:
docker logs -f <container_name>

# Or if using docker-compose:
docker-compose logs -f web
```

### Option 2: Bench Logs
```bash
# In frappe-bench directory:
bench --site development.localhost console

# Or check log files:
tail -f logs/web.log
```

### Option 3: Browser Console
1. Open browser console (F12)
2. Go to Custom POS page
3. Run: `testBundlePricingDebug()`
4. Check both browser console and server logs

## 🧪 Test Scenarios

### Test 1: Basic Bundle
```javascript
testBundlePricingWithCart([{
    is_bundle: true,
    item_code: "BUNDLE_PARENT",
    bundle_name: "Basic Test Bundle",
    price: 100.00,
    items: [
        { item_code: "SKU006", qty: 1 },
        { item_code: "SKU007", qty: 1 }
    ]
}], "Basic Bundle Test");
```

### Test 2: Mixed Cart (Bundle + Regular Items)
```javascript
testBundlePricingWithCart([
    {
        is_bundle: true,
        item_code: "BUNDLE_PARENT",
        bundle_name: "Mixed Cart Bundle",
        price: 150.00,
        items: [
            { item_code: "SKU006", qty: 1 },
            { item_code: "SKU007", qty: 1 }
        ]
    },
    {
        is_bundle: false,
        item_code: "SKU006",
        qty: 2,
        price: 25.00
    }
], "Mixed Cart Test");
```

### Test 3: Multiple Bundles
```javascript
testBundlePricingWithCart([
    {
        is_bundle: true,
        item_code: "BUNDLE_PARENT",
        bundle_name: "Bundle 1",
        price: 100.00,
        items: [{ item_code: "SKU006", qty: 1 }]
    },
    {
        is_bundle: true,
        item_code: "BUNDLE_PARENT",
        bundle_name: "Bundle 2", 
        price: 200.00,
        items: [
            { item_code: "SKU006", qty: 1 },
            { item_code: "SKU007", qty: 1 }
        ]
    }
], "Multiple Bundles Test");
```

## 🔍 What to Look For

### ✅ Success Indicators
- All parameters logged correctly
- Item prices found in database
- Discount calculations are accurate
- Final invoice total matches expected bundle price
- Stock ledger entries created for all items
- Invoice saves and submits successfully

### ❌ Failure Indicators
- Missing item codes in database
- No prices found for items
- Calculation errors (totals don't match)
- ERPNext overriding custom discount amounts
- Save/submit errors

## 🛠️ Troubleshooting

### Common Issues & Solutions

1. **"No price found for item"**
   - Check Item Price doctype
   - Verify price list name matches POS profile
   - Ensure items have standard_rate set

2. **"POS Profile not found"**
   - Verify POS profile name in test parameters
   - Check if POS profile exists and is active

3. **"Customer not found"**
   - Verify customer name exists
   - Use "Walk-In Customer" for testing

4. **Discount amounts not applied**
   - Check if ignore_pricing_rule is set
   - Verify ERPNext version compatibility

## 📊 Expected Results

For a bundle with:
- Parent item: £100
- Child item 1: £50
- Child item 2: £30
- Bundle price: £80

Expected output:
- Parent item: rate=£100, discount_amount=£38.89, final=£61.11
- Child item 1: rate=£50, discount_amount=£11.11, final=£38.89
- Child item 2: rate=£30, discount_amount=£6.67, final=£23.33
- **Total: £123.33 → £80.00** ✅

## 🎉 Success Confirmation

You'll know the implementation is working when:
1. ✅ All debug output shows in server logs
2. ✅ Discount amounts are calculated proportionally
3. ✅ Final invoice total matches bundle price
4. ✅ All items appear in invoice with correct amounts
5. ✅ Stock movements are recorded for all items
6. ✅ Individual item profitability is trackable

## 🔧 Debug Functions Available

From browser console:
- `testBundlePricingDebug()` - Basic test
- `testMultipleBundles()` - Show test configurations
- `debugSystemState()` - Check system state
- `testBundlePricingWithCart(cart, name)` - Custom test

Ready to test! 🚀 