"""In-process GGUF inference through llama.cpp (llama-cpp-python). No server, no Ollama."""
from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Iterator

from .base import ThinkSplitter, build_messages, trim_to_budget


class LocalEngine:
    name = "local"

    def __init__(self):
        self.llm = None
        self.path = ""
        self.n_ctx = 0
        self.native_thinking = False
        self.load_info: dict = {}
        self._lock = threading.RLock()        # one generation or load at a time

    # ---- capability probes ---------------------------------------------------
    @staticmethod
    def runtime() -> dict:
        try:
            import llama_cpp  # type: ignore
        except Exception as e:                # missing wheel / DLL problem
            return {"installed": False, "error": str(e), "gpu_offload": False, "version": ""}
        try:
            gpu = bool(llama_cpp.llama_supports_gpu_offload())
        except Exception:
            gpu = False
        return {"installed": True, "error": "", "gpu_offload": gpu, "version": getattr(llama_cpp, "__version__", "")}

    @property
    def loaded(self) -> bool:
        return self.llm is not None

    def ready(self) -> bool:
        return self.loaded

    # ---- load / unload ---------------------------------------------------------
    def load(self, path: str, n_ctx: int = 4096, n_gpu_layers: int = -1, n_threads: int = 0,
             flash_attn: bool = True, thinking: bool = False) -> dict:
        from llama_cpp import Llama  # type: ignore  (imported lazily: heavy, and optional until first load)
        if not Path(path).is_file():
            raise FileNotFoundError(path)
        with self._lock:
            self.unload()
            t0 = time.time()
            kw = dict(model_path=path, n_ctx=int(n_ctx), n_gpu_layers=int(n_gpu_layers), verbose=False,
                      flash_attn=bool(flash_attn))
            if n_threads:
                kw["n_threads"] = int(n_threads)
            try:
                self.llm = Llama(**kw)
            except Exception:
                if not kw["flash_attn"]:
                    raise
                kw["flash_attn"] = False         # some backends/models reject flash attention
                self.llm = Llama(**kw)
            self.path, self.n_ctx, self.native_thinking = path, int(n_ctx), bool(thinking)
            self.load_info = {"path": path, "name": Path(path).stem, "n_ctx": self.n_ctx,
                              "n_gpu_layers": int(n_gpu_layers), "seconds": round(time.time() - t0, 1),
                              "thinking": self.native_thinking}
            return self.load_info

    def unload(self) -> None:
        with self._lock:
            self.llm = None
            self.path = ""
            self.load_info = {}
            import gc
            gc.collect()

    # ---- generation --------------------------------------------------------------
    def stream(self, history: list[dict], system_prompt: str, params: dict, deep: bool,
               stop: threading.Event) -> Iterator[dict]:
        if not self.llm:
            raise RuntimeError("No local model is loaded")
        with self._lock:
            llm = self.llm
            msgs = build_messages(history, system_prompt, deep, self.native_thinking)
            max_tokens = int(params.get("max_tokens", 1024))
            if deep:
                max_tokens = max(max_tokens * 2, 2048)
            max_tokens = min(max_tokens, max(256, self.n_ctx // 2))
            count = lambda t: len(llm.tokenize(t.encode("utf-8", "ignore"), add_bos=False))
            msgs, dropped = trim_to_budget(msgs, self.n_ctx - max_tokens - 32, count)
            if dropped:
                yield {"t": "notice", "text": f"Context is full: {dropped} oldest message(s) were left out of this reply."}

            temp = float(params.get("temperature", 0.7))
            if deep and self.native_thinking:
                temp = max(temp, 0.6)               # Qwen3 guidance: avoid greedy decoding when thinking
            gen = llm.create_chat_completion(
                messages=msgs, stream=True, max_tokens=max_tokens, temperature=temp,
                top_p=float(params.get("top_p", 0.9)), top_k=int(params.get("top_k", 40)),
                repeat_penalty=float(params.get("repeat_penalty", 1.1)))
            split = ThinkSplitter()
            n, t0, first = 0, time.time(), None
            try:
                for chunk in gen:
                    if stop.is_set():
                        break
                    delta = chunk["choices"][0].get("delta", {})
                    text = delta.get("content")
                    if not text:
                        continue
                    if first is None:
                        first = time.time()
                    n += 1
                    for kind, part in split.feed(text):
                        yield {"t": kind, "text": part}
                for kind, part in split.flush():
                    yield {"t": kind, "text": part}
            finally:
                close = getattr(gen, "close", None)
                if close:
                    close()
            dt = max(time.time() - (first or t0), 1e-3)
            yield {"t": "end", "stats": {"tokens": n, "tps": round(n / dt, 1), "engine": "local",
                                         "model": self.load_info.get("name", ""), "stopped": stop.is_set()}}
