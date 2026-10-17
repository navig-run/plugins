"""Ingest a dump of audio clips into a sound library — classified, named, cleaned, reversible.

The use case is a folder of rips (TikTok/Instagram/YouTube): names like
``Feel_the_voice_of_Nature_#throatsinging_#alexkuula_7508306146298.m4a``, ``audio (3).mp3``,
a hex hash, or a ``.octet-stream`` that is really audio. ``audio-sort`` answers *music, voice
or sfx* from a model trained on folders you already sorted; this answers *what is it, what
should it be called, and give me a clean mp3 of it* — with no training, from three kinds of
evidence, every decision explained in a ``reason``:

1. **keywords** in the filename and its hashtags (and in a transcript, with ``--transcribe``),
   matched against category rules (defaults below; ``--rules`` YAML overrides or extends them);
2. **audio**: the share of the clip Silero VAD calls speech, plus the harmonicity and beat
   strength ``audio_class`` already measures;
3. when neither speaks clearly, the clip goes to ``_review/`` — never a guess.

Each file is converted to mp3 (light denoise, edge-silence trim, loudness-normalised) under
``LIBRARY/<category>/<slug>.mp3``; the original is moved (or copied) to
``LIBRARY/_originals/<its path under SRC>``. The JSONL log is written before each step, so
``audio-ingest-undo`` puts every original back and removes what was made from it.
"""

from __future__ import annotations

import csv
import hashlib
import json
import re
import shutil
import subprocess
import time
from collections import Counter
from dataclasses import asdict, dataclass
from pathlib import Path

# category → keywords (lower-case; matched against filename words, hashtags, transcript).
# Order is the tie-break. Multilingual on purpose: the rips are EN / RU / FR.
DEFAULT_RULES: dict[str, list[str]] = {
    "voice-singing": ["throatsinging", "throat", "chant", "chanting", "monk", "monks", "monastery", "choir",
                      "chor", "maedchenchor", "knabenchor", "chorus", "mantra", "gregorian", "singing", "singer", "vocal", "acapella", "a cappella",
                      "harmonies", "overtone", "khoomei", "avemaria", "opera", "hymn", "anthem", "march", "marche",
                      "militaire", "legion", "coloratura", "aria", "soprano", "tenor", "duet", "handel",
                      "горлов", "хор", "мантр", "пение", "гимн", "марш", "chorale"],
    "instruments": ["flute", "cedar", "kalimba", "handpan", "hangdrum", "didgeridoo", "sitar", "shakuhachi",
                    "duduk", "harp", "violin", "cello", "accordion", "flûte", "флейт", "дудук", "варган"],
    "voice-speech": ["speech", "quote", "quotes", "motivation", "motivational", "discipline", "podcast",
                     "interview", "reportage", "documentary", "news", "talk", "lecture", "psychology",
                     "psychologicalquotes", "rules", "power", "philosophy", "story", "речь", "цитат",
                     "интервью", "мотивац", "parole", "discours", "écoute", "ecoutes", "reportag", "governor"],
    "nature": ["nature", "animal", "animals", "animalworld", "bird", "birds", "bellbird", "wolf", "rain", "forest",
               "water", "ocean", "sea", "wind", "thunder", "storm", "river", "jungle", "insect", "frog", "owl",
               "природ", "птиц", "лес", "дожд", "волк", "oiseau", "pluie", "forêt"],
    "machines": ["construction", "machine", "machinery", "engine", "factory", "industrial", "backhoe",
                 "excavator", "loader", "siteclearance", "drill", "train", "metal", "robot", "warehouse",
                 "стройк", "машин", "завод", "chantier", "usine"],
    "drones-tones": ["crystal", "bowl", "bowls", "singingbowl", "binaural", "binauralbeats", "meditation",
                     "healing", "frequency", "hz", "tuner", "drone", "gong", "tibetan", "chakra", "solfeggio",
                     "brain", "brainwave", "focus", "relax", "sleep", "potential", "медитац", "чаш", "кристал"],
    "songs": ["cover", "song", "music", "musician", "remix", "beat", "guitar", "piano", "band", "systemofadown",
              "drumandbass", "dnb", "techno", "trap", "hiphop",
              "toxicity", "schubert", "bernstein", "rap", "песня", "кавер", "chanson", "musique"],
    "fx": ["sfx", "effect", "effects", "whoosh", "impact", "glitch", "explosion", "gunshot", "laser", "sound effect",
           "siren", "tornado", "alarm", "hammer", "thor", "scary", "creepy", "horror", "radio", "uvb76", "doomsday",
           "signal", "morse", "beep", "эффект", "сирен", "bruitage"],
    "ambience": ["asmr", "ambience", "ambient", "atmosphere", "city", "street", "crowd", "room", "night",
                 "space", "cosmos", "quasar", "pulsar", "nasa", "universe", "космос", "атмосфер", "город", "ambiance"],
}

# platform junk stripped from names before slugging (and never counted as a keyword)
_JUNK = [
    r"tiktok video\s*#?\d*", r"#?\d{9,}", r"\b[0-9a-f]{16,}\b", r"musicaldown\.com", r"snaptik\w*", r"ssstik\w*",
    r"savetik\.co", r"fdownloader\.net", r"y2mate\.\w+", r"\(\d{2,3}\s*kbps\)",
    r"original sound\s*-?", r"\baudio\s*\(\d+\)", r"\bapplication\s*\(\d+\)", r"\(\d+\)",
    r"#(fyp|foryou|foryoupage|foryourpage|viral|tiktok|reels|trending|xyzbca|explore|fy|capcut)\b",
]
_STOP = {"the", "a", "an", "of", "and", "to", "in", "is", "it", "on", "for", "with", "that", "this", "you", "your",
         "my", "at", "by", "from", "be", "are", "was", "how", "what", "who", "one", "most"}
MIN_CONF = 0.6
LOG = "audio-ingest-log.jsonl"


class IngestError(RuntimeError):
    """ffmpeg is missing or a file could not be converted."""


@dataclass
class Item:
    src: str
    category: str
    confidence: float
    reason: str
    name: str
    dest: str
    original_dest: str
    duration: float = 0.0
    transcript: str = ""
    action: str = "ingest"


# ── naming ───────────────────────────────────────────────────────────────────

def clean_name(stem: str) -> str:
    """The meaningful words of a rip's filename: platform IDs, hashes and junk tags removed."""
    s = stem.replace("_", " ")
    for pat in _JUNK:
        s = re.sub(pat, " ", s, flags=re.I)
    s = s.replace("#", " ")
    return re.sub(r"\s{2,}", " ", s).strip(" -.,")


def words(text: str) -> list[str]:
    return [w for w in re.findall(r"[\w']+", text.lower()) if w not in _STOP]


def hashtags(stem: str) -> list[str]:
    return [h.lower() for h in re.findall(r"#(\w+)", stem)]


# ── classification ───────────────────────────────────────────────────────────

def load_rules(path: Path | None) -> dict[str, list[str]]:
    rules = {k: list(v) for k, v in DEFAULT_RULES.items()}
    if path:
        import yaml  # noqa: PLC0415  (only when a rules file is given)

        extra = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
        for cat, kws in extra.items():
            rules[cat] = [str(k).lower() for k in kws or []]
    return rules


def keyword_scores(stem: str, transcript: str, rules: dict[str, list[str]]) -> Counter:
    """Hashtags count double (the uploader's own label); filename words and transcript once."""
    tags = hashtags(stem)
    text_words = words(clean_name(stem)) + words(transcript)
    score: Counter = Counter()
    for cat, kws in rules.items():
        for kw in kws:
            # hashtags are compounds (#maedchenchorhamburg, #звуккосмоса): a keyword of 5+
            # letters anywhere inside one counts (5, not 4: "thor" would fire on #author)
            if any(t == kw or t.startswith(kw) or (len(kw) >= 5 and kw in t) for t in tags):
                score[cat] += 2
            if any(w == kw or (len(kw) >= 4 and w.startswith(kw)) for w in text_words):
                score[cat] += 1
    return score


def classify(stem: str, feats: dict | None, transcript: str, rules: dict[str, list[str]]) -> tuple[str, float, str]:
    score = keyword_scores(stem, transcript, rules)
    order = list(rules)
    speech = (feats or {}).get("speech_ratio", 0.0)
    if score:
        best = max(score.items(), key=lambda kv: (kv[1], -order.index(kv[0])))
        cat, hits = best
        conf = 0.9 if hits >= 2 else 0.7
        reason = f"keywords → {cat} ({hits})"
        # the audio can contradict a "speech" label with no speech in it, or vice versa
        if feats is not None and cat == "voice-speech" and speech < 0.15:
            conf -= 0.2
            reason += f"; but speech_ratio {speech:.2f}"
        return cat, round(conf, 2), reason
    if feats is None:
        return "unknown", 0.3, "no keyword, no audio features"
    if speech >= 0.5 or (speech >= 0.3 and len(transcript.split()) >= 4):
        return "voice-speech", 0.65, f"speech_ratio {speech:.2f}" + (" + transcript" if speech < 0.5 else "")
    beat, harm = feats.get("beat_strength", 0.0), feats.get("harmonicity_mean", 0.0)
    # a siren or a drone has rhythm OR tone; a song needs both, clearly
    if beat >= 0.5 and harm >= 0.4:
        return "songs", 0.6, f"beat {beat:.2f} · harmonicity {harm:.2f}"
    # clearly tonal and nobody talking: music, even with no beat (arias, flutes, street players)
    if harm >= 0.5 and speech < 0.2:
        return "music", 0.6, f"tonal: harmonicity {harm:.2f} · speech {speech:.2f}"
    return "unknown", 0.4, f"speech {speech:.2f} · beat {beat:.2f} · harmonicity {harm:.2f}"


def transcribe(path: Path, model_name: str = "small") -> str:
    """A short transcript of the middle 30 s (faster-whisper, local). Empty on any failure."""
    try:
        from faster_whisper import WhisperModel  # noqa: PLC0415

        from navig_explore import audio_class as ac  # noqa: PLC0415

        x = ac.decode_window(path, ac.probe_duration(path))
        if x is None:
            return ""
        model = _whisper(model_name, WhisperModel)
        segs, _ = model.transcribe(x, vad_filter=True, beam_size=1, condition_on_previous_text=False)
        return plausible(" ".join(s.text.strip() for s in segs)[:300])
    except Exception:  # noqa: BLE001 — a transcript is evidence, never a requirement
        return ""


def plausible(text: str) -> str:
    """Whisper over music invents text, often in a script the clip has nothing to do with
    (Sinhala, Arabic, CJK over a beat). Keep a transcript only when it is mostly Latin or
    Cyrillic letters — the languages these libraries actually hold — and say nothing otherwise."""
    letters = [c for c in text if c.isalpha()]
    if not letters:
        return ""
    ok = sum(1 for c in letters if c.isascii() or "À" <= c <= "ɏ" or "Ѐ" <= c <= "ӿ")
    return text if ok / len(letters) >= 0.8 else ""


_models: dict = {}


def _whisper(name: str, cls):
    if name not in _models:
        _models[name] = cls(name, device="auto", compute_type="int8")
    return _models[name]


# ── planning ─────────────────────────────────────────────────────────────────

def iter_audio(src: Path) -> list[Path]:
    from navig_explore import audio_class as ac  # noqa: PLC0415

    return list(ac.iter_audio(src))


def plan(src: Path, library: Path, *, rules: dict[str, list[str]], use_audio: bool = True,
         use_transcript: bool = False, min_conf: float = MIN_CONF, limit: int | None = None,
         progress=None) -> list[Item]:
    from navig_explore import audio_class as ac  # noqa: PLC0415

    files = iter_audio(src)[:limit] if limit else iter_audio(src)
    taken: set[str] = set()
    items: list[Item] = []
    counters: Counter = Counter()
    cache = _Cache(library / ".mediaexplorer" / "audio-ingest-cache.jsonl")
    for n, f in enumerate(files, 1):
        hit = cache.get(f)
        dur = hit.get("duration") if hit else ac.probe_duration(f)
        feats = hit.get("feats") if hit else None
        if use_audio and feats is None:
            x = ac.decode_window(f, dur)
            if x is not None:
                feats = ac.features_from_array(x, dur) or {}
                feats.update(ac.speech_features(x))
        text = (hit or {}).get("transcript")
        if use_transcript and text is None:
            text = transcribe(f)
        text = text or ""
        cache.put(f, {"duration": dur, "feats": feats, "transcript": text if use_transcript else None})
        cat, conf, reason = classify(f.stem, feats, text, rules)
        folder = cat if conf >= min_conf else f"_review/{cat}"
        base = _slug(clean_name(f.stem))
        if len(base) < 3 and text:
            base = _slug(" ".join(text.split()[:6]))
        if len(base) < 3:
            counters[cat] += 1
            base = f"{cat}-{counters[cat]:02d}"
        name = _unique_name(base, folder, taken)
        items.append(Item(src=str(f), category=cat, confidence=conf, reason=reason, name=name,
                          dest=str(library / folder / f"{name}.mp3"),
                          original_dest=str(library / "_originals" / f.relative_to(src)),
                          duration=dur, transcript=text))
        if progress:
            progress(n, len(files), items[-1])
    return items


class _Cache:
    """Features and transcripts per file (path + size + mtime), so a re-plan after tuning the
    rules costs seconds, not a re-decode and a re-transcription of every clip."""

    def __init__(self, path: Path):
        self.path = path
        self.rows: dict[str, dict] = {}
        if path.exists():
            for line in path.read_text(encoding="utf-8").splitlines():
                try:
                    r = json.loads(line)
                    self.rows[r["key"]] = r
                except (ValueError, KeyError):
                    continue

    @staticmethod
    def key(f: Path) -> str:
        st = f.stat()
        return f"{f.resolve()}|{st.st_size}|{int(st.st_mtime)}"

    def get(self, f: Path) -> dict | None:
        return self.rows.get(self.key(f))

    def put(self, f: Path, data: dict) -> None:
        k = self.key(f)
        old = self.rows.get(k, {})
        merged = {**old, **{a: b for a, b in data.items() if b is not None}, "key": k}
        if merged == old:
            return
        self.rows[k] = merged
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(merged, ensure_ascii=False) + "\n")


def _slug(text: str) -> str:
    from navig_explore.rename import slugify  # noqa: PLC0415

    return slugify(text, max_len=60)


def _unique_name(base: str, folder: str, taken: set[str]) -> str:
    name, i = base, 2
    while f"{folder}/{name}" in taken:
        name, i = f"{base}-{i}", i + 1
    taken.add(f"{folder}/{name}")
    return name


def write_plan(items: list[Item], path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(asdict(items[0])) if items else ["src"])
        w.writeheader()
        for it in items:
            w.writerow(asdict(it))
    return path


# ── apply / undo ─────────────────────────────────────────────────────────────

def filter_graph(lufs: float, denoise: bool) -> str:
    steps = ["highpass=f=40"]
    if denoise:
        steps.append("afftdn=nr=8:nf=-50:tn=1")  # light: rip hiss, never the content
    trim = "silenceremove=start_periods=1:start_threshold=-55dB:start_silence=0.15"
    steps += [trim, "areverse", trim, "areverse", f"loudnorm=I={lufs}:TP=-1.5:LRA=11"]
    return ",".join(steps)


def convert(src: Path, dst: Path, *, lufs: float = -16.0, bitrate: str = "320k", denoise: bool = True,
            timeout: int = 600) -> None:
    exe = shutil.which("ffmpeg")
    if not exe:
        raise IngestError("ffmpeg is not on PATH")
    dst.parent.mkdir(parents=True, exist_ok=True)
    cmd = [exe, "-nostdin", "-y", "-v", "error", "-i", str(src), "-vn", "-map", "0:a:0",
           "-af", filter_graph(lufs, denoise), "-ar", "44100", "-c:a", "libmp3lame", "-b:a", bitrate, str(dst)]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                              timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        dst.unlink(missing_ok=True)
        raise IngestError(f"ffmpeg timed out on {src.name}") from exc
    if proc.returncode != 0 or not dst.exists() or dst.stat().st_size == 0:
        dst.unlink(missing_ok=True)
        tail = (proc.stderr or "").strip().splitlines()
        raise IngestError(f"{src.name}: {tail[-1] if tail else 'no audio written'}")


def _md5(p: Path) -> str:
    h = hashlib.md5()
    with p.open("rb") as fh:
        for b in iter(lambda: fh.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def apply(items: list[Item], library: Path, *, lufs: float = -16.0, bitrate: str = "320k",
          denoise: bool = True, originals: str = "move", progress=None) -> dict:
    """Convert each file, then move (or copy) its original. Logged before each step."""
    side = library / ".mediaexplorer"
    side.mkdir(parents=True, exist_ok=True)
    log_path = side / LOG
    stats: Counter = Counter()
    rows = []
    with log_path.open("a", encoding="utf-8") as log:
        for n, it in enumerate(items, 1):
            src, dst, orig = Path(it.src), Path(it.dest), Path(it.original_dest)
            if not src.exists():
                stats["missing"] += 1
                continue
            dst = _free(dst)
            orig = _free(orig)
            entry = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "src": str(src), "processed": str(dst),
                     "original": str(orig), "originals": originals, "category": it.category,
                     "confidence": it.confidence, "reason": it.reason, "md5": _md5(src)}
            log.write(json.dumps(entry, ensure_ascii=False) + "\n")
            log.flush()
            try:
                convert(src, dst, lufs=lufs, bitrate=bitrate, denoise=denoise)
                orig.parent.mkdir(parents=True, exist_ok=True)
                (shutil.move if originals == "move" else shutil.copy2)(str(src), str(orig))
                stats["ingested"] += 1
                rows.append({**asdict(it), "dest": str(dst), "original_dest": str(orig)})
            except (IngestError, OSError) as exc:
                stats["failed"] += 1
                dst.unlink(missing_ok=True)
                if progress:
                    progress(n, len(items), it, error=str(exc))
                continue
            if progress:
                progress(n, len(items), it)
    write_index(library, rows)
    return dict(stats)


def _free(p: Path) -> Path:
    if not p.exists():
        return p
    for i in range(2, 10000):
        c = p.with_name(f"{p.stem}-{i}{p.suffix}")
        if not c.exists():
            return c
    raise IngestError(f"no free name for {p}")


def undo(log_path: Path) -> dict:
    """Put every original back where it came from and remove what was made from it."""
    entries = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    stats: Counter = Counter()
    for e in reversed(entries):
        src, orig, made = Path(e["src"]), Path(e["original"]), Path(e["processed"])
        if e.get("originals", "move") == "move":
            if orig.exists():
                src.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(orig), str(src if not src.exists() else _free(src)))
                stats["restored"] += 1
            elif not src.exists():
                stats["missing"] += 1
        else:
            orig.unlink(missing_ok=True)
        if made.exists():
            made.unlink()  # derived from the original, which is back — not user data
            stats["removed"] += 1
    return dict(stats)


# ── index ────────────────────────────────────────────────────────────────────

INDEX_FIELDS = ["category", "name", "duration", "dest", "original_dest", "src", "confidence", "reason", "transcript"]


def write_index(library: Path, rows: list[dict]) -> None:
    """Append to INDEX.csv and rebuild README.md from the whole index (never clobbers rows)."""
    idx = library / "INDEX.csv"
    new = not idx.exists()
    with idx.open("a", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=INDEX_FIELDS, extrasaction="ignore")
        if new:
            w.writeheader()
        for r in rows:
            w.writerow({**r, "dest": str(Path(r["dest"]).relative_to(library)),
                        "original_dest": str(Path(r["original_dest"]).relative_to(library))})
    all_rows = list(csv.DictReader(idx.open(encoding="utf-8")))
    lines = ["# Sound library", "", f"{len(all_rows)} sounds, ingested by `navig explore audio-ingest`. "
             "Originals in `_originals/`; undo with `navig explore audio-ingest-undo .mediaexplorer/audio-ingest-log.jsonl`.", ""]
    for cat in sorted({r["category"] for r in all_rows}):
        rs = [r for r in all_rows if r["category"] == cat]
        lines += [f"## {cat} ({len(rs)})", "", "| File | Length | Original | Why |", "|---|---|---|---|"]
        for r in sorted(rs, key=lambda r: r["name"]):
            d = float(r["duration"] or 0)
            lines.append(f"| `{Path(r['dest']).name}` | {int(d // 60)}:{d % 60:04.1f} | "
                         f"{Path(r['src']).name[:60]} | {r['reason']} |")
        lines.append("")
    (library / "README.md").write_text("\n".join(lines), encoding="utf-8")
