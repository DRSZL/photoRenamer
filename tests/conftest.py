"""
Test bootstrap.

photo_organizer imports `webview` at module load, but the pure logic under
test never needs a real GUI. Stub the module so the suite runs in a headless
CI / dev environment without pywebview installed.
"""
import os
import sys
import types

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

if 'webview' not in sys.modules:
    _stub = types.ModuleType('webview')
    _stub.FOLDER_DIALOG = 0
    _stub.create_window = lambda *a, **k: None
    _stub.start = lambda *a, **k: None
    sys.modules['webview'] = _stub
