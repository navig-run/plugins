"""The frame loop: every frame composed, graded, grained and piped to ffmpeg.

Ported verbatim from the channel workspace's ``kinetic.py`` — plates move,
words land when spoken, cuts accent, red words pulse at the frame edges, and
the master comes out at 1080x1920 / 30 fps. The only change is where the
paths come from (:mod:`navig_video.kinetic.ctx`).
"""

from __future__ import annotations

import math
import subprocess
from pathlib import Path

import numpy as np
from PIL import Image

from navig_video.kinetic.ctx import ctx
from navig_video.kinetic.mix import mix
from navig_video.kinetic.pictures import (
    EDGE, FPS, H, W, Plate, VideoPlate, Words, ease_out_back, end_card,
    glitch, hud, loading_bar, static_overlay, title_open,
)
from navig_video.kinetic.timeline import END_HOLD, Timeline, plan

__all__ = ["render", "still"]


def _episode(name: str) -> dict:
    from navig_video.workspace import load_workspace_module

    EP = load_workspace_module(ctx().ws, "episodes")
    return EP.EPISODES[name]


def _shots(tl: Timeline) -> dict[int, tuple[float, float, str]]:
    """Consecutive beats on the same plate are one continuous move."""
    shots: dict[int, tuple[float, float, str]] = {}
    for b in tl.beats:
        s = shots.get(b.seg)
        shots[b.seg] = (s[0] if s else b.t0, b.t1, b.plate)
    return shots


def _make_plates(shots: dict, force_stills: bool) -> dict:
    cx = ctx()
    return {
        seg: (VideoPlate(pid, s1 - s0) if (cx.plates / f"{pid}.mp4").exists()
              and not force_stills else Plate(pid, seg))
        for seg, (s0, s1, pid) in shots.items()
    }


def _frame_fn(name: str, lang: str, ep: dict, tl: Timeline, stills: bool = False):
    """Build the frame(t) closure — the whole picture pipeline."""
    shots = _shots(tl)
    plates = _make_plates(shots, stills)
    over, top = static_overlay(), hud(ep, lang)
    words = Words()
    layouts = {i: words.layout(b.words) for i, b in enumerate(tl.beats)}
    grain = [np.random.default_rng(i).normal(0, 9, (H // 2, W // 2)).astype(np.int16)
             for i in range(6)]

    def beat_at(t):
        for i, b in enumerate(tl.beats):
            if b.t0 <= t < b.t1:
                return i, b
        return None, None

    def frame(t: float) -> np.ndarray:
        if t < tl.open_until:
            # the standard title open: glyph flood → interlace → slice shear →
            # clean mark, then the first beat cuts in
            img = title_open(ep, lang, t, t / max(tl.open_until, 0.01)).convert("RGBA")
            img.alpha_composite(over)
            arr = np.asarray(img.convert("RGB")).astype(np.int16)
            g = grain[(int(t * FPS) // 2) % len(grain)]
            arr += np.repeat(np.repeat(g, 2, 0), 2, 1)[..., None]
            return np.clip(arr, 0, 255).astype(np.uint8)
        if t >= tl.end_t0:
            p = (t - tl.end_t0) / END_HOLD
            img = end_card(ep, lang, p)
            if t - tl.end_t0 < 0.1:
                return glitch(np.asarray(img), 1.0, int(t * 100))
            return np.asarray(img)
        i, b = beat_at(t)
        if b is None:
            i, b = 0, tl.beats[0]
        s0, s1, _ = shots[b.seg]
        img = plates[b.seg].frame((t - s0) / max(s1 - s0, 0.01)).convert("RGBA")
        # cross-fade INTO a 'fade' beat that starts a new shot
        since = t - b.t0
        if b.cut == "fade" and i > 0 and tl.beats[i - 1].seg != b.seg and since < 0.45:
            pb = tl.beats[i - 1]
            ps0, ps1, _ = shots[pb.seg]
            prev = plates[pb.seg].frame((t - ps0) / max(ps1 - ps0, 0.01)).convert("RGBA")
            img = Image.blend(prev, img, since / 0.45)
        img.alpha_composite(over)
        # words: each lands when it is spoken
        for w, x, y, size in layouts[i]:
            if t < w["s"]:
                continue
            k = (t - w["s"]) / 0.13
            # slam: arrives 1.35x, undershoots a touch, settles at 1.0
            sc = 1.0 + (1.0 - ease_out_back(k)) * 0.35 if k < 1 else 1.0
            spr = words.sprite(w["w"].upper(), size, w["red"])
            if k < 1:
                if getattr(tl, "motion", None) == "rise":
                    # card-style entrance; speech keeps the timing
                    spr2 = spr
                    oy = y - 30 + int(30 * (1 - (1 - k) ** 2))
                    ox = x - 30
                else:
                    nw, nh = max(1, int(spr.width * sc)), max(1, int(spr.height * sc))
                    spr2 = spr.resize((nw, nh), Image.Resampling.BILINEAR)
                    ox = int(x - 30 - (nw - spr.width) / 2)
                    oy = int(y - 30 - (nh - spr.height) / 2)
                alpha = min(1.0, k * 2.5)
                if alpha < 1:
                    a = np.asarray(spr2).copy()
                    a[..., 3] = (a[..., 3] * alpha).astype(np.uint8)
                    spr2 = Image.fromarray(a, "RGBA")
            else:
                spr2, ox, oy = spr, x - 30, y - 30
            if w["red"] and k < 2:
                ox += int(math.sin(t * 90) * 4 * (1 - k / 2))
            img.alpha_composite(spr2, (max(0, ox), max(0, oy)))
        if b.bar:
            loading_bar(img, tl, i, b, t)
        img.alpha_composite(top)
        arr = np.asarray(img.convert("RGB")).astype(np.int16)
        g = grain[(int(t * FPS) // 2) % len(grain)]   # grain on twos: film, and compressible
        arr += np.repeat(np.repeat(g, 2, 0), 2, 1)[..., None]
        arr = np.clip(arr, 0, 255).astype(np.uint8)
        # cut accents
        if b.cut == "flash" and since < 0.1:
            k = 1 - since / 0.1
            arr = np.clip(arr.astype(np.int16) + np.array([255, 74, 61]) * 0.55 * k,
                          0, 255).astype(np.uint8)
        elif b.cut in ("hard", "glitch") and i > 0 and since < (0.13 if b.cut == "glitch" else 0.05):
            arr = glitch(arr, 1.0 if b.cut == "glitch" else 0.5, int(t * 1000))
        # interference: the picture tears in bursts while the line plays
        if b.fx == "interference":
            fi = int(t * FPS)
            if fi % 7 in (0, 1) or (fi * 2654435761) % 11 == 0:
                arr = glitch(arr, 0.7, fi)
            y0 = (fi * 97) % (H - 80)
            arr[y0:y0 + 6] = np.clip(arr[y0:y0 + 6].astype(np.int16) + 70, 0, 255).astype(np.uint8)
        # a red word lands with a short pulse at the frame EDGES only — a
        # full-frame wash turned every red word into a maroon flash
        for w, *_ in layouts[i]:
            dt = t - w["s"]
            if w["red"] and 0 <= dt < 3 / FPS:
                k = 1 - dt / (3 / FPS)
                arr = np.clip(arr.astype(np.int16) + (EDGE * k).astype(np.int16),
                              0, 255).astype(np.uint8)
        return arr

    return frame


def still(name: str, lang: str, vals: dict, at: float) -> Path:
    """One frame at ``at`` seconds — for eyeballing a render without paying for it."""
    ep = _episode(name)
    tl = plan(name, lang, vals)
    frame = _frame_fn(name, lang, ep, tl)
    p = ctx().out / f"_still-{name}-{lang}-{at}.png"
    p.parent.mkdir(parents=True, exist_ok=True)
    Image.fromarray(frame(at)).save(p)
    print("still", p)
    return p


def render(name: str, lang: str, vals: dict, force_stills: bool = False) -> Path:
    """Full render → ``<out>/<lang>/<name>.mp4`` (1080x1920, 30 fps, −14 LUFS)."""
    ep = _episode(name)
    tl = plan(name, lang, vals)
    cx = ctx()
    out = cx.out / lang / f"{name}.mp4"
    out.parent.mkdir(parents=True, exist_ok=True)

    frame = _frame_fn(name, lang, ep, tl, stills=force_stills)
    audio = mix(name, lang, tl, cx.ws)
    n = int(math.ceil(tl.total * FPS))
    enc = subprocess.Popen(
        ["ffmpeg", "-v", "error", "-y", "-f", "rawvideo", "-pix_fmt", "rgb24",
         "-s", f"{W}x{H}", "-r", str(FPS), "-i", "-", "-i", str(audio),
         "-map", "0:v", "-map", "1:a", "-c:v", "libx264", "-preset", "medium",
         "-crf", "19", "-maxrate", "16M", "-bufsize", "32M", "-pix_fmt", "yuv420p",
         "-c:a", "aac", "-b:a", "256k",
         "-movflags", "+faststart", "-shortest", str(out)],
        stdin=subprocess.PIPE)
    for fi in range(n):
        enc.stdin.write(frame(fi / FPS).tobytes())
    enc.stdin.close()
    enc.wait()
    print(f"wrote {out}  {tl.total:.1f}s")
    return out
