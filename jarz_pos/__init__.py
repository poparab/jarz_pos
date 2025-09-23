"""App package for Jarz POS.

Keep import-time side effects to a minimum. Do not import frappe or perform DB operations here.
"""

__version__ = '0.0.1'

# Minimal compatibility: expose a marker so imports like jarz_pos.jarz_pos work without heavy aliasing.
import sys as _sys
_sys.modules.setdefault('jarz_pos.jarz_pos', _sys.modules[__name__])

__all__ = ['__version__']
