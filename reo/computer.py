"""Computer Control: lets Reo operate the Windows desktop (mouse, keyboard, windows) from actions in his replies.

Protocol: the model writes  <action>{"do": "click", "x": 100, "y": 200}</action>  blocks, Reo executes them and sends the
results back, and the model continues until it answers without an action block (see Bridge._generate).
While Reo is acting, the arrow cursor turns gold, and pressing Esc stops him immediately."""
from __future__ import annotations

import atexit
import json
import logging
import math
import os
import re
import threading
import time
from typing import NamedTuple

log = logging.getLogger("reo.computer")

CURSOR_SCALE = 1.75       # Reo's gold cursor is this much bigger than the normal arrow
MAX_STEPS = 20            # model turns per request
MAX_ACTIONS = 8           # actions per turn

PROMPT = """

Computer Control is ON and the user has just asked you to do something on their computer, so you may use actions now. (In a later message where they do not ask for an action, you will not be given this section: just talk.)

Put one or more JSON actions in your reply, each wrapped like <action>{{"do": "app.open", "name": "notepad"}}</action>, then stop. The results come back in the next message and you continue. When the task is finished, answer in plain text with no action block. Screen: {w}x{h} pixels, origin at the top left. You cannot see the screen. Prefer SEMANTIC actions; use primitives only when you need pixel-level control. Work in small steps and check results.

SEMANTIC (prefer these):
- {{"do":"app.open","name":"calculator"}}        // app name, .exe, or Start-menu name. The result tells you if a window really appeared
- {{"do":"app.check","name":"calculator"}}       // is a window of that app open right now?
- {{"do":"app.focus","title":"part of title"}}
- {{"do":"app.close","title":"part of title"}}
- {{"do":"browser.goto","url":"https://..."}}
- {{"do":"browser.search","query":"..."}}
- {{"do":"shell.run","command":"dir","cwd":"C:\\\\"}}  // cwd optional; output is returned
- {{"do":"file.read","path":"C:\\\\Users\\\\..."}}
- {{"do":"file.write","path":"...","text":"...","mode":"overwrite|append"}}
- {{"do":"file.open","path":"..."}}
- {{"do":"clipboard.get"}}
- {{"do":"clipboard.set","text":"..."}}
- {{"do":"notify","title":"...","body":"..."}}

PRIMITIVES (fallback):
- {{"do":"move","x":N,"y":N}}
- {{"do":"click","x":N,"y":N,"button":"left|right|middle","count":1}}
- {{"do":"drag","x1":N,"y1":N,"x2":N,"y2":N}}
- {{"do":"scroll","amount":N}}  // positive down; optional x, y
- {{"do":"type","text":"..."}}
- {{"do":"key","keys":"ctrl+c"}}  // enter, tab, esc, win, alt+tab, f5, up, down, ...
- {{"do":"open","target":"notepad or path or URL"}}  // legacy alias of app.open / file.open / browser.goto
- {{"do":"windows"}}, {{"do":"focus","title":"..."}}, {{"do":"cursor"}}, {{"do":"wait","seconds":N}}

Rules:
- Only use the do values listed above. Never invent verbs (no browser.click, no "$ Discord", no type as a shell command).
- One action object per <action> fence. After emitting actions, stop and wait for results.
- Esc stops you immediately; if that happens, stop and say so.

REPORTING RESULTS (important):
- Results that start with OPENED, CLOSED, FOCUSED or OPEN are confirmed by the host. Results that start with LAUNCHED, NOT CONFIRMED, NOT OPEN, FAILED, CLOSE NOT CONFIRMED or FOCUS NOT CONFIRMED are NOT confirmed.
- Tell the user exactly what the result says. Only say something is open/closed/done if the result confirms it.
- On NOT CONFIRMED: say you could not confirm it. You may check once with app.check or windows, then report what you found. Do not retry in a loop.
- On FAILED: say it failed and give the reason from the result.
- typed/clicked/pressed results only say which window received the input; you cannot see the screen, so do not claim the visible outcome.
- Never invent system errors, error messages, diagnostics, shutdowns, glitches or corruption. You know only what the result text says. If a result is unclear, say it was unclear."""

# Appended in place of PROMPT when the switch is on but the user's message does not ask for anything to be done on the computer.
IDLE_NOTE = ("\n\nComputer Control is switched on, but the user has NOT asked you to operate the computer in this message. "
             "Answer in plain text only and never write <action> tags. If they seem to want something done on the computer, "
             "tell them to ask for it directly (for example: \"open calculator\").")

# Added to every batch of results fed back to the model.
RESULTS_TAIL = ("\n[Report these results truthfully. OPENED/CLOSED/FOCUSED/OPEN are confirmed; LAUNCHED, NOT CONFIRMED, NOT OPEN, "
                "FAILED, CLOSE NOT CONFIRMED and FOCUS NOT CONFIRMED are not. Never say something worked unless it is confirmed. Never invent errors or diagnostics: only report what is written above.]")

# Lines the host writes into the chat. Reo must never write (or see) them: copied from the history they make him fake actions as plain text.
_HOST_LINE_RE = re.compile(r"^[ \t]*\*(?:Computer|Result|Stopped)\b[^\n]*\*[ \t]*$", re.M | re.I)
_CALC_TAG_RE = re.compile(r"\[\s*CURRENCY CALCULATION[^\]]*\]", re.I)


def strip_host_lines(text: str) -> tuple[str, bool]:
    """Remove host-style status lines (and echoed calculation tags) from text. Returns (clean text, whether any were found)."""
    out, n1 = _HOST_LINE_RE.subn("", text or "")
    out, n2 = _CALC_TAG_RE.subn("", out)
    return re.sub(r"\n{3,}", "\n\n", out).strip(), bool(n1 or n2)


def used_computer(text: str) -> bool:
    return bool(re.search(r"^[ \t]*\*Computer:", text or "", re.M))


_ACTION_RE = re.compile(r"<action>(.*?)</action>", re.S | re.I)


def prompt(w: int, h: int) -> str:
    return PROMPT.format(w=w, h=h)


# ---------------------------------------------------------------- intent gate: does THIS message ask Reo to operate the computer?
# Computer Control is a permission (the tray switch), not a mode. The host only gives Reo the action protocol, and only runs
# his actions, for a message that clearly asks for one. Chat, questions about how to do something, banter and attached file
# contents never qualify. Conservative on purpose: if it misses, the user rephrases ("open ...") or starts with /pc.
_ATTACH_RE = re.compile(r"\n*\[Attached file:[^\]]*\]\n~{3,}.*?\n~{3,}", re.S)
_LEAD = (r"(?:please|pls|plz|por favor|porfa|can you|could you|would you|will you|can u|puedes|podr[ií]as|me puedes|me podr[ií]as|"
         r"reo|hey|hi|hello|hola|oye|ok|okay|vale|now|ahora|then|luego|despu[eé]s|and|y|also|tambi[eé]n|just|ya|first|primero|"
         r"go ahead and|i want you to|i need you to|i'd like you to|need you to|want you to|quiero que|necesito que|me gustar[ií]a que)")
_START = rf"(?:^|[.!?¿¡;:,\n])\s*(?:{_LEAD}[,:]?\s+)*"
_SITE = r"(?=[^.!?\n]{0,60}?(?:https?://|\b[\w-]+\.(?:com|org|net|es|io|tv|gg|app|dev|co|me|ai|edu|gov|info)\b))"
_VERB = (
    r"(?:open(?!\s*(?:source\b|up\b|ai\b|minded\b|-|world\b))|launch|click|double[- ]?click|right[- ]?click|minimi[sz]e|maximi[sz]e|"
    r"scroll|drag|close(?!\s+(?:to|by|enough|friends?|call|up|together)\b)|press|type(?!\s+of\b)|"
    r"(?:go to|navigate to|browse to)\s+(?:the\s+)?(?:site\s+|page\s+|website\s+)?" + _SITE + r"|"
    r"abre|abrir|[aá]breme|abrime|abras|lanza|lanzar|cierra|cerrar|cierres|minimiza|maximiza|haz\s+(?:un\s+|doble\s+)?(?:clic|click)|"
    r"hacer\s+(?:un\s+)?(?:clic|click)|clica|pulsa|pulses|presiona|presiones|teclea|desplaza|arrastra|"
    r"(?:ve a|ir a|navega a|navega en|entra en)\s+(?:la\s+|el\s+)?(?:p[aá]gina\s+|web\s+|sitio\s+)?" + _SITE + r")\b"
)
_IMPERATIVE = re.compile(_START + _VERB, re.I)
_APPWORDS = (r"(?:notepad|bloc de notas|block de notas|word|excel|chrome|navegador|browser|terminal|cmd|powershell|ventana|window|"
             r"barra de b[uú]squeda|search bar|campo|field|box|cuadro)")
_ELSE = [re.compile(p, re.I) for p in (
    r"^\s*/pc\b",                                                                                   # explicit override
    r"\b(?:write|escribe|escribir|type|teclea)\b[^.\n]{0,80}\b(?:in|into|on|en)\s+(?:the\s+|a\s+|my\s+|el\s+|la\s+|un\s+|mi\s+)?" + _APPWORDS,
    r"\b(?:search|busca|buscar|googlea|look up|lookup)\b[^.\n]{0,60}\b(?:on|in|en|with|con)\s+(?:google|youtube|bing|the browser|chrome|edge|firefox|brave|opera|el navegador|internet|the web)\b",
    r"\b(?:use|usa|utiliza|using)\s+(?:the\s+|el\s+|la\s+)?(?:firefox|chrome|edge|brave|opera|browser|navegador|notepad|bloc de notas|calculator|calculadora|explorer)\s+(?:to|para)\b",
    r"\b(?:play|reproduce|pon|ponme)\b[^.\n]{0,60}\b(?:on|in|en)\s+(?:youtube|spotify)\b",
    r"\b(?:look up|lookup|visit|visita|entra a|abre)\s+(?:youtube|google|twitch|netflix|spotify|discord|reddit|twitter|github|gmail|whatsapp)\b",
    r"\b(?:run|ejecuta|execute)\s+(?:the\s+|el\s+|este\s+|that\s+)?(?:command|comando|script)\b",
    r"\b(?:run|ejecuta)\b[^.\n]{0,60}\b(?:in|en)\s+(?:the\s+|el\s+|la\s+)?(?:cmd|powershell|terminal|consola|shell)\b",
    r"\b(?:read|lee|leer)\s+(?:the\s+|el\s+|este\s+|ese\s+)?(?:file|archivo|fichero)\b",
    r"\b(?:clipboard|portapapeles)\b",
    r"\b(?:what|which|qu[eé]|cu[aá]les)\b[^.?!\n]*\b(?:windows|ventanas|apps|aplicaciones|programas)\b[^.?!\n]*\b(?:open|opened|abiert[oa]s?|running)\b",
    r"\b(?:show|send|make)\s+(?:me\s+)?a\s+notification\b|\b(?:mu[eé]strame|env[ií]ame|haz)\s+una\s+notificaci[oó]n\b",
)]
_PATH = re.compile(r"[A-Za-z]:\\[^\s\"']+")
_MORE_APPS = ("spotify", "discord", "steam", "obs", "vlc", "telegram", "whatsapp", "zoom", "teams", "slack", "vscode", "visual studio code",
              "minecraft", "epic games", "battle.net", "twitch", "paint", "photoshop", "blender", "unity")
_app_start_re = None


def _app_start() -> "re.Pattern":
    """'Start Spotify', 'run notepad', 'ejecuta discord', 'use Chrome': an imperative that names an app (built from APPS on first use)."""
    global _app_start_re
    if _app_start_re is None:
        names = sorted({n for syn, _l, _k in APPS for n in syn} | set(_MORE_APPS), key=len, reverse=True)
        alt = "|".join(re.escape(n) for n in names)
        verbs = r"(?:start|run|launch|use|inicia|iniciar|ejecuta|ejecutar|enciende|usa|utiliza|abre)"
        _app_start_re = re.compile(_START + verbs + rf"\s+(?:the\s+|el\s+|la\s+)?(?:{alt})\b", re.I)
    return _app_start_re
_PATH_VERB = re.compile(r"\b(?:read|lee|leer|write|escribe|open|abre|delete|borra|list|lista|run|ejecuta|show|muestra|copy|copia|move|mueve)\b", re.I)


# "try again", "it didn't work", "the calculator is not open": only counts right after Reo has just used the computer.
_RETRY = re.compile(
    r"\b(?:again|retry|otra vez|de nuevo|nuevamente|int[eé]ntalo|didn'?t work|did not work|doesn'?t work|not working|never opened|didn'?t open|did not open|"
    r"isn'?t open|is not open|not open|no funcion\w*|no (?:se )?(?:ha )?abri\w*|no abre|no est[aá] abiert\w*|sigue sin|still (?:not|nothing)|it comes off|try it)\b", re.I)


def wants_control(text: str, prev_used: bool = False) -> bool:
    """True when the user's own words in this message ask Reo to operate the computer.
    `prev_used`: Reo's previous reply used the computer, so a follow-up like "try again" or "it's not open" counts too."""
    t = _ATTACH_RE.sub("", text or "").strip()
    if not t:
        return False
    if prev_used and _RETRY.search(t):
        return True
    if _IMPERATIVE.search(t) or _app_start().search(t):
        return True
    if any(r.search(t) for r in _ELSE):
        return True
    return bool(_PATH.search(t) and _PATH_VERB.search(t))


def parse_actions(text: str) -> tuple[list[dict], str]:
    """Split a reply into its actions and the remaining text."""
    acts: list[dict] = []
    for m in _ACTION_RE.finditer(text or ""):
        raw = m.group(1).strip()
        try:
            a = json.loads(raw)
            acts.append(a if isinstance(a, dict) else {"do": "_invalid", "raw": raw})
        except ValueError:
            acts.append({"do": "_invalid", "raw": raw[:120]})
    return acts, _ACTION_RE.sub("", text or "").strip()


def describe(a: dict) -> str:
    d = str(a.get("do", "?")).lower()
    if d in ("move", "click", "double_click", "right_click"):
        where = f" at ({a['x']}, {a['y']})" if "x" in a and "y" in a else ""
        return f"{d}{where}"
    if d == "type":
        t = str(a.get("text", ""))
        return f'type "{t[:40]}{"..." if len(t) > 40 else ""}"'
    if d == "key":
        return f"press {a.get('keys', '')}"
    if d in ("open", "file.open"):
        return f"open {a.get('target') or a.get('path') or ''}".strip()
    if d in ("focus", "app.focus"):
        return f"focus {a.get('title', '')}".strip()
    if d == "app.open":
        return f"open app {a.get('name', '')}".strip()
    if d == "app.close":
        return f"close {a.get('title', '')}".strip()
    if d == "app.check":
        return f"check {a.get('name', '')}".strip()
    if d == "browser.goto":
        return f"goto {a.get('url', '')}".strip()
    if d == "browser.search":
        return f'search "{str(a.get("query", ""))[:40]}"'
    if d == "shell.run":
        return f"shell.run {a.get('command', '')}".strip()
    if d == "file.read":
        return f"read {a.get('path', '')}".strip()
    if d == "file.write":
        return f"write {a.get('path', '')}".strip()
    if d == "clipboard.get":
        return "clipboard get"
    if d == "clipboard.set":
        t = str(a.get("text", ""))
        return f'clipboard set "{t[:30]}{"..." if len(t) > 30 else ""}"'
    if d == "notify":
        return f"notify {a.get('title', '')}".strip()
    return d


# ---------------------------------------------------------------- key parsing (pure)
MODS = {"ctrl": 0x11, "control": 0x11, "shift": 0x10, "alt": 0x12, "win": 0x5B, "windows": 0x5B, "cmd": 0x5B, "super": 0x5B}
NAMED = {"enter": 0x0D, "return": 0x0D, "tab": 0x09, "esc": 0x1B, "escape": 0x1B, "space": 0x20, "backspace": 0x08, "delete": 0x2E, "del": 0x2E,
         "insert": 0x2D, "home": 0x24, "end": 0x23, "pageup": 0x21, "pgup": 0x21, "pagedown": 0x22, "pgdn": 0x22,
         "left": 0x25, "up": 0x26, "right": 0x27, "down": 0x28, "capslock": 0x14, "printscreen": 0x2C,
         "volumeup": 0xAF, "volumedown": 0xAE, "volumemute": 0xAD, "playpause": 0xB3}
EXTENDED = {0x21, 0x22, 0x23, 0x24, 0x25, 0x26, 0x27, 0x28, 0x2D, 0x2E, 0x5B, 0x5C}


def parse_keys(spec: str) -> tuple[list[int], int | None]:
    """"ctrl+shift+t" -> ([ctrl, shift], vk_of_t). Returns virtual-key codes; raises ValueError for unknown keys."""
    mods: list[int] = []
    key = None
    for tok in [t.strip().lower() for t in str(spec).split("+") if t.strip()]:
        if tok in MODS:
            mods.append(MODS[tok])
        elif tok in NAMED:
            key = NAMED[tok]
        elif re.fullmatch(r"f([1-9]|1[0-9]|2[0-4])", tok):
            key = 0x70 + int(tok[1:]) - 1
        elif len(tok) == 1 and tok.isalnum():
            key = ord(tok.upper())
        else:
            raise ValueError(f"unknown key '{tok}'")
    if key is None and not mods:
        raise ValueError("no key given")
    return mods, key


# ---------------------------------------------------------------- launching apps, and knowing whether it worked (pure logic; testable anywhere)
class Win(NamedTuple):
    title: str
    proc: str            # process image name without .exe, lower case ("" when unknown)
    cloaked: int = 0     # DWM cloak flags: 0 = really on screen; 2 = on another virtual desktop; 1/4 = hidden by the app / its host


class Target(NamedTuple):
    kind: str            # "file" (ShellExecute: exe, path, URL, protocol) | "appsfolder" (Start-menu app id)
    target: str
    keywords: tuple      # lower-case words that appear in the app's window title or process name
    label: str


# (names the user may say, what to launch, words in its window title / process name). Titles are localized, hence both languages.
APPS = [
    (("calculator", "calc", "calculadora", "calculadora de windows"), "calc.exe", ("calculator", "calculadora", "calc")),
    (("notepad", "bloc de notas", "block de notas", "bloc de notas de windows"), "notepad.exe", ("notepad", "bloc de notas")),
    (("paint", "mspaint", "ms paint"), "mspaint.exe", ("paint",)),
    (("file explorer", "explorer", "windows explorer", "explorador", "explorador de archivos", "this pc", "mi pc", "este equipo"), "explorer.exe",
     ("explorer", "explorador")),
    (("cmd", "command prompt", "simbolo del sistema", "símbolo del sistema"), "cmd.exe", ("cmd", "command prompt", "símbolo del sistema", "simbolo del sistema")),
    (("powershell", "windows powershell"), "powershell.exe", ("powershell",)),
    (("terminal", "windows terminal"), "wt.exe", ("terminal", "windowsterminal")),
    (("settings", "configuracion", "configuración", "ajustes"), "ms-settings:", ("settings", "configuración", "configuracion", "systemsettings")),
    (("task manager", "administrador de tareas", "taskmgr"), "taskmgr.exe", ("task manager", "administrador de tareas", "taskmgr")),
    (("snipping tool", "herramienta recortes", "recortes"), "snippingtool.exe", ("snipping", "recorte")),
    (("control panel", "panel de control"), "control.exe", ("control panel", "panel de control")),
    (("chrome", "google chrome"), "chrome.exe", ("chrome",)),
    (("edge", "microsoft edge"), "msedge.exe", ("edge",)),
    (("firefox",), "firefox.exe", ("firefox",)),
    (("word", "microsoft word"), "winword.exe", ("word", "winword")),
    (("excel", "microsoft excel"), "excel.exe", ("excel",)),
    (("powerpoint", "microsoft powerpoint"), "powerpnt.exe", ("powerpoint", "powerpnt")),
]


def _norm(name: str) -> str:
    n = str(name or "").strip().strip("\"'").lower()
    n = re.sub(r"^(?:the|el|la|windows)\s+", "", n)
    n = re.sub(r"\s+(?:app|application|aplicaci[oó]n|program|programa)$", "", n)
    return n.strip()


def keywords_for(name: str) -> tuple:
    n = _norm(name)
    for names, _launch, kws in APPS:
        if n in names or n.removesuffix(".exe") in names:
            return kws
    stem = n.removesuffix(".exe")
    base = os.path.splitext(os.path.basename(stem))[0] if ("\\" in stem or "/" in stem) else stem
    return tuple(k for k in dict.fromkeys((n, base)) if len(k) >= 3)


def matches(keywords: tuple, w: Win) -> bool:
    t, p = w.title.lower(), w.proc.lower()
    return any(k in t or k in p for k in keywords)


def pick_start_app(apps: list[dict], query: str) -> tuple[str, str] | None:
    """Best Start-menu entry for a spoken name: exact name, then names starting with it, then names containing it."""
    q = _norm(query)
    if not q:
        return None
    rows = [(str(a.get("Name", "")), str(a.get("AppID", ""))) for a in apps if a.get("Name") and a.get("AppID")]
    for test in (lambda n: n.lower() == q, lambda n: n.lower().startswith(q), lambda n: q in n.lower()):
        hit = sorted((r for r in rows if test(r[0])), key=lambda r: len(r[0]))
        if hit:
            return hit[0]
    return None


def judge(keywords: tuple, before: dict, after: dict, fg_before: int, fg_after: int):
    """Compare window lists taken before and after a launch. Returns (status, Win|None).
    Only windows that are really on screen count (cloaked ones are hidden or on another virtual desktop).
    NEW: a new window matching the app.  FRONT: a matching window that was already open came to the front.
    EXISTING: a matching window was already open (nothing new).  HIDDEN: a matching window exists but is cloaked.
    UNSURE: only unrelated new windows.  NONE: nothing."""
    shown = {h: w for h, w in after.items() if not w.cloaked}
    new = [w for h, w in shown.items() if h not in before]
    for w in new:
        if matches(keywords, w):
            return "NEW", w
    fg = shown.get(fg_after)
    if fg is not None and fg_after != fg_before and matches(keywords, fg):
        return "FRONT", fg
    for h, w in shown.items():
        if h in before and matches(keywords, w):
            return "EXISTING", w
    for w in after.values():
        if w.cloaked and matches(keywords, w):
            return "HIDDEN", w
    if new:
        return "UNSURE", new[0]
    return "NONE", None


class AppLauncher:
    """Starts an app and only reports success when a window for it is really on screen.
    `ops` supplies: snapshot() -> ({hwnd: Win}, foreground_hwnd), launch(Target), find_start_app(name) -> (display, appid)|None,
    which(name) -> path|None, now(), sleep(s), check() (raises Stopped on Esc)."""

    def __init__(self, ops, timeout: float = 6.0):
        self.ops, self.timeout = ops, timeout

    def resolve(self, name: str) -> Target | None:
        raw = str(name or "").strip().strip("\"'")
        n = _norm(raw)
        if not n:
            return None
        for names, launch, kws in APPS:
            if n in names or n.removesuffix(".exe") in names:
                return Target("file", launch, kws, names[0])
        if "://" in raw or re.fullmatch(r"[a-z][a-z0-9+.-]+:", raw, re.I):
            return Target("file", raw, (), raw)
        if os.path.exists(raw):
            return Target("file", raw, keywords_for(raw), raw)
        found = self.ops.which(raw)
        if found:
            return Target("file", found, keywords_for(raw), raw)
        if not raw.lower().endswith((".exe", ".bat", ".cmd", ".lnk")):
            app = self.ops.find_start_app(raw)
            if app:
                display, appid = app
                return Target("appsfolder", appid, tuple(dict.fromkeys((display.lower(), n))), display)
        return None

    def open_app(self, name: str) -> str:
        t = self.resolve(name)
        if t is None:
            return (f"FAILED - no app called '{name}' was found (not a known app, not on PATH, not in the Start menu). "
                    "Nothing was launched. Tell the user you could not find it.")
        before, fg0 = self.ops.snapshot()
        try:
            self.ops.launch(t)
        except Exception as e:                                  # noqa: BLE001 - surface the OS error text to the model
            return f"FAILED - could not launch {t.label}: {e}"
        end = self.ops.now() + self.timeout
        status, win = "NONE", None
        while True:
            self.ops.sleep(0.25)
            self.ops.check()
            after, fg1 = self.ops.snapshot()
            status, win = judge(t.keywords, before, after, fg0, fg1)
            log.debug("launch %s: %s %s", t.label, status, win)
            if status in ("NEW", "FRONT") or self.ops.now() >= end:
                break
        if status == "NEW":
            return f"OPENED - '{win.title}' is now open on screen."
        if status == "FRONT":
            return f"OPENED - '{win.title}' was already running and is now in front."
        if status == "EXISTING":
            return (f"OPENED - '{win.title}' was already open (launching again did not start a new window), "
                    "so it may be behind other windows.")
        if status == "HIDDEN":
            where = "on another virtual desktop" if win.cloaked & 2 else "hidden (cloaked by Windows)"
            return (f"NOT CONFIRMED - a window for '{t.label}' exists ('{win.title}') but it is {where}, not on the screen the user is looking at. "
                    "Do not tell the user it is open; tell them where it is.")
        if status == "UNSURE":
            return (f"LAUNCHED - a new window appeared ('{win.title}') but its title does not match '{t.label}', so this is not confirmed. "
                    "Say it probably opened, not that it did.")
        return (f"NOT CONFIRMED - '{t.label}' was launched but no window for it appeared within {self.timeout:g}s. "
                "Do not tell the user it is open. You may check once with app.check, then say what you found.")

    def check_app(self, name: str) -> str:
        kws = keywords_for(name)
        if not kws:
            return "FAILED - missing app name"
        snap, _fg = self.ops.snapshot()
        hits = [w.title for w in snap.values() if matches(kws, w) and not w.cloaked]
        if hits:
            return "OPEN - " + "; ".join(f"'{h}'" for h in hits[:3])
        hidden = [w for w in snap.values() if matches(kws, w) and w.cloaked]
        if hidden:
            where = "on another virtual desktop" if hidden[0].cloaked & 2 else "hidden"
            return f"NOT OPEN - no window matching '{name}' is on screen; one exists ('{hidden[0].title}') but it is {where}."
        return f"NOT OPEN - no window matching '{name}' is on screen."


_STATUS_RE = re.compile(r"^(OPENED|CLOSED|FOCUSED|OPEN|LAUNCHED|NOT CONFIRMED|NOT OPEN|FAILED|CLOSE NOT CONFIRMED|FOCUS NOT CONFIRMED)\b")


def status_line(result: str) -> str:
    """The host-verified status of an action, shown to the user in the chat whatever the model says afterwards. '' for plain results."""
    first = (result or "").strip().splitlines()[0] if (result or "").strip() else ""
    return (first[:180] + ("..." if len(first) > 180 else "")) if _STATUS_RE.match(first) else ""


# ---------------------------------------------------------------- the gold arrow (Pillow; testable anywhere)
def gold_arrow(size: int = 32):
    """The standard arrow pointer, filled with Reo's gold texture. Returns (RGBA image, (hotspot_x, hotspot_y))."""
    from PIL import Image, ImageChops, ImageDraw
    from .orb_art import gold_gradient
    ss = 8
    n = size * ss
    k = n / 32
    pts = [(1, 1), (1, 23.5), (6.2, 18.8), (10, 27.5), (13.6, 26), (9.8, 17.6), (17, 17.6)]
    poly = [(x * k, y * k) for x, y in pts]
    mask = Image.new("L", (n, n), 0)
    ImageDraw.Draw(mask).polygon(poly, fill=255)
    outline = Image.new("L", (n, n), 0)
    ImageDraw.Draw(outline).polygon(poly, outline=255, fill=255, width=max(2, int(2.2 * k)))
    out = Image.new("RGBA", (n, n), (0, 0, 0, 0))
    out.paste(Image.new("RGBA", (n, n), (20, 14, 2, 255)), (0, 0), outline)       # dark edge
    inner = mask.resize((n, n))
    gold = gold_gradient(n, n)
    shrink = Image.new("L", (n, n), 0)
    ImageDraw.Draw(shrink).polygon(poly, fill=255)
    from PIL import ImageFilter
    inner = shrink.filter(ImageFilter.MinFilter(int(2 * k) | 1))
    out.paste(gold, (0, 0), inner)
    return out.resize((size, size), Image.LANCZOS), (max(0, int(round(1 * size / 32))), max(0, int(round(1 * size / 32))))


# ---------------------------------------------------------------- the real thing (Windows)
class WinComputer:
    def __init__(self):
        import ctypes
        from ctypes import wintypes
        self.c, self.wt = ctypes, wintypes
        self.u, self.g = ctypes.windll.user32, ctypes.windll.gdi32
        self.aborted = threading.Event()
        self._watch = None
        self._run = False
        self._self_esc = False
        self._cursor_on = False
        self._define_structs()
        self.launcher = AppLauncher(self)
        self._start_apps: list | None = None
        try:
            self._scale = max(1.0, self.u.GetDpiForSystem() / 96.0)
        except Exception:
            self._scale = 1.0

    available = True

    def _define_structs(self):
        c, w = self.c, self.wt

        class MOUSEINPUT(c.Structure):
            _fields_ = [("dx", w.LONG), ("dy", w.LONG), ("mouseData", w.DWORD), ("dwFlags", w.DWORD), ("time", w.DWORD), ("dwExtraInfo", c.c_size_t)]

        class KEYBDINPUT(c.Structure):
            _fields_ = [("wVk", w.WORD), ("wScan", w.WORD), ("dwFlags", w.DWORD), ("time", w.DWORD), ("dwExtraInfo", c.c_size_t)]

        class _U(c.Union):
            _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT)]

        class INPUT(c.Structure):
            _anonymous_ = ("u",)
            _fields_ = [("type", w.DWORD), ("u", _U)]

        self.MOUSEINPUT, self.KEYBDINPUT, self.INPUT = MOUSEINPUT, KEYBDINPUT, INPUT
        self.u.SendInput.argtypes = [w.UINT, c.POINTER(INPUT), c.c_int]
        self.u.GetAsyncKeyState.argtypes = [c.c_int]

    # ---- session: gold cursor + Esc watcher
    def begin(self):
        self.aborted.clear()
        self._run = True
        if self._watch is None or not self._watch.is_alive():
            self._watch = threading.Thread(target=self._watch_esc, daemon=True, name="reo-esc-watch")
            self._watch.start()
        self._gold_cursor(True)

    def end(self):
        self._run = False
        self._gold_cursor(False)

    def _watch_esc(self):
        prev = False
        while self._run:
            down = bool(self.u.GetAsyncKeyState(0x1B) & 0x8000)
            if down and not prev and not self._self_esc:
                self.aborted.set()
            prev = down
            time.sleep(0.015)

    def _gold_cursor(self, on: bool):
        try:
            if on and not self._cursor_on:
                self._cursor_on = True
                _marker().write_text("1")
                atexit.register(self._restore)
                self._set_gold()
            elif not on and self._cursor_on:
                self._cursor_on = False
                self._restore()
        except Exception:
            log.exception("cursor change failed")

    def _restore(self):
        self.u.SystemParametersInfoW(0x57, 0, None, 0)            # SPI_SETCURSORS: back to the user's own cursors
        try:
            _marker().unlink()
        except OSError:
            pass

    def _set_gold(self):
        c, w = self.c, self.wt
        from .orb_native import premultiplied_bgra
        size = round(max(32, int(self.u.GetSystemMetrics(13))) * CURSOR_SCALE)   # SM_CXCURSOR, enlarged
        img, (hx, hy) = gold_arrow(size)

        class BMIH(c.Structure):
            _fields_ = [("biSize", w.DWORD), ("biWidth", w.LONG), ("biHeight", w.LONG), ("biPlanes", w.WORD), ("biBitCount", w.WORD),
                        ("biCompression", w.DWORD), ("biSizeImage", w.DWORD), ("a", w.LONG), ("b", w.LONG), ("c", w.DWORD), ("d", w.DWORD)]

        class ICONINFO(c.Structure):
            _fields_ = [("fIcon", w.BOOL), ("xHotspot", w.DWORD), ("yHotspot", w.DWORD), ("hbmMask", w.HANDLE), ("hbmColor", w.HANDLE)]

        self.g.CreateDIBSection.restype = w.HANDLE
        self.g.CreateDIBSection.argtypes = [w.HDC, c.POINTER(BMIH), w.UINT, c.POINTER(c.c_void_p), w.HANDLE, w.DWORD]
        self.g.CreateBitmap.restype = w.HANDLE
        self.u.CreateIconIndirect.restype = w.HANDLE
        self.u.SetSystemCursor.argtypes = [w.HANDLE, w.DWORD]
        bits = c.c_void_p()
        bi = BMIH(c.sizeof(BMIH), size, -size, 1, 32, 0, 0, 0, 0, 0, 0)
        color = self.g.CreateDIBSection(None, c.byref(bi), 0, c.byref(bits), None, 0)
        c.memmove(bits, premultiplied_bgra(img), size * size * 4)
        mask = self.g.CreateBitmap(size, size, 1, 1, None)
        info = ICONINFO(False, hx, hy, mask, color)
        hcur = self.u.CreateIconIndirect(c.byref(info))
        self.g.DeleteObject(mask)
        self.g.DeleteObject(color)
        if hcur:
            self.u.SetSystemCursor(hcur, 32512)                  # OCR_NORMAL; the system owns hcur now

    # ---- input primitives
    def _send(self, *inputs):
        arr = (self.INPUT * len(inputs))(*inputs)
        self.u.SendInput(len(inputs), arr, self.c.sizeof(self.INPUT))

    def _mouse(self, flags, data=0):
        i = self.INPUT(0)
        i.mi = self.MOUSEINPUT(0, 0, data, flags, 0, 0)
        return i

    def _key(self, vk=0, scan=0, flags=0):
        i = self.INPUT(1)
        i.ki = self.KEYBDINPUT(vk, scan, flags, 0, 0)
        return i

    def screen_size(self):
        return self.u.GetSystemMetrics(0), self.u.GetSystemMetrics(1)

    def cursor_pos(self):
        p = self.wt.POINT()
        self.u.GetCursorPos(self.c.byref(p))
        return p.x, p.y

    def _clamp(self, x, y):
        w, h = self.screen_size()
        return max(0, min(w - 1, int(x))), max(0, min(h - 1, int(y)))

    def _check(self):
        if self.aborted.is_set():
            raise Stopped()

    def move(self, x, y):
        x, y = self._clamp(x, y)
        x0, y0 = self.cursor_pos()
        dist = math.hypot(x - x0, y - y0)
        steps = max(1, min(60, int(dist / 18)))
        for i in range(1, steps + 1):
            self._check()
            t = i / steps
            t = t * t * (3 - 2 * t)                              # ease in/out
            self.u.SetCursorPos(int(x0 + (x - x0) * t), int(y0 + (y - y0) * t))
            time.sleep(0.008)
        self.u.SetCursorPos(x, y)

    def click(self, button="left", count=1):
        down, up = {"left": (0x2, 0x4), "right": (0x8, 0x10), "middle": (0x20, 0x40)}.get(button, (0x2, 0x4))
        for _ in range(max(1, min(3, int(count)))):
            self._check()
            self._send(self._mouse(down))
            time.sleep(0.03)
            self._send(self._mouse(up))
            time.sleep(0.06)

    def press(self, spec):
        mods, key = parse_keys(spec)
        esc = key == 0x1B
        self._self_esc = esc
        try:
            seq = [self._key(m, 0, 1 if m in EXTENDED else 0) for m in mods]
            if key is not None:
                seq.append(self._key(key, 0, 1 if key in EXTENDED else 0))
                seq.append(self._key(key, 0, 2 | (1 if key in EXTENDED else 0)))
            seq += [self._key(m, 0, 2 | (1 if m in EXTENDED else 0)) for m in reversed(mods)]
            self._send(*seq)
            time.sleep(0.05)
        finally:
            if esc:
                time.sleep(0.1)
                self._self_esc = False

    def type_text(self, text):
        for ch in str(text)[:2000]:
            self._check()
            if ch in "\r\n":
                self._send(self._key(0x0D), self._key(0x0D, 0, 2))
            elif ch == "\t":
                self._send(self._key(0x09), self._key(0x09, 0, 2))
            else:
                units = ch.encode("utf-16-le")
                for k in range(0, len(units), 2):
                    u = units[k] | (units[k + 1] << 8)
                    self._send(self._key(0, u, 4), self._key(0, u, 4 | 2))
            time.sleep(0.012)

    def windows(self):
        out = []
        fg = self.u.GetForegroundWindow()
        proto = self.c.WINFUNCTYPE(self.wt.BOOL, self.wt.HWND, self.wt.LPARAM)

        def cb(h, _):
            if self.u.IsWindowVisible(h):
                n = self.u.GetWindowTextLengthW(h)
                if n:
                    buf = self.c.create_unicode_buffer(n + 1)
                    self.u.GetWindowTextW(h, buf, n + 1)
                    r = self.wt.RECT()
                    self.u.GetWindowRect(h, self.c.byref(r))
                    if r.right - r.left > 40 and not self.u.IsIconic(h):
                        out.append((buf.value, (r.left, r.top, r.right, r.bottom), h == fg))
            return True
        self.u.EnumWindows(proto(cb), 0)
        return out[:15]

    def _fg_title(self) -> str:
        try:
            h = self.u.GetForegroundWindow()
            n = self.u.GetWindowTextLengthW(h)
            buf = self.c.create_unicode_buffer(n + 1)
            self.u.GetWindowTextW(h, buf, n + 1)
            return buf.value
        except Exception:
            return ""

    def _find_window(self, title):
        want = str(title).lower()
        for name, _r, _fg in self.windows():
            if want and want in name.lower():
                h = self.u.FindWindowW(None, name)
                if h:
                    return name, h
        return None, None

    def focus(self, title):
        name, h = self._find_window(title)
        if not h:
            return f"FAILED - no window with '{title}' in its title"
        self.u.keybd_event(0x12, 0, 0, 0)                      # a tap of Alt lets SetForegroundWindow through
        self.u.keybd_event(0x12, 0, 2, 0)
        self.u.ShowWindow(h, 9)
        self.u.SetForegroundWindow(h)
        for _ in range(6):
            time.sleep(0.1)
            if self.u.GetForegroundWindow() == h:
                return f"FOCUSED - '{name}' is now the active window."
        return f"FOCUS NOT CONFIRMED - asked Windows to bring '{name}' to the front, but it is not the active window (now: '{self._fg_title()}')."

    def close_window(self, title):
        name, h = self._find_window(title)
        if not h:
            return f"FAILED - no window with '{title}' in its title"
        self.u.PostMessageW(h, 0x0010, 0, 0)                   # WM_CLOSE
        end = time.time() + 2.5
        while time.time() < end:
            self._check()
            time.sleep(0.1)
            if not self.u.IsWindow(h):
                return f"CLOSED - '{name}' is gone."
        return (f"CLOSE NOT CONFIRMED - asked '{name}' to close but its window is still open "
                "(it may be asking to save). Do not tell the user it is closed.")

    def clipboard_get(self):
        CF_UNICODETEXT = 13
        if not self.u.OpenClipboard(None):
            return "clipboard busy"
        try:
            h = self.u.GetClipboardData(CF_UNICODETEXT)
            if not h:
                return ""
            from ctypes import c_wchar_p
            p = self.c.windll.kernel32.GlobalLock(h)
            try:
                return c_wchar_p(p).value or ""
            finally:
                self.c.windll.kernel32.GlobalUnlock(h)
        finally:
            self.u.CloseClipboard()

    def clipboard_set(self, text):
        CF_UNICODETEXT = 13
        GMEM_MOVEABLE = 0x0002
        data = str(text)
        if not self.u.OpenClipboard(None):
            return "clipboard busy"
        try:
            self.u.EmptyClipboard()
            buf = self.c.create_unicode_buffer(data)
            nbytes = (len(data) + 1) * 2
            h = self.c.windll.kernel32.GlobalAlloc(GMEM_MOVEABLE, nbytes)
            p = self.c.windll.kernel32.GlobalLock(h)
            self.c.memmove(p, buf, nbytes)
            self.c.windll.kernel32.GlobalUnlock(h)
            self.u.SetClipboardData(CF_UNICODETEXT, h)
            return f"clipboard set ({len(data)} chars)"
        finally:
            self.u.CloseClipboard()

    def shell_run(self, command, cwd=None):
        import subprocess
        cmd = str(command or "").strip()
        if not cmd:
            return "empty command"
        # hard cap: no interactive shells, short timeout, truncated output
        try:
            r = subprocess.run(
                cmd, shell=True, cwd=cwd or None, capture_output=True, text=True,
                timeout=20, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            out = ((r.stdout or "") + (r.stderr or "")).strip()
            if len(out) > 2000:
                out = out[:2000] + "\n…(truncated)"
            return f"exit {r.returncode}" + (f"\n{out}" if out else "")
        except subprocess.TimeoutExpired:
            return "command timed out (20s)"
        except Exception as e:
            return f"shell failed: {e}"

    def file_read(self, path):
        from pathlib import Path
        p = Path(str(path))
        if not p.is_file():
            return f"not a file: {p}"
        if p.stat().st_size > 200_000:
            return f"file too large ({p.stat().st_size} bytes); max 200KB"
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except Exception as e:
            return f"read failed: {e}"
        if len(text) > 4000:
            return text[:4000] + "\n…(truncated)"
        return text or "(empty file)"

    def file_write(self, path, text, mode="overwrite"):
        from pathlib import Path
        p = Path(str(path))
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            data = str(text)
            if str(mode).lower() == "append":
                with p.open("a", encoding="utf-8") as f:
                    f.write(data)
            else:
                p.write_text(data, encoding="utf-8")
            size = p.stat().st_size
            return f"wrote {len(data)} chars to {p} (verified on disk: file is now {size} bytes)"
        except Exception as e:
            return f"write failed: {e}"

    def notify(self, title, body):
        # lightweight balloon via Shell_NotifyIcon is heavy; MessageBox is blocking.
        # Use a non-blocking PowerShell toast when available, else skip gracefully.
        import subprocess
        t = str(title or "Reo").replace("'", "''")
        b = str(body or "").replace("'", "''")
        ps = (
            "[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] > $null; "
            "$t = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent("
            "[Windows.UI.Notifications.ToastTemplateType]::ToastText02); "
            "$t.GetElementsByTagName('text').Item(0).AppendChild($t.CreateTextNode('%s')) > $null; "
            "$t.GetElementsByTagName('text').Item(1).AppendChild($t.CreateTextNode('%s')) > $null; "
            "[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('Reo').Show("
            "[Windows.UI.Notifications.ToastNotification]::new($t))"
        ) % (t, b)
        try:
            subprocess.run(
                ["powershell", "-NoProfile", "-Command", ps],
                timeout=8, capture_output=True,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            return "notified"
        except Exception as e:
            return f"notify failed: {e}"

    def app_open(self, name):
        return self.launcher.open_app(name)

    # ---- what AppLauncher needs from the OS
    def now(self):
        return time.time()

    def sleep(self, seconds):
        time.sleep(seconds)

    def check(self):
        self._check()

    def _cloaked(self, h) -> int:
        try:
            val = self.c.c_int(0)
            self.c.windll.dwmapi.DwmGetWindowAttribute(self.wt.HWND(h), 14, self.c.byref(val), self.c.sizeof(val))   # DWMWA_CLOAKED
            return int(val.value)
        except Exception:
            return 0

    def _proc_name(self, h) -> str:
        try:
            k32 = self.c.windll.kernel32
            pid = self.wt.DWORD(0)
            self.u.GetWindowThreadProcessId(self.wt.HWND(h), self.c.byref(pid))
            k32.OpenProcess.restype = self.wt.HANDLE
            hp = k32.OpenProcess(0x1000, False, pid.value)       # PROCESS_QUERY_LIMITED_INFORMATION
            if not hp:
                return ""
            try:
                buf = self.c.create_unicode_buffer(520)
                size = self.wt.DWORD(520)
                k32.QueryFullProcessImageNameW.argtypes = [self.wt.HANDLE, self.wt.DWORD, self.wt.LPWSTR, self.c.POINTER(self.wt.DWORD)]
                if k32.QueryFullProcessImageNameW(hp, 0, buf, self.c.byref(size)):
                    return os.path.splitext(os.path.basename(buf.value))[0].lower()
            finally:
                k32.CloseHandle(hp)
        except Exception:
            pass
        return ""

    def snapshot(self):
        """Every visible, titled top-level window (minimized ones included) as {hwnd: Win} with its cloak flags, plus the foreground hwnd."""
        out: dict = {}
        proto = self.c.WINFUNCTYPE(self.wt.BOOL, self.wt.HWND, self.wt.LPARAM)

        def cb(h, _):
            try:
                if self.u.IsWindowVisible(h) and self.u.GetWindowTextLengthW(h):
                    n = self.u.GetWindowTextLengthW(h)
                    buf = self.c.create_unicode_buffer(n + 1)
                    self.u.GetWindowTextW(h, buf, n + 1)
                    out[int(h)] = Win(buf.value, self._proc_name(h), self._cloaked(h))
            except Exception:
                pass
            return True
        self.u.EnumWindows(proto(cb), 0)
        return out, int(self.u.GetForegroundWindow() or 0)

    def which(self, name):
        import shutil
        n = str(name or "").strip()
        hit = shutil.which(n) or shutil.which(n + ".exe")
        if hit:
            return hit
        try:
            import winreg
            exe = n if n.lower().endswith(".exe") else n + ".exe"
            for root in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
                try:
                    with winreg.OpenKey(root, "SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\App Paths\\" + exe) as k:
                        val = winreg.QueryValueEx(k, "")[0]
                        if val and os.path.exists(os.path.expandvars(val.strip('"'))):
                            return os.path.expandvars(val.strip('"'))
                except OSError:
                    continue
        except Exception:
            pass
        return None

    def find_start_app(self, name):
        if self._start_apps is None:
            import subprocess
            self._start_apps = []
            try:
                r = subprocess.run(["powershell", "-NoProfile", "-Command", "Get-StartApps | ConvertTo-Json -Compress"],
                                   capture_output=True, text=True, timeout=15, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                data = json.loads(r.stdout or "[]")
                self._start_apps = data if isinstance(data, list) else [data]
            except Exception:
                log.debug("Get-StartApps failed", exc_info=True)
        return pick_start_app(self._start_apps, name)

    def launch(self, t: "Target"):
        if t.kind == "appsfolder":
            import subprocess
            subprocess.Popen(["explorer.exe", "shell:AppsFolder\\" + t.target], creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        else:
            os.startfile(t.target)  # noqa: S606

    # ---- one action
    def execute(self, a: dict) -> str:
        res = self._execute(a)
        log.info("action %s -> %s", json.dumps(a, ensure_ascii=False)[:300], str(res)[:300].replace("\n", " | "))
        return res

    def _execute(self, a: dict) -> str:
        do = str(a.get("do", "")).lower()
        try:
            self._check()
            # ---- semantic
            if do == "app.open":
                return self.app_open(a.get("name") or a.get("target") or "")
            if do == "app.check":
                return self.launcher.check_app(a.get("name") or a.get("title") or "")
            if do == "app.focus":
                return self.focus(a.get("title", ""))
            if do == "app.close":
                return self.close_window(a.get("title", ""))
            if do == "browser.goto":
                url = str(a.get("url") or a.get("target") or "").strip()
                if not url:
                    return "missing url"
                if not re.match(r"^[a-z]+://", url, re.I):
                    url = "https://" + url
                os.startfile(url)  # noqa: S606
                time.sleep(0.6)
                return f"LAUNCHED - sent {url} to the default browser. Whether the page loaded is not verified."
            if do == "browser.search":
                from urllib.parse import quote_plus
                q = str(a.get("query") or "").strip()
                if not q:
                    return "missing query"
                url = "https://www.google.com/search?q=" + quote_plus(q)
                os.startfile(url)  # noqa: S606
                time.sleep(0.6)
                return f"LAUNCHED - sent a Google search for {q!r} to the default browser. Whether the page loaded is not verified."
            if do == "shell.run":
                return self.shell_run(a.get("command", ""), a.get("cwd"))
            if do == "file.read":
                return self.file_read(a.get("path", ""))
            if do == "file.write":
                return self.file_write(a.get("path", ""), a.get("text", ""), a.get("mode", "overwrite"))
            if do == "file.open":
                path = str(a.get("path") or a.get("target") or "")
                if not path:
                    return "missing path"
                if not os.path.exists(path):
                    return f"FAILED - '{path}' does not exist."
                before, _ = self.snapshot()
                os.startfile(path)  # noqa: S606
                time.sleep(1.2)
                after, _ = self.snapshot()
                new = [w.title for h, w in after.items() if h not in before]
                if new:
                    return f"OPENED - '{new[0]}' appeared after opening {path}."
                return f"LAUNCHED - asked Windows to open {path}, but no new window appeared yet, so this is not confirmed."
            if do == "clipboard.get":
                text = self.clipboard_get()
                if len(text) > 2000:
                    return text[:2000] + "\n…(truncated)"
                return text if text != "" else "(empty clipboard)"
            if do == "clipboard.set":
                return self.clipboard_set(a.get("text", ""))
            if do == "notify":
                return self.notify(a.get("title", "Reo"), a.get("body", ""))

            # ---- primitives
            if do == "move":
                self.move(a["x"], a["y"])
                return f"moved to {self.cursor_pos()}"
            if do in ("click", "double_click", "right_click"):
                if "x" in a and "y" in a:
                    self.move(a["x"], a["y"])
                button = "right" if do == "right_click" else str(a.get("button", "left"))
                self.click(button, 2 if do == "double_click" else a.get("count", 1))
                return f"clicked at {self.cursor_pos()} (active window: '{self._fg_title()}')"
            if do == "drag":
                self.move(a["x1"], a["y1"])
                self._send(self._mouse(0x2))
                time.sleep(0.08)
                self.move(a["x2"], a["y2"])
                self._send(self._mouse(0x4))
                return f"dragged to {self.cursor_pos()}"
            if do == "scroll":
                if "x" in a and "y" in a:
                    self.move(a["x"], a["y"])
                amt = int(a.get("amount", 3))
                self._send(self._mouse(0x800, (-amt * 120) & 0xFFFFFFFF))
                return f"scrolled {amt}"
            if do == "type":
                txt = str(a.get("text", ""))
                self.type_text(txt)
                return f"typed {min(len(txt), 2000)} characters into the active window '{self._fg_title()}' (delivery to a text field is not visible to me)"
            if do == "key":
                self.press(a.get("keys", ""))
                return f"pressed {a.get('keys')} (active window: '{self._fg_title()}')"
            if do == "open":  # legacy: path / URL / app name
                return self.app_open(a.get("target") or a.get("name") or "")
            if do == "windows":
                return "; ".join(f"{'[active] ' if fg else ''}{n} at {r}" for n, r, fg in self.windows()) or "no windows"
            if do == "focus":
                return self.focus(a.get("title", ""))
            if do == "cursor":
                return f"cursor at {self.cursor_pos()}, screen {self.screen_size()}"
            if do == "wait":
                end = time.time() + min(5.0, float(a.get("seconds", 1)))
                while time.time() < end:
                    self._check()
                    time.sleep(0.05)
                return "waited"
            return f"unknown action '{do}'"
        except Stopped:
            raise
        except KeyError as e:
            return f"missing field {e}"
        except Exception as e:
            log.exception("action failed: %r", a)
            return f"FAILED - internal error ({type(e).__name__}: {e}). Nothing is confirmed; tell the user it failed."



class Stopped(Exception):
    """Esc (or the stop button) was pressed."""


def _marker():
    from .config import app_dir
    return app_dir() / "gold_cursor.flag"


def recover_cursor():
    """If Reo was killed while his gold cursor was active, give the user their own cursors back on the next start."""
    if os.name != "nt":
        return
    try:
        m = _marker()
        if m.exists():
            import ctypes
            ctypes.windll.user32.SystemParametersInfoW(0x57, 0, None, 0)
            m.unlink()
    except Exception:
        log.debug("cursor recovery failed", exc_info=True)


def make():
    if os.name != "nt":
        return None
    try:
        return WinComputer()
    except Exception:
        log.exception("computer control unavailable")
        return None
