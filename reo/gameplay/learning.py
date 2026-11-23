"""How Reo gets better at a boss. The model's weights never change; what improves is the playbook it is shown.

Rules for the learning to converge instead of oscillating:
  * One change set at a time. A playbook version that has been changed is "pending" until it has been tried
    MIN_TRIAL times; while pending, no further playbook changes are accepted (facts and ideas still are).
  * Every version is scored from the attempt log (win rate, then boss HP left). A pending version that scored
    worse than the version it came from is rolled back; otherwise its trial rules become permanent rules.
  * The playbook stays short (MAX_RULES); the rules with the least evidence are dropped first.
"""
from __future__ import annotations

import copy
import time

from .store import BossStore

MAX_RULES = 30
MIN_TRIAL = 3
HP_MARGIN = 5.0          # boss HP left must be this many points worse to count as worse when win rates tie


def equiv(pb: dict) -> set:
    """Versions whose attempts count for this playbook (a rollback inherits the version it restored)."""
    return {pb["version"], *pb.get("same_as", [])}


def version_stats(attempts: list[dict], versions: set) -> dict:
    rows = [a for a in attempts if a.get("playbook_version") in versions]
    n = len(rows)
    wins = sum(1 for a in rows if a.get("outcome") == "win")
    hp = [100.0 if a.get("boss_hp_left") is None else float(a["boss_hp_left"]) for a in rows]
    return {"n": n, "wins": wins, "win_rate": wins / n if n else 0.0, "avg_hp": sum(hp) / n if n else 100.0}


def render(pb: dict) -> str:
    """The playbook as shown to the model."""
    if not pb["rules"]:
        return "(no rules yet)"
    lines = []
    for r in pb["rules"]:
        tag = " (being tested)" if r["status"] == "trial" else f" (seen in {r['wins']} wins, {r['losses']} losses)"
        lines.append(f"- [{r['id']}] {r['text']}{tag}")
    return "\n".join(lines)


def can_change(pb: dict) -> bool:
    return not pb.get("pending")


def update_evidence(boss: BossStore, won: bool) -> None:
    pb = boss.playbook()
    for r in pb["rules"]:
        r["wins" if won else "losses"] += 1
    boss.save_playbook(pb, history=False)


def evaluate(boss: BossStore, min_trial: int = MIN_TRIAL) -> str | None:
    """Judge a pending version once it has had enough attempts. Returns 'kept', 'rolled_back' or None."""
    pb = boss.playbook()
    if not pb.get("pending"):
        return None
    attempts = boss.attempts()
    cur = version_stats(attempts, equiv(pb))
    if cur["n"] < min_trial:
        return None
    parent = boss.playbook_version(pb.get("parent"))
    base = version_stats(attempts, equiv(parent)) if parent else None
    worse = bool(base and base["n"] > 0 and (
        cur["win_rate"] < base["win_rate"]
        or (cur["win_rate"] == base["win_rate"] and cur["avg_hp"] > base["avg_hp"] + HP_MARGIN)))
    if worse:
        nb = copy.deepcopy(parent)
        nb.update(version=pb["version"] + 1, parent=parent.get("parent"), pending=False,
                  same_as=sorted(equiv(parent)), note=f"rollback of v{pb['version']}", created=time.time(),
                  next_id=pb["next_id"])
        boss.save_playbook(nb)
        return "rolled_back"
    for r in pb["rules"]:
        if r["status"] == "trial":
            r["status"] = "rule"
    pb["pending"] = False
    boss.save_playbook(pb)
    return "kept"


def _evidence(r: dict) -> int:
    return r["wins"] + r["losses"]


def apply_changes(boss: BossStore, changes: list[dict], note: str = "") -> int | None:
    """Apply up to two rule changes as a new pending version. Returns the new version number, or None."""
    pb = copy.deepcopy(boss.playbook())
    if not can_change(pb):
        return None
    rules, changed = pb["rules"], False
    for ch in (changes or [])[:2]:
        op, text = ch.get("op"), " ".join(str(ch.get("text") or "").split())[:240]
        rule = next((r for r in rules if r["id"] == ch.get("id")), None)
        if op == "add" and text:
            rules.append({"id": f"r{pb['next_id']}", "text": text, "status": "trial", "wins": 0, "losses": 0})
            pb["next_id"] += 1
            changed = True
        elif op == "edit" and rule and text:
            rule.update(text=text, status="trial", wins=0, losses=0)
            changed = True
        elif op == "remove" and rule:
            rules.remove(rule)
            changed = True
    if not changed:
        return None
    while len(rules) > MAX_RULES:
        pool = [r for r in rules if r["status"] == "rule"] or rules
        rules.remove(min(pool, key=_evidence))
    pb.update(parent=pb["version"], version=pb["version"] + 1, pending=True, same_as=[],
              note=(note or "reflection")[:200], created=time.time())
    boss.save_playbook(pb)
    return pb["version"]


def set_manual_rules(boss: BossStore, text: str) -> int:
    """The user's own starter playbook: one rule per line. Replaces the rules and starts a fresh version."""
    pb = copy.deepcopy(boss.playbook())
    lines = [" ".join(ln.lstrip("-* ").split())[:240] for ln in str(text or "").splitlines()]
    lines = [ln for ln in lines if ln][:MAX_RULES]
    pb["rules"] = [{"id": f"r{i}", "text": ln, "status": "rule", "wins": 0, "losses": 0}
                   for i, ln in enumerate(lines, pb["next_id"])]
    pb.update(next_id=pb["next_id"] + len(lines), parent=pb["version"], version=pb["version"] + 1, pending=False,
              same_as=[], note="manual", created=time.time())
    boss.save_playbook(pb)
    return pb["version"]


def summary(boss: BossStore) -> list[dict]:
    """Per-version results for the panel."""
    attempts = boss.attempts()
    out = []
    for v in sorted({a.get("playbook_version") for a in attempts if a.get("playbook_version")}):
        st = version_stats(attempts, {v})
        out.append({"version": v, **st})
    return out
