import json
import time

from reo import diaglog
from tests.test_stt import make_bridge


def test_clean_keeps_only_counters():
    r = diaglog.clean({"event": "turn", "ms_total": 12.345, "chars": 5, "text": "secret prompt", "error": "C:\\Users\\me\\x.py: boom",
                       "ok": True, "api_key": "k", "deep": 1})
    assert r["ms_total"] == 12.3 and r["chars"] == 5 and r["ok"] is True
    assert "text" not in r and "api_key" not in r and "deep" not in r          # wrong type (int for a bool) is dropped
    assert "\\" not in r["error"] and "Users" not in r["error"]
    assert diaglog.clean({"nothing": 1}) is None


def test_log_batches_rotates_and_never_raises(tmp_path):
    f = tmp_path / "logs" / "e.jsonl"
    d = diaglog.DiagnosticLog(f, maximum_bytes=400)
    for i in range(30):
        d.write(event="turn", ms_total=i)
        d.flush()
    d.close()
    assert f.exists() and (tmp_path / "logs" / "e.jsonl.1").exists()
    assert f.stat().st_size <= 400 + 120
    assert all(json.loads(line)["event"] == "turn" for line in f.read_text().splitlines())
    bad = diaglog.DiagnosticLog(tmp_path / "logs" / "e.jsonl" / "x" / "y.jsonl")      # unwritable parent: must stay silent
    bad.write(event="turn"); bad.close()


def test_chat_turn_is_logged_without_text(home, monkeypatch):
    b, events, holder = make_bridge(monkeypatch)
    cid = b.new_conversation()["id"]
    b.chat(cid, "hello there, this text must not be logged")
    for _ in range(100):
        if any(e.get("e") in ("end", "error") for e in events):
            break
        time.sleep(0.05)
    rows = [json.loads(x) for x in b._diag.tail(20)]
    turns = [r for r in rows if r["event"] == "turn"]
    assert turns and turns[-1]["ok"] is True and turns[-1]["ms_total"] >= 0
    assert "must not be logged" not in "".join(b._diag.tail(20))
    assert any(r["event"] == "startup" for r in rows)
    rep = b.bug_report("x")
    assert "recent timings" in rep["text"] and '"event":"turn"' in rep["text"]


def test_first_run_setup_starts_the_steps(home, monkeypatch):
    b, events, holder = make_bridge(monkeypatch)
    calls = []
    monkeypatch.setattr(b, "scan_hardware", lambda *a, **k: {"recommendations": [{"id": "m1", "file": "m1.gguf", "recommended": True, "fits": True}]})
    monkeypatch.setattr(b, "download_model", lambda cid="", **k: calls.append(("model", cid)) or {"ok": True})
    monkeypatch.setattr(b, "stt_prepare", lambda: calls.append(("stt",)) or {"ok": True})
    monkeypatch.setattr(b, "download_voice", lambda key: calls.append(("voice", key)) or {"ok": True})
    r = b.first_run_setup()
    assert r["model"] == "m1" and r["speech"] is True and r["voice"].endswith("medium")
    assert ("model", "m1") in calls and ("stt",) in calls
    monkeypatch.setattr(b, "download_model", lambda cid="", **k: {"error": "no"})
    assert "model" not in b.first_run_setup()


def test_old_default_context_window_is_raised_once(home, monkeypatch):
    from reo.config import Settings
    monkeypatch.setenv("REO_MOCK_ENGINE", "1")
    from reo.bridge import Bridge
    b = Bridge(emit=lambda e: None)
    assert b._settings.get("n_ctx") >= 8192 and b._settings.get("ctx_migrated") is True
    b._settings.update({"n_ctx": 4096})                      # the user chooses 4096 again after the migration: respected
    b2 = Bridge(emit=lambda e: None)
    b.shutdown()
    assert b2._settings.get("n_ctx") == 4096


def test_slow_first_text_shows_a_notice(home, monkeypatch):
    b, events, holder = make_bridge(monkeypatch)
    b.FIRST_TEXT_NOTICE = 0.05
    import reo.engines.base as base
    orig = b._pick_engine

    class Slow:
        name = "slow"
        def stream(self, *a, **k):
            time.sleep(0.4)
            yield {"t": "token", "text": "hi"}
            yield {"t": "end", "stats": {}}
    monkeypatch.setattr(b, "_pick_engine", lambda opts: (Slow(), ""))
    cid = b.new_conversation()["id"]
    b.chat(cid, "hello")
    for _ in range(100):
        if any(e.get("e") in ("end", "error") for e in events):
            break
        time.sleep(0.05)
    assert any(e.get("e") == "notice" and "Still working" in e.get("text", "") for e in events)
    assert any(e.get("e") == "end" for e in events)


# ---- stopwatch, pause/resume, snooze
def test_pause_resume_keeps_time_left(tmp_path):
    from reo import reminders
    fired = []
    r = reminders.Reminders(tmp_path / "r.json", fired.append)
    item = r.add(1000 + 600, "tea", "", False)
    assert r.pause(item["id"], now=1000 + 100)["left"] == 500
    assert r.check(now=5000) == [] and fired == []                      # a paused reminder never fires
    assert r.resume(item["id"], now=9000)["due"] == 9500
    assert r.check(now=9499) == [] and len(r.check(now=9500)) == 1
    assert r.pause("nope") is None and r.resume("nope") is None


def test_stopwatch_parse_and_time(tmp_path):
    from reo import reminders as m
    assert m.parse_stopwatch("start a stopwatch") == "start"
    assert m.parse_stopwatch("inicia el cronómetro") == "start"
    assert m.parse_stopwatch("stop the stopwatch") == "stop"
    assert m.parse_stopwatch("para el cronómetro") == "stop"
    assert m.parse_stopwatch("how long is the stopwatch at") == "read"
    assert m.parse_stopwatch("cuánto lleva el cronómetro") == "read"
    assert m.parse_stopwatch("reset the stopwatch") == "reset"
    assert m.parse_stopwatch("pon un cronómetro de 5 minutos") is None      # has a duration: that is a timer
    assert m.parse_stopwatch("what is a stopwatch made of and how does it work in detail please " * 3) is None
    assert m.fmt_elapsed(3725) == "1 hour 2 minutes 5 seconds" and m.fmt_elapsed(61, es=True) == "1 minuto 1 segundo"
    sw = m.Stopwatch(tmp_path / "s.json")
    sw.start(now=100); assert sw.read(now=160) == 60
    assert sw.stop(now=190) == 90 and not sw.running
    sw2 = m.Stopwatch(tmp_path / "s.json")                                  # survives a restart
    assert sw2.elapsed == 90
    sw2.start(now=200); sw2.reset(); assert sw2.read(now=999) == 0


def test_bridge_stopwatch_chat_snooze_and_pause(home, monkeypatch):
    b, events, holder = make_bridge(monkeypatch)
    cid = b.new_conversation()["id"]

    def say(t):
        events.clear()
        assert "user" in b.chat(cid, t)
        for _ in range(100):
            if any(e.get("e") in ("end", "error") for e in events):
                break
            time.sleep(0.05)
        return "".join(e.get("text", "") for e in events if e.get("e") == "delta")
    assert "started" in say("start the stopwatch")
    assert b.stopwatch("read")["running"] is True
    assert "stopped at" in say("stop the stopwatch")
    assert "Stopwatch reset" in say("reset the stopwatch")
    r = b.snooze_reminder("tea", "", False, 5)["reminder"]
    assert 290 < r["due"] - time.time() <= 301
    items = b.pause_reminder(r["id"])
    assert items[0]["paused"] is True
    items = b.resume_reminder(r["id"])
    assert not items[0].get("paused")
