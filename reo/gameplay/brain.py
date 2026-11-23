"""The thinking part of Gameplay: one vision call per turn, one text-and-log call per lost attempt.

`ask(system, text, images) -> str` is whatever model backend is in use (the cloud API for now; a local
vision model can be plugged in later without touching anything else). The model never sees pixel
coordinates: it can only name targets that exist in coords.json, and only `tap` and `wait` exist.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Callable

MAX_ACTIONS = 8
MAX_WAIT = 5.0

DECIDE_SYSTEM = """You are the player of a turn-based mobile game that runs on a PC. Each time you are shown a screenshot of the battle at the moment you can act, choose this turn's moves.
You can only tap named targets and wait. You cannot type, drag or press keys.
Reply with ONLY one JSON object, no prose and no code fences:
{"state": {"turn": <int or null>, "boss_hp_pct": <0-100 or null>, "ult_ready": <true|false|null>, "hand": [<short description per card>], "boss_telegraph": "<what the boss is about to do, or empty>"},
 "actions": [{"type": "tap", "target": "<one of the valid targets>"}, {"type": "wait", "seconds": <0-5>}],
 "reason": "<one short sentence>"}
Read the screen carefully; if something is unreadable, say null instead of guessing. Follow the rules and verified facts you are given. Rules marked "being tested" are experiments: follow them faithfully so they can be judged."""

REFLECT_SYSTEM = """You are the coach of a game-playing agent that just lost a boss fight. You are given its turn log, its current playbook and what is already known about the boss.
Work out the most likely cause of the loss, citing specific turns. Be conservative: card draws are random, so one loss is weak evidence.
Reply with ONLY one JSON object:
{"cause": "<one or two sentences citing turns>",
 "facts": ["<only things directly observed in the log, e.g. 'boss uses an area attack on turn 3'>"],
 "hypotheses": ["<ideas worth testing later>"],
 "changes": [{"op": "add", "text": "<new rule>"}, {"op": "edit", "id": "<rule id>", "text": "<new text>"}, {"op": "remove", "id": "<rule id>"}]}
At most 3 facts and at most 2 changes. Prefer small, specific changes over rewrites. Use an empty list when you have nothing solid."""


class BrainError(RuntimeError):
    pass


@dataclass
class Decision:
    state: dict = field(default_factory=dict)
    actions: list = field(default_factory=list)
    reason: str = ""
    raw: str = ""


def parse_json(text: str) -> dict:
    """The first JSON object in a model reply (tolerates code fences and surrounding prose)."""
    text = re.sub(r"```(?:json)?", "", str(text or ""))
    dec = json.JSONDecoder()
    for m in re.finditer(r"\{", text):
        try:
            obj, _ = dec.raw_decode(text[m.start():])
        except ValueError:
            continue
        if isinstance(obj, dict):
            return obj
    raise BrainError("The model did not reply with a JSON object.")


def validate_actions(actions, targets) -> tuple[list[dict], list[str]]:
    """Keep only whitelisted actions. Returns (valid, problems)."""
    valid, problems = [], []
    if not isinstance(actions, list):
        return [], ["'actions' must be a list"]
    for a in actions[:MAX_ACTIONS]:
        kind = a.get("type") if isinstance(a, dict) else None
        if kind == "tap":
            if a.get("target") in targets:
                valid.append({"type": "tap", "target": a["target"]})
            else:
                problems.append(f"unknown target {a.get('target')!r}")
        elif kind == "wait":
            try:
                valid.append({"type": "wait", "seconds": max(0.0, min(MAX_WAIT, float(a.get("seconds", 1))))})
            except (TypeError, ValueError):
                problems.append("bad wait")
        else:
            problems.append(f"unknown action {kind!r}")
    if len(actions) > MAX_ACTIONS:
        problems.append(f"more than {MAX_ACTIONS} actions")
    return valid, problems


def _bullets(items: list[str], empty: str) -> str:
    return "\n".join(f"- {i}" for i in items) if items else empty


class Brain:
    def __init__(self, ask: Callable[[str, str, list], str]):
        self.ask = ask

    def decide(self, ctx: dict) -> Decision:
        """ctx: game, boss, attempt, turn, targets, profile, playbook, hypotheses_under_test, recent (turn summaries),
        last_attempt (one line or ''), images (JPEG bytes: full screen first, then optional crops)."""
        text = (
            f"Game: {ctx['game']}. Boss: {ctx['boss']}. Attempt {ctx['attempt']}, my turn number {ctx['turn']}.\n"
            f"Valid targets: {', '.join(ctx['targets'])}\n\n"
            f"Verified facts about this boss:\n{_bullets(ctx['profile'], '(none yet)')}\n\n"
            f"Playbook:\n{ctx['playbook']}\n\n"
            f"Last turns of this attempt:\n{_bullets(ctx['recent'], '(first turn)')}\n"
            + (f"\nPrevious attempt: {ctx['last_attempt']}\n" if ctx.get("last_attempt") else "")
            + ("\nThe first image is the whole battle screen; the others are zoomed crops of important areas (named in order: "
               + ", ".join(ctx["crop_names"]) + ").\n" if ctx.get("crop_names") else "\nThe image is the whole battle screen.\n")
            + "Choose this turn's actions."
        )
        problems: list[str] = []
        for _ in range(2):
            note = "" if not problems else f"\n\nYour previous reply was rejected: {'; '.join(problems)}. Use only the valid targets."
            raw = self.ask(DECIDE_SYSTEM, text + note, ctx["images"])
            try:
                obj = parse_json(raw)
            except BrainError as e:
                problems = [str(e)]
                continue
            actions, bad = validate_actions(obj.get("actions"), set(ctx["targets"]))
            if actions:
                state = obj.get("state") if isinstance(obj.get("state"), dict) else {}
                return Decision(state=state, actions=actions, reason=str(obj.get("reason") or "")[:300], raw=raw)
            problems = bad or ["no usable actions"]
        raise BrainError("The model gave no usable moves: " + "; ".join(problems))

    def reflect(self, ctx: dict) -> dict:
        """ctx: game, boss, outcome, turns (list of turn summaries), boss_hp_left, playbook, profile, hypotheses,
        can_change (bool). Returns {'cause', 'facts', 'hypotheses', 'changes'} already trimmed."""
        text = (
            f"Game: {ctx['game']}. Boss: {ctx['boss']}. Result: {ctx['outcome']} after {len(ctx['turns'])} turns, "
            f"boss HP left: {ctx['boss_hp_left']}.\n\n"
            f"Turn log:\n{_bullets(ctx['turns'], '(empty)')}\n\n"
            f"Playbook:\n{ctx['playbook']}\n\n"
            f"Verified facts:\n{_bullets(ctx['profile'], '(none)')}\n\n"
            f"Untested ideas so far:\n{_bullets(ctx['hypotheses'], '(none)')}\n\n"
            + ("" if ctx["can_change"] else "The playbook is currently being evaluated, so do NOT propose changes now (use an empty list).\n")
        )
        obj = parse_json(self.ask(REFLECT_SYSTEM, text, []))
        facts = [str(f) for f in (obj.get("facts") or []) if isinstance(f, (str, int, float))][:3]
        hyps = [str(h) for h in (obj.get("hypotheses") or []) if isinstance(h, (str, int, float))][:5]
        changes = [c for c in (obj.get("changes") or []) if isinstance(c, dict) and c.get("op") in ("add", "edit", "remove")][:2]
        return {"cause": str(obj.get("cause") or "")[:400], "facts": facts, "hypotheses": hyps,
                "changes": changes if ctx["can_change"] else []}
