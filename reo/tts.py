"""Built-in text-to-speech for Reo.

Backends (tried in order for a chosen voice):
  1. Piper  – offline neural voices downloaded into voicebanks/<id>/ (needs the `piper-tts` package)
  2. SAPI   – Windows system voices (comtypes)

A Piper voice that cannot be used (engine missing, voice not downloaded, voice broken) falls back to the Windows voice, so Reo
still talks, and a `notice` event tells the UI why. Downloads never happen silently in the middle of a reply: the UI starts them
(`VoiceDownloader`), shows progress, and only a verified, loadable voice is moved into voicebanks/.

Public contract: Speaker(emit).speak(text) / .stop() / .available()
Speaker events: kind=start | done | status | notice | error   (notice/error carry `code` and `message`)
Download events (VoiceDownloader / install_engine): kind=download|engine, id, state, done, total, speed, message

Voice selection uses settings["voice"]:
  - "" or "system"          → default SAPI voice
  - "sapi:<name>"           → named Windows voice
  - "<piper-key>"           → e.g. "en_US-lessac-medium" (folder under voicebanks/)
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Callable

from . import engine_install

log = logging.getLogger("reo.tts")

# Popular Piper voices we surface by default (key → friendly name, approx MB). Sizes are only a hint; the real size comes from the server.
DEFAULT_CATALOG = {
    "en_US-lessac-medium":   {"name": "Lessac (US, medium)",   "lang": "en_US", "quality": "medium", "mb": 63},
    "en_US-amy-medium":      {"name": "Amy (US, medium)",      "lang": "en_US", "quality": "medium", "mb": 63},
    "en_US-ryan-medium":     {"name": "Ryan (US, medium)",     "lang": "en_US", "quality": "medium", "mb": 63},
    "en_US-joe-medium":      {"name": "Joe (US, medium)",      "lang": "en_US", "quality": "medium", "mb": 63},
    "en_US-kristin-medium":  {"name": "Kristin (US, medium)",  "lang": "en_US", "quality": "medium", "mb": 63},
    "en_GB-alan-medium":     {"name": "Alan (GB, medium)",     "lang": "en_GB", "quality": "medium", "mb": 63},
    "en_GB-alba-medium":     {"name": "Alba (GB, medium)",     "lang": "en_GB", "quality": "medium", "mb": 63},
    "en_US-lessac-high":     {"name": "Lessac (US, high)",     "lang": "en_US", "quality": "high",   "mb": 110},
    "en_US-amy-low":         {"name": "Amy (US, low)",         "lang": "en_US", "quality": "low",    "mb": 28},
    "es_ES-davefx-medium":   {"name": "Davefx (Spain, medium)", "lang": "es_ES", "quality": "medium", "mb": 63},
    "es_MX-ald-medium":      {"name": "Ald (Mexico, medium)",   "lang": "es_MX", "quality": "medium", "mb": 63},
    "es_MX-claude-high":     {"name": "Claude (Mexico, high)",  "lang": "es_MX", "quality": "high",   "mb": 110},
}

VOICES_JSON_URL = "https://huggingface.co/rhasspy/piper-voices/raw/main/voices.json"
HF_RESOLVE = "https://huggingface.co/rhasspy/piper-voices/resolve/main"
PIPER_REQUIREMENT = "piper-tts>=1.3"
_KEY_RE = re.compile(r"^[a-z]{2,3}_[A-Za-z0-9]{2,8}-[A-Za-z0-9_]+-[a-z_]+$")      # en_US-lessac-medium; also keeps paths/URLs safe


class SpeechError(RuntimeError):
    """A failure the UI can explain: `code` is machine-readable, the message is for people."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code, self.message = code, message


def speakable(text: str, limit: int = 1500) -> str:
    """Markdown → something worth reading aloud."""
    t = re.sub(r"```.*?```", " (code omitted) ", text, flags=re.S)
    t = re.sub(r"`([^`]*)`", r"\1", t)
    t = re.sub(r"!\[[^\]]*\]\([^)]*\)", " ", t)
    t = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", t)
    t = re.sub(r"https?://\S+", " link ", t)
    t = re.sub(r"^\s{0,3}#{1,6}\s*", "", t, flags=re.M)
    t = re.sub(r"^\s*[-*+]\s+", "", t, flags=re.M)
    t = re.sub(r"[*_~>|]+", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    if len(t) > limit:
        cut = t[:limit]
        k = max(cut.rfind(". "), cut.rfind("? "), cut.rfind("! "))
        t = cut[: k + 1] if k > limit // 2 else cut
    return t


# ---------------------------------------------------------------------------
# Engines and voices on disk
# ---------------------------------------------------------------------------

def _voicebanks_root() -> Path:
    from .config import voicebanks_dir
    return voicebanks_dir()


_piper_state: dict = {"ok": None, "error": ""}


def _try_import_piper() -> str:
    """'' when `piper` imports, otherwise the real reason (a missing package, or a broken DLL / onnxruntime)."""
    try:
        import importlib
        importlib.invalidate_caches()
        from piper import PiperVoice  # noqa: F401
        return ""
    except Exception as e:
        msg = f"{type(e).__name__}: {e}"
        if "DLL load failed" in msg:
            msg += " (install the Microsoft Visual C++ Redistributable: https://aka.ms/vs/17/release/vc_redist.x64.exe)"
        return msg


def piper_status(refresh: bool = False) -> tuple[bool, str]:
    """Whether the `piper` package can be imported, and if not, the real reason. Falls back to the engine Reo downloaded itself."""
    if _piper_state["ok"] is None or refresh:
        err = _try_import_piper()
        if err and engine_install.has_portable():
            for name in [m for m in sys.modules if m == "piper" or m.startswith(("piper.", "onnxruntime"))]:
                sys.modules.pop(name, None)                      # forget a half-imported or broken copy
            engine_install.activate(first=True)
            err = _try_import_piper()
        _piper_state.update(ok=not err, error=err)
    return bool(_piper_state["ok"]), str(_piper_state["error"])


def ps_available() -> bool:
    """The Windows speech synthesizer through PowerShell: needs no Python package, so Reo can always speak on Windows."""
    return os.name == "nt" and shutil.which("powershell") is not None


def sapi_available() -> bool:
    try:
        import comtypes.client  # noqa: F401
        return True
    except Exception:
        return ps_available()


def engine_info() -> dict:
    """What the UI needs to decide between 'download a voice', 'install the engine' and 'nothing works'."""
    ok, err = piper_status()
    frozen = bool(getattr(sys, "frozen", False))
    return {"piper": ok, "piper_error": err, "sapi": sapi_available(), "frozen": frozen, "can_install": not ok}


def voice_files(key: str) -> tuple[Path | None, Path | None]:
    """(model, config) of an installed voice, or (None, None). Both files must exist: a half-downloaded voice is not installed."""
    if not _KEY_RE.match(key or ""):
        return None, None
    d = _voicebanks_root() / key
    onnx = next((p for p in d.glob("*.onnx") if p.stat().st_size > 100_000), None) if d.is_dir() else None
    cfg = Path(str(onnx) + ".json") if onnx else None
    return (onnx, cfg) if onnx and cfg and cfg.is_file() else (None, None)


def voice_installed(key: str) -> bool:
    return voice_files(key)[0] is not None


def _voice_name(key: str) -> str:
    meta = DEFAULT_CATALOG.get(key, {})
    return meta.get("name") or key.replace("-", " ").replace("_", " ").title()


def list_installed_piper() -> list[dict]:
    """Return installed Piper voices found under voicebanks/."""
    root = _voicebanks_root()
    out = []
    if not root.is_dir():
        return out
    for d in sorted(root.iterdir()):
        if not d.is_dir() or not voice_installed(d.name):
            continue
        meta = DEFAULT_CATALOG.get(d.name, {})
        out.append({
            "id": d.name,
            "name": _voice_name(d.name),
            "backend": "piper",
            "installed": True,
            "path": str(d),
            "lang": meta.get("lang", ""),
            "quality": meta.get("quality", ""),
        })
    return out


def _list_ps_voices() -> list[dict]:
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command",
                            "[Console]::OutputEncoding=[Text.Encoding]::UTF8; Add-Type -AssemblyName System.Speech; "
                            "(New-Object System.Speech.Synthesis.SpeechSynthesizer).GetInstalledVoices() | "
                            "ForEach-Object { $_.VoiceInfo.Name }"],
                           capture_output=True, text=True, encoding="utf-8", timeout=15, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return [{"id": f"sapi:{n.strip()}", "name": n.strip(), "backend": "sapi", "installed": True} for n in r.stdout.splitlines() if n.strip()]
    except Exception:
        return []


def list_sapi_voices() -> list[dict]:
    """Windows system voices (empty on non-Windows)."""
    if os.name != "nt":
        return []
    try:
        import comtypes.client  # noqa: F401
    except Exception:
        return _list_ps_voices()
    try:
        import comtypes.client
        voice = comtypes.client.CreateObject("SAPI.SpVoice")
        tokens = voice.GetVoices()
        out = []
        for i in range(tokens.Count):
            tok = tokens.Item(i)
            name = tok.GetDescription()
            out.append({
                "id": f"sapi:{name}",
                "name": name,
                "backend": "sapi",
                "installed": True,
            })
        return out
    except Exception:
        return []


def list_available_voices(include_catalog: bool = True) -> list[dict]:
    """All voices the UI can show: system + installed Piper + catalog entries not yet downloaded (installed=False)."""
    voices = []
    voices.append({"id": "system", "name": "Windows default voice", "backend": "sapi", "installed": True})
    voices.extend(list_sapi_voices())
    installed = list_installed_piper()
    have = {v["id"] for v in installed}
    voices.extend(installed)
    if include_catalog:
        for key, meta in DEFAULT_CATALOG.items():
            if key not in have:
                voices.append({
                    "id": key,
                    "name": meta["name"],
                    "backend": "piper",
                    "installed": False,
                    "mb": meta.get("mb", 60),
                    "lang": meta.get("lang", ""),
                    "quality": meta.get("quality", ""),
                })
    return voices


# ---------------------------------------------------------------------------
# Downloading voices (and the engine) with real progress
# ---------------------------------------------------------------------------

class DownloadCancelled(Exception):
    pass


def _friendly(e: Exception) -> tuple[str, str]:
    """(code, message) for a download failure."""
    try:
        import httpx
    except Exception:                                            # pragma: no cover
        httpx = None
    if isinstance(e, SpeechError):
        return e.code, e.message
    if httpx is not None and isinstance(e, httpx.HTTPStatusError):
        if e.response.status_code == 404:
            return "not_found", "This voice was not found on the server."
        return "http", f"The server answered {e.response.status_code}. Try again in a moment."
    if httpx is not None and isinstance(e, (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout, httpx.RemoteProtocolError, httpx.ReadError)):
        return "network", "Could not reach huggingface.co. Check your internet connection and try again."
    if isinstance(e, OSError) and getattr(e, "errno", None) == 28:
        return "disk", "Not enough free disk space for this voice."
    return "failed", f"{type(e).__name__}: {e}"


class VoiceDownloader:
    """Downloads Piper voices in the background, one at a time, with byte-level progress.

    Files are written as *.part, checked (size, md5 when the catalog has one, config readable, and a real load test when Piper is
    present) and only then moved into voicebanks/<key>/, so a voice that shows as installed always works.
    `client_factory` and `loader` exist so tests can run it offline."""

    def __init__(self, emit: Callable, client_factory: Callable | None = None, loader: Callable | None = None,
                 prepare: Callable | None = None):
        self._emit = emit
        self._client_factory = client_factory
        self._loader = loader
        self._prepare = prepare                                 # runs first, inside the download (e.g. set up the speech engine)
        self._lock = threading.Lock()
        self._active: dict[str, threading.Event] = {}
        self._gate = threading.Lock()                           # one download at a time: they are big and share the connection

    # ---- control
    def active(self) -> list[str]:
        with self._lock:
            return list(self._active)

    def start(self, key: str) -> dict:
        key = str(key or "").strip()
        if not _KEY_RE.match(key):
            return {"ok": False, "code": "bad_voice", "error": f"'{key}' is not a Piper voice name."}
        if voice_installed(key):
            self._emit(kind="download", id=key, state="done", done=1, total=1, message="Already downloaded.")
            return {"ok": True, "started": False, "installed": True}
        with self._lock:
            if key in self._active:
                return {"ok": True, "started": False, "already": True}
            cancel = threading.Event()
            self._active[key] = cancel
        threading.Thread(target=self._work, args=(key, cancel), daemon=True, name=f"reo-voice-{key}").start()
        return {"ok": True, "started": True}

    def cancel(self, key: str) -> bool:
        with self._lock:
            ev = self._active.get(key)
        if ev:
            ev.set()
        return bool(ev)

    # ---- worker
    def _client(self):
        if self._client_factory:
            return self._client_factory()
        import httpx
        return httpx.Client(follow_redirects=True, timeout=httpx.Timeout(30.0, read=60.0), headers={"User-Agent": "Reo/1.0", "Accept-Encoding": "identity"})

    def _plan(self, client, key: str) -> list[dict]:
        """Files to fetch as [{rel, name, size, md5}], the model first, using the official catalog when it is reachable."""
        catalog: dict = {}
        cache = _voicebanks_root() / "voices.json"
        try:
            if cache.is_file() and time.time() - cache.stat().st_mtime < 7 * 86400:
                catalog = json.loads(cache.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            catalog = {}
        if key not in catalog:
            try:
                r = client.get(VOICES_JSON_URL)
                r.raise_for_status()
                catalog = r.json()
                cache.write_text(json.dumps(catalog), encoding="utf-8")
            except Exception as e:                                # the conventional layout below still works
                log.warning("could not fetch voices.json: %s", e)
        files = []
        entry = catalog.get(key) or {}
        for rel, meta in (entry.get("files") or {}).items():
            if rel.endswith((".onnx", ".onnx.json")):
                files.append({"rel": rel, "size": int(meta.get("size_bytes") or 0), "md5": str(meta.get("md5_digest") or "")})
        if not files:
            lang, name, quality = key.split("-", 2)
            base = f"{lang.split('_')[0]}/{lang}/{name}/{quality}"
            files = [{"rel": f"{base}/{key}.onnx.json", "size": 0, "md5": ""}, {"rel": f"{base}/{key}.onnx", "size": 0, "md5": ""}]
        for f in files:
            f["name"] = Path(f["rel"]).name
        return sorted(files, key=lambda f: not f["name"].endswith(".onnx"))       # the big model first, so the bar's total is right from the start

    def _work(self, key: str, cancel: threading.Event):
        folder = _voicebanks_root() / key
        parts: list[Path] = []
        try:
            self._emit(kind="download", id=key, state="queued", done=0, total=0, message="Waiting for another download to finish…")
            with self._gate:
                if cancel.is_set():
                    raise DownloadCancelled()
                self._emit(kind="download", id=key, state="starting", done=0, total=0, message="Connecting…")
                if self._prepare:
                    self._prepare(key, cancel)
                    if cancel.is_set():
                        raise DownloadCancelled()
                    self._emit(kind="download", id=key, state="starting", done=0, total=0, message="Connecting…")
                folder.mkdir(parents=True, exist_ok=True)
                with self._client() as client:
                    plan = self._plan(client, key)
                    known = sum(f["size"] for f in plan) if all(f["size"] for f in plan) else 0
                    done_before, last, t0, ema = 0, 0.0, time.time(), 0.0
                    lastb, lastt = 0, t0
                    for f in plan:
                        part = folder / (f["name"] + ".part")
                        parts.append(part)
                        url = f"{HF_RESOLVE}/{f['rel']}"
                        with client.stream("GET", url) as r:
                            r.raise_for_status()
                            length = int(r.headers.get("content-length") or 0) or f["size"]
                            if known == 0:
                                f["size"] = length
                            got = 0
                            with open(part, "wb") as fh:
                                for chunk in r.iter_bytes():
                                    if cancel.is_set():
                                        raise DownloadCancelled()
                                    fh.write(chunk)
                                    got += len(chunk)
                                    now = time.time()
                                    if now - last >= 0.12:
                                        inst = (done_before + got - lastb) / max(now - lastt, 1e-3)
                                        ema = inst if ema == 0 else 0.7 * ema + 0.3 * inst
                                        lastb, lastt = done_before + got, now
                                        total = known or max(done_before + length, done_before + got)
                                        self._emit(kind="download", id=key, state="downloading", done=done_before + got, total=total,
                                                   speed=int(ema), file=f["name"], message=f"Downloading {f['name']}")
                                        last = now
                        if f["size"] and part.stat().st_size != f["size"]:
                            raise SpeechError("corrupt", f"{f['name']} came down incomplete ({part.stat().st_size} of {f['size']} bytes). Try again.")
                        done_before += part.stat().st_size
                        self._emit(kind="download", id=key, state="downloading", done=done_before, total=known or done_before, speed=int(ema),
                                   file=f["name"], message=f"Downloaded {f['name']}")
                    total = done_before
                    self._emit(kind="download", id=key, state="verifying", done=total, total=total, message="Checking the voice…")
                    self._verify(plan, folder)
                    for f in sorted(plan, key=lambda f: f["name"].endswith(".onnx")):     # config first, model last: it only appears once both are in place
                        os.replace(folder / (f["name"] + ".part"), folder / f["name"])
                    self._load_test(key)
                self._emit(kind="download", id=key, state="done", done=total, total=total, message=f"{_voice_name(key)} is ready.")
        except DownloadCancelled:
            self._cleanup(folder, parts)
            self._emit(kind="download", id=key, state="cancelled", message="Download cancelled.")
        except Exception as e:                                    # noqa: BLE001 - every failure must reach the UI
            log.exception("voice download failed: %s", key)
            self._cleanup(folder, parts)
            code, msg = _friendly(e)
            self._emit(kind="download", id=key, state="error", code=code, message=msg)
        finally:
            with self._lock:
                self._active.pop(key, None)

    def _verify(self, plan: list[dict], folder: Path):
        for f in plan:
            part = folder / (f["name"] + ".part")
            if f["md5"]:
                h = hashlib.md5()
                with open(part, "rb") as fh:
                    for blk in iter(lambda: fh.read(1 << 20), b""):
                        h.update(blk)
                if h.hexdigest() != f["md5"]:
                    raise SpeechError("corrupt", f"{f['name']} failed its checksum. Try again.")
            if f["name"].endswith(".onnx.json"):
                try:
                    json.loads(part.read_text(encoding="utf-8"))
                except ValueError:
                    raise SpeechError("corrupt", "The voice's config file is not valid. Try again.") from None
            elif part.stat().st_size < 100_000:
                raise SpeechError("corrupt", "The voice model is too small to be real. Try again.")

    def _load_test(self, key: str):
        """If Piper is installed, make sure it can really load the voice; a voice that cannot load is removed and reported."""
        loader = self._loader
        if loader is None:
            if not piper_status()[0]:
                return                                            # nothing to test with yet; the engine will be installed next

            def loader(path):
                from piper import PiperVoice
                PiperVoice.load(path)
        onnx, _cfg = voice_files(key)
        try:
            loader(str(onnx))
        except Exception as e:                                    # noqa: BLE001
            for p in (_voicebanks_root() / key).glob("*"):
                p.unlink(missing_ok=True)
            raise SpeechError("corrupt", f"The voice downloaded but Piper cannot load it ({e}).") from e

    @staticmethod
    def _cleanup(folder: Path, parts: list[Path]):
        for p in parts:
            p.unlink(missing_ok=True)
        try:
            folder.rmdir()                                        # only if empty: an installed voice is never touched
        except OSError:
            pass


def install_engine(emit: Callable, client_factory: Callable | None = None, use_pip: bool = True) -> dict:
    """Get the Piper speech engine working. Source runs try pip first; if that is not possible or fails (and always in the packaged
    exe) the wheels are downloaded from PyPI into Reo's own folder. Never raises: returns {ok} or {ok: False, code, error}."""
    ok, err = piper_status(refresh=True)
    if ok:
        emit(kind="engine", id="piper", state="done", message="The speech engine is installed.")
        return {"ok": True}

    def fail(code: str, msg: str) -> dict:
        emit(kind="engine", id="piper", state="error", code=code, message=msg)
        return {"ok": False, "code": code, "error": msg}

    emit(kind="engine", id="piper", state="installing", message="Setting up the speech engine…", done=0, total=0)
    if use_pip and not getattr(sys, "frozen", False):
        import importlib.util
        if importlib.util.find_spec("pip") is not None:
            try:
                r = subprocess.run([sys.executable, "-m", "pip", "install", "--disable-pip-version-check", PIPER_REQUIREMENT],
                                   capture_output=True, text=True, timeout=900, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                if r.returncode == 0 and piper_status(refresh=True)[0]:
                    emit(kind="engine", id="piper", state="done", message="Speech engine installed.")
                    return {"ok": True}
                log.warning("pip could not install the speech engine: %s", (r.stderr or r.stdout or "").strip()[-400:])
            except Exception as e:                                # noqa: BLE001
                log.warning("pip install of the speech engine failed: %s", e)
    try:
        engine_install.install(lambda **ev: emit(kind="engine", id="piper", **ev), client_factory)
    except engine_install.EngineError as e:
        return fail(e.code, e.message)
    ok, err = piper_status(refresh=True)
    if not ok:
        return fail("broken", f"The speech engine was installed but will not start: {err}")
    emit(kind="engine", id="piper", state="done", message="Speech engine installed.")
    return {"ok": True}


# ---------------------------------------------------------------------------
# Backends
# ---------------------------------------------------------------------------

class _SapiBackend:
    def __init__(self):
        self._voice = None
        self._voice_id = None

    def available(self) -> bool:
        try:
            import comtypes.client  # noqa: F401
            return True
        except Exception:
            return False

    def _ensure(self, voice_id: str | None = None):
        import comtypes
        import comtypes.client
        comtypes.CoInitialize()
        if self._voice is None:
            self._voice = comtypes.client.CreateObject("SAPI.SpVoice")
        if voice_id and voice_id.startswith("sapi:") and voice_id != self._voice_id:
            name = voice_id[5:]
            tokens = self._voice.GetVoices()
            for i in range(tokens.Count):
                tok = tokens.Item(i)
                if tok.GetDescription() == name:
                    self._voice.Voice = tok
                    self._voice_id = voice_id
                    break

    def speak(self, text: str, cancel: threading.Event, voice_id: str | None = None):
        self._ensure(voice_id)
        self._voice.Speak(text, 1 | 2)  # async + purge
        while not self._voice.WaitUntilDone(80):
            if cancel.is_set():
                self._voice.Speak("", 2)
                break

    def stop(self):
        if self._voice is not None:
            try:
                self._voice.Speak("", 2)
            except Exception:
                pass


class _PsBackend:
    """Windows speech through a hidden PowerShell: the zero-dependency fallback (works without comtypes or Piper)."""
    _SCRIPT = ("[Console]::InputEncoding=[Text.Encoding]::UTF8; Add-Type -AssemblyName System.Speech; "
               "$s=New-Object System.Speech.Synthesis.SpeechSynthesizer; $v=$env:REO_VOICE; "
               "if($v){try{$s.SelectVoice($v)}catch{}}; $s.SetOutputToDefaultAudioDevice(); $s.Speak([Console]::In.ReadToEnd())")

    def __init__(self):
        self._proc = None

    def available(self) -> bool:
        return ps_available()

    def speak(self, text: str, cancel: threading.Event, voice_id: str | None = None):
        env = dict(os.environ, REO_VOICE=(voice_id[5:] if voice_id and voice_id.startswith("sapi:") else ""))
        self._proc = proc = subprocess.Popen(["powershell", "-NoProfile", "-NonInteractive", "-Command", self._SCRIPT], stdin=subprocess.PIPE,
                                             stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, env=env,
                                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        try:
            proc.stdin.write(text.encode("utf-8"))
            proc.stdin.close()
        except OSError:
            pass
        while proc.poll() is None:
            if cancel.is_set():
                proc.kill()
                return
            time.sleep(0.05)
        if proc.returncode:
            err = (proc.stderr.read() or b"").decode("utf-8", "replace").strip().splitlines()[-1:] or [""]
            raise SpeechError("no_engine", f"The Windows voice failed: {err[0] or 'PowerShell speech returned an error'}")

    def stop(self):
        p = self._proc
        if p is not None and p.poll() is None:
            try:
                p.kill()
            except Exception:
                pass


class _PiperBackend:
    def __init__(self):
        self._voices: dict[str, object] = {}  # key → PiperVoice
        self._lock = threading.Lock()

    def available(self) -> bool:
        return piper_status()[0]

    def forget(self, key: str):
        with self._lock:
            self._voices.pop(key, None)

    def _load(self, key: str):
        with self._lock:
            if key in self._voices:
                return self._voices[key]
            onnx, _cfg = voice_files(key)
            if onnx is None:
                raise SpeechError("voice_missing", f"The voice {_voice_name(key)} is not downloaded.")
            from piper import PiperVoice
            v = PiperVoice.load(str(onnx))
            self._voices[key] = v
            return v

    @staticmethod
    def _audio(voice, text: str, cancel: threading.Event):
        """(float32 samples, sample rate). Works with piper-tts 1.3+ (AudioChunk) and 1.2 (raw int16 stream)."""
        import numpy as np
        chunks, rate = [], 22050
        if hasattr(voice, "synthesize_stream_raw"):                       # piper-tts 1.2.x
            rate = int(getattr(getattr(voice, "config", None), "sample_rate", rate))
            for raw in voice.synthesize_stream_raw(text):
                if cancel.is_set():
                    return None, rate
                chunks.append(np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0)
        else:
            for chunk in voice.synthesize(text):
                if cancel.is_set():
                    return None, rate
                rate = int(chunk.sample_rate)
                chunks.append(np.asarray(chunk.audio_float_array, dtype=np.float32).reshape(-1))
        return (np.concatenate(chunks) if chunks else None), rate

    def speak(self, text: str, cancel: threading.Event, voice_id: str):
        try:
            import sounddevice as sd
        except Exception as e:
            raise SpeechError("no_audio", f"Audio output is not available ({e}).") from e
        voice = self._load(voice_id)
        audio, rate = self._audio(voice, text, cancel)
        if cancel.is_set() or audio is None or not len(audio):
            return
        sd.play(audio, rate)
        try:
            while sd.get_stream().active:
                if cancel.is_set():
                    sd.stop()
                    break
                time.sleep(0.05)
        finally:
            sd.wait()


# ---------------------------------------------------------------------------
# Public Speaker
# ---------------------------------------------------------------------------

class Speaker:
    def __init__(self, emit: Callable, settings=None, on_engine_missing: Callable | None = None):
        self._emit = emit
        self._settings = settings
        self._q: queue.Queue = queue.Queue()
        self._cancel = threading.Event()
        self._thread: threading.Thread | None = None
        self.error = ""
        self._sapi = _SapiBackend()
        self._ps = _PsBackend()
        self._piper = _PiperBackend()
        self._on_engine_missing = on_engine_missing               # lets the app repair the engine in the background
        self._told: set = set()                                   # (code, voice) notices already shown this session

    def available(self) -> bool:
        return self._sapi.available() or self._ps.available() or self._piper.available()

    def _system_available(self) -> bool:
        return self._sapi.available() or self._ps.available()

    def _voice_id(self) -> str:
        if self._settings is not None:
            return str(self._settings.get("voice") or "system")
        return "system"

    @staticmethod
    def _is_sapi(voice_id: str) -> bool:
        return voice_id.startswith("sapi:") or voice_id in ("", "system")

    def readiness(self, voice_id: str) -> dict:
        """Can this voice speak right now? {ok} or {ok: False, code, message}; codes: engine_missing | voice_missing | no_engine."""
        if self._is_sapi(voice_id):
            return {"ok": True} if self._system_available() else {"ok": False, "code": "no_engine", "message": "The Windows speech engine is not available."}
        ok, err = piper_status()
        if not ok:
            why = "" if "No module named" in err else f" ({err})"
            return {"ok": False, "code": "engine_missing", "message": "Neural voices need the speech engine, which is not set up yet" + why + "."}
        if not voice_installed(voice_id):
            return {"ok": False, "code": "voice_missing", "message": f"The voice {_voice_name(voice_id)} is not downloaded yet."}
        return {"ok": True}

    def speak(self, text: str, voice_id: str | None = None, strict: bool = False) -> dict:
        """Speak text. `voice_id` overrides the setting for this utterance only. With strict=True (previews) a voice that is not ready
        is reported instead of being replaced by the Windows voice."""
        t = speakable(text)
        if not t:
            self._emit(kind="done")
            return {"ok": True}
        vid = voice_id if voice_id else self._voice_id()
        if strict:
            ready = self.readiness(vid)
            if not ready["ok"]:
                return {"ok": False, "code": ready["code"], "error": ready["message"]}
        if self._thread is None or not self._thread.is_alive():
            self._thread = threading.Thread(target=self._run, daemon=True, name="reo-tts")
            self._thread.start()
        self._cancel.set()
        self._q.put((t, vid))
        return {"ok": True}

    def stop(self):
        self._cancel.set()
        try:
            while True:
                self._q.get_nowait()
        except queue.Empty:
            pass
        try:
            self._sapi.stop()
        except Exception:
            pass
        try:
            self._ps.stop()
        except Exception:
            pass
        try:
            import sounddevice as sd
            sd.stop()
        except Exception:
            pass
        return True

    def _tell(self, code: str, voice_id: str, message: str):
        if (code, voice_id) not in self._told:
            self._told.add((code, voice_id))
            self._emit(kind="notice", code=code, voice=voice_id, message=message)

    def _speak_sapi(self, text: str, voice_id: str | None):
        name = voice_id if voice_id and voice_id.startswith("sapi:") else None
        if self._sapi.available():
            try:
                return self._sapi.speak(text, self._cancel, name)
            except Exception as e:                                # noqa: BLE001 - fall through to the PowerShell voice
                log.warning("comtypes speech failed (%s); trying PowerShell", e)
        if self._ps.available():
            return self._ps.speak(text, self._cancel, name)
        raise SpeechError("no_engine", "No speech engine is available on this PC.")

    def _speak_one(self, text: str, voice_id: str):
        if self._is_sapi(voice_id):
            return self._speak_sapi(text, voice_id)
        ready = self.readiness(voice_id)
        if ready["ok"]:
            try:
                return self._piper.speak(text, self._cancel, voice_id)
            except SpeechError as e:
                code, msg = e.code, e.message
            except Exception as e:                                # noqa: BLE001 - a broken voice must not leave Reo silent
                log.exception("piper failed for %s", voice_id)
                self._piper.forget(voice_id)
                code, msg = "voice_failed", f"The voice {_voice_name(voice_id)} failed ({type(e).__name__}: {e})."
        else:
            code, msg = ready["code"], ready["message"]
            if code == "engine_missing" and self._on_engine_missing:
                try:
                    self._on_engine_missing()                    # repair it in the background; this reply uses the Windows voice
                except Exception:                                # noqa: BLE001
                    log.debug("engine repair hook failed", exc_info=True)
        if self._system_available():                              # fall back: Reo still talks, and the UI is told why
            self._tell(code, voice_id, msg + " Using the Windows voice instead.")
            return self._speak_sapi(text, None)
        raise SpeechError(code, msg)

    def _run(self):
        while True:
            text, voice_id = self._q.get()
            self._cancel.clear()
            self._emit(kind="start")
            try:
                self._speak_one(text, voice_id)
            except SpeechError as e:
                log.warning("speech failed: %s (%s)", e.message, e.code)
                self.error = e.message
                self._emit(kind="error", code=e.code, voice=voice_id, message=e.message)
            except Exception as e:                                # noqa: BLE001
                log.exception("speech failed")
                self.error = str(e)
                self._emit(kind="error", code="failed", voice=voice_id, message=str(e))
            if self._q.empty():
                self._emit(kind="done")


class MockSpeaker:
    """Test stand-in: 'speaks' for a moment, then reports done."""

    def __init__(self, emit):
        self._emit = emit
        self.spoken: list[str] = []
        self._cancel = threading.Event()

    def available(self):
        return True

    def readiness(self, voice_id):
        return {"ok": True}

    def speak(self, text, voice_id=None, strict=False):
        self.spoken.append(text)
        self._cancel.clear()

        def work():
            self._emit(kind="start")
            self._cancel.wait(0.3)
            self._emit(kind="done")
        threading.Thread(target=work, daemon=True).start()
        return {"ok": True}

    def stop(self):
        self._cancel.set()
        return True
