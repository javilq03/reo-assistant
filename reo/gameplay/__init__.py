"""Gameplay: Reo plays a game on the PC, and learns how to beat a specific boss over repeated attempts.

    io        the game window (capture, normalized clicks, F8 point picking)
    screens   recognizing known screens from reference crops, no AI
    store     everything Reo knows about a game, as editable files
    learning  versioned playbook, scoring, rollback
    brain     the per-turn vision call and the post-loss reflection call
    runner    the attempt loop
    manager   thread, kill switch and calibration; what the Bridge uses
"""
from .manager import GameplayManager

__all__ = ["GameplayManager"]
