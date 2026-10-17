"""The pictures: plates that move, the HUD, the word sprites, the end card.

Ported verbatim from the channel workspace's ``kinetic.py`` — every constant
(the grade curve, the slam easing, the vignette, the safe areas) was tuned by
eye against renders, so this is a move, not a rewrite. Paths come from the
bound workspace (:mod:`navig_video.kinetic.ctx`); fonts are the channel's own
(Bebas has Cyrillic — Druk does not).
"""

from __future__ import annotations

import math
import subprocess
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont

from navig_video.kinetic.ctx import ctx
from navig_video.kinetic.timeline import Timeline

__all__ = [
    "Plate", "VideoPlate", "Words", "duration_of", "ease_out_back",
    "end_card", "glitch", "grade", "hud", "missing_fonts",
    "static_overlay", "title_open",
]

W, H, FPS = 1080, 1920, 30
SAFE_TOP, SAFE_BOTTOM, SAFE_RIGHT = 200, 1920 - 420, 1080 - 140
PAD = 72

USR = str(Path.home() / "AppData/Local/Microsoft/Windows/Fonts") + "/"
F_SLAM = USR + "BebasNeue Bold.ttf"          # has Cyrillic; Druk does not
F_MONO = USR + "JetBrainsMono-Medium.ttf"
F_MONO_B = USR + "JetBrainsMono-Bold.ttf"

WHITE = (244, 246, 250)
RED = (255, 74, 61)          # video red: reads as signal on a phone, not pink
VOID = (8, 10, 15)

# red edge glow for the red-word pulse: strong at the borders, zero inside
_yy, _xx = np.mgrid[0:H, 0:W].astype(np.float32)
_edge = np.clip((np.sqrt(((_xx - W / 2) / (W / 2)) ** 2 + ((_yy - H / 2) / (H / 2)) ** 2) - 0.75) * 2.2, 0, 1)
EDGE = (_edge[..., None] * np.array([70, 6, 4], np.float32))
del _yy, _xx, _edge


def _font(path: str, size: int):
    return ImageFont.truetype(path, size)


def missing_fonts() -> list[str]:
    """Names of the faces this module loads that will not open.

    ``_font`` has NO fallback — a missing face must refuse the render up front
    (``navig video kinetic render`` checks this before any synth or network
    work) instead of dying mid-frames with a traceback.
    """
    missing = []
    for path in (F_SLAM, F_MONO, F_MONO_B):
        try:
            ImageFont.truetype(path, 20)
        except Exception:  # noqa: BLE001 — any load error counts as absent
            missing.append(Path(path).name)
    return missing


def duration_of(p: Path) -> float:
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                        "-of", "csv=p=0", str(p)], capture_output=True, text=True)
    return float(r.stdout.strip())


def grade(img: Image.Image) -> np.ndarray:
    """Darken toward the void, crush the mids a little, keep the reds alive.

    The plates are already dark; this makes them sit UNDER white type and
    gives every plate the same black point so cuts do not jump in exposure.
    """
    a = np.asarray(img.convert("RGB")).astype(np.float32) / 255.0
    lum = a @ np.array([0.2126, 0.7152, 0.0722], dtype=np.float32)
    redness = np.clip((a[..., 0] - np.maximum(a[..., 1], a[..., 2])) * 3.0, 0, 1)
    sat = 0.72 + 0.4 * redness[..., None]
    a = lum[..., None] + (a - lum[..., None]) * sat
    a = np.power(np.clip(a, 0, 1), 1.06) * 0.94
    navy = np.array([0.03, 0.045, 0.07], dtype=np.float32)
    a = a + navy * (1 - lum[..., None]) * 0.6
    return (np.clip(a, 0, 1) * 255).astype(np.uint8)


class Plate:
    """A still that can move: cover-fit, oversized, then cropped per frame."""

    OVER = 1.22

    def __init__(self, pid: str, motion: int):
        src = ctx().plates / f"{pid}.png"
        img = Image.open(src)
        s = max(W / img.width, H / img.height) * self.OVER
        img = img.resize((round(img.width * s), round(img.height * s)), Image.LANCZOS)
        self.img = Image.fromarray(grade(img))
        self.motion = motion

    def frame(self, p: float) -> Image.Image:
        """p in 0..1 across the shot. Push in, with a slow drift that
        alternates direction shot to shot so consecutive plates never move
        the same way (the 'ken burns' tell)."""
        e = 0.5 - 0.5 * math.cos(math.pi * min(max(p, 0), 1))
        z = 1.0 + 0.11 * e if self.motion % 3 != 2 else 1.11 - 0.11 * e
        # The image is a cover-fit scaled by OVER, so the plain cover view is a
        # W*OVER-wide window. Starting 6% inside it leaves room to drift even
        # on the plate's tight axis.
        cw = W * self.OVER / (1.06 * z)
        ch = cw * H / W
        dx = (self.img.width - cw)
        dy = (self.img.height - ch)
        dirx = [0.5, 0.25, 0.75, 0.4, 0.6][self.motion % 5]
        drift = (e - 0.5) * 0.35 * (1 if self.motion % 2 else -1)
        x = dx * min(max(dirx + drift, 0), 1)
        y = dy * (0.45 + 0.1 * (e - 0.5))
        return self.img.resize((W, H), Image.Resampling.BILINEAR, box=(x, y, x + cw, y + ch))


class VideoPlate:
    """A motion clip used as a plate. Streamed, graded, with a slight push-in
    so a motion shot and a still shot share one camera language. A shot longer
    than the clip holds on its last frame (the push keeps it alive)."""

    def __init__(self, pid: str, shot_dur: float):
        src = ctx().plates / f"{pid}.mp4"
        self.dur = shot_dur
        r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0",
                            "-show_entries", "stream=width,height,r_frame_rate",
                            "-of", "csv=p=0", str(src)], capture_output=True, text=True)
        w, h, fr = r.stdout.strip().split(",")
        n, d = fr.split("/")
        self.sw, self.sh, self.fps = int(w), int(h), float(n) / float(d)
        self.proc = subprocess.Popen(
            ["ffmpeg", "-v", "error", "-i", str(src), "-f", "rawvideo",
             "-pix_fmt", "rgb24", "-"], stdout=subprocess.PIPE)
        self.idx, self.last = -1, None

    def _read_to(self, i: int) -> Image.Image:
        while self.idx < i:
            buf = self.proc.stdout.read(self.sw * self.sh * 3)
            if len(buf) < self.sw * self.sh * 3:
                break
            self.idx += 1
            arr = np.frombuffer(buf, np.uint8).reshape(self.sh, self.sw, 3)
            self.last = Image.fromarray(grade(Image.fromarray(arr)))
        return self.last

    def frame(self, p: float) -> Image.Image:
        # before the shot starts (the lead-in) show its first frame, never nothing
        im = self._read_to(max(0, int(p * self.dur * self.fps)))
        z = 1.0 + 0.05 * min(max(p, 0), 1)
        s = max(W / im.width, H / im.height) * z
        cw, ch = W / s, H / s
        x, y = (im.width - cw) / 2, (im.height - ch) / 2
        return im.resize((W, H), Image.Resampling.BILINEAR, box=(x, y, x + cw, y + ch))


def static_overlay() -> Image.Image:
    """Vignette + scanlines + the HUD frame. Built once."""
    o = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    yy, xx = np.mgrid[0:H, 0:W].astype(np.float32)
    r = np.sqrt(((xx - W / 2) / (W * 0.62)) ** 2 + ((yy - H * 0.46) / (H * 0.62)) ** 2)
    vig = np.clip((r - 0.55) * 1.25, 0, 1) ** 1.6 * 225
    # a gentle top/bottom fall-off so the HUD and the platform UI sit on dark
    top = np.clip(1 - yy / 360, 0, 1) ** 2 * 170
    bot = np.clip((yy - (H - 620)) / 620, 0, 1) ** 1.5 * 200
    alpha = np.clip(np.maximum(np.maximum(vig, top), bot), 0, 255)
    arr = np.zeros((H, W, 4), dtype=np.uint8)
    arr[..., 3] = alpha.astype(np.uint8)
    arr[::3, :, 3] = np.clip(arr[::3, :, 3].astype(int) + 14, 0, 255)   # scanlines
    o = Image.fromarray(arr, "RGBA")
    d = ImageDraw.Draw(o)
    hair = (255, 255, 255, 70)
    inset, arm = 44, 70
    for cx, cy, sx, sy in ((inset, inset, 1, 1), (W - inset, inset, -1, 1),
                           (inset, H - inset, 1, -1), (W - inset, H - inset, -1, -1)):
        d.line([(cx, cy), (cx + sx * arm, cy)], fill=hair, width=3)
        d.line([(cx, cy), (cx, cy + sy * arm)], fill=hair, width=3)
    return o


def hud(ep: dict, lang: str) -> Image.Image:
    """Mark + wordmark top-left, transmission readout top-right."""
    o = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(o)
    f_wm = _font(F_SLAM, 52)
    mark = ctx().mark
    if mark.exists():
        m = Image.open(mark).convert("RGBA")
        mh = 54
        m = m.resize((max(1, round(m.width * mh / m.height)), mh), Image.LANCZOS)
        # Centre the mark on the wordmark's CAP height, measured — anchor "lm"
        # centres on ascender..descender, which sat the mark visibly low.
        top, centre = 88, 88 + mh // 2
        o.alpha_composite(m, (PAD, top))
        bb = d.textbbox((0, 0), "SCHEMA", font=f_wm)
        d.text((PAD + m.width + 14, centre - (bb[1] + bb[3]) / 2), "SCHEMA",
               font=f_wm, fill=(255, 255, 255, 235))
    f = _font(F_MONO, 22)
    lines = [f"TX {ep['tx']}", ep["coords"]]
    y = 86
    for i, ln in enumerate(lines):
        tw = d.textlength(ln, font=f)
        d.text((W - PAD - tw, y), ln, font=f, fill=(255, 255, 255, 165))
        if i == 0:   # the live square sits BEFORE the id, never on it
            d.rectangle([W - PAD - tw - 22, y + 8, W - PAD - tw - 12, y + 18],
                        fill=RED + (255,))
        y += 32
    return o


class Words:
    """Sprites for each word, laid out once per beat."""

    def __init__(self):
        self.cache: dict = {}

    def layout(self, words: list[dict]):
        """Break the beat into lines that fit, as big as the line count allows.
        Fewer words = bigger type: a one-word beat fills the frame."""
        n = len(words)
        size = 250 if n <= 1 else 210 if n <= 2 else 176 if n <= 4 else 148 if n <= 7 else 124
        maxw = SAFE_RIGHT - PAD
        while True:
            f = _font(F_SLAM, size)
            space = f.getlength(" ")
            lines, cur, cw = [], [], 0.0
            for w in words:
                ww = f.getlength(w["w"].upper())
                if cur and cw + space + ww > maxw:
                    lines.append(cur)
                    cur, cw = [], 0.0
                cur.append((w, ww))
                cw += (space if len(cur) > 1 else 0) + ww
            if cur:
                lines.append(cur)
            lh = int(size * 0.92)
            if len(lines) * lh <= (SAFE_BOTTOM - SAFE_TOP - 260) and all(
                    sum(x[1] for x in ln) + space * (len(ln) - 1) <= maxw for ln in lines):
                break
            size -= 8
        total = len(lines) * lh
        y0 = int((SAFE_TOP + 180 + SAFE_BOTTOM) / 2 - total / 2) - 40
        placed = []
        for li, ln in enumerate(lines):
            x = PAD
            for w, ww in ln:
                placed.append((w, int(round(x)), int(y0 + li * lh), size))
                x += ww + space
        return placed

    def sprite(self, text: str, size: int, red: bool) -> Image.Image:
        k = (text, size, red)
        if k not in self.cache:
            f = _font(F_SLAM, size)
            bb = f.getbbox(text)
            pad = 30
            im = Image.new("RGBA", (bb[2] + pad * 2, bb[3] + pad * 2), (0, 0, 0, 0))
            d = ImageDraw.Draw(im)
            d.text((pad, pad), text, font=f, fill=(0, 0, 0, 200))
            sh = im.filter(ImageFilter.GaussianBlur(9))
            d2 = ImageDraw.Draw(sh)
            d2.text((pad, pad), text, font=f, fill=(RED if red else WHITE) + (255,))
            self.cache[k] = sh
        return self.cache[k]


def ease_out_back(x: float) -> float:
    c1, c3 = 1.70158, 2.70158
    x = min(max(x, 0), 1)
    return 1 + c3 * (x - 1) ** 3 + c1 * (x - 1) ** 2


def glitch(arr: np.ndarray, amount: float, seed: int) -> np.ndarray:
    """Horizontal tears + a red/cyan split. Used for 2-4 frames, never held."""
    rng = np.random.default_rng(seed)
    out = arr.copy()
    for _ in range(int(6 + 10 * amount)):
        y = rng.integers(0, H - 40)
        h = rng.integers(6, 60)
        s = int(rng.integers(-60, 60) * amount)
        out[y:y + h] = np.roll(out[y:y + h], s, axis=1)
    sh = int(10 * amount)
    if sh:
        out[..., 0] = np.roll(out[..., 0], sh, axis=1)
        out[..., 2] = np.roll(out[..., 2], -sh, axis=1)
    return out


def loading_bar(img: Image.Image, tl: Timeline, i: int, b, t: float) -> None:
    """A progress bar under the words, filled to the LIVE share of the lead
    city's seats (1 of 5 = 20%). It fills once and holds across consecutive
    beats; a "full" beat runs it to 100% — the punchline, labelled as what
    five people would do, never as a city that is already lit."""
    v = tl.vals
    seats = v.get("seatsToMayor") or 5
    have = v.get("lead_verified") or 0
    lead = 100.0 * have / seats
    target = 100.0 if b.bar == "full" else lead
    prev = tl.beats[i - 1] if i else None
    start = (lead if prev is not None and prev.bar else 0.0)
    if prev is not None and prev.bar == b.bar:
        start = target
    k = min(max((t - b.t0) / 0.9, 0), 1)
    k = 1 - (1 - k) ** 3
    pct = start + (target - start) * k
    d = ImageDraw.Draw(img, "RGBA")
    x0, x1, y = PAD, SAFE_RIGHT - 20, 1330
    d.rectangle([x0, y, x1, y + 22], outline=(255, 255, 255, 150), width=2)
    d.rectangle([x0 + 5, y + 5, x0 + 5 + (x1 - x0 - 10) * pct / 100, y + 17],
                fill=RED + (255,))
    f = _font(F_MONO_B, 26)
    ru = tl.lang == "ru"
    if b.bar == "full":
        lab = (f"ЕСЛИ ИХ {seats}  ·  {pct:3.0f}%" if ru else f"WITH {seats} PEOPLE  ·  {pct:3.0f}%")
    else:
        lab = f"{'ЗАГРУЗКА' if ru else 'LOADING'}  ·  {have}/{seats}  ·  {pct:3.0f}%"
    d.text((x0, y - 44), lab, font=f, fill=(255, 255, 255, 220))


def end_card(ep: dict, lang: str, p: float) -> Image.Image:
    """The loop point: the mark, one line, the date. Nothing to click."""
    img = Image.new("RGB", (W, H), VOID)
    d = ImageDraw.Draw(img, "RGBA")
    step = 72
    a = int(10 + 8 * p)
    for x in range(0, W + 1, step):
        d.line([(x, 0), (x, H)], fill=(255, 255, 255, a))
    for y in range(0, H + 1, step):
        d.line([(0, y), (W, y)], fill=(255, 255, 255, a))
    mark = ctx().mark
    if mark.exists():
        m = Image.open(mark).convert("RGBA")
        mh = int(260 * (1.04 - 0.04 * min(p * 3, 1)))
        m = m.resize((max(1, round(m.width * mh / m.height)), mh), Image.LANCZOS)
        img.paste(m, ((W - m.width) // 2, 600 - mh // 2), m)
    line = (ep["end"].get(lang) or ep["end"]["en"]).upper()
    # fit: shrink until the line sits inside the safe width, then wrap at two
    # lines if it still would not (the French line ran off both edges)
    maxw, size = W - 2 * PAD - 40, 132
    while size > 72 and d.textlength(line, font=_font(F_SLAM, size)) > maxw:
        size -= 6
    f = _font(F_SLAM, size)
    rows = [line]
    if d.textlength(line, font=f) > maxw:
        words = line.split()
        half = len(words) // 2
        rows = [" ".join(words[:half]), " ".join(words[half:])]
    y = 860 - (len(rows) - 1) * size * 0.5
    for r in rows:
        d.text((W / 2, y), r, font=f, fill=WHITE + (255,), anchor="mm")
        y += size * 0.98
    fm = _font(F_MONO, 26)
    d.text((W / 2, 1000), "SCHEMA  ·  01.01.2027", font=fm,
           fill=(255, 255, 255, 150), anchor="mm")
    if int(p * 6) % 2 == 0:
        d.polygon([(W / 2 - 14, 1070), (W / 2 + 12, 1084), (W / 2 - 14, 1098)],
                  fill=RED + (255,))
    return img


#: the cold open's glyph-rain charset — mono, dense enough to read as noise
_OPEN_CHARS = "010100110101<>/\\|{}[]#*+=-.:;"


def title_open(ep: dict, lang: str, t: float, p: float) -> Image.Image:
    """The standard 3 s cold open: noise → signal → silence, then hold.

    Beats (absolute seconds from the cut): 0.0–0.8 full-frame glyph rain with
    the mark carved out of it as negative space; 0.8–1.4 interlace bands, the
    phase snapping per cut; 1.4–2.1 the settle composition sheared through
    horizontal slice-glitch states; 2.1–end the clean mark + wordmark on the
    void, held until the first beat lands at LEAD_IN + OPEN_HOLD. One colour
    per beat — red is the flood's energy, and it drains as the mark settles.

    Deterministic by design: every draw derives from the frame index (or its
    glitch state), so a re-render is byte-identical. ``p`` is the open's own
    0..1 progress, used only for the faint grid's breath.
    """
    fi = int(round(t * FPS))
    mark = ctx().mark
    img = Image.new("RGB", (W, H), VOID)

    def _mark(mh: int) -> Image.Image | None:
        if not mark.exists():
            return None
        m = Image.open(mark).convert("RGBA")
        return m.resize((max(1, round(m.width * mh / m.height)), mh), Image.LANCZOS)

    def ink(c: tuple, a: int) -> tuple[int, int, int]:
        """Text ink pre-blended over the VOID — ``d.text`` on an RGB layer
        ignores the fill's alpha and paints opaque, so the blend is ours."""
        return tuple(int(VOID[i] + (c[i] - VOID[i]) * max(0, min(a, 255)) / 255)
                     for i in range(3))

    def carve(mh: int, cy: int) -> None:
        """The mark is never drawn on — it is cut out of the field."""
        m = _mark(mh)
        if m is None:
            return
        sil = Image.new("RGBA", m.size, VOID + (255,))
        sil.putalpha(m.getchannel("A"))
        img.paste(sil, ((W - m.width) // 2, cy - m.height // 2), sil)

    def rain(phase: int) -> None:
        """Glyph noise falling ~2 cells a frame (60 cell/s); ``phase`` 0 =
        the flood, >0 = interlace bands on that phase (stripe phase shifts
        each cut — three distinct states)."""
        d = ImageDraw.Draw(img, "RGBA")
        f = _font(F_MONO, 20)
        cell = 26
        n = len(_OPEN_CHARS)
        for gy in range(H // cell + 1):
            if phase and ((gy + phase) % 4) >= 2:
                continue
            for gx in range(W // cell + 1):
                k = (gx * 13 + (gy + 2 * fi + gx * 5) * 7 + phase * 31) % n
                a = 34 + ((gx * 31 + gy * 17 + fi * 7) % 55) + (50 if phase else 0)
                col = (ink(RED, a + 70) if (gx * 3 + gy * 5 + fi) % 7 == 0
                       else ink((150, 165, 185), a))
                d.text((gx * cell, gy * cell), _OPEN_CHARS[k], font=f, fill=col)

    def settle() -> Image.Image:
        """The clean rest state: mark, wordmark, one small caption chip."""
        s = Image.new("RGB", (W, H), VOID)
        sd = ImageDraw.Draw(s, "RGBA")
        a = int(6 + 8 * p)
        for x in range(0, W + 1, 72):
            sd.line([(x, 0), (x, H)], fill=(255, 255, 255, a))
        for y in range(0, H + 1, 72):
            sd.line([(0, y), (W, y)], fill=(255, 255, 255, a))
        m = _mark(320)
        if m is not None:
            s.paste(m, ((W - m.width) // 2, 760 - m.height // 2), m)
        sd.text((W / 2, 985), "SCHEMA", font=_font(F_SLAM, 96),
                fill=WHITE + (255,), anchor="mm")
        k = min(max((t - 2.4) / 0.6, 0.0), 1.0)   # the caption fades in late
        if k > 0:
            sd.text((W / 2, 1075), f"{ep['tx']}  ·  {ep['coords']}",
                    font=_font(F_MONO, 24), fill=ink(WHITE, int(190 * k)),
                    anchor="mm")
        return s

    if t < 0.8:                                  # flood: the field, mark carved
        rain(0)
        carve(520, 780)
        return img
    if t < 1.4:                                  # interlace: phase snaps per cut
        rain(1 + int((t - 0.8) / 0.2))           # three states, 0.2 s apiece
        carve(520, 780)
        return img
    if t < 2.1:                                  # shear: slices converge to settle
        arr = np.asarray(settle()).copy()
        state = (fi - int(1.4 * FPS)) // 4        # six states, four frames apiece
        srng = np.random.default_rng(7000 + state)
        k = min(state / 6.0, 1.0)
        bh = H // 8
        for bi in range(8):
            if int(srng.integers(0, 3)) == 0:
                continue                          # some bands stay put — hard cuts
            off = int(srng.integers(-48, 49) * (1.0 - k))
            arr[bi * bh:(bi + 1) * bh] = np.roll(arr[bi * bh:(bi + 1) * bh], off, axis=1)
        arr = glitch(arr, 0.55 * (1.0 - k), 7000 + state)
        out = Image.fromarray(arr)
        dd = ImageDraw.Draw(out, "RGBA")
        f = _font(F_MONO, 22)
        for _ in range(12):                       # stray glyphs trail the slices
            x = int(srng.integers(0, W - 24))
            y = int(srng.integers(0, H - 24))
            dd.text((x, y), _OPEN_CHARS[int(srng.integers(0, len(_OPEN_CHARS)))],
                    font=f, fill=ink(WHITE, 175))
        return out
    return settle()                               # clean mark + wordmark, held
