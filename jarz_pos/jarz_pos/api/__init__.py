"""Compatibility shim for legacy imports.

This package re-exports the canonical API modules from ``jarz_pos.api`` so
existing integrations that import via ``jarz_pos.jarz_pos.api.<module>`` keep
working across environments.

Preferred import path: jarz_pos.api.<module>
"""

# No runtime side effects; modules are thin re-export files.
