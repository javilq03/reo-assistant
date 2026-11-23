"""What the Bridge talks to: one place that owns the Gameplay thread, the kill switch and calibration.

Only one job (a run or a calibration) exists at a time. Every method returns JSON-friendly data; progress
goes out through emit(kind=..., ...) which the Bridge forwards to the page as {"e": "gameplay", ...}.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Callable

from . import learning
from .brain import Brain, BrainError
from .io import GameWindowError, crop_norm, wait_point
from .runner import GameplayRunner, RunConfig
from .store import GameStore, slug

log = logging.getLogger("reo.gameplay")


class GameplayManager:
    def __init__(self, emit: Callable[..., None], get_computer: Callable, get_ask: Callable, store: GameStore | None = None,
                 window_factory: Callable | None = None, sleep: Callable[[float], None] = time.sleep):
        self._emit_cb, self._get_computer, self._get_ask = emit, get_computer, get_ask
        self.store = store or GameStore()
        self._window_factory = window_factory
        self.sleep = sleep
        self._lock = threading.Lock()
        self._busy = ""                     # "run" | "calibrate" | ""
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._last = {"game": "", "boss": ""}
        self.point_timeout = 90.0            # how long a calibration waits for F8

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
                return "Gameplay is already " + ("running" if self._busy == "run" else "waiting for a calibration click") + "."
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

    # ------------------------------------------------------------------ calibration (point at it in the game, press F8)
    def calibrate(self, game: str, kind: str, name: str) -> dict:
        if kind not in ("target", "screen", "region"):
            return {"error": "Unknown calibration."}
        name = slug(name).replace("-", "_")
        if not self.store.exists(game) or not name:
            return {"error": "Give it a name first."}
        g = self.store.game(game)
        if not g["window_title"]:
            return {"error": "Set the game window title first."}
        try:
            window = self._window(g["window_title"])
        except GameWindowError as e:
            return {"error": str(e)}
        if not window.find():
            return {"error": f"I cannot find a window titled '{g['window_title']}'. Is the game open?"}
        busy = self._claim("calibrate")
        if busy:
            return {"error": busy}
        self._stop.clear()
        threading.Thread(target=self._calibrate, args=(window, slug(game), kind, name), daemon=True, name="reo-calibrate").start()
        return {"ok": True}

    def cancel(self) -> bool:
        self._stop.set()
        return True

    def _prompt(self, window, text: str) -> None:
        self.emit(kind="calibrate", phase="wait", text=text)
        notify = getattr(window, "notify", None)
        if notify:
            notify(text)

    def _point(self, window, text: str):
        """Prompt, then wait for F8 over the game. Raises GameWindowError with the reason when none arrives."""
        self._prompt(window, text)
        p = wait_point(window, self._stop, timeout=self.point_timeout, sleep=self.sleep)
        if p is not None:
            return p
        if self._stop.is_set() or window.aborted():
            raise GameWindowError("Cancelled.")
        raise GameWindowError("I did not see F8 with the pointer over the game. If the game runs as administrator, run Reo as "
                              "administrator too (Windows hides key presses and clicks aimed at it). Otherwise click Reo's "
                              "window first and press F8 while the pointer is over the game.")

    def _calibrate(self, window, game: str, kind: str, name: str) -> None:
        ok, err = False, ""
        arm, disarm = getattr(window, "arm_hotkey", None), getattr(window, "disarm_hotkey", None)
        try:
            window.begin()
            if arm and not arm():
                self.emit(kind="log", text="Another program is using F8, so I am watching the key directly. If F8 is not detected, close that program.")
            warn = getattr(window, "elevation_warning", lambda: "")()
            if warn:
                self.emit(kind="log", text=warn)
            window.focus()
            if kind == "target":
                p = self._point(window, f"Hover over '{name}' in the game and press F8. Esc cancels.")
                self.store.set_target(game, name, p)
            else:
                what = "a screen" if kind == "screen" else "a region"
                a = self._point(window, f"{name}: hover the TOP-LEFT corner of {what} to capture and press F8. Esc cancels.")
                b = self._point(window, f"{name}: now the BOTTOM-RIGHT corner and press F8.")
                x0, y0, x1, y1 = min(a[0], b[0]), min(a[1], b[1]), max(a[0], b[0]), max(a[1], b[1])
                if x1 - x0 < 0.01 or y1 - y0 < 0.01:
                    raise GameWindowError("That area is too small. Try again.")
                region = [x0, y0, x1 - x0, y1 - y0]
                if kind == "region":
                    self.store.set_region(game, name, region)
                else:
                    window.focus()                              # F8 may have been pressed while Reo had focus: bring the game back first
                    self.sleep(0.4)
                    if not window.is_foreground():
                        raise GameWindowError("I could not bring the game to the front to capture it. Try again.")
                    self.store.save_screen(game, name, crop_norm(window.capture(), region), region)
            ok = True
        except GameWindowError as e:
            err = str(e)
        except Exception as e:                                  # noqa: BLE001
            log.exception("calibration failed")
            err = f"Calibration failed: {e}"
        finally:
            try:
                if disarm:
                    disarm()
                window.end()
            finally:
                self._release()
        self.emit(kind="calibrate", phase="done" if ok else "failed", name=name, what=kind, error=err)
