"""Conversation settings that change how Reo answers: communication style, brief replies, reply language and the
hidden ";listen" follow-up tag. Everything here only adds text to the system prompt for that turn."""
from __future__ import annotations

import re

# Neutral keeps Reo's own persona untouched. The others are tone layers on top of it; accuracy rules always stay.
STYLES: dict[str, tuple[str, str]] = {
    "neutral": ("Neutral", ""),
    "romantic": ("Romantic", "Speak warmly and affectionately, a little flirty and sweet, like someone fond of the user. "
                             "Soften the sarcasm into teasing. Stay tasteful: nothing explicit. Keep every number and result exact."),
    "business": ("Business", "Speak as a professional assistant: formal, precise, structured and neutral. No sarcasm, no slang, "
                             "and do not use the word 'colega'. Use short headings or bullet points when they help. Keep every number and result exact."),
    "wild": ("Wild", "Speak with chaotic, high energy: exaggerated, silly, very informal, quick jokes and unexpected comparisons. "
                     "Never hateful or harmful, and never at the cost of accuracy: numbers and results stay exact."),
}

REPLY_LANGUAGES: dict[str, str] = {
    "auto": "Automatic", "en": "English", "es": "Spanish", "fr": "French", "de": "German", "it": "Italian",
    "pt": "Portuguese", "ja": "Japanese", "zh": "Chinese", "ko": "Korean", "ru": "Russian",
}

LISTEN_TAG = ";listen"
_LISTEN_RE = re.compile(r"\s*;listen\s*$", re.I)


def strip_listen(text: str) -> tuple[str, bool]:
    """(text without the trailing ;listen tag, whether it was there)."""
    t = text or ""
    if _LISTEN_RE.search(t):
        return _LISTEN_RE.sub("", t).rstrip(), True
    return t, False


def note(settings) -> str:
    """Prompt addendum for the user's style, brevity, language and follow-up choices ('' when everything is default)."""
    out = ""
    style = str(settings.get("comm_style") or "neutral")
    text = STYLES.get(style, STYLES["neutral"])[1]
    if text:
        out += f"\n\n[COMMUNICATION STYLE: {STYLES[style][0].upper()}] {text}"
    if settings.get("brief_replies"):
        out += ("\n\n[BREVITY] Answer simple messages (greetings, yes/no, quick facts, small talk) in one or two short sentences. "
                "Only write more when the user asks for detail or the task needs it.")
    code = str(settings.get("reply_language") or "auto")
    if code != "auto" and code in REPLY_LANGUAGES:
        name = REPLY_LANGUAGES[code]
        out += f"\n\n[REPLY LANGUAGE] Always write your replies in {name}, whatever language the user writes in. Keep names such as Reo, Arche, Lumus and Chronos as they are."
    if settings.get("wake_word") and settings.get("wake_follow_up"):
        out += (f"\n\n[FOLLOW-UP LISTENING] When your reply asks the user a question and you expect an answer right away, end it with the exact text {LISTEN_TAG} "
                "(nothing after it). The app hides it and keeps the microphone open for a few seconds. Never mention it and never use it otherwise.")
    return out
