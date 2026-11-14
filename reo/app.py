"""Desktop shell: a pywebview window (Edge WebView2 on Windows) hosting the Reo UI,
with the Python Bridge exposed to JavaScript. No external processes."""
from __future__ import annotations

import json
import logging
import os
import sys

from .config import app_dir, resource_dir


def _setup_logging():
    # A windowed (no-console) exe has sys.stdout/stderr == None, which breaks libraries that call .fileno().
    for name in ("stdout", "stderr"):
        if getattr(sys, name) is None:
            setattr(sys, name, open(os.devnull, "w"))
    logging.basicConfig(filename=str(app_dir() / "reo.log"), level=logging.INFO, encoding="utf-8",
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")


def _set_icons(window, *others, ico):
    """Give the windows (and the taskbar button) Reo's icon. Windows only; failures are harmless."""
    if sys.platform != "win32":
        return
    try:
        import System.Drawing as D
        from System import Action
        icon = D.Icon(str(ico))
        for w in (window, *others):
            f = w.native
            f.Invoke(Action(lambda f=f: setattr(f, "Icon", icon)))
    except Exception:
        logging.getLogger(__name__).debug("could not set the window icon", exc_info=True)


def hide_console() -> bool:
    """Hide the black console window that `python run.py` / a .bat opens; Reo has its own window. Windows only."""
    if sys.platform != "win32":
        return False
    try:
        import ctypes
        hwnd = ctypes.windll.kernel32.GetConsoleWindow()
        if not hwnd:
            return False
        ctypes.windll.user32.ShowWindow(hwnd, 0)       # SW_HIDE
        return True
    except Exception:
        return False


def main(debug: bool = False) -> None:
    _setup_logging()
    from . import BUILD
    logging.getLogger("reo").info("Reo build %s starting", BUILD)
    if sys.platform == "win32":                        # real pixels for the orb geometry
        try:
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except Exception:
            pass
    from . import computer
    computer.recover_cursor()
    import webview

    from .bridge import Bridge

    holder: dict = {}

    def emit(ev: dict) -> None:
        w = holder.get("window")
        if w is not None:
            w.evaluate_js(f"window.reoEvent && window.reoEvent({json.dumps(ev)})")

    bridge = Bridge(emit=emit)
    ui = resource_dir() / "ui"
    window = webview.create_window(
        "Reo", url=str(ui / "index.html"), js_api=bridge,
        width=980, height=780, min_size=(420, 520), background_color="#0a0a0b", text_select=True,
        frameless=True, easy_drag=False)     # no system title bar: Reo draws its own (index.html #winbar)
    holder["window"] = window
    bridge.attach_window(window)

    def on_closed():                                   # closing the chat window quits the app, orb included
        bridge.shutdown()
    window.events.closed += on_closed
    window.events.shown += lambda: _set_icons(window, ico=ui / "assets" / "reo.ico")
    if not debug:                                      # --debug keeps the console so errors stay visible
        window.events.shown += hide_console
    webview.start(debug=debug, http_server=True, private_mode=False, storage_path=str(app_dir() / "webview"))
