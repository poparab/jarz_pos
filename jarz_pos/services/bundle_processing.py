"""
Bundle Processing Service for Jarz POS

Handles bundle expansion and pricing logic for POS invoices.
Bundle logic:
1. Parent item gets 100% discount (becomes 0)
2. Child items get equal discount to match bundle price (uniform percentage)
3. Rounding correction applied on last child to ensure discounted children total equals bundle price
4. All items are added to invoice separately
"""

import frappe
from frappe import _
from frappe.utils import cint, flt


class BundleProcessor:
    """
    Handles bundle expansion and pricing logic for POS invoices
    """

    def __init__(self, bundle_code, quantity=1):
        self.bundle_code = bundle_code
        self.quantity = quantity
        self.bundle_doc = None
        self.parent_item = None
        self.bundle_items = []

    def get_item_rate(self, item_code):
        """
        Get the selling rate for an item from price list or standard selling rate
        """
        try:
            # First try to get item document and check standard selling rate
            item_doc = frappe.get_doc("Item", item_code)
            if item_doc.standard_rate and flt(item_doc.standard_rate) > 0:
                return flt(item_doc.standard_rate)

            # Try to get from selling price lists
            price_list_entries = frappe.get_all("Item Price",
                filters={
                    "item_code": item_code,
                    "selling": 1,
                    "price_list_rate": [">", 0]
                },
                fields=["price_list_rate"],
                order_by="creation desc",
                limit=1
            )

            if price_list_entries:
                return flt(price_list_entries[0].price_list_rate)

            # Last resort: check if item has any valuation rate
            if item_doc.valuation_rate and flt(item_doc.valuation_rate) > 0:
                return flt(item_doc.valuation_rate)

            frappe.log_error(f"No rate found for item: {item_code}, setting default rate of 100", "Bundle Processing")
            return 100.0  # Default fallback rate

        except Exception as e:
            frappe.log_error(f"Error getting rate for {item_code}: {e!s}", "Bundle Processing")
            return 100.0  # Default fallback rate

    def load_bundle(self):
        """Load bundle document and validate"""
        try:
            # Get the bundle document using Frappe API
            self.bundle_doc = frappe.get_doc("Jarz Bundle", self.bundle_code)

            if not self.bundle_doc:
                frappe.throw(_("Bundle {0} not found").format(self.bundle_code))

            # Use the erpnext_item field as the parent item (this is the key fix!)
            if not self.bundle_doc.erpnext_item:
                frappe.throw(_("Bundle {0} has no ERPNext item configured").format(self.bundle_code))

            # The parent item is the ERPNext item from the bundle
            self.parent_item = frappe.get_doc("Item", self.bundle_doc.erpnext_item)

            # Load bundle items from the child table (items is the table field)
            for item_group_row in self.bundle_doc.items:
                # The bundle item group contains item_group (not item_code) and quantity (not qty)
                # We need to get items from this item group
                item_group_name = item_group_row.item_group
                item_quantity = item_group_row.quantity

                # Get the first available item from this item group
                items_in_group = frappe.get_all("Item",
                    filters={
                        "item_group": item_group_name,
                        "disabled": 0,
                        "has_variants": 0
                    },
                    fields=["name", "item_name", "standard_rate", "stock_uom"],
                    limit=1)

                if not items_in_group:
                    frappe.throw(f"No available items found in item group '{item_group_name}'")

                selected_item = items_in_group[0]
                item_doc = frappe.get_doc("Item", selected_item['name'])

                self.bundle_items.append({
                    'item': item_doc,
                    'qty': item_quantity,
                    'rate': self.get_item_rate(item_doc.name),
                    'item_group': item_group_name
                })

            frappe.log_error(f"Bundle loaded: {self.bundle_code}, ERPNext Item: {self.parent_item.name}, Child Items: {len(self.bundle_items)}", "Bundle Processing")

        except Exception as e:
            frappe.log_error(f"Bundle loading error: {e!s}", "Bundle Processing")
            raise

    def calculate_child_discount_percentage(self):
        """Return uniform discount percentage for child items ensuring bundle price match.
        Raises if bundle_price > total child gross (cannot inflate price when parent is 100% discounted).
        """
        bundle_price = flt(self.bundle_doc.bundle_price)
        if bundle_price <= 0:
            frappe.throw(_(f"Bundle {self.bundle_code} price is not set or invalid"))

        total_child_price = sum([
            flt(item['rate']) * flt(item['qty']) * self.quantity
            for item in self.bundle_items
        ])

        if total_child_price <= 0:
            frappe.throw(_(f"Bundle {self.bundle_code} has zero total child price"))

        if bundle_price > total_child_price + 1e-9:  # small epsilon
            frappe.throw(_(f"Bundle price ({bundle_price}) exceeds sum of child item prices ({total_child_price}) for bundle {self.bundle_code}"))

        # discount% = ((total_child_price - bundle_price) / total_child_price) * 100
        discount_percentage = ((total_child_price - bundle_price) / total_child_price) * 100.0
        discount_percentage = max(0.0, min(100.0, discount_percentage))  # clamp

        return discount_percentage, total_child_price, bundle_price

    def calculate_bundle_discount(self):
        """
        Calculate discount percentage for child items
        Parent gets 100% discount, children get equal discount to match bundle price
        """
        # Get bundle target price
        bundle_price = flt(self.bundle_doc.bundle_price)

        # Calculate total price of all child items without discount
        total_child_price = sum([
            flt(item['rate']) * flt(item['qty']) * self.quantity
            for item in self.bundle_items
        ])

        if total_child_price == 0:
            return 0

        # Calculate discount percentage needed for children
        # Formula: discount% = ((total_price - target_price) / total_price) * 100
        discount_percentage = ((total_child_price - bundle_price) / total_child_price) * 100

        # Ensure discount is not negative
        calculated_discount = max(0, discount_percentage)

        frappe.log_error(f"Bundle discount calculation: Bundle price={bundle_price}, Total child price={total_child_price}, Discount={calculated_discount}%", "Bundle Processing")

        return calculated_discount

    def get_invoice_items(self):
        """Get formatted items for invoice following ERPNext discount logic exactly.
        Key insight: ERPNext prioritizes discount_percentage over discount_amount.
        For 100% discount (parent): use discount_percentage = 100
        For partial discount (children): use discount_percentage only, let ERPNext compute discount_amount
        """
        if not self.bundle_doc:
            self.load_bundle()

        invoice_items = []

        # Precision discovery
        try:
            rate_precision = frappe.get_precision("Sales Invoice Item", "rate") or 2
        except Exception:
            rate_precision = 2
        try:
            amount_precision = frappe.get_precision("Sales Invoice Item", "amount") or 2
        except Exception:
            amount_precision = 2

        # Parent line – 100% discount via discount_percentage (ERPNext will set rate = 0.0)
        parent_rate = self.get_item_rate(self.parent_item.name)
        parent_line = {
            'item_code': self.parent_item.name,
            'item_name': self.parent_item.item_name,
            'description': f"Bundle: {self.parent_item.description or self.parent_item.item_name}",
            'qty': self.quantity,
            'rate': flt(parent_rate, rate_precision),  # Include rate for compatibility
            'price_list_rate': flt(parent_rate, rate_precision),  # CRITICAL: Set price_list_rate
            'discount_percentage': 100.0,  # CRITICAL: ERPNext will set rate = 0.0 automatically
            'is_bundle_parent': 1,
            'bundle_code': self.bundle_code
        }
        invoice_items.append(parent_line)

        # Compute uniform percentage for children
        uniform_pct, _total_child_gross, bundle_price = self.calculate_child_discount_percentage()

        # First pass children using discount_percentage only
        child_lines = []
        running_children_discounted_total = 0.0
        for bi in self.bundle_items:
            unit_rate = flt(bi['rate'], rate_precision)
            qty_total = flt(bi['qty']) * self.quantity
            # ERPNext will compute: rate = price_list_rate * (1 - discount_percentage/100)
            expected_discounted_rate = unit_rate * (1 - uniform_pct / 100.0)
            line_discounted_total = flt(expected_discounted_rate * qty_total, amount_precision)
            running_children_discounted_total += line_discounted_total

            child_lines.append({
                'item_code': bi['item'].name,
                'item_name': bi['item'].item_name,
                'description': bi['item'].description or bi['item'].item_name,
                'qty': qty_total,
                'rate': unit_rate,  # Include rate for compatibility
                'price_list_rate': unit_rate,  # CRITICAL: Set price_list_rate for ERPNext
                'discount_percentage': uniform_pct,  # CRITICAL: Let ERPNext compute rate and discount_amount
                'is_bundle_child': 1,
                'parent_bundle': self.bundle_code,
                '_unit_rate': unit_rate,
                '_qty_total': qty_total,
                '_expected_line_total': line_discounted_total
            })

        # Rounding correction: adjust last child's discount_percentage to hit exact bundle_price
        target_children_total = flt(bundle_price, amount_precision)
        current_children_total = flt(running_children_discounted_total, amount_precision)
        residual = flt(target_children_total - current_children_total, amount_precision)

        min_step = 1 / (10 ** amount_precision)
        if child_lines and abs(residual) >= min_step:
            last = child_lines[-1]
            unit_rate = last['_unit_rate']
            qty_total = last['_qty_total']
            current_expected_line_total = last['_expected_line_total']

            # Calculate what the last line total should be to hit exact target
            desired_last_line_total = flt(current_expected_line_total + residual, amount_precision)
            desired_last_line_total = min(max(0.0, desired_last_line_total), unit_rate * qty_total)

            # Calculate required discount percentage for last line
            if unit_rate > 0 and qty_total > 0:
                desired_unit_rate = desired_last_line_total / qty_total
                adjusted_discount_pct = ((unit_rate - desired_unit_rate) / unit_rate) * 100.0
                adjusted_discount_pct = min(max(0.0, adjusted_discount_pct), 100.0)
                last['discount_percentage'] = flt(adjusted_discount_pct, 6)  # High precision for percentage

        # Cleanup helper fields
        for cl in child_lines:
            cl.pop('_unit_rate', None)
            cl.pop('_qty_total', None)
            cl.pop('_expected_line_total', None)

        invoice_items.extend(child_lines)

        frappe.logger("jarz_pos.bundle").info({
            'bundle': self.bundle_code,
            'parent_discount_pct': 100.0,
            'children_uniform_discount_pct': uniform_pct,
            'bundle_price_target': bundle_price,
            'children_total_expected': target_children_total,
            'residual_adjustment': residual,
            'approach': 'discount_percentage_only_erpnext_native'
        })
        return invoice_items


def process_bundle_for_invoice(bundle_identifier, quantity=1):
    """
    Main function to process bundle for invoice

    Args:
        bundle_identifier (str): Could be either:
            - ERPNext Item code (from bundle.erpnext_item field)
            - Jarz Bundle record ID
        quantity (int): Quantity of bundles

    Returns:
        list: List of invoice items (parent + children with discounts)
    """
    try:
        frappe.log_error(f"🔍 Processing bundle identifier: '{bundle_identifier}'", "Bundle Processing")

        bundle_code = None
        bundle_doc = None

        # Try to find bundle by erpnext_item first (preferred method)
        bundle_records = frappe.get_all("Jarz Bundle",
            filters={"erpnext_item": bundle_identifier},
            fields=["name", "bundle_name", "erpnext_item", "bundle_price"],
            limit=1)

        if bundle_records:
            bundle_code = bundle_records[0]["name"]
            frappe.log_error(f"✅ Found bundle by erpnext_item: {bundle_code} ({bundle_records[0]['bundle_name']})", "Bundle Processing")
        else:
            # Try to find it as a direct bundle record ID
            if frappe.db.exists("Jarz Bundle", bundle_identifier):
                bundle_code = bundle_identifier
                bundle_doc = frappe.get_doc("Jarz Bundle", bundle_code)
                frappe.log_error(f"✅ Found bundle by record ID: {bundle_code} ({bundle_doc.bundle_name})", "Bundle Processing")
            else:
                frappe.throw(f"No Jarz Bundle found for identifier '{bundle_identifier}'. Checked both erpnext_item field and bundle record ID.")

        # Process the bundle using the bundle record ID
        processor = BundleProcessor(bundle_code, quantity)
        result = processor.get_invoice_items()

        frappe.log_error(f"✅ Bundle processing complete: {len(result)} items generated", "Bundle Processing")
        return result

    except Exception as e:
        frappe.log_error(f"❌ Bundle processing error for identifier {bundle_identifier}: {e!s}", "Bundle Processing")
        raise


def validate_bundle_configuration_by_item(bundle_identifier):
    """
    Validate bundle configuration by identifier (ERPNext item code or bundle ID)

    Args:
        bundle_identifier (str): Could be ERPNext item code or bundle record ID

    Returns:
        tuple: (is_valid, message, bundle_code)
    """
    try:
        bundle_code = None

        # Try to find the bundle by erpnext_item first
        bundle_records = frappe.get_all("Jarz Bundle",
            filters={"erpnext_item": bundle_identifier},
            fields=["name"],
            limit=1)

        if bundle_records:
            bundle_code = bundle_records[0]["name"]
        elif frappe.db.exists("Jarz Bundle", bundle_identifier):
            # Try as direct bundle record ID
            bundle_code = bundle_identifier
        else:
            return False, f"No Jarz Bundle found for identifier '{bundle_identifier}'", None

        # Use existing validation function
        is_valid, message = validate_bundle_configuration(bundle_code)
        return is_valid, message, bundle_code

    except Exception as e:
        return False, f"Bundle validation error: {e!s}", None


def validate_bundle_configuration(bundle_code):
    """
    Validate bundle configuration before processing
    """
    try:
        # Get bundle document using Frappe API
        bundle_doc = frappe.get_doc("Jarz Bundle", bundle_code)

        if not bundle_doc:
            return False, f"Bundle {bundle_code} not found"

        # Check ERPNext item exists (this is the parent item)
        if not bundle_doc.erpnext_item:
            return False, "Bundle has no ERPNext item configured"

        # Check ERPNext item is valid
        if not frappe.db.exists("Item", bundle_doc.erpnext_item):
            return False, f"ERPNext item {bundle_doc.erpnext_item} does not exist"

        # Check bundle has child items
        if not bundle_doc.items:
            return False, "Bundle has no child items configured"

        # Check all child item groups exist and have items
        for item_group_row in bundle_doc.items:
            item_group_name = item_group_row.item_group

            # Check if item group exists
            if not frappe.db.exists("Item Group", item_group_name):
                return False, f"Item group {item_group_name} does not exist"

            # Check if there are items in this group
            items_in_group = frappe.get_all("Item",
                filters={
                    "item_group": item_group_name,
                    "disabled": 0
                },
                limit=1)

            if not items_in_group:
                return False, f"No available items found in item group {item_group_name}"

        # Check bundle price is set
        if not bundle_doc.bundle_price or flt(bundle_doc.bundle_price) <= 0:
            return False, "Bundle price not set or invalid"

        return True, "Bundle configuration is valid"

    except Exception as e:
        return False, f"Bundle validation error: {e!s}"


# Legacy function for backward compatibility
def process_bundle_item(bundle_id, bundle_qty, bundle_price, selling_price_list):
    """
    Legacy function - redirects to new implementation

    Args:
        bundle_id (str): Could be either Jarz Bundle ID or ERPNext Item code
        bundle_qty (int): Quantity
        bundle_price (float): Price (not used in new implementation)
        selling_price_list (str): Price list (not used in new implementation)

    Returns:
        list: Processed bundle items
    """
    try:
        # Try to process as ERPNext item first (new way)
        return process_bundle_for_invoice(bundle_id, bundle_qty)
    except Exception as e:
        frappe.log_error(f"Legacy bundle processing failed for {bundle_id}: {e!s}", "Bundle Processing")

        # If that fails, try to find if bundle_id is actually a Jarz Bundle record ID
        try:
            bundle_doc = frappe.get_doc("Jarz Bundle", bundle_id)
            if bundle_doc.erpnext_item:
                return process_bundle_for_invoice(bundle_doc.erpnext_item, bundle_qty)
        except Exception:
            pass

        # If all fails, re-raise the original error
        raise e

@frappe.whitelist()
def test_bundle_pricing(bundle_identifier, qty: int = 1):
    """Utility endpoint to test bundle pricing & rounding.
    Returns diagnostic info including computed discount percentage and reconciliation.
    Usage (bench):
    bench execute jarz_pos.services.bundle_processing.test_bundle_pricing --kwargs '{"bundle_identifier": "ITEM-CODE", "qty": 2}'
    """
    processor = BundleProcessor(bundle_identifier, qty)
    processor.load_bundle()
    discount_pct, total_child_price, bundle_price = processor.calculate_child_discount_percentage()
    items = processor.get_invoice_items()
    child_discounted_sum = 0.0
    for it in items:
        if it.get('is_bundle_child'):
            # emulate ERPNext discounted amount computation
            line_original = it['rate'] * it['qty']
            line_discount = line_original * (it.get('discount_percentage', 0) / 100.0)
            child_discounted_sum += flt(line_original - line_discount, 2)
    return {
        'bundle_identifier': bundle_identifier,
        'qty': qty,
        'discount_percentage_uniform_base': discount_pct,
        'bundle_price': bundle_price,
        'total_child_gross': total_child_price,
        'children_discounted_sum': flt(child_discounted_sum, 2),
        'difference': flt(bundle_price - child_discounted_sum, 2),
        'items_generated': items
    }
