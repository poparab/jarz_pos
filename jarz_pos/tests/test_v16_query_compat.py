"""Static guards against ERPNext v16 query-layer incompatibilities.

These tests read the app's source rather than executing it, on purpose.

The bug that motivated them shipped to production and survived a green test
suite: `kanban._get_invoice_note_counts` passed
``fields=["sales_invoice", "count(name) as note_count"]`` to `frappe.get_all`.
ERPNext v16 rejects SQL functions written as strings in SELECT:

    ValidationError: SQL functions are not allowed as strings in SELECT:
    count(name) as note_count. Use dict syntax like {'COUNT': '*'} instead.

The helper's bare ``except Exception: return {}`` swallowed that, so every
Kanban card silently reported zero notes. The unit test covering the feature
mocked `frappe.get_all` and therefore proved nothing about real v16 behaviour.

A mock-based test can never catch this class of bug, and the local bench still
runs Frappe v15 (which accepts the string form), so an execution-based test
would not catch it either. A static check catches it everywhere, cheaply.
"""

import ast
import os
import re
import unittest

#: ORM entry points whose `fields` argument is passed to the v16 query engine.
QUERY_METHODS = {"get_all", "get_list", "get_value", "get_values"}

#: Position of the `fields` argument when passed positionally.
FIELDS_POSITIONAL_INDEX = {"get_all": 2, "get_list": 2, "get_value": 2, "get_values": 2}

#: SQL functions commonly written as strings in `fields` — all rejected by v16.
SQL_FUNCTION_RE = re.compile(
	r"\b(count|sum|avg|min|max|ifnull|coalesce|group_concat|concat|concat_ws|"
	r"distinct|cast|round|truncate|length|locate|now|timestamp)\s*\(",
	re.IGNORECASE,
)


def _app_package_root():
	"""Absolute path of the `jarz_pos` package (never a .claude worktree copy)."""
	return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _iter_python_files(root):
	for dirpath, dirnames, filenames in os.walk(root):
		# Skip caches and any nested checkouts/worktrees of the app.
		dirnames[:] = [d for d in dirnames if d not in {"__pycache__", ".git", "node_modules"}]
		for filename in filenames:
			if filename.endswith(".py"):
				yield os.path.join(dirpath, filename)


def _called_method_name(node):
	"""Return the attribute name for `x.y(...)` calls, else None."""
	if isinstance(node.func, ast.Attribute):
		return node.func.attr
	return None


def _fields_argument(node, method_name):
	"""Resolve the `fields` argument of an ORM call, keyword or positional."""
	for keyword in node.keywords:
		if keyword.arg == "fields":
			return keyword.value
	index = FIELDS_POSITIONAL_INDEX.get(method_name)
	if index is not None and len(node.args) > index:
		return node.args[index]
	return None


def _string_constants(node):
	"""Yield every string literal directly inside a list/tuple/str node."""
	if isinstance(node, ast.Constant) and isinstance(node.value, str):
		yield node
	elif isinstance(node, (ast.List, ast.Tuple)):
		for element in node.elts:
			if isinstance(element, ast.Constant) and isinstance(element.value, str):
				yield element


class TestV16QueryCompatibility(unittest.TestCase):
	"""Guards that the app never reintroduces v16-illegal query syntax."""

	def test_no_sql_function_strings_in_orm_field_lists(self):
		"""No `fields=[...]` entry may contain a SQL function written as a string.

		ERPNext v16 raises ValidationError on these. Use the query builder
		(`frappe.qb` + `frappe.query_builder.functions.Count`) for real
		aggregates, or select plain columns and reduce in Python.
		"""

		offenders = []
		root = _app_package_root()

		for path in _iter_python_files(root):
			with open(path, "r", encoding="utf-8") as handle:
				source = handle.read()
			try:
				tree = ast.parse(source, filename=path)
			except SyntaxError:  # pragma: no cover - keeps the guard non-fatal
				continue

			for node in ast.walk(tree):
				if not isinstance(node, ast.Call):
					continue
				method_name = _called_method_name(node)
				if method_name not in QUERY_METHODS:
					continue
				fields_arg = _fields_argument(node, method_name)
				if fields_arg is None:
					continue
				for constant in _string_constants(fields_arg):
					if SQL_FUNCTION_RE.search(constant.value):
						relative = os.path.relpath(path, root)
						offenders.append(
							f"{relative}:{constant.lineno} -> {method_name}(fields=[..., "
							f"{constant.value!r} ...])"
						)

		self.assertEqual(
			offenders,
			[],
			"SQL functions are not allowed as strings in SELECT on ERPNext v16. "
			"Offending ORM calls:\n  " + "\n  ".join(offenders),
		)

	def test_guard_detects_the_original_kanban_regression(self):
		"""The guard above must actually flag the exact shape of the shipped bug.

		Without this, a broken matcher would make the guard silently vacuous —
		the same failure mode as the mocked test that missed the real bug.
		"""

		tree = ast.parse(
			'frappe.get_all("Jarz Invoice Note", filters={}, '
			'fields=["sales_invoice", "count(name) as note_count"], group_by="sales_invoice")'
		)
		call = tree.body[0].value
		fields_arg = _fields_argument(call, _called_method_name(call))

		flagged = [c.value for c in _string_constants(fields_arg) if SQL_FUNCTION_RE.search(c.value)]

		self.assertEqual(flagged, ["count(name) as note_count"])

	def test_guard_allows_plain_column_field_lists(self):
		"""Ordinary column selections must not trip the guard (no false positives)."""

		tree = ast.parse(
			'frappe.get_all("Jarz Invoice Note", '
			'fields=["sales_invoice", "note", "added_on", "creation"])'
		)
		call = tree.body[0].value
		fields_arg = _fields_argument(call, _called_method_name(call))

		flagged = [c.value for c in _string_constants(fields_arg) if SQL_FUNCTION_RE.search(c.value)]

		self.assertEqual(flagged, [])
