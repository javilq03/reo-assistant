"""What the Bridge talks to: one place that owns the Gameplay thread, the kill switch and calibration.

Only one job (a run) exists at a time. Every method returns JSON-friendly data; progress
goes out through emit(kind=..., ...) which the Bridge forwards to the page as {"e": "gameplay", ...}.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Callable

from . import learning
from .brain import Brain, BrainError
from .io import GameWindowError, crop_norm
from .runner import GameplayRunner, RunConfig
from .store import GameStore, slug

log = logging.getLogger("reo.gameplay")


class GameplayManager:
    def __init__(self, emit: Callable[..., None], get_computer: Callable, get_ask: Callable, store: GameStore | None = None,
                 window_factory: Callable | None = None, sleep: Callable[[float], None] = time.sleep,
                 focus_reo: Callable | None = None):
        self._emit_cb, self._get_computer, self._get_ask = emit, get_computer, get_ask
        self.store = store or GameStore()
        self._window_factory = window_factory
        self.sleep = sleep
        self._focus_reo = focus_reo
        self._snap = None
        self._lock = threading.Lock()
        self._busy = ""                     # "run" | ""
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._last = {"game": "", "boss": ""}

    # ------------------------------------------------------------------ plumbing
    def emit(self, **ev) -> None:
        try:
            self._emit_cb(**ev)
        except Exception:                                      # noqa: BLE001
            log.exception("gameplay emit failed")

    def busy(self) -> str:
        return self._busy

    def _window(self, title: str):
        if self._window_factory:
            return self._window_factory(title)
        pc = self._get_computer()
        if pc is None:
            raise GameWindowError("Gameplay needs Windows (it uses the same controls as Computer Control).")
        from .io import WinGameWindow
        return WinGameWindow(pc, title)

    def _claim(self, what: str) -> str:
        with self._lock:
            if self._busy:
                return "Gameplay is already " + ("running") + "."
            self._busy = what
        return ""

    def _release(self) -> None:
        with self._lock:
            self._busy = ""

    # ------------------------------------------------------------------ reading state for the panel
    def state(self, game: str = "") -> dict:
        self.store.ensure_default()
        games = self.store.list_games()
        ids = [g["id"] for g in games]
        gid = slug(game) if slug(game) in ids else (self._last["game"] if self._last["game"] in ids else ids[0])
        g = self.store.game(gid)
        screens = self.store.screens(gid)
        for st, cfg in screens.items():
            cfg["captured"] = bool(cfg.get("template") and self.store.template_path(gid, st).is_file())
        out = {"games": games, "game": g, "coords": self.store.coords(gid), "screens": screens,
               "regions": self.store.regions(gid), "bosses": self.store.bosses(gid), "busy": self._busy,
               "problems": self.store.problems(gid, g.get("boss", "")), "boss": self.boss_info(gid, g.get("boss", ""))}
        return out

    def boss_info(self, game: str, boss: str) -> dict:
        if not slug(boss) or not self.store.exists(game):
            return {"profile": [], "hypotheses": [], "rules": "", "version": None, "pending": False, "versions": [], "attempts": []}
        b = self.store.boss(game, boss)
        pb = b.playbook()
        return {"profile": b.profile(), "hypotheses": b.hypotheses(), "rules": "\n".join(r["text"] for r in pb["rules"]),
                "rule_status": [{"id": r["id"], "text": r["text"], "status": r["status"], "wins": r["wins"], "losses": r["losses"]}
                                for r in pb["rules"]],
                "version": pb["version"], "pending": bool(pb.get("pending")), "note": pb.get("note", ""),
                "versions": learning.summary(b), "attempts": b.attempts()[-12:]}

    # ------------------------------------------------------------------ editing the knowledge
    def new_game(self, name: str) -> dict:
        gid = self.store.create_game(name)
        self._last["game"] = gid
        return self.state(gid)

    def save_game(self, game: str, patch: dict) -> dict:
        if not self.store.exists(game):
            return {"error": "Unknown game."}
        self.store.save_game(game, patch or {})
        self._last["game"] = slug(game)
        return self.state(game)

    def delete_game(self, game: str) -> dict:
        if self._busy:
            return {"error": "Stop Gameplay first."}
        self.store.delete_game(game)
        return self.state()

    def set_screen_cfg(self, game: str, state: str, patch: dict) -> dict:
        try:
            self.store.set_screen_cfg(game, state, patch or {})
        except ValueError as e:
            return {"error": str(e)}
        return self.state(game)

    def remove(self, game: str, kind: str, name: str) -> dict:
        {"target": self.store.remove_target, "screen": self.store.remove_screen,
         "region": self.store.remove_region}.get(kind, lambda *_: None)(game, name)
        return self.state(game)

    def teach(self, game: str, boss: str, text: str) -> dict:
        text = " ".join(str(text or "").split())
        if not text:
            return {"error": "Nothing to save."}
        if not slug(boss):
            return {"error": "Enter a boss name first."}
        self.store.boss(game, boss).add_facts([text])
        return self.state(game)

    def set_rules(self, game: str, boss: str, text: str) -> dict:
        if not slug(boss):
            return {"error": "Enter a boss name first."}
        b = self.store.boss(game, boss)
        if b.playbook().get("pending"):
            return {"error": "The current playbook is being evaluated. Replacing it now would spoil the comparison; stop here or wait for it to finish."}
        learning.set_manual_rules(b, text)
        return self.state(game)

    # ------------------------------------------------------------------ run
    def start(self, game: str, boss: str, max_attempts: int = 10, stop_on_win: bool = True) -> dict:
        if not self.store.exists(game):
            return {"error": "Unknown game."}
        self.store.save_game(game, {"boss": boss, "max_attempts": max_attempts, "stop_on_win": stop_on_win})
        g = self.store.game(game)
        problems = self.store.problems(game, g["boss"])
        if problems:
            return {"error": "Not ready yet.", "problems": problems}
        try:
            ask = self._get_ask()
            window = self._window(g["window_title"])
        except (BrainError, GameWindowError) as e:
            return {"error": str(e)}
        if not window.find():
            return {"error": f"I cannot find a window titled '{g['window_title']}'. Is the game open?"}
        busy = self._claim("run")
        if busy:
            return {"error": busy}
        self._stop.clear()
        self._last = {"game": slug(game), "boss": g["boss"]}
        cfg = RunConfig(game=slug(game), boss=g["boss"], max_attempts=g["max_attempts"], stop_on_win=g["stop_on_win"])
        self._thread = threading.Thread(target=self._run, args=(window, cfg, ask), daemon=True, name="reo-gameplay")
        self._thread.start()
        return {"ok": True}

    def _run(self, window, cfg: RunConfig, ask) -> None:
        self.emit(kind="status", running=True, game=cfg.game, boss=cfg.boss)
        result: dict = {}
        try:
            window.begin()
            warn = getattr(window, "elevation_warning", lambda: "")()
            if warn:
                self.emit(kind="log", text=warn)
            result = GameplayRunner(self.store, window, Brain(ask), self.emit, self._stop, sleep=self.sleep).run(cfg)
        except Exception as e:                                  # noqa: BLE001
            log.exception("gameplay run crashed")
            result = {"stopped": f"Something went wrong: {e}", "finished": False}
        finally:
            try:
                window.end()
            finally:
                self._release()
        self.emit(kind="done", running=False, **result)

    def stop(self) -> bool:
        self._stop.set()
        return True

    # ------------------------------------------------------------------ calibration (click on a screenshot of the game)
    def snapshot(self, game: str) -> dict:
        """Photograph the game window and hand the picture to the panel, where the user clicks on it.
        No key press is involved, so nothing here depends on who owns the keyboard."""
        if not self.store.exists(game):
            return {"error": "Unknown game."}
        g = self.store.game(game)
        if not g["window_title"]:
            return {"error": "Set the game window title first."}
        if self._busy:
            return {"error": "Gameplay is already running."}
        try:
            window = self._window(g["window_title"])
            if not window.find():
                return {"error": f"I cannot find a window titled '{g['window_title']}'. Is the game open?"}
            window.focus()
            self.sleep(0.6)
            if not window.is_foreground():
                return {"error": "I could not bring the game to the front. Click on the game once, then try again."}
            img = window.capture()
        except GameWindowError as e:
            return {"error": str(e)}
        except Exception as e:                                  # noqa: BLE001
            log.exception("snapshot failed")
            return {"error": f"Could not take the screenshot: {e}"}
        finally:
            back = self._focus_reo
            if back:
                try:
                    back()
                except Exception:                               # noqa: BLE001
                    log.exception("could not bring Reo back")
        import base64
        from .io import encode
        self._snap = {"game": slug(game), "img": img}
        data = base64.b64encode(encode(img)).decode("ascii")
        out = {"ok": True, "image": "data:image/jpeg;base64," + data, "width": img.width, "height": img.height}
        warn = getattr(window, "elevation_warning", lambda: "")()
        if warn:
            out["warning"] = warn
        return out

    def save_pick(self, game: str, kind: str, name: str, pts: list) -> dict:
        """pts are normalized: one [x, y] for a target, [x, y, w, h] for a screen or region."""
        name = slug(name).replace("-", "_")
        if kind not in ("target", "screen", "region") or not name:
            return {"error": "Give it a name first."}
        if not self.store.exists(game):
            return {"error": "Unknown game."}
        try:
            v = [min(1.0, max(0.0, float(x))) for x in pts]
        except (TypeError, ValueError):
            return {"error": "That selection is not valid."}
        if kind == "target":
            if len(v) < 2:
                return {"error": "Click a spot on the picture."}
            self.store.set_target(game, name, (v[0], v[1]))
            return self.state(game)
        if len(v) != 4 or v[2] < 0.01 or v[3] < 0.01:
            return {"error": "Drag a rectangle that is a bit larger."}
        if kind == "region":
            self.store.set_region(game, name, v)
            return self.state(game)
        snap = self._snap
        if not snap or snap["game"] != slug(game):
            return {"error": "Take a new screenshot first."}
        self.store.save_screen(game, name, crop_norm(snap["img"], v), v)
        return self.state(game)
