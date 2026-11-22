import io
import sys
import threading
import zipfile
from pathlib import Path

import pytest

from reo import server_install
from reo.engines.server import ServerEngine

FAKE = str(Path(__file__).with_name("fake_llama_server.py"))
REL = {"assets": [
    {"name": "llama-b9000-bin-win-cpu-x64.zip", "size": 10, "browser_download_url": "u1"},
    {"name": "llama-b9000-bin-win-vulkan-x64.zip", "size": 10, "browser_download_url": "u2"},
    {"name": "llama-b9000-bin-win-cuda-12.4-x64.zip", "size": 10, "browser_download_url": "u3"},
    {"name": "cudart-llama-bin-win-cuda-12.4-x64.zip", "size": 10, "browser_download_url": "u4"},
    {"name": "llama-b9000-bin-win-hip-radeon-x64.zip", "size": 10, "browser_download_url": "u5"},
    {"name": "llama-b9000-bin-macos-arm64.zip", "size": 10, "browser_download_url": "u6"},
]}


def test_pick_assets_per_kind():
    assert [a["name"] for a in server_install.pick_assets(REL, "cpu")] == ["llama-b9000-bin-win-cpu-x64.zip"]
    assert [a["name"] for a in server_install.pick_assets(REL, "vulkan")] == ["llama-b9000-bin-win-vulkan-x64.zip"]
    assert [a["name"] for a in server_install.pick_assets(REL, "cuda")] == ["llama-b9000-bin-win-cuda-12.4-x64.zip", "cudart-llama-bin-win-cuda-12.4-x64.zip"]
    assert server_install.pick_assets({"assets": []}, "cpu") == []


class FakeClient:
    def __init__(self, zips):
        self.zips = zips
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def get(self, url):
        class R:
            def raise_for_status(s): pass
            def json(s): return {"assets": [{"name": "llama-b1-bin-win-cpu-x64.zip", "size": 5, "browser_download_url": "z"}]}
        return R()
    def stream(self, method, url):
        data = self.zips
        class S:
            def __enter__(s): return s
            def __exit__(s, *a): return False
            def raise_for_status(s): pass
            def iter_bytes(s): yield data
        return S()


def _zip(names):
    b = io.BytesIO()
    with zipfile.ZipFile(b, "w") as z:
        for n in names:
            z.writestr(n, "x")
    return b.getvalue()


def test_install_unpacks_and_rejects_unsafe(home):
    events = []
    d = server_install.install(lambda **k: events.append(k), "cpu", lambda: FakeClient(_zip(["llama-server.exe", "ggml.dll"])))
    assert (d / "llama-server.exe").is_file() and server_install.exe_path() is not None
    assert events[-1]["state"] == "installing"
    with pytest.raises(server_install.ServerInstallError):
        server_install.install(lambda **k: None, "cpu", lambda: FakeClient(_zip(["../evil.exe", "llama-server.exe"])))
    with pytest.raises(server_install.ServerInstallError):
        server_install.install(lambda **k: None, "cpu", lambda: FakeClient(_zip(["readme.txt"])))


def test_server_engine_lifecycle_and_stream(tmp_path):
    model = tmp_path / "m.gguf"; model.write_bytes(b"x")
    eng = ServerEngine(command=[sys.executable, FAKE], start_timeout=20)
    assert eng.ready() is False
    info = eng.load(str(model), n_ctx=4096)
    try:
        assert eng.ready() and info["server"] is True and eng.n_ctx == 4096
        evs = list(eng.stream([{"role": "user", "text": "hi"}], "sys", {}, False, threading.Event()))
        text = "".join(e["text"] for e in evs if e["t"] == "token")
        assert text.startswith("Hello there") and any(e["t"] == "thought" and e["text"] == "hmm" for e in evs)
        assert evs[-1]["t"] == "end" and evs[-1]["stats"]["tokens"] >= 3
        proc = eng.proc
    finally:
        eng.unload()
    assert eng.ready() is False and proc.poll() is not None                 # the child is gone after unload


def test_server_engine_reports_a_crash(tmp_path):
    model = tmp_path / "m.gguf"; model.write_bytes(b"x")
    eng = ServerEngine(command=[sys.executable, "-c", "import sys; sys.exit(3)"], start_timeout=10)
    with pytest.raises(RuntimeError, match="stopped while loading"):
        eng.load(str(model))
    assert not eng.loaded


def test_bridge_uses_server_engine_when_chosen(home, monkeypatch):
    monkeypatch.delenv("REO_MOCK_ENGINE", raising=False)
    from reo.bridge import Bridge
    from reo.config import Settings
    s = Settings(); s.update({"runtime_mode": "server"})
    b = Bridge(emit=lambda e: None, settings=s, mock=False, stt=object())
    assert type(b._local).__name__ == "ServerEngine"
    st = b.server_status()
    assert st["installed"] is False and st["active"] is True and st["mode"] == "server"
    assert b._runtime()["installed"] is False
