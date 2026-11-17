"""Reo is an offline language model: it cannot know the weather, a channel's latest stream, who owns an account, or what a web page says.
Asked anyway, a small model happily writes a confident, plausible answer. When a message asks for that kind of thing, the app adds a
blunt note next to the answer so the honest reply ("I can't look that up") wins over the made-up one."""
from __future__ import annotations

import re

_LIVE = re.compile(
    r"\b(?:weather|forecast|temperature|rain(?:ing)?|clima|tiempo\s+(?:hace|va\s+a\s+hacer)|previsi[oó]n|temperatura|llover[aá]|"
    r"news|noticias|headlines|score|resultado\s+del|who\s+won|qui[eé]n\s+gan[oó]|standings|clasificaci[oó]n|"
    r"price\s+of|stock|bitcoin|cotizaci[oó]n|precio\s+de(?:l)?|exchange\s+rate|"
    r"last\s+(?:stream|video|post|tweet|upload|match|game)|latest|[uú]ltim[oa]\s+(?:stream|v[ií]deo|directo|publicaci[oó]n|tuit|partido|episodio)|"
    r"is\s+\S+\s+(?:live|online)|est[aá]\s+\S+\s+(?:en\s+directo|conectad[oa])|"
    r"who\s+(?:owns|runs|made|is\s+the\s+owner)|qui[eé]n\s+(?:es\s+el\s+due[nñ]o|posee|gestiona|lleva)|propiedad\s+de|due[nñ]o\s+de|"
    r"channel|canal\s+de|twitch|youtube|instagram|tiktok|streamer|subscribers?|suscriptores|seguidores|followers|"
    r"(?:give|send|get|dame|p[aá]same|dime)\s+(?:me\s+)?(?:the\s+|el\s+|un\s+)?(?:link|url|enlace)|"
    r"search\s+(?:for|the\s+web|online)|busca\s+(?:en\s+internet|en\s+la\s+web|online)|look\s+(?:it\s+)?up|what\s+is\s+happening|qu[eé]\s+(?:est[aá]\s+pasando|ha\s+pasado))\b",
    re.I)
_SOURCE = re.compile(
    r"\b(?:where\s+(?:do|did)\s+you\s+(?:get|got|find|know)|how\s+do\s+you\s+know|what(?:'s|\s+is)\s+your\s+source|are\s+you\s+(?:online|connected)|"
    r"do\s+you\s+have\s+(?:internet|access)|de\s+d[oó]nde\s+(?:sacas|obtienes|has\s+sacado)|c[oó]mo\s+(?:lo\s+)?sabes|tienes\s+(?:internet|acceso)|"
    r"est[aá]s\s+conectado|qu[eé]\s+fuente|fuente\s+de|d[oó]nde\s+lo\s+(?:has\s+)?(?:le[ií]d|visto|encontr))", re.I)


def needs_live_data(text: str) -> bool:
    return bool(_LIVE.search((text or "").split("\n\n")[0]))


def asks_for_source(text: str) -> bool:
    return bool(_SOURCE.search((text or "").split("\n\n")[0]))


def note(text: str, computer_available: bool = False) -> str:
    """Per-message reminder; '' when the message does not ask for outside facts."""
    if asks_for_source(text):
        return ("\n\n[HONESTY] The user is asking where your information comes from. The truth: you are a language model running on their "
                "computer with NO internet and no live sources. Everything you say comes from training, not from a lookup. Earlier answers in this "
                "chat that sounded like a lookup (weather, channels, links, dates of streams) were NOT real: say that plainly, apologise briefly "
                "in your own style, and do not repeat any of those facts.")
    if needs_live_data(text):
        how = ("If they want, offer to open the right page in their browser (they must ask you to operate the computer)."
               if computer_available else "Offer to work with text they paste in.")
        return ("\n\n[NO LIVE DATA] This message asks for something you cannot know: live or specific real-world facts (weather, news, "
                "streams, channels, owners, prices, links, recent events). You have no internet. Do NOT answer with numbers, names, dates, "
                "owners or URLs, and do not write 'checking' or 'revisando'. Say in one plain sentence that you can't look that up, in your "
                f"usual tone, and, if it was a person or channel you do not recognise, that you do not know them. {how} "
                "Any earlier answer in this chat that looked like a lookup was invented: do not build on it.")
    return ""
