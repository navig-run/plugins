"""Voice profiles → what the engine is actually asked to do.

A voice profile is a ``speech`` preset (``navig.presets``, tool ``speech``): provider, model,
voice, language, speed, pitch, ElevenLabs stability / similarity, a delivery note, and
pronunciation rules. The same profile drives the Dock, ``navig voice speak --profile`` and
NAVIG OS, so this module is the ONE translation from profile params to the engine.

Capability-aware: a field the chosen provider does not implement is not silently "applied"
— it is listed in :attr:`ProfilePlan.ignored`, so a UI or the CLI can say so instead of
pretending a slider did something.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from navig_audio.voice.tts import TTSProvider

#: Which profile fields each provider really implements (and the TTSConfig field they set).
_MODEL_FIELD = {
    TTSProvider.ELEVENLABS: "elevenlabs_model",
    TTSProvider.OPENAI: "openai_model",
    TTSProvider.MIMO: "mimo_model",
    TTSProvider.OPENROUTER: "openrouter_model",
    TTSProvider.DEEPGRAM: "deepgram_model",
}
_SPEED_PROVIDERS = {TTSProvider.EDGE, TTSProvider.OPENAI, TTSProvider.OPENROUTER, TTSProvider.GOOGLE_CLOUD}
_PITCH_PROVIDERS = {TTSProvider.EDGE}
_STABILITY_PROVIDERS = {TTSProvider.ELEVENLABS}
_STYLE_PROVIDERS = {TTSProvider.MIMO}


@dataclass
class ProfilePlan:
    """The engine-facing form of a profile."""

    provider: TTSProvider | None  # None = let the routing policy choose
    voice: str | None
    lang: str | None  # fr | ru | en | None (detect)
    speed: float  # 1.0 when the profile says nothing
    config: dict[str, Any] = field(default_factory=dict)  # TTSConfig overrides
    kwargs: dict[str, Any] = field(default_factory=dict)  # extra synthesize() kwargs
    pronounce: list[tuple[str, str]] = field(default_factory=list)
    ignored: list[str] = field(default_factory=list)


def parse_pronounce(items: list[str] | None) -> list[tuple[str, str]]:
    """``["SQL=sequel", "NAVIG = навиг"]`` → pairs. Malformed entries are skipped."""
    pairs: list[tuple[str, str]] = []
    for item in items or []:
        term, sep, say = str(item).partition("=")
        term, say = term.strip(), say.strip()
        if sep and term and say:
            pairs.append((term, say))
    return pairs


def apply_pronounce(text: str, pairs: list[tuple[str, str]]) -> str:
    """Replace whole-word, case-insensitive occurrences of each term with how to say it.

    Longest terms first, so "NAVIG OS" wins over "NAVIG". Word boundaries are Unicode-aware
    (``\\w`` covers Cyrillic), and a term that is not word-shaped (``C++``) matches literally.
    """
    for term, say in sorted(pairs, key=lambda p: -len(p[0])):
        pat = re.escape(term)
        left = r"(?<!\w)" if term[:1].isalnum() or term[:1] == "_" else ""
        right = r"(?!\w)" if term[-1:].isalnum() or term[-1:] == "_" else ""
        text = re.sub(f"{left}{pat}{right}", lambda _m, s=say: s, text, flags=re.IGNORECASE)
    return text


def plan_from_params(params: dict[str, Any] | None) -> ProfilePlan:
    """Translate a ``speech`` preset's validated params into a :class:`ProfilePlan`."""
    p = dict(params or {})
    prov_s = str(p.get("provider") or "auto").lower()
    provider = None if prov_s == "auto" else TTSProvider(prov_s)
    lang = p.get("lang")
    lang = None if lang in (None, "", "auto") else str(lang)
    speed = float(p.get("speed") or 1.0)
    plan = ProfilePlan(provider=provider, voice=(p.get("voice") or None), lang=lang, speed=speed)
    plan.pronounce = parse_pronounce(p.get("pronounce"))

    def supported(name: str, providers: set[TTSProvider]) -> bool:
        # Auto: the routing decides later; keep the field and let the engine ignore it.
        if provider is None or provider in providers:
            return True
        plan.ignored.append(name)
        return False

    if p.get("model"):
        if provider in _MODEL_FIELD:
            plan.config[_MODEL_FIELD[provider]] = str(p["model"])
        else:
            plan.ignored.append("model")
    if "speed" in p and speed != 1.0 and supported("speed", _SPEED_PROVIDERS):
        plan.config["speed"] = speed
        if provider == TTSProvider.GOOGLE_CLOUD:
            plan.config["google_cloud_speaking_rate"] = speed
    if p.get("pitch_hz") not in (None, 0) and supported("pitch_hz", _PITCH_PROVIDERS):
        plan.config["pitch"] = f"{int(p['pitch_hz']):+d}Hz"
    if p.get("stability") is not None and supported("stability", _STABILITY_PROVIDERS):
        plan.config["elevenlabs_stability"] = float(p["stability"])
    if p.get("similarity") is not None and supported("similarity", _STABILITY_PROVIDERS):
        plan.config["elevenlabs_similarity"] = float(p["similarity"])
    if p.get("style") and supported("style", _STYLE_PROVIDERS):
        plan.kwargs["style"] = str(p["style"])
    return plan


def load_profile(profile_id: str) -> tuple[str, ProfilePlan]:
    """Load a ``speech`` preset by id from NAVIG's preset catalog.

    Raises ``LookupError`` (no such profile / not a speech profile) or ``ImportError`` when
    navig itself is not installed (navig-audio on its own has no preset catalog).
    """
    from navig.presets import catalog  # lazy: navig-audio runs without navig

    preset = catalog.get_preset(profile_id)
    if preset is None:
        raise LookupError(f"no voice profile {profile_id!r} (see: navig presets list --tool speech)")
    if preset.tool != "speech":
        raise LookupError(f"{profile_id!r} is a {preset.tool} preset, not a voice profile")
    return preset.name, plan_from_params(preset.params)
