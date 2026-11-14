"""The floating orb and its reply bubble as native Windows layered windows (ctypes + Win32, no web view).

Each window is a WS_EX_LAYERED popup painted with UpdateLayeredWindow from a premultiplied BGRA bitmap, so
transparent pixels are truly see-through and click-through. Each lives on its own thread with its own message loop,
which is what makes mouse capture, dragging and clicks reliable."""
from __future__ import annotations

import ctypes
import logging
import os
import threading
import time
from ctypes import wintypes
from pathlib import Path

import numpy as np
from PIL import Image

from . import orb_art

log = logging.getLogger("reo.orb")

WM_DESTROY, WM_TIMER, WM_MOUSEMOVE, WM_LBUTTONDOWN, WM_LBUTTONUP, WM_LBUTTONDBLCLK = 0x02, 0x113, 0x200, 0x201, 0x202, 0x203
WM_MOUSEACTIVATE, WM_APP, WM_QUIT = 0x21, 0x8000, 0x12
WS_POPUP, WS_EX_LAYERED, WS_EX_TOPMOST, WS_EX_TOOLWINDOW, WS_EX_NOACTIVATE = 0x80000000, 0x80000, 0x8, 0x80, 0x08000000
SWP_NOSIZE, SWP_NOZORDER, SWP_NOACTIVATE = 0x1, 0x4, 0x10
SW_HIDE, SW_SHOWNOACTIVATE = 0, 4
CLASS = "ReoLayeredWindow"
CLICK_TIMER = 1


class _BMIH(ctypes.Structure):
    _fields_ = [("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG), ("biHeight", wintypes.LONG), ("biPlanes", wintypes.WORD),
                ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD), ("biSizeImage", wintypes.DWORD),
                ("biXPelsPerMeter", wintypes.LONG), ("biYPelsPerMeter", wintypes.LONG), ("biClrUsed", wintypes.DWORD),
                ("biClrImportant", wintypes.DWORD)]


class _BLEND(ctypes.Structure):
    _fields_ = [("BlendOp", ctypes.c_ubyte), ("BlendFlags", ctypes.c_ubyte), ("SourceConstantAlpha", ctypes.c_ubyte), ("AlphaFormat", ctypes.c_ubyte)]


class _WNDCLASS(ctypes.Structure):
    pass


_WNDPROC = None
_windows: dict[int, "LayeredWindow"] = {}
_registered = False
_u = _g = _k = None


def _load():
    global _u, _g, _k, _WNDPROC
    if _u is not None:
        return
    _u, _g, _k = ctypes.windll.user32, ctypes.windll.gdi32, ctypes.windll.kernel32
    LRESULT = ctypes.c_ssize_t
    _WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND, wintypes.UINT, ctypes.c_size_t, ctypes.c_ssize_t)
    _WNDCLASS._fields_ = [("style", wintypes.UINT), ("lpfnWndProc", _WNDPROC), ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int),
                          ("hInstance", wintypes.HINSTANCE), ("hIcon", wintypes.HANDLE), ("hCursor", wintypes.HANDLE),
                          ("hbrBackground", wintypes.HANDLE), ("lpszMenuName", wintypes.LPCWSTR), ("lpszClassName", wintypes.LPCWSTR)]
    _u.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT, ctypes.c_size_t, ctypes.c_ssize_t]
    _u.DefWindowProcW.restype = LRESULT
    _u.CreateWindowExW.argtypes = [wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD, ctypes.c_int, ctypes.c_int,
                                   ctypes.c_int, ctypes.c_int, wintypes.HWND, wintypes.HANDLE, wintypes.HINSTANCE, wintypes.LPVOID]
    _u.CreateWindowExW.restype = wintypes.HWND
    _u.RegisterClassW.argtypes = [ctypes.POINTER(_WNDCLASS)]
    _u.LoadCursorW.restype = wintypes.HANDLE
    _u.LoadCursorW.argtypes = [wintypes.HINSTANCE, wintypes.LPCWSTR]
    _u.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.UINT]
    _u.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    _u.GetDC.argtypes = [wintypes.HWND]
    _u.GetDC.restype = wintypes.HDC
    _u.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
    _u.SetCapture.argtypes = [wintypes.HWND]
    _u.SetTimer.argtypes = [wintypes.HWND, ctypes.c_size_t, wintypes.UINT, wintypes.LPVOID]
    _u.KillTimer.argtypes = [wintypes.HWND, ctypes.c_size_t]
    _u.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, ctypes.c_size_t, ctypes.c_ssize_t]
    _u.UpdateLayeredWindow.argtypes = [wintypes.HWND, wintypes.HDC, ctypes.POINTER(wintypes.POINT), ctypes.POINTER(wintypes.SIZE),
                                       wintypes.HDC, ctypes.POINTER(wintypes.POINT), wintypes.COLORREF, ctypes.POINTER(_BLEND), wintypes.DWORD]
    _g.CreateCompatibleDC.argtypes = [wintypes.HDC]
    _g.CreateCompatibleDC.restype = wintypes.HDC
    _g.CreateDIBSection.argtypes = [wintypes.HDC, ctypes.POINTER(_BMIH), wintypes.UINT, ctypes.POINTER(ctypes.c_void_p), wintypes.HANDLE, wintypes.DWORD]
    _g.CreateDIBSection.restype = wintypes.HANDLE
    _g.SelectObject.argtypes = [wintypes.HDC, wintypes.HANDLE]
    _g.SelectObject.restype = wintypes.HANDLE
    _g.DeleteObject.argtypes = [wintypes.HANDLE]
    _g.DeleteDC.argtypes = [wintypes.HDC]
    _k.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
    _k.GetModuleHandleW.restype = wintypes.HINSTANCE


def _wndproc(hwnd, msg, wp, lp):
    w = _windows.get(hwnd)
    if w is not None:
        try:
            r = w.handle(msg, wp, lp)
            if r is not None:
                return r
        except Exception:
            log.exception("window message failed")
    return _u.DefWindowProcW(hwnd, msg, wp, lp)


def _register():
    global _registered, _cb
    if _registered:
        return
    _load()
    _cb = _WNDPROC(_wndproc)                                   # keep a reference for the life of the process
    wc = _WNDCLASS()
    wc.style = 0x8                                             # CS_DBLCLKS
    wc.lpfnWndProc = _cb
    wc.hInstance = _k.GetModuleHandleW(None)
    wc.hCursor = _u.LoadCursorW(None, ctypes.cast(ctypes.c_void_p(32512), wintypes.LPCWSTR))
    wc.lpszClassName = CLASS
    if not _u.RegisterClassW(ctypes.byref(wc)):
        raise OSError("RegisterClassW failed")
    _registered = True


def premultiplied_bgra(img) -> bytes:
    a = np.asarray(img.convert("RGBA"), dtype=np.uint16)
    out = np.empty(a.shape, np.uint8)
    al = a[..., 3]
    out[..., 0] = (a[..., 2] * al + 127) // 255
    out[..., 1] = (a[..., 1] * al + 127) // 255
    out[..., 2] = (a[..., 0] * al + 127) // 255
    out[..., 3] = al
    return out.tobytes()


class LayeredWindow:
    """One layered popup on its own thread. Handlers: on_click, on_double, on_moved (all optional, called on that thread)."""

    def __init__(self, draggable=False, delay_click=False, on_click=None, on_double=None, on_moved=None):
        self.draggable, self.delay_click = draggable, delay_click
        self.on_click, self.on_double, self.on_moved = on_click, on_double, on_moved
        self.hwnd = None
        self.x = self.y = 0
        self.w = self.h = 0
        self.visible = False
        self._pending = None
        self._lock = threading.Lock()
        self._ready = threading.Event()
        self._err = None
        self._down = None
        self._pt = (0, 0)
        self._dragging = False
        self._skip_up = False
        self._dc = self._bmp = self._old = None
        self._bits = None
        threading.Thread(target=self._run, daemon=True, name="reo-orb-window").start()
        if not self._ready.wait(10) or self._err:
            raise OSError(f"could not create the window: {self._err}")

    # ---- window thread
    def _run(self):
        try:
            _register()
            self.hwnd = _u.CreateWindowExW(WS_EX_LAYERED | WS_EX_TOPMOST | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE, CLASS, "Reo", WS_POPUP,
                                           0, 0, 1, 1, None, None, _k.GetModuleHandleW(None), None)
            if not self.hwnd:
                raise OSError("CreateWindowExW failed")
            _windows[self.hwnd] = self
        except Exception as e:
            self._err = e
            self._ready.set()
            return
        self._ready.set()
        msg = wintypes.MSG()
        while _u.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            _u.TranslateMessage(ctypes.byref(msg))
            _u.DispatchMessageW(ctypes.byref(msg))

    @staticmethod
    def _client(lp):
        return ctypes.c_short(lp & 0xFFFF).value, ctypes.c_short((lp >> 16) & 0xFFFF).value

    def handle(self, msg, wp, lp):
        if msg == WM_APP:
            self._apply()
            return 0
        if msg == WM_MOUSEACTIVATE:
            return 3                                           # MA_NOACTIVATE
        if msg == WM_LBUTTONDOWN:
            pt = wintypes.POINT()
            _u.GetCursorPos(ctypes.byref(pt))
            self._down, self._dragging, self._skip_up = (pt.x, pt.y, self.x, self.y), False, False
            self._pt = self._client(lp)
            _u.SetCapture(self.hwnd)
            return 0
        if msg == WM_MOUSEMOVE and self._down and (wp & 1):
            pt = wintypes.POINT()
            _u.GetCursorPos(ctypes.byref(pt))
            dx, dy = pt.x - self._down[0], pt.y - self._down[1]
            if not self._dragging and self.draggable and (dx * dx + dy * dy) > 16:
                self._dragging = True
            if self._dragging:
                self.x, self.y = self._down[2] + dx, self._down[3] + dy
                _u.SetWindowPos(self.hwnd, None, self.x, self.y, 0, 0, SWP_NOSIZE | SWP_NOZORDER | SWP_NOACTIVATE)
            return 0
        if msg == WM_LBUTTONUP:
            _u.ReleaseCapture()
            was_drag, self._down = self._dragging, None
            self._dragging = False
            if self._skip_up:
                self._skip_up = False
                return 0
            if was_drag:
                self.on_moved and self.on_moved(self.x, self.y)
            elif self.delay_click:
                self._pt = self._client(lp)
                _u.SetTimer(self.hwnd, CLICK_TIMER, _u.GetDoubleClickTime(), None)
            else:
                self.on_click and self.on_click(*self._client(lp))
            return 0
        if msg == WM_LBUTTONDBLCLK:
            _u.KillTimer(self.hwnd, CLICK_TIMER)
            self._skip_up = True
            self.on_double and self.on_double(*self._client(lp))
            return 0
        if msg == WM_TIMER and wp == CLICK_TIMER:
            _u.KillTimer(self.hwnd, CLICK_TIMER)
            self.on_click and self.on_click(*self._pt)
            return 0
        return None

    def _apply(self):
        with self._lock:
            p, self._pending = self._pending, None
        if p is None:
            return
        data, w, h, x, y = p
        x = self.x if x is None else x            # resolved here, on the window thread, so it can't race a drag
        y = self.y if y is None else y
        if (w, h) != (self.w, self.h):
            self._free()
            hdc = _u.GetDC(None)
            bi = _BMIH(ctypes.sizeof(_BMIH), w, -h, 1, 32, 0, 0, 0, 0, 0, 0)
            bits = ctypes.c_void_p()
            self._bmp = _g.CreateDIBSection(hdc, ctypes.byref(bi), 0, ctypes.byref(bits), None, 0)
            self._dc = _g.CreateCompatibleDC(hdc)
            self._old = _g.SelectObject(self._dc, self._bmp)
            _u.ReleaseDC(None, hdc)
            self._bits, self.w, self.h = bits, w, h
        ctypes.memmove(self._bits, data, w * h * 4)
        self.x, self.y = x, y
        pos, size, src = wintypes.POINT(x, y), wintypes.SIZE(w, h), wintypes.POINT(0, 0)
        blend = _BLEND(0, 0, 255, 1)                           # AC_SRC_OVER, per-pixel alpha
        _u.UpdateLayeredWindow(self.hwnd, None, ctypes.byref(pos), ctypes.byref(size), self._dc, ctypes.byref(src), 0, ctypes.byref(blend), 2)

    def _free(self):
        if self._dc:
            _g.SelectObject(self._dc, self._old)
            _g.DeleteObject(self._bmp)
            _g.DeleteDC(self._dc)
            self._dc = self._bmp = None

    # ---- any thread
    def set_image(self, img, x=None, y=None):
        data = premultiplied_bgra(img)
        with self._lock:
            self._pending = (data, img.width, img.height, None if x is None else int(x), None if y is None else int(y))
        _u.PostMessageW(self.hwnd, WM_APP, 0, 0)

    def move(self, x, y):
        self.x, self.y = int(x), int(y)
        _u.SetWindowPos(self.hwnd, None, self.x, self.y, 0, 0, SWP_NOSIZE | SWP_NOZORDER | SWP_NOACTIVATE)

    def show(self):
        self.visible = True
        _u.ShowWindow(self.hwnd, SW_SHOWNOACTIVATE)

    def hide(self):
        self.visible = False
        _u.ShowWindow(self.hwnd, SW_HIDE)

    def close(self):
        if self.hwnd:
            _u.PostMessageW(self.hwnd, WM_DESTROY, 0, 0)


class NativeOrb:
    """What the bridge talks to. Positions are physical pixels.

    The orb and its reply bubble are drawn into ONE layered window: showing a reply just makes the window bigger on the
    bubble's side (the orb stays put on screen), and dragging moves that single window, so the bubble follows the orb
    for free."""

    def __init__(self, icon_path, on_click=None, on_exit=None, on_moved=None, on_bubble_hidden=None):
        _load()
        try:
            self._dpi_scale = max(1.0, _u.GetDpiForSystem() / 96.0)
        except Exception:
            self._dpi_scale = 1.0
        self._icon = icon_path
        self._size = orb_art.DIAM
        self.art = orb_art.OrbArt(icon_path, self._dpi_scale)
        self._on_click, self._on_exit, self._user_moved = on_click, on_exit, on_moved
        self.on_bubble_hidden = on_bubble_hidden
        self.win = LayeredWindow(draggable=True, delay_click=True, on_click=self._clicked, on_double=self._double, on_moved=self._moved)
        self.state, self.level = "idle", 0.0
        self._t0 = time.time()
        self._off = (0, 0)                  # where the orb sits inside the window
        self._canvas = (self.art.S, self.art.S)
        self._bubble = None                 # (image, x, y) inside the window, or None
        self._bubble_timer = None
        self._lock = threading.RLock()
        self._run = True
        self._last = None
        threading.Thread(target=self._loop, daemon=True, name="reo-orb-render").start()

    # geometry
    @property
    def size(self) -> int:
        return self.art.S

    def screen(self):
        r = wintypes.RECT()
        if _u.SystemParametersInfoW(0x30, 0, ctypes.byref(r), 0):          # SPI_GETWORKAREA
            return r.left, r.top, r.right - r.left, r.bottom - r.top
        return 0, 0, _u.GetSystemMetrics(0), _u.GetSystemMetrics(1)

    def position(self):
        """Screen position of the orb itself (not of the window, which may include the bubble)."""
        return self.win.x + self._off[0], self.win.y + self._off[1]

    # input (window thread)
    def _in_bubble(self, x, y):
        b = self._bubble
        return bool(b) and b[1] <= x < b[1] + b[0].width and b[2] <= y < b[2] + b[0].height

    def _clicked(self, x=0, y=0):
        if self._in_bubble(x, y):
            self.hide_bubble()                                   # a click on the bubble dismisses it
        elif self._on_click:
            self._on_click()

    def _double(self, x=0, y=0):
        if self._in_bubble(x, y):
            self.hide_bubble()
        elif self._on_exit:
            self._on_exit()

    def _moved(self, x, y):
        if self._user_moved:
            self._user_moved(*self.position())

    # orb
    def show(self, x, y):
        with self._lock:
            self._bubble, self._off, self._canvas = None, (0, 0), (self.art.S, self.art.S)
            self.win.x, self.win.y = int(x), int(y)
            self._last = None
            self._draw(force=True)
        self.win.show()

    def hide(self):
        self.hide_bubble()
        self.win.hide()

    def set_state(self, state):
        self.state = state if state in ("idle", "listening", "thinking") else "idle"

    def set_level(self, v):
        self.level = max(0.0, min(1.0, float(v)))

    def set_size(self, diameter: int):
        """Resize the orb (diameter in logical px, 150 = default), keeping its centre where it is."""
        diameter = int(diameter)
        if diameter == self._size:
            return
        with self._lock:
            self.hide_bubble()
            old = self.art.S
            cx, cy = self.win.x + old / 2, self.win.y + old / 2
            self.art = orb_art.OrbArt(self._icon, self._dpi_scale * diameter / orb_art.DIAM)
            self._size = diameter
            self._canvas = (self.art.S, self.art.S)
            self.win.x, self.win.y = int(cx - self.art.S / 2), int(cy - self.art.S / 2)
            self._last = None
            if self.win.visible:
                self._draw(force=True)

    def _draw(self, force=False, pos=None):
        with self._lock:
            t = time.time() - self._t0
            key = (self.state, round(self.level, 2), round(t * 30) if self.state != "idle" else 0)
            if not force and key == self._last:
                return
            self._last = key
            frame = self.art.frame(self.state, self.level, t)
            if self._bubble is None and self._canvas == frame.size:
                img = frame
            else:
                img = Image.new("RGBA", self._canvas, (0, 0, 0, 0))
                img.alpha_composite(frame, self._off)
                if self._bubble:
                    img.alpha_composite(self._bubble[0], (self._bubble[1], self._bubble[2]))
            if pos is None:
                self.win.set_image(img)
            else:
                self.win.set_image(img, pos[0], pos[1])

    def _loop(self):
        while self._run:
            try:
                if self.win.visible:
                    self._draw()
            except Exception:
                log.debug("orb draw failed", exc_info=True)
            time.sleep(1 / 30 if self.state != "idle" else 0.1)

    # bubble (same window)
    def _layout(self, img, tail_y):
        S = self.art.S
        return orb_art.compose_layout(S, self.position(), img.size, tail_y, self.screen(), self._dpi_scale,
                                      reach=self.art.D / 2 + 6 * self._dpi_scale)

    def show_bubble(self, text, timeout=None):
        with self._lock:
            img, tail_y = orb_art.render_bubble(text, "right", self._dpi_scale)
            side, canvas, off, boff, origin = self._layout(img, tail_y)
            if side == "left":
                img, tail_y = orb_art.render_bubble(text, "left", self._dpi_scale)
                side, canvas, off, boff, origin = self._layout(img, tail_y)
            self._bubble, self._canvas, self._off = (img, boff[0], boff[1]), canvas, off
            self._last = None
            self._draw(force=True, pos=origin)                   # the window grows; the orb stays where it is
        self.set_bubble_timeout(timeout if timeout is not None else min(40.0, max(6.0, 4 + len(text) * .06)))

    def set_bubble_timeout(self, seconds):
        if self._bubble_timer:
            self._bubble_timer.cancel()
        self._bubble_timer = None
        if seconds and self._bubble is not None:
            self._bubble_timer = threading.Timer(seconds, self.hide_bubble)
            self._bubble_timer.daemon = True
            self._bubble_timer.start()

    def hold_bubble(self):
        self.set_bubble_timeout(None)

    def hide_bubble(self):
        with self._lock:
            if self._bubble_timer:
                self._bubble_timer.cancel()
                self._bubble_timer = None
            if self._bubble is None:
                return
            ox, oy = self.position()
            self._bubble, self._canvas, self._off = None, (self.art.S, self.art.S), (0, 0)
            self._last = None
            if self.win.visible:
                self._draw(force=True, pos=(ox, oy))             # shrink back to just the orb
        if self.on_bubble_hidden:
            self.on_bubble_hidden()

    def close(self):
        self._run = False
        self.win.close()


def create(icon_path, **callbacks):
    if os.name != "nt":
        return None
    try:
        return NativeOrb(Path(icon_path), **callbacks)
    except Exception:
        log.exception("native orb unavailable")
        return None
