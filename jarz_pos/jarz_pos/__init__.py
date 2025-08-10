# Backward compatibility shim so that import jarz_pos.jarz_pos works
from importlib import import_module as _im
# Re-export selected namespaces
try:
    api = _im('jarz_pos.api')  # noqa: F401
except Exception:
    pass
try:
    services = _im('jarz_pos.services')  # noqa: F401
except Exception:
    pass
try:
    utils = _im('jarz_pos.utils')  # noqa: F401
except Exception:
    pass
