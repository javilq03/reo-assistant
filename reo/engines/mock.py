"""Deterministic fake engine for UI development and tests (REO_MOCK_ENGINE=1)."""
from __future__ import annotations

import os
import threading
import time
from typing import Iterator


class MockEngine:
    name = "local"
    loaded = True
    path = "mock.gguf"
    load_info = {"name": "Mock model", "path": "mock.gguf", "n_ctx": 4096, "n_gpu_layers": 0, "seconds": 0.0,
                 "thinking": True}

    def ready(self) -> bool:
        return True

    @staticmethod
    def runtime() -> dict:
        return {"installed": True, "error": "", "gpu_offload": False, "version": "mock"}

    def load(self, path, **kw):
        return self.load_info

    def unload(self):
        pass

    def stream(self, history, system_prompt, params, deep, stop: threading.Event) -> Iterator[dict]:
        last = history[-1]["text"] if history else ""
        if deep:
            for w in "Let me think about that. ".split(" "):
                yield {"t": "thought", "text": w + " "}
        words = (f"You said: {last}\n\nHere is a code sample:\n\n```python\nprint('hello from Reo')\n```\n\n"
                 "And **bold**, `inline code`, and a list:\n- one\n- two\n").split(" ")
        n = 0
        for w in words:
            if stop.is_set():
                break
            time.sleep(float(os.environ.get("REO_MOCK_DELAY", "0.01")))
            n += 1
            yield {"t": "token", "text": w + " "}
        yield {"t": "end", "stats": {"tokens": n, "tps": 99.0, "engine": "local", "model": "Mock model",
                                     "stopped": stop.is_set()}}
