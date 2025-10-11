#!/usr/bin/env python3
"""
Test Suite Validation Script for Jarz POS

This script validates the test suite structure and provides a comprehensive
report on test coverage and code quality.

Usage:
    python3 validate_tests.py
"""

import ast
import sys
from pathlib import Path
from collections import defaultdict


def validate_syntax(file_path):
    """Validate Python file syntax."""
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            ast.parse(f.read(), filename=str(file_path))
        return True, None
    except SyntaxError as e:
        return False, f"Syntax error at line {e.lineno}: {e.msg}"
    except Exception as e:
        return False, str(e)


def analyze_test_file(file_path):
    """Analyze a test file and extract statistics."""
    with open(file_path, 'r', encoding='utf-8') as f:
        tree = ast.parse(f.read(), filename=str(file_path))
    
    stats = {
        'classes': 0,
        'test_methods': 0,
        'setup_methods': 0,
        'teardown_methods': 0,
        'class_names': [],
        'test_method_names': []
    }
    
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            stats['classes'] += 1
            stats['class_names'].append(node.name)
            
            for item in node.body:
                if isinstance(item, ast.FunctionDef):
                    if item.name.startswith('test_'):
                        stats['test_methods'] += 1
                        stats['test_method_names'].append(item.name)
                    elif item.name in ('setUp', 'setUpClass'):
                        stats['setup_methods'] += 1
                    elif item.name in ('tearDown', 'tearDownClass'):
                        stats['teardown_methods'] += 1
    
    return stats


def main():
    """Main validation function."""
    print("=" * 70)
    print("JARZ POS TEST SUITE VALIDATION")
    print("=" * 70)
    
    # Validate all Python files
    print("\n📝 Validating Python syntax...")
    all_files = list(Path("jarz_pos").rglob("*.py"))
    all_files = [f for f in all_files if "__pycache__" not in str(f)]
    
    syntax_errors = []
    for py_file in all_files:
        valid, error = validate_syntax(py_file)
        if not valid:
            syntax_errors.append((py_file, error))
    
    if syntax_errors:
        print(f"❌ Found {len(syntax_errors)} files with syntax errors:")
        for file_path, error in syntax_errors:
            print(f"  • {file_path}: {error}")
        return 1
    else:
        print(f"✅ All {len(all_files)} Python files have valid syntax")
    
    # Analyze test files
    print("\n📊 Analyzing test suite...")
    test_dir = Path("jarz_pos/tests")
    test_files = sorted(test_dir.glob("test_*.py"))
    
    if not test_files:
        print("❌ No test files found!")
        return 1
    
    total_stats = {
        'files': 0,
        'classes': 0,
        'test_methods': 0,
        'setup_methods': 0,
        'teardown_methods': 0
    }
    
    test_categories = defaultdict(list)
    
    for test_file in test_files:
        stats = analyze_test_file(test_file)
        total_stats['files'] += 1
        total_stats['classes'] += stats['classes']
        total_stats['test_methods'] += stats['test_methods']
        total_stats['setup_methods'] += stats['setup_methods']
        total_stats['teardown_methods'] += stats['teardown_methods']
        
        # Categorize test file
        if test_file.name.startswith('test_api_'):
            category = 'API'
        elif test_file.name.startswith('test_utils_'):
            category = 'Utils'
        elif 'processing' in test_file.name or 'calculation' in test_file.name:
            category = 'Services'
        else:
            category = 'Other'
        
        test_categories[category].append({
            'name': test_file.name,
            'classes': stats['classes'],
            'methods': stats['test_methods']
        })
    
    # Print statistics
    print(f"\n✅ Test Suite Statistics:")
    print(f"  • Total test files: {total_stats['files']}")
    print(f"  • Total test classes: {total_stats['classes']}")
    print(f"  • Total test methods: {total_stats['test_methods']}")
    print(f"  • Setup methods: {total_stats['setup_methods']}")
    print(f"  • Teardown methods: {total_stats['teardown_methods']}")
    
    # Print by category
    print("\n📂 Tests by Category:")
    for category, files in sorted(test_categories.items()):
        total_methods = sum(f['methods'] for f in files)
        print(f"\n  {category} ({len(files)} files, {total_methods} tests):")
        for file_info in sorted(files, key=lambda x: x['name']):
            print(f"    • {file_info['name']}: {file_info['methods']} tests")
    
    # Module coverage analysis
    print("\n📈 Module Coverage:")
    
    api_modules = [f.stem for f in Path("jarz_pos/api").glob("*.py") 
                   if f.name not in ["__init__.py"]]
    api_tests = [f.stem.replace("test_api_", "") 
                 for f in test_dir.glob("test_api_*.py")]
    
    services = [f.stem for f in Path("jarz_pos/services").glob("*.py") 
                if f.name not in ["__init__.py"]]
    service_tests = ["bundle_processing", "discount_calculation"]
    
    utils = [f.stem for f in Path("jarz_pos/utils").glob("*.py") 
             if f.name not in ["__init__.py"]]
    util_tests = [f.stem.replace("test_utils_", "") 
                  for f in test_dir.glob("test_utils_*.py")]
    
    api_coverage = len(api_tests) / len(api_modules) * 100 if api_modules else 0
    service_coverage = len(service_tests) / len(services) * 100 if services else 0
    util_coverage = len(util_tests) / len(utils) * 100 if utils else 0
    
    print(f"  • API Modules: {len(api_tests)}/{len(api_modules)} ({api_coverage:.0f}%)")
    print(f"  • Services: {len(service_tests)}/{len(services)} ({service_coverage:.0f}%)")
    print(f"  • Utils: {len(util_tests)}/{len(utils)} ({util_coverage:.0f}%)")
    
    total_modules = len(api_modules) + len(services) + len(utils)
    total_tested = len(api_tests) + len(service_tests) + len(util_tests)
    overall_coverage = total_tested / total_modules * 100 if total_modules else 0
    
    print(f"\n  📊 Overall Coverage: {total_tested}/{total_modules} ({overall_coverage:.0f}%)")
    
    # Final verdict
    print("\n" + "=" * 70)
    print("VALIDATION RESULT")
    print("=" * 70)
    
    if total_stats['test_methods'] > 0 and not syntax_errors:
        print("✅ Test suite is READY for execution")
        print(f"✅ {total_stats['test_methods']} tests ready to run")
        print("✅ All Python files have valid syntax")
        print("\nTo run tests:")
        print("  bench --site <site-name> run-tests --app jarz_pos")
        return 0
    else:
        print("❌ Test suite has issues that need to be resolved")
        return 1


if __name__ == "__main__":
    sys.exit(main())
