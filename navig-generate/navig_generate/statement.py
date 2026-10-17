"""Render a STATEMENT overlay — one line, in Schema's own interface language.

Ported verbatim from the channel workspace's ``statement.py`` (every constant
was tuned by eye against renders, so this is a move, not a rewrite):

  * **Druk Medium** carries the statement. Condensed and editorial — it sets
    "A CITY LIGHTS WHEN 5 PEOPLE CLAIM IT" in 858px where GT Walsheim needs
    1748, which on a 1080 frame is the difference between one line and three.
  * **Fira Mono** carries the data, as it already did. Mono *is* the HUD voice.
  * **Corner brackets and a readout** frame it, so the picture reads as
    something being looked AT through an instrument, rather than a still with
    words on it.

Colour law unchanged: #FF6A5C carries the words, #D63A32 carries the shapes.
Safe zone unchanged: nothing below y=1540 — TikTok owns the bottom 380px.

The mark lives with the workspace (``<ws>/mark.png``), not here: media never
enters the navig repo.
"""
from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

__all__ = [
    "DRUK", "FLOOR", "H", "PAD", "SPLIT", "W",
    "brackets", "font", "grid", "missing_fonts", "prepare_line", "readout",
    "render", "scanlines", "shadow", "tracked", "tracked_w", "vcenter", "wrap",
]

W, H = 1080, 1920
FLOOR = H - 380
PAD = 60

#: What a `*red word*` marker becomes inside a rendered line: render() splits on
#: this and alternates the colours, so the halves are one stream of parts.
SPLIT = "\x01"

KET = (214, 58, 50, 255)
KET_T = (255, 106, 92, 255)       # video accent, redder than the #FFB4AE token
WHITE = (255, 255, 255, 255)
HAIR = (255, 255, 255, 64)        # the HUD hairline
HAIR_DIM = (255, 255, 255, 34)

WIN = "C:/Windows/Fonts/"
USR = str(Path.home() / "AppData/Local/Microsoft/Windows/Fonts") + "/"
DRUK = USR + "Druk-Medium.otf"
BEBAS = USR + "BebasNeue Bold.ttf"   # has Cyrillic; Druk does not (kinetic.py F_SLAM says the same)
INTER_MD = WIN + "Inter-Medium.ttf"
INTER_SB = WIN + "Inter-SemiBold.ttf"
MONO_B = USR + "FiraMono-Bold.ttf"
MONO_R = USR + "FiraMono-Regular.ttf"


def font(path: str, size: int):
    """Load a face, falling back rather than failing a whole render."""
    try:
        return ImageFont.truetype(path, size)
    except Exception:  # noqa: BLE001 — any font error falls back, never aborts
        return ImageFont.truetype(INTER_SB, size)


def missing_fonts() -> list[str]:
    """Names of the faces ``render`` needs that will not load.

    ``font()`` falls back to Inter-SemiBold for every face but that one —
    missing IT is fatal (the fallback itself is gone), anything else renders
    but diverges from byte-parity, which the CLI warns about.
    """
    faces = {
        "Druk-Medium.otf": DRUK,
        "Inter-Medium.ttf": INTER_MD,
        "Inter-SemiBold.ttf": INTER_SB,
        "FiraMono-Bold.ttf": MONO_B,
        "FiraMono-Regular.ttf": MONO_R,
    }
    missing = []
    for name, path in faces.items():
        try:
            ImageFont.truetype(path, 20)
        except Exception:  # noqa: BLE001 — any load error counts as absent
            missing.append(name)
    return missing


#: A private-use codepoint no face maps: whatever a face draws for it IS its
#: "missing glyph" (Druk: nothing at all; Bebas: a visible .notdef box).
_NOTDEF = "\ue000"


def covers(path: str, text: str) -> bool:
    """True when the face at ``path`` has a real glyph for every visible
    character of ``text``.

    Faces disagree about what a missing glyph looks like — Druk draws an EMPTY
    mask, Bebas draws a box — so each character is compared with the mask of an
    unmapped codepoint rather than tested for "has ink". Whitespace has no ink
    in any face and is never "missing".
    """
    try:
        f = ImageFont.truetype(path, 40)
    except Exception:  # noqa: BLE001 — an unloadable face covers nothing
        return False

    def key(ch: str):
        # public API only: draw the glyph and compare pixels (ImagingCore, what
        # getmask() returns, is private and has no tobytes())
        im = Image.new("L", (96, 96), 0)
        ImageDraw.Draw(im).text((8, 8), ch, font=f, fill=255)
        return im.tobytes()

    notdef = key(_NOTDEF)
    return all(key(ch) != notdef for ch in set(text) if not ch.isspace())


def pick_font(paths: list[str], text: str) -> str:
    """The first face in ``paths`` that can draw all of ``text``; the LAST one
    when none can (it draws *something* — a statement must never abort).

    This is why Latin and French still render through Druk, byte-identical to
    before, while a Russian line falls through to Bebas instead of coming out
    blank: Druk has no Cyrillic, and ``font()`` only falls back when a face
    fails to LOAD, not when it cannot draw the text.
    """
    for path in paths:
        if covers(path, text):
            return path
    return paths[-1]


def wrap(d, text, fnt, maxw):
    words, lines, cur = text.split(), [], ""
    for w in words:
        t = (cur + " " + w).strip()
        if d.textlength(t, font=fnt) <= maxw:
            cur = t
        else:
            if cur:
                lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines


def shadow(d, xy, text, fnt, *, blur=4, alpha=185):
    """A soft dark halo so text survives a bright patch of photograph."""
    x, y = xy
    for dx, dy in ((-2, 0), (2, 0), (0, -2), (0, 2), (-2, -2), (2, -2),
                   (-2, 2), (2, 2), (-blur, 0), (blur, 0), (0, blur)):
        d.text((x + dx, y + dy), text, font=fnt, fill=(6, 8, 12, alpha))


def tracked(d, xy, text, fnt, fill, track=0.0):
    x, y = xy
    for ch in text:
        d.text((x, y), ch, font=fnt, fill=fill)
        x += d.textlength(ch, font=fnt) + track
    return x - xy[0]


def tracked_w(d, text, fnt, track=0.0):
    return sum(d.textlength(c, font=fnt) for c in text) + track * max(0, len(text) - 1)


def vcenter(d, xy, text, fnt, box_h, fill, track=0.0):
    """Tracked text vertically centred in a badge of box_h starting at xy[1].

    The old fixed offset (y+8) left the OUTPOST/tag text riding high — the
    ascender gap of the face ate the slack instead of the glyph box. Ported
    from the workspace's ``statement.py`` (round-6b, owner screenshot).
    """
    bbox = d.textbbox((0, 0), text, font=fnt)
    ty = xy[1] + (box_h - (bbox[3] - bbox[1])) / 2 - bbox[1]
    return tracked(d, (xy[0], ty), text, fnt, fill, track)


def brackets(d, inset=44, arm=78, weight=3):
    """Hairline corner brackets — the world kit's targeting frame.

    Deliberately not a border. A border closes a picture; brackets say
    something is being LOOKED AT, which is the posture of the whole channel.
    """
    x0, y0, x1, y1 = inset, inset, W - inset, H - inset
    for cx, cy, dx, dy in ((x0, y0, 1, 1), (x1, y0, -1, 1),
                           (x0, y1, 1, -1), (x1, y1, -1, -1)):
        d.line([(cx, cy), (cx + dx * arm, cy)], fill=HAIR, width=weight)
        d.line([(cx, cy), (cx, cy + dy * arm)], fill=HAIR, width=weight)


def readout(d, spec: dict):
    """Monospaced data, top-right. Real values or nothing at all."""
    bits = [b for b in (spec.get("coords"), spec.get("wave"), spec.get("clock")) if b]
    if not bits:
        return
    f = font(MONO_R, 22)
    txt = "  ·  ".join(str(b) for b in bits)
    tw = tracked_w(d, txt, f, 2)
    tracked(d, (W - PAD - tw, 64), txt, f, (255, 255, 255, 150), 2)
    d.line([(W - PAD - tw, 98), (W - PAD, 98)], fill=HAIR_DIM, width=2)
    if spec.get("bip"):
        # the live square, left of the readout — it blinks because the caller
        # alternates this overlay with a bip-less twin, so the background
        # behind it stays continuous.
        d.rectangle([W - PAD - tw - 26, 69, W - PAD - tw - 14, 81], fill=KET)


def scanlines(out: Path, period: int = 4, alpha: int = 22) -> Path:
    """A CRT lattice — the faintest possible texture layer for video."""
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    for y in range(0, H, period):
        d.line([(0, y), (W, y)], fill=(0, 0, 0, alpha), width=1)
    img.save(out)
    return out


def grid(img: Image.Image, spec: dict):
    """A faint lattice behind the text block, and nowhere else."""
    if not spec.get("grid", True):
        return
    g = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(g)
    top, step = int(H * 0.62), 72
    for x in range(0, W + 1, step):
        d.line([(x, top), (x, FLOOR)], fill=(255, 255, 255, 12), width=1)
    for y in range(top, FLOOR + 1, step):
        d.line([(0, y), (W, y)], fill=(255, 255, 255, 12), width=1)
    img.alpha_composite(g)


def prepare_line(spec: dict) -> dict:
    """The CLI's one transform: `*red word*` markers become split separators.

    The original did this in its ``__main__``; a named function makes the
    contract testable — render() splits the line on SPLIT and alternates the
    colours across the parts.
    """
    out = dict(spec)
    out["line"] = (out.get("line") or "").replace("*", SPLIT)
    return out


def render(spec: dict, out: Path, mark: Path | None = None) -> Path:
    """Render the overlay to ``out`` (1080x1920 RGBA) and return its path."""
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    size = int(spec.get("size", 92))
    # Each face is chosen by the text it must draw (see pick_font): Latin and
    # French stay on Druk / Inter / Fira Mono, byte-identical; Cyrillic falls
    # through to Bebas (headline) / Inter (sub, tag) instead of rendering blank.
    line_txt = (spec.get("line") or "").upper().replace(SPLIT, "")
    f_line = font(pick_font([DRUK, BEBAS, INTER_SB], line_txt), int(size * 1.24))  # Druk sets small for its point size
    f_sub = font(pick_font([INTER_MD, INTER_SB], spec.get("sub", "") or ""), 34)
    f_tag = font(pick_font([MONO_B, INTER_SB], (spec.get("tag", "") or "").upper()), 25)
    f_wm = font(INTER_SB, 40)

    grid(img, spec)
    if spec.get("hud", True):
        brackets(d)
        readout(d, spec)

    if mark is not None and mark.exists() and spec.get("mark", True):
        m = Image.open(mark).convert("RGBA")
        mh = 46
        m = m.resize((max(1, round(m.width * mh / m.height)), mh), Image.LANCZOS)
        bbox = d.textbbox((0, 0), "SCHEMA", font=f_wm)
        cap_h = bbox[3] - bbox[1]
        centre = 78
        img.alpha_composite(m, (PAD, centre - mh // 2))
        tracked(d, (PAD + m.width + 18, centre - cap_h // 2 - bbox[1]),
                "SCHEMA", f_wm, (255, 255, 255, 228), 40 * 0.04)

    # Druk is drawn for capitals; lowercase in a condensed display face at
    # this size reads as a mistake rather than a choice.
    line = (spec.get("line") or "").upper()
    lines = wrap(d, line, f_line, W - 2 * PAD) if line else []
    lh = int(size * 1.08)
    sub = spec.get("sub", "")
    sub_lines = wrap(d, sub, f_sub, W - 2 * PAD) if sub else []

    total = len(lines) * lh + (len(sub_lines) * 46 + 26 if sub_lines else 0)
    tag = spec.get("tag", "")
    if tag:
        total += 58
    y = FLOOR - 56 - total

    if tag:
        d.polygon([(PAD, y + 10), (PAD + 16, y + 19), (PAD, y + 28)], fill=KET)
        tx = PAD + 29
        tw = tracked_w(d, tag.upper(), f_tag, 3)
        d.rectangle([tx, y, tx + tw + 28, y + 38], fill=KET)
        vcenter(d, (tx + 14, y), tag.upper(), f_tag, 38, WHITE, 3)
        y += 58

    for ln in lines:
        x = PAD
        for i, part in enumerate(ln.split(SPLIT)):
            if not part:
                continue
            shadow(d, (x, y), part, f_line, blur=5, alpha=190)
            d.text((x, y), part, font=f_line, fill=KET_T if i % 2 else WHITE)
            # textlength TRIMS a trailing space, so advancing by it alone
            # ran "WHEN " straight into the red "5 PEOPLE". Measure against
            # a sentinel and subtract it back out.
            x += (d.textlength(part + "|", font=f_line)
                  - d.textlength("|", font=f_line))
        y += lh

    if sub_lines:
        y += 26
        for ln in sub_lines:
            shadow(d, (PAD, y), ln, f_sub, blur=3, alpha=190)
            d.text((PAD, y), ln, font=f_sub, fill=(255, 255, 255, 212))
            y += 46

    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    img.save(out)
    return out
