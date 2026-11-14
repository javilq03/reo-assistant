"""Voice downloads (offline, with a fake server), fallback to the Windows voice, Piper playback, and the download UI."""
import hashlib
import json
import sys
import threading
import time
import types

import httpx
import pytest

from reo import tts

KEY = "en_US-amy-low"
BASE = "https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_US/amy/low/"
ONNX = (b"onnx-model-bytes-" * 20000)[:300_000]
CFG = json.dumps({"audio": {"sample_rate": 16000}}).encode()


def catalog(md5=None, size=None):
    return {KEY: {"files": {
        f"en/en_US/amy/low/{KEY}.onnx": {"size_bytes": size or len(ONNX), "md5_digest": md5 or hashlib.md5(ONNX).hexdigest()},
        f"en/en_US/amy/low/{KEY}.onnx.json": {"size_bytes": len(CFG), "md5_digest": hashlib.md5(CFG).hexdigest()},
        f"en/en_US/amy/low/MODEL_CARD": {"size_bytes": 5, "md5_digest": "x"}}}}


class Slow(httpx.SyncByteStream):
    def __init__(self, data, delay=0.0):
        self.data, self.delay = data, delay

    def __iter__(self):
        for i in range(0, len(self.data), 50_000):
            time.sleep(self.delay)
            yield self.data[i:i + 50_000]


def server(cat=None, status=200, delay=0.0, boom=None):
    def handler(req: httpx.Request):
        if boom:
            raise boom
        url = str(req.url)
        if url.endswith("voices.json"):
            return httpx.Response(200, json=cat if cat is not None else catalog())
        if status != 200:
            return httpx.Response(status)
        data = ONNX if url.endswith(".onnx") else CFG
        return httpx.Response(200, headers={"content-length": str(len(data))}, stream=Slow(data, delay))
    return lambda: httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True)


def run_download(factory, loader=lambda p: None, key=KEY, cancel_after=None):
    events = []
    d = tts.VoiceDownloader(lambda **ev: events.append(ev), client_factory=factory, loader=loader)
    r = d.start(key)
    if cancel_after is not None:
        end = time.time() + 5
        while time.time() < end and not any(e["state"] == "downloading" and e["done"] >= cancel_after for e in events):
            time.sleep(0.01)
        d.cancel(key)
    end = time.time() + 15
    while time.time() < end and d.active():
        time.sleep(0.02)
    return r, events, d


def states(events):
    out = []
    for e in events:
        if not out or out[-1] != e["state"]:
            out.append(e["state"])
    return out


def test_download_reports_progress_verifies_and_installs(home):
    r, ev, _ = run_download(server(delay=0.01))
    assert r == {"ok": True, "started": True}
    assert states(ev) == ["queued", "starting", "downloading", "verifying", "done"]
    dl = [e for e in ev if e["state"] == "downloading"]
    assert dl[-1]["total"] == len(ONNX) + len(CFG) and dl[-1]["done"] > 0 and all(e["total"] == dl[0]["total"] for e in dl)
    assert tts.voice_installed(KEY) and not list((home / "voicebanks" / KEY).glob("*.part"))
    v = next(x for x in tts.list_available_voices() if x["id"] == KEY)
    assert v["installed"] is True and v["name"] == "Amy (US, low)"
    assert ev[-1]["state"] == "done" and "ready" in ev[-1]["message"]


def test_download_without_catalog_uses_conventional_urls(home):
    def handler(req):
        url = str(req.url)
        if url.endswith("voices.json"):
            return httpx.Response(503)
        assert url.startswith(BASE), url
        data = ONNX if url.endswith(".onnx") else CFG
        return httpx.Response(200, headers={"content-length": str(len(data))}, content=data)
    _, ev, _ = run_download(lambda: httpx.Client(transport=httpx.MockTransport(handler)))
    assert states(ev)[-1] == "done" and tts.voice_installed(KEY)
    assert [e for e in ev if e["state"] == "downloading"][-1]["total"] == len(ONNX) + len(CFG)


def test_bad_checksum_is_rejected_and_leaves_nothing(home):
    _, ev, _ = run_download(server(cat=catalog(md5="0" * 32)))
    assert ev[-1]["state"] == "error" and ev[-1]["code"] == "corrupt" and "checksum" in ev[-1]["message"]
    assert not tts.voice_installed(KEY) and not (home / "voicebanks" / KEY).exists()


def test_voice_that_piper_cannot_load_is_removed(home):
    def bad(path):
        raise RuntimeError("bad onnx")
    _, ev, _ = run_download(server(), loader=bad)
    assert ev[-1]["state"] == "error" and ev[-1]["code"] == "corrupt" and "cannot load" in ev[-1]["message"]
    assert not tts.voice_installed(KEY)


@pytest.mark.parametrize("kw,code", [({"status": 404}, "not_found"), ({"status": 500}, "http"),
                                     ({"boom": httpx.ConnectError("down")}, "network")])
def test_download_errors_are_explained(home, kw, code):
    _, ev, _ = run_download(server(**kw))
    assert ev[-1]["state"] == "error" and ev[-1]["code"] == code and ev[-1]["message"]
    assert not tts.voice_installed(KEY)


def test_cancel_stops_and_cleans_up(home):
    _, ev, _ = run_download(server(delay=0.05), cancel_after=1)
    assert ev[-1]["state"] == "cancelled" and not (home / "voicebanks" / KEY).exists()


def test_bad_names_and_repeat_requests(home):
    d = tts.VoiceDownloader(lambda **e: None, client_factory=server())
    for bad in ("../../evil", "en_US-x", "", "a b-c-d"):
        assert d.start(bad)["ok"] is False
    assert d.start(KEY)["started"] is True
    assert d.start(KEY).get("already") is True or d.start(KEY).get("installed") is True


# ---------------------------------------------------------------- speaking: fallback and Piper playback
def make_speaker(monkeypatch, piper_ok, sapi_ok, installed=False):
    events, spoken = [], []
    sp = tts.Speaker(lambda **ev: events.append(ev))
    monkeypatch.setattr(tts, "piper_status", lambda refresh=False: (piper_ok, "" if piper_ok else "ModuleNotFoundError: piper"))
    monkeypatch.setattr(tts, "voice_installed", lambda k: installed)
    monkeypatch.setattr(sp._sapi, "available", lambda: sapi_ok)
    monkeypatch.setattr(sp._sapi, "speak", lambda text, cancel, vid=None, vol=100: spoken.append(text))
    return sp, events, spoken


def wait_done(events):
    end = time.time() + 5
    while time.time() < end and not any(e["kind"] == "done" for e in events):
        time.sleep(0.02)


def test_missing_engine_falls_back_to_windows_voice_and_says_why_once(monkeypatch):
    sp, ev, spoken = make_speaker(monkeypatch, piper_ok=False, sapi_ok=True)
    sp.speak("hello", voice_id=KEY); wait_done(ev)
    ev.clear(); sp.speak("again", voice_id=KEY); wait_done(ev)
    assert spoken == ["hello", "again"]
    notices = [e for e in ev if e["kind"] == "notice"]
    assert notices == []                                          # the first one was already shown
    assert not any(e["kind"] == "error" for e in ev)


def test_notice_codes(monkeypatch):
    sp, ev, _ = make_speaker(monkeypatch, piper_ok=False, sapi_ok=True)
    sp.speak("hi", voice_id=KEY); wait_done(ev)
    n = next(e for e in ev if e["kind"] == "notice")
    assert n["code"] == "engine_missing" and "Windows voice" in n["message"] and "piper-tts is not installed" not in n["message"]
    sp2, ev2, _ = make_speaker(monkeypatch, piper_ok=True, sapi_ok=True, installed=False)
    sp2.speak("hi", voice_id=KEY); wait_done(ev2)
    assert next(e for e in ev2 if e["kind"] == "notice")["code"] == "voice_missing"


def test_preview_is_strict_and_does_not_play_another_voice(monkeypatch):
    sp, ev, spoken = make_speaker(monkeypatch, piper_ok=True, sapi_ok=True, installed=False)
    r = sp.speak("hello", voice_id=KEY, strict=True)
    assert r["ok"] is False and r["code"] == "voice_missing" and spoken == []


def test_nothing_available_is_an_error_with_a_code(monkeypatch):
    sp, ev, _ = make_speaker(monkeypatch, piper_ok=False, sapi_ok=False)
    sp.speak("hi", voice_id=KEY); wait_done(ev)
    e = next(x for x in ev if x["kind"] == "error")
    assert e["code"] == "engine_missing" and "speech engine" in e["message"] and "piper-tts" not in e["message"]


def test_piper_voice_plays_through_sounddevice(home, monkeypatch):
    import numpy as np
    played = []

    class Chunk:
        sample_rate = 16000
        audio_float_array = np.linspace(-0.5, 0.5, 1600, dtype=np.float32)

    class FakeVoice:
        @staticmethod
        def load(path):
            assert path.endswith(f"{KEY}.onnx")
            return FakeVoice()

        def synthesize(self, text):
            yield Chunk()
            yield Chunk()

    fake_piper = types.ModuleType("piper"); fake_piper.PiperVoice = FakeVoice
    fake_sd = types.ModuleType("sounddevice")
    fake_sd.play = lambda audio, rate: played.append((len(audio), rate))
    fake_sd.stop = lambda: None
    fake_sd.wait = lambda: None
    fake_sd.get_stream = lambda: types.SimpleNamespace(active=False)
    monkeypatch.setitem(sys.modules, "piper", fake_piper)
    monkeypatch.setitem(sys.modules, "sounddevice", fake_sd)
    tts._piper_state.update(ok=None, error="")
    folder = home / "voicebanks" / KEY
    folder.mkdir(parents=True)
    (folder / f"{KEY}.onnx").write_bytes(ONNX); (folder / f"{KEY}.onnx.json").write_bytes(CFG)
    events = []
    sp = tts.Speaker(lambda **ev: events.append(ev))
    assert sp.readiness(KEY) == {"ok": True}
    sp.speak("hello there", voice_id=KEY, strict=True); wait_done(events)
    assert played == [(3200, 16000)] and not any(e["kind"] in ("error", "notice") for e in events)
    tts._piper_state.update(ok=None, error="")


def test_half_downloaded_voice_does_not_count_as_installed(home):
    folder = home / "voicebanks" / KEY
    folder.mkdir(parents=True)
    (folder / f"{KEY}.onnx").write_bytes(ONNX)                   # model without its config
    assert not tts.voice_installed(KEY)
    (folder / f"{KEY}.onnx.json").write_bytes(CFG)
    assert tts.voice_installed(KEY)


def test_packaged_exe_skips_pip_and_downloads_the_engine_itself(monkeypatch):
    from reo import engine_install
    events = []
    monkeypatch.setattr(tts, "piper_status", lambda refresh=False: (False, "x"))
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(tts.subprocess, "run", lambda *a, **k: pytest.fail("pip must not be used in the packaged exe"))

    def no_network(emit, cf=None, **kw):
        raise engine_install.EngineError("network", "Could not reach pypi.org. Check your internet connection and try again.")
    monkeypatch.setattr(engine_install, "install", no_network)
    r = tts.install_engine(lambda **ev: events.append(ev))
    assert r["ok"] is False and r["code"] == "network" and events[-1]["state"] == "error" and "pypi.org" in events[-1]["message"]


def test_bridge_exposes_downloads_and_flags_busy_voices(home, monkeypatch):
    monkeypatch.setenv("REO_MOCK_ENGINE", "1")
    from reo.bridge import Bridge
    events = []
    b = Bridge(emit=events.append)
    b._voicedl = tts.VoiceDownloader(lambda **ev: events.append(dict(ev, e="voicedl")), client_factory=server(), loader=lambda p: None)
    assert b.download_voice(KEY)["started"] is True
    end = time.time() + 10
    while time.time() < end and not any(e.get("e") == "voicedl" and e["state"] == "done" for e in events):
        time.sleep(0.02)
    assert any(e.get("e") == "voicedl" and e["state"] == "done" for e in events)
    voices = {v["id"]: v for v in b.list_voicebanks()}
    assert voices[KEY]["installed"] is True and voices["en_US-lessac-medium"]["installed"] is False
    assert b.voice_status()["engine"].keys() >= {"piper", "sapi", "frozen", "can_install"}
    assert b.tts_preview(KEY)["ok"] in (True, False)


def test_pronunciation_rules_replace_whole_words_only():
    r = "colega = co le ga\n# comment\nReo -> Ray-oh"
    assert tts.apply_pronunciations("Hola Colega, colegas y colega.", r) == "Hola co le ga, colegas y co le ga."
    assert tts.apply_pronunciations("Hi Reo", r) == "Hi Ray-oh"
    assert tts.apply_pronunciations("nada", "") == "nada"


def test_volume_is_clamped_and_default_rule_ships():
    from reo.config import DEFAULTS
    assert [tts.clamp_volume(x) for x in (150, -5, "40", None)] == [100, 0, 40, 100]
    assert tts.apply_pronunciations("colega", DEFAULTS["pronunciations"]) == "co le ga" and DEFAULTS["volume"] == 100


def test_speaker_applies_rules_and_volume():
    class S:
        d = {"pronunciations": "colega = co le ga", "volume": 30, "voice": "system"}
        def get(self, k): return self.d.get(k)
    sp = tts.Speaker(lambda **k: None, S())
    got = {}
    class B:
        def available(self): return True
        def speak(self, text, cancel, name=None, volume=100): got.update(text=text, vol=volume)
    sp._sapi = B()
    sp._speak_one("Hola colega", "system")
    assert got == {"text": "Hola co le ga", "vol": 30}
