"""Bug report: a plain text file with versions, hardware, non-secret settings and the end of the log. Never contains
messages, the system prompt, API keys or file contents."""
from __future__ import annotations

import platform
import sys
import time
from pathlib import Path

REDACT = {"api_key", "system_prompt", "pronunciations", "base_url", "custom_models", "model_path", "ack_audio", "orb_pos"}


def build(settings: dict, hardware: dict | None, runtime: dict | None, log_path: Path, note: str = "", log_lines: int = 150, events: list | None = None) -> str:
    safe = {k: v for k, v in sorted(settings.items()) if k not in REDACT and not k.endswith("_set")}
    tail = ""
    try:
        tail = "\n".join(log_path.read_text(encoding="utf-8", errors="replace").splitlines()[-log_lines:])
    except OSError:
        tail = "(no log file)"
    lines = [
        "Reo bug report",
        f"time: {time.strftime('%Y-%m-%d %H:%M:%S')}",
        f"python: {sys.version.split()[0]}  os: {platform.platform()}  frozen: {bool(getattr(sys, 'frozen', False))}",
        f"runtime: {runtime or {}}",
        f"hardware: {hardware or '(not scanned)'}",
        "",
        "settings (secrets, prompt and paths removed):",
        *[f"  {k}: {v}" for k, v in safe.items()],
        "",
        "what went wrong:",
        (note or "(nothing written)").strip(),
        "",
        "recent timings (counters only):",
        *(events or ["(none yet)"]),
        "",
        f"last {log_lines} log lines:",
        tail,
    ]
    return "\n".join(lines)
