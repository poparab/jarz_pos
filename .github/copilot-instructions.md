# Copilot Instructions for Jarz POS

This document provides guidance for GitHub Copilot when working on the Jarz POS project.

## Project Overview

Jarz POS is a comprehensive, touch-optimized Point of Sale (POS) system built for ERPNext/Frappe Framework. It features:

- Advanced bundle management with multi-item configurations
- Real-time inventory tracking with visual indicators
- Intelligent delivery management with city-based pricing
- Enhanced cart functionality with editing capabilities
- Customer management with recent customer quick access
- Full-screen POS interface optimized for tablets and touch screens

**Tech Stack:**
- Backend: Python 3.10+ with Frappe Framework
- Frontend: JavaScript (ES2022), HTML, CSS
- Database: MariaDB (via Frappe ORM)
- Framework: ERPNext v13/v14/v15

## Code Style and Formatting

### Python
- Follow PEP 8 guidelines with modifications defined in `pyproject.toml`
- Use **tabs** for indentation (not spaces)
- Maximum line length: 110 characters
- Use type hints where beneficial
- Use ruff for linting and formatting
- Docstrings: Use comprehensive docstrings for modules, classes, and functions
- Variable naming: Use descriptive names, snake_case for functions and variables

**Example:**
```python
def create_pos_invoice(customer: str, items: list[dict], pos_profile: str) -> dict:
	"""Create a POS invoice with proper validation.
	
	Args:
		customer: Customer ID or name
		items: List of item dictionaries with item_code, qty, rate
		pos_profile: POS Profile name
		
	Returns:
		dict: Created invoice document
	"""
	# Implementation
	pass
```

### JavaScript
- Use **tabs** for indentation (4 spaces equivalent)
- ES2022+ syntax is preferred
- Global frappe objects are available (frappe, __, cur_frm, etc.)
- Use jQuery ($) for DOM manipulation when working with Frappe
- Semicolons are optional but be consistent
- Use camelCase for variables and functions

**Example:**
```javascript
function loadRecentCustomers() {
	frappe.call({
		method: "jarz_pos.api.pos.get_recent_customers",
		callback: function(r) {
			if (r.message) {
				displayCustomers(r.message);
			}
		}
	});
}
```

### JSON
- Use **spaces** for indentation (2 spaces)
- No trailing commas
- No final newline
- Used primarily for Frappe DocType schemas

## Architecture and Patterns

### Frappe Framework Conventions
- Use `@frappe.whitelist()` decorator for API endpoints
- Always validate permissions in whitelisted methods
- Use `frappe.throw()` for user-facing errors
- Use `frappe.db.get_value()`, `frappe.get_doc()` for database operations
- Prefer Frappe ORM over raw SQL when possible
- Use `frappe.call()` for client-server communication

### Module Organization
- API endpoints: `jarz_pos/api/*.py`
- DocTypes: `jarz_pos/doctype/<doctype_name>/`
- Pages: `jarz_pos/page/<page_name>/`
- Services/Business logic: `jarz_pos/services/*.py`
- Utilities: `jarz_pos/utils/*.py`

### Modular Design
The codebase follows a modular pattern to avoid monolithic files:
- Separate concerns into focused modules (see `jarz_pos/page/custom_pos/REFACTORING_README.md`)
- Each module has a single, well-defined responsibility
- Use utility modules for shared functionality
- Maintain backward compatibility when refactoring

## Development Workflow

### Before Making Changes
1. Understand the Frappe DocType system if modifying schemas
2. Check for existing utility functions before creating new ones
3. Review `REFACTORING_README.md` for module responsibilities
4. Test with actual ERPNext installation when possible

### Code Quality
- Run pre-commit hooks before committing (configured in `.pre-commit-config.yaml`)
- Linters: ruff (Python), eslint (JavaScript), prettier (JavaScript/CSS)
- Format Python code with `ruff format`
- Follow existing patterns in similar files

### Testing
- Test API endpoints with actual Frappe site: `bench --site <site> console`
- Validate DocType changes: `bench --site <site> migrate`
- Manual testing in POS interface at `/app/custom-pos`
- Consider offline scenarios and error handling

## Key Features and Patterns

### Bundle System
- Bundles are complex multi-item configurations
- Each bundle must have an `erpnext_item` field linking to ERPNext Item
- Bundle editing allows modification after adding to cart
- Use `modules/bundle_processing.py` for bundle-related logic

### Delivery Management
- City-based delivery charges (income) and expenses
- Delivery income added as tax charges
- Delivery expense added as invoice discount
- Edit delivery expenses during checkout
- Use `services/delivery_handling.py` for delivery logic

### Customer Management
- Recent customer display (last 5 customers)
- Smart search with mobile/name pre-filling
- Customer creation with delivery address
- Use `api/pos.py` for customer-related endpoints

### Cart Operations
- Remove items and bundles with confirmation
- Edit bundles in cart with validation
- Real-time total updates
- Inventory validation before adding items

## Common Patterns

### Error Handling
```python
try:
	# Operation
	result = frappe.get_doc("DocType", name)
except frappe.DoesNotExistError:
	frappe.throw(_("Record not found"), frappe.DoesNotExistError)
except Exception as e:
	frappe.log_error(f"Error in operation: {str(e)}")
	frappe.throw(_("An error occurred"))
```

### API Responses
```python
@frappe.whitelist()
def my_api_endpoint(param1: str, param2: Optional[int] = None):
	"""API endpoint description."""
	# Validate permissions
	if not frappe.has_permission("DocType", "read"):
		frappe.throw(_("Not permitted"), frappe.PermissionError)
	
	# Business logic
	result = perform_operation(param1, param2)
	
	# Return structured response
	return {
		"success": True,
		"data": result,
		"message": _("Operation completed")
	}
```

### Client-Side API Calls
```javascript
frappe.call({
	method: "jarz_pos.api.module.function_name",
	args: {
		param1: value1,
		param2: value2
	},
	callback: function(r) {
		if (r.message) {
			// Handle success
			console.log(r.message);
		}
	},
	error: function(r) {
		// Handle error
		frappe.msgprint(__("Error occurred"));
	}
});
```

## Important Files and Directories

- `jarz_pos/hooks.py` - App configuration, whitelisted methods, workspace setup
- `jarz_pos/page/custom_pos/custom_pos.js` - Main POS interface logic
- `jarz_pos/api/pos.py` - Core POS API endpoints
- `jarz_pos/api/couriers.py` - Delivery and courier management
- `jarz_pos/services/` - Business logic layer
- `jarz_pos/utils/` - Shared utility functions
- `Prompts.txt` - Mobile app development prompts (React Native)
- `REFACTORING_README.md` - Module organization guide

## Permissions and Security

- Always check user permissions before operations
- Use `frappe.has_permission(doctype, ptype, doc)` for permission checks
- Manager-only functions should use role-based checks
- Validate all user inputs server-side
- Never trust client-side data

## Documentation

- Update README.md for user-facing changes
- Update USAGE.md for operational changes
- Document new API endpoints in docstrings
- Add comments for complex business logic
- Keep Prompts.txt updated for mobile app features

## Special Considerations

### Frappe-Specific
- Frappe uses server-side rendering with Jinja2
- DocType schemas are JSON files
- Custom fields can be added via code or UI
- Hooks system controls app lifecycle events

### POS-Specific
- POS Profile controls warehouse, price list, and user access
- Inventory checks must be real-time
- Touch-friendly UI with large buttons
- Support full-screen mode (ESC key toggle)
- Offline capability considerations for future

### Delivery System
- City doctype links to Customer Address
- Delivery income ≠ delivery expense (profit tracking)
- Account resolution: "Freight and Forwarding Charges" or fallback
- Dual accounting: income as tax, expense as discount

## Migration and Upgrades

- Custom fields added via patches in `jarz_pos/patches/`
- Database migrations run with `bench migrate`
- Preserve backward compatibility in API changes
- Use `before_uninstall` and `after_uninstall` hooks for cleanup

## Getting Help

- Check Frappe documentation: https://frappeframework.com/docs
- ERPNext documentation: https://docs.erpnext.com
- Review existing code patterns in similar features
- Console debugging: `bench --site <site> console`
- Server logs: `bench --site <site> logs`
