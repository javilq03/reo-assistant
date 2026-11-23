"""Everything Reo knows about a game, on disk and human-editable.

    games/<game>/game.json              name, window title, last boss, limits
    games/<game>/coords.json            named click targets, normalized [x, y]
    games/<game>/screens.json           screen states: reference region, what to tap, whether it ends a fight
    games/<game>/regions.json           named regions (e.g. "hand") sent to the model at higher resolution
    games/<game>/ui_templates/<state>.png
    games/<game>/bosses/<boss>/profile.md          verified facts
    games/<game>/bosses/<boss>/hypotheses.md       untested ideas
    games/<game>/bosses/<boss>/playbook.json       current strategy rules (versioned)
    games/<game>/bosses/<boss>/playbook_history/v<n>.json
    games/<game>/bosses/<boss>/attempts.jsonl      one line per attempt
    games/<game>/bosses/<boss>/attempts/<n>/turn_<k>.jpg + turn_<k>.json
"""
from __future__ import annotations

import json
import os
import re
import shutil
import time
from pathlib import Path

from ..config import games_dir

DEFAULT_GAME = {
    "id": "sdsgc",
    "name": "The Seven Deadly Sins: Grand Cross",
    "window_title": "",
    "boss": "",
    "max_attempts": 10,
    "stop_on_win": True,
    "targets": ["card_slot_1", "card_slot_2", "card_slot_3", "card_slot_4", "ultimate", "confirm", "retry"],
}
DEFAULT_SCREENS = {
    "battle_ready": {"tap": None, "outcome": None},
    "victory": {"tap": "confirm", "outcome": "win"},
    "defeat": {"tap": "confirm", "outcome": "loss"},
    "retry_prompt": {"tap": "retry", "outcome": None},
    "stage_select": {"tap": None, "outcome": None},
}
KEEP_TURN_IMAGES = 10          # attempts whose screenshots are kept; older ones keep only the json


def slug(s: str) -> str:
    return re.sub(r"[^a-z0-9_-]+", "-", str(s or "").strip().lower()).strip("-")[:40]


def _read_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def _write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def new_playbook() -> dict:
    return {"version": 1, "parent": None, "same_as": [], "pending": False, "next_id": 1, "rules": [],
            "note": "initial", "created": time.time()}


class BossStore:
    def __init__(self, directory: Path):
        self.dir = directory
        self.dir.mkdir(parents=True, exist_ok=True)

    # ---- profile (verified facts) and hypotheses (untested ideas): one bullet per line
    def _lines(self, name: str) -> list[str]:
        try:
            raw = (self.dir / name).read_text(encoding="utf-8").splitlines()
        except OSError:
            return []
        return [ln[2:].strip() for ln in raw if ln.startswith("- ") and ln[2:].strip()]

    def _put_lines(self, name: str, title: str, items: list[str]) -> None:
        (self.dir / name).write_text(f"# {title}\n\n" + "\n".join(f"- {i}" for i in items) + "\n", encoding="utf-8")

    def profile(self) -> list[str]:
        return self._lines("profile.md")

    def add_facts(self, facts) -> int:
        have = self.profile()
        seen = {f.lower() for f in have}
        added = 0
        for f in facts or []:
            f = " ".join(str(f).split())[:240]
            if f and f.lower() not in seen:
                have.append(f)
                seen.add(f.lower())
                added += 1
        if added:
            self._put_lines("profile.md", "Boss profile (verified facts)", have)
        return added

    def hypotheses(self) -> list[str]:
        return self._lines("hypotheses.md")

    def add_hypotheses(self, items, cap: int = 15) -> int:
        have = self.hypotheses()
        seen = {h.lower() for h in have}
        added = 0
        for h in items or []:
            h = " ".join(str(h).split())[:240]
            if h and h.lower() not in seen:
                have.append(h)
                seen.add(h.lower())
                added += 1
        if added:
            self._put_lines("hypotheses.md", "Untested ideas", have[-cap:])
        return added

    # ---- playbook (versioned)
    def playbook(self) -> dict:
        pb = _read_json(self.dir / "playbook.json", None)
        if not isinstance(pb, dict) or "version" not in pb:
            pb = new_playbook()
            self.save_playbook(pb)
        return pb

    def save_playbook(self, pb: dict, history: bool = True) -> None:
        _write_json(self.dir / "playbook.json", pb)
        if history:
            _write_json(self.dir / "playbook_history" / f"v{pb['version']}.json", pb)

    def playbook_version(self, v) -> dict | None:
        if not v:
            return None
        return _read_json(self.dir / "playbook_history" / f"v{int(v)}.json", None)

    # ---- attempts
    def attempts(self) -> list[dict]:
        out = []
        try:
            for ln in (self.dir / "attempts.jsonl").read_text(encoding="utf-8").splitlines():
                try:
                    out.append(json.loads(ln))
                except ValueError:
                    continue
        except OSError:
            pass
        return out

    def add_attempt(self, rec: dict) -> None:
        with open(self.dir / "attempts.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    def next_attempt_no(self) -> int:
        return max((a.get("attempt", 0) for a in self.attempts()), default=0) + 1

    def log_turn(self, attempt: int, turn: int, jpg: bytes, data: dict) -> None:
        d = self.dir / "attempts" / str(attempt)
        d.mkdir(parents=True, exist_ok=True)
        (d / f"turn_{turn}.jpg").write_bytes(jpg)
        _write_json(d / f"turn_{turn}.json", data)

    def turn_logs(self, attempt: int) -> list[dict]:
        d = self.dir / "attempts" / str(attempt)
        rows = []
        for p in sorted(d.glob("turn_*.json"), key=lambda p: int(re.sub(r"\D", "", p.stem) or 0)):
            rows.append(_read_json(p, {}))
        return rows

    def prune_turn_images(self, keep: int = KEEP_TURN_IMAGES) -> None:
        root = self.dir / "attempts"
        nums = sorted((int(p.name) for p in root.glob("*") if p.name.isdigit()), reverse=True)
        for n in nums[keep:]:
            for jpg in (root / str(n)).glob("*.jpg"):
                try:
                    jpg.unlink()
                except OSError:
                    pass


class GameStore:
    def __init__(self, root: Path | None = None):
        self.root = root or games_dir()
        self.root.mkdir(parents=True, exist_ok=True)

    # ---- games
    def dir(self, game: str) -> Path:
        return self.root / slug(game)

    def exists(self, game: str) -> bool:
        return bool(slug(game)) and (self.dir(game) / "game.json").is_file()

    def list_games(self) -> list[dict]:
        out = []
        for p in sorted(self.root.glob("*/game.json")):
            g = _read_json(p, {})
            out.append({"id": p.parent.name, "name": g.get("name") or p.parent.name})
        return out

    def ensure_default(self) -> None:
        if not self.list_games():
            self.create_game(DEFAULT_GAME["name"], DEFAULT_GAME["id"])

    def create_game(self, name: str, gid: str = "") -> str:
        gid = slug(gid or name) or "game"
        base, n = gid, 2
        while self.exists(gid):
            gid, n = f"{base}-{n}", n + 1
        g = dict(DEFAULT_GAME, id=gid, name=(name or gid).strip()[:60])
        _write_json(self.dir(gid) / "game.json", g)
        _write_json(self.dir(gid) / "screens.json", DEFAULT_SCREENS)
        _write_json(self.dir(gid) / "coords.json", {})
        _write_json(self.dir(gid) / "regions.json", {})
        return gid

    def game(self, game: str) -> dict:
        g = dict(DEFAULT_GAME)
        g.update(_read_json(self.dir(game) / "game.json", {}))
        g["id"] = slug(game)
        return g

    def save_game(self, game: str, patch: dict) -> dict:
        g = self.game(game)
        for k in ("name", "window_title", "boss"):
            if k in patch:
                g[k] = str(patch[k] or "").strip()[:80]
        if "boss" in patch:
            g["boss"] = slug(patch["boss"])
        if "max_attempts" in patch:
            try:
                g["max_attempts"] = max(1, min(200, int(patch["max_attempts"])))
            except (TypeError, ValueError):
                pass
        if "stop_on_win" in patch:
            g["stop_on_win"] = bool(patch["stop_on_win"])
        _write_json(self.dir(game) / "game.json", g)
        return g

    # ---- targets (named click points)
    def coords(self, game: str) -> dict:
        return _read_json(self.dir(game) / "coords.json", {})

    def set_target(self, game: str, name: str, point) -> None:
        name = slug(name).replace("-", "_")
        if not name:
            raise ValueError("A target needs a name")
        c = self.coords(game)
        c[name] = [round(float(point[0]), 4), round(float(point[1]), 4)]
        _write_json(self.dir(game) / "coords.json", c)
        g = self.game(game)
        if name not in g["targets"]:
            g["targets"].append(name)
            _write_json(self.dir(game) / "game.json", g)

    def remove_target(self, game: str, name: str) -> None:
        c = self.coords(game)
        c.pop(name, None)
        _write_json(self.dir(game) / "coords.json", c)
        g = self.game(game)
        if name in g["targets"]:
            g["targets"].remove(name)
            _write_json(self.dir(game) / "game.json", g)

    # ---- screens (reference crops) and regions
    def screens(self, game: str) -> dict:
        return _read_json(self.dir(game) / "screens.json", {})

    def template_path(self, game: str, state: str) -> Path:
        return self.dir(game) / "ui_templates" / f"{slug(state).replace('-', '_')}.png"

    def save_screen(self, game: str, state: str, crop, region) -> None:
        state = slug(state).replace("-", "_")
        if not state:
            raise ValueError("A screen needs a name")
        path = self.template_path(game, state)
        path.parent.mkdir(parents=True, exist_ok=True)
        crop.convert("RGB").save(path, "PNG")
        s = self.screens(game)
        cfg = s.get(state) or {"tap": None, "outcome": None}
        cfg.update(template=path.name, region=[round(float(v), 4) for v in region])
        s[state] = cfg
        _write_json(self.dir(game) / "screens.json", s)

    def set_screen_cfg(self, game: str, state: str, patch: dict) -> None:
        s = self.screens(game)
        if state not in s:
            raise ValueError("Unknown screen")
        if "tap" in patch:
            s[state]["tap"] = patch["tap"] or None
        if "outcome" in patch:
            s[state]["outcome"] = patch["outcome"] if patch["outcome"] in ("win", "loss") else None
        _write_json(self.dir(game) / "screens.json", s)

    def remove_screen(self, game: str, state: str) -> None:
        s = self.screens(game)
        if state in s:
            s[state].pop("template", None)
            s[state].pop("region", None)
            _write_json(self.dir(game) / "screens.json", s)
        try:
            self.template_path(game, state).unlink()
        except OSError:
            pass

    def regions(self, game: str) -> dict:
        return _read_json(self.dir(game) / "regions.json", {})

    def set_region(self, game: str, name: str, region) -> None:
        name = slug(name).replace("-", "_")
        r = self.regions(game)
        r[name] = [round(float(v), 4) for v in region]
        _write_json(self.dir(game) / "regions.json", r)

    def remove_region(self, game: str, name: str) -> None:
        r = self.regions(game)
        r.pop(name, None)
        _write_json(self.dir(game) / "regions.json", r)

    # ---- bosses
    def boss(self, game: str, boss: str) -> BossStore:
        return BossStore(self.dir(game) / "bosses" / (slug(boss) or "default"))

    def bosses(self, game: str) -> list[str]:
        root = self.dir(game) / "bosses"
        return sorted(p.name for p in root.glob("*") if p.is_dir()) if root.is_dir() else []

    def delete_game(self, game: str) -> None:
        if self.exists(game):
            shutil.rmtree(self.dir(game), ignore_errors=True)

    # ---- what is still missing before a run can start
    def problems(self, game: str, boss: str) -> list[str]:
        g, c, s = self.game(game), self.coords(game), self.screens(game)
        out = []
        if not g["window_title"]:
            out.append("Set the game window title.")
        if not slug(boss):
            out.append("Enter a boss name.")
        if not any(k.startswith("card_slot") for k in c):
            out.append("Set at least one card_slot target.")
        ready = s.get("battle_ready") or {}
        if not ready.get("template"):
            out.append("Capture the 'battle_ready' screen.")
        if not any((cfg.get("outcome") and cfg.get("template")) for cfg in s.values()):
            out.append("Capture a 'victory' or 'defeat' screen so Reo can tell how a fight ended.")
        for st, cfg in s.items():
            tap = cfg.get("tap")
            if cfg.get("template") and tap and tap not in c:
                out.append(f"Screen '{st}' taps '{tap}', which has no position yet.")
        return out
