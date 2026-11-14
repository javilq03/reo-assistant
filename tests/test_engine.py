"""The real llama.cpp engine against a tiny generated GGUF (output is gibberish; we test the plumbing)."""
import threading

import pytest

llama_cpp = pytest.importorskip("llama_cpp")
from reo.engines.local import LocalEngine  # noqa: E402
from tinygguf import make  # noqa: E402


@pytest.fixture(scope="module")
def tiny(tmp_path_factory):
    return make(tmp_path_factory.mktemp("m") / "tiny.gguf")


def collect(eng, history, **kw):
    params = {"max_tokens": 24, "temperature": 0.8}
    ev = list(eng.stream(history, "You are a test.", params, kw.get("deep", False), kw.get("stop", threading.Event())))
    return ev


def test_load_stream_and_stats(tiny):
    eng = LocalEngine()
    info = eng.load(str(tiny), n_ctx=256, n_gpu_layers=0, flash_attn=False)
    assert eng.ready() and info["name"] == "tiny"
    ev = collect(eng, [{"role": "user", "text": "hello world"}])
    assert ev[-1]["t"] == "end" and ev[-1]["stats"]["tokens"] > 0
    assert any(e["t"] == "token" for e in ev)
    eng.unload()
    assert not eng.ready()


def test_stop_flag_halts_generation(tiny):
    eng = LocalEngine()
    eng.load(str(tiny), n_ctx=256, n_gpu_layers=0, flash_attn=False)
    stop = threading.Event()
    n = 0
    for e in eng.stream([{"role": "user", "text": "hi"}], "", {"max_tokens": 120, "temperature": 1.0}, False, stop):
        if e["t"] == "token":
            n += 1
            if n == 3:
                stop.set()
        if e["t"] == "end":
            assert e["stats"]["stopped"] is True
    assert n <= 5


def test_context_trimming_reports_notice(tiny):
    eng = LocalEngine()
    eng.load(str(tiny), n_ctx=256, n_gpu_layers=0, flash_attn=False)
    hist = []
    for i in range(30):
        hist += [{"role": "user", "text": f"question number {i} " * 4}, {"role": "bot", "text": "answer " * 6}]
    hist.append({"role": "user", "text": "last one"})
    ev = collect(eng, hist)
    notes = [e for e in ev if e["t"] == "notice"]
    assert notes and "oldest" in notes[0]["text"]
    assert ev[-1]["t"] == "end"          # and it still answers instead of overflowing the context


def test_runtime_probe():
    rt = LocalEngine.runtime()
    assert rt["installed"] and isinstance(rt["gpu_offload"], bool)
