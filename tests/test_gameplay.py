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
from reo.gameplay.io import crop_norm, encode
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


# ------------------------------------------------------------------ calibration (click on a screenshot)

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


def test_snapshot_returns_a_picture_and_brings_reo_back(store):
    w = FakeWindow(["battle_ready"])
    back = []
    m = make_manager(store, w)
    m._focus_reo = lambda: back.append(1)
    r = m.snapshot("tg")
    assert r["ok"] and r["image"].startswith("data:image/jpeg;base64,") and r["width"] > 0
    assert back == [1]


def test_snapshot_reports_a_game_that_will_not_come_forward_and_still_returns_to_reo(store):
    w = FakeWindow(["battle_ready"])
    w.focus = lambda: False
    w.fg = False
    back = []
    m = make_manager(store, w)
    m._focus_reo = lambda: back.append(1)
    assert "bring the game to the front" in m.snapshot("tg")["error"]
    assert back == [1]


def test_snapshot_needs_a_window_title(home):
    s = GameStore(home / "games")
    gid = s.create_game("NoTitle")
    m = make_manager(s, FakeWindow([]))
    assert "window title" in m.snapshot(gid)["error"]


def test_snapshot_passes_on_the_administrator_warning(store):
    w = FakeWindow(["battle_ready"])
    w.warning = "run as administrator"
    assert make_manager(store, w).snapshot("tg")["warning"] == "run as administrator"


def test_picking_a_target_and_a_region_saves_them(store):
    m = make_manager(store, FakeWindow(["battle_ready"]))
    r = m.save_pick("tg", "target", "ultimate", [0.25, 0.75])
    assert r["coords"]["ultimate"] == [0.25, 0.75]
    r = m.save_pick("tg", "region", "hand", [0.1, 0.6, 0.5, 0.3])
    assert r["regions"]["hand"] == [0.1, 0.6, 0.5, 0.3]


def test_picking_a_screen_crops_the_last_snapshot(store):
    m = make_manager(store, FakeWindow(["victory"]))
    assert "new screenshot" in m.save_pick("tg", "screen", "victory2", [0.2, 0.2, 0.5, 0.5])["error"]
    m.snapshot("tg")
    r = m.save_pick("tg", "screen", "victory2", [0.2, 0.2, 0.5, 0.5])
    assert r["screens"]["victory2"]["captured"] and store.template_path("tg", "victory2").is_file()


def test_picks_reject_bad_input(store):
    m = make_manager(store, FakeWindow([]))
    assert m.save_pick("tg", "target", "", [0.5, 0.5])["error"]
    assert m.save_pick("tg", "target", "x", [0.5])["error"]
    assert m.save_pick("tg", "region", "x", [0.1, 0.1, 0.001, 0.5])["error"]
    assert m.save_pick("tg", "target", "x", ["a", "b"])["error"]


def test_snapshot_is_refused_while_a_run_is_active(store):
    m = make_manager(store, FakeWindow([]))
    m._busy = "run"
    assert "running" in m.snapshot("tg")["error"]
    m._busy = ""


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
