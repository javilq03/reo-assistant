"""Installs Reo's neural speech engine (the `piper-tts` package plus onnxruntime) without pip.

Why: pip is not always usable (packaged Reo.exe has none, a Store Python can be read-only, a proxy can block it), and when the
install fails Reo used to be silent. This module downloads the prebuilt wheels straight from PyPI, checks their SHA-256,
unpacks them into %APPDATA%\\Reo\\engine\\py<version>\\ and puts that folder on sys.path. It works the same from source and
from the packaged exe. Pure standard library + httpx; every step reports byte progress through `emit`."""
from __future__ import annotations

import hashlib
import logging
import os
import re
import shutil
import sys
import sysconfig
import zipfile
from pathlib import Path
from typing import Callable

log = logging.getLogger("reo.engine")

PACKAGES = ("piper-tts", "onnxruntime", "pathvalidate")       # numpy is already part of Reo
PYPI_JSON = "https://pypi.org/pypi/{}/json"
_dll_handles: list = []


class EngineError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code, self.message = code, message


# ---------------------------------------------------------------- where it lives
def _py(py=None) -> tuple[int, int]:
    return tuple(py) if py else (sys.version_info.major, sys.version_info.minor)


def engine_dir(py=None) -> Path:
    from .config import app_dir
    m, n = _py(py)
    return app_dir() / "engine" / f"py{m}{n}"


def has_portable(py=None) -> bool:
    return (engine_dir(py) / "piper").is_dir()


def activate(py=None, first: bool = False) -> bool:
    """Make the downloaded engine importable. `first` puts it ahead of any (possibly broken) copy installed elsewhere."""
    d = engine_dir(py)
    if not (d / "piper").is_dir():
        return False
    s = str(d)
    if s in sys.path:
        sys.path.remove(s)
    sys.path.insert(0, s) if first else sys.path.append(s)
    if os.name == "nt" and hasattr(os, "add_dll_directory"):
        for sub in ("onnxruntime/capi", "piper", "onnxruntime"):
            p = d / sub
            if p.is_dir():
                try:
                    _dll_handles.append(os.add_dll_directory(str(p)))
                except OSError:
                    pass
    return True


# ---------------------------------------------------------------- choosing wheels (pip's job, in miniature)
def platform_tags(plat: str | None = None) -> set[str]:
    p = (plat or sysconfig.get_platform()).lower()
    if p == "win-amd64":
        return {"win_amd64"}
    if p == "win32":
        return {"win32"}
    if p == "win-arm64":
        return {"win_arm64"}
    if p.startswith("linux-"):
        arch = p.split("-", 1)[1]
        tags = {f"linux_{arch}", f"manylinux1_{arch}", f"manylinux2010_{arch}", f"manylinux2014_{arch}"}
        return tags | {f"manylinux_2_{i}_{arch}" for i in range(5, 45)}
    return {re.sub(r"[-.]", "_", p)}


def parse_wheel(filename: str):
    if not filename.endswith(".whl"):
        return None
    parts = filename[:-4].split("-")
    if len(parts) < 5:
        return None
    py, abi, plat = parts[-3:]
    return set(py.split(".")), set(abi.split(".")), set(plat.split("."))


def wheel_ok(filename: str, py=None, plats: set[str] | None = None) -> bool:
    t = parse_wheel(filename)
    if not t:
        return False
    pys, abis, plts = t
    major, minor = _py(py)
    plats = platform_tags() if plats is None else plats
    if not (plts & (plats | {"any"})):
        return False
    for tag in pys:
        m = re.fullmatch(r"(cp|py)(\d)(\d*)", tag)
        if not m or int(m.group(2)) != major:
            continue
        impl, mm = m.group(1), m.group(3)
        if impl == "py":
            if mm == "" or int(mm) <= minor:
                return True
        elif mm:
            if int(mm) == minor and f"cp{major}{minor}" in abis:
                return True
            if "abi3" in abis and int(mm) <= minor:
                return True
    return False


def python_ok(spec: str, py=None) -> bool:
    """Handles the simple `requires_python` forms used by the wheels we need (>=3.11, >=3.9,<3.14 ...)."""
    cur = _py(py)
    for part in (spec or "").split(","):
        m = re.fullmatch(r"\s*(>=|>|<=|<)\s*(\d+)(?:\.(\d+))?(?:\.\d+)?\s*", part)
        if not m:
            continue
        op, want = m.group(1), (int(m.group(2)), int(m.group(3) or 0))
        if (op == ">=" and cur < want) or (op == ">" and cur <= want) or (op == "<=" and cur > want) or (op == "<" and cur >= want):
            return False
    return True


def _vkey(v: str):
    return [int(x) for x in re.findall(r"\d+", v)]


def pick_wheel(meta: dict, py=None, plats: set[str] | None = None) -> dict | None:
    """Newest release that has a wheel this Python can use: {filename, url, sha256, size, version}."""
    rels = meta.get("releases") or {}
    for ver in sorted((v for v in rels if not re.search(r"[A-Za-z]", v)), key=_vkey, reverse=True)[:80]:
        for f in rels[ver]:
            if f.get("yanked") or f.get("packagetype") != "bdist_wheel":
                continue
            if python_ok(f.get("requires_python") or "", py) and wheel_ok(f.get("filename", ""), py, plats):
                return {"filename": f["filename"], "url": f["url"], "sha256": (f.get("digests") or {}).get("sha256", ""),
                        "size": int(f.get("size") or 0), "version": ver}
    return None


# ---------------------------------------------------------------- install
def _client():
    import httpx
    return httpx.Client(follow_redirects=True, timeout=httpx.Timeout(30.0, read=90.0),
                        headers={"User-Agent": "Reo/1.0", "Accept-Encoding": "identity"})


def _friendly(e: Exception) -> tuple[str, str]:
    if isinstance(e, EngineError):
        return e.code, e.message
    try:
        import httpx
        if isinstance(e, httpx.HTTPStatusError):
            return "http", f"pypi.org answered {e.response.status_code}. Try again in a moment."
        if isinstance(e, httpx.TransportError):
            return "network", "Could not reach pypi.org. Check your internet connection and try again."
    except Exception:                                              # pragma: no cover
        pass
    if isinstance(e, zipfile.BadZipFile):
        return "corrupt", "A downloaded file was damaged. Try again."
    if isinstance(e, OSError) and getattr(e, "errno", None) == 28:
        return "disk", "Not enough free disk space for the speech engine."
    return "failed", f"{type(e).__name__}: {e}"


def install(emit: Callable, client_factory: Callable | None = None, py=None, plats: set[str] | None = None) -> Path:
    """Download, verify and unpack the engine. `emit(state=..., message=..., done=..., total=..., speed=...)` reports progress.
    Raises EngineError (code + message for people). Returns the folder it was unpacked into."""
    import time
    major, minor = _py(py)
    final = engine_dir(py)
    work = final.parent / "_work"
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True, exist_ok=True)
    try:
        with (client_factory or _client)() as client:
            emit(state="installing", message="Looking up the speech engine…", done=0, total=0)
            plan = []
            for pkg in PACKAGES:
                r = client.get(PYPI_JSON.format(pkg))
                r.raise_for_status()
                w = pick_wheel(r.json(), py, plats)
                if not w:
                    raise EngineError("no_wheel", f"There is no prebuilt '{pkg}' for Python {major}.{minor} on this PC. "
                                                  "Python 3.10 to 3.12 (64-bit) is supported.")
                plan.append(w)
            total = sum(w["size"] for w in plan)
            done, last, t0, lastb = 0, 0.0, time.time(), 0
            for w in plan:
                part = work / (w["filename"] + ".part")
                h = hashlib.sha256()
                with client.stream("GET", w["url"]) as r:
                    r.raise_for_status()
                    with open(part, "wb") as fh:
                        for chunk in r.iter_bytes():
                            fh.write(chunk)
                            h.update(chunk)
                            done += len(chunk)
                            now = time.time()
                            if now - last >= 0.12:
                                emit(state="downloading", message="Downloading the speech engine…", done=done, total=max(total, done),
                                     speed=int((done - lastb) / max(now - last, 1e-3)) if last else 0)
                                last, lastb = now, done
                if w["sha256"] and h.hexdigest() != w["sha256"]:
                    raise EngineError("corrupt", f"{w['filename']} failed its checksum. Try again.")
                part.replace(work / w["filename"])
            emit(state="installing", message="Unpacking the speech engine…", done=total, total=total)
            stage = work / "stage"
            for w in plan:
                with zipfile.ZipFile(work / w["filename"]) as z:
                    z.extractall(stage)
            if not (stage / "piper").is_dir():
                raise EngineError("corrupt", "The downloaded engine is missing its files. Try again.")
            if final.exists():
                shutil.rmtree(final)
            final.parent.mkdir(parents=True, exist_ok=True)
            os.replace(stage, final)
    except EngineError:
        raise
    except Exception as e:                                         # noqa: BLE001 - every failure gets a sentence
        log.exception("engine install failed")
        code, msg = _friendly(e)
        raise EngineError(code, msg) from e
    finally:
        shutil.rmtree(work, ignore_errors=True)
    return final
