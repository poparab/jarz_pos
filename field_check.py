#!/usr/bin/env python3

import frappe

def check_sales_invoice_fields():
    """Check custom fields in Sales Invoice"""
    try:
        # Get all fields in Sales Invoice
        meta = frappe.get_meta('Sales Invoice')
        
        print("=== ALL CUSTOM FIELDS IN SALES INVOICE ===")
        custom_fields = [f for f in meta.fields if f.fieldname.startswith('custom_')]
        
        for field in custom_fields:
            print(f"Field: {field.fieldname}")
            print(f"Label: {field.label}")
            print(f"Type: {field.fieldtype}")
            print("-" * 40)
        
        print(f"\nTotal custom fields found: {len(custom_fields)}")
        
        print("\n=== DELIVERY/DATETIME RELATED FIELDS ===")
        delivery_fields = []
        for field in meta.fields:
            field_name_lower = field.fieldname.lower()
            field_label_lower = (field.label or '').lower()
            
            if any(keyword in field_name_lower or keyword in field_label_lower 
                  for keyword in ['delivery', 'datetime', 'required', 'time']):
                delivery_fields.append(field)
                print(f"Field: {field.fieldname}")
                print(f"Label: {field.label}")
                print(f"Type: {field.fieldtype}")
                print("-" * 40)
        
        print(f"\nTotal delivery/datetime fields found: {len(delivery_fields)}")
        
        # Also check Custom Field doctype
        print("\n=== CUSTOM FIELD DOCTYPE RECORDS ===")
        custom_field_records = frappe.get_all(
            'Custom Field',
            filters={'dt': 'Sales Invoice'},
            fields=['fieldname', 'label', 'fieldtype']
        )
        
        for record in custom_field_records:
            print(f"Field: {record.fieldname}")
            print(f"Label: {record.label}")
            print(f"Type: {record.fieldtype}")
            print("-" * 40)
            
        print(f"\nTotal Custom Field records found: {len(custom_field_records)}")
        
    except Exception as e:
        print(f"Error: {str(e)}")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    check_sales_invoice_fields()
