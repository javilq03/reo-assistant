"""Start Reo with Windows (per-user, no administrator rights): one value under HKCU\\...\\Run."""
from __future__ import annotations

import os
import sys
from pathlib import Path

KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
NAME = "Reo"
ROOT = Path(__file__).resolve().parent.parent


def command() -> str:
    """The command Windows should run at sign-in. Packaged exe: the exe. From source: pythonw (no console) + run.py."""
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}" --startup'
    exe = Path(sys.executable)
    w = exe.with_name("pythonw.exe")
    return f'"{w if w.exists() else exe}" "{ROOT / "run.py"}" --startup'


def _reg(reg=None):
    if reg is not None:
        return reg
    import winreg
    return winreg


def is_enabled(reg=None) -> bool:
    if reg is None and os.name != "nt":
        return False
    try:
        r = _reg(reg)
        with r.OpenKey(r.HKEY_CURRENT_USER, KEY, 0, r.KEY_READ) as k:
            value, _ = r.QueryValueEx(k, NAME)
        return bool(value)
    except OSError:
        return False


def set_enabled(on: bool, reg=None, cmd: str | None = None) -> tuple[bool, str]:
    """(worked, message for people). Verifies by reading the value back."""
    if reg is None and os.name != "nt":
        return False, "Starting with Windows only works on Windows."
    try:
        r = _reg(reg)
        with r.OpenKey(r.HKEY_CURRENT_USER, KEY, 0, r.KEY_SET_VALUE | r.KEY_READ) as k:
            if on:
                r.SetValueEx(k, NAME, 0, r.REG_SZ, cmd or command())
            else:
                try:
                    r.DeleteValue(k, NAME)
                except FileNotFoundError:
                    pass
    except OSError as e:
        return False, f"Windows refused the change: {e}"
    now = is_enabled(reg)
    return (now == bool(on)), ("" if now == bool(on) else "Windows did not keep the change.")
