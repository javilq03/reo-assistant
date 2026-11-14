"""Speech-to-text engine (fake microphone + fake Whisper) and the native orb window logic (fake windows)."""
import threading
import time
import types

import numpy as np
import pytest

from reo import stt as S
from reo.config import Settings


class FakeSD:
    """sounddevice stand-in: plays a list of (seconds, amplitude) segments, then silence."""
    def __init__(self, plan):
        self.plan = plan
        self.default = types.SimpleNamespace(hostapi=0)

    def query_devices(self):
        return [{"name": "Mic A", "max_input_channels": 1}, {"name": "Speakers", "max_input_channels": 0},
                {"name": "Mic A", "max_input_channels": 1}]

    def query_hostapis(self, i):
        return {"devices": [0, 1]}

    def InputStream(self, samplerate, channels, dtype, blocksize, device, callback):
        sd = self

        class Stream:
            def __enter__(s):
                s.stop = False
                s.t = threading.Thread(target=s.run, daemon=True)
                s.t.start()
                return s

            def __exit__(s, *a):
                s.stop = True

            def run(s):
                blocks = []
                for secs, amp in sd.plan:
                    blocks += [amp] * int(secs * 20)
                rng = np.random.default_rng(1)
                i = 0
                while not s.stop:
                    amp = blocks[i] if i < len(blocks) else 0.0005
                    i += 1
                    data = (rng.standard_normal((blocksize, 1)) * amp).astype("float32")
                    callback(data, blocksize, None, None)
                    time.sleep(0.002)     # run faster than real time
        return Stream()


class FakeModel:
    def __init__(self):
        self.calls = []

    def transcribe(self, audio, **kw):
        self.calls.append((len(audio), kw))
        return [types.SimpleNamespace(text=" hello reo ")], None


def make(plan, tmp_path):
    events, model = [], FakeModel()
    st = S.SpeechToText(Settings(tmp_path / "s.json"), lambda **e: events.append(e), root=tmp_path / "speech",
                        sd=FakeSD(plan), model_factory=lambda *a: model)
    return st, events, model


def wait_final(events, t=10):
    end = time.time() + t
    while time.time() < end:
        f = [e for e in events if e["kind"] == "final"]
        if f:
            return f
        time.sleep(0.02)
    raise AssertionError("no final event: " + str(events[-3:]))


def test_microphones_deduplicated(tmp_path):
    st, _, _ = make([], tmp_path)
    assert st.list_microphones() == [{"id": "Mic A", "name": "Mic A"}]
    assert st.status()["ok"]


def test_one_utterance_one_final(tmp_path):
    st, ev, model = make([(1.0, 0.0005), (1.2, 0.2)], tmp_path)
    assert st.start("Mic A") == {"ok": True}
    f = wait_final(ev)
    assert [e["text"] for e in f] == ["hello reo"]
    kinds = [e["kind"] for e in ev]
    assert "speech" in kinds and "transcribing" in kinds and "level" in kinds
    assert len(model.calls) == 1
    time.sleep(0.2)
    assert len([e for e in ev if e["kind"] == "final"]) == 1


def test_short_click_is_ignored_then_real_speech(tmp_path):
    st, ev, model = make([(0.5, 0.0005), (0.15, 0.3), (1.5, 0.0005), (1.2, 0.2)], tmp_path)
    st.start("")
    wait_final(ev)
    assert len(model.calls) == 1
    assert model.calls[0][0] > 16000 * 0.9      # the speech, not the click


def test_stop_early_transcribes_and_cancel_discards(tmp_path):
    st, ev, model = make([(0.3, 0.0005), (30, 0.2)], tmp_path)
    st.start("")
    time.sleep(1.2)
    st.stop()
    assert wait_final(ev)[0]["text"] == "hello reo"
    st2, ev2, m2 = make([(0.3, 0.0005), (30, 0.2)], tmp_path)
    st2.start("")
    time.sleep(1.0)
    st2.cancel()
    assert wait_final(ev2)[0]["text"] == "" and not m2.calls


def test_silence_times_out_with_empty_final(tmp_path, monkeypatch):
    monkeypatch.setattr(S, "NO_SPEECH_BLOCKS", 40)
    st, ev, model = make([], tmp_path)
    st.start("")
    assert wait_final(ev)[0]["text"] == "" and not model.calls


def test_busy_and_restart(tmp_path):
    st, ev, _ = make([(0.3, 0.0005), (30, 0.2)], tmp_path)
    assert st.start("")["ok"]
    assert st.start("").get("code") == "audio-capture"
    st.cancel()
    wait_final(ev)
    time.sleep(0.1)
    assert st.start("")["ok"]
    st.cancel()


class FakeWin:
    def __init__(self, x=0, y=0):
        self.x, self.y, self.width, self.height, self.log = x, y, 1000, 800, []

    def show(self): self.log.append("show")
    def hide(self): self.log.append("hide")
    def minimize(self): self.log.append("min")
    def move(self, x, y): self.x, self.y = x, y
    def resize(self, w, h): self.width, self.height = w, h


class FakeNative:
    size = 220

    def __init__(self, **cb):
        self.cb, self.calls, self.pos = cb, [], (0, 0)

    def set_size(self, d): self.calls.append(("size", d)); self.size = round(220 * d / 150)
    def screen(self): return (0, 0, 1920, 1080)
    def position(self): return self.pos
    def show(self, x, y): self.pos = (x, y); self.calls.append(("show", x, y))
    def hide(self): self.calls.append("hide")
    def set_state(self, st): self.calls.append(("state", st))
    def set_level(self, v): self.calls.append(("level", v))
    def show_bubble(self, text): self.calls.append(("bubble", text))
    def hide_bubble(self): self.calls.append("hide_bubble")
    def hold_bubble(self): self.calls.append("hold")
    def set_bubble_timeout(self, s): self.calls.append(("timeout", s))


def make_bridge(monkeypatch):
    monkeypatch.setenv("REO_MOCK_ENGINE", "1")
    from reo.bridge import Bridge
    events = []
    b = Bridge(emit=events.append)
    holder = {}
    b._native_factory = lambda **cb: holder.setdefault("n", FakeNative(**cb))
    return b, events, holder


def test_native_orb_lifecycle(home, monkeypatch):
    b, events, holder = make_bridge(monkeypatch)
    assert b.set_orb_mode(True) is False                 # no chat window attached (browser dev mode): in-page orb
    main = FakeWin()
    b.attach_window(main)
    assert b.set_orb_mode(True) is True
    n = holder["n"]
    assert ("size", 150) in n.calls
    assert n.calls[-1] == ("show", (1920 - 220) // 2, (1080 - 220) // 2)      # middle of the screen until moved
    assert "hide" in main.log
    b.orb_state("listening")
    assert ("state", "listening") in n.calls
    b._emit_stt(kind="level", v=0.5)
    assert ("level", 0.5) in n.calls
    b.orb_popup("hello")
    assert ("bubble", "hello") in n.calls
    b._emit_tts(kind="start")
    b._emit_tts(kind="done")
    assert "hold" in n.calls and ("timeout", 1.5) in n.calls
    n.cb["on_moved"](300, 400)
    assert b._settings.get("orb_pos") == [300, 400]
    n.pos = (300, 400)
    n.cb["on_click"]()
    assert events[-1] == {"e": "orb", "kind": "click"}
    n.cb["on_exit"]()
    assert main.log[-1] == "show" and "hide" in n.calls and events[-1] == {"e": "orb", "kind": "exit"}
    assert b.set_orb_mode(True) is True and n.calls[-1] == ("show", 300, 400)


def test_orb_unavailable_falls_back(home, monkeypatch):
    b, _, _ = make_bridge(monkeypatch)
    b._native_factory = lambda **cb: None
    b.attach_window(FakeWin())
    assert b.set_orb_mode(True) is False and not b._orb_on


def test_chat_window_drag_and_resize(home, monkeypatch):
    b, _, _ = make_bridge(monkeypatch)
    w = FakeWin(100, 100)
    b.attach_window(w)
    assert b.window_drag_begin("move")
    b.window_drag_to(50, 20)
    assert (w.x, w.y, w.width, w.height) == (150, 120, 1000, 800)
    b.window_drag_end()
    b.window_drag_begin("bottomright")
    b.window_drag_to(100, 50)
    assert (w.width, w.height) == (1100, 850)
    b.window_drag_begin("topleft")
    b.window_drag_to(30, 30)
    assert (w.x, w.y, w.width, w.height) == (180, 150, 1070, 820)
    b.window_drag_begin("right")
    b.window_drag_to(-5000, 0)
    assert w.width == 420                                # never smaller than the minimum


def test_orb_art_and_bubble_geometry():
    from reo import orb_art as A
    art = A.OrbArt(__import__("pathlib").Path(__file__).resolve().parent.parent / "reo/ui/assets/reo.png", 1.0)
    for st in ("idle", "listening", "thinking"):
        im = art.frame(st, .5, .3)
        assert im.size == (220, 220) and im.getpixel((0, 0))[3] == 0          # corners stay see-through
        assert im.getpixel((110, 110))[3] == 255                              # the sphere is opaque
    img, ty = A.render_bubble("hello " * 200, "left", 1.0)
    assert img.height <= A.BUBBLE_MAX_H + 6 and 0 < ty < img.height
    # orb on the left of the screen: bubble to its right; orb on the right: to its left; always on screen
    side, x, y = A.bubble_geometry((100, 400, 220, 220), (300, 80), 40, (0, 0, 1920, 1080))
    assert side == "right" and x > 100 + 110
    side, x, y = A.bubble_geometry((1600, 400, 220, 220), (300, 80), 40, (0, 0, 1920, 1080))
    assert side == "left" and x + 300 < 1600 + 110 + 5
    side, x, y = A.bubble_geometry((100, 0, 220, 220), (300, 80), 40, (0, 0, 1920, 1080))
    assert y >= 4


def test_premultiply_and_speakable():
    from PIL import Image
    from reo.orb_native import premultiplied_bgra
    assert premultiplied_bgra(Image.new("RGBA", (1, 1), (200, 100, 0, 128))) == bytes([0, 50, 100, 128])
    from reo.tts import speakable
    t = speakable("# Title\n\nSome **bold** text with `code` and [a link](http://x.y).\n```py\nprint(1)\n```\n- item")
    assert "print" not in t and "**" not in t and "http" not in t and "bold" in t and "a link" in t


def test_orb_size_setting_applies_live_and_scales_art(home, monkeypatch):
    b, _, holder = make_bridge(monkeypatch)
    b.attach_window(FakeWin())
    b.set_orb_mode(True)
    n = holder["n"]
    b.save_settings({"orb_size": 220})
    assert ("size", 220) in n.calls
    b.save_settings({"orb_size": 10})
    assert ("size", 45) in n.calls                       # clamped to the new minimum
    b.save_settings({"orb_size": 9999})
    assert ("size", 260) in n.calls                      # clamped
    from reo import orb_art as A
    path = __import__("pathlib").Path(__file__).resolve().parent.parent / "reo/ui/assets/reo.png"
    big = A.OrbArt(path, 220 / 150)
    assert big.S == round(220 * 220 / 150) and big.D == 220
    px = big.frame("idle").getpixel((big.S // 2, big.S // 2 - big.D // 2 + 6))
    assert px[3] == 255


def test_orb_and_bubble_share_one_window_layout():
    from reo import orb_art as A
    screen = (0, 0, 1920, 1080)
    # orb left of centre: window grows to the right, the orb stays at its screen position
    side, canvas, off, boff, origin = A.compose_layout(220, (100, 400), (300, 80), 40, screen, 1.0, reach=81)
    assert side == "right" and off == (0, 0) and origin == (100, 400 + min(0, 400 + 110 - 40 - 400))
    assert canvas[0] > 220 and boff[0] > 110
    # the orb's screen position is origin + offset
    assert (origin[0] + off[0], origin[1] + off[1]) == (100, 400)
    # orb right of centre: the window grows to the left, so the orb sits at an offset inside it
    side, canvas, off, boff, origin = A.compose_layout(220, (1600, 400), (300, 80), 40, screen, 1.0, reach=81)
    assert side == "left" and off[0] > 0 and boff[0] == 0
    assert (origin[0] + off[0], origin[1] + off[1]) == (1600, 400)
    # a tall bubble extends the window upward while the orb does not move
    side, canvas, off, boff, origin = A.compose_layout(220, (100, 500), (300, 400), 200, screen, 1.0, reach=81)
    assert (origin[0] + off[0], origin[1] + off[1]) == (100, 500) and canvas[1] >= 400


def test_wake_word_extraction_and_awake_window(home, monkeypatch):
    from reo import wake
    assert wake.extract("Hey Reo, what time is it?") == (True, "what time is it")
    assert wake.extract("hello Reo open notepad") == (True, "open notepad")
    assert wake.extract("Reo") == (True, "")
    assert wake.extract("what is the weather") == (False, "")
    b, _, _ = make_bridge(monkeypatch)
    assert b.wake_check("nobody called me") == {"ok": False, "command": ""}
    assert b.wake_check("Hey Reo") == {"ok": True, "command": ""}                 # just the name: listen for the next sentence
    assert b.wake_check("turn it up") == {"ok": True, "command": "turn it up"}
    assert b.wake_check("and this one") == {"ok": False, "command": ""}           # the window closes after one sentence


def test_computer_actions_parse_and_keys():
    from reo import computer as C
    acts, text = C.parse_actions('Opening it. <action>{"do":"key","keys":"win"}</action><action>{"do":"type","text":"notepad"}</action>')
    assert [a["do"] for a in acts] == ["key", "type"] and text == "Opening it."
    assert C.parse_keys("ctrl+shift+t") == ([0x11, 0x10], ord("T"))
    assert C.parse_keys("alt+f4") == ([0x12], 0x73)
    import pytest
    with pytest.raises(ValueError):
        C.parse_keys("ctrl+banana")
    from PIL import Image
    img, hot = C.gold_arrow(56)
    assert img.size == (56, 56) and hot == (2, 2) and img.getpixel((8, 14))[3] == 255 and img.getpixel((50, 3))[3] == 0
    r, g, b, _ = img.getpixel((8, 14))
    assert r > g > b                                       # gold, not grey


class FakeComputer:
    def __init__(self):
        self.aborted = threading.Event()
        self.log, self.began, self.ended = [], 0, 0

    def screen_size(self): return (1920, 1080)
    def begin(self): self.began += 1; self.aborted.clear()
    def end(self): self.ended += 1

    def execute(self, a):
        from reo import computer as C
        if self.aborted.is_set():
            raise C.Stopped()
        self.log.append(a)
        if a.get("do") == "key" and a.get("keys") == "esc-user":
            self.aborted.set()                              # the user pressed Esc mid-task
        return "ok"


def _scripted_bridge(monkeypatch, replies):
    monkeypatch.setenv("REO_MOCK_ENGINE", "1")
    from reo.bridge import Bridge
    from reo.engines.mock import MockEngine
    seen = []

    class Scripted(MockEngine):
        def stream(self, history, system_prompt, params, deep, stop):
            seen.append((history[-1]["text"], system_prompt))
            yield {"t": "token", "text": replies.pop(0)}
            yield {"t": "end", "stats": {}}
    events = []
    b = Bridge(emit=events.append)
    b._local = Scripted()
    b._computer = FakeComputer()
    return b, events, seen


def _run_chat(b, events, text, opts):
    c = b.new_conversation()
    cid = c["id"] if isinstance(c, dict) and "id" in c else c
    b.chat(cid, text, opts)
    end = time.time() + 10
    while time.time() < end and not any(e.get("e") in ("end", "error") for e in events):
        time.sleep(0.02)
    return cid


def test_computer_control_loop_acts_then_answers(home, monkeypatch):
    replies = ['Opening Start. <action>{"do":"key","keys":"win"}</action>', 'Typing. <action>{"do":"type","text":"notepad"}</action>', "Done, Notepad is open."]
    b, events, seen = _scripted_bridge(monkeypatch, list(replies))
    _run_chat(b, events, "open notepad", {"computerControl": True})
    c = b._computer
    assert [a["do"] for a in c.log] == ["key", "type"] and c.began == 1 and c.ended == 1
    assert "Computer Control is ON" in seen[0][1] and "1920x1080" in seen[0][1]
    assert "REPORTING RESULTS" in seen[0][1]
    assert seen[1][0].startswith("[Computer results]") and "press win: ok" in seen[1][0]       # results are fed back
    end = [e for e in events if e.get("e") == "end"][-1]
    assert end["e"] == "end"
    msgs = b._store.get(next(e for e in events if e.get("e") == "start")["conv"])["messages"]
    assert msgs[-1]["role"] == "bot" and "Done, Notepad is open." in msgs[-1]["text"] and "<action>" not in msgs[-1]["text"]
    assert "*Computer: press win*" in msgs[-1]["text"]


def test_computer_control_off_ignores_actions(home, monkeypatch):
    b, events, seen = _scripted_bridge(monkeypatch, ['<action>{"do":"key","keys":"win"}</action>'])
    _run_chat(b, events, "hi", {"computerControl": False})
    assert b._computer.log == [] and "Computer Control" not in (seen[0][1] or "")


def test_escape_stops_computer_control(home, monkeypatch):
    replies = ['<action>{"do":"key","keys":"esc-user"}</action><action>{"do":"type","text":"never typed"}</action>', "should not be reached"]
    b, events, seen = _scripted_bridge(monkeypatch, list(replies))
    _run_chat(b, events, "open notepad and do things", {"computerControl": True})
    c = b._computer
    assert [a["do"] for a in c.log] == ["key"]               # the second action never ran
    assert len(seen) == 1 and c.ended == 1                   # and the model was not asked to continue
    msgs = b._store.get(next(e for e in events if e.get("e") == "start")["conv"])["messages"]
    assert "Stopped" in msgs[-1]["text"]
