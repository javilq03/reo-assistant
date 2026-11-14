"""Conversation storage: one JSON file per conversation (crash-safe, easy to back up)."""
from __future__ import annotations

import json
import threading
import time
import uuid
from pathlib import Path

from .config import conversations_dir


def _id() -> str:
    return uuid.uuid4().hex[:12]


class ConversationStore:
    def __init__(self, folder: Path | None = None):
        self.dir = folder or conversations_dir()
        self.dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()

    def _path(self, cid: str) -> Path:
        if not cid.isalnum():
            raise ValueError("bad conversation id")
        return self.dir / f"{cid}.json"

    # ---- CRUD ----------------------------------------------------------------
    def create(self) -> dict:
        now = time.time()
        conv = {"id": _id(), "title": "New conversation", "created": now, "updated": now, "messages": []}
        self.save(conv)
        return conv

    def get(self, cid: str) -> dict | None:
        try:
            return json.loads(self._path(cid).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def save(self, conv: dict) -> None:
        with self._lock:
            p = self._path(conv["id"])
            tmp = p.with_suffix(".tmp")
            tmp.write_text(json.dumps(conv, ensure_ascii=False), encoding="utf-8")
            tmp.replace(p)

    def delete(self, cid: str) -> None:
        with self._lock:
            try:
                self._path(cid).unlink()
            except OSError:
                pass

    def rename(self, cid: str, title: str) -> None:
        conv = self.get(cid)
        if conv:
            conv["title"] = title.strip()[:80] or conv["title"]
            self.save(conv)

    def add_message(self, cid: str, role: str, text: str, **meta) -> dict:
        with self._lock:
            conv = self.get(cid)
            if conv is None:
                raise KeyError(cid)
            msg = {"id": _id(), "role": role, "text": text, "ts": time.time(), **meta}
            conv["messages"].append(msg)
            conv["updated"] = msg["ts"]
            if role == "user" and conv["title"] == "New conversation":
                t = " ".join(text.split())
                conv["title"] = (t[:40] + "...") if len(t) > 40 else t
            self.save(conv)
            return msg

    def update_message(self, cid: str, mid: str, **fields) -> None:
        with self._lock:
            conv = self.get(cid)
            if not conv:
                return
            for m in conv["messages"]:
                if m["id"] == mid:
                    m.update(fields)
                    break
            conv["updated"] = time.time()
            self.save(conv)

    def truncate_from(self, cid: str, mid: str, keep_target: bool = False) -> None:
        """Drop message `mid` and everything after it (keep_target keeps `mid` itself)."""
        with self._lock:
            conv = self.get(cid)
            if not conv:
                return
            for i, m in enumerate(conv["messages"]):
                if m["id"] == mid:
                    conv["messages"] = conv["messages"][: i + 1 if keep_target else i]
                    break
            self.save(conv)

    # ---- listing / search ------------------------------------------------------
    def list(self, query: str = "") -> list[dict]:
        q = query.strip().lower()
        out = []
        for p in self.dir.glob("*.json"):
            try:
                c = json.loads(p.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if not c.get("messages") and not q:
                # hide empty drafts unless they are the only thing around
                pass
            if q:
                hay = c.get("title", "").lower() + "\n" + "\n".join(m.get("text", "") for m in c.get("messages", [])).lower()
                if q not in hay:
                    continue
            out.append({"id": c["id"], "title": c.get("title", ""), "updated": c.get("updated", 0),
                        "count": len(c.get("messages", []))})
        out.sort(key=lambda c: c["updated"], reverse=True)
        return out

    def prune_empty(self, keep: str | None = None) -> None:
        for c in self.list():
            if c["count"] == 0 and c["id"] != keep:
                self.delete(c["id"])

    def to_markdown(self, cid: str) -> str:
        conv = self.get(cid) or {"title": "", "messages": []}
        lines = [f"# {conv['title']}", ""]
        for m in conv["messages"]:
            who = "You" if m["role"] == "user" else "Reo"
            lines += [f"**{who}:**", "", m.get("text", ""), ""]
        return "\n".join(lines)
