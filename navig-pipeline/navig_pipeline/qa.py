"""Pre-flight QA for short-form video — catch what frame-hunting used to find.

Three layers, cheapest first:

* :func:`lint_scene` — the scene JSON alone, no browser: karaoke cues that strand a word on
  a screen of its own, cues too long for the band, cues that split a sentence, a terminal
  that will type an unredacted or overlong capture header instead of a clean command.
* :func:`check_layout` — the compiled stage's own measurements (``window.__stage.layout()``,
  taken after ``fit()`` with the longest cue in the band): a caption running into the panel
  under it, a load-bearing box in TikTok's UI strips.
* :func:`probe_video` — the rendered file: 1080×1920, 30 fps, ``yuv420p`` (anything else
  plays black on phones), an audio stream, the expected length.

Every rule here is a bug that shipped, or nearly did, in the first narrated series: the
orphaned "files." cue, the E05 caption cutting through its terminal, the E02 command
typed with a device flag. A finding is data — the caller decides whether a warning blocks.
"""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from navig_pipeline.captions import CUE_MERGE_MAX, CUE_TAIL_MIN

#: TikTok's own UI sits over these strips (stage pixels); see stage.css `--safe-*`.
SAFE_TOP, SAFE_BOTTOM, SAFE_RIGHT = 150, 1520, 930
#: How far a panel's frame may tuck under the bottom/right strips (stage.css `- 40px`).
PANEL_TUCK = 40
#: A caption needs this much air above the panel under it to read as separate.
CAPTION_GAP = 12
#: Longer than this and the typed command wraps to a third line on a 44-column panel.
COMMAND_MAX = 72
#: Flags that only exist because the capture had to address a device or a sandbox — a
#: viewer copying the command off the screen would get the wrong thing.
CAPTURE_ONLY = re.compile(r"(--udid\b|--config-dir\b|\bwsl -e\b|\bNAVIG_CONFIG_DIR=|>/dev/null)")


@dataclass
class Finding:
    level: str          # "error" | "warn"
    where: str          # "seg 2", "video", …
    rule: str           # short id, stable for scripts
    message: str

    def as_dict(self) -> dict[str, str]:
        return asdict(self)


def _cue_text(cue: Any) -> str:
    return str(cue[0]) if isinstance(cue, (list, tuple)) and cue else ""


def lint_scene(scene: dict[str, Any], casts: dict[int, dict[str, Any]] | None = None) -> list[Finding]:
    """Rules that need only the scene (and, optionally, each terminal's cast header)."""
    found: list[Finding] = []
    for i, seg in enumerate(scene.get("segments") or []):
        where = f"seg {i}"
        cues = ((seg.get("speech") or {}).get("cues")) or []
        for k, cue in enumerate(cues):
            text = _cue_text(cue).strip()
            if len(cues) > 1 and len(text) < CUE_TAIL_MIN:
                found.append(Finding("error", where, "orphan-cue",
                                     f"cue {k} {text!r} is a screen on its own — merge it into its neighbour"))
            if len(text) > CUE_MERGE_MAX:
                found.append(Finding("warn", where, "long-cue",
                                     f"cue {k} is {len(text)} chars (> {CUE_MERGE_MAX}) — it runs past three lines"))
            if k and text[:1].islower() and not re.search(r"[.!?,;:]$", _cue_text(cues[k - 1]).strip()):
                first = text.split()[0] if text.split() else ""
                if re.search(r"[.!?]$", first):
                    # "…delete production" | "files. Fortunately." — the word that FINISHES the
                    # previous screen's sentence opens this one. The reported orphan bug.
                    found.append(Finding("error", where, "stranded-tail",
                                         f"cue {k} opens with {first!r}, the end of cue {k - 1}'s sentence — "
                                         "it belongs on the previous screen"))
                else:
                    found.append(Finding("warn", where, "split-sentence",
                                         f"cue {k} {text[:24]!r}… continues a sentence cut mid-phrase"))
        if seg.get("type") == "terminal":
            command = seg.get("command")
            header = ((casts or {}).get(i) or {}).get("command") or ""
            shown = command if command is not None else header
            if command is None and CAPTURE_ONLY.search(header):
                found.append(Finding("error", where, "capture-command",
                                     "the cast header carries capture-only flags "
                                     f"({CAPTURE_ONLY.search(header).group(0).strip()}) and will be typed on "
                                     "screen — set `command` to what a viewer should run"))
            if shown and len(shown) > COMMAND_MAX:
                found.append(Finding("warn", where, "long-command",
                                     f"typed command is {len(shown)} chars — it wraps to 3+ lines; shorten `command`"))
    return found


def check_layout(boxes: list[dict[str, Any]]) -> list[Finding]:
    """Judge the stage's ``layout()`` measurements: collisions and safe zones."""
    found: list[Finding] = []
    for b in boxes:
        where = f"seg {b.get('index')}"
        cap, panel = b.get("caption"), b.get("panel")
        if cap and panel and cap["bottom"] + CAPTION_GAP > panel["top"]:
            found.append(Finding("error", where, "caption-collision",
                                 f"caption bottom {cap['bottom']} meets the {b.get('type')} panel at "
                                 f"{panel['top']} — the longest cue runs into it"))
        for name in ("caption", "panel", "title"):
            r = b.get(name)
            if not r:
                continue
            # Panels tuck PANEL_TUCK under both strips by design (stage.css: `bottom:
            # calc(var(--safe-bottom) - 40px)`) — chrome, not words. Text boxes get none.
            tuck = PANEL_TUCK if name == "panel" else 0
            if r["bottom"] > SAFE_BOTTOM + tuck:
                found.append(Finding("warn", where, "safe-bottom",
                                     f"{name} reaches y={r['bottom']} — under TikTok's caption bar (>{SAFE_BOTTOM})"))
            if r["top"] < SAFE_TOP:
                found.append(Finding("warn", where, "safe-top",
                                     f"{name} starts at y={r['top']} — under the status/HUD strip (<{SAFE_TOP})"))
            if r["right"] > SAFE_RIGHT + (PANEL_TUCK if name == "panel" else 0):
                found.append(Finding("warn", where, "safe-right",
                                     f"{name} reaches x={r['right']} — under the action rail (>{SAFE_RIGHT})"))
    return found


#: Seek here (fraction of the segment) before measuring: entrances have landed, nothing has
#: left yet. Rects include CSS transforms, so a mid-animation measurement is fiction — the
#: first version of this probe measured the full page at t≈0 and flagged a card still
#: sliding in from the right as sitting under the action rail.
SETTLED_AT = 0.9


async def measure_layout(page: Path, segments: int, *, port: int) -> list[dict[str, Any]]:
    """Measure each segment the way `clip` films it: alone (``?seg=i``), settled, post-fit."""
    import asyncio

    from navig_browser import cdp_actions

    from navig_pipeline.stage import page_url

    boxes: list[dict[str, Any]] = []
    for i in range(segments):
        await cdp_actions.navigate(port, page_url(page, seg=i, manual=1))
        for _ in range(100):  # fonts + fit, like record_stepped waits
            r = await cdp_actions.eval_js(port, "document.body.dataset.ready === '1'")
            if r.get("ok") and r.get("result"):
                break
            await asyncio.sleep(0.1)
        r = await cdp_actions.eval_js(
            port, f"window.__stage.seek(window.__stage.duration * {SETTLED_AT}).then(() => window.__stage.layout())")
        if not r.get("ok"):
            raise RuntimeError(f"could not read the stage layout of segment {i}: {r.get('error')}")
        boxes.extend(r.get("result") or [])
    return boxes


def probe_video(path: Path, *, expect_secs: float | None = None) -> list[Finding]:
    """The rendered file's specs — the ones a phone silently punishes."""
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries",
             "stream=codec_type,width,height,r_frame_rate,pix_fmt:format=duration",
             "-of", "json", str(path)],
            capture_output=True, encoding="utf-8", errors="replace", timeout=30, check=False,
        ).stdout
        info = json.loads(out or "{}")
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        return [Finding("error", "video", "unreadable", f"ffprobe could not read {path.name}: {exc}")]
    streams = info.get("streams") or []
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    found: list[Finding] = []
    if video is None:
        return [Finding("error", "video", "no-video", f"{path.name} has no video stream")]
    if (video.get("width"), video.get("height")) != (1080, 1920):
        found.append(Finding("error", "video", "size", f"{video.get('width')}×{video.get('height')}, not 1080×1920"))
    pix = video.get("pix_fmt")
    if pix == "yuvj420p":
        # Same 4:2:0 layout, FULL range: plays, but players and platform transcoders disagree
        # on its blacks. Stage renders before media-core's range fix all came out this way.
        found.append(Finding("warn", "video", "pix-range",
                             "yuvj420p (full range) — colours may shift on some players; "
                             "re-render for limited-range yuv420p"))
    elif pix != "yuv420p":
        found.append(Finding("error", "video", "pix-fmt",
                             f"pix_fmt {pix} — phones need 4:2:0 yuv420p or play black"))
    num, _, den = str(video.get("r_frame_rate", "0/1")).partition("/")
    try:
        fps = float(num) / float(den or 1)
    except ValueError:
        fps = 0.0
    if abs(fps - 30) > 0.01:
        found.append(Finding("warn", "video", "fps", f"{fps:g} fps, not 30"))
    if not any(s.get("codec_type") == "audio" for s in streams):
        found.append(Finding("error", "video", "no-audio", "no audio stream — the track was not muxed"))
    dur = float((info.get("format") or {}).get("duration") or 0)
    if expect_secs is not None and abs(dur - expect_secs) > 0.15:
        found.append(Finding("warn", "video", "duration", f"{dur:.2f}s, expected {expect_secs:.2f}s"))
    return found


# -- copy vs footage -------------------------------------------------------------
#
# E05's posting caption said "840 extensions scanned"; the video's real capture scanned 4.
# Copy is written by people (and now by `episode draft`), footage is captured — a number
# the viewer reads must be one the footage (or the scene's own text) actually shows.

_NUMBER = re.compile(r"(?<![\w.])\d[\d,.]*\d|(?<![\w.])\d(?![\w])")
_CODE = re.compile(r"`[^`]*`")


def _numbers(text: str) -> set[str]:
    """Numbers a reader sees, normalised (`3,072` = `3072`); code spans excluded."""
    return {n.replace(",", "").rstrip(".") for n in _NUMBER.findall(_CODE.sub(" ", text or ""))}


def lint_copy(posting: dict[str, Any] | None, scene: dict[str, Any], cast_texts: list[str]) -> list[Finding]:
    """Numbers in posting copy and on-screen hook text that no capture or scene text supports."""
    evidence = set()
    for t in cast_texts:
        evidence |= _numbers(_ANSI_RE.sub("", t))
    for seg in scene.get("segments") or []:
        for key in ("caption", "step", "label"):
            evidence |= _numbers(str(seg.get(key) or ""))
        for cue in ((seg.get("speech") or {}).get("cues")) or []:
            evidence |= _numbers(str(cue[0]))
    found: list[Finding] = []
    caption = (posting or {}).get("caption") or ""
    for n in sorted(_numbers(caption) - evidence):
        found.append(Finding("error", "posting", "unsupported-number",
                             f"caption claims {n!r}, which no capture or scene text shows — "
                             "copy must match footage"))
    hook = next((s for s in scene.get("segments") or [] if s.get("type") == "hook"), {})
    for n in sorted(_numbers(f"{hook.get('title', '')} {hook.get('sub', '')}") - evidence):
        found.append(Finding("warn", "seg 0", "unsupported-number",
                             f"hook says {n!r}, which no capture shows — check it is true"))
    return found


_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[@-~]")
