"""Bounded diagnostic log, modelled on Capri's BoundedDiagnosticLog: counters and stage timings only (never prompts,
replies, paths or keys), one JSON object per line, batched writes on a background thread, rotated at a size cap and
never allowed to interrupt the operation it describes."""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

MAX_BYTES = 512 * 1024
MAX_QUEUE = 128
_NUM = (int, float)
_ALLOWED = {                                   # field -> type; anything else is dropped
    "event": str, "engine": str, "ms_total": _NUM, "ms_first": _NUM, "ms_load": _NUM, "chars": _NUM, "thought_chars": _NUM,
    "deep": bool, "computer": bool, "web": bool, "ok": bool, "error": str, "stage": str, "state": str, "size": _NUM,
    "kind": str, "count": _NUM, "build": str, "frozen": bool,
}


def clean(value: dict) -> dict | None:
    """Keep only allowed, correctly typed fields; short strings only; stamp the time."""
    if not isinstance(value, dict) or not isinstance(value.get("event"), str):
        return None
    out = {"t": round(time.time(), 3)}
    for k, typ in _ALLOWED.items():
        v = value.get(k)
        if v is None or isinstance(v, bool) != (typ is bool) or not isinstance(v, typ):
            continue
        if isinstance(v, str):
            v = v[:60]
            if any(c in v for c in ("\\", "/", ":", "\n")):            # looks like a path, URL or prose: keep only the type name before it
                v = v.split(":")[0].split("\\")[-1].split("/")[-1][:40]
        elif isinstance(v, float):
            v = round(v, 1)
        out[k] = v
    return out


class DiagnosticLog:
    def __init__(self, file: Path, maximum_bytes: int = MAX_BYTES):
        self.file, self.maximum_bytes = Path(file), maximum_bytes
        self._queue: list[str] = []
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._closed = False
        self._thread = threading.Thread(target=self._run, daemon=True, name="reo-diaglog")
        self._thread.start()

    def write(self, **fields) -> None:
        rec = clean(fields)
        if not rec:
            return
        with self._lock:
            if len(self._queue) >= MAX_QUEUE:
                return
            self._queue.append(json.dumps(rec, separators=(",", ":")) + "\n")
        self._wake.set()

    def _run(self) -> None:
        while not self._closed:
            self._wake.wait(0.5)
            self._wake.clear()
            time.sleep(0.025)                                  # batch bursts
            self.flush()

    def flush(self) -> None:
        with self._lock:
            data, self._queue = "".join(self._queue), []
        if not data:
            return
        try:
            self.file.parent.mkdir(parents=True, exist_ok=True)
            if self.file.exists():
                if self.file.is_symlink() or not self.file.is_file():
                    return
                if self.file.stat().st_size + len(data.encode()) > self.maximum_bytes:
                    prev = self.file.with_name(self.file.name + ".1")
                    if prev.exists():
                        if prev.is_symlink() or not prev.is_file():
                            return
                        prev.unlink()
                    os.replace(self.file, prev)
            with open(self.file, "a", encoding="utf-8") as f:
                f.write(data)
        except OSError:
            pass                                                # diagnostics must never interrupt the caller

    def tail(self, n: int = 40) -> list[str]:
        self.flush()
        try:
            return self.file.read_text(encoding="utf-8", errors="replace").splitlines()[-n:]
        except OSError:
            return []

    def close(self) -> None:
        self._closed = True
        self._wake.set()
        self.flush()
