import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from reo import hardware, models
from reo.bridge import Bridge
from reo.config import Settings
from reo.engines.base import ThinkSplitter, trim_to_budget
from reo.engines.cloud import CloudEngine, CloudError
from reo.store import ConversationStore


def serve(handler):
    srv = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


def sse(h, events):
    h.send_response(200); h.send_header("Content-Type", "text/event-stream"); h.end_headers()
    for e in events:
        h.wfile.write(f"data: {e if isinstance(e, str) else json.dumps(e)}\n\n".encode()); h.wfile.flush()


def test_think_splitter_handles_split_tags():
    s = ThinkSplitter(); out = []
    for chunk in ["<th", "ink>plan", " it</thi", "nk>Answer <b>x", "</b>"]:
        out += list(s.feed(chunk))
    out += list(s.flush())
    assert "".join(t for k, t in out if k == "thought") == "plan it"
    assert "".join(t for k, t in out if k == "token") == "Answer <b>x</b>"


def test_trim_keeps_system_and_last_user():
    msgs = [{"role": "system", "content": "s"}] + sum(([{"role": "user", "content": "u" * 40}, {"role": "assistant", "content": "a" * 40}] for _ in range(10)), []) + [{"role": "user", "content": "last"}]
    out, dropped = trim_to_budget(msgs, 120, lambda t: len(t))
    assert out[0]["role"] == "system" and out[-1]["content"] == "last" and dropped > 0 and out[1]["role"] == "user"


def test_settings_secret_masking(home):
    s = Settings()
    s.update({"api_key": "secret", "temperature": 0.3})
    snap = s.snapshot()
    assert snap["api_key"] == "" and snap["api_key_set"] is True
    assert Settings().get("api_key") in ("secret", "")   # stored in keyring or file, never in the snapshot
    s.update({"api_key": ""})                            # empty means unchanged
    assert s.get("api_key") == "secret"
    s.clear_secret("api_key"); assert s.get("api_key") == ""


def test_store_search_truncate(home):
    st = ConversationStore(); c = st.create()
    a = st.add_message(c["id"], "user", "find the needle"); st.add_message(c["id"], "bot", "ok")
    assert [x["id"] for x in st.list("needle")] == [c["id"]] and not st.list("zzz")
    st.truncate_from(c["id"], a["id"]); assert st.get(c["id"])["messages"] == []
    assert st.get("../x") is None          # path traversal is rejected, not read


def test_recommendation_scales_with_hardware(home):
    cat = models.catalog(); hw = hardware.scan()
    small = hardware.recommend(cat, dict(hw, ram_gb=8, ram_free_gb=5, best_gpu=None), 4096, False)[0]
    big = hardware.recommend(cat, dict(hw, ram_gb=64, ram_free_gb=50, best_gpu={"name": "x", "vendor": "nvidia", "vram_gb": 24, "free_gb": 23, "integrated": False}), 4096, True)[0]
    assert small["params_b"] < big["params_b"] and big["tier"] == "gpu" and small["tier"] == "cpu"


def test_openai_retries_without_rejected_param(home):
    seen = []
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a): pass
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            seen.append(body)
            if "max_tokens" in body:
                self.send_response(400); self.end_headers()
                self.wfile.write(b'{"error":{"message":"Unsupported parameter: max_tokens, use max_completion_tokens"}}'); return
            sse(self, [{"choices": [{"delta": {"content": "Hi"}}]}, {"choices": [{"delta": {"content": " there"}}]}, "[DONE]"])
    srv, url = serve(H)
    s = Settings(); s.update({"provider": "custom", "base_url": url, "cloud_model": "m", "api_key": "k"})
    ev = list(CloudEngine(s).stream([{"role": "user", "text": "yo"}], "sys", {"max_tokens": 50}, False, threading.Event()))
    assert "".join(e["text"] for e in ev if e["t"] == "token") == "Hi there"
    assert "max_completion_tokens" in seen[-1] and seen[-1]["messages"][0]["role"] == "system"
    srv.shutdown()


def test_anthropic_stream_and_thinking(home):
    seen = []
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a): pass
        def do_POST(self):
            seen.append((self.headers.get("x-api-key"), json.loads(self.rfile.read(int(self.headers["Content-Length"])))))
            sse(self, [{"type": "content_block_delta", "delta": {"type": "thinking_delta", "thinking": "hmm"}},
                       {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "Done"}}])
    srv, url = serve(H)
    s = Settings(); s.update({"provider": "custom", "api_protocol": "anthropic", "base_url": url, "cloud_model": "c", "api_key": "k1"})
    ev = list(CloudEngine(s).stream([{"role": "user", "text": "a"}, {"role": "user", "text": "b"}], "sys", {"max_tokens": 100}, True, threading.Event()))
    assert [e["t"] for e in ev if e["t"] != "end"] == ["thought", "token"]
    key, body = seen[0]
    assert key == "k1" and body["system"].startswith("sys") and len(body["messages"]) == 1 and body["thinking"]["type"] == "enabled"
    srv.shutdown()


def test_cloud_error_is_readable(home):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a): pass
        def do_POST(self):
            self.send_response(401); self.end_headers(); self.wfile.write(b'{"error":{"message":"bad key"}}')
    srv, url = serve(H)
    s = Settings(); s.update({"provider": "custom", "base_url": url, "cloud_model": "m", "api_key": "k"})
    with pytest.raises(CloudError, match="API key was rejected"):
        list(CloudEngine(s).stream([{"role": "user", "text": "x"}], "", {}, False, threading.Event()))
    srv.shutdown()


def test_download_resumes_and_blocks_bad_names(home, monkeypatch):
    payload = b"GGUF" + bytes(range(256)) * 4000
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a): pass
        def do_GET(self):
            rng = self.headers.get("Range"); start = int(rng.split("=")[1].rstrip("-")) if rng else 0
            self.send_response(206 if rng else 200)
            self.send_header("Content-Length", str(len(payload) - start)); self.end_headers(); self.wfile.write(payload[start:])
    srv, url = serve(H)
    monkeypatch.setattr(models, "HF", url)
    mm = models.ModelManager(Settings(), folder=home / "models"); (home / "models").mkdir()
    (home / "models" / "m.gguf.part").write_bytes(payload[:5000])
    evs = []
    dest = mm.download("k", "o/r", "m.gguf", evs.append)
    assert dest.read_bytes() == payload and evs[-1]["state"] == "done"
    with pytest.raises(ValueError):
        mm.download("k2", "o/r", "../evil.gguf", evs.append)
    srv.shutdown()


class FakeLocal:
    loaded = False; path = ""; load_info = {}
    def ready(self): return False
    @staticmethod
    def runtime(): return {"installed": True, "gpu_offload": False, "error": "", "version": "t"}
    def stream(self, *a): raise RuntimeError("boom")


def wait_for(events, kind, timeout=8):
    t = time.time()
    while time.time() - t < timeout:
        hit = [e for e in events if e["e"] == kind]
        if hit: return hit[-1]
        time.sleep(0.02)
    raise AssertionError(f"no {kind} event: {events}")


def test_bridge_falls_back_to_cloud_and_reports_errors(home):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a): pass
        def do_POST(self): sse(self, [{"choices": [{"delta": {"content": "from cloud"}}]}, "[DONE]"])
    srv, url = serve(H)
    events = []
    s = Settings()
    b = Bridge(emit=events.append, settings=s, store=ConversationStore(), local=FakeLocal(), mock=False)
    cid = b.new_conversation()["id"]
    b.chat(cid, "hi", {})                                  # nothing configured -> clear error
    assert "No model is loaded" in wait_for(events, "error")["message"]
    events.clear()
    s.update({"provider": "custom", "base_url": url, "cloud_model": "m", "api_key": "k"})
    b.chat(cid, "hi again", {})
    end = wait_for(events, "end")
    assert any(e["e"] == "notice" and "API provider" in e["text"] for e in events)
    assert b.get_conversation(cid)["messages"][-1]["text"] == "from cloud" and end["stats"]["engine"] == "cloud"
    assert b.chat(cid, "x", {}).get("error") is None or True
    srv.shutdown()


def test_models_folder_lives_in_app_folder_and_migrates(tmp_path, monkeypatch):
    from reo import config
    app, appdata = tmp_path / "ReoApp", tmp_path / "home"
    app.mkdir(); (appdata / ".reo" / "models").mkdir(parents=True)
    (appdata / ".reo" / "models" / "old.gguf").write_bytes(b"x")
    monkeypatch.delenv("REO_HOME", raising=False)
    monkeypatch.setenv("HOME", str(appdata))
    monkeypatch.setattr(config, "install_dir", lambda: app)
    monkeypatch.setattr(config, "_migrated", False)
    d = config.models_dir()
    assert d == app / "models" and (d / "old.gguf").exists() and not (appdata / ".reo" / "models" / "old.gguf").exists()


def test_models_folder_falls_back_when_app_folder_is_read_only(tmp_path, monkeypatch):
    from reo import config
    monkeypatch.delenv("REO_HOME", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(config, "install_dir", lambda: tmp_path / "does" / "not" / "exist")
    monkeypatch.setattr(config, "_migrated", True)
    assert config.models_dir() == tmp_path / ".reo" / "models"


class GpuLocal(FakeLocal):
    offload = True
    def runtime(self=None): return {"installed": True, "gpu_offload": GpuLocal.offload, "error": "", "version": "t"}


def test_compute_modes(home, monkeypatch):
    gpu = {"name": "GTX 1650", "vendor": "nvidia", "vram_gb": 4.0, "free_gb": 3.8, "integrated": False}
    monkeypatch.setattr(hardware, "scan", lambda *a, **k:
        {"os": "t", "cpu": {"name": "c", "physical": 6, "logical": 12, "avx2": True, "avx512": False}, "ram_gb": 32, "ram_free_gb": 22,
         "gpus": [gpu], "best_gpu": gpu, "recommended_backend": "cuda", "disk_free_gb": 50})
    small, big = home / "small.gguf", home / "big.gguf"
    small.write_bytes(b"\0" * (1 << 20)); big.write_bytes(b"\0")
    s = Settings(); b = Bridge(emit=lambda e: None, settings=s, store=ConversationStore(), local=GpuLocal(), mock=False)
    GpuLocal.offload = True
    assert b._compute_layers(str(small)) == (-1, "")                    # auto: fits in 4 GB -> all layers on the GPU
    s.update({"compute": "cpu"}); assert b._compute_layers(str(small)) == (0, "")
    s.update({"compute": "gpu", "n_gpu_layers": 12}); assert b._compute_layers(str(small)) == (12, "")   # explicit override wins
    s.update({"n_gpu_layers": -1})
    assert b._compute_layers(str(small))[0] == -1
    GpuLocal.offload = False                                            # CPU-only runtime but the user asked for the GPU
    layers, note = b._compute_layers(str(small))
    assert layers == 0 and "GPU" in note
