import os
import sys
import tempfile
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


@pytest.fixture()
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("REO_HOME", str(tmp_path))
    return tmp_path


@pytest.fixture()
def ui_server(home, monkeypatch):
    monkeypatch.setenv("REO_MOCK_ENGINE", "1")
    from reo.devserver import make_server
    srv, bridge = make_server(port=0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}", bridge
    srv.shutdown()
