"""Drawing for the floating orb and its reply bubble (Pillow only, so it runs and is testable anywhere).

Everything is rendered to RGBA images with real per-pixel transparency; orb_native.py puts them on screen
as Windows layered windows."""
from __future__ import annotations

import math
import os
from functools import lru_cache
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont

GOLD = (212, 175, 55)
LOGICAL = 220          # orb window size at 100% scale (orb plus glow room)
DIAM = 150             # the sphere itself
BUBBLE_MAX_W = 320
BUBBLE_MAX_H = 300
TAIL = 12

_FONTS = [r"C:\Windows\Fonts\segoeui.ttf", r"C:\Windows\Fonts\arial.ttf", "DejaVuSans.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"]


@lru_cache(maxsize=8)
def font(px: int):
    for f in _FONTS:
        try:
            return ImageFont.truetype(f, px)
        except OSError:
            continue
    return ImageFont.load_default(px)


def _grid(n):
    ys, xs = np.mgrid[0:n, 0:n].astype("float32")
    return (xs + .5) / n, (ys + .5) / n


def gold_gradient(w: int, h: int) -> Image.Image:
    """Reo's gold texture: the same 135deg gradient as the buttons."""
    yy, xx = np.mgrid[0:h, 0:w].astype("float32")
    t = (xx / max(1, w - 1) + yy / max(1, h - 1)) / 2
    stops = np.array([[246, 220, 130], [212, 175, 55], [181, 139, 26]], "float32")
    col = np.where(t[..., None] < .48, stops[0] + (stops[1] - stops[0]) * (t[..., None] / .48),
                   stops[1] + (stops[2] - stops[1]) * ((t[..., None] - .48) / .52))
    return Image.fromarray(np.dstack([col, np.full((h, w), 255, "float32")]).astype("uint8"), "RGBA")


def _fallback_mark(base: Image.Image) -> None:
    """Procedural stand-in for the missing artwork: a dark "R" centred on the gold sphere."""
    n = base.width
    d = ImageDraw.Draw(base)
    f = font(int(n * .5))
    try:
        box = d.textbbox((0, 0), "R", font=f)
        w, h = box[2] - box[0], box[3] - box[1]
        d.text(((n - w) / 2 - box[0], (n - h) / 2 - box[1]), "R", font=f, fill=(60, 44, 8, 255))
    except Exception:                                   # noqa: BLE001
        d.ellipse([n * .38, n * .38, n * .62, n * .62], fill=(60, 44, 8, 255))


class OrbArt:
    def __init__(self, icon_path: str | Path, scale: float = 1.0):
        """scale: pixels per logical unit. The orb is DIAM units wide inside a LOGICAL-unit window; the window keeps the
        same proportions at any size."""
        self.scale = scale
        self.S = round(LOGICAL * scale)
        self.D = round(DIAM * scale)
        self.orb = self._make_orb(Path(icon_path), self.D)
        dim = np.asarray(self.orb).astype("float32")
        dim[..., :3] *= .62
        self.orb_dim = Image.fromarray(dim.astype("uint8"), "RGBA")
        m = Image.new("L", (self.S, self.S), 0)
        pad = (self.S - self.D) // 2
        ImageDraw.Draw(m).ellipse([pad, pad, pad + self.D, pad + self.D], fill=255)
        self.glow = m.filter(ImageFilter.GaussianBlur(self.S * .065))
        self._badge = self._make_badge()

    # ---- static pieces
    @staticmethod
    def _make_orb(icon: Path, d: int) -> Image.Image:
        ss = 3
        n = d * ss
        base = gold_gradient(n, n)
        try:
            ic = Image.open(icon).convert("RGBA")
            k = int(n * .86)
            ic = ic.resize((k, k), Image.LANCZOS)
            base.alpha_composite(ic, ((n - k) // 2, (n - k) // 2))
        except OSError:
            _fallback_mark(base)                    # no reo.png: draw a plain "R" so the orb is never blank
        x, y = _grid(n)
        hl = np.clip(1 - np.hypot(x - .32, y - .22) / .38, 0, 1) * .5
        vg = np.clip((np.hypot(x - .5, y - .5) - .31) / .19, 0, 1) * .3
        ov = np.zeros((n, n, 4), "float32")
        ov[..., :3] = 255
        ov[..., 3] = hl * 255
        base.alpha_composite(Image.fromarray(ov.astype("uint8"), "RGBA"))
        ov[..., :3] = 0
        ov[..., 3] = vg * 255
        base.alpha_composite(Image.fromarray(ov.astype("uint8"), "RGBA"))
        rim = Image.new("RGBA", (n, n), (0, 0, 0, 0))
        w = max(3, int(d * .022 * ss))
        ImageDraw.Draw(rim).ellipse([w // 2, w // 2, n - 1 - w // 2, n - 1 - w // 2], outline=(184, 147, 42, 255), width=w)
        base.alpha_composite(rim)
        mask = Image.new("L", (n, n), 0)
        ImageDraw.Draw(mask).ellipse([0, 0, n - 1, n - 1], fill=255)
        r, g, b, a = base.split()
        base = Image.merge("RGBA", (r, g, b, Image.fromarray((np.asarray(a, "float32") * np.asarray(mask, "float32") / 255).astype("uint8"))))
        return base.resize((d, d), Image.LANCZOS)

    def _make_badge(self) -> Image.Image:
        """A small 'microphone muted' badge."""
        n = max(12, round(self.D * .27))
        ss = 4
        im = Image.new("RGBA", (n * ss, n * ss), (0, 0, 0, 0))
        d = ImageDraw.Draw(im)
        N = n * ss
        d.ellipse([0, 0, N - 1, N - 1], fill=(26, 26, 29, 255), outline=(212, 175, 55, 255), width=max(2, N // 14))
        cx = N / 2
        d.rounded_rectangle([cx - N * .11, N * .2, cx + N * .11, N * .5], radius=N * .11, fill=(235, 235, 238, 255))
        d.arc([cx - N * .2, N * .3, cx + N * .2, N * .62], 20, 160, fill=(235, 235, 238, 255), width=max(2, N // 18))
        d.line([cx, N * .62, cx, N * .76], fill=(235, 235, 238, 255), width=max(2, N // 18))
        d.line([N * .22, N * .78, N * .78, N * .22], fill=(229, 72, 77, 255), width=max(3, N // 10))
        return im.resize((n, n), Image.LANCZOS)

    # ---- one frame
    def frame(self, state: str, level: float = 0.0, t: float = 0.0, mood: str | None = None, mood_p: float = 0.0, sleep: float = 0.0) -> Image.Image:
        """state: idle | listening | thinking | speaking. mood/mood_p: a colour pulse (mood_p goes 1 -> 0). sleep: 0 awake .. 1 asleep."""
        S, D = self.S, self.D
        level = max(0.0, min(1.0, level))
        img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
        strength = {"idle": .16, "listening": .34 + .5 * level, "thinking": .32, "speaking": .4 + .5 * level}.get(state, .16)
        strength *= 1 - .6 * sleep
        glow_c = GOLD
        if mood and mood_p > 0:
            from .mood import COLORS
            glow_c = tuple(int(a * (1 - mood_p) + b * mood_p) for a, b in zip(GOLD, COLORS.get(mood, GOLD)))
            strength = min(1.0, strength + .5 * mood_p)
        gold = Image.new("RGBA", (S, S), glow_c + (0,))
        gold.putalpha(self.glow.point(lambda v: int(v * min(1.0, strength))))
        img.alpha_composite(gold)

        if state == "listening":                    # expanding rings
            ss = 2
            layer = Image.new("RGBA", (S * ss, S * ss), (0, 0, 0, 0))
            dr = ImageDraw.Draw(layer)
            for k in (0.0, .5):
                p = (t * .5 + k) % 1.0
                r = D / 2 * (1 + .24 * p) * ss
                a = int(170 * (1 - p))
                c = S * ss / 2
                dr.ellipse([c - r, c - r, c + r, c + r], outline=(246, 220, 130, a), width=max(2, int(2 * self.scale * ss)))
            img.alpha_composite(layer.resize((S, S), Image.LANCZOS))

        if state == "speaking":                     # soft rings that follow the voice level
            ss = 2
            layer = Image.new("RGBA", (S * ss, S * ss), (0, 0, 0, 0))
            dr = ImageDraw.Draw(layer)
            for k_ in (0.0, .5):
                p_ = (t * .9 + k_) % 1.0
                r = D / 2 * (1 + (.10 + .22 * level) * p_) * ss
                c = S * ss / 2
                dr.ellipse([c - r, c - r, c + r, c + r], outline=(246, 220, 130, int(150 * (1 - p_))), width=max(2, int(2.5 * self.scale * ss)))
            img.alpha_composite(layer.resize((S, S), Image.LANCZOS))

        k = 1 + level * .06 + (.018 * math.sin(t * 2.4) if state in ("listening", "speaking") else 0)
        if sleep:
            k *= 1 - .05 * sleep + .012 * sleep * math.sin(t * 1.1)      # slow breathing while asleep
        art = self.orb_dim if (state == "idle" or sleep > .5) else self.orb
        if abs(k - 1) > .002:
            sz = max(2, round(D * k))
            art = art.resize((sz, sz), Image.BILINEAR)
        ox = (S - art.width) // 2
        img.alpha_composite(art, (ox, ox))

        if state == "thinking":                     # spinner arc
            ss = 2
            layer = Image.new("RGBA", (S * ss, S * ss), (0, 0, 0, 0))
            dr = ImageDraw.Draw(layer)
            r = (D / 2 + 2 * self.scale) * ss
            c = S * ss / 2
            a0 = (t * 330) % 360
            segs = 24
            for i in range(segs):
                a = int(255 * (i / segs) ** 1.5)
                s0 = a0 - 150 + i * 150 / segs
                dr.arc([c - r, c - r, c + r, c + r], s0, s0 + 150 / segs + 1.5, fill=(246, 220, 130, a), width=max(3, int(5 * self.scale * ss)))
            img.alpha_composite(layer.resize((S, S), Image.LANCZOS))

        if state == "idle":                         # idle means the microphone is off
            bx = (S + D) // 2 - self._badge.width - int(D * .02)
            by = (S + D) // 2 - self._badge.height - int(D * .02)
            img.alpha_composite(self._badge, (bx, by))
        if sleep > 0:
            a = img.getchannel("A").point(lambda v: int(v * (1 - .5 * sleep)))
            img.putalpha(a)
        return img


# ---------------------------------------------------------------- reply bubble
def _wrap(text: str, f, width: float) -> list[str]:
    lines: list[str] = []
    for para in text.replace("\r", "").split("\n"):
        words, cur = para.split(" "), ""
        for w in words:
            trial = (cur + " " + w) if cur else w
            if f.getlength(trial) <= width:
                cur = trial
                continue
            if cur:
                lines.append(cur)
            while f.getlength(w) > width:            # a single very long word
                cut = len(w)
                while cut > 1 and f.getlength(w[:cut]) > width:
                    cut -= 1
                lines.append(w[:cut])
                w = w[cut:]
            cur = w
        lines.append(cur)
    return lines


def render_bubble(text: str, side: str, scale: float = 1.0) -> tuple[Image.Image, int]:
    """Gold speech bubble. `side` is where the bubble sits relative to the orb ("right": the tail is on its left edge).
    Returns (image, y of the tail tip within the image)."""
    ss = 2
    sc = scale * ss
    f = font(round(15 * sc))
    padx, pady, r, tail = 16 * sc, 12 * sc, 16 * sc, TAIL * sc
    inner_w = BUBBLE_MAX_W * sc - 2 * padx
    lines = _wrap(text.strip() or " ", f, inner_w)
    lh = 15 * sc * 1.42
    cap = max(1, int((BUBBLE_MAX_H * sc - 2 * pady) // lh))
    if len(lines) > cap:
        lines = lines[:cap]
        last = lines[-1].rstrip()
        while last and f.getlength(last + "...") > inner_w:
            last = last[:-1]
        lines[-1] = last + "..."
    tw = max(f.getlength(l) for l in lines)
    bw, bh = int(min(BUBBLE_MAX_W * sc, tw + 2 * padx)), int(len(lines) * lh + 2 * pady)
    W, H = int(bw + tail), int(bh + 4 * sc)
    bx, by = (int(tail) if side == "right" else 0), int(2 * sc)

    # gold gradient (135deg) clipped to a rounded rectangle
    yy, xx = np.mgrid[0:bh, 0:bw].astype("float32")
    t = (xx / bw + yy / bh) / 2
    stops = np.array([[246, 220, 130], [212, 175, 55], [181, 139, 26]], "float32")
    col = np.where(t[..., None] < .48, stops[0] + (stops[1] - stops[0]) * (t[..., None] / .48),
                   stops[1] + (stops[2] - stops[1]) * ((t[..., None] - .48) / .52))
    gloss = np.clip(1 - yy / (bh * .52), 0, 1)[..., None] * .38
    col = col + (255 - col) * gloss
    box = Image.fromarray(np.dstack([col, np.full((bh, bw), 255, "float32")]).astype("uint8"), "RGBA")
    mask = Image.new("L", (bw, bh), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, bw - 1, bh - 1], radius=r, fill=255)
    out = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    out.paste(box, (bx, by), mask)
    d = ImageDraw.Draw(out)
    d.rounded_rectangle([bx, by, bx + bw - 1, by + bh - 1], radius=r, outline=(243, 220, 140, 255), width=max(1, int(sc)))
    ty = by + bh // 2
    th = 10 * sc
    if side == "right":
        d.polygon([(2 * sc, ty), (bx + 1, ty - th), (bx + 1, ty + th)], fill=(214, 177, 58, 255))
    else:
        d.polygon([(W - 2 * sc, ty), (bx + bw - 1, ty - th), (bx + bw - 1, ty + th)], fill=(214, 177, 58, 255))
    y = by + pady
    for l in lines:
        d.text((bx + padx, y), l, font=f, fill=(28, 20, 4, 255))
        y += lh
    return out.resize((max(1, W // ss), max(1, H // ss)), Image.LANCZOS), ty // ss


def bubble_geometry(orb_rect, bubble_wh, tail_y, screen, scale=1.0, reach=None):
    """Where to put the bubble. orb_rect=(x, y, w, h) of the orb window; screen=(x, y, w, h).
    The bubble goes on the side facing the middle of the screen. Returns (side, x, y)."""
    ox, oy, ow, oh = orb_rect
    sx, sy, sw, sh = screen
    bw, bh = bubble_wh
    cx, cy = ox + ow / 2, oy + oh / 2
    side = "right" if cx < sx + sw / 2 else "left"
    reach = reach if reach is not None else (DIAM / 2 + 6) * scale
    x = cx + reach - 2 * scale if side == "right" else cx - reach - bw + 2 * scale
    y = cy - tail_y                                   # the tail points at the orb's centre
    x = max(sx + 4, min(x, sx + sw - bw - 4))
    y = max(sy + 4, min(y, sy + sh - bh - 4))
    return side, int(x), int(y)


def compose_layout(S, orb_xy, bubble_wh, tail_y, screen, scale=1.0, reach=None):
    """Orb and bubble live in ONE window, so dragging moves both with a single SetWindowPos and no re-rendering.
    Returns (side, (W, H) canvas, orb offset in the canvas, bubble offset in the canvas, screen origin of the canvas)."""
    ox, oy = orb_xy
    bw, bh = bubble_wh
    side, bx, by = bubble_geometry((ox, oy, S, S), bubble_wh, tail_y, screen, scale, reach)
    minx, miny = min(ox, bx), min(oy, by)
    maxx, maxy = max(ox + S, bx + bw), max(oy + S, by + bh)
    return side, (maxx - minx, maxy - miny), (ox - minx, oy - miny), (bx - minx, by - miny), (minx, miny)
