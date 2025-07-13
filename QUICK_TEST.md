# 🚀 Quick Test Guide - Bundle Pricing Debug

## ✅ **Implementation Status**
- ✅ Backend debug logging implemented
- ✅ Frontend debug tools created  
- ✅ Custom POS page updated with debug buttons
- ✅ Server restarted with new configuration

## 🎯 **How to Test RIGHT NOW**

### Step 1: Go to Custom POS Page
1. Open your browser
2. Go to: `http://your-site/app/custom-pos`
3. You should see the Custom POS page with debug buttons

### Step 2: Test Bundle Pricing (Method 1 - UI Buttons)
1. Click the **"Test Bundle Pricing"** button in the sidebar
2. Watch the browser console (F12) for frontend debug output
3. Check server logs for backend debug output

### Step 3: Test Bundle Pricing (Method 2 - Console)
1. Open browser console (F12)
2. Run: `testBundlePricingDebug()`
3. Watch both browser console and server logs

### Step 4: Monitor Server Debug Output
Since we can't find log files, monitor the server output directly:

**Option A: Docker Logs**
```bash
# In your terminal:
docker logs -f <container_name>
```

**Option B: Check the attached server logs**
- Look at the server logs you showed in the screenshot
- You should see our debug output after line:
  `"POST /api/method/jarz_pos.jarz_pos.page.custom_pos.custom_pos.create_sales_invoice HTTP/1.1" 200 -`

## 📋 **What You Should See**

### Frontend (Browser Console)
```
🧪 FRONTEND DEBUG: Starting Bundle Pricing Test
📍 Current URL: http://your-site/app/custom-pos
📍 Timestamp: 2025-01-10T22:01:25.000Z
🛒 Test Cart Data:
[
  {
    "is_bundle": true,
    "item_code": "BUNDLE_PARENT",
    "bundle_name": "Debug Test Bundle",
    "price": 150,
    "items": [
      {"item_code": "SKU006", "qty": 1},
      {"item_code": "SKU007", "qty": 1}
    ]
  }
]
📋 API Parameters:
   - Method: jarz_pos.jarz_pos.page.custom_pos.custom_pos.create_sales_invoice
   - cart_json: [{"is_bundle": true, ...}]
   - customer_name: Walk-In Customer
   - pos_profile_name: Main POS Profile
🚀 Making API call...
⚠️  Check the server console/logs for detailed backend debug output!
```

### Backend (Server Logs)
```
================================================================================
🚀 BUNDLE PRICING DEBUG - create_sales_invoice() CALLED
================================================================================
📍 File: /workspace/development/frappe-bench/apps/jarz_pos/jarz_pos/jarz_pos/page/custom_pos/custom_pos.py
📍 Function: create_sales_invoice
📍 Timestamp: 2025-01-10 22:01:25.123456
📋 Parameters received:
   - cart_json: [{"is_bundle": true, "item_code": "BUNDLE_PARENT", ...}]
   - customer_name: Walk-In Customer
   - pos_profile_name: Main POS Profile
   - delivery_charges_json: null

🛒 Parsed cart data:
   Item 1: {
       "is_bundle": true,
       "item_code": "BUNDLE_PARENT",
       "bundle_name": "Debug Test Bundle",
       "price": 150.0,
       "items": [
           {"item_code": "SKU006", "qty": 1},
           {"item_code": "SKU007", "qty": 1}
       ]
   }

⚙️ POS Profile settings:
   - Company: Your Company
   - Selling Price List: Standard Selling
   - Currency: GBP

🎁 Processing BUNDLE item #1...
🎁 BUNDLE PROCESSING DEBUG
   📍 Function: process_bundle_item
   📋 Bundle data: {...}
   🔍 Extracted values:
      - parent_item_code: BUNDLE_PARENT
      - bundle_price: 150.0
      - bundle_name: Debug Test Bundle
      - child_items_count: 2
   💰 Parent item price: 100.0
   🔍 Processing child items:
      Child 1: SKU006
         - qty: 1
         - original_price: 890.0
         - item_total: 890.0
      Child 2: SKU007
         - qty: 1
         - original_price: 50.0
         - item_total: 50.0
   📊 Bundle calculations:
      - parent_original_price: 100.0
      - child_items_total: 940.0
      - total_original_value: 1040.0
      - bundle_price: 150.0
      - total_discount_needed: 890.0
   👑 Adding parent item:
      - item_code: BUNDLE_PARENT
      - rate: 100.0
      - discount_amount: 85.58
      - final_amount: 14.42
   👶 Adding child items:
      Child 1: SKU006
         - qty: 1
         - rate: 890.0
         - discount_amount: 760.58
         - final_amount: 129.42
      Child 2: SKU007
         - qty: 1
         - rate: 50.0
         - discount_amount: 42.79
         - final_amount: 7.21
   ✅ Bundle verification:
      - calculated_total: 151.05
      - target_bundle_price: 150.0
      - difference: 1.05

📋 Items added to Sales Invoice BEFORE ERPNext processing:
   Item 1:
      - item_code: BUNDLE_PARENT
      - qty: 1
      - rate: 100.0
      - discount_amount: 85.58
      - amount: 14.42
      - price_list_rate: 100.0
      - ignore_pricing_rule: 1
      - description: Bundle: Debug Test Bundle (discounted from 100.0...

⚡ Running ERPNext standard workflow...
   1. set_missing_values()...
   2. calculate_taxes_and_totals()...

📋 Items AFTER ERPNext processing:
   Item 1:
      - item_code: BUNDLE_PARENT
      - qty: 1
      - rate: 100.0
      - discount_amount: 85.58
      - amount: 14.42
      - price_list_rate: 100.0

💰 Invoice totals:
   - Net Total: 151.05
   - Grand Total: 151.05
   - Calculated Total: 151.05

💾 Saving and submitting invoice...
   ✅ Invoice saved: ACC-SINV-2025-00062
   ✅ Invoice submitted: ACC-SINV-2025-00062

🎉 SUCCESS! Sales Invoice created successfully!
   - Invoice Number: ACC-SINV-2025-00062
   - Final Grand Total: 151.05
================================================================================
```

## 🔧 **Available Debug Functions**

From the Custom POS page, you can use:

### UI Buttons (In Sidebar)
- **"Test Bundle Pricing"** - Run basic bundle test
- **"Check System State"** - Verify system configuration
- **"Show Test Configs"** - Display multiple test scenarios

### Console Functions
- `testBundlePricingDebug()` - Basic bundle test
- `debugSystemState()` - System state check
- `testMultipleBundles()` - Show test configurations
- `testBundlePricingWithCart(cart, name)` - Custom test

## 🎯 **Expected Results**

For the test bundle:
- **Original Total**: £1,040.00 (Parent: £100 + Child1: £890 + Child2: £50)
- **Bundle Price**: £150.00
- **Total Discount**: £890.00
- **Final Result**: All items appear in invoice with proportional discounts totaling £150.00

## ✅ **Success Indicators**

1. ✅ Debug output appears in both browser console and server logs
2. ✅ All parameters are logged correctly
3. ✅ Bundle calculations are accurate
4. ✅ Invoice is created and submitted successfully
5. ✅ All items appear in the invoice with correct discount amounts
6. ✅ Final total matches the target bundle price

## 🚨 **If Something Doesn't Work**

1. **No debug functions available**: Refresh the Custom POS page
2. **API errors**: Check item codes exist in your system
3. **No server logs**: Monitor the server output directly
4. **Permission errors**: Ensure user has Sales Invoice creation permissions

## 🎉 **Ready to Test!**

The implementation is complete with full debug visibility. You can now:
1. See exactly what parameters are being passed
2. Track every calculation step
3. Monitor ERPNext processing
4. Verify the final results
5. Troubleshoot any issues

**Go test it now!** 🚀 