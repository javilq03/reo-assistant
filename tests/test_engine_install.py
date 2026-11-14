"""The speech engine without pip (fake PyPI), the PowerShell voice fallback, the one-click voice+engine flow."""
import hashlib
import io
import json
import sys
import threading
import time
import zipfile

import httpx
import pytest

from reo import engine_install as EI
from reo import tts


# ---------------------------------------------------------------- choosing wheels
def test_wheel_selection_matches_what_pip_would_pick():
    win = {"win_amd64"}
    ok = lambda f, py=(3, 12): EI.wheel_ok(f, py, win)                                    # noqa: E731
    assert ok("onnxruntime-1.20.0-cp312-cp312-win_amd64.whl")
    assert not ok("onnxruntime-1.20.0-cp311-cp311-win_amd64.whl")                         # another Python
    assert not ok("onnxruntime-1.20.0-cp312-cp312-win32.whl")                             # another platform
    assert ok("piper_tts-1.8.0-cp39-abi3-win_amd64.whl")                                  # abi3 works on newer Pythons
    assert not ok("piper_tts-1.8.0-cp39-abi3-win_amd64.whl", (3, 8))
    assert ok("pathvalidate-3.3.1-py3-none-any.whl") and ok("x-1-py2.py3-none-any.whl")
    assert not ok("pathvalidate-3.3.1.tar.gz")
    assert EI.platform_tags("win-amd64") == win and "manylinux_2_28_x86_64" in EI.platform_tags("linux-x86_64")


def test_python_requirement_and_newest_usable_release():
    assert EI.python_ok(">=3.11", (3, 12)) and not EI.python_ok(">=3.11", (3, 10)) and not EI.python_ok(">=3.9,<3.13", (3, 13))
    meta = {"releases": {
        "1.32.0": [{"filename": "onnxruntime-1.32.0-cp311-cp311-win_amd64.whl", "packagetype": "bdist_wheel", "requires_python": ">=3.11",
                    "url": "u1", "digests": {"sha256": "a"}, "size": 5}],
        "1.19.0": [{"filename": "onnxruntime-1.19.0-cp310-cp310-win_amd64.whl", "packagetype": "bdist_wheel", "requires_python": ">=3.10",
                    "url": "u2", "digests": {"sha256": "b"}, "size": 4}],
        "1.33.0rc1": [{"filename": "onnxruntime-1.33.0rc1-cp310-cp310-win_amd64.whl", "packagetype": "bdist_wheel", "url": "u3"}]}}
    assert EI.pick_wheel(meta, (3, 11), {"win_amd64"})["version"] == "1.32.0"
    assert EI.pick_wheel(meta, (3, 10), {"win_amd64"})["version"] == "1.19.0"            # older Python gets the older build; pre-releases skipped
    assert EI.pick_wheel(meta, (3, 9), {"win_amd64"}) is None


# ---------------------------------------------------------------- installing from a fake PyPI
def _wheel(files: dict) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, text in files.items():
            z.writestr(name, text)
    return buf.getvalue()


def fake_pypi(tamper=False, missing=None, status=200):
    blobs = {
        "piper_tts-9.0.0-py3-none-any.whl": _wheel({"piper/__init__.py": "class PiperVoice: pass\n"}),
        "onnxruntime-9.0.0-py3-none-any.whl": _wheel({"onnxruntime/__init__.py": ""}),
        "pathvalidate-9.0.0-py3-none-any.whl": _wheel({"pathvalidate/__init__.py": ""}),
    }

    def handler(req: httpx.Request):
        url = str(req.url)
        if status != 200:
            return httpx.Response(status)
        if url.startswith("https://pypi.org/pypi/"):
            pkg = url.split("/")[-2]
            if pkg == missing:
                return httpx.Response(200, json={"releases": {}})
            fn = next(f for f in blobs if f.split("-")[0] == pkg.replace("-", "_"))
            digest = "0" * 64 if (tamper and pkg == "onnxruntime") else hashlib.sha256(blobs[fn]).hexdigest()
            return httpx.Response(200, json={"releases": {"9.0.0": [{"filename": fn, "packagetype": "bdist_wheel", "requires_python": ">=3.9",
                                  "url": "https://files.test/" + fn, "digests": {"sha256": digest}, "size": len(blobs[fn])}]}})
        data = blobs[url.rsplit("/", 1)[1]]
        return httpx.Response(200, headers={"content-length": str(len(data))}, content=data)
    return lambda: httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True)


def test_engine_installs_from_pypi_with_progress(home):
    ev = []
    path = EI.install(lambda **e: ev.append(e), fake_pypi())
    assert path == EI.engine_dir() and EI.has_portable()
    assert (path / "piper" / "__init__.py").is_file() and (path / "onnxruntime").is_dir() and (path / "pathvalidate").is_dir()
    assert not (path.parent / "_work").exists()                                           # temp files cleaned up
    assert any(e["state"] == "downloading" and e["total"] > 0 for e in ev) or ev[-1]["state"] == "installing"
    assert ev[-1]["message"].startswith("Unpacking")


@pytest.mark.parametrize("kw,code", [({"tamper": True}, "corrupt"), ({"missing": "onnxruntime"}, "no_wheel"), ({"status": 503}, "http")])
def test_engine_install_failures_are_explained_and_leave_nothing_behind(home, kw, code):
    with pytest.raises(EI.EngineError) as e:
        EI.install(lambda **ev: None, fake_pypi(**kw))
    assert e.value.code == code and e.value.message
    assert not EI.has_portable() and not (EI.engine_dir().parent / "_work").exists()


def test_network_error_message(home):
    def boom():
        def handler(req):
            raise httpx.ConnectError("down")
        return httpx.Client(transport=httpx.MockTransport(handler))
    with pytest.raises(EI.EngineError) as e:
        EI.install(lambda **ev: None, boom)
    assert e.value.code == "network" and "pypi.org" in e.value.message


def test_install_engine_falls_back_to_download_when_pip_cannot(home, monkeypatch):
    state = {"ok": False}
    monkeypatch.setattr(tts, "piper_status", lambda refresh=False: (state["ok"], "" if state["ok"] else "ModuleNotFoundError: piper"))
    monkeypatch.setattr("subprocess.run", lambda *a, **k: type("R", (), {"returncode": 1, "stdout": "", "stderr": "externally-managed-environment"})())
    real = EI.install

    def fake_install(emit, cf=None, **kw):
        real(emit, cf, **kw)
        state["ok"] = True
    monkeypatch.setattr(EI, "install", fake_install)
    ev = []
    r = tts.install_engine(lambda **e: ev.append(e), fake_pypi())
    assert r == {"ok": True} and ev[-1]["state"] == "done" and EI.has_portable()


def test_install_engine_reports_a_broken_engine(home, monkeypatch):
    monkeypatch.setattr(tts, "piper_status", lambda refresh=False: (False, "ImportError: DLL load failed while importing onnxruntime"))
    ev = []
    r = tts.install_engine(lambda **e: ev.append(e), fake_pypi(), use_pip=False)
    assert r["ok"] is False and r["code"] == "broken" and "DLL load failed" in r["error"] and ev[-1]["state"] == "error"


def test_downloaded_engine_is_activated_ahead_of_a_broken_copy(home, monkeypatch):
    EI.install(lambda **e: None, fake_pypi())
    seen = iter(["ImportError: DLL load failed", ""])
    monkeypatch.setattr(tts, "_try_import_piper", lambda: next(seen))
    monkeypatch.setitem(sys.modules, "piper", type(sys)("piper"))
    try:
        assert tts.piper_status(refresh=True) == (True, "")
        assert sys.path[0] == str(EI.engine_dir()) and "piper" not in sys.modules
    finally:
        sys.path[:] = [p for p in sys.path if p != str(EI.engine_dir())]


# ---------------------------------------------------------------- Windows voice without comtypes
class FakeProc:
    def __init__(self, polls=2, rc=0, err=b""):
        self.stdin, self.polls, self.returncode, self.stderr, self.written = io.BytesIO(), polls, rc, io.BytesIO(err), None
        self.killed = False

    def poll(self):
        if self.killed:
            return -9
        self.polls -= 1
        return None if self.polls >= 0 else self.returncode

    def kill(self):
        self.killed = True


def test_powershell_voice_speaks_utf8_text_and_can_be_cancelled(monkeypatch):
    procs = []
    monkeypatch.setattr(tts, "ps_available", lambda: True)

    def popen(cmd, **kw):
        p = FakeProc(polls=1000)
        p.stdin = type("S", (), {"write": lambda self, b: procs.append(("text", b)), "close": lambda self: None})()
        procs.append(("cmd", cmd, kw))
        procs.append(("proc", p))
        return p
    monkeypatch.setattr(tts.subprocess, "Popen", popen)
    cancel = threading.Event()
    b = tts._PsBackend()
    threading.Timer(0.15, cancel.set).start()
    b.speak("Hola, ¿qué tal? Ñandú", cancel, "sapi:Microsoft Helena")
    assert ("text", "Hola, ¿qué tal? Ñandú".encode("utf-8")) in procs
    cmd = next(p for p in procs if p[0] == "cmd")
    assert "System.Speech" in cmd[1][-1] and cmd[2]["env"]["REO_VOICE"] == "Microsoft Helena"
    assert next(p for p in procs if p[0] == "proc")[1].killed


def test_speaker_uses_powershell_when_comtypes_is_missing(monkeypatch):
    ev, spoken = [], []
    sp = tts.Speaker(lambda **e: ev.append(e), None)
    monkeypatch.setattr(sp._sapi, "available", lambda: False)
    monkeypatch.setattr(tts, "ps_available", lambda: True)
    monkeypatch.setattr(sp._ps, "speak", lambda text, cancel, name=None: spoken.append(text))
    assert sp.available() and sp.readiness("system") == {"ok": True}
    sp.speak("hello there")
    end = time.time() + 3
    while time.time() < end and not any(e["kind"] == "done" for e in ev):
        time.sleep(0.02)
    assert spoken == ["hello there"] and not any(e["kind"] == "error" for e in ev)


def test_missing_engine_falls_back_speaks_and_repairs_in_background(home, monkeypatch):
    ev, spoken, healed = [], [], []
    sp = tts.Speaker(lambda **e: ev.append(e), None, on_engine_missing=lambda: healed.append(1))
    monkeypatch.setattr(tts, "piper_status", lambda refresh=False: (False, "ModuleNotFoundError: No module named 'piper'"))
    monkeypatch.setattr(sp._sapi, "available", lambda: False)
    monkeypatch.setattr(tts, "ps_available", lambda: True)
    monkeypatch.setattr(sp._ps, "speak", lambda text, cancel, name=None: spoken.append(text))
    r = sp.readiness("en_US-amy-low")
    assert r["code"] == "engine_missing" and "piper-tts" not in r["message"] and "not set up yet" in r["message"]
    sp.speak("hi", voice_id="en_US-amy-low")
    end = time.time() + 3
    while time.time() < end and not any(e["kind"] == "done" for e in ev):
        time.sleep(0.02)
    assert spoken == ["hi"] and healed == [1]
    assert any(e["kind"] == "notice" and e["code"] == "engine_missing" and "Windows voice" in e["message"] for e in ev)


def test_engine_error_reason_is_shown_when_it_is_not_just_missing(monkeypatch):
    monkeypatch.setattr(tts, "piper_status", lambda refresh=False: (False, "ImportError: DLL load failed while importing onnxruntime"))
    msg = tts.Speaker(lambda **e: None).readiness("en_US-amy-low")["message"]
    assert "DLL load failed" in msg


# ---------------------------------------------------------------- one click: engine first, then the voice
def test_voice_download_sets_up_the_engine_first_on_the_same_bar(home, monkeypatch):
    from tests.test_voice import KEY, run_download, server, states
    state = {"ok": False}
    monkeypatch.setattr(tts, "piper_status", lambda refresh=False: (state["ok"], ""))

    def fake_install(emit, *a, **k):
        emit(kind="engine", id="piper", state="downloading", message="Downloading the speech engine…", done=10, total=100, speed=5)
        emit(kind="engine", id="piper", state="done", message="Speech engine installed.")
        state["ok"] = True
        return {"ok": True}
    monkeypatch.setattr(tts, "install_engine", fake_install)

    from reo.bridge import Bridge
    monkeypatch.setenv("REO_MOCK_ENGINE", "1")
    events = []
    b = Bridge(emit=events.append)
    d = tts.VoiceDownloader(lambda **ev: b._emit(e="voicedl", **ev), client_factory=server(), loader=lambda p: None, prepare=b._prepare_voice)
    assert d.start(KEY)["started"]
    end = time.time() + 10
    while time.time() < end and d.active():
        time.sleep(0.02)
    ev = [e for e in events if e.get("e") == "voicedl"]
    eng = [e for e in ev if e.get("phase") == "engine"]
    assert eng and eng[0]["done"] == 10 and eng[0]["total"] == 100 and eng[0]["id"] == KEY and "speech engine" in eng[0]["message"]
    assert ev[-1]["state"] == "done" and tts.voice_installed(KEY)
    assert ev.index(eng[0]) < next(i for i, e in enumerate(ev) if e["state"] == "downloading" and e.get("phase") != "engine")


def test_voice_download_stops_with_the_engine_error_when_it_cannot_be_set_up(home, monkeypatch):
    from tests.test_voice import KEY, server
    monkeypatch.setattr(tts, "piper_status", lambda refresh=False: (False, "x"))
    monkeypatch.setattr(tts, "install_engine", lambda emit, *a, **k: {"ok": False, "code": "network", "error": "Could not reach pypi.org."})
    from reo.bridge import Bridge
    monkeypatch.setenv("REO_MOCK_ENGINE", "1")
    events = []
    b = Bridge(emit=events.append)
    d = tts.VoiceDownloader(lambda **ev: b._emit(e="voicedl", **ev), client_factory=server(), loader=lambda p: None, prepare=b._prepare_voice)
    d.start(KEY)
    end = time.time() + 10
    while time.time() < end and d.active():
        time.sleep(0.02)
    last = [e for e in events if e.get("e") == "voicedl"][-1]
    assert last["state"] == "error" and last["code"] == "network" and "pypi.org" in last["message"] and not tts.voice_installed(KEY)
