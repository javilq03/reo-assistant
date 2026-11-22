"""Capri-style features: styles, brief replies, reply language, wake phrase + follow-up, reminders, capabilities, support."""
import time

from tests.test_stt import _run_chat, _scripted_bridge


def test_style_brief_language_and_listen_notes():
    from reo import styles
    assert styles.note({}) == ""
    n = styles.note({"comm_style": "business", "brief_replies": True, "reply_language": "fr", "wake_word": True, "wake_follow_up": True})
    assert "BUSINESS" in n and "[BREVITY]" in n and "French" in n and ";listen" in n
    assert "FOLLOW-UP" not in styles.note({"wake_word": False, "wake_follow_up": True})
    assert styles.strip_listen("Which one? ;listen") == ("Which one?", True)
    assert styles.strip_listen("plain") == ("plain", False)


def test_prompt_gets_style_and_language_override(home, monkeypatch):
    b, events, seen = _scripted_bridge(monkeypatch, ["ok"])
    b._settings.update({"comm_style": "wild", "reply_language": "de"})
    _run_chat(b, events, "hola, qué tal", {})
    prompt = seen[0][1]
    assert "COMMUNICATION STYLE: WILD" in prompt and "German" in prompt and "[LANGUAGE]" not in prompt


def test_listen_tag_is_hidden_and_opens_follow_up_window(home, monkeypatch):
    b, events, seen = _scripted_bridge(monkeypatch, ["Which file do you mean? ;listen"])
    b._settings.update({"wake_word": True, "wake_follow_up": True})
    cid = _run_chat(b, events, "open the file", {})
    assert b._store.get(cid)["messages"][-1]["text"] == "Which file do you mean?"
    assert [e for e in events if e["e"] == "end"][0]["listen"] is True
    r = b.wake_check("the second one")                        # no wake phrase needed inside the 12 s window
    assert r["ok"] and r["command"] == "the second one" and r.get("followup")
    assert not b.wake_check("and again")["ok"]                  # the window was used up


def test_follow_up_is_limited_to_three_in_a_row(home, monkeypatch):
    b, _, _ = _scripted_bridge(monkeypatch, [])
    b._settings.update({"wake_word": True, "wake_follow_up": True})
    for _ in range(3):
        b._arm_follow_up()
        assert b.wake_check("yes")["ok"]
    b._arm_follow_up()
    assert not b.wake_check("yes")["ok"]
    assert b.wake_check("Reo, hi")["named"]                     # hearing the name resets the count
    b._arm_follow_up()
    assert b.wake_check("yes")["ok"]


def test_custom_wake_phrase(home, monkeypatch):
    b, _, _ = _scripted_bridge(monkeypatch, [])
    b._settings.update({"wake_phrase": "Hey Jarvis"})
    assert b.wake_check("hey jarvis open notes") == {"ok": True, "command": "open notes", "named": True}
    assert not b.wake_check("Reo open notes")["ok"]


def test_reminder_chat_is_answered_by_the_app_and_fires(home, monkeypatch):
    b, events, seen = _scripted_bridge(monkeypatch, [])
    cid = _run_chat(b, events, "remind me in 10 minutes to call Ana", {})
    msgs = b._store.get(cid)["messages"]
    assert "Done, I'll remind you in 10 minutes" in msgs[-1]["text"] and "call Ana" in msgs[-1]["text"]
    assert seen == []                                            # the model was never asked
    items = b.list_reminders()
    assert len(items) == 1 and items[0]["text"] == "call Ana" and 590 < items[0]["due"] - time.time() <= 600
    fired = b._reminders.check(now=time.time() + 601)
    assert len(fired) == 1 and b.list_reminders() == []
    ev = [e for e in events if e["e"] == "reminder"][0]
    assert ev["text"] == "Reminder: call Ana" and b._store.get(cid)["messages"][-1]["text"] == "Reminder: call Ana"


def test_spanish_timer_and_manual_reminders(home, monkeypatch):
    b, events, _ = _scripted_bridge(monkeypatch, [])
    cid = _run_chat(b, events, "pon un temporizador de 5 minutos", {})
    assert b._store.get(cid)["messages"][-1]["text"].startswith("Temporizador puesto: 5 minutos")
    assert b.add_reminder("stretch", 1)["reminder"]["text"] == "stretch"
    assert b.add_reminder("x", 0)["error"] and b.add_reminder("x", "abc")["error"]
    rid = b.list_reminders()[0]["id"]
    assert len(b.delete_reminder(rid)) == 1


def test_reminders_survive_restart(home):
    from reo.reminders import Reminders
    path = home / "r.json"
    r = Reminders(path, lambda _: None)
    r.add(time.time() + 60, "later")
    assert [x["text"] for x in Reminders(path, lambda _: None).list()] == ["later"]


def test_capabilities_gate_actions_and_reset_each_launch(home, monkeypatch):
    from reo import computer
    b, _, _ = _scripted_bridge(monkeypatch, [])
    g = b.get_capabilities()["grants"]
    assert g == computer.CAPABILITY_DEFAULTS and g["shell"] is False and g["files"] is False
    comp = object.__new__(computer.WinComputer)
    comp._check = lambda: None
    comp.shell_run = lambda command, cwd=None: "ran " + command
    comp.grants = b._caps
    assert comp._execute({"do": "shell.run", "command": "dir"}).startswith("BLOCKED")
    assert comp._execute({"do": "file.read", "path": "C:\\x"}).startswith("BLOCKED")
    b.set_capability("shell", True)
    assert comp._execute({"do": "shell.run", "command": "dir"}) == "ran dir"
    assert b.set_capability("nope", True)["error"]


def test_bug_report_has_no_secrets_or_messages(home, monkeypatch):
    b, events, _ = _scripted_bridge(monkeypatch, ["a private answer"])
    b._settings.update({"system_prompt": "SECRET PROMPT TEXT", "comm_style": "wild"})
    _run_chat(b, events, "a private question", {})
    r = b.bug_report("it crashed when I opened the menu")
    assert "it crashed" in r["text"] and "comm_style: wild" in r["text"]
    for bad in ("SECRET PROMPT TEXT", "a private question", "a private answer", "api_key"):
        assert bad not in r["text"]
    assert (home / "reports").is_dir()


def test_context_usage_and_delete_all(home, monkeypatch):
    b, events, _ = _scripted_bridge(monkeypatch, ["x" * 400])
    cid = _run_chat(b, events, "hi", {})
    u = b.context_usage(cid)
    assert u["used"] > 100 and u["max"] >= 1024 and 0 < u["percent"] <= 100
    out = b.delete_all_conversations()
    assert out["conversation"]["messages"] == [] and len(out["conversations"]) <= 1


def test_ack_audio_roundtrip(home, monkeypatch):
    b, _, _ = _scripted_bridge(monkeypatch, [])
    f = home / "ding.wav"
    f.write_bytes(b"RIFFxxxxWAVE")
    assert b.set_ack_audio(str(f))["ok"] and b._settings.get("wake_ack") == "custom"
    assert b.get_ack_audio().startswith("data:audio/wav;base64,")
    assert b.set_ack_audio(str(home / "x.txt"))["error"]
    b.clear_ack_audio()
    assert b.get_ack_audio() == "" and b._settings.get("wake_ack") == "voice"
