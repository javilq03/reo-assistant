"""What day and time Reo believes it is. A language model cannot know this, so the app tells it on every message.
Two sources: this computer's clock ("device", the default) or a date and time the user chose ("manual", which then keeps counting)."""
from __future__ import annotations

import time
from datetime import datetime, timedelta

_DAYS = {"en": ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"],
         "es": ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"]}
_MONTHS = {"en": ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"],
           "es": ["enero", "febrero", "marzo", "abril", "mayo", "junio", "julio", "agosto", "septiembre", "octubre", "noviembre", "diciembre"]}


def parse_manual(text: str) -> datetime | None:
    for fmt in ("%Y-%m-%dT%H:%M", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(str(text or "").strip(), fmt)
        except ValueError:
            continue
    return None


def now(settings, clock=time.time) -> tuple[datetime, str]:
    """(current datetime, 'device' | 'manual'). A chosen date and time keeps running from the moment it was set."""
    if str(settings.get("clock_mode") or "device") == "manual":
        base = parse_manual(settings.get("clock_manual"))
        if base:
            try:
                set_at = float(settings.get("clock_set_at") or clock())
            except (TypeError, ValueError):
                set_at = clock()
            return base + timedelta(seconds=max(0.0, clock() - set_at)), "manual"
    return datetime.fromtimestamp(clock()), "device"


def describe(dt: datetime, lang: str = "en") -> str:
    lang = lang if lang in _DAYS else "en"
    day, month = _DAYS[lang][dt.weekday()], _MONTHS[lang][dt.month - 1]
    if lang == "es":
        return f"{day} {dt.day} de {month} de {dt.year}, {dt:%H:%M}"
    return f"{day} {dt.day} {month} {dt.year}, {dt:%H:%M}"


def note(settings, lang: str | None = None, clock=time.time) -> str:
    dt, source = now(settings, clock)
    src = "this computer's clock" if source == "device" else "the date and time the user set in Reo's settings"
    zone = time.tzname[0] if source == "device" else "the user's local time"
    lines = [f"[CLOCK] Right now it is {describe(dt, 'en')} ({zone}); in Spanish: {describe(dt, 'es')}. Source: {src}.",
             "Use this for any question about the date, the day of the week or the time, and for words like today, tomorrow or yesterday. "
             "If the user says the day is different, the clock above still decides unless it says it was set manually; do not just agree. "
             "You have no internet: you cannot check weather, forecasts, news or prices. Say so plainly instead of inventing them."]
    return "\n\n" + "\n".join(lines)
