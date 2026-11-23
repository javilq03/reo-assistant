"""Gameplay: screen recognition, knowledge files, learning rules, the attempt loop, calibration, the vision call.
Everything runs against a fake game window that shows coloured screens and records clicks."""
import json
import threading

import httpx
import pytest
from PIL import Image

from reo.engines.cloud import CloudEngine, CloudError
from reo.gameplay import learning
from reo.gameplay.brain import Brain, BrainError, parse_json, validate_actions
from reo.gameplay.io import crop_norm, encode, wait_point
from reo.gameplay.manager import GameplayManager
from reo.gameplay.runner import GameplayRunner, RunConfig
from reo.gameplay.screens import ScreenDetector, similarity
from reo.gameplay.store import GameStore

REAL_CLIENT = httpx.Client                    # captured before any test patches it
W, H = 400, 300
REGION = [0.0, 0.0, 0.25, 0.25]              # the corner of the window where each screen draws its marker
COLORS = {"battle_ready": (200, 40, 40), "victory": (40, 200, 40), "defeat": (40, 40, 200),
          "retry_prompt": (200, 200, 40), "animating": (90, 90, 90)}


def frame(kind: str) -> Image.Image:
    """A window image whose top-left corner carries the screen's marker (a coloured box with a notch, so it has structure)."""
    im = Image.new("RGB", (W, H), (15, 15, 15))
    box = Image.new("RGB", (100, 75), COLORS.get(kind, (15, 15, 15)))
    box.paste(Image.new("RGB", (40, 75), (255, 255, 255)), (60, 0))          # a bold white bar: real UI crops have structure
    im.paste(box, (0, 0))
    return im


class FakeWindow:
    """Plays a script of screens: each capture returns the next one (the last repeats)."""
    def __init__(self, script):
        self.script, self.i, self.clicks = list(script), 0, []
        self.stop_after = None
        self.fg = True
        self.pointer = None
        self.keys = []
        self.hits = []                 # registered-hotkey presses (what RegisterHotKey delivers)
        self.armed, self.arm_ok, self.arm_calls = False, True, 0
        self.warning = ""

    def find(self): return True
    def valid(self): return True
    def focus(self): self.fg = True; return True
    def is_foreground(self): return self.fg
    def aborted(self): return False
    def begin(self): pass
    def end(self): pass

    def capture(self):
        kind = self.script[min(self.i, len(self.script) - 1)]
        self.i += 1
        return frame(kind)

    def click(self, nx, ny): self.clicks.append((round(nx, 2), round(ny, 2)))

    def cursor_norm(self): return self.pointer
    def key_down(self, vk): return self.keys.pop(0) if self.keys else False
    def arm_hotkey(self): self.arm_calls += 1; self.armed = self.arm_ok; return self.arm_ok
    def disarm_hotkey(self): self.armed = False
    def hotkey_hit(self): return self.hits.pop(0) if self.hits else False
    def elevation_warning(self): return self.warning


class Clock:
    def __init__(self): self.t = 0.0
    def now(self): return self.t
    def sleep(self, s): self.t += s


@pytest.fixture()
def store(home):
    s = GameStore(home / "games")
    gid = s.create_game("Test Game", "tg")
    s.save_game(gid, {"window_title": "Test", "boss": "demon"})
    for name, p in {"card_slot_1": (0.2, 0.8), "card_slot_2": (0.4, 0.8), "confirm": (0.5, 0.5), "retry": (0.6, 0.5)}.items():
        s.set_target(gid, name, p)
    for st in ("battle_ready", "victory", "defeat", "retry_prompt"):
        s.save_screen(gid, st, crop_norm(frame(st), REGION), REGION)
    s.set_screen_cfg(gid, "defeat", {"tap": "retry", "outcome": "loss"})
    return s


def runner(store, script, answers, reflections=None, clock=None, window=None, **cfg):
    """A runner whose model replies come from `answers` (decision dicts) and `reflections` (reflection dicts)."""
    clock = clock or Clock()
    answers, reflections = list(answers), list(reflections or [])

    def ask(system, text, images):
        if "coach" in system:
            return json.dumps(reflections.pop(0) if reflections else {"cause": "", "facts": [], "hypotheses": [], "changes": []})
        return json.dumps(answers.pop(0) if answers else answers_default)

    answers_default = {"state": {"boss_hp_pct": 50}, "actions": [{"type": "tap", "target": "card_slot_1"}], "reason": "default"}
    win = window or FakeWindow(script)
    r = GameplayRunner(store, win, Brain(ask), lambda **ev: events.append(ev), threading.Event(), sleep=clock.sleep, now=clock.now)
    events = []
    r.events = events
    r.win = win
    r.cfg = RunConfig(game="tg", boss="demon", **cfg)
    return r


def decision(hp, *targets, reason="r"):
    return {"state": {"turn": 1, "boss_hp_pct": hp, "boss_telegraph": "heavy"},
            "actions": [{"type": "tap", "target": t} for t in targets], "reason": reason}


# ------------------------------------------------------------------ screens
def test_similarity_tells_screens_apart_and_ignores_uniform_dark(store):
    det = ScreenDetector(store, "tg")
    for st in ("battle_ready", "victory", "defeat", "retry_prompt"):
        assert det.classify(frame(st))[0] == st
    assert det.classify(frame("animating"))[0] is None
    blank = Image.new("RGB", (W, H), (200, 40, 40))                      # right colour, no structure
    assert det.classify(blank)[0] != "battle_ready"
    t = crop_norm(frame("battle_ready"), REGION)
    from reo.gameplay.screens import _arr
    assert similarity(_arr(t), _arr(t)) > 0.99


def test_structure_guard_rejects_a_flat_sample_that_is_close_in_colour():
    import numpy as np
    template = np.zeros((64, 64, 3), dtype=np.float32)
    template[:, :32] = (230, 40, 40)
    template[:, 32:] = (140, 40, 40)
    flat = np.full((64, 64, 3), (185, 40, 40), dtype=np.float32)              # colours are close on average...
    assert 1 - float(np.abs(flat - template).mean()) / 255 > 0.9
    assert similarity(flat, template) == 0.0                                   # ...but there is no structure, so no match


def test_detector_survives_window_resize(store):
    det = ScreenDetector(store, "tg")
    big = frame("victory").resize((W * 2, H * 2))
    assert det.classify(big)[0] == "victory"


# ------------------------------------------------------------------ store and readiness
def test_problems_list_what_is_missing(home):
    s = GameStore(home / "games")
    gid = s.create_game("Fresh")
    probs = " ".join(s.problems(gid, ""))
    assert "window title" in probs and "boss name" in probs and "card_slot" in probs and "battle_ready" in probs
    s2 = GameStore(home / "games")
    assert s2.problems("fresh", "x")


def test_ready_store_has_no_problems(store):
    assert store.problems("tg", "demon") == []


def test_screen_tap_without_a_position_is_reported(store):
    store.remove_target("tg", "retry")
    assert any("retry" in p for p in store.problems("tg", "demon"))


def test_default_game_is_seeded(home):
    s = GameStore(home / "games")
    m = GameplayManager(lambda **e: None, lambda: None, lambda: None, store=s)
    st = m.state()
    assert st["game"]["name"].startswith("The Seven Deadly Sins")
    assert st["problems"]


# ------------------------------------------------------------------ brain
def test_parse_json_tolerates_fences_and_prose():
    assert parse_json('Sure!\n```json\n{"a": 1}\n```') == {"a": 1}
    with pytest.raises(BrainError):
        parse_json("no json here")


def test_actions_are_whitelisted():
    ok, bad = validate_actions([{"type": "tap", "target": "a"}, {"type": "tap", "target": "evil"}, {"type": "type", "text": "x"},
                                {"type": "wait", "seconds": 99}], {"a"})
    assert ok == [{"type": "tap", "target": "a"}, {"type": "wait", "seconds": 5.0}]
    assert len(bad) == 2


def test_brain_retries_once_then_gives_up():
    calls = []

    def ask(system, text, images):
        calls.append(text)
        return '{"actions": [{"type": "tap", "target": "nope"}]}'
    ctx = {"game": "g", "boss": "b", "attempt": 1, "turn": 1, "targets": ["a"], "profile": [], "playbook": "", "recent": [], "images": []}
    with pytest.raises(BrainError):
        Brain(ask).decide(ctx)
    assert len(calls) == 2 and "rejected" in calls[1]


def test_reflection_is_trimmed_and_respects_can_change():
    reply = {"cause": "c", "facts": ["a", "b", "c", "d"], "hypotheses": ["h"], "changes": [{"op": "add", "text": "x"}, {"op": "add", "text": "y"}, {"op": "add", "text": "z"}, {"op": "bogus"}]}
    ctx = {"game": "g", "boss": "b", "outcome": "loss", "turns": [], "boss_hp_left": 30, "playbook": "", "profile": [], "hypotheses": [], "can_change": True}
    r = Brain(lambda *a: json.dumps(reply)).reflect(ctx)
    assert len(r["facts"]) == 3 and len(r["changes"]) == 2
    assert Brain(lambda *a: json.dumps(reply)).reflect(dict(ctx, can_change=False))["changes"] == []


# ------------------------------------------------------------------ learning
def attempt(boss, n, version, outcome, hp):
    boss.add_attempt({"attempt": n, "outcome": outcome, "turns": 5, "boss_hp_left": hp, "playbook_version": version})


def test_changes_are_one_at_a_time(store):
    b = store.boss("tg", "demon")
    assert learning.apply_changes(b, [{"op": "add", "text": "save ult for phase 2"}]) == 2
    pb = b.playbook()
    assert pb["pending"] and pb["rules"][0]["status"] == "trial"
    assert learning.apply_changes(b, [{"op": "add", "text": "another"}]) is None       # still being evaluated


def test_worse_version_is_rolled_back_and_inherits_the_old_scores(store):
    b = store.boss("tg", "demon")
    learning.set_manual_rules(b, "keep red cards for the boss turn")                   # v2, solid rule
    for n in range(1, 4):
        attempt(b, n, 2, "win" if n == 1 else "loss", 20)
    assert learning.apply_changes(b, [{"op": "add", "text": "never use ult"}]) == 3
    for n in range(4, 7):
        attempt(b, n, 3, "loss", 70)
    assert learning.evaluate(b) == "rolled_back"
    pb = b.playbook()
    assert [r["text"] for r in pb["rules"]] == ["keep red cards for the boss turn"]
    assert pb["version"] == 4 and not pb["pending"] and 2 in pb["same_as"]
    assert learning.version_stats(b.attempts(), learning.equiv(pb))["n"] == 3          # the failed version's attempts do not count


def test_better_version_is_kept_and_trial_rules_become_rules(store):
    b = store.boss("tg", "demon")
    learning.set_manual_rules(b, "base rule")
    for n in range(1, 4):
        attempt(b, n, 2, "loss", 60)
    learning.apply_changes(b, [{"op": "add", "text": "new idea"}])
    assert learning.evaluate(b) is None                                                 # not enough attempts yet
    for n in range(4, 7):
        attempt(b, n, 3, "loss", 30)
    assert learning.evaluate(b) == "kept"
    pb = b.playbook()
    assert not pb["pending"] and {r["status"] for r in pb["rules"]} == {"rule"}


def test_playbook_is_capped(store):
    b = store.boss("tg", "demon")
    learning.set_manual_rules(b, "\n".join(f"rule {i}" for i in range(30)))
    learning.apply_changes(b, [{"op": "add", "text": "one more"}])
    pb = b.playbook()
    assert len(pb["rules"]) == 30 and any(r["text"] == "one more" for r in pb["rules"])


def test_facts_and_hypotheses_dedupe(store):
    b = store.boss("tg", "demon")
    assert b.add_facts(["Boss AoE on turn 3", "boss aoe on turn 3", "Boss heals at 50%"]) == 2
    assert b.add_hypotheses(["ult on turn 5"]) == 1 and b.add_hypotheses(["ULT on turn 5"]) == 0
    assert (b.dir / "profile.md").read_text().count("- ") == 2


# ------------------------------------------------------------------ the loop
def test_loss_then_win_navigates_logs_and_learns(store):
    script = ["animating", "battle_ready", "animating", "defeat", "retry_prompt", "battle_ready", "animating", "victory"]
    r = runner(store, script,
               answers=[decision(80, "card_slot_1"), decision(30, "card_slot_2")],
               reflections=[{"cause": "died on turn 1 to the heavy attack", "facts": ["boss does a heavy attack on turn 1"],
                             "hypotheses": ["guard first"], "changes": [{"op": "add", "text": "guard on turn 1"}]}])
    res = r.run(r.cfg)
    assert res["finished"] and res["attempts"] == 2 and res["wins"] == 1 and res["losses"] == 1
    b = store.boss("tg", "demon")
    att = b.attempts()
    assert [a["outcome"] for a in att] == ["loss", "win"]
    assert att[0]["boss_hp_left"] == 80 and att[1]["boss_hp_left"] == 0
    assert att[0]["playbook_version"] == 1 and att[1]["playbook_version"] == 2          # the reflection changed the playbook
    assert b.profile() == ["boss does a heavy attack on turn 1"]
    assert b.hypotheses() == ["guard first"]
    assert (0.2, 0.8) in r.win.clicks and (0.4, 0.8) in r.win.clicks                       # card taps
    assert (0.6, 0.5) in r.win.clicks and (0.5, 0.5) in r.win.clicks                       # retry + confirm navigation
    assert (b.dir / "attempts" / "1" / "turn_1.jpg").is_file() and b.turn_logs(1)[0]["reason"] == "r"
    msgs = [e["message"] for e in r.events if e["kind"] == "attempt"]
    assert "lost on turn 1" in msgs[0] and "guard on turn 1" in msgs[0] and "won on turn 1" in msgs[1]


def test_stops_after_max_attempts(store):
    script = ["battle_ready", "animating", "defeat", "retry_prompt", "battle_ready", "animating", "defeat"]
    r = runner(store, script, answers=[decision(50, "card_slot_1")] * 5, max_attempts=2, stop_on_win=True)
    res = r.run(r.cfg)
    assert res["finished"] and res["attempts"] == 2 and "2 attempts" in res["stopped"]


def test_unrecognized_screen_stops_with_a_saved_screenshot(store):
    r = runner(store, ["animating"], answers=[], unknown_limit=10)
    res = r.run(r.cfg)
    assert not res["finished"] and "could not recognize" in res["stopped"]
    assert list(store.dir("tg").glob("unknown_*.jpg"))


def test_turn_that_never_advances_stops_after_three_tries(store):
    r = runner(store, ["battle_ready"], answers=[decision(50, "card_slot_1")] * 6)
    res = r.run(r.cfg)
    assert "did not advance" in res["stopped"] and res["decisions"] == 3


def test_model_failures_stop_the_run(store):
    r = runner(store, ["battle_ready"], answers=[{"actions": []}] * 20)
    res = r.run(r.cfg)
    assert "keeps failing" in res["stopped"]


def test_decision_cap(store):
    r = runner(store, ["battle_ready", "animating", "battle_ready", "animating"], answers=[decision(50, "card_slot_1")] * 5, max_decisions=1)
    res = r.run(r.cfg)
    assert "cap" in res["stopped"] and res["decisions"] == 1


def test_stops_when_the_game_loses_focus(store):
    win = FakeWindow(["battle_ready"])
    win.focus = lambda: False
    win.fg = False
    r = runner(store, ["battle_ready"], answers=[decision(50, "card_slot_1")], window=win)
    res = r.run(r.cfg)
    assert "not in front" in res["stopped"] and win.clicks == []


def test_stop_event_ends_the_run(store):
    r = runner(store, ["animating"], answers=[])
    threading.Timer(0, r.stop.set).start()
    import time
    time.sleep(0.05)
    res = r.run(r.cfg)
    assert res["stopped"] == "Stopped."


def test_max_turns_records_a_timeout_and_stops(store):
    script = ["battle_ready", "animating"] * 10
    r = runner(store, script, answers=[decision(50, "card_slot_1")] * 10, max_turns=2)
    res = r.run(r.cfg)
    assert "went past 2 turns" in res["stopped"]
    assert store.boss("tg", "demon").attempts()[-1]["outcome"] == "timeout"


# ------------------------------------------------------------------ calibration
def test_wait_point_returns_the_pointer_on_f8():
    w = FakeWindow([])
    w.keys = [False, False, True, True, False]
    w.pointer = (0.25, 0.75)
    clock = Clock()
    assert wait_point(w, threading.Event(), timeout=5, sleep=clock.sleep, now=clock.now) == (0.25, 0.75)


def test_wait_point_times_out_and_ignores_pointer_outside_the_window():
    w = FakeWindow([])
    w.keys = [False, True, False, True, False]
    w.pointer = None
    clock = Clock()
    assert wait_point(w, threading.Event(), timeout=2, sleep=clock.sleep, now=clock.now) is None


def test_wait_point_accepts_a_registered_hotkey_when_polling_sees_nothing():
    w = FakeWindow([])
    w.keys = []                                      # GetAsyncKeyState-style polling never sees F8 (game has the input)
    w.hits = [False, True]
    w.pointer = (0.4, 0.6)
    clock = Clock()
    assert wait_point(w, threading.Event(), timeout=5, sleep=clock.sleep, now=clock.now) == (0.4, 0.6)


def test_hotkey_press_with_the_pointer_outside_the_game_is_ignored():
    w = FakeWindow([])
    w.hits = [True, True]
    w.pointer = None
    clock = Clock()
    assert wait_point(w, threading.Event(), timeout=2, sleep=clock.sleep, now=clock.now) is None


def make_manager(store, window, ask=lambda *a: "{}"):
    events = []
    m = GameplayManager(lambda **e: events.append(e), lambda: None, lambda: ask, store=store, window_factory=lambda t: window, sleep=lambda s: None)
    m.events = events
    return m


def wait_idle(m, timeout=5):
    import time
    end = time.time() + timeout
    while m.busy() and time.time() < end:
        time.sleep(0.01)
    assert not m.busy()


def test_calibrating_a_target_saves_its_position(store):
    w = FakeWindow(["battle_ready"])
    w.keys = [False, True, False]
    w.pointer = (0.3, 0.9)
    m = make_manager(store, w)
    assert m.calibrate("tg", "target", "ultimate") == {"ok": True}
    wait_idle(m)
    assert store.coords("tg")["ultimate"] == [0.3, 0.9]
    assert m.events[-1]["phase"] == "done"


def test_calibrating_a_screen_saves_a_template_from_the_live_window(store):
    w = FakeWindow(["victory"])
    pts = iter([(0.0, 0.0), (0.25, 0.25)])
    w.cursor_norm = lambda: next(pts)
    w.keys = [False, True, False, True, False]
    m = make_manager(store, w)
    assert m.calibrate("tg", "screen", "victory2") == {"ok": True}
    wait_idle(m)
    cfg = store.screens("tg")["victory2"]
    assert cfg["region"] == [0.0, 0.0, 0.25, 0.25] and store.template_path("tg", "victory2").is_file()
    assert ScreenDetector(store, "tg").classify(frame("victory"))[0] in ("victory", "victory2")


def test_calibration_uses_the_hotkey_and_always_releases_it(store):
    w = FakeWindow(["battle_ready"])
    w.hits = [True]
    w.pointer = (0.3, 0.9)
    m = make_manager(store, w)
    m.calibrate("tg", "target", "ultimate")
    wait_idle(m)
    assert store.coords("tg")["ultimate"] == [0.3, 0.9]            # found with polling dead: only the hotkey fired
    assert w.arm_calls == 1 and w.armed is False


def test_f8_owned_by_another_program_is_reported_but_polling_still_works(store):
    w = FakeWindow(["battle_ready"])
    w.arm_ok = False
    w.keys = [False, True, False]
    w.pointer = (0.1, 0.2)
    m = make_manager(store, w)
    m.calibrate("tg", "target", "ultimate")
    wait_idle(m)
    assert store.coords("tg")["ultimate"] == [0.1, 0.2]
    assert any(e["kind"] == "log" and "using F8" in e["text"] for e in m.events)


def test_game_running_as_administrator_is_called_out(store):
    w = FakeWindow(["battle_ready"])
    w.warning = "The game is running as administrator and Reo is not."
    w.hits = [True]
    w.pointer = (0.5, 0.5)
    m = make_manager(store, w)
    m.calibrate("tg", "target", "ultimate")
    wait_idle(m)
    assert any(e["kind"] == "log" and "administrator" in e["text"] for e in m.events)


def test_missing_f8_explains_what_to_try(store):
    w = FakeWindow(["battle_ready"])
    m = make_manager(store, w)
    m.point_timeout = 0.05
    m.calibrate("tg", "target", "ultimate")
    wait_idle(m)
    last = m.events[-1]
    assert last["phase"] == "failed" and "did not see F8" in last["error"] and "administrator" in last["error"]
    assert "ultimate" not in store.coords("tg")
    assert w.armed is False


def test_screen_capture_brings_the_game_to_the_front_first(store):
    """F8 pressed while Reo had focus used to end in 'the game was not in front when capturing'."""
    w = FakeWindow(["victory"])
    pts = iter([(0.0, 0.0), (0.25, 0.25)])

    def cursor():
        p = next(pts)
        if p == (0.25, 0.25):
            w.fg = False                                   # Reo took the focus when the user clicked it
        return p
    w.cursor_norm = cursor
    w.hits = [True, True]
    m = make_manager(store, w)
    assert m.calibrate("tg", "screen", "victory2") == {"ok": True}
    wait_idle(m)
    assert m.events[-1]["phase"] == "done"
    assert store.template_path("tg", "victory2").is_file()


def test_screen_capture_fails_clearly_when_the_game_cannot_be_brought_forward(store):
    w = FakeWindow(["victory"])
    pts = iter([(0.0, 0.0), (0.25, 0.25)])
    w.cursor_norm = lambda: next(pts)
    w.hits = [True, True]
    calls = {"n": 0}

    def focus():                                           # the first focus (start of calibration) works, the later one does not
        calls["n"] += 1
        w.fg = calls["n"] == 1
        return w.fg
    w.focus = focus
    m = make_manager(store, w)
    m.calibrate("tg", "screen", "victory2")
    wait_idle(m)
    last = m.events[-1]
    assert last["phase"] == "failed" and "bring the game to the front" in last["error"]
    assert not store.template_path("tg", "victory2").exists()


def test_calibration_needs_a_window_title(home):
    s = GameStore(home / "games")
    gid = s.create_game("NoTitle")
    m = make_manager(s, FakeWindow([]))
    assert "window title" in m.calibrate(gid, "target", "x")["error"]


def test_only_one_job_at_a_time(store):
    w = FakeWindow(["animating"])
    w.keys = []
    m = make_manager(store, w)
    assert m.calibrate("tg", "target", "ultimate") == {"ok": True}
    assert "already" in m.calibrate("tg", "target", "other")["error"]
    m.cancel()
    wait_idle(m)


def test_start_refuses_when_not_ready_and_lists_why(home):
    s = GameStore(home / "games")
    gid = s.create_game("Fresh")
    m = make_manager(s, FakeWindow([]))
    r = m.start(gid, "boss")
    assert r["error"] == "Not ready yet." and r["problems"]


def test_manager_runs_a_full_session_and_reports(store):
    script = ["battle_ready", "animating", "victory"]
    answers = iter([json.dumps(decision(10, "card_slot_1"))])
    w = FakeWindow(script)
    m = make_manager(store, w, ask=lambda s, t, i: next(answers))
    assert m.start("tg", "demon", 3, True) == {"ok": True}
    wait_idle(m)
    import time
    for _ in range(100):
        if any(e["kind"] == "done" for e in m.events):
            break
        time.sleep(0.01)
    done = [e for e in m.events if e["kind"] == "done"][0]
    assert done["finished"] and done["wins"] == 1
    info = m.state("tg")["boss"]
    assert info["attempts"][-1]["outcome"] == "win" and info["version"] == 1


def test_teach_and_manual_rules(store):
    m = make_manager(store, FakeWindow([]))
    m.teach("tg", "demon", "the boss reflects damage when it glows red")
    m.set_rules("tg", "demon", "- save ult for phase 2\n- keep 2 red cards")
    info = m.state("tg")["boss"]
    assert info["profile"] == ["the boss reflects damage when it glows red"]
    assert info["rules"].splitlines() == ["save ult for phase 2", "keep 2 red cards"] and info["version"] == 2


# ------------------------------------------------------------------ the vision call
def cloud(monkeypatch, protocol, handler):
    from reo.config import Settings
    s = Settings()
    s.update({"provider": "anthropic" if protocol == "anthropic" else "openai", "api_key": "k", "cloud_model": "m"})
    monkeypatch.setattr(httpx, "Client", lambda **kw: REAL_CLIENT(transport=httpx.MockTransport(handler), **{k: v for k, v in kw.items() if k != "transport"}))
    return CloudEngine(s)


def test_vision_openai_wire_format(monkeypatch, home):
    seen = {}

    def handler(req):
        seen.update(json.loads(req.content), url=str(req.url))
        return httpx.Response(200, json={"choices": [{"message": {"content": "hello"}}]})
    img = encode(frame("victory"))
    out = cloud(monkeypatch, "openai", handler).vision("sys", "look", [img])
    assert out == "hello" and seen["url"].endswith("/chat/completions")
    parts = seen["messages"][1]["content"]
    assert parts[0] == {"type": "text", "text": "look"} and parts[1]["image_url"]["url"].startswith("data:image/jpeg;base64,")


def test_vision_anthropic_wire_format(monkeypatch, home):
    seen = {}

    def handler(req):
        seen.update(json.loads(req.content), url=str(req.url))
        return httpx.Response(200, json={"content": [{"type": "text", "text": "ok"}]})
    out = cloud(monkeypatch, "anthropic", handler).vision("sys", "look", [encode(frame("victory"))])
    assert out == "ok" and seen["url"].endswith("/messages") and seen["system"] == "sys"
    block = seen["messages"][0]["content"][0]
    assert block["type"] == "image" and block["source"]["media_type"] == "image/jpeg"


def test_vision_retries_without_rejected_parameters_and_reports_errors(monkeypatch, home):
    calls = []

    def handler(req):
        body = json.loads(req.content)
        calls.append(body)
        if "max_tokens" in body:
            return httpx.Response(400, json={"error": {"message": "Unsupported parameter: max_tokens, use max_completion_tokens"}})
        return httpx.Response(200, json={"choices": [{"message": {"content": "fine"}}]})
    assert cloud(monkeypatch, "openai", handler).vision("s", "t", [encode(frame("victory"))]) == "fine"
    assert "max_completion_tokens" in calls[1]
    with pytest.raises(CloudError, match="401"):
        cloud(monkeypatch, "openai", lambda req: httpx.Response(401, json={"error": {"message": "bad key"}})).vision("s", "t", [])


# ------------------------------------------------------------------ the Windows hotkey thread, against a fake user32
def test_hotkey_thread_registers_receives_and_unregisters():
    import ctypes
    from types import SimpleNamespace
    from reo.gameplay.io import WM_HOTKEY, WM_QUIT, VK_F8, _Hotkey

    calls = []
    queue = [WM_HOTKEY, WM_QUIT]                         # one F8, then the quit that close() posts
    quit_posted = threading.Event()

    class User32:
        def RegisterHotKey(self, hwnd, ident, mods, vk):
            calls.append(("register", mods, vk))
            return 1

        def GetMessageW(self, ref, *_):
            while not queue:
                return 0
            m = queue.pop(0)
            if m == WM_QUIT:
                quit_posted.wait(2)
                return 0                                 # GetMessage returns 0 on WM_QUIT
            ref._obj.message = m
            return 1

        def PostThreadMessageW(self, tid, msg, *_):
            calls.append(("quit", msg))
            quit_posted.set()

        def UnregisterHotKey(self, hwnd, ident):
            calls.append(("unregister",))

    fake_c = SimpleNamespace(byref=ctypes.byref, windll=SimpleNamespace(kernel32=SimpleNamespace(GetCurrentThreadId=lambda: 4242)))
    hk = _Hotkey(User32(), fake_c, VK_F8)
    assert hk.ok and hk.hit.wait(2)                      # the WM_HOTKEY message set the flag
    hk.close()
    for _ in range(100):
        if ("unregister",) in calls:
            break
        threading.Event().wait(0.01)
    assert calls[0] == ("register", 0x4000, VK_F8)       # MOD_NOREPEAT, F8
    assert ("quit", WM_QUIT) in calls and ("unregister",) in calls


def test_hotkey_that_cannot_be_registered_reports_not_ok():
    import ctypes
    from types import SimpleNamespace
    from reo.gameplay.io import VK_F8, _Hotkey

    class User32:
        def RegisterHotKey(self, *a): return 0           # F8 already owned by another program
    fake_c = SimpleNamespace(byref=ctypes.byref, windll=SimpleNamespace(kernel32=SimpleNamespace(GetCurrentThreadId=lambda: 1)))
    hk = _Hotkey(User32(), fake_c, VK_F8)
    assert hk.ok is False
