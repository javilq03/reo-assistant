"""The attempt loop: watch the screen, tap through everything boring without AI, call the model once per turn
that needs a decision, record every attempt, and learn from losses.

Every way out is a RunStop with a plain-language reason: the Stop button, Esc, the game window disappearing or
losing focus, a screen nobody recognizes, a turn that does not advance, a spend/time cap, or the model failing.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Callable

import numpy as np
from PIL import Image

from . import learning
from .brain import Brain, BrainError
from .io import GameWindowError, crop_norm, encode
from .screens import ScreenDetector
from .store import GameStore

log = logging.getLogger("reo.gameplay")


class RunStop(Exception):
    """A deliberate, explained end of the run."""


class Finished(RunStop):
    """The run reached its goal or its attempt limit (as opposed to being stopped or blocked)."""


@dataclass
class RunConfig:
    game: str
    boss: str
    max_attempts: int = 10
    stop_on_win: bool = True
    max_turns: int = 60            # per attempt
    max_decisions: int = 400       # model calls per run (the spend cap)
    max_minutes: float = 120.0
    settle: float = 1.5            # pause after acting, for the animation to start
    tap_gap: float = 0.4
    poll: float = 1.0
    ready_timeout: float = 8.0     # still on the same battle screen this long after acting = maybe stuck
    unknown_limit: float = 60.0    # an unrecognized (or idle) screen for this long ends the run


def _sig(img) -> np.ndarray:
    return np.asarray(img.convert("L").resize((32, 32), Image.BILINEAR), dtype=np.float32)


def _diff(img, sig) -> float:
    return float(np.abs(_sig(img) - sig).mean()) / 255.0


class GameplayRunner:
    def __init__(self, store: GameStore, window, brain: Brain, emit: Callable[..., None], stop,
                 sleep: Callable[[float], None] = time.sleep, now: Callable[[], float] = time.time):
        self.store, self.window, self.brain, self.emit, self.stop = store, window, brain, emit, stop
        self.sleep, self.now = sleep, now
        self.attempt: dict | None = None

    # ------------------------------------------------------------------ small helpers
    def say(self, text: str) -> None:
        self.emit(kind="log", text=text)

    def _check(self) -> None:
        if self.stop.is_set() or self.window.aborted():
            raise RunStop("Stopped.")

    def _sleep(self, secs: float) -> None:
        left = float(secs)
        while left > 0:
            self._check()
            step = min(0.25, left)
            self.sleep(step)
            left -= step
        self._check()

    def _ensure_window(self) -> None:
        if not self.window.valid() and not self.window.find():
            raise RunStop("I lost the game window.")
        if not self.window.is_foreground():
            for _ in range(3):
                self.window.focus()
                self._sleep(0.4)
                if self.window.is_foreground():
                    return
            raise RunStop("The game window is not in front, so I stopped before clicking something else.")

    def _capture(self):
        self._ensure_window()
        try:
            return self.window.capture()
        except GameWindowError as e:
            raise RunStop(str(e)) from e

    def _tap(self, target: str) -> None:
        pt = self.coords.get(target)
        if not pt:
            raise RunStop(f"'{target}' has no position yet. Set it in the Gameplay panel.")
        self._ensure_window()
        self.window.click(pt[0], pt[1])

    # ------------------------------------------------------------------ the run
    def run(self, cfg: RunConfig) -> dict:
        from ..computer import Stopped
        self.cfg = cfg
        self.summary = {"attempts": 0, "wins": 0, "losses": 0, "decisions": 0, "stopped": "", "finished": False}
        try:
            self._loop()
        except Finished as e:
            self.summary.update(stopped=str(e), finished=True)
        except RunStop as e:
            self.summary["stopped"] = str(e)
        except Stopped:
            self.summary["stopped"] = "Stopped."
        return self.summary

    def _loop(self) -> None:
        cfg = self.cfg
        self.boss = self.store.boss(cfg.game, cfg.boss)
        self.coords = self.store.coords(cfg.game)
        self.regions = self.store.regions(cfg.game)
        self.det = ScreenDetector(self.store, cfg.game)
        self.game = self.store.game(cfg.game)
        self.t0 = self.now()
        self.acted_at: float | None = None
        self.acted_sig = None
        self.stuck = 0
        self.idle_since: float | None = None
        self.last_state: str | None = None
        prev = self.boss.attempts()
        self.last_line = self._line(prev[-1]) if prev else ""
        self._ensure_window()
        self.say(f"Started: {self.game['name']}, boss '{cfg.boss}', up to {cfg.max_attempts} attempts. Press Esc to stop.")
        while True:
            self._check()
            if (self.now() - self.t0) / 60.0 > cfg.max_minutes:
                raise RunStop(f"Reached the {int(cfg.max_minutes)} minute limit.")
            img = self._capture()
            state, _score = self.det.classify(img)
            if state != self.last_state:
                self.last_state = state
                self.emit(kind="screen", state=state or "unknown")
            if state == "battle_ready":
                self.idle_since = None
                if self.attempt is None:
                    self._begin_attempt()
                self._take_turn(img)
                continue
            self.acted_at, self.stuck = None, 0
            if state is None:
                self._wait_idle(img, None)
                continue
            scfg = self.det.screens().get(state) or {}
            outcome, tap, done = scfg.get("outcome"), scfg.get("tap"), False
            if outcome and self.attempt is not None:
                self._end_attempt("win" if outcome == "win" else "loss")
                done = (outcome == "win" and cfg.stop_on_win) or self.summary["attempts"] >= cfg.max_attempts
            if tap:
                self.idle_since = None
                self._tap(tap)
                self._sleep(cfg.settle)
            else:
                self._wait_idle(img, state)
            if done:
                raise Finished("Won the fight." if outcome == "win" and cfg.stop_on_win
                               else f"Finished {self.summary['attempts']} attempts.")

    def _wait_idle(self, img, state: str | None) -> None:
        cfg = self.cfg
        if self.idle_since is None:
            self.idle_since = self.now()
        if self.now() - self.idle_since > cfg.unknown_limit:
            if state is None:
                p = self.store.dir(cfg.game) / f"unknown_{int(time.time())}.jpg"
                try:
                    p.write_bytes(encode(img))
                except OSError:
                    pass
                raise RunStop(f"I could not recognize this screen for {int(cfg.unknown_limit)} seconds. A screenshot was "
                              "saved in the game's folder. Capture it as a screen in the panel, or start a fight and press Start again.")
            raise RunStop(f"Stuck on '{state}'. Start the fight yourself and press Start again.")
        self._sleep(cfg.poll)

    # ------------------------------------------------------------------ attempts and turns
    def _begin_attempt(self) -> None:
        pb = self.boss.playbook()
        n = self.boss.next_attempt_no()
        self.attempt = {"n": n, "turn": 0, "hp": None, "log": [], "fails": 0, "version": pb["version"]}
        self.emit(kind="attempt_start", attempt=n, version=pb["version"])
        self.say(f"Attempt {n} started (playbook v{pb['version']}).")

    def _take_turn(self, img) -> None:
        cfg, a = self.cfg, self.attempt
        if self.acted_at is not None:
            if self.now() - self.acted_at < cfg.ready_timeout:
                self._sleep(0.5)
                return
            if _diff(img, self.acted_sig) < 0.01:
                self.stuck += 1
                if self.stuck >= 3:
                    raise RunStop("The turn did not advance after 3 tries. Check that the card positions are right.")
            else:
                self.stuck = 0
        if a["turn"] >= cfg.max_turns:
            self._end_attempt("timeout")
            raise RunStop(f"An attempt went past {cfg.max_turns} turns, so I stopped.")
        if self.summary["decisions"] >= cfg.max_decisions:
            raise RunStop(f"Reached the cap of {cfg.max_decisions} model calls for one run.")
        crops = [(name, encode(crop_norm(img, r), 1024)) for name, r in self.regions.items()]
        ctx = {"game": self.game["name"], "boss": cfg.boss, "attempt": a["n"], "turn": a["turn"] + 1,
               "targets": sorted(self.coords), "profile": self.boss.profile(),
               "playbook": learning.render(self.boss.playbook()), "recent": a["log"][-3:],
               "last_attempt": self.last_line, "crop_names": [n for n, _ in crops],
               "images": [encode(img)] + [b for _, b in crops]}
        try:
            dec = self.brain.decide(ctx)
        except BrainError as e:
            a["fails"] += 1
            self.say(f"The model gave no usable move ({e}).")
            if a["fails"] >= 3:
                raise RunStop(f"The model keeps failing: {e}") from e
            self.acted_at, self.acted_sig = self.now(), _sig(img)
            self._sleep(2)
            return
        self.summary["decisions"] += 1
        a["turn"] += 1
        hp = dec.state.get("boss_hp_pct")
        if isinstance(hp, (int, float)) and not isinstance(hp, bool):
            a["hp"] = max(0.0, min(100.0, float(hp)))
        moves = ", ".join(f"{x['type']} {x.get('target', x.get('seconds', ''))}" for x in dec.actions)
        a["log"].append(f"turn {a['turn']}: boss {a['hp'] if a['hp'] is not None else '?'}%, "
                        f"telegraph '{dec.state.get('boss_telegraph') or ''}', did [{moves}] because {dec.reason}")
        self.boss.log_turn(a["n"], a["turn"], encode(img),
                           {"turn": a["turn"], "state": dec.state, "actions": dec.actions, "reason": dec.reason})
        self.emit(kind="turn", attempt=a["n"], turn=a["turn"], hp=a["hp"], reason=dec.reason)
        for act in dec.actions:
            self._check()
            if act["type"] == "tap":
                self._tap(act["target"])
                self._sleep(cfg.tap_gap)
            else:
                self._sleep(act["seconds"])
        self.acted_at, self.acted_sig = self.now(), _sig(img)
        self._sleep(cfg.settle)

    @staticmethod
    def _line(rec: dict) -> str:
        hp = rec.get("boss_hp_left")
        return (f"attempt {rec['attempt']} was a {rec['outcome']} on turn {rec['turns']}"
                + (f", boss at {int(hp)}%" if hp is not None and rec["outcome"] != "win" else ""))

    def _end_attempt(self, outcome: str) -> None:
        a, self.attempt = self.attempt, None
        won = outcome == "win"
        rec = {"attempt": a["n"], "ts": time.time(), "outcome": outcome, "turns": a["turn"],
               "boss_hp_left": 0 if won else a["hp"], "playbook_version": a["version"]}
        self.boss.add_attempt(rec)
        self.boss.prune_turn_images()
        self.summary["attempts"] += 1
        self.summary["wins" if won else "losses"] += 1
        learning.update_evidence(self.boss, won)
        verdict = learning.evaluate(self.boss)
        msg = f"Attempt {a['n']} {'won' if won else 'lost'} on turn {a['turn']}"
        if not won and a["hp"] is not None:
            msg += f", boss at {int(a['hp'])}%"
        msg += "."
        if verdict == "rolled_back":
            msg += " The last playbook change did worse, so I went back to the previous version."
        elif verdict == "kept":
            msg += " The last playbook change held up and is now a permanent rule."
        if not won and outcome == "loss":
            msg += self._reflect(a, outcome)
        self.last_line = self._line(rec)
        self.emit(kind="attempt", message=msg, **rec)
        self.say(msg)

    def _reflect(self, a: dict, outcome: str) -> str:
        pb = self.boss.playbook()
        ctx = {"game": self.game["name"], "boss": self.cfg.boss, "outcome": outcome, "turns": a["log"],
               "boss_hp_left": a["hp"], "playbook": learning.render(pb), "profile": self.boss.profile(),
               "hypotheses": self.boss.hypotheses(), "can_change": learning.can_change(pb)}
        try:
            r = self.brain.reflect(ctx)
        except Exception as e:                                  # noqa: BLE001  a failed reflection must not end the run
            log.warning("reflection failed: %s", e)
            return " (Could not reflect on this loss.)"
        self.boss.add_facts(r["facts"])
        self.boss.add_hypotheses(r["hypotheses"])
        v = learning.apply_changes(self.boss, r["changes"], note=r["cause"]) if r["changes"] else None
        out = f" Cause: {r['cause']}" if r["cause"] else ""
        if v:
            first = r["changes"][0]
            out += f" Next I will test: {first.get('text') or 'removing ' + str(first.get('id'))} (playbook v{v})."
        return out
