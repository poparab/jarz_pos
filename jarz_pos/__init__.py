"""App package for Jarz POS.

Keep import-time side effects to a minimum. Do not import frappe or perform DB operations here.
"""

__version__ = '0.0.1'

# Keep module clean: no aliasing or sub-imports at import time
__all__ = ['__version__']
