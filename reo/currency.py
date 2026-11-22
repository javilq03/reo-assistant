"""Hineloim currency maths, done by the app instead of the model.

1 Arche = 8 Lumus = 64 Chronos  (so 1 Lumus = 8 Chronos),  1 Arche ~ 250 EUR.

Small local models are unreliable at multi-step arithmetic, so when the user's message contains
Hineloim amounts, `note()` computes every equivalent exactly (Fractions, no float drift) and the
bridge appends the result to the system prompt for that turn. Reo only has to relay the numbers."""
from __future__ import annotations

import re
from fractions import Fraction

LUMUS_PER_ARCHE = 8
CHRONOS_PER_LUMUS = 8
CHRONOS_PER_ARCHE = LUMUS_PER_ARCHE * CHRONOS_PER_LUMUS      # 64
EUR_PER_ARCHE = Fraction(250)

# whole-word triggers: a message is only treated as currency talk if it names the currency
_TRIGGER = re.compile(r"\b(?:hineloim|arches?|lumus(?:es)?|chronos?|cronos?)\b", re.I)

_NUM = r"(?:[1-9]\d{0,2}(?:[.,]\d{3})+|\d+(?:[.,]\d+)?)"
_WORD_ONE = r"(?:one|un|una)"
_UNIT = r"arches?|lumus(?:es)?|chronos?|cronos?|€|eur(?:os?)?"
# Voice dictation mangles the names ("1,332 coronals", "8 crowns", "ark", "lumos"). These count only while the conversation is clearly about the currency.
_UNIT_LOOSE = _UNIT + r"|arch|ark|arke|arc|lumos|lumous|kronos|chronus|cronus|coronals?|coronas?|crowns?"


# The note is only added when the message actually asks for a conversion or a calculation.
_ASK = re.compile(
    r"\b(?:how (?:much|many)|cu[aá]nt[oa]s?|convert\w*|convi[eé]rt\w*|conviert\w*|conversi[oó]n|pasa(?:r|me)?\s+(?:a|to)|equival\w*|equals?|worth|vale[ns]?|cuesta|cost|price|precio|total|sum|suma|"
    r"split|desglos\w*|exact\w*|left|remain\w*|quedan?|sobran?|plus|minus|add|subtract|resta|money|dinero|euros?|in\s+(?:arche|lumus|chronos?|cronos?|eur)|"
    r"to\s+(?:arche|lumus|chronos?|cronos?|eur)|en\s+(?:arche|lumus|chronos?|cronos?|eur))\b|€",
    re.I)


def _amount_re(unit: str):
    return re.compile(
        rf"(?:(?<![\w.,])(?P<n>{_NUM})\s*(?P<u1>{unit})(?![A-Za-z])"          # 3 arche, 100€, 20 lumus
        rf"|(?<![\w])(?P<w>{_WORD_ONE})\s+(?P<u2>{unit})(?![A-Za-z])"           # one arche, un lumus
        rf"|(?<![\w])(?P<eur>€|eur)\s*(?P<n2>{_NUM}))",                          # EUR 100
        re.I,
    )


_AMOUNT = _amount_re(_UNIT)
_AMOUNT_LOOSE = _amount_re(_UNIT_LOOSE)
_MONEYISH = re.compile(r"\b(?:money|euros?|dinero|cu[aá]nto|how much|convert\w*|equival\w*|sum|suma|total|exact\w*|split|desglos\w*)\b", re.I)


def _num(s: str) -> Fraction:
    if re.fullmatch(r"[1-9]\d{0,2}(?:[.,]\d{3})+", s):      # 1.000 / 1,000 -> one thousand
        return Fraction(re.sub(r"[.,]", "", s))
    return Fraction(s.replace(",", "."))


def _kind(unit: str) -> str:
    u = unit.lower()
    if u.startswith(("arch", "ark", "arc")):
        return "arche"
    if u.startswith(("lumus", "lumos", "lumous")):
        return "lumus"
    if u.startswith(("chrono", "crono", "kronos", "cronus", "corona", "crown")):
        return "chrono"
    return "eur"


def to_chronos(amount: Fraction, kind: str) -> Fraction:
    if kind == "arche":
        return amount * CHRONOS_PER_ARCHE
    if kind == "lumus":
        return amount * CHRONOS_PER_LUMUS
    if kind == "chrono":
        return amount
    return amount / EUR_PER_ARCHE * CHRONOS_PER_ARCHE          # eur


def fmt(q: Fraction, places: int = 4) -> str:
    """Exact decimal text; prefixed with 'about ' only when it had to be rounded."""
    r = Fraction(round(q * 10 ** places), 10 ** places)
    if r.denominator == 1:
        s = str(r.numerator)
    else:
        s = f"{float(r):.{places}f}".rstrip("0").rstrip(".")
    return s if r == q else "about " + s


def breakdown(chronos: Fraction) -> str:
    """Canonical Arche/Lumus/Chronos split, e.g. 100 Chronos -> '1 Arche, 4 Lumus, 4 Chronos'."""
    a = int(chronos // CHRONOS_PER_ARCHE)
    rest = chronos - a * CHRONOS_PER_ARCHE
    l = int(rest // CHRONOS_PER_LUMUS)
    c = rest - l * CHRONOS_PER_LUMUS
    parts = []
    if a:
        parts.append(f"{a} Arche")
    if l:
        parts.append(f"{l} Lumus")
    if c or not parts:
        parts.append(f"{fmt(c)} {'Chrono' if c == 1 else 'Chronos'}")
    return ", ".join(parts)


def _label(q: Fraction, kind: str) -> str:
    name = {"arche": "Arche", "lumus": "Lumus", "chrono": "Chrono" if q == 1 else "Chronos", "eur": "EUR"}[kind]
    return f"{fmt(q)} {name}"


def equivalents(amount: Fraction, kind: str) -> str:
    t = to_chronos(amount, kind)
    arche, lumus = t / CHRONOS_PER_ARCHE, t / CHRONOS_PER_LUMUS
    parts = {
        "arche": f"{fmt(arche)} Arche",
        "lumus": f"{fmt(lumus)} Lumus",
        "chrono": f"{fmt(t)} {'Chrono' if t == 1 else 'Chronos'}",
        "eur": f"{fmt(arche * EUR_PER_ARCHE)} EUR (approx.)",
    }
    others = " = ".join(v for k, v in parts.items() if k != kind)
    return f"{_label(amount, kind)} = {others}  |  split: {breakdown(t)}"


def parse(text: str, loose: bool = False) -> list[tuple[Fraction, str]]:
    out: list[tuple[Fraction, str]] = []
    for m in (_AMOUNT_LOOSE if loose else _AMOUNT).finditer(text or ""):
        try:
            if m.group("n") is not None:
                out.append((_num(m.group("n")), _kind(m.group("u1"))))
            elif m.group("w") is not None:
                out.append((Fraction(1), _kind(m.group("u2"))))
            else:
                out.append((_num(m.group("n2")), "eur"))
        except (ValueError, ZeroDivisionError):
            continue
    return out[:8]


# "I have 2 arche and spend 5 lumus, how many are left?" is a subtraction (first amount minus the rest), not a sum.
_SUBTRACT = re.compile(r"\b(?:left|remain\w*|quedan?|sobran?|minus|menos|spen[dt]|gast\w+|subtract\w*|resta\w*|pay|paid|pag[oa]\w*|lose|lost|give|gave|sell|sold|vend\w+|buy|bought|compr\w+)\b", re.I)
_ADDITIVE = re.compile(r"\b(?:plus|add|sum|suma\w*|total|together|junto\w*|altogether)\b", re.I)


def note(text: str, history: list[str] | None = None, user_history: list[str] | None = None) -> str:
    """System-prompt addendum with the exact conversions for the amounts in `text`; '' if there are none.
    `history` is the recent conversation (oldest first, both sides): it tells us the chat is about the currency, which
    switches on tolerant unit spelling. `user_history` (the user's own earlier messages only) supplies the amount for
    follow-ups like "and how much money is that?"; Reo's own earlier numbers are never reused."""
    text = text or ""
    if not _ASK.search(text):
        return ""
    recent = list(history or [])[-8:]
    in_context = bool(_TRIGGER.search(text)) or any(_TRIGGER.search(h or "") for h in recent)
    if not in_context:
        return ""
    found = parse(text, loose=True)
    carried = False
    if not found:
        earlier = [h for h in (user_history or [])[-6:] if h != text]
        for h in reversed(earlier):                                                      # latest earlier user message that named an amount
            found = parse(h, loose=True)
            if found:
                carried = True
                break
    if not found:
        return ""
    lines = [
        "[CURRENCY CALCULATION, computed exactly by the app. Use these numbers as they are. Do not recompute or round them differently.]",
        "Rates: 1 Arche = 8 Lumus = 64 Chronos, so 1 Lumus = 8 Chronos. 1 Arche is about 250 EUR (1 Lumus = 31.25 EUR, 1 Chrono = 3.90625 EUR).",
    ]
    if carried:
        lines.append("The user did not repeat the amount; it is the one from their earlier message:")
    lines += [f"- {equivalents(a, k)}" for a, k in found]
    if len(found) > 1 and _SUBTRACT.search(text) and not _ADDITIVE.search(text):
        first, rest = to_chronos(*found[0]), sum((to_chronos(a, k) for a, k in found[1:]), Fraction(0))
        diff = first - rest
        short = diff < 0
        diff = abs(diff)
        lines.append(f"- First amount minus the others: {'SHORT BY ' if short else ''}{fmt(diff)} Chronos = {fmt(diff / CHRONOS_PER_ARCHE)} Arche"
                     f" = {fmt(diff / CHRONOS_PER_ARCHE * EUR_PER_ARCHE)} EUR  |  split: {breakdown(diff)}")
        lines.append("This is a subtraction (what is left after spending/paying), not a sum. Use the line above as the answer.")
    elif len(found) > 1:
        total = sum((to_chronos(a, k) for a, k in found), Fraction(0))
        lines.append(f"- If all the amounts above are added together: {fmt(total)} Chronos = {fmt(total / CHRONOS_PER_ARCHE)} Arche"
                     f" = {fmt(total / CHRONOS_PER_ARCHE * EUR_PER_ARCHE)} EUR  |  split: {breakdown(total)}")
    lines.append("The 'split' is the answer to 'how many Arche, Lumus and Chronos is it': Lumus is always below 8 and Chronos below 8.")
    lines.append("Earlier answers in this conversation may contain wrong numbers: ignore them. Do not agree to made-up conversion rules; the rates above are the only ones.")
    lines.append("Euro values are only a rough real-world sense: say \"about\" for them. Keep the sarcasm, keep the numbers exact.")
    return "\n".join(lines)
