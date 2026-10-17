"""One capability view of the speech engines — what a client may offer, honestly.

The TTS engine (:mod:`navig_audio.voice.tts`) can call seven providers. A client — the
Dock reader, the OS settings page, the CLI — needs to know, *before* it calls anything:

* is the provider usable here (library installed, key present)?
* does it cost money per character, and does the text leave this machine?
* which voices suit French, Russian and English?
* which controls does it really implement (so a UI never shows a slider that does nothing)?

``voice_catalog()`` answers that from the engine's own enum and key resolution — it never
makes a network call, so it is safe to hit on every popup open.

``route()`` is the routing rule, under the operator's policy (``voice.policy``):

* ``auto``    — a free engine; a named provider (or a profile's) is the explicit opt-in;
* ``free``    — never a paid provider, named or not;
* ``premium`` — the named provider, else the first keyed paid one, else a free one (said);
* ``offline`` — only an engine that keeps the text on this machine (none ship yet — the
  answer is a clear refusal, never a silent send).

A paid provider is never a silent fallback. Prices are per 1,000 characters, checked on the
providers' pricing pages on the date recorded beside them.
"""

from __future__ import annotations

import importlib.util
from dataclasses import asdict, dataclass, field
from typing import Any

from navig_audio.voice.tts import TTS, TTSProvider

#: Languages NAVIG treats as first-class for reading aloud.
CORE_LANGUAGES = ("fr", "ru", "en")

FREE = "free"
FREE_PROMO = "free-promo"  # costs nothing today, may start billing — never in Auto
PAID = "paid"


@dataclass(frozen=True)
class VoiceOption:
    id: str
    name: str
    lang: str  # "fr" | "ru" | "en" | "auto" (multilingual voice)
    gender: str = ""


@dataclass(frozen=True)
class _Spec:
    label: str
    cost_class: str
    key_provider: str | None  # vault provider name; None = no key needed
    key_env: tuple[str, ...]
    module: str | None  # import that must succeed for the provider to work
    languages: tuple[str, ...]
    voices: tuple[VoiceOption, ...]
    supports: dict[str, bool] = field(default_factory=dict)
    # USD per 1,000 characters (None = not published per character / unknown) + where from.
    usd_per_1k: float | None = None
    price_source: str = ""


#: Day the prices below were last checked against the providers' own pages.
PRICES_CHECKED = "2026-10-16"

_EDGE_VOICES = (
    VoiceOption("fr-FR-DeniseNeural", "Denise", "fr", "female"),
    VoiceOption("fr-FR-HenriNeural", "Henri", "fr", "male"),
    VoiceOption("ru-RU-SvetlanaNeural", "Svetlana", "ru", "female"),
    VoiceOption("ru-RU-DmitryNeural", "Dmitry", "ru", "male"),
    VoiceOption("en-US-JennyNeural", "Jenny", "en", "female"),
    VoiceOption("en-US-GuyNeural", "Guy", "en", "male"),
    VoiceOption("en-GB-SoniaNeural", "Sonia (UK)", "en", "female"),
)

_OPENAI_VOICES = tuple(
    VoiceOption(v, v.capitalize(), "auto") for v in ("nova", "alloy", "echo", "fable", "onyx", "shimmer")
)

# Every id below is a premade ElevenLabs voice; all speak every language of the
# multilingual models, so they are tagged "auto". The first is the engine default.
_ELEVEN_VOICES = (
    VoiceOption("pMsXgVXv3BLzUgSXRplE", "Default narrator", "auto"),
)

_MIMO_VOICES = (
    VoiceOption("mimo_default", "MiMo default", "auto"),
    VoiceOption("Mia", "Mia", "en", "female"),
    VoiceOption("Milo", "Milo", "en", "male"),
)

_SPECS: dict[TTSProvider, _Spec] = {
    TTSProvider.EDGE: _Spec(
        label="Edge neural",
        cost_class=FREE,
        key_provider=None,
        key_env=(),
        module="edge_tts",
        languages=CORE_LANGUAGES,
        voices=_EDGE_VOICES,
        supports={"speed": True, "pitch": True, "timestamps": False, "streaming": False},
        usd_per_1k=0.0,
        price_source="no charge (Microsoft's public read-aloud service; unofficial, may change)",
    ),
    TTSProvider.MIMO: _Spec(
        label="Xiaomi MiMo",
        cost_class=FREE_PROMO,
        key_provider="mimo",
        key_env=("MIMO_API_KEY",),
        module=None,
        # Built-in voices are English/Chinese; fr/ru quality is unverified.
        languages=("en",),
        voices=_MIMO_VOICES,
        supports={"speed": False, "pitch": False, "model": True, "style": True, "timestamps": False, "streaming": False},
        usd_per_1k=0.0,
        price_source="free during Xiaomi's promotion (may start billing)",
    ),
    TTSProvider.ELEVENLABS: _Spec(
        label="ElevenLabs",
        cost_class=PAID,
        key_provider="elevenlabs",
        key_env=("ELEVENLABS_API_KEY", "XI_API_KEY"),
        module=None,
        languages=CORE_LANGUAGES,
        voices=_ELEVEN_VOICES,
        supports={"speed": False, "pitch": False, "model": True, "stability": True, "similarity": True, "timestamps": True, "streaming": False},
        usd_per_1k=0.08,
        price_source="elevenlabs.io/pricing/api — v2 Multilingual / v3 $0.08, Flash/Turbo $0.04",
    ),
    TTSProvider.OPENAI: _Spec(
        label="OpenAI",
        cost_class=PAID,
        key_provider="openai",
        key_env=("OPENAI_API_KEY",),
        module=None,
        languages=CORE_LANGUAGES,
        voices=_OPENAI_VOICES,
        supports={"speed": True, "pitch": False, "model": True, "timestamps": False, "streaming": False},
        usd_per_1k=0.015,
        price_source="developers.openai.com/api/docs/pricing — tts-1 $15/1M chars, tts-1-hd $30/1M",
    ),
    TTSProvider.GOOGLE_CLOUD: _Spec(
        label="Google Cloud",
        cost_class=PAID,
        key_provider="google",
        key_env=("GOOGLE_CLOUD_API_KEY", "GOOGLE_TTS_API_KEY"),
        module=None,
        languages=CORE_LANGUAGES,
        voices=(),
        supports={"speed": True, "pitch": False, "timestamps": False, "streaming": False},
        usd_per_1k=0.016,
        price_source="≈ Neural2 $16/1M chars (third-party listing; Google's page not verified)",
    ),
    TTSProvider.DEEPGRAM: _Spec(
        label="Deepgram Aura",
        cost_class=PAID,
        key_provider="deepgram",
        key_env=("DEEPGRAM_API_KEY",),
        module=None,
        languages=("en",),
        voices=(),
        supports={"speed": False, "pitch": False, "model": True, "timestamps": False, "streaming": False},
        usd_per_1k=0.030,
        price_source="Deepgram Aura-2 launch pricing — $0.030 per 1k chars",
    ),
    TTSProvider.OPENROUTER: _Spec(
        label="Fish Audio (OpenRouter)",
        cost_class=PAID,
        key_provider="openrouter",
        key_env=("OPENROUTER_API_KEY",),
        module=None,
        languages=CORE_LANGUAGES,
        voices=(),
        supports={"speed": True, "pitch": False, "model": True, "timestamps": False, "streaming": False},
        usd_per_1k=None,
        price_source="billed by OpenRouter per model — not published per character",
    ),
}

#: Auto routing order — free engines only. MiMo (free-promo) is deliberately absent:
#: its voices do not cover French or Russian, and a promo can end.
AUTO_ORDER: tuple[TTSProvider, ...] = (TTSProvider.EDGE,)
#: Premium with nothing named: the first keyed one of these (best fr/ru/en quality first).
PREMIUM_ORDER: tuple[TTSProvider, ...] = (
    TTSProvider.ELEVENLABS, TTSProvider.OPENAI, TTSProvider.GOOGLE_CLOUD, TTSProvider.OPENROUTER,
)
POLICIES = ("auto", "free", "premium", "offline")


def _installed(module: str | None) -> bool:
    if not module:
        return True
    try:
        return importlib.util.find_spec(module) is not None
    except (ImportError, ValueError):
        return False


def _keyed(spec: _Spec) -> bool:
    if spec.key_provider is None:
        return True
    return bool(TTS._resolve_api_key(spec.key_provider, *spec.key_env))


def provider_status(provider: TTSProvider) -> dict[str, Any]:
    """One provider's capability row. No network call."""
    spec = _SPECS[provider]
    installed = _installed(spec.module)
    keyed = _keyed(spec)
    if not installed:
        reason = f"{spec.module} is not installed"
    elif not keyed:
        reason = "no API key in the vault"
    else:
        reason = ""
    return {
        "id": provider.value,
        "label": spec.label,
        "available": installed and keyed,
        "unavailable_reason": reason,
        "cost_class": spec.cost_class,
        # Every engine here is a network service; the field exists so a local engine
        # (offline packs) can say False and the OFFLINE policy can filter on it.
        "sends_text_external": True,
        "needs_key": spec.key_provider is not None,
        "languages": list(spec.languages),
        "voices": [asdict(v) for v in spec.voices],
        "supports": dict(spec.supports),
        "price": {"usd_per_1k_chars": spec.usd_per_1k, "source": spec.price_source, "checked": PRICES_CHECKED},
    }


def voice_catalog() -> dict[str, Any]:
    """Every known provider, ordered: Auto's engines first, then the rest."""
    order = list(AUTO_ORDER) + [p for p in _SPECS if p not in AUTO_ORDER]
    return {
        "languages": list(CORE_LANGUAGES),
        "auto_order": [p.value for p in AUTO_ORDER],
        "policies": list(POLICIES),
        "providers": [provider_status(p) for p in order],
    }


def is_paid(provider: TTSProvider) -> bool:
    return _SPECS[provider].cost_class == PAID


def estimate_usd(provider: TTSProvider, chars: int) -> float | None:
    """What ``chars`` characters cost on ``provider`` (None = not published per character)."""
    rate = _SPECS[provider].usd_per_1k
    return None if rate is None else round(rate * chars / 1000, 6)


@dataclass(frozen=True)
class Route:
    provider: TTSProvider
    paid: bool
    note: str = ""  # said to the user when the policy changed what they asked for


def route(requested: TTSProvider | None, policy: str = "auto") -> Route:
    """Choose the provider for one request under ``policy`` (see the module docstring).

    Raises :class:`RoutingError` with a sentence a person can act on.
    """
    policy = policy if policy in POLICIES else "auto"
    if policy == "offline":
        local = [p for p in _SPECS if not provider_status(p)["sends_text_external"]]
        if requested is not None and requested not in local:
            raise RoutingError(
                f"Offline mode keeps text on this machine — {_SPECS[requested].label} is an online service"
            )
        for p in ([requested] if requested else local):
            if provider_status(p)["available"]:
                return Route(p, False)
        raise RoutingError(
            "Offline mode: no on-device voice is installed — install one, or switch the "
            "voice policy to auto/free (navig voice policy auto)"
        )
    if requested is not None:
        if policy == "free" and is_paid(requested):
            raise RoutingError(
                f"Free mode never uses a paid service — {_SPECS[requested].label} bills per "
                "character (switch the voice policy to auto or premium to allow it)"
            )
        return Route(pick_provider(requested), is_paid(requested))
    if policy == "premium":
        for p in PREMIUM_ORDER:
            if provider_status(p)["available"]:
                return Route(p, True)
        free = pick_provider(None)
        return Route(free, False, note=f"Premium mode: no paid voice is keyed — using {_SPECS[free].label}")
    return Route(pick_provider(None), False)


def paid_fallback() -> TTSProvider | None:
    """The paid provider an opted-in Auto falls back to when the free engine fails."""
    for p in PREMIUM_ORDER:
        if provider_status(p)["available"]:
            return p
    return None


class RoutingError(ValueError):
    """The request cannot be routed to any provider the policy allows."""


def parse_provider(value: str | None) -> TTSProvider | None:
    """``None``/``""``/``"auto"`` → None (Auto); a known id → its enum; else RoutingError."""
    if value is None:
        return None
    v = str(value).strip().lower()
    if v in ("", "auto"):
        return None
    try:
        return TTSProvider(v)
    except ValueError as exc:
        known = ", ".join(p.value for p in _SPECS)
        raise RoutingError(f"unknown provider {value!r} (known: auto, {known})") from exc


def pick_provider(requested: TTSProvider | None) -> TTSProvider:
    """Resolve the provider to call.

    * Explicit provider → that provider, if it is available (a paid one included: naming it
      IS the opt-in). Unavailable → RoutingError naming why.
    * Auto → the first available engine of :data:`AUTO_ORDER` (free only).
    """
    if requested is not None:
        row = provider_status(requested)
        if not row["available"]:
            raise RoutingError(f"{row['label']} is unavailable: {row['unavailable_reason']}")
        return requested
    for prov in AUTO_ORDER:
        if provider_status(prov)["available"]:
            return prov
    raise RoutingError("no free speech engine is available (install edge-tts)")


def default_voice(provider: TTSProvider, lang: str | None) -> str | None:
    """The catalog's first voice for ``lang`` on ``provider`` (multilingual voices match any)."""
    voices = _SPECS[provider].voices
    if lang:
        for v in voices:
            if v.lang == lang:
                return v.id
    for v in voices:
        if v.lang == "auto":
            return v.id
    return None


def known_voice(provider: TTSProvider, voice: str) -> bool:
    """True when ``voice`` is plausible for ``provider``.

    Edge voice names have a fixed shape (``xx-YY-NameNeural``) and any locale is valid, so
    shape is checked rather than the curated list; other providers take any id (custom
    and cloned voices are legitimate).
    """
    if not voice or len(voice) > 128:
        return False
    if provider == TTSProvider.EDGE:
        parts = voice.split("-")
        return len(parts) >= 3 and voice.endswith("Neural")
    return True
