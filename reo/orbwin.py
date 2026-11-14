"""Chat-window geometry helpers (physical pixels): used to move and resize the frameless chat window from the page."""
from __future__ import annotations

import logging
import os

log = logging.getLogger(__name__)


class Generic:
    """pywebview move/resize only (non-Windows and tests)."""
    def scale(self, win) -> float:
        return 1.0

    def screen(self) -> tuple:
        try:
            import webview
            sc = webview.screens[0]
            return 0, 0, int(sc.width), int(sc.height)
        except Exception:
            return 0, 0, 1920, 1080

    def rect(self, win) -> tuple:
        return int(win.x), int(win.y), int(getattr(win, "width", 0)), int(getattr(win, "height", 0))

    def move_resize(self, win, x, y, w, h):
        win.resize(int(w), int(h))
        win.move(int(x), int(y))
        win.x, win.y, win.width, win.height = int(x), int(y), int(w), int(h)


class Win32(Generic):
    def __init__(self):
        import ctypes
        from ctypes import wintypes
        self.c, self.wt = ctypes, wintypes
        self.u = ctypes.windll.user32
        self.u.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
        self.u.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.UINT]
        self.u.GetDpiForWindow.argtypes = [wintypes.HWND]

    def hwnd(self, win):
        h = win.native.Handle
        try:
            return int(h.ToInt64())
        except Exception:
            return int(h)

    def scale(self, win) -> float:
        try:
            return max(1.0, self.u.GetDpiForWindow(self.hwnd(win)) / 96.0)
        except Exception:
            return 1.0

    def rect(self, win) -> tuple:
        r = self.wt.RECT()
        self.u.GetWindowRect(self.hwnd(win), self.c.byref(r))
        return r.left, r.top, r.right - r.left, r.bottom - r.top

    def move_resize(self, win, x, y, w, h):
        # SWP_NOZORDER | SWP_NOACTIVATE
        if not self.u.SetWindowPos(self.hwnd(win), None, int(x), int(y), int(w), int(h), 0x0004 | 0x0010):
            raise OSError("SetWindowPos failed")


def make():
    if os.name == "nt":
        try:
            return Win32()
        except Exception:
            log.exception("Win32 geometry unavailable")
    return Generic()
