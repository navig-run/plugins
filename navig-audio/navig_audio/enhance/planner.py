"""Turn an analysis + a mode into an explainable processing plan.

The planner picks stages, never samples: each stage carries the reason it is there
(the finding it answers, or the mode that asked for it), the engine that runs it,
and whether it leaves the machine. An empty plan is a valid answer — "this file is
already clean" — and the default mode (Preserve Original) only ever adds a stage a
measurement asked for.

Modes:

* ``preserve`` — strict restoration. Fix measured defects, protect the true peak,
  nothing else. No EQ, no compression, no reverb, no widening, no generative engine.
* ``studio``   — preserve + gentle tonal balance toward the category's reference, light
  compression where the category suits it, and loudness to the category's target.
* ``cinematic``— studio + creative impact (low-end weight, saturation, optional space or
  width). Labelled creative: its result is never presented as a faithful restoration.
* ``custom``   — every stage is decided by the typed params.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from typing import Any

from navig_audio.enhance.analysis import AudioReport

MODES = ("preserve", "studio", "cinematic", "custom")

# Loudness targets by category (LUFS integrated) and the true-peak ceiling (dBTP).
TARGET_LUFS = {"speech": -16.0, "music": -14.0, "sfx": -18.0, "ambience": -23.0, "mixed": -16.0}
DEFAULT_TP = -1.0
# Short clips: gated LUFS is unreliable, so they are levelled by peak instead.
SFX_PEAK_TARGET = -1.0

# Reference tonal balance (band energy shares) per category, from typical material.
REFERENCE_SHARES = {
    "speech": {"sub": 0.01, "low": 0.12, "low_mid": 0.55, "high_mid": 0.25, "air": 0.07},
    "music": {"sub": 0.06, "low": 0.30, "low_mid": 0.40, "high_mid": 0.17, "air": 0.07},
    "ambience": {"sub": 0.04, "low": 0.25, "low_mid": 0.45, "high_mid": 0.18, "air": 0.08},
}
BAND_CENTRES = {"sub": 40, "low": 120, "low_mid": 800, "high_mid": 3500, "air": 10000}
MAX_EQ_DB = 3.0

# Stage risk to content, lowest first — a failed verification drops the riskiest first.
RISK = ["dc", "limit", "gain", "loudness", "trim", "band", "dehum", "declip", "declick", "denoise",
        "isolate", "eq", "compress", "impact", "space", "width"]


@dataclass
class Stage:
    name: str
    filter: str  # ffmpeg -af fragment ('' for non-ffmpeg engines)
    reason: str  # plain language
    engine: str = "ffmpeg"  # ffmpeg · elevenlabs
    local: bool = True
    creative: bool = False
    settings: dict[str, Any] = field(default_factory=dict)


@dataclass
class Plan:
    mode: str
    category: str
    stages: list[Stage]
    target_lufs: float | None
    true_peak_db: float
    level_by: str  # lufs · peak · none
    creative: bool
    notes: list[str] = field(default_factory=list)
    skipped: list[dict[str, str]] = field(default_factory=list)

    @property
    def empty(self) -> bool:
        return not self.stages

    @property
    def cloud(self) -> bool:
        return any(not s.local for s in self.stages)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["empty"] = self.empty
        d["cloud"] = self.cloud
        return d

    def without(self, name: str) -> Plan:
        stages = [s for s in self.stages if s.name != name]
        return Plan(self.mode, self.category, stages, self.target_lufs, self.true_peak_db, self.level_by,
                    self.creative and any(s.creative for s in stages), list(self.notes),
                    [*self.skipped, {"stage": name, "why": "dropped after a failed verification"}])

    def riskiest(self) -> str | None:
        names = [s.name for s in self.stages]
        for name in reversed(RISK):
            if name in names:
                return name
        return names[-1] if names else None


DEFAULTS: dict[str, dict[str, Any]] = {
    "preserve": {"denoise": "auto", "declip": True, "declick": True, "dehum": "auto", "trim_silence": False,
                 "eq": "off", "compress": "off", "space": "off", "stereo_width": 1.0},
    "studio": {"denoise": "auto", "declip": True, "declick": True, "dehum": "auto", "trim_silence": True,
               "eq": "adaptive", "compress": "gentle", "space": "off", "stereo_width": 1.0},
    "cinematic": {"denoise": "auto", "declip": True, "declick": True, "dehum": "auto", "trim_silence": True,
                  "eq": "adaptive", "compress": "moderate", "space": "off", "stereo_width": 1.0},
    "custom": {"denoise": "off", "declip": False, "declick": False, "dehum": "off", "trim_silence": False,
               "eq": "off", "compress": "off", "space": "off", "stereo_width": 1.0},
}


def _on(finding_code: str, report: AudioReport, *, allow_low: bool = False) -> bool:
    f = report.finding(finding_code)
    if f is None or f.severity == "info":
        return False
    return allow_low or f.confidence != "low"


def plan(report: AudioReport, mode: str = "preserve", params: dict[str, Any] | None = None,
         *, engines: dict[str, bool] | None = None) -> Plan:
    """Build the plan. ``params`` are typed ``audio.enhance`` preset settings."""
    if mode not in MODES:
        raise ValueError(f"mode must be one of {', '.join(MODES)}")
    p = {**DEFAULTS[mode], **(params or {})}
    engines = engines or {}
    cat = report.category if p.get("category", "auto") == "auto" else p["category"]
    tp = float(p.get("true_peak_db", DEFAULT_TP))
    st: list[Stage] = []
    skipped: list[dict[str, str]] = []
    notes: list[str] = []

    # ── restoration (all modes) ──────────────────────────────────────
    if _on("dc_offset", report):
        st.append(Stage("dc", "highpass=f=10:poles=1", "Re-centre the waveform (DC offset)"))
    hp, lp = float(p.get("highpass_hz") or 0), float(p.get("lowpass_hz") or 0)
    if hp >= 20 or (lp and lp < report.sample_rate / 2 - 500):
        parts, why = [], []
        if hp >= 20:
            parts.append(f"highpass=f={hp:.0f}:poles=2")
            why.append(f"rumble below {hp:.0f} Hz")
        if lp and lp < report.sample_rate / 2 - 500:
            parts.append(f"lowpass=f={lp:.0f}:poles=2")
            why.append(f"hiss above {lp / 1000:.1f} kHz")
        st.append(Stage("band", ",".join(parts), "Remove " + " and ".join(why) + " (set by the preset)",
                        settings={"highpass_hz": hp or None, "lowpass_hz": lp or None}))
    if p["dehum"] != "off" and (p["dehum"] in ("50", "60") or _on("hum", report)):
        f0 = int(p["dehum"]) if p["dehum"] in ("50", "60") else int(report.hum_hz or 50)
        notches = ",".join(f"bandreject=f={f0 * h}:width_type=q:w=35" for h in (1, 2, 3))
        st.append(Stage("dehum", notches, f"Remove {f0} Hz mains hum", settings={"hz": f0}))
    if p["declip"] and _on("clipping", report):
        st.append(Stage("declip", "adeclip=window=55:overlap=75:arorder=8:threshold=10",
                        "Rebuild clipped peaks"))
    if p["declick"] and _on("clicks", report):
        if cat == "sfx":
            skipped.append({"stage": "declick", "why": "sharp transients are part of a sound effect"})
        else:
            st.append(Stage("declick", "adeclick=window=55:overlap=75:arorder=2:threshold=2", "Remove clicks"))
    if p["denoise"] != "off" and (p["denoise"] in ("light", "strong") or _on("noise", report)):
        floor = report.noise_floor_db if report.noise_floor_db is not None else -60.0
        nf = max(-80.0, min(-20.0, floor))
        if p["denoise"] == "auto":
            # Reduce by what the measured gap-to-signal distance is short of, plus a margin.
            snr = report.snr_db if report.snr_db is not None else 40.0
            from navig_audio.enhance.analysis import NOISE_SNR_MINOR

            nr = int(max(6, min(20, round(NOISE_SNR_MINOR - snr + 4))))
            if cat in ("sfx", "music"):
                nr = min(nr, 12)  # tonal content: be gentler, decays live near the floor
        else:
            nr = {"light": 6, "strong": 18}[p["denoise"]]
        engine = str(p.get("engine") or "auto")
        use_dfn = cat == "speech" and engines.get("deepfilter") and engine in ("auto", "deepfilter")
        if engine == "deepfilter" and not use_dfn:
            why = "it is for speech" if cat != "speech" else "deep-filter.exe was not found"
            skipped.append({"stage": "deepfilter", "why": f"DeepFilterNet not used: {why}; ffmpeg denoise instead"})
        if use_dfn:
            # DeepFilterNet (local, CPU): a speech noise-suppression network that FILTERS the
            # recording — it does not generate audio. Its attenuation is capped so a
            # preserve run cannot strip the voice; verification still checks every event.
            limit = {"preserve": 18, "custom": 24}.get(mode, 30) if p["denoise"] != "strong" else 40
            st.append(Stage("denoise", "", "Reduce background noise in speech (DeepFilterNet, local)",
                            engine="deepfilter", settings={"atten_lim_db": limit}))
        else:
            # nf = OUR measured floor, tracking off: measured on a white-noise bed, tn=1 cut
            # the floor by ~4 dB where the fixed, measured floor cut it by ~9 dB.
            st.append(Stage("denoise", f"afftdn=nr={nr}:nf={nf:.0f}",
                            "Reduce steady background noise", settings={"nr": nr, "nf": round(nf)}))
    if p.get("engine") == "elevenlabs":
        if cat != "speech":
            skipped.append({"stage": "isolate", "why": f"ElevenLabs isolation is for voice; this is {cat}"})
        elif not p.get("allow_cloud"):
            skipped.append({"stage": "isolate", "why": "cloud processing is off — allow it to upload this file"})
        elif not engines.get("elevenlabs"):
            skipped.append({"stage": "isolate", "why": "no ElevenLabs key is configured"})
        else:
            st.insert(0, Stage("isolate", "", "Isolate the voice from the background (uploads to ElevenLabs)",
                               engine="elevenlabs", local=False))
    if p["trim_silence"] and report.finding("edge_silence"):
        st.append(Stage("trim", "", "Trim long silence at the edges",
                        settings={"lead_s": report.lead_silence_s, "trail_s": report.trail_silence_s}))

    # ── tone + dynamics (studio / cinematic / custom) ────────────────
    if p["eq"] != "off" and cat in REFERENCE_SHARES:
        eq = _adaptive_eq(report, cat, gentle=p["eq"] == "gentle")
        if eq:
            st.append(Stage("eq", ",".join(eq["filters"]), "Even out the tonal balance", settings=eq["moves"]))
    elif p["eq"] != "off":
        skipped.append({"stage": "eq", "why": f"no tonal reference for {cat}; left as recorded"})
    if p["compress"] != "off":
        if cat == "sfx" and mode != "custom":
            skipped.append({"stage": "compress", "why": "compression would flatten a sound effect's impact"})
        else:
            ratio, thr = (2.0, -24.0) if p["compress"] == "gentle" else (3.0, -20.0)
            st.append(Stage("compress", f"acompressor=threshold={thr}dB:ratio={ratio}:attack=15:release=180:makeup=1",
                            "Even out the dynamics", settings={"ratio": ratio, "threshold_db": thr}))

    if mode == "cinematic":
        impact = "bass=g=2:f=90:w=0.7,treble=g=1.5:f=9000"
        if cat != "sfx":
            # Saturation rounds transients off — measured on a cash-register SFX it hid 3 of
            # its 12 events — so sound effects get the tonal weight without it.
            impact += ",asoftclip=type=tanh:threshold=0.95"
        st.append(Stage("impact", impact, "Add weight and bite (creative)", creative=True))
        notes.append("Cinematic is a creative treatment, not a faithful restoration.")
    if p["space"] != "off" and mode in ("cinematic", "custom"):
        delays, decays = ("40|70", "0.25|0.15") if p["space"] == "room" else ("90|160", "0.3|0.2")
        st.append(Stage("space", f"aecho=0.8:0.85:{delays}:{decays}", f"Add {p['space']} ambience (creative)",
                        creative=True))
    width = float(p.get("stereo_width", 1.0))
    if abs(width - 1.0) > 0.01 and mode in ("cinematic", "custom"):
        if report.channels >= 2:
            st.append(Stage("width", f"extrastereo=m={width:.2f}", "Change the stereo width (creative)",
                            creative=True))
        else:
            skipped.append({"stage": "width", "why": "the source is mono; no stereo is invented"})

    # ── level ────────────────────────────────────────────────────────
    level_by, target = "none", None
    if mode == "preserve":
        if _on("very_quiet", report):
            level_by, target = ("peak" if report.short_clip else "lufs"), p.get("target_lufs", TARGET_LUFS.get(cat))
            st.append(Stage("gain", "", "Bring a very quiet recording up to a usable level"))
        elif _on("too_hot", report) or _on("clipping", report) or (st and math.isfinite(report.true_peak_db)
                                                                   and report.true_peak_db > tp):
            level_by = "peak"
            st.append(Stage("limit", "", f"Keep peaks under {tp:.1f} dBTP"))
        notes.append("Preserve Original keeps the recorded level unless a measurement says otherwise.")
    else:
        level_by = "peak" if report.short_clip or cat == "sfx" else "lufs"
        target = p.get("target_lufs", TARGET_LUFS.get(cat, -16.0))
        st.append(Stage("loudness", "", f"Level to {target:.0f} LUFS with a {tp:.1f} dBTP ceiling"
                        if level_by == "lufs" else f"Level peaks to {SFX_PEAK_TARGET:.1f} dBTP"))
    if report.lossy:
        notes.append("The source is lossy; the result cannot recover what its encoder discarded.")
    if not st:
        notes.append("Nothing measured needs fixing; the original is the best version.")
    return Plan(mode, cat, st, target, tp, level_by, any(s.creative for s in st), notes, skipped)


def _adaptive_eq(report: AudioReport, cat: str, *, gentle: bool) -> dict[str, Any] | None:
    ref = REFERENCE_SHARES[cat]
    moves: dict[str, float] = {}
    filters: list[str] = []
    limit = 1.5 if gentle else MAX_EQ_DB
    for band, want in ref.items():
        have = report.band_shares.get(band, 0.0)
        if have <= 0 or want <= 0:
            continue
        delta = 10 * math.log10(want / have) * 0.5  # move halfway: correct, don't remake
        delta = max(-limit, min(limit, delta))
        if abs(delta) < 0.75:
            continue
        if band == "sub" and delta > 0:
            continue  # never invent sub-bass
        if band == "air" and delta > 0 and report.bandwidth_hz < 8000:
            continue  # boosting highs that aren't there only lifts noise
        moves[band] = round(delta, 1)
        filters.append(f"equalizer=f={BAND_CENTRES[band]}:width_type=o:w=1.5:g={delta:.1f}")
    return {"filters": filters, "moves": moves} if filters else None
