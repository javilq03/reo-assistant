"""Follow mouse, sleep look, mood pulse, speaking state and the procedural fallback art."""
from PIL import Image

from reo import mood, orb_art
from tests.test_stt import make_bridge


def _ns():
    import importlib, sys, types
    # orb_native imports ctypes.windll lazily; the pure helpers must import anywhere
    return importlib.import_module("reo.orb_native")


def test_follow_target_sits_beside_cursor_and_flips_at_edges():
    n = _ns()
    work = (0, 0, 1920, 1080)
    assert n.follow_target(500, 400, 150, 1.0, work) == (536, 436)
    x, y = n.follow_target(1900, 1070, 150, 1.0, work)
    assert x + 150 <= 1920 and y + 150 <= 1080 and x < 1900 and y < 1070


def test_follow_step_eases_and_stops():
    n = _ns()
    pos = (0, 0)
    for _ in range(200):
        pos = n.follow_step(pos, (300, 200))
    assert abs(pos[0] - 300) <= 2 and abs(pos[1] - 200) <= 2
    assert n.follow_step((300, 200), (301, 201)) == (300, 200)
    first = n.follow_step((0, 0), (100, 0))
    assert 0 < first[0] < 100


def test_mood_detection_en_es():
    assert mood.detect("Done! Anything else?") == "happy"
    assert mood.detect("Lo siento, no puedo hacer eso.") == "sad"
    assert mood.detect("Which one do you prefer?") == "curious"
    assert mood.detect("The capital of France is Paris.") is None


def test_art_frames_for_every_state_mood_and_sleep(tmp_path):
    art = orb_art.OrbArt(tmp_path / "missing.png", 0.5)          # missing artwork -> procedural "R"
    base = art.frame("idle")
    for st in ("idle", "listening", "thinking", "speaking"):
        f = art.frame(st, .5, 1.2)
        assert f.size == base.size
    m = art.frame("speaking", .3, 1.0, "happy", 1.0, 0.0)
    asleep = art.frame("idle", 0, 1.0, None, 0, 1.0)
    assert m.tobytes() != art.frame("speaking", .3, 1.0).tobytes()
    assert max(asleep.getchannel("A").get_flattened_data()) < max(base.getchannel("A").get_flattened_data())
    centre = art.orb.getpixel((art.D // 2, art.D // 2))
    assert centre[3] > 0


def test_bridge_applies_orb_settings_and_mood(monkeypatch):
    b, events, holder = make_bridge(monkeypatch)

    class N:
        size = 220
        def __init__(self, **cb): self.calls = []
        def set_size(self, d): pass
        def screen(self): return (0, 0, 1920, 1080)
        def position(self): return (10, 20)
        def show(self, x, y): self.calls.append("show")
        def hide(self): self.calls.append("hide")
        def set_state(self, s): self.calls.append(("state", s))
        def set_level(self, v): pass
        def configure(self, follow=None, sleep=None, mood=None): self.calls.append(("cfg", follow, sleep, mood))
        def set_mood(self, m): self.calls.append(("mood", m))
        def close(self): pass
    b._native_factory = lambda **cb: holder.setdefault("n", N(**cb))
    b._window = type("W", (), {"hide": lambda s: None, "show": lambda s: None})()
    b.save_settings({"orb_follow": True})
    assert b.set_orb_mode(True) is True
    n = holder["n"]
    assert ("cfg", True, True, True) in n.calls
    b.save_settings({"orb_follow": False, "orb_mood": False})
    assert ("cfg", False, True, False) in n.calls
    b.save_settings({"orb_mood": True})
    assert b.orb_mood("Great, done!") == "happy" and ("mood", "happy") in n.calls
    b.orb_state("speaking")
    assert ("state", "speaking") in n.calls
    b._settings.update({"orb_follow": True, "orb_pos": [1, 2]})
    b.set_orb_mode(False)
    assert b._settings.get("orb_pos") == [1, 2]                     # a trailing orb does not overwrite the dragged spot
