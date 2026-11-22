"""Reminders and timers. "Remind me in 10 minutes to call Ana", "pon un temporizador de 5 minutos", "recuérdame a las 18:30 ...".

The app handles these itself (instant and exact) instead of asking the model: parse() understands the sentence, Reminders keeps
them in %APPDATA%\\Reo\\reminders.json and a small thread fires them when they are due."""
from __future__ import annotations

import json
import re
import threading
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable

_WORDS = {"a": 1, "an": 1, "one": 1, "un": 1, "una": 1, "uno": 1, "two": 2, "dos": 2, "three": 3, "tres": 3, "four": 4, "cuatro": 4, "five": 5, "cinco": 5,
          "six": 6, "seis": 6, "seven": 7, "siete": 7, "eight": 8, "ocho": 8, "nine": 9, "nueve": 9, "ten": 10, "diez": 10, "fifteen": 15, "quince": 15,
          "twenty": 20, "veinte": 20, "thirty": 30, "treinta": 30, "forty": 40, "cuarenta": 40, "sixty": 60, "sesenta": 60}
_NUM = r"(?:\d+(?:[.,]\d+)?|" + "|".join(sorted(_WORDS, key=len, reverse=True)) + r")"
_UNIT = r"(?:seconds?|secs?|segundos?|segs?|minutes?|mins?|minutos?|hours?|hrs?|horas?|h)"
_DUR = rf"(?:(?P<half>half an hour|media hora)|(?P<n>{_NUM})\s*(?P<u>{_UNIT})\b)"
_REMIND = r"(?:remind me|remember me|reminder|recu[eé]rdame|recuerdame|av[ií]same|avisame|d[ií]melo|dimelo)"
_TIMER = r"(?:(?:set|start|create|make|put|pon|ponme|pone|crea|inicia|programa)\s+(?:me\s+)?(?:a\s+|an\s+|un\s+|una\s+)?(?:timer|alarm|countdown|temporizador|cron[oó]metro|alarma))"
_CLOCK = r"(?:at|a las|a la|para las)\s+(?P<h>\d{1,2})(?:[:.h](?P<m>\d{2}))?\s*(?P<ap>am|pm|a\.m\.|p\.m\.)?"


def _secs(m: re.Match) -> float | None:
    if m.group("half"):
        return 1800.0
    raw = m.group("n").lower()
    n = _WORDS[raw] if raw in _WORDS else float(raw.replace(",", "."))
    u = m.group("u").lower()
    mult = 1 if u.startswith(("sec", "seg")) else 3600 if u.startswith(("h", "hr", "hou", "hor")) else 60
    return n * mult


def _clean(what: str) -> str:
    w = re.sub(r"^\s*(?:to|that|about|of|de|que|a|para|sobre)\s+", "", what.strip(" ,.;:!?"), flags=re.I)
    return w.strip(" ,.;:!?")[:200]


def parse(text: str, now: float | None = None) -> dict | None:
    """{'due': epoch, 'text': what to remind, 'timer': bool} or None when the sentence is not a reminder/timer request."""
    t = (text or "").strip()
    if not t or len(t) > 300 or "\n" in t:
        return None
    now = time.time() if now is None else now
    tm = re.search(_TIMER + r"\s*(?:for|of|de|para|por)?\s*" + _DUR, t, re.I)
    if tm:
        return {"due": now + _secs(tm), "text": "", "timer": True}
    if not re.search(_REMIND, t, re.I):
        return None
    m = re.search(_REMIND + r"\s*(?:(?:in|en|dentro de|after|tras)\s+)" + _DUR + r"\s*(?P<what>.*)$", t, re.I)
    if m:
        return {"due": now + _secs(m), "text": _clean(m.group("what")), "timer": False}
    m = re.search(_REMIND + r"\s*(?P<what>.*?)\s+(?:in|en|dentro de|after|tras)\s+" + _DUR + r"\s*[.!?]*$", t, re.I)
    if m:
        return {"due": now + _secs(m), "text": _clean(m.group("what")), "timer": False}
    m = re.search(_REMIND + r"\s*" + _CLOCK + r"\s*(?P<what>.*)$", t, re.I)
    if not m:
        m = re.search(_REMIND + r"\s*(?P<what>.*?)\s+" + _CLOCK + r"\s*[.!?]*$", t, re.I)
    if m:
        h, mi = int(m.group("h")), int(m.group("m") or 0)
        ap = (m.group("ap") or "").lower().replace(".", "")
        if ap == "pm" and h < 12:
            h += 12
        if ap == "am" and h == 12:
            h = 0
        if h > 23 or mi > 59:
            return None
        base = datetime.fromtimestamp(now)
        due = base.replace(hour=h, minute=mi, second=0, microsecond=0)
        if due.timestamp() <= now:
            due += timedelta(days=1)
        return {"due": due.timestamp(), "text": _clean(m.group("what")), "timer": False}
    return None


def describe_delay(secs: float, es: bool = False) -> str:
    secs = max(0, int(round(secs)))
    if secs < 90:
        n, u = secs, ("segundo" if es else "second")
    elif secs < 5400:
        n, u = round(secs / 60), ("minuto" if es else "minute")
    else:
        n, u = round(secs / 360) / 10, ("hora" if es else "hour")
        n = int(n) if n == int(n) else n
    plural = "" if n == 1 else "s"
    return f"{n} {u}{plural}"


class Reminders:
    def __init__(self, path: Path, on_due: Callable[[dict], None]):
        self.path = Path(path)
        self.on_due = on_due
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._items: list[dict] = self._load()

    def _load(self) -> list[dict]:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            return [r for r in raw if isinstance(r, dict) and "due" in r and "id" in r]
        except (OSError, ValueError):
            return []

    def _save(self) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._items, ensure_ascii=False), encoding="utf-8")
        tmp.replace(self.path)

    def add(self, due: float, text: str, cid: str = "", timer: bool = False) -> dict:
        r = {"id": uuid.uuid4().hex[:10], "due": float(due), "text": (text or "").strip()[:200], "cid": cid, "timer": bool(timer)}
        with self._lock:
            self._items.append(r)
            self._save()
        return r

    def list(self) -> list[dict]:
        with self._lock:
            return sorted((dict(r) for r in self._items), key=lambda r: r["due"])

    def pause(self, rid: str, now: float | None = None) -> dict | None:
        """Freeze a running timer or reminder; the time left is kept while it is paused."""
        now = time.time() if now is None else now
        with self._lock:
            for r in self._items:
                if r["id"] == rid and not r.get("paused"):
                    r["paused"], r["left"] = True, max(0.0, r["due"] - now)
                    self._save()
                    return dict(r)
        return None

    def resume(self, rid: str, now: float | None = None) -> dict | None:
        now = time.time() if now is None else now
        with self._lock:
            for r in self._items:
                if r["id"] == rid and r.get("paused"):
                    r["due"], r["paused"] = now + float(r.get("left", 0)), False
                    r.pop("left", None)
                    self._save()
                    return dict(r)
        return None

    def delete(self, rid: str) -> None:
        with self._lock:
            self._items = [r for r in self._items if r["id"] != rid]
            self._save()

    def clear(self) -> None:
        with self._lock:
            self._items = []
            self._save()

    def check(self, now: float | None = None) -> list[dict]:
        """Fire everything that is due (also anything that came due while Reo was closed). Returns what fired."""
        now = time.time() if now is None else now
        with self._lock:
            due = [r for r in self._items if r["due"] <= now and not r.get("paused")]
            if due:
                gone = {r["id"] for r in due}
                self._items = [r for r in self._items if r["id"] not in gone]
                self._save()
        for r in sorted(due, key=lambda r: r["due"]):
            try:
                self.on_due(r)
            except Exception:                                    # noqa: BLE001 - one bad callback must not stop the others
                pass
        return due

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()

        def loop():
            while not self._stop.wait(1.0):
                self.check()

        self._thread = threading.Thread(target=loop, daemon=True, name="reo-reminders")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()


# ---------------------------------------------------------------------------------------------- stopwatch
_SW = r"(?:stop\s?watch|cron[oó]metro)"


def parse_stopwatch(text: str) -> str | None:
    """'start' | 'stop' | 'read' | 'reset' for sentences about the stopwatch (EN/ES), else None. A cronometro WITH a duration is a timer
    and is handled by parse()."""
    t = (text or "").strip()
    if not t or len(t) > 120 or not re.search(_SW, t, re.I) or re.search(_DUR, t, re.I):
        return None
    if re.search(r"\b(reset|restart|clear|reinicia|reinicio|pon a cero|resetea|borra)\b", t, re.I):
        return "reset"
    if re.search(r"\b(stop|pause|end|finish|para|detén|deten|pausa|termina|apaga)\b", t, re.I):
        return "stop"
    if re.search(r"\b(how long|how much|what'?s|what is|check|read|show|cu[aá]nto|qu[eé] marca|dime|mira)\b", t, re.I):
        return "read"
    if re.search(r"\b(start|begin|run|go|resume|inicia|empieza|comienza|arranca|pon|ponme|activa|crea|continu[aá])\b", t, re.I):
        return "start"
    return None


def fmt_elapsed(secs: float, es: bool = False) -> str:
    secs = int(max(0, secs))
    h, m, s = secs // 3600, (secs % 3600) // 60, secs % 60
    names = ("hora", "minuto", "segundo") if es else ("hour", "minute", "second")
    out = [f"{n} {u}{'' if n == 1 else 's'}" for n, u in ((h, names[0]), (m, names[1]), (s, names[2])) if n]
    return " ".join(out) if out else (f"0 segundos" if es else "0 seconds")


class Stopwatch:
    """One stopwatch, kept across restarts. Time accumulates while it runs."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = threading.Lock()
        self.started: float | None = None
        self.elapsed = 0.0
        try:
            d = json.loads(self.path.read_text(encoding="utf-8"))
            self.started = d.get("started")
            self.elapsed = float(d.get("elapsed", 0))
        except (OSError, ValueError, TypeError):
            pass

    def _save(self) -> None:
        try:
            self.path.write_text(json.dumps({"started": self.started, "elapsed": self.elapsed}), encoding="utf-8")
        except OSError:
            pass

    def read(self, now: float | None = None) -> float:
        now = time.time() if now is None else now
        return self.elapsed + (now - self.started if self.started else 0.0)

    @property
    def running(self) -> bool:
        return self.started is not None

    def start(self, now: float | None = None) -> None:
        with self._lock:
            if self.started is None:
                self.started = time.time() if now is None else now
                self._save()

    def stop(self, now: float | None = None) -> float:
        with self._lock:
            self.elapsed = self.read(now)
            self.started = None
            self._save()
            return self.elapsed

    def reset(self) -> None:
        with self._lock:
            self.started, self.elapsed = None, 0.0
            self._save()

    def state(self, now: float | None = None) -> dict:
        return {"running": self.running, "elapsed": round(self.read(now), 1)}
