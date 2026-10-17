"""A series — its episodes side by side: what each cut IS, and how it did.

The measure half already exists in navig-social: an engagement ledger keyed by campaign,
network and post (``navig social engagement / report / sync``). A series reuses it —
campaign = the series (``tiktok-s01`` by default, or ``posting.json`` ``campaign``),
network = ``tiktok``, post = the episode folder name — so stats recorded here show up in
``navig social report`` too, and nothing new is stored.

TikTok has no analytics API, so numbers come in by hand from the app's insights screen:
``record`` stores them as *snapshots* (``upsert_metric`` — re-entering replaces, never
sums). ``facts`` reads what the cut is from the episode's own sources (shotlist cuts,
narration plan, voice timings), which is what makes a comparison mean something: a
retention gap next to a 4.8s hook vs a 3.3s one is a lead; two numbers alone are noise.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

DEFAULT_CAMPAIGN = "tiktok-s01"
NETWORK = "tiktok"
#: The numbers TikTok's insights screen shows, as stored (integers, like the ledger).
METRICS = ("views", "likes", "comments", "shares", "saves", "avg_watch_ms", "completion_pct", "follows")


class SeriesError(RuntimeError):
    """A series operation could not complete."""


def _duration(path: Path) -> float | None:
    from navig_media_core.media.audio_edit import AudioEditError, probe_duration  # noqa: PLC0415

    try:
        return round(probe_duration(path, timeout=30), 2)
    except AudioEditError:
        return None


def campaign_for(episode: Path, default: str = DEFAULT_CAMPAIGN) -> str:
    p = episode / "posting.json"
    if p.is_file():
        try:
            return json.loads(p.read_text(encoding="utf-8")).get("campaign") or default
        except ValueError:
            return default
    return default


def episodes(series: Path) -> list[Path]:
    """Episode folders of a series: any child holding a scene and a shotlist."""
    return sorted(d for d in Path(series).iterdir()
                  if d.is_dir() and any(d.glob("scene*.json")) and any(d.glob("clip*.md")))


def facts(episode: Path) -> dict[str, Any]:
    """What this cut is: length, hook length, cuts, bed, narration pace."""
    md = episode / "clip-v2.md" if (episode / "clip-v2.md").is_file() else episode / "clip.md"
    out: dict[str, Any] = {"episode": episode.name, "shotlist": md.name if md.is_file() else None}
    if md.is_file():
        text = md.read_text(encoding="utf-8")
        m = re.search(r"^sections: \[([^\]]+)\]", text, re.M)
        if m:
            cuts = [float(x) for x in m.group(1).split(",")]
            out["cuts"] = len(cuts)
            out["hook_s"] = round(cuts[1], 2) if len(cuts) > 1 else None
        a = re.search(r"^audio: (.+)$", text, re.M)
        if a:
            out["audio"] = a.group(1).strip()
    # Length = the RENDER the viewer watched (watch ratio divides by it). The voice's last
    # word is not it — E07's narration ends at 22.1s, its video runs 24.6s.
    if md.is_file():
        renders = sorted((episode / f"out{md.stem[len('clip'):]}").glob("*.mp4"),
                         key=lambda p: p.stat().st_mtime, reverse=True)
        if renders:
            out["length_s"] = _duration(renders[0])
    plan = episode / "rebuild.json"
    if plan.is_file():
        cfg = json.loads(plan.read_text(encoding="utf-8"))
        out["bed"] = Path(str(cfg.get("bed", ""))).stem or None
        out.setdefault("length_s", cfg.get("length"))
    beats = episode / "_vo" / "beats.json"
    if beats.is_file():
        lines = json.loads(beats.read_text(encoding="utf-8")).get("lines") or []
        words = sum(len(str(ln.get("text", "")).split()) for ln in lines)
        spoken = sum(float(ln.get("dur", 0)) for ln in lines)
        out["narration"] = "voiced"
        out["wps"] = round(words / spoken, 2) if spoken else None
        out.setdefault("length_s", round(max((ln.get("end", 0) for ln in lines), default=0), 2) or None)
    else:
        out["narration"] = "music"
    return out


def _store():
    try:
        from navig_social.social.engagement import get_engagement
    except ImportError as exc:  # pragma: no cover - depends on the install
        raise SeriesError("stats live in navig-social's engagement ledger — install navig-social") from exc
    return get_engagement()


def record(episode: Path, values: dict[str, int], *, campaign: str | None = None, store: Any = None) -> str:
    """Store a snapshot of this episode's numbers (replace-latest per metric)."""
    store = store or _store()
    campaign = campaign or campaign_for(episode)
    clean = {k: int(v) for k, v in values.items() if v is not None}
    if not clean:
        raise SeriesError("nothing to record — pass at least one metric")
    for metric, value in clean.items():
        if value < 0:
            raise SeriesError(f"{metric} cannot be negative")
        store.upsert_metric(campaign=campaign, network=NETWORK, post_id=episode.name,
                            metric=metric, value=value, source="manual")
    return campaign


def table(series: Path, *, campaign: str = DEFAULT_CAMPAIGN, store: Any = None) -> list[dict[str, Any]]:
    """One row per episode: its facts joined with its recorded numbers."""
    store = store or _store()
    numbers = store.post_metrics(campaign, network=NETWORK)
    rows = []
    for ep in episodes(series):
        row = facts(ep)
        row.update(numbers.get(ep.name, {}))
        views, watch, length = row.get("views"), row.get("avg_watch_ms"), row.get("length_s")
        if watch and length:
            row["watch_ratio"] = round(watch / 1000 / float(length), 2)
        if views and row.get("follows") is not None:
            row["follow_per_k"] = round(1000 * row["follows"] / views, 1)
        rows.append(row)
    return rows
