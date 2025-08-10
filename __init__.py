# Compatibility shim: allow legacy imports expecting jarz_pos.jarz_pos
# by exposing a submodule attribute pointing to the actual package
import importlib, sys as _sys
try:
    _pkg = importlib.import_module('jarz_pos.jarz_pos')  # noqa: F401
except ModuleNotFoundError:
    # Create a lightweight module object referencing the inner package path
    import types as _types, os as _os
    from importlib.util import spec_from_loader, module_from_spec
    inner_path = _os.path.join(_os.path.dirname(__file__), 'jarz_pos')
    if _os.path.isdir(inner_path):
        spec = spec_from_loader('jarz_pos.jarz_pos', loader=None, origin=inner_path)
        m = module_from_spec(spec)
        # inject basic attributes
        m.__path__ = [inner_path]
        m.__file__ = __file__
        m.__package__ = 'jarz_pos'
        # register in sys.modules so import jarz_pos.jarz_pos works
        _sys.modules['jarz_pos.jarz_pos'] = m
        # also alias common subpackages for double-jarz imports
        try:
            from . import api as _api  # type: ignore
            _sys.modules['jarz_pos.jarz_pos.api'] = _api
        except Exception:
            pass
        try:
            from . import services as _services  # type: ignore
            _sys.modules['jarz_pos.jarz_pos.services'] = _services
        except Exception:
            pass
        try:
            from . import utils as _utils  # type: ignore
            _sys.modules['jarz_pos.jarz_pos.utils'] = _utils
        except Exception:
            pass
        try:
            from . import events as _events  # type: ignore
            _sys.modules['jarz_pos.jarz_pos.events'] = _events
        except Exception:
            pass
        try:
            from . import doctype as _doctype  # type: ignore
            _sys.modules['jarz_pos.jarz_pos.doctype'] = _doctype
        except Exception:
            pass