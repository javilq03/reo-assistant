"""The game window: what Reo sees and where it clicks.

All positions are NORMALIZED to the game window's client area (0..1), so the knowledge saved for a game
keeps working when the window is moved or resized. A window object offers:

    find() -> bool            locate the window by (part of) its title
    valid() -> bool           the window still exists and is not minimized
    focus() -> bool           bring it to the front
    is_foreground() -> bool
    capture() -> PIL.Image    client area only
    click(nx, ny)             move the pointer there and click
    cursor_norm() -> (nx, ny) | None   pointer position inside the window, None when outside
    key_down(vk) -> bool
    aborted() -> bool         the user pressed Esc / Stop
    begin() / end()           Esc watcher + gold cursor for the duration of a run

WinGameWindow is the real Windows implementation; tests use a fake with the same shape.
"""
from __future__ import annotations

import io
import threading
import time
from typing import Callable

VK_F8 = 0x77


class GameWindowError(RuntimeError):
    pass


def encode(img, max_w: int = 1280, quality: int = 85) -> bytes:
    """JPEG bytes of a PIL image, downscaled so vision calls stay fast and cheap."""
    from PIL import Image
    im = img.convert("RGB")
    if im.width > max_w:
        im = im.resize((max_w, max(1, round(im.height * max_w / im.width))), Image.LANCZOS)
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=quality)
    return buf.getvalue()


def crop_norm(img, region):
    """Crop a PIL image to a normalized [x, y, w, h] region."""
    x, y, w, h = region
    W, H = img.size
    x0, y0 = int(x * W), int(y * H)
    return img.crop((x0, y0, max(x0 + 1, int((x + w) * W)), max(y0 + 1, int((y + h) * H))))


def wait_point(window, stop, timeout: float = 90.0, poll: float = 0.03, sleep: Callable[[float], None] = time.sleep,
               now: Callable[[], float] = time.time, vk: int = VK_F8):
    """Wait for the user to hover over something in the game and press F8. Returns (nx, ny) or None
    (timeout, Esc or stop). A press counts when the window reports a registered hotkey hit (delivered by Windows
    whichever program is in front) or when polling sees the key go down; presses with the pointer outside the
    game window are ignored."""
    hit = getattr(window, "hotkey_hit", None)
    end = now() + timeout
    prev = window.key_down(vk)
    while now() < end:
        if stop.is_set() or window.aborted():
            return None
        down = window.key_down(vk)
        if (down and not prev) or bool(hit and hit()):
            p = window.cursor_norm()
            if p is not None:
                return p
        prev = down
        sleep(poll)
    return None


WM_HOTKEY, WM_QUIT, MOD_NOREPEAT = 0x0312, 0x0012, 0x4000
ELEVATION_WARNING = ("The game is running as administrator and Reo is not. Windows then blocks Reo's clicks and hides key "
                     "presses aimed at the game. Close Reo and start it again with right-click > Run as administrator.")


class _Hotkey:
    """RegisterHotKey on its own thread with its own message loop. Windows delivers a registered hotkey even while another
    program is in front, which polling the key state cannot promise (it sees nothing when the game has the input)."""

    def __init__(self, user32, ctypes_mod, vk: int, ident: int = 0x7E08):
        self.u, self.c, self.vk, self.ident = user32, ctypes_mod, vk, ident
        self.hit = threading.Event()
        self.ok = False
        self._tid = 0
        self._ready = threading.Event()
        threading.Thread(target=self._loop, daemon=True, name="reo-hotkey").start()
        self._ready.wait(2.0)

    def _loop(self) -> None:
        from ctypes import wintypes
        c, u = self.c, self.u
        try:
            self._tid = c.windll.kernel32.GetCurrentThreadId()
            self.ok = bool(u.RegisterHotKey(None, self.ident, MOD_NOREPEAT, self.vk))
        finally:
            self._ready.set()
        if not self.ok:
            return
        msg = wintypes.MSG()
        while u.GetMessageW(c.byref(msg), None, 0, 0) > 0:
            if msg.message == WM_HOTKEY:
                self.hit.set()
        u.UnregisterHotKey(None, self.ident)

    def close(self) -> None:
        if self._tid:
            self.u.PostThreadMessageW(self._tid, WM_QUIT, 0, 0)
            self._tid = 0


class WinGameWindow:
    """A native Windows game window, driven through the same ctypes handles as Computer Control."""

    def __init__(self, computer, title: str):
        self.pc = computer
        self.title = (title or "").strip()
        self._h = 0
        self._hk: _Hotkey | None = None

    # ---- F8 (calibration)
    def arm_hotkey(self) -> bool:
        """Start listening for F8 through RegisterHotKey. False when another program already owns F8."""
        self.disarm_hotkey()
        self._hk = _Hotkey(self.pc.u, self.pc.c, VK_F8)
        ok = self._hk.ok
        if not ok:
            self._hk = None
        return ok

    def disarm_hotkey(self) -> None:
        if self._hk:
            self._hk.close()
            self._hk = None

    def hotkey_hit(self) -> bool:
        hk = self._hk
        if hk and hk.hit.is_set():
            hk.hit.clear()
            return True
        return False

    def elevation_warning(self) -> str:
        """ELEVATION_WARNING when the game is confirmed to run as administrator while Reo does not, else ''.
        Windows (UIPI) blocks clicks and key state across that boundary. When the game cannot be inspected
        (protected process, access denied) this stays silent rather than guess."""
        try:
            c, w, u = self.pc.c, self.pc.wt, self.pc.u
            if not self._h or c.windll.shell32.IsUserAnAdmin():
                return ""
            pid = w.DWORD()
            u.GetWindowThreadProcessId(self._h, c.byref(pid))
            k32, adv = c.windll.kernel32, c.windll.advapi32
            k32.OpenProcess.restype = w.HANDLE
            proc = k32.OpenProcess(0x1000, False, pid.value)       # PROCESS_QUERY_LIMITED_INFORMATION
            if not proc:
                return ""
            try:
                tok = w.HANDLE()
                if not adv.OpenProcessToken(proc, 0x8, c.byref(tok)):  # TOKEN_QUERY
                    return ""
                try:
                    elevated, got = w.DWORD(), w.DWORD()
                    if adv.GetTokenInformation(tok, 20, c.byref(elevated), 4, c.byref(got)) and elevated.value:   # TokenElevation
                        return ELEVATION_WARNING
                finally:
                    k32.CloseHandle(tok)
            finally:
                k32.CloseHandle(proc)
        except Exception:                                      # noqa: BLE001  a diagnostic must never break calibration
            pass
        return ""

    # ---- lookup
    def _matches(self) -> list:
        c, w, u = self.pc.c, self.pc.wt, self.pc.u
        want = self.title.lower()
        found: list = []
        proto = c.WINFUNCTYPE(w.BOOL, w.HWND, w.LPARAM)

        def cb(h, _):
            if u.IsWindowVisible(h):
                n = u.GetWindowTextLengthW(h)
                if n:
                    buf = c.create_unicode_buffer(n + 1)
                    u.GetWindowTextW(h, buf, n + 1)
                    if want in buf.value.lower():
                        found.append(h)
            return True
        u.EnumWindows(proto(cb), 0)
        return found

    def find(self) -> bool:
        if not self.title:
            return False
        for h in self._matches():
            if not self.pc.u.IsIconic(h):
                self._h = h
                return True
        return False

    def valid(self) -> bool:
        u = self.pc.u
        return bool(self._h and u.IsWindow(self._h) and not u.IsIconic(self._h))

    def rect(self) -> tuple[int, int, int, int]:
        c, w, u = self.pc.c, self.pc.wt, self.pc.u
        r, pt = w.RECT(), w.POINT(0, 0)
        u.GetClientRect(self._h, c.byref(r))
        u.ClientToScreen(self._h, c.byref(pt))
        return pt.x, pt.y, pt.x + r.right, pt.y + r.bottom

    # ---- focus / state
    def focus(self) -> bool:
        return str(self.pc.focus(self.title)).startswith("FOCUSED")

    def is_foreground(self) -> bool:
        return bool(self._h) and self.pc.u.GetForegroundWindow() == self._h

    def aborted(self) -> bool:
        return self.pc.aborted.is_set()

    def notify(self, text: str) -> None:
        """A Windows toast, so the prompt is visible even when the game covers Reo's window."""
        try:
            self.pc.notify("Reo Gameplay", text)
        except Exception:                                      # noqa: BLE001
            pass

    def begin(self):
        self.pc.begin()

    def end(self):
        self.pc.end()

    # ---- eyes and hands
    def capture(self):
        if not self.valid():
            raise GameWindowError("The game window is gone or minimized.")
        from PIL import ImageGrab
        l, t, r, b = self.rect()
        if r - l < 50 or b - t < 50:
            raise GameWindowError("The game window is too small to read.")
        return ImageGrab.grab(bbox=(l, t, r, b), all_screens=True).convert("RGB")

    def click(self, nx: float, ny: float):
        l, t, r, b = self.rect()
        x = l + max(0.0, min(1.0, nx)) * (r - l)
        y = t + max(0.0, min(1.0, ny)) * (b - t)
        self.pc.move(x, y)
        self.pc.click()

    def cursor_norm(self):
        if not self.valid():
            return None
        l, t, r, b = self.rect()
        x, y = self.pc.cursor_pos()
        if not (l <= x < r and t <= y < b):
            return None
        return (x - l) / (r - l), (y - t) / (b - t)

    def key_down(self, vk: int) -> bool:
        return bool(self.pc.u.GetAsyncKeyState(vk) & 0x8000)
