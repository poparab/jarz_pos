# Jarz POS Test Suite

This directory contains the comprehensive test suite for Jarz POS.

## Test Coverage

### Total Statistics
- **Test Files**: 28
- **Test Methods**: 213
- **API Modules Covered**: 17/20 (85%)
- **Service Modules Covered**: 5/7 (71%)
- **Utility Modules Covered**: 4/4 (100%)

## Test Files

### API Endpoint Tests (17 files)
- `test_api_cash_transfer.py` - Cash transfer and account management
- `test_api_couriers.py` - Courier and delivery operations
- `test_api_customer.py` - Customer management
- `test_api_delivery_slots.py` - Delivery slot management
- `test_api_global_methods.py` - Global method wrappers
- `test_api_health.py` - Health check endpoint
- `test_api_inventory_count.py` - Inventory reconciliation
- `test_api_invoices.py` - Invoice creation and management
- `test_api_maintenance.py` - System maintenance
- `test_api_manager.py` - Manager dashboard and orders
- `test_api_manufacturing.py` - Work orders and BOM
- `test_api_notifications.py` - Real-time notifications
- `test_api_pos.py` - POS profiles and products
- `test_api_purchase.py` - Purchase operations
- `test_api_test_connection.py` - Connection and health checks
- `test_api_transfer.py` - Transfer operations
- `test_api_user.py` - User roles and permissions

### Business Logic Tests (5 files)
- `test_bundle_processing.py` - Bundle expansion and pricing
- `test_discount_calculation.py` - Discount calculations
- `test_settlement_strategies.py` ⭐ NEW - All 6 settlement cases
- `test_kanban_state_transitions.py` ⭐ NEW - Kanban state flows
- `test_invoice_creation_cases.py` ⭐ NEW - POS invoice cases

### Utility Tests (4 files)
- `test_utils_account.py` - Account utilities
- `test_utils_delivery.py` - Delivery utilities
- `test_utils_error_handler.py` - Error handling
- `test_utils_invoice.py` - Invoice utilities

### Enhanced Existing Tests (2 files)
- `test_kanban.py` - Kanban board functionality
- `test_invoice_utils_extended.py` - Extended invoice utilities

## Running Tests

### All Tests
```bash
bench --site <site-name> run-tests --app jarz_pos
```

### Specific Module
```bash
bench --site <site-name> run-tests --app jarz_pos --module jarz_pos.tests.test_api_user
```

### With Coverage
```bash
bench --site <site-name> run-tests --app jarz_pos --coverage
```

## Test Principles

1. **Structure Validation**: All API tests validate response structure
2. **Input Validation**: Tests verify required parameter checking
3. **Error Handling**: Tests handle missing data gracefully
4. **Permission Handling**: Tests account for role-based access
5. **Edge Cases**: Tests cover boundary conditions and special cases

## Code Quality

All tests pass:
- ✅ Python syntax validation
- ✅ Ruff linter (F, E rules)
- ✅ Code formatting (ruff format)
- ✅ Import organization

## Documentation

For detailed testing guide, see [TESTING.md](../TESTING.md) in the root directory.

## Contributing

When adding new functionality:
1. Write tests first (TDD)
2. Cover happy path and edge cases
3. Document test purpose
4. Follow existing patterns
5. Ensure all checks pass

## Last Updated

- **Date**: 2025-10-10
- **Version**: 1.1.0
- **Maintainer**: Jarz POS Team

## Six Invoice Cases

All 6 invoice settlement cases are fully documented and tested:
1. Unpaid + Settle Now
2. Unpaid + Settle Later
3. Paid + Settle Now
4. Paid + Settle Later
5. Sales Partner Flow
6. Pickup Flow

See [SIX_INVOICE_CASES.md](../../SIX_INVOICE_CASES.md) for detailed business logic documentation.
