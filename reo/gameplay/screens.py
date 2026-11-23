"""Cheap screen recognition (no AI): compare a saved reference crop with the same region of the live window.

The user captures a reference once per screen (battle ready, victory, defeat, retry...) while the game shows it.
Most of a fight is waiting through animations and tapping through result screens, and this handles all of that
without a single model call.
"""
from __future__ import annotations

import numpy as np
from PIL import Image

from .io import crop_norm

SIZE = (64, 64)
DEFAULT_THRESHOLD = 0.88


def _arr(img) -> np.ndarray:
    return np.asarray(img.convert("RGB").resize(SIZE, Image.BILINEAR), dtype=np.float32)


def similarity(sample: np.ndarray, template: np.ndarray) -> float:
    """0..1. Pixel closeness, and (for templates that have structure) how well the structure lines up, so a
    uniformly dark screen cannot match a dark-ish template just because the colours are near."""
    score = 1.0 - float(np.abs(sample - template).mean()) / 255.0
    gt, gs = template.mean(axis=2), sample.mean(axis=2)
    if gt.std() > 8.0:
        if gs.std() < 1.0:                       # a flat sample (under one grey level of variation) has no structure to compare
            return 0.0
        corr = float(np.corrcoef(gt.ravel(), gs.ravel())[0, 1])
        score = min(score, max(0.0, corr))
    return score


class ScreenDetector:
    def __init__(self, store, game: str):
        self.store, self.game = store, game
        self._cache: dict = {}

    def screens(self) -> dict:
        return self.store.screens(self.game)

    def _template(self, state: str, cfg: dict):
        path = self.store.template_path(self.game, state)
        if not path.is_file():
            return None
        key = (state, path.stat().st_mtime_ns)
        if key not in self._cache:
            self._cache = {k: v for k, v in self._cache.items() if k[0] != state}
            with Image.open(path) as im:
                self._cache[key] = _arr(im)
        return self._cache[key]

    def classify(self, img) -> tuple[str | None, float]:
        """(state, score) of the best match above its threshold, else (None, best score seen)."""
        best, best_score, top = None, 0.0, 0.0
        for state, cfg in self.screens().items():
            region = cfg.get("region")
            if not region or not cfg.get("template"):
                continue
            tpl = self._template(state, cfg)
            if tpl is None:
                continue
            s = similarity(_arr(crop_norm(img, region)), tpl)
            top = max(top, s)
            if s >= float(cfg.get("threshold") or DEFAULT_THRESHOLD) and s > best_score:
                best, best_score = state, s
        return (best, best_score) if best else (None, top)
