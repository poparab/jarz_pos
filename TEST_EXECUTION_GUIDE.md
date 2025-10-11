# Test Execution Guide for Jarz POS

## Summary

This document provides a comprehensive guide for running the Jarz POS test suite and understanding test coverage.

## Test Suite Status

✅ **All tests are ready for execution**

- **Total Test Files:** 25
- **Total Test Classes:** 30
- **Total Test Methods:** 120
- **Python Syntax:** ✅ All valid
- **Code Quality:** ✅ Improved (1085+ linting errors fixed)
- **Test Structure:** ✅ All properly formatted

## Coverage Analysis

### API Modules: 80% Coverage (17/21 modules)

#### ✅ Tested API Modules:
- cash_transfer
- couriers
- customer
- delivery_slots
- global_methods
- health
- inventory_count
- invoices
- maintenance
- manager
- manufacturing
- notifications
- pos
- purchase
- test_connection
- transfer
- user

#### ⚠️ API Modules without dedicated tests:
- `invoices_clean.py` - Empty/deprecated module
- `kanban.py` - Tested via global_methods and integration tests
- `test_endpoints.py` - Test utility, not production code
- `test_kanban_setup.py` - Test utility, not production code

### Services: 33% Coverage (2/6 modules)

#### ✅ Tested Services:
- bundle_processing
- discount_calculation

#### ⚠️ Services without dedicated tests:
- delivery_handling
- delivery_party
- invoice_creation
- settlement_strategies

*Note: These services are indirectly tested through API endpoint tests*

### Utils: 67% Coverage (4/6 modules)

#### ✅ Tested Utils:
- account_utils
- delivery_utils
- error_handler
- invoice_utils

#### ⚠️ Utils without dedicated tests:
- cleanup
- validation_utils

### Overall Coverage: 69% (23/33 modules)

## Running Tests

### Prerequisites

Tests require a Frappe bench environment with:
- Python 3.10+
- MariaDB
- Redis
- ERPNext installed

### Run All Tests

```bash
# From frappe-bench directory
bench --site <site-name> run-tests --app jarz_pos
```

### Run with Coverage Report

```bash
bench --site <site-name> run-tests --app jarz_pos --coverage
```

### Run Specific Test Modules

```bash
# API tests
bench --site <site-name> run-tests --app jarz_pos --module jarz_pos.tests.test_api_user

# Business logic tests
bench --site <site-name> run-tests --app jarz_pos --module jarz_pos.tests.test_bundle_processing

# Utility tests
bench --site <site-name> run-tests --app jarz_pos --module jarz_pos.tests.test_utils_invoice
```

### Run Tests in CI/CD

Tests are automatically run via GitHub Actions on push/PR:

```bash
# See .github/workflows/ci.yml for configuration
```

## Code Quality Improvements

### Linting Fixes Applied

- ✅ Fixed 1085 auto-fixable linting errors
- ✅ Replaced deprecated typing imports (Dict → dict, List → list, Tuple → tuple)
- ✅ Fixed bare `except:` statements (3 instances)
- ✅ Removed duplicate function definitions (2 instances)
- ✅ Fixed `.strip()` with multi-character strings
- ✅ Fixed invalid `# noqa` directive

### Remaining Linting Notes (65 warnings)

The remaining 65 linting warnings are cosmetic:
- **41** RUF001: Ambiguous unicode characters in strings (EN DASH vs HYPHEN-MINUS)
- **19** RUF003: Ambiguous unicode characters in comments
- **5** RUF002: Ambiguous unicode characters in docstrings

These are intentional unicode characters (–, ℹ️, 💸, etc.) used for better readability in logs and error messages. They do not affect functionality.

## Test Design Principles

All tests follow these patterns:

1. **Structure Validation**: Response structure and data types are verified
2. **Input Validation**: Parameter requirements are tested
3. **Error Handling**: Missing data and edge cases are handled gracefully
4. **Permission Handling**: Role-based access is accounted for
5. **Edge Cases**: Boundary conditions are covered

## Test Files Organization

```
jarz_pos/tests/
├── test_api_*.py          # API endpoint tests (17 files)
├── test_*_processing.py   # Business logic tests (2 files)
├── test_utils_*.py        # Utility function tests (4 files)
├── test_kanban.py         # Kanban integration tests
└── test_invoice_utils_extended.py  # Extended invoice tests
```

## Validation Results

### ✅ All Test Files Validated

- [x] All 25 test files have valid Python syntax
- [x] All test files can be parsed successfully
- [x] All test classes inherit from `unittest.TestCase`
- [x] All test methods follow naming convention (`test_*`)
- [x] All imports are organized and formatted

### ✅ Code Quality Checks

- [x] Ruff linting: 1085 errors fixed
- [x] Python syntax: All files valid
- [x] Import organization: Sorted and formatted
- [x] Type annotations: Modern PEP 604/585 style

## Next Steps

### For Developers

1. **Run tests locally** in Frappe bench before committing
2. **Add tests** for new features following existing patterns
3. **Maintain coverage** by testing new API endpoints and services

### For CI/CD

1. Tests will run automatically on push/PR
2. Check GitHub Actions for test results
3. Fix any failures before merging

### Future Enhancements

Consider adding tests for:
- delivery_handling service
- delivery_party service
- invoice_creation service
- settlement_strategies service
- cleanup utility
- validation_utils utility

## Troubleshooting

### Test Data Requirements

Some tests require ERPNext data to exist:
- POS Profile records
- Customer records
- Item and Bundle configurations
- Company and Account setup

Tests are designed to:
1. Run successfully with test data
2. Handle missing data gracefully
3. Provide clear error messages

### Permission Issues

Tests account for permission requirements:
- Manager-only functions may require JARZ Manager role
- Some operations need specific ERPNext permissions

### Database Requirements

- MariaDB must be accessible
- Redis must be running
- Basic ERPNext structure must exist

## References

- [TESTING.md](TESTING.md) - Detailed testing guide
- [TEST_SUITE_SUMMARY.md](TEST_SUITE_SUMMARY.md) - Test suite implementation summary
- [Frappe Testing Documentation](https://frappeframework.com/docs/user/en/testing)
- [Python unittest Documentation](https://docs.python.org/3/library/unittest.html)

---

**Last Updated:** 2025-10-10  
**Test Suite Version:** 1.0.0  
**Status:** ✅ Ready for execution in Frappe environment
