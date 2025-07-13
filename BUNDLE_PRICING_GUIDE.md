# Bundle Pricing Implementation Guide

## Overview

This guide explains the new **ERPNext-compliant bundle pricing implementation** using the `discount_amount` approach. This method ensures proper stock tracking, accurate accounting, and individual item profitability analysis while achieving the desired bundle pricing.

## Key Features

✅ **ERPNext Compliant**: Uses standard `discount_amount` field instead of zero rates  
✅ **Stock Tracking**: All items properly tracked in inventory  
✅ **Item Profitability**: Individual item revenues recorded for analysis  
✅ **Accounting Accuracy**: Proper GL entries and financial reporting  
✅ **Proportional Discounts**: Fair distribution of discounts across all items  

## How It Works

### 1. Bundle Structure
- **Parent Item**: Bundle container item (appears on invoice)
- **Child Items**: Actual items being sold (tracked in stock)
- **Bundle Price**: Final total price customer pays

### 2. Calculation Method

```
Original Total = Parent Price + Sum(Child Item Prices)
Total Discount = Original Total - Bundle Price
```

Each item gets a proportional discount:
```
Item Discount = Total Discount × (Item Value / Original Total)
Final Item Price = Original Price - Item Discount
```

### 3. Example Calculation

**Bundle Setup:**
- Parent Item: £100.00
- Child Item 1: £890.00 (Coffee Mug)
- Child Item 2: £50.00 (Television)
- **Bundle Price: £150.00**

**Calculation:**
```
Original Total: £100 + £890 + £50 = £1,040.00
Total Discount: £1,040 - £150 = £890.00

Parent Discount: £890 × (£100/£1,040) = £85.58
Child 1 Discount: £890 × (£890/£1,040) = £761.63  
Child 2 Discount: £890 × (£50/£1,040) = £42.79

Final Prices:
- Parent: £100.00 - £85.58 = £14.42
- Child 1: £890.00 - £761.63 = £128.37
- Child 2: £50.00 - £42.79 = £7.21
- Total: £150.00 ✅
```

## Implementation Details

### Code Structure

The main function `process_bundle_item()` in `custom_pos.py` handles:

1. **Price Retrieval**: Gets prices from Item Price doctype or Item.standard_rate
2. **Discount Calculation**: Proportional discount distribution
3. **Invoice Creation**: Adds items with proper `discount_amount` values

### Key Functions

```python
def process_bundle_item(si, bundle, selling_price_list):
    """Process bundle using discount_amount approach"""
    
def get_item_price(item_code, price_list):
    """Get item price with fallback to standard_rate"""
```

### Sales Invoice Fields Used

- `rate`: Original item price
- `discount_amount`: Calculated discount per item
- `amount`: Final price after discount
- `price_list_rate`: Reference price
- `ignore_pricing_rule`: Prevents ERPNext override

## Usage

### 1. Frontend Cart Data Structure

```javascript
const cartData = [{
    "is_bundle": true,
    "item_code": "BUNDLE_PARENT_ITEM",
    "bundle_name": "Special Bundle",
    "price": 150.00,  // Final bundle price
    "items": [
        {"item_code": "CHILD_ITEM_1", "qty": 1},
        {"item_code": "CHILD_ITEM_2", "qty": 2}
    ]
}];
```

### 2. API Call

```javascript
frappe.call({
    method: "jarz_pos.jarz_pos.page.custom_pos.custom_pos.create_sales_invoice",
    args: {
        cart_json: JSON.stringify(cartData),
        customer_name: "Customer Name",
        pos_profile_name: "POS Profile"
    },
    callback: function(r) {
        console.log("Invoice created:", r.message);
    }
});
```

### 3. Result

The system creates a Sales Invoice with:
- All items listed individually
- Proper stock movements
- Accurate accounting entries
- Individual item revenue tracking

## Benefits Over Previous Approaches

### ❌ Zero Rate Approach (Old)
- Items showed £0.00 rates
- Poor accounting accuracy  
- No individual profitability
- ERPNext override issues

### ✅ Discount Amount Approach (New)
- Items show actual prices
- Accurate financial reporting
- Individual item profitability 
- ERPNext compliant workflow

## Stock & Accounting Impact

### Stock Ledger Entries
```
BUNDLE_PARENT_ITEM: -1 qty (out of stock)
CHILD_ITEM_1: -1 qty (out of stock)  
CHILD_ITEM_2: -2 qty (out of stock)
```

### General Ledger Entries
```
Dr. Customer Account: £150.00
Cr. Income Account (Parent): £14.42
Cr. Income Account (Child 1): £128.37
Cr. Income Account (Child 2): £7.21
```

## Business Intelligence Benefits

### Profitability Analysis
- Track which bundle items are most profitable
- Analyze individual item performance
- Make data-driven pricing decisions

### Inventory Management  
- Proper stock levels for all items
- Accurate reorder points
- Real inventory valuation

### Financial Reporting
- Accurate revenue recognition
- Proper cost allocation
- Compliant accounting practices

## Testing

The implementation includes comprehensive tests:

1. **Calculation Accuracy**: Verifies math is correct
2. **Stock Movements**: Ensures inventory tracking
3. **Accounting Entries**: Validates GL entries
4. **Data Structure**: Tests cart processing

Run tests:
```bash
python test_bundle_manual.py
```

## Troubleshooting

### Common Issues

1. **"No price found" errors**
   - Ensure items have prices in Item Price doctype
   - Check price list is correct
   - Verify Item.standard_rate as fallback

2. **Bundle price higher than item total**
   - System warns but continues without discount
   - Review pricing strategy

3. **Stock errors**
   - Ensure sufficient stock for all items
   - Check warehouse settings in POS Profile

### Debug Tips

- Check browser console for API errors
- Review server logs for detailed error messages
- Verify item codes match exactly
- Confirm POS Profile configuration

## Migration from Old System

To migrate from the old zero-rate system:

1. **No data migration needed** - old invoices remain unchanged
2. **Update frontend** to use new cart structure (if needed)
3. **Test thoroughly** with sample bundles
4. **Monitor** first few live transactions

## Conclusion

This ERPNext-compliant bundle pricing system provides:
- Accurate financial reporting
- Proper inventory management  
- Individual item profitability tracking
- Regulatory compliance
- Future-proof architecture

The proportional discount approach ensures fairness while maintaining the simplicity of bundle pricing for customers. 