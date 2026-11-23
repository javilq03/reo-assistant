"""Cloud/API fallback. Two wire protocols cover essentially every provider:

  "openai"    POST {base}/chat/completions   OpenAI, Gemini (OpenAI-compat), NVIDIA NIM, xAI Grok,
                                             Groq, OpenRouter, Mistral, DeepSeek, LM Studio, ...
  "anthropic" POST {base}/messages           Claude
"""
from __future__ import annotations

import base64
import json
import re
import threading
import time
from typing import Iterator

import httpx

from .base import ThinkSplitter, build_messages

PRESETS = {
    "openai":     {"label": "OpenAI",             "base_url": "https://api.openai.com/v1",                                  "protocol": "openai"},
    "anthropic":  {"label": "Anthropic (Claude)", "base_url": "https://api.anthropic.com/v1",                               "protocol": "anthropic"},
    "gemini":     {"label": "Google Gemini",      "base_url": "https://generativelanguage.googleapis.com/v1beta/openai",    "protocol": "openai"},
    "nvidia":     {"label": "NVIDIA NIM",         "base_url": "https://integrate.api.nvidia.com/v1",                        "protocol": "openai"},
    "xai":        {"label": "xAI (Grok)",         "base_url": "https://api.x.ai/v1",                                        "protocol": "openai"},
    "openrouter": {"label": "OpenRouter",         "base_url": "https://openrouter.ai/api/v1",                               "protocol": "openai"},
    "groq":       {"label": "Groq",               "base_url": "https://api.groq.com/openai/v1",                             "protocol": "openai"},
    "custom":     {"label": "Custom (OpenAI-compatible)", "base_url": "",                                                   "protocol": "openai"},
}


class CloudError(RuntimeError):
    pass


def _mime(data: bytes) -> str:
    return "image/png" if data[:8] == b"\x89PNG\r\n\x1a\n" else "image/jpeg"


def _friendly(status: int, body: str) -> str:
    detail = ""
    try:
        j = json.loads(body)
        e = j.get("error", j)
        detail = e.get("message") if isinstance(e, dict) else str(e)
    except Exception:
        detail = body[:200]
    hint = {401: "The API key was rejected.", 403: "The key has no access to this model or region.",
            404: "Model or endpoint not found - check the model name and base URL.",
            429: "Rate limit or quota reached."}.get(status, "")
    return f"{status}: {hint} {detail}".strip()


class CloudEngine:
    name = "cloud"

    def __init__(self, settings):
        self.settings = settings

    # ---- configuration ------------------------------------------------------------
    def config(self) -> dict:
        s = self.settings
        preset = PRESETS.get(s.get("provider"), PRESETS["custom"])
        base = (s.get("base_url") or preset["base_url"]).rstrip("/")
        proto = s.get("api_protocol") if s.get("provider") == "custom" else preset["protocol"]
        return {"base": base, "protocol": proto or "openai", "model": s.get("cloud_model"), "key": s.get("api_key")}

    def ready(self) -> bool:
        c = self.config()
        local_ok = c["base"].startswith(("http://localhost", "http://127.0.0.1"))   # LM Studio etc. need no key
        return bool(c["base"] and c["model"] and (c["key"] or local_ok))

    def _headers(self, c: dict) -> dict:
        if c["protocol"] == "anthropic":
            return {"x-api-key": c["key"], "anthropic-version": "2023-06-01", "content-type": "application/json"}
        h = {"content-type": "application/json"}
        if c["key"]:
            h["authorization"] = f"Bearer {c['key']}"
        return h

    # ---- model list / test ----------------------------------------------------------
    def list_models(self) -> list[str]:
        c = self.config()
        if not c["base"]:
            raise CloudError("Set a base URL first")
        try:
            r = httpx.get(c["base"] + "/models", headers=self._headers(c), timeout=15)
        except httpx.HTTPError as e:
            raise CloudError(f"Could not reach {c['base']}: {e}") from e
        if r.status_code >= 400:
            raise CloudError(_friendly(r.status_code, r.text))
        ids = [str(m.get("id", "")).removeprefix("models/") for m in r.json().get("data", [])]
        return sorted(i for i in ids if i)

    # ---- one-shot call with images (used by Gameplay to read the screen) ------------------
    def vision(self, system: str, text: str, images: list[bytes], params: dict | None = None) -> str:
        """Non-streaming request carrying screenshots (PNG/JPEG bytes). Returns the reply text."""
        c = self.config()
        if not self.ready():
            raise CloudError("The API provider is not configured (key, model, base URL)")
        params = params or {}
        enc = [(_mime(b), base64.b64encode(b).decode()) for b in images]
        max_tokens = int(params.get("max_tokens", 900))
        temperature = params.get("temperature", 0.3)
        if c["protocol"] == "anthropic":
            content = [{"type": "image", "source": {"type": "base64", "media_type": m, "data": d}} for m, d in enc]
            content.append({"type": "text", "text": text})
            body = {"model": c["model"], "max_tokens": max_tokens, "temperature": temperature,
                    "messages": [{"role": "user", "content": content}]}
            if system:
                body["system"] = system
            data = self._post(c, c["base"] + "/messages", body)
            return "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")
        content = [{"type": "text", "text": text}]
        content += [{"type": "image_url", "image_url": {"url": f"data:{m};base64,{d}"}} for m, d in enc]
        body = {"model": c["model"], "temperature": temperature, "max_tokens": max_tokens,
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": content}]}
        for _ in range(4):                          # strip parameters the model rejects, then retry
            try:
                data = self._post(c, c["base"] + "/chat/completions", body)
                return (data.get("choices") or [{}])[0].get("message", {}).get("content") or ""
            except CloudError as e:
                msg = str(e).lower()
                if not msg.startswith("400"):
                    raise
                if "max_tokens" in msg and "max_tokens" in body:
                    body["max_completion_tokens"] = body.pop("max_tokens")
                elif "temperature" in msg and "temperature" in body:
                    body.pop("temperature")
                else:
                    raise
        raise CloudError("The provider kept rejecting the request")

    def _post(self, c: dict, url: str, body: dict) -> dict:
        try:
            with httpx.Client(timeout=httpx.Timeout(20, read=120)) as client:
                r = client.post(url, headers=self._headers(c), json=body)
        except httpx.HTTPError as e:
            raise CloudError(f"Network error: {e}") from e
        if r.status_code >= 400:
            raise CloudError(_friendly(r.status_code, r.text))
        try:
            return r.json()
        except ValueError as e:
            raise CloudError("The provider sent an unreadable reply") from e

    # ---- streaming -----------------------------------------------------------------------
    def stream(self, history: list[dict], system_prompt: str, params: dict, deep: bool,
               stop: threading.Event) -> Iterator[dict]:
        c = self.config()
        if not self.ready():
            raise CloudError("The API provider is not configured (key, model, base URL)")
        msgs = build_messages(history, system_prompt, deep, native_thinking=False)
        t0, n, first = time.time(), 0, None
        split = ThinkSplitter()
        gen = self._anthropic if c["protocol"] == "anthropic" else self._openai
        for kind, text in gen(c, msgs, params, deep, stop):
            if kind == "usage":
                continue
            if first is None:
                first = time.time()
            n += 1
            if kind == "thought":
                yield {"t": "thought", "text": text}
            else:
                for k, part in split.feed(text):     # some OpenAI-compatible hosts inline <think>
                    yield {"t": k, "text": part}
        for k, part in split.flush():
            yield {"t": k, "text": part}
        dt = max(time.time() - (first or t0), 1e-3)
        yield {"t": "end", "stats": {"tokens": n, "tps": round(n / dt, 1), "engine": "cloud", "model": c["model"],
                                     "stopped": stop.is_set()}}

    def _sse(self, c: dict, url: str, body: dict, stop: threading.Event):
        """POST and yield parsed SSE `data:` JSON objects. Raises CloudError with a readable message."""
        try:
            with httpx.Client(timeout=httpx.Timeout(20, read=90)) as client:
                with client.stream("POST", url, headers=self._headers(c), json=body) as r:
                    if r.status_code >= 400:
                        r.read()
                        raise CloudError(_friendly(r.status_code, r.text))
                    for line in r.iter_lines():
                        if stop.is_set():
                            return
                        if not line.startswith("data:"):
                            continue
                        data = line[5:].strip()
                        if data == "[DONE]":
                            return
                        try:
                            yield json.loads(data)
                        except ValueError:
                            continue
        except httpx.HTTPError as e:
            raise CloudError(f"Network error: {e}") from e

    def _openai(self, c, msgs, params, deep, stop):
        body = {"model": c["model"], "messages": msgs, "stream": True,
                "temperature": params.get("temperature", 0.7), "top_p": params.get("top_p", 0.9),
                "max_tokens": int(params.get("max_tokens", 1024)) * (2 if deep else 1)}
        if deep:
            body["reasoning_effort"] = "high"
        url = c["base"] + "/chat/completions"
        for _ in range(4):                          # strip parameters the model rejects, then retry
            try:
                for ev in self._sse(c, url, body, stop):
                    ch = (ev.get("choices") or [{}])[0].get("delta") or {}
                    r = ch.get("reasoning_content") or ch.get("reasoning")
                    if r:
                        yield "thought", r
                    if ch.get("content"):
                        yield "token", ch["content"]
                return
            except CloudError as e:
                msg = str(e).lower()
                if not msg.startswith("400"):
                    raise
                if "max_tokens" in msg and "max_tokens" in body:
                    body["max_completion_tokens"] = body.pop("max_tokens")
                elif "reasoning_effort" in msg and "reasoning_effort" in body:
                    body.pop("reasoning_effort")
                elif re.search(r"temperature|top_p", msg) and ("temperature" in body or "top_p" in body):
                    body.pop("temperature", None)
                    body.pop("top_p", None)
                else:
                    raise

    def _anthropic(self, c, msgs, params, deep, stop):
        system = "\n\n".join(m["content"] for m in msgs if m["role"] == "system")
        turns: list[dict] = []
        for m in msgs:
            if m["role"] == "system":
                continue
            if turns and turns[-1]["role"] == m["role"]:
                turns[-1]["content"] += "\n\n" + m["content"]
            else:
                turns.append({"role": m["role"], "content": m["content"]})
        while turns and turns[0]["role"] != "user":
            turns.pop(0)
        max_tokens = int(params.get("max_tokens", 1024))
        body = {"model": c["model"], "messages": turns, "stream": True, "max_tokens": max_tokens,
                "temperature": params.get("temperature", 0.7)}
        if system:
            body["system"] = system
        if deep:
            budget = max(2048, max_tokens)
            body.pop("temperature", None)
            body["max_tokens"] = budget + max_tokens
            body["thinking"] = {"type": "enabled", "budget_tokens": budget}
        url = c["base"] + "/messages"
        for attempt in range(2):
            try:
                for ev in self._sse(c, url, body, stop):
                    if ev.get("type") == "error":
                        raise CloudError(ev.get("error", {}).get("message", "API error"))
                    if ev.get("type") == "content_block_delta":
                        d = ev.get("delta", {})
                        if d.get("type") == "thinking_delta":
                            yield "thought", d.get("thinking", "")
                        elif d.get("type") == "text_delta":
                            yield "token", d.get("text", "")
                return
            except CloudError as e:
                if attempt == 0 and str(e).startswith("400") and "thinking" in body:
                    body.pop("thinking")            # model without extended thinking support
                    body["max_tokens"] = max_tokens
                    body["temperature"] = params.get("temperature", 0.7)
                    continue
                raise
