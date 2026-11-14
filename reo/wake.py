"""Wake word: in hands-free mode Reo only answers when he hears his name ("Reo", "Hey Reo", "Hello Reo")."""
from __future__ import annotations

import re
import unicodedata

# Whisper often hears the name as one of these.
NAMES = {"reo", "rio", "rayo", "reyo", "raio", "reho", "rheo", "ryo", "leo", "reau", "rejo", "ray-o"}
GREETINGS = {"hey", "hi", "hello", "hola", "ok", "okay", "oye", "ey", "eh", "yo"}


def _norm(tok: str) -> str:
    t = unicodedata.normalize("NFKD", tok.lower())
    t = "".join(c for c in t if not unicodedata.combining(c))
    return re.sub(r"[^a-z\-]", "", t)


def extract(text: str) -> tuple[bool, str]:
    """(heard_name, command). The name, and a greeting right before it, are removed from the command."""
    words = re.findall(r"\S+", text or "")
    norm = [_norm(w) for w in words]
    idx = next((i for i, n in enumerate(norm) if n in NAMES), None)
    if idx is None:
        return False, ""
    drop = {idx}
    if idx > 0 and norm[idx - 1] in GREETINGS:
        drop.add(idx - 1)
    rest = [w for i, w in enumerate(words) if i not in drop]
    cmd = " ".join(rest).strip(" ,.;:!?-")
    return True, cmd
