"""Model files: discover installed GGUFs, download from Hugging Face (resumable), delete."""
from __future__ import annotations

import re
import threading
import time
from pathlib import Path
from typing import Callable

import httpx

from .config import Settings, models_dir, resource_dir
from .hardware import GB, load_catalog

HF = "https://huggingface.co"
_SAFE = re.compile(r"[^A-Za-z0-9._-]")


def catalog() -> list[dict]:
    return load_catalog(resource_dir() / "data" / "catalog.json")


def hf_url(repo: str, file: str) -> str:
    return f"{HF}/{repo}/resolve/main/{file}"


class ModelManager:
    def __init__(self, settings: Settings, folder: Path | None = None):
        self.settings = settings
        self.dir = folder or models_dir()
        self._cancel: dict[str, threading.Event] = {}
        self._lock = threading.Lock()

    # ---- discovery -----------------------------------------------------------
    def installed(self) -> list[dict]:
        out, seen = [], set()
        for p in sorted(self.dir.glob("*.gguf")):
            out.append(self._info(p))
            seen.add(str(p))
        for c in self.settings.get("custom_models") or []:
            p = Path(c.get("path", ""))
            if p.is_file() and str(p) not in seen:
                out.append({**self._info(p), "custom": True, "name": c.get("name") or p.stem})
        by_file = {m["file"]: m for m in catalog()}
        for m in out:
            hit = by_file.get(Path(m["path"]).name)
            if hit:
                m["catalog_id"] = hit["id"]
                m["thinking"] = hit.get("thinking", False)
                m["name"] = hit["name"]
        return out

    @staticmethod
    def _info(p: Path) -> dict:
        return {"path": str(p), "file": p.name, "name": p.stem, "size_gb": round(p.stat().st_size / GB, 2),
                "custom": False, "catalog_id": None, "thinking": "qwen3" in p.name.lower()}

    def register_custom(self, path: str) -> dict:
        p = Path(path)
        if not p.is_file() or p.suffix.lower() != ".gguf":
            raise ValueError("Pick a .gguf file")
        custom = [c for c in (self.settings.get("custom_models") or []) if c.get("path") != str(p)]
        custom.append({"id": _SAFE.sub("_", p.stem), "name": p.stem, "path": str(p)})
        self.settings.update({"custom_models": custom})
        return self._info(p)

    def delete(self, path: str) -> None:
        p = Path(path).resolve()
        if p.parent == self.dir.resolve() and p.suffix == ".gguf":
            p.unlink(missing_ok=True)
        else:   # custom model living elsewhere: forget it, never touch the user's file
            custom = [c for c in (self.settings.get("custom_models") or []) if Path(c["path"]).resolve() != p]
            self.settings.update({"custom_models": custom})

    # ---- download --------------------------------------------------------------
    def cancel(self, key: str) -> None:
        ev = self._cancel.get(key)
        if ev:
            ev.set()

    def download(self, key: str, repo: str, file: str, emit: Callable[[dict], None]) -> Path:
        """Blocking, resumable download into models/. Emits progress dicts. Raises on failure."""
        if "/" in file or "\\" in file or not file.lower().endswith(".gguf"):
            raise ValueError("Invalid file name")
        dest = self.dir / file
        part = dest.with_suffix(dest.suffix + ".part")
        if dest.exists():
            emit({"key": key, "state": "done", "done": dest.stat().st_size, "total": dest.stat().st_size})
            return dest
        cancel = threading.Event()
        with self._lock:
            if key in self._cancel and not self._cancel[key].is_set():
                raise RuntimeError("Already downloading")
            self._cancel[key] = cancel
        try:
            have = part.stat().st_size if part.exists() else 0
            headers = {"Range": f"bytes={have}-"} if have else {}
            with httpx.Client(follow_redirects=True, timeout=httpx.Timeout(30, read=60)) as c:
                with c.stream("GET", hf_url(repo, file), headers=headers) as r:
                    if r.status_code == 416:      # already complete
                        part.replace(dest)
                        emit({"key": key, "state": "done", "done": have, "total": have})
                        return dest
                    if r.status_code == 401 or r.status_code == 403:
                        raise RuntimeError("Hugging Face refused the download (gated model? sign-in required)")
                    r.raise_for_status()
                    resumed = r.status_code == 206
                    if not resumed:
                        have = 0
                    total = have + int(r.headers.get("content-length", 0))
                    done, t0, last = have, time.time(), 0.0
                    with open(part, "ab" if resumed else "wb") as f:
                        for chunk in r.iter_bytes(1 << 20):
                            if cancel.is_set():
                                emit({"key": key, "state": "cancelled", "done": done, "total": total})
                                return part
                            f.write(chunk)
                            done += len(chunk)
                            now = time.time()
                            if now - last > 0.25:
                                last = now
                                emit({"key": key, "state": "downloading", "done": done, "total": total,
                                      "speed": (done - have) / max(now - t0, 1e-3)})
            if total and part.stat().st_size != total:
                raise RuntimeError("Download incomplete, try again to resume")
            part.replace(dest)
            emit({"key": key, "state": "done", "done": done, "total": total})
            return dest
        finally:
            with self._lock:
                self._cancel.pop(key, None)
