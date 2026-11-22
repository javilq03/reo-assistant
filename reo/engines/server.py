"""Managed llama-server: Reo starts llama.cpp's own server hidden on a free local port, talks to it over HTTP and stops it when
the model is unloaded or Reo quits (the same arrangement Capri uses). Same interface as LocalEngine."""
from __future__ import annotations

import atexit
import json
import logging
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Iterator

import httpx

from .base import ThinkSplitter, approx_tokens, build_messages, trim_to_budget

log = logging.getLogger("reo.server")
CREATE_NO_WINDOW = 0x08000000


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class ServerEngine:
    name = "local"                      # reported to the page like the in-process engine

    def __init__(self, command: list[str] | None = None, start_timeout: float = 180.0):
        self._command = command           # override for tests: argv prefix instead of llama-server
        self.start_timeout = start_timeout
        self.proc: subprocess.Popen | None = None
        self.port = 0
        self.path = ""
        self.n_ctx = 0
        self.native_thinking = False
        self.load_info: dict = {}
        self._lock = threading.RLock()
        atexit.register(self.unload)

    @staticmethod
    def runtime() -> dict:
        from . import server_exe
        exe = server_exe()
        return {"installed": bool(exe), "error": "" if exe else "llama-server is not installed", "gpu_offload": True,
                "version": "server", "mode": "server"}

    @property
    def loaded(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def ready(self) -> bool:
        return self.loaded

    def _argv(self, path, n_ctx, n_gpu_layers, n_threads, flash_attn, port) -> list[str]:
        if self._command:
            return [*self._command, "--port", str(port), "--model", path]
        from . import server_exe
        exe = server_exe()
        if not exe:
            raise FileNotFoundError("llama-server is not installed")
        argv = [str(exe), "-m", path, "-c", str(int(n_ctx)), "--host", "127.0.0.1", "--port", str(port),
                "-ngl", str(999 if int(n_gpu_layers) < 0 else int(n_gpu_layers)), "--no-webui"]
        if n_threads:
            argv += ["-t", str(int(n_threads))]
        argv += ["-fa", "on" if flash_attn else "off"]
        return argv

    def load(self, path: str, n_ctx: int = 4096, n_gpu_layers: int = -1, n_threads: int = 0,
             flash_attn: bool = True, thinking: bool = False) -> dict:
        if not Path(path).is_file():
            raise FileNotFoundError(path)
        with self._lock:
            self.unload()
            t0 = time.time()
            port = free_port()
            argv = self._argv(path, n_ctx, n_gpu_layers, n_threads, flash_attn, port)
            kw = {}
            if sys.platform == "win32":
                kw["creationflags"] = CREATE_NO_WINDOW
            self.proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **kw)
            self.port = port
            try:
                self._wait_ready()
            except Exception:
                self.unload()
                raise
            self.path, self.n_ctx, self.native_thinking = path, int(n_ctx), bool(thinking)
            self.load_info = {"path": path, "name": Path(path).stem, "n_ctx": self.n_ctx, "n_gpu_layers": int(n_gpu_layers),
                              "seconds": round(time.time() - t0, 1), "thinking": self.native_thinking, "server": True}
            return self.load_info

    def _wait_ready(self) -> None:
        end = time.time() + self.start_timeout
        url = f"http://127.0.0.1:{self.port}/health"
        while time.time() < end:
            if self.proc is None or self.proc.poll() is not None:
                raise RuntimeError("llama-server stopped while loading the model (it may not fit in memory)")
            try:
                if httpx.get(url, timeout=2.0).status_code == 200:
                    return
            except httpx.HTTPError:
                pass
            time.sleep(0.25)
        raise RuntimeError("llama-server did not become ready in time")

    def unload(self) -> None:
        with self._lock:
            p, self.proc = self.proc, None
            self.path, self.load_info = "", {}
            if p is not None and p.poll() is None:
                p.terminate()
                try:
                    p.wait(5)
                except subprocess.TimeoutExpired:
                    p.kill()

    def stream(self, history: list[dict], system_prompt: str, params: dict, deep: bool, stop: threading.Event) -> Iterator[dict]:
        if not self.loaded:
            raise RuntimeError("No local model is loaded")
        with self._lock:
            msgs = build_messages(history, system_prompt, deep, self.native_thinking)
            max_tokens = int(params.get("max_tokens", 1024))
            if deep:
                max_tokens = max(max_tokens * 2, 2048)
            max_tokens = min(max_tokens, max(256, self.n_ctx // 2))
            msgs, dropped = trim_to_budget(msgs, self.n_ctx - max_tokens - 32, approx_tokens)
            if dropped:
                yield {"t": "notice", "text": f"Context is full: {dropped} oldest message(s) were left out of this reply."}
            temp = float(params.get("temperature", 0.7))
            if deep and self.native_thinking:
                temp = max(temp, 0.6)
            body = {"messages": msgs, "stream": True, "max_tokens": max_tokens, "temperature": temp,
                    "top_p": float(params.get("top_p", 0.9)), "top_k": int(params.get("top_k", 40)),
                    "repeat_penalty": float(params.get("repeat_penalty", 1.1))}
            split = ThinkSplitter()
            n, t0, first = 0, time.time(), None
            url = f"http://127.0.0.1:{self.port}/v1/chat/completions"
            with httpx.Client(timeout=httpx.Timeout(10.0, read=600.0)) as c, c.stream("POST", url, json=body) as r:
                if r.status_code != 200:
                    raise RuntimeError(f"llama-server answered {r.status_code}")
                for line in r.iter_lines():
                    if stop.is_set():
                        break
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    try:
                        text = json.loads(data)["choices"][0].get("delta", {}).get("content")
                    except (ValueError, KeyError, IndexError):
                        continue
                    if not text:
                        continue
                    if first is None:
                        first = time.time()
                    n += 1
                    for kind, part in split.feed(text):
                        yield {"t": kind, "text": part}
            for kind, part in split.flush():
                yield {"t": kind, "text": part}
            dt = max(time.time() - (first or t0), 1e-3)
            yield {"t": "end", "stats": {"tokens": n, "tps": round(n / dt, 1), "engine": "local",
                                         "model": self.load_info.get("name", ""), "stopped": stop.is_set()}}
