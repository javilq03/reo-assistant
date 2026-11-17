"""Which language is the user writing in right now? Reo answers in that one, message by message (Spanish or English).
A small word-list check is enough here and works offline with no extra package."""
from __future__ import annotations

import re

_ES = set("""el la los las un una unos unas de del que y en es son está estás estoy eres soy por con para qué cómo cuándo dónde quién cuál
quiero quieres puedes puede puedo abre abrir ábreme cierra cerrar cierra dime dame haz hazlo hola gracias favor vale oye colega
muy más pero también ahora entonces porque aquí allí eso esto este esta ese esa mi mis tu tus su sus nos yo tú él ella ellos nuestro
tengo tienes tiene hay hacer hace busca buscar pon pone ponme mata matar cuánto cuántos cuántas cuánta dinero lumus arche
nada algo todo todos bien mal sí ya otra vez siempre nunca que se lo al le les mas dices dice dijo hablame habla hablas entiendo entiende
escuchando escucho escuchas canción inglés español porque pasa pasó abres cierras ayuda ayúdame sabes sé saber ves ver veo""".split())
_EN = set("""the and is are was were to of you your what how when where who which open close please can could would should me my it its that this
these with for do does did not don't dont i i'm im want tell give make find search kill quit hello hi thanks thank okay just
very more but also now then because here there them they we our have has had get got there's what's how's let's about from
into over after before again always never nothing something everything""".split())
_WORD = re.compile(r"[a-záéíóúüñ']+", re.I)


def detect(text: str) -> str | None:
    """'es', 'en' or None when the text gives no clue (a bare name, a number, an emoji)."""
    t = (text or "").lower()
    words = _WORD.findall(t)
    es = sum(w in _ES for w in words) + 2 * bool(re.search(r"[¿¡ñ]", t)) + bool(re.search(r"[áéíóú]", t))
    en = sum(w in _EN for w in words)
    if es == en:
        return None
    return "es" if es > en else "en"


def of_conversation(user_messages: list[str]) -> str | None:
    """Language of the newest user message that has a clear one (a short "ok" or "Steam" keeps the previous language)."""
    for m in reversed(user_messages or []):
        lang = detect((m or "").split("\n\n")[0][:300])               # the typed text comes first; attached file contents follow
        if lang:
            return lang
    return None


_NAMES = {"es": "Spanish", "en": "English"}


def note(user_messages: list[str]) -> str:
    lang = of_conversation(user_messages)
    if not lang:
        return ""
    other = "English" if lang == "es" else "Spanish"
    return (f"\n\n[LANGUAGE] The user's latest message is in {_NAMES[lang]}. Write your whole reply in {_NAMES[lang]}, "
            f"even if earlier messages in this chat were in {other}. If their next message is in another language, follow that one instead. "
            f"Keep names such as Reo, Arche, Lumus and Chronos as they are.")


_SUFFIX = {"es": "\n\n[Responde en español.]", "en": "\n\n[Reply in English.]"}


def tag_last_user(history: list[dict], user_messages: list[str]) -> list[dict]:
    """Copy of `history` whose final user turn ends with a short language reminder, for the model only (never stored or shown).
    Small local models follow what is right next to their answer far better than a line at the top of a long prompt."""
    code = of_conversation(user_messages)
    if not code:
        return history
    out = list(history)
    for i in range(len(out) - 1, -1, -1):
        if out[i].get("role") == "user":
            out[i] = dict(out[i], text=(out[i].get("text") or "") + _SUFFIX[code])
            break
    return out
