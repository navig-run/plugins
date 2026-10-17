"""Episodes — one folder, one command, from a narration plan to a ready-to-post pack.

An episode folder holds the irreplaceable sources (scene JSON, a clip shotlist, real
casts, a narration plan) and everything derived from them (voice, mix, render, cover).
:func:`build` runs the derivation in a fixed order and stops at the first stage that fails:

    narrate → qa (scene + layout) → render → qa (video) → publish → recipe

**Narration** (:func:`narrate`) is the part that used to live in a studio script
(``rebuild_episode.py``): synthesize each planned line (ElevenLabs, timed, cached by
content), join them with the planned silences, place the cuts where the voice says they
go, mix the bed at a measured level under the voice, limit, and write the speech-locked
karaoke cues into the scene. Its plan is ``rebuild.json``::

    {"bed": "deep-current" | "<path>", "length": 31.0, "lang": "en",
     "lines": [["n1", "daniel", "Text.", 1.3], ...],          # id, voice, text, gap after
     "segs":  [{"line": "n1", "hold": 0.5}, {"until": "n4", "lead": 0.5}, {}],
     "speech_map": [null, 1, 2], "updates": [{"i": 1, "set": {"pace": 5}}],
     "scene": "scene.json", "clip": "clip-v2.md", "vo_dir": "_vo"}

**Spending.** A line already in the voice cache costs nothing; a new one bills
ElevenLabs. Uncached lines are refused unless ``allow_spend`` — and the refusal names
them. A shotlist with generated (paid) video shots is refused the same way. Nothing is
ever uploaded: ``publish`` builds a pack, a human posts.

The remaining stages run the plugin's own verbs (``clip``, ``qa``, ``publish``,
``recipe``) as subprocesses with ``--json``, so an episode build behaves exactly like the
commands an operator would type — same browser lifecycle, same refusals, same files.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from navig_pipeline.captions import speech_block

Progress = Callable[[str], None]
#: A stage runner: argv (after `navig-pipeline`) → (exit code, parsed --json payload).
Runner = Callable[[list[str], Path], tuple[int, dict[str, Any]]]

#: Where shared beds live, relative to a series folder (videos/_music beside tiktok-series).
MUSIC_DIRS = ("../_music", "../../_music")
STAGES = ("narrate", "qa", "render", "qa-video", "publish", "recipe")
#: Seconds kept after the last spoken word when a voice outgrows the plan's length.
TAIL_S = 3.0


class EpisodeError(RuntimeError):
    """A stage could not run; the message says which and what to do."""


@dataclass
class Stage:
    name: str
    status: str = "pending"      # ran | skipped | failed | pending
    detail: str = ""
    seconds: float = 0.0
    data: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {"stage": self.name, "status": self.status, "detail": self.detail,
                "seconds": round(self.seconds, 2), **({"data": self.data} if self.data else {})}


# -- narration -----------------------------------------------------------------


def load_plan(episode: Path, plan: Path | None = None) -> dict[str, Any] | None:
    """The narration plan: ``plan`` if given, else ``<episode>/rebuild.json``, else None."""
    path = plan if plan is not None else episode / "rebuild.json"
    if plan is not None and not plan.is_absolute() and not plan.is_file() and (episode / plan).is_file():
        # `episode build e07 --plan rebuild.fr.json` from the series folder — the plan lives
        # in the episode (that is what `localize --narrate`'s own next-step hint prints)
        path = episode / plan
    if not path.is_file():
        if plan is not None:
            raise EpisodeError(f"narration plan not found: {path}")
        return None
    try:
        cfg = json.loads(path.read_text(encoding="utf-8"))
    except ValueError as exc:
        raise EpisodeError(f"{path.name} is not valid JSON: {exc}") from exc
    for key in ("lines", "segs", "speech_map", "length", "bed"):
        if key not in cfg:
            raise EpisodeError(f"{path.name} has no `{key}`")
    return cfg


def vo_dir(episode: Path, cfg: dict[str, Any]) -> Path:
    return episode / cfg.get("vo_dir", "_vo")


def uncached_lines(cfg: dict[str, Any], cache: Path) -> list[str]:
    """Line ids whose voice is not in the cache — the ones that would bill."""
    from navig_audio.voice.line import line_key

    lang = cfg.get("lang", "en")
    out = []
    for lid, voice, text, _gap in cfg["lines"]:
        key = line_key(text, voice, lang)
        if not ((cache / f"{key}.mp3").is_file() and (cache / f"{key}.json").is_file()):
            out.append(lid)
    return out


def _run(cmd: list[str]) -> subprocess.CompletedProcess:
    r = subprocess.run(cmd, capture_output=True, encoding="utf-8", errors="replace", check=False)
    if r.returncode != 0:
        raise EpisodeError(f"{Path(cmd[0]).name} failed: {r.stderr.strip()[-600:]}")
    return r


def _duration(path: Path) -> float:
    from navig_media_core.media.audio_edit import AudioEditError, probe_duration  # noqa: PLC0415

    try:
        return probe_duration(path)
    except AudioEditError as exc:
        raise EpisodeError(str(exc)) from exc


def loudness(path: Path, start: float, dur: float) -> float:
    """Integrated loudness (LUFS) of a window; -70 for silence or an unreadable window."""
    from navig_media_core.media.loudness import LoudnessError, measure  # noqa: PLC0415

    try:
        lufs = measure(path, start=start, duration=dur).lufs
    except LoudnessError:
        return -70.0
    return lufs if lufs != float("-inf") else -70.0


def synthesize(cfg: dict[str, Any], cache: Path, *, allow_spend: bool,
               progress: Progress | None = None) -> list[dict[str, Any]]:
    """Every planned line as a timed voice file (cache first; new lines only with consent)."""
    from navig_audio.voice.line import synth_timed

    missing = uncached_lines(cfg, cache)
    if missing and not allow_spend:
        raise EpisodeError(f"{len(missing)} line(s) not in the voice cache ({', '.join(missing)}) — "
                           "synthesizing bills ElevenLabs; re-run with --allow-spend")
    lang = cfg.get("lang", "en")
    rendered = []
    for lid, voice, text, gap in cfg["lines"]:
        r = synth_timed(text, voice, lang, cache)
        dur = _duration(r.path)
        rendered.append({"id": lid, "voice": voice, "text": text, "gap": gap,
                         "file": str(r.path), "dur": round(dur, 3), "words": r.words, "cached": r.cached})
        if progress:
            progress(f"    {lid} {voice:7s} {dur:5.2f}s {'cached' if r.cached else 'synth'}  {text[:44]}")
    return rendered


def assemble_voice(rendered: list[dict[str, Any]], out: Path) -> list[dict[str, Any]]:
    """Join the lines with their planned silences; returns ``lines`` with ``end`` set."""
    t, inputs, n = 0.0, [], 0
    for ln in rendered:
        inputs += ["-i", ln["file"]]
        n += 1
        t += ln["dur"]
        ln["end"] = round(t, 3)
        if ln["gap"] > 0:
            inputs += ["-f", "lavfi", "-t", f"{ln['gap']:.3f}", "-i", "anullsrc=r=44100:cl=mono"]
            n += 1
            t += ln["gap"]
    # every input normalised first: mp3s arrive stereo/fltp, anullsrc mono — concat refuses
    # mismatched layouts
    norm = "".join(f"[{i}:a]aformat=sample_fmts=fltp:sample_rates=44100:channel_layouts=mono[a{i}];"
                   for i in range(n))
    graph = norm + "".join(f"[a{i}]" for i in range(n)) + f"concat=n={n}:v=0:a=1[out]"
    _run(["ffmpeg", "-v", "error", "-y", *inputs, "-filter_complex", graph, "-map", "[out]",
          "-ar", "44100", "-ac", "1", str(out)])
    return rendered


def plan_sections(cfg: dict[str, Any], lines: list[dict[str, Any]]) -> list[float]:
    """Cut points from the plan: a line's end + hold, or a line's start − lead."""
    ends = {ln["id"]: ln["end"] for ln in lines}
    starts = {ln["id"]: ln["end"] - ln["dur"] for ln in lines}
    sections = [0.0]
    for i, seg in enumerate(cfg["segs"][:-1]):
        if "line" in seg:
            sections.append(round(ends[seg["line"]] + seg.get("hold", 0.5), 2))
        elif "until" in seg:
            sections.append(round(starts[seg["until"]] - seg.get("lead", 0.5), 2))
        else:
            raise EpisodeError(f"segment {i} of the plan needs `line` or `until`")
    if sections != sorted(sections):
        raise EpisodeError(f"the plan's cuts go backwards: {sections}")
    return sections


def resolve_bed(episode: Path, bed: str) -> Path:
    p = Path(bed)
    if p.is_file():
        return p
    for rel in MUSIC_DIRS:
        cand = (episode.parent / rel / f"{bed}.mp3").resolve()
        if cand.is_file():
            return cand
    raise EpisodeError(f"bed {bed!r} not found (a path, or a name in videos/_music)")


def mix(vo: Path, bed: Path, out_dir: Path, length: float) -> dict[str, float]:
    """Voice over a bed measured to sit ~6 LU under it, then limited to ~-14 LUFS.

    `navig audio master` re-levels BOTH inputs, which cancels any bed attenuation — so the
    mix is explicit: measured α, amix without normalisation, static gain, limiter.
    """
    padded = out_dir / "vo-padded.wav"
    _run(["ffmpeg", "-v", "error", "-y", "-i", str(vo), "-af", f"apad=whole_dur={length}",
          "-ar", "44100", "-ac", "1", str(padded)])
    speech, level = loudness(padded, 0.3, 2.4), loudness(bed, 0.0, 20.0)
    alpha = 10 ** ((speech - 6.0 - level) / 20)
    mixed = out_dir / "mix.wav"
    _run(["ffmpeg", "-v", "error", "-y", "-i", str(padded), "-i", str(bed), "-filter_complex",
          f"[1:a]volume={alpha:.4f}[bd];[0:a][bd]amix=inputs=2:duration=first:normalize=0[mx]",
          "-map", "[mx]", "-ar", "44100", "-ac", "1", str(mixed)])
    gain = round(-14 - loudness(mixed, 0.0, length), 2)
    final_wav = out_dir / "track-final.wav"
    _run(["ffmpeg", "-v", "error", "-y", "-i", str(mixed), "-af",
          f"volume={gain}dB,alimiter=limit=0.794:attack=5:release=60:level=false",
          "-ar", "44100", "-ac", "1", str(final_wav)])
    final = out_dir / "track-final.mp3"
    _run(["ffmpeg", "-v", "error", "-y", "-t", str(length), "-i", str(final_wav),
          "-c:a", "libmp3lame", "-q:a", "1", str(final)])
    return {"alpha": round(alpha, 4), "gain_db": gain}


def separation(track: Path, last_end: float) -> float:
    """Speech vs the bed-only tail after the last line, in LU (want ≥ 5)."""
    return round(loudness(track, 0.3, 2.2) - loudness(track, last_end + 0.4, 1.6), 1)


def timed_words(lines: list[dict[str, Any]]) -> list[tuple[str, float, float]]:
    out = []
    for ln in lines:
        base = ln["end"] - ln["dur"]
        for w in ln["words"]:
            out.append((w["w"], round(base + w["s"], 3), round(base + w["e"], 3)))
    return out


def write_clip_md(path: Path, sections: list[float], *, audio: str, scene: str,
                  segments: int, title: str) -> None:
    """Create the speech-locked shotlist, or move an existing one's cuts in place.

    An existing shotlist keeps its own section names, shot lines and extra frontmatter —
    only ``sections:`` (and ``audio:``) are rewritten, so a hand-tuned cut is never lost.
    """
    line = f"sections: [{', '.join(str(x) for x in sections)}]"
    if path.is_file():
        text = path.read_text(encoding="utf-8")
        text, hits = re.subn(r"^sections: \[[^\]]*\]$", line, text, count=1, flags=re.M)
        if not hits:
            raise EpisodeError(f"{path.name} has frontmatter but no `sections:` line to move")
        text = re.sub(r"^audio: .*$", f"audio: {audio}", text, count=1, flags=re.M)
        path.write_text(text, encoding="utf-8")
        return
    names = ["Hook", "A", "B", "C", "D", "E", "F", "G", "H"]
    md = ["---", f"title: {title}", f"audio: {audio}", line, "beat_sync: false", "---", ""]
    for i in range(segments):
        md += [f"## {names[i] if i < len(names) else i}", f'[shot: url="stage:{scene}#{i}"]', ""]
    path.write_text("\n".join(md), encoding="utf-8")


def narrate(episode: Path, cfg: dict[str, Any], *, allow_spend: bool = False,
            progress: Progress | None = None) -> dict[str, Any]:
    """Voice, mix, cut and caption an episode from its plan. Returns a summary."""
    vo = vo_dir(episode, cfg)
    vo.mkdir(parents=True, exist_ok=True)
    rendered = synthesize(cfg, vo / "cache", allow_spend=allow_spend, progress=progress)
    lines = assemble_voice(rendered, vo / "vo.wav")
    (vo / "beats.json").write_text(json.dumps(
        {"lines": [{k: v for k, v in ln.items() if k != "words"} for ln in lines]}, indent=2),
        encoding="utf-8")
    sections = plan_sections(cfg, lines)
    # A translated voice can run longer than the plan's original length; never cut the last
    # word or the outro's breath (no change for a plan whose length already covers it).
    length = max(float(cfg["length"]), round(max(ln["end"] for ln in lines) + TAIL_S, 2))
    levels = mix(vo / "vo.wav", resolve_bed(episode, cfg["bed"]), vo, length)
    sep = separation(vo / "track-final.mp3", max(ln["end"] for ln in lines))

    scene_path = episode / cfg.get("scene", "scene.json")
    scene = json.loads(scene_path.read_text(encoding="utf-8"))
    for u in cfg.get("updates") or []:
        scene["segments"][u["i"]].update(u["set"])
    words = timed_words(lines)
    for sec_i, seg_i in enumerate(cfg["speech_map"]):
        if seg_i is not None:
            scene["segments"][seg_i]["speech"] = speech_block(words, sections[sec_i], sections[sec_i + 1])
    scene_path.write_text(json.dumps(scene, indent=2, ensure_ascii=False), encoding="utf-8")

    clip = episode / cfg.get("clip", "clip-v2.md")
    write_clip_md(clip, sections, audio=f"{vo.name}/track-final.mp3", scene=scene_path.name,
                  segments=len(scene["segments"]), title=cfg.get("title", f"{episode.name} narrated"))
    return {"sections": sections, "length": length, "separation_lu": sep, **levels,
            "lines": len(lines), "synthesized": sum(1 for ln in rendered if not ln["cached"]),
            "clip": clip.name}


def plan_from_legacy(episode: Path, *, scene: str = "scene-v2.json", clip: str = "clip-v2.md",
                     bed: str = "abyss-bed-40s") -> dict[str, Any]:
    """Reconstruct a plan for an episode cut before ``rebuild.json`` existed.

    E07's lines live in ``_vo/build_vo.py`` (``LINES = [...]``), its timings in
    ``_vo/beats.json`` and its cuts in the shotlist. Each cut is re-expressed as
    "after line X, hold H" so the same plan places the cuts again from ANY voice — which
    is what a translated narration needs, its lines being longer or shorter.
    """
    import ast

    vo = episode / "_vo"
    src = (vo / "build_vo.py").read_text(encoding="utf-8")
    m = re.search(r"^LINES = (\[.*?^\])", src, re.S | re.M)
    if not m:
        raise EpisodeError(f"{episode.name}: no LINES list in _vo/build_vo.py")
    lines = [list(x) for x in ast.literal_eval(m.group(1))]
    beats = json.loads((vo / "beats.json").read_text(encoding="utf-8"))["lines"]
    md = (episode / clip).read_text(encoding="utf-8")
    sections = [float(x) for x in re.search(r"^sections: \[([^\]]+)\]", md, re.M).group(1).split(",")]
    segs: list[dict[str, Any]] = []
    for cut in sections[1:]:
        before = [b for b in beats if b["end"] <= cut + 1e-6]
        if not before:
            raise EpisodeError(f"{episode.name}: no line ends before the cut at {cut}s")
        last = before[-1]
        segs.append({"line": last["id"], "hold": round(cut - last["end"], 3)})
    segs.append({})
    scene_doc = json.loads((episode / scene).read_text(encoding="utf-8"))
    speech_map = [i if seg.get("speech") else None for i, seg in enumerate(scene_doc["segments"])]
    length = round(_duration(vo / "track-final.mp3"), 2)
    return {"bed": bed, "length": length, "lang": "en", "lines": lines, "segs": segs,
            "speech_map": speech_map[:len(sections)], "updates": [], "scene": scene, "clip": clip,
            "vo_dir": "_vo"}


# -- the build -------------------------------------------------------------------


def shotlist_for(episode: Path, cfg: dict[str, Any] | None) -> tuple[Path, Path]:
    """(shotlist, out dir): the plan's clip, else clip-v2.md, else clip.md."""
    if cfg is not None:
        md = episode / cfg.get("clip", "clip-v2.md")
    elif (episode / "clip-v2.md").is_file():
        md = episode / "clip-v2.md"
    else:
        md = episode / "clip.md"
    if not md.is_file():
        raise EpisodeError(f"{episode.name}: no shotlist ({md.name})")
    suffix = md.stem[len("clip"):]                 # "" | "-v2" | "-v2-fr"
    return md, episode / f"out{suffix}"


def scene_of(shotlist: Path) -> Path:
    m = re.search(r'stage:([^"#\s]+)', shotlist.read_text(encoding="utf-8"))
    if not m:
        raise EpisodeError(f"{shotlist.name} films no stage scene")
    return shotlist.parent / m.group(1)


def cli_runner(argv: list[str], cwd: Path) -> tuple[int, dict[str, Any]]:
    """Run one of this plugin's own verbs with --json; (exit code, payload)."""
    r = subprocess.run([sys.executable, "-m", "navig_pipeline.cli", *argv], cwd=str(cwd),
                       capture_output=True, encoding="utf-8", errors="replace", check=False)
    payload: dict[str, Any] = {}
    start = r.stdout.find("{")
    if start != -1:
        try:
            payload = json.loads(r.stdout[start:])
        except ValueError:
            payload = {}
    if r.returncode and not payload:
        payload = {"error": (r.stderr or r.stdout).strip()[-500:]}
    return r.returncode, payload


def _newest_mp4(out_dir: Path, since: float) -> Path | None:
    files = [p for p in out_dir.glob("*.mp4") if p.stat().st_mtime >= since]
    return max(files, key=lambda p: p.stat().st_mtime) if files else None


def build(episode: Path, *, plan: Path | None = None, allow_spend: bool = False, layout: bool = True,
          index: Path | None = None, youtube: bool = False, recipe_out: Path | None = None,
          runner: Runner = cli_runner, progress: Progress | None = None) -> list[Stage]:
    """Run every stage in order; stop at the first failure. Returns one row per stage."""
    episode = Path(episode).resolve()
    stages = [Stage(name) for name in STAGES]
    by = {s.name: s for s in stages}
    ctx: dict[str, Any] = {}

    def step(name: str, fn: Callable[[], str | None]) -> bool:
        s = by[name]
        t0 = time.monotonic()
        if progress:
            progress(f"▶ {name}")
        try:
            detail = fn()
            s.status = "skipped" if detail is not None and detail.startswith("skip:") else "ran"
            s.detail = (detail or "").removeprefix("skip:").strip()
        except EpisodeError as exc:
            s.status, s.detail = "failed", str(exc)
        s.seconds = time.monotonic() - t0
        return s.status != "failed"

    def do_narrate() -> str:
        cfg = load_plan(episode, plan)
        ctx["cfg"] = cfg
        if cfg is None:
            return "skip: no rebuild.json — using the episode's existing track"
        summary = narrate(episode, cfg, allow_spend=allow_spend, progress=progress)
        by["narrate"].data = summary
        warn = "" if summary["separation_lu"] >= 5 else " (below 5 LU — the bed may mask words)"
        return (f"{summary['lines']} lines · cuts {summary['sections']} · "
                f"speech {summary['separation_lu']} LU over the bed{warn}")

    def do_qa() -> str:
        md, out_dir = shotlist_for(episode, ctx.get("cfg"))
        ctx["shotlist"], ctx["out"] = md, out_dir
        scene = scene_of(md)
        ctx["scene"] = scene
        code, data = runner(["qa", scene.name, "--json", *(["--layout"] if layout else [])], episode)
        findings = data.get("findings") or []
        by["qa"].data = {"findings": findings}
        if code:
            raise EpisodeError("scene QA failed: " + "; ".join(
                f"{f['where']} {f['rule']}" for f in findings if f.get("level") == "error")
                or data.get("error", "see `qa`"))
        return f"{scene.name} clean" + (f" · {len(findings)} warning(s)" if findings else "")

    def do_render() -> str:
        md, out_dir = ctx["shotlist"], ctx["out"]
        code, plan_data = runner(["clip", md.name, "--dry-run", "--json"], episode)
        if code:
            raise EpisodeError(f"render plan failed: {plan_data.get('error', '')}")
        if plan_data.get("generate") and not allow_spend:
            raise EpisodeError(f"{plan_data['generate']} shot(s) would be generated (paid video) — "
                               "re-run with --allow-spend, or point them at existing footage")
        ctx["expect"] = plan_data.get("audio_s")
        t0 = time.time() - 1
        code, data = runner(["clip", md.name, "-o", out_dir.name], episode)
        video = _newest_mp4(out_dir, t0)
        if code or video is None:
            raise EpisodeError(f"render failed: {data.get('error', 'no mp4 written')}")
        ctx["video"] = video
        return f"{out_dir.name}/{video.name}"

    def do_qa_video() -> str:
        video = ctx["video"]
        argv = ["qa", ctx["scene"].name, "--video", str(video), "--json"]
        if ctx.get("expect"):
            argv += ["--expect", f"{ctx['expect']:.2f}"]
        code, data = runner(argv, episode)
        findings = data.get("findings") or []
        by["qa-video"].data = {"findings": findings}
        if code:
            raise EpisodeError("render QA failed: " + "; ".join(
                f"{f['rule']}: {f['message']}" for f in findings if f.get("level") == "error"))
        return "specs ok" + (f" · {len(findings)} warning(s)" if findings else "")

    def do_publish() -> str:
        argv = ["publish", str(ctx["video"]), "--json"]
        if index is not None:
            argv += ["--index", str(index)]
        if youtube:
            argv += ["--youtube"]
        code, data = runner(argv, episode)
        by["publish"].data = {k: data.get(k) for k in ("title", "cover", "entry")}
        if code:
            raise EpisodeError("not ready to post: " + "; ".join(
                f.get("message", "") for f in data.get("findings", []) if f.get("level") == "error"))
        return f"{data.get('title', '')!r} — cover + entry written"

    def do_recipe() -> str:
        dst = recipe_out or episode.with_name(f"{episode.name}.recipe.json")
        code, data = runner(["recipe", "export", episode.name, "-o", str(dst)], episode.parent)
        if code:
            raise EpisodeError(f"recipe export failed: {data.get('error', '')}")
        return dst.name

    for name, fn in (("narrate", do_narrate), ("qa", do_qa), ("render", do_render),
                     ("qa-video", do_qa_video), ("publish", do_publish), ("recipe", do_recipe)):
        if not step(name, fn):
            break
    return stages
