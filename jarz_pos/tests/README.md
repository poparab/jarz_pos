# Jarz POS Test Suite

This directory contains the comprehensive test suite for Jarz POS.

## Test Coverage

### Total Statistics
- **Test Files**: 30
- **Test Methods**: 217+
- **API Modules Covered**: 17/20 (85%)
- **Service Modules Covered**: 6/7 (86%)
- **Utility Modules Covered**: 4/4 (100%)

**Latest Addition:** Complete coverage for all 6 invoice cases and settlement strategies (95 new tests).

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

### Business Logic Tests (7 files)
- `test_bundle_processing.py` - Bundle expansion and pricing
- `test_discount_calculation.py` - Discount calculations
- `test_settlement_strategies.py` - **NEW:** All 4 settlement handlers (14 tests)
- `test_sales_partner_flow.py` - **NEW:** Sales partner fees and accounting (16 tests)
- `test_pickup_flow.py` - **NEW:** Pickup detection and zero-shipping (20 tests)
- `test_kanban_settlement.py` - **NEW:** Kanban and settle later operations (25 tests)
- `test_invoice_cases_integration.py` - **NEW:** End-to-end invoice cases (20 tests)

### Utility Tests (4 files)
- `test_utils_account.py` - Account utilities
- `test_utils_delivery.py` - Delivery utilities
- `test_utils_error_handler.py` - Error handling
- `test_utils_invoice.py` - Invoice utilities

### Enhanced Existing Tests (2 files)
- `test_kanban.py` - Kanban board functionality
- `test_invoice_utils_extended.py` - Extended invoice utilities

## Six Invoice Cases - Complete Coverage

The test suite now includes comprehensive end-to-end testing for all six invoice scenarios:

1. **Paid + Settle Now** - Customer pays upfront, courier settles immediately
2. **Paid + Settle Later** - Customer pays upfront, courier settles at period end
3. **Unpaid + Settle Now** - COD with immediate courier settlement
4. **Unpaid + Settle Later** - COD with deferred courier settlement
5. **Sales Partner** - Both cash and online payment variants with fee calculation
6. **Pickup** - Zero shipping, works with all settlement combinations

See `test_invoice_cases_integration.py` for complete integration tests and [INVOICE_CASES_TEST_COVERAGE.md](../../INVOICE_CASES_TEST_COVERAGE.md) for detailed documentation.

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
- **Version**: 1.0.0
- **Maintainer**: Jarz POS Team
