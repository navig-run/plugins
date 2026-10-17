"""From a finished render to a ready-to-post pack — everything but the final click.

``build_pack`` does the steps that used to be done by hand after every render: check the
file's specs (:func:`navig_pipeline.qa.probe_video`), pull the cover from the hook (t=1.6s
— the whole headline and its sub are on screen), and write the posting entry (title,
caption, hashtags) next to the clip as ``<clip>.posting.md``. Optionally the entry is
also upserted into a series' ``POSTING.md`` between markers, so re-running replaces
rather than duplicates.

**Nothing is posted.** TikTok has no upload API in navig — that stays a human upload by
design. For YouTube Shorts the real ``social upload --dry-run`` is run, so the operator
sees exactly what would be sent; an actual upload is a separate, explicit command.

Copy comes from the episode's ``posting.json`` when it exists
(``{"title", "caption", "hashtags": [...]}``); otherwise it is drafted from the scene's
hook — the same words the video opens with.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from navig_pipeline.qa import Finding, probe_video

#: The hook's slam pops word by word (~1.0s for seven words), glitches (~1.15s) and its sub
#: fades in by ~1.3s; 0.3s caught "MY AI" half-drawn on the first real cover.
COVER_AT_S = 1.6
DEFAULT_TAGS = ("#navig", "#cli", "#opensource")
#: TikTok's caption limit; past it the app truncates silently.
CAPTION_MAX = 2200


class PublishError(RuntimeError):
    """The pack could not be built."""


@dataclass
class Pack:
    clip: Path
    title: str
    caption: str
    hashtags: list[str]
    cover: Path | None = None
    entry: Path | None = None
    findings: list[Finding] = field(default_factory=list)
    youtube: dict[str, Any] | None = None

    @property
    def blocking(self) -> list[Finding]:
        return [f for f in self.findings if f.level == "error"]

    def as_dict(self) -> dict[str, Any]:
        return {"clip": str(self.clip), "title": self.title, "caption": self.caption,
                "hashtags": self.hashtags, "cover": str(self.cover) if self.cover else None,
                "entry": str(self.entry) if self.entry else None,
                "findings": [f.as_dict() for f in self.findings], "youtube": self.youtube}


def _read(path: Path) -> str:
    with open(path, encoding="utf-8", newline="") as fh:
        return fh.read()


def _write(path: Path, text: str) -> None:
    with open(path, "w", encoding="utf-8", newline="") as fh:
        fh.write(text)


def _plain(text: str) -> str:
    """Scene markup (`*em*`, `**b**`, non-breaking hyphens) → plain caption text."""
    text = re.sub(r"\*{1,2}([^*]+)\*{1,2}", r"\1", text or "")
    return text.replace("‑", "-").replace("\n", " ").strip()


def copy_for(scene: dict[str, Any] | None, posting: dict[str, Any] | None, clip: Path) -> tuple[str, str, list[str]]:
    """Title, caption and hashtags: ``posting.json`` wins, else drafted from the hook."""
    posting = posting or {}
    hook = next((s for s in (scene or {}).get("segments", []) if s.get("type") == "hook"), {})
    outro = next((s for s in (scene or {}).get("segments", []) if s.get("type") == "outro"), {})
    title = posting.get("title") or _plain(hook.get("title", "")) or clip.stem.replace("-", " ")
    caption = posting.get("caption")
    if not caption:
        # The hook's sub is the payoff line; the install command goes on a line of its own
        # (joined with a space, "Here's why it couldn't pipx install navig" read as one
        # garbled sentence — seen on the first real pack).
        parts = [_plain(hook.get("sub", "")), f"→ {outro['cta']}" if outro.get("cta") else ""]
        caption = "\n".join(p for p in parts if p) or title
    tags = list(posting.get("hashtags") or DEFAULT_TAGS)
    tags = [t if t.startswith("#") else f"#{t}" for t in tags]
    return title, caption, tags


def scene_for_clip(clip: Path) -> Path | None:
    """The scene this render was cut from — read from its own shotlist, never guessed.

    ``out/`` belongs to ``clip.md``, ``out-v2/`` to ``clip-v2.md``; the shotlist's first
    ``stage:`` line names the scene. Sorting scene files by name picked ``scene.json`` for
    an ``out-v2`` render cut from ``scene-v2.json`` — the old hook copy, on the new video.
    """
    clip = Path(clip).resolve()
    out_dir = clip.parent
    episode = out_dir.parent if out_dir.name.startswith("out") else out_dir
    suffix = out_dir.name[len("out"):] if out_dir.name.startswith("out") else ""
    shotlist = episode / f"clip{suffix}.md"
    if shotlist.is_file():
        m = re.search(r'stage:([^"#\s]+)', shotlist.read_text(encoding="utf-8"))
        if m and (episode / m.group(1)).is_file():
            return episode / m.group(1)
    scenes = sorted(episode.glob("scene*.json"), key=lambda q: q.stat().st_mtime, reverse=True)
    scenes = [q for q in scenes if q.name.count(".") == 1]  # not scene.fr.json translations
    return scenes[0] if scenes else None


def extract_cover(clip: Path, out: Path, at: float = COVER_AT_S) -> Path:
    r = subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", f"{at:.3f}", "-i", str(clip),
                        "-frames:v", "1", "-q:v", "2", str(out)],
                       capture_output=True, encoding="utf-8", errors="replace", check=False)
    if r.returncode != 0 or not out.is_file():
        raise PublishError(f"could not extract the cover: {r.stderr.strip()[-300:]}")
    return out


def index_key(clip: Path, index: Path) -> str:
    """The clip's identity in a POSTING.md: its path relative to that file.

    Keyed by filename alone, a French cut (`out-v2-fr/clip-e07….mp4`) and the English one
    (`out-v2/clip-e07….mp4`) shared a key — the first French publish REPLACED the English
    entry. A relative path is unique per render and still readable in the file.
    """
    try:
        return Path(clip).resolve().relative_to(Path(index).resolve().parent).as_posix()
    except ValueError:
        return Path(clip).resolve().as_posix()


def entry_markdown(pack: Pack) -> str:
    tags = " ".join(pack.hashtags)
    lines = [f"### {pack.title}", "", f"- **File:** `{pack.clip.name}`"]
    if pack.cover:
        lines.append(f"- **Cover:** `{pack.cover.name}` (hook at {COVER_AT_S}s)")
    caption = "\n".join(f"  > {ln}" for ln in pack.caption.splitlines())
    lines += ["- **Caption:**", caption, f"- **Hashtags:** {tags}",
              "- **Upload:** TikTok by hand (no API) · YouTube Shorts: "
              "`social upload <file> --to youtube` after the dry-run below looks right"]
    if pack.findings:
        lines.append("- **QA:** " + "; ".join(f"{f.level} {f.rule}: {f.message}" for f in pack.findings))
    else:
        lines.append("- **QA:** specs ok (1080×1920 · 30 fps · yuv420p · audio)")
    return "\n".join(lines) + "\n"


def upsert(index: Path, key: str, block: str) -> None:
    """Replace the block between this clip's markers in ``index``, or append it."""
    start, end = f"<!-- publish:{key} -->", f"<!-- /publish:{key} -->"
    # newline="" both ways: the operator's file keeps ITS line endings. Path.write_text on
    # Windows turns every "\n" into "\r\n" — the first real run rewrote every line of an
    # LF POSTING.md to CRLF just to append one block.
    text = _read(index) if index.exists() else ""
    eol = "\r\n" if "\r\n" in text else "\n"
    body = eol.join([start, *block.rstrip().splitlines(), end]) + eol
    pattern = re.compile(re.escape(start) + r".*?" + re.escape(end) + r"(?:\r?\n)?", re.S)
    if pattern.search(text):
        text = pattern.sub(lambda _m: body, text, count=1)
    else:
        text = text.rstrip("\r\n") + (eol * 2 if text else "") + body
    _write(index, text)


def youtube_dry_run(pack: Pack) -> dict[str, Any]:
    """The real `social upload --dry-run` for a Short — nothing is sent."""
    try:
        import navig_social  # noqa: F401
    except ImportError:
        return {"ok": False, "skipped": "navig-social is not installed"}
    argv = [sys.executable, "-m", "navig_social.cli", "upload", str(pack.clip), "--to", "youtube",
            "--title", pack.title[:100], "--description", f"{pack.caption}\n\n{' '.join(pack.hashtags)}",
            "--tags", ",".join(t.lstrip("#") for t in pack.hashtags), "--dry-run", "--json"]
    r = subprocess.run(argv, capture_output=True, encoding="utf-8", errors="replace", timeout=120, check=False)
    try:
        payload = json.loads(r.stdout) if r.stdout.strip() else {}
    except ValueError:
        payload = {"raw": r.stdout.strip()[-500:]}
    return {"ok": r.returncode == 0, "exit": r.returncode, "result": payload,
            "error": r.stderr.strip()[-300:] if r.returncode else ""}


def build_pack(clip: Path, *, scene: dict[str, Any] | None = None, posting: dict[str, Any] | None = None,
               expect_secs: float | None = None, index: Path | None = None,
               youtube: bool = False) -> Pack:
    clip = Path(clip)
    if not clip.is_file():
        raise PublishError(f"no such clip: {clip}")
    title, caption, tags = copy_for(scene, posting, clip)
    pack = Pack(clip=clip, title=title, caption=caption, hashtags=tags)
    pack.findings = probe_video(clip, expect_secs=expect_secs)
    full = f"{caption} {' '.join(tags)}"
    if len(full) > CAPTION_MAX:
        pack.findings.append(Finding("warn", "copy", "caption-length",
                                     f"caption + hashtags is {len(full)} chars (TikTok cuts at {CAPTION_MAX})"))
    pack.cover = extract_cover(clip, clip.with_name(f"{clip.stem}.cover.jpg"))
    pack.entry = clip.with_name(f"{clip.stem}.posting.md")
    if youtube and not pack.blocking:
        pack.youtube = youtube_dry_run(pack)
    md = entry_markdown(pack)
    _write(pack.entry, md)
    if index is not None:
        upsert(index, index_key(clip, index), md)
    return pack
