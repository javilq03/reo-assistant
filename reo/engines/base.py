"""Shared engine pieces: event types, <think> splitting, prompt assembly."""
from __future__ import annotations

from typing import Iterable, Iterator

# Engines yield dicts: {"t": "token"|"thought", "text": str}  and finally {"t": "end", "stats": {...}}


class ThinkSplitter:
    """Splits a token stream into answer text and <think>...</think> reasoning, even when
    a tag is cut across chunks."""

    OPEN, CLOSE = "<think>", "</think>"

    def __init__(self):
        self.in_think = False
        self._buf = ""

    def feed(self, text: str) -> Iterator[tuple[str, str]]:
        self._buf += text
        while True:
            tag = self.CLOSE if self.in_think else self.OPEN
            i = self._buf.find(tag)
            if i >= 0:
                if i:
                    yield ("thought" if self.in_think else "token", self._buf[:i])
                self._buf = self._buf[i + len(tag):]
                self.in_think = not self.in_think
                continue
            # keep a possible partial tag at the end of the buffer
            keep = 0
            for n in range(min(len(tag) - 1, len(self._buf)), 0, -1):
                if tag.startswith(self._buf[-n:]):
                    keep = n
                    break
            emit = self._buf[: len(self._buf) - keep]
            if emit:
                yield ("thought" if self.in_think else "token", emit)
            self._buf = self._buf[len(self._buf) - keep:]
            return

    def flush(self) -> Iterator[tuple[str, str]]:
        if self._buf:
            yield ("thought" if self.in_think else "token", self._buf)
            self._buf = ""


def build_messages(history: list[dict], system_prompt: str, deep: bool, native_thinking: bool) -> list[dict]:
    """Convert stored messages ({role: user|bot, text}) to chat-completion messages."""
    sys_text = system_prompt.strip()
    if deep and not native_thinking:
        sys_text += "\n\nThink through the problem step by step before giving the final answer."
    msgs = [{"role": "system", "content": sys_text}] if sys_text else []
    for m in history:
        role = "user" if m["role"] == "user" else "assistant"
        msgs.append({"role": role, "content": m.get("text", "")})
    if native_thinking and msgs and msgs[-1]["role"] == "user":
        # Qwen3 soft switch
        msgs[-1] = {**msgs[-1], "content": msgs[-1]["content"] + ("\n/think" if deep else "\n/no_think")}
    return msgs


def trim_to_budget(msgs: list[dict], budget_tokens: int, count: "callable") -> tuple[list[dict], int]:
    """Drop the oldest non-system turns until the prompt fits. Returns (messages, dropped_count)."""
    def total(ms):  # per-message overhead ~ 8 tokens for role markers
        return sum(count(m["content"]) + 8 for m in ms)

    head = [m for m in msgs[:1] if m["role"] == "system"]
    body = msgs[len(head):]
    dropped = 0
    while len(body) > 1 and total(head + body) > budget_tokens:
        body = body[1:]
        dropped += 1
        while len(body) > 1 and body[0]["role"] != "user":   # keep turns well-formed
            body = body[1:]
            dropped += 1
    return head + body, dropped


def approx_tokens(text: str) -> int:
    return max(1, len(text) // 3)


def drain(gen: Iterable[dict]) -> str:
    return "".join(e["text"] for e in gen if e.get("t") == "token")
