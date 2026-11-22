"""Orb reactions: a rough mood from the tone of a reply (English and Spanish). Only drives a short colour pulse."""
from __future__ import annotations

import re

_RULES = [
    ("sad", r"\b(sorry|unfortunately|can'?t|cannot|couldn'?t|failed|error|lo siento|desafortunadamente|no puedo|no pude|fall[oó])\b"),
    ("happy", r"\b(great|glad|congrat\w*|awesome|perfect|done|welcome|thanks|thank you|nice|genial|me alegra|felicidades|perfecto|listo|gracias|de nada)\b|!"),
    ("curious", r"\?\s*$|^\s*(¿|which|what|do you|would you)\b"),
]
COLORS = {"happy": (120, 220, 140), "sad": (110, 150, 230), "curious": (190, 140, 240)}


def detect(text: str) -> str | None:
    t = (text or "").strip()
    if not t:
        return None
    last = t[-240:]
    for name, pat in _RULES:
        if re.search(pat, last, re.I):
            return name
    return None
