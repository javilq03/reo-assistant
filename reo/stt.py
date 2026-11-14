"""Built-in speech-to-text: microphone capture (sounddevice) -> utterance detection -> Whisper (faster-whisper).

Fully local. The first use downloads the chosen Whisper model once into  <Reo folder>\\models\\speech.
One call to start() listens for ONE utterance, then emits {"kind": "final", "text": ...} exactly once.
Events go out through emit(**event):  level | speech | transcribing | status | error | final
"""
from __future__ import annotations

import os
import queue
import threading
import time
from collections import deque
from pathlib import Path
from typing import Callable

SR = 16000
BLOCK = 800                      # 50 ms
PRE_BLOCKS = 8                   # 0.4 s kept from before the voice starts
END_SILENCE_BLOCKS = 18          # 0.9 s of quiet ends the utterance
MIN_VOICED_BLOCKS = 6            # shorter than 0.3 s of voice is treated as a click/cough
MAX_BLOCKS = 600                 # 30 s cap per utterance
NO_SPEECH_BLOCKS = 300           # 15 s of nothing: report an empty result so hands-free can re-arm
MODEL_MB = {"tiny": 75, "base": 145, "small": 480, "medium": 1500}


class SpeechToText:
    def __init__(self, settings, emit: Callable[..., None], root: Path | None = None, sd=None, model_factory=None):
        self.settings = settings
        self._emit = emit
        self._root = root
        self._sd = sd
        self._factory = model_factory
        self._model = None
        self._model_key = ""
        self._model_lock = threading.Lock()
        self._busy = threading.Lock()
        self._cancel = threading.Event()
        self._finish = threading.Event()

    # ------------------------------------------------------------------ environment
    @property
    def root(self) -> Path:
        if self._root is None:
            from .config import models_dir
            self._root = models_dir() / "speech"
        self._root.mkdir(parents=True, exist_ok=True)
        return self._root

    def _sounddevice(self):
        if self._sd is None:
            import sounddevice  # raises ImportError / OSError (PortAudio missing)
            self._sd = sounddevice
        return self._sd

    def _size(self) -> str:
        s = str(self.settings.get("stt_model") or "base")
        return s if s in MODEL_MB else "base"

    def _cached(self) -> bool:
        return any(self.root.glob(f"models--*faster-whisper-{self._size()}*"))

    def status(self) -> dict:
        reason = ""
        if self._factory is None:
            try:
                import faster_whisper  # noqa: F401
            except Exception as e:
                reason = f"The speech package is not installed ({e}). Restart Reo to install it."
        if not reason:
            try:
                if not self.list_microphones():
                    reason = "No microphone was found."
            except Exception as e:
                reason = f"Audio input is not available ({e})."
        return {"ok": not reason, "reason": reason, "model": self._size(), "cached": self._cached(),
                "download_mb": MODEL_MB[self._size()]}

    def list_microphones(self) -> list[dict]:
        sd = self._sounddevice()
        devs = sd.query_devices()
        try:                      # Windows lists every device once per audio API; keep only the default API
            api = sd.query_hostapis(sd.default.hostapi)
            allowed = set(api["devices"])
        except Exception:
            allowed = set(range(len(devs)))
        out, seen = [], set()
        for i, d in enumerate(devs):
            if d["max_input_channels"] > 0 and i in allowed and d["name"] not in seen:
                seen.add(d["name"])
                out.append({"id": d["name"], "name": d["name"]})
        return out

    def _device_index(self, name: str):
        if not name:
            return None
        sd = self._sounddevice()
        for i, d in enumerate(sd.query_devices()):
            if d["max_input_channels"] > 0 and d["name"] == name:
                return i
        return None               # device unplugged: fall back to the default microphone

    # ------------------------------------------------------------------ model
    def _ensure_model(self):
        size = self._size()
        with self._model_lock:
            if self._model is not None and self._model_key == size:
                return self._model
            if self._cached():
                self._emit(kind="status", text="Loading the speech model...")
            else:
                self._emit(kind="status", text=f"Downloading the speech model (first time only, about {MODEL_MB[size]} MB)...")
            if self._factory:
                model = self._factory(size, str(self.root), self._threads())
            else:
                os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
                from faster_whisper import WhisperModel
                model = WhisperModel(size, device="cpu", compute_type="int8", cpu_threads=self._threads(),
                                     download_root=str(self.root))
            self._model, self._model_key = model, size
            self._emit(kind="status", text="Speech model ready.")
            return model

    def _threads(self) -> int:
        n = int(self.settings.get("n_threads") or 0)
        return n or max(2, min(8, (os.cpu_count() or 4) // 2))

    def prepare(self) -> dict:
        """Download/load the model in the background (Settings > Speech)."""
        def work():
            try:
                self._ensure_model()
            except Exception as e:
                self._emit(kind="error", code="model", message=f"Could not load the speech model: {e}")
        threading.Thread(target=work, daemon=True, name="reo-stt-prepare").start()
        return {"ok": True}

    # ------------------------------------------------------------------ one utterance
    def start(self, device: str = "") -> dict:
        if not self._busy.acquire(blocking=False):
            return {"error": "Already listening", "code": "audio-capture"}
        self._cancel.clear()
        self._finish.clear()
        threading.Thread(target=self._run, args=(device,), daemon=True, name="reo-stt").start()
        return {"ok": True}

    def stop(self):
        """Finish now: transcribe what was heard so far."""
        self._finish.set()
        return True

    def cancel(self):
        self._cancel.set()
        return True

    def _run(self, device: str):
        text = ""
        try:
            model = self._ensure_model()
            audio = self._capture(device)
            if audio is not None and not self._cancel.is_set():
                self._emit(kind="transcribing")
                text = self._transcribe(model, audio)
        except Exception as e:
            code = "audio-capture"
            self._emit(kind="error", code=code, message=f"Speech input failed: {e}")
        finally:
            self._busy.release()
            self._emit(kind="final", text="" if self._cancel.is_set() else text)

    def _capture(self, device: str):
        import numpy as np
        sd = self._sounddevice()
        q: queue.Queue = queue.Queue()

        def cb(indata, frames, t, status):
            q.put(indata[:, 0].copy())

        with sd.InputStream(samplerate=SR, channels=1, dtype="float32", blocksize=BLOCK,
                            device=self._device_index(device), callback=cb):
            pre: deque = deque(maxlen=PRE_BLOCKS)
            buf: list = []
            speaking, hot, voiced, silent, waited, noise = False, 0, 0, 0, 0, 0.004
            last_emit, t0 = 0.0, time.time()
            while True:
                if self._cancel.is_set():
                    return None
                if self._finish.is_set():
                    return np.concatenate(buf) if speaking and voiced >= MIN_VOICED_BLOCKS else None
                try:
                    blk = q.get(timeout=0.1)
                except queue.Empty:
                    if not speaking and time.time() - t0 > NO_SPEECH_BLOCKS * BLOCK / SR + 5:
                        return None                       # stream stalled
                    continue
                rms = float(np.sqrt(np.mean(blk * blk)))
                now = time.time()
                if now - last_emit > 0.05:
                    last_emit = now
                    self._emit(kind="level", v=round(min(1.0, rms * 8), 3))
                loud = rms > max(0.012, noise * 3.0)
                if not speaking:
                    noise = min(0.05, 0.97 * noise + 0.03 * rms)
                    pre.append(blk)
                    waited += 1
                    hot = hot + 1 if loud else 0
                    if hot >= 2:
                        speaking, buf, voiced, silent = True, list(pre), hot, 0
                        self._emit(kind="speech")
                    elif waited >= NO_SPEECH_BLOCKS:
                        return None
                else:
                    buf.append(blk)
                    if loud:
                        voiced, silent = voiced + 1, 0
                    else:
                        silent += 1
                    if silent >= END_SILENCE_BLOCKS or len(buf) >= MAX_BLOCKS:
                        if voiced >= MIN_VOICED_BLOCKS:
                            return np.concatenate(buf)
                        speaking, hot, buf, waited = False, 0, [], 0      # a click, not speech: keep listening
                        pre.clear()

    def _transcribe(self, model, audio) -> str:
        lang = str(self.settings.get("stt_language") or "auto")
        hint = "Reo. Hey Reo. Hello Reo." if self.settings.get("wake_word") else None     # helps Whisper spell the name
        segs, _info = model.transcribe(audio, language=None if lang == "auto" else lang, vad_filter=True, beam_size=1,
                                       condition_on_previous_text=False, without_timestamps=True, initial_prompt=hint)
        return " ".join(s.text.strip() for s in segs).strip()


class MockSTT:
    """Scripted microphone for UI tests: each start() 'hears' the next scripted phrase, then silence."""

    def __init__(self, emit):
        self._emit = emit
        self.script: list[str] = [os.environ["REO_MOCK_STT_TEXT"]] if os.environ.get("REO_MOCK_STT_TEXT") else []
        self._ev = threading.Event()
        self._cancel = False
        self._busy = False

    def status(self):
        return {"ok": True, "reason": "", "model": "base", "cached": True, "download_mb": 145}

    def list_microphones(self):
        return [{"id": "Mock microphone", "name": "Mock microphone"}, {"id": "Second mic", "name": "Second mic"}]

    def prepare(self):
        self._emit(kind="status", text="Speech model ready.")
        return {"ok": True}

    def start(self, device=""):
        if self._busy:
            return {"error": "Already listening", "code": "audio-capture"}
        self._busy, self._cancel = True, False
        self._ev.clear()

        def work():
            text = ""
            if self.script:
                time.sleep(0.15)
                self._emit(kind="level", v=0.6)
                self._emit(kind="speech")
                text = self.script.pop(0)
                time.sleep(0.15)
            else:
                self._ev.wait(15)
            self._busy = False
            self._emit(kind="final", text="" if self._cancel else text)
        threading.Thread(target=work, daemon=True).start()
        return {"ok": True}

    def stop(self):
        self._ev.set()
        return True

    def cancel(self):
        self._cancel = True
        self._ev.set()
        return True
