"""Jarz POS top-level package.

Clean version after DocTypes moved under the canonical module slug folder:

	jarz_pos/jarz_pos/jarz_pos/doctype/

The temporary legacy path shim has been removed. If you still have a legacy
clone with DocTypes directly under ``jarz_pos/jarz_pos/doctype`` rebase to pick
up the structural migration.
"""

__version__ = "0.0.1"
__all__ = ["__version__"]
