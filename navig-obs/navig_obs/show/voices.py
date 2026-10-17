"""Who speaks, in which language: the stage's language picks the pigeon's voice.

The scene collection names its language — ``SCHEMA BROADCAST (ru)``,
``SCHEMA BROADCAST MOBILE (fr)`` — so a line said while the Russian stage is
loaded is spoken in Russian, by the Russian voice, with Russian pronunciation
(navig-audio's lexicon, via ``navig voice speak --lang``).

One :class:`Voice` per language, from navig config ``obs.show.say_voices``
(``{"ru": {"provider": "elevenlabs", "voice": "<id>", "style": "…",
"fx": "pitch=+2,tempo=1.08", "fallback": [{"provider": "mimo", …}]}}``) over
the shipped :data:`DEFAULTS`.

**A line is never silent because one engine failed.** :func:`chain` gives the
voice, then its ``fallback`` voices, then the language's free Edge voice
(:data:`SAFETY`, no key, no quota) — a spent quota, a dead key or a network
blip falls through to the next instead of a card without a voice.

``fx`` is the pigeon's character, applied to the clip after synthesis:

* ``pitch=<semitones>`` — up (+) or down (−), the speed kept;
* ``tempo=<factor>`` — faster (>1) or slower (<1), the pitch kept.
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import Any

LANGS = ("en", "fr", "ru")
DEFAULT_LANG = "en"

_COLLECTION_LANG = re.compile(r"\((en|fr|ru)\)\s*$", re.IGNORECASE)

# The pigeon's delivery note (providers that take one: MiMo). Written in the
# language it is spoken in — a note in English nudges a French line English.
PIGEON_STYLE = {
    "en": "You are the Grid Pigeon, a small, sly, warm city pigeon reading the "
    "news on a late-night broadcast. Natural, a little wry, never shouting.",
    "fr": "Tu es le Pigeon de la Grille, un petit pigeon des villes, malin et "
    "chaleureux, qui lit les nouvelles d'une émission de nuit. Naturel, un peu "
    "ironique, jamais crié.",
    "ru": "Ты — Голубь Сетки, маленький хитрый и тёплый городской голубь, "
    "читающий новости в ночном эфире. Естественно, чуть иронично, без крика.",
}


@dataclass(frozen=True)
class Voice:
    provider: str
    voice: str | None = None
    style: str | None = None
    fx: str | None = None
    fallback: tuple[Voice, ...] = ()

    def to_json(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "provider": self.provider,
            "voice": self.voice,
            "style": self.style,
            "fx": self.fx,
        }
        if self.fallback:
            out["fallback"] = [f.to_json() for f in self.fallback]
        return out

    def label(self) -> str:
        return f"{self.provider}/{self.voice or 'default'}"


# The last resort of every language: free, keyless, no quota (Edge neural).
SAFETY: dict[str, Voice] = {
    "en": Voice("edge", "en-US-GuyNeural"),
    "fr": Voice("edge", "fr-FR-HenriNeural"),
    "ru": Voice("edge", "ru-RU-DmitryNeural"),
}

# Shipped until the operator picks (`navig obs show voices`). All male: the old
# defaults (MiMo's Mia, Edge's Jenny) were women's voices. MiMo is English-only
# in practice — measured 2026-10-16: its French slurs ("SCHEMA est déployé"
# heard back as "Chemin est déployé") and its Russian is unintelligible — so
# French and Russian ship on Edge; a richer voice (an ElevenLabs id from the
# operator's own library) goes in obs.show.say_voices.
DEFAULTS: dict[str, Voice] = {
    "en": Voice("mimo", "Dean", PIGEON_STYLE["en"]),
    "fr": SAFETY["fr"],
    "ru": SAFETY["ru"],
}


def lang_of(collection: str | None) -> str | None:
    """``SCHEMA BROADCAST (ru)`` → ``ru``; no language suffix → None."""
    m = _COLLECTION_LANG.search(collection or "")
    return m.group(1).lower() if m else None


def norm_lang(lang: str | None) -> str | None:
    code = (lang or "").strip().lower().split("-")[0]
    return code if code in LANGS else None


def _voice(raw: Any, base: Voice | None) -> Voice | None:
    if not isinstance(raw, Mapping):
        return base
    provider = str(raw.get("provider") or (base.provider if base else "")).strip()
    if not provider:
        return base

    def opt(key: str) -> str | None:
        if key in raw:
            v = raw.get(key)
            return str(v).strip() or None if v is not None else None
        # A different provider never inherits the old provider's voice id —
        # nor its style note, which was written for that engine.
        if base is None or (key in ("voice", "style") and provider != base.provider):
            return None
        return getattr(base, key)

    fallback: tuple[Voice, ...] = ()
    if isinstance(raw.get("fallback"), list):
        fallback = tuple(
            v for v in (_voice(f, None) for f in raw["fallback"]) if v is not None
        )
    elif base is not None and provider == base.provider:
        fallback = base.fallback
    return Voice(provider, opt("voice"), opt("style"), opt("fx"), fallback)


def table(raw: Any = None) -> dict[str, Voice]:
    """The per-language voices: config (a mapping or its JSON text) over DEFAULTS."""
    if isinstance(raw, str):
        try:
            raw = json.loads(raw) if raw.strip() else None
        except ValueError:
            raw = None
    out = dict(DEFAULTS)
    if isinstance(raw, Mapping):
        for lang, entry in raw.items():
            code = norm_lang(str(lang))
            if code:
                v = _voice(entry, out.get(code))
                if v is not None:
                    out[code] = v
    return out


def resolve(
    lang: str,
    voices: Mapping[str, Voice],
    *,
    provider: str | None = None,
    voice: str | None = None,
) -> Voice:
    """The voice for ``lang``; ``--provider``/``--voice`` override it for one line."""
    base = voices.get(lang) or voices.get(DEFAULT_LANG) or DEFAULTS[DEFAULT_LANG]
    if provider and provider != base.provider:
        # One line on another engine: the language's own voice becomes its
        # first fallback, so a bad --provider still speaks.
        return Voice(provider, voice, None, base.fx, (base, *base.fallback))
    if voice:
        return replace(base, voice=voice)
    return base


def chain(v: Voice, lang: str) -> list[Voice]:
    """Who tries, in order: the voice, its fallbacks, then the free Edge voice."""
    out = [v, *v.fallback]
    safety = SAFETY.get(lang) or SAFETY[DEFAULT_LANG]
    if not any(x.provider == safety.provider and x.voice == safety.voice for x in out):
        out.append(safety)
    return out


# ── fx: the pigeon's character ───────────────────────────────────────────────
_FX_ITEM = re.compile(r"^\s*(pitch|tempo)\s*=\s*([+-]?\d+(?:\.\d+)?)\s*$", re.I)
PITCH_LIMIT = 6.0  # semitones either way — past that it is a chipmunk, not a bird
TEMPO_RANGE = (0.75, 1.35)


def parse_fx(spec: str | None) -> dict[str, float]:
    """``"pitch=+2,tempo=1.08"`` → ``{"pitch": 2.0, "tempo": 1.08}``.

    Raises ValueError on anything else, so a typo is loud, not a silent no-op.
    """
    out: dict[str, float] = {}
    for item in (spec or "").split(","):
        if not item.strip():
            continue
        m = _FX_ITEM.match(item)
        if not m:
            raise ValueError(f"fx '{item.strip()}': expected pitch=<semitones> or tempo=<factor>")
        out[m.group(1).lower()] = float(m.group(2))
    if abs(out.get("pitch", 0.0)) > PITCH_LIMIT:
        raise ValueError(f"fx pitch must be within ±{PITCH_LIMIT:g} semitones")
    lo, hi = TEMPO_RANGE
    if not lo <= out.get("tempo", 1.0) <= hi:
        raise ValueError(f"fx tempo must be within {lo:g}–{hi:g}")
    return out


def fx_filter(spec: str | None, sample_rate: int) -> str | None:
    """The ffmpeg ``-af`` chain for ``spec`` (None when it changes nothing).

    Pitch: resample-shift (``asetrate``) then restore the speed with ``atempo``;
    tempo multiplies into the same ``atempo``. Loudness is evened last so a
    pitched clip sits at the same level as an untouched one.
    """
    fx = parse_fx(spec)
    ratio = math.pow(2.0, fx.get("pitch", 0.0) / 12.0)
    tempo = fx.get("tempo", 1.0)
    if abs(ratio - 1.0) < 1e-6 and abs(tempo - 1.0) < 1e-6:
        return None
    parts: list[str] = []
    if abs(ratio - 1.0) >= 1e-6:
        parts += [f"asetrate={round(sample_rate * ratio)}", f"aresample={sample_rate}"]
    speed = tempo / ratio
    if abs(speed - 1.0) >= 1e-6:
        parts.append(f"atempo={speed:.5f}")
    parts.append("loudnorm=I=-16:TP=-1.5:LRA=11")
    return ",".join(parts)


# ── the world's lines, in every language ─────────────────────────────────────
KICKER = {"en": "THE GRID", "fr": "LA GRILLE", "ru": "СЕТКА"}

# The world layers a patch touches (Schema's Source log, layers.ts), as the
# app itself names them in each language (source.layer_* in its bundles).
LAYER_NAMES: dict[str, dict[str, str]] = {
    "rooms": {"en": "the Rooms", "fr": "les Salles", "ru": "Комнаты"},
    "zone": {"en": "the Zone layer", "fr": "la couche Zone", "ru": "Слой Зоны"},
    "grid": {"en": "the Grid", "fr": "la Grille", "ru": "Сетка"},
    "passport": {"en": "the Passport office", "fr": "le bureau des Passeports", "ru": "Паспортный стол"},
    "soul": {"en": "the Soul ledger", "fr": "le registre de l’Âme", "ru": "Реестр Души"},
    "missions": {"en": "the Mission board", "fr": "le tableau des Missions", "ru": "Доска Миссий"},
    "pulse": {"en": "the Pulse ledger", "fr": "le registre du Pulse", "ru": "Реестр Пульса"},
    "wire": {"en": "the Wire", "fr": "le Fil", "ru": "Эфир"},
    "broadcast": {"en": "Schema TV", "fr": "Schema TV", "ru": "Schema TV"},
    "tongues": {"en": "the Network's tongues", "fr": "les langues du Réseau", "ru": "Языки Сети"},
    "ensina": {"en": "Ensina's desk", "fr": "le bureau d’Ensina", "ru": "Стол Энсины"},
    "citizen": {"en": "the citizen card", "fr": "la carte citoyen", "ru": "Карточка гражданина"},
    "gate": {"en": "the Gate", "fr": "la Porte", "ru": "Врата"},
    "courier": {"en": "the Courier", "fr": "le Courrier", "ru": "Курьер"},
    "relay": {"en": "the Relay", "fr": "le Relais", "ru": "Ретранслятор"},
    "darkgrid": {"en": "the Dark Grid", "fr": "la Grille sombre", "ru": "Тёмная Сетка"},
    "mask": {"en": "the Mask", "fr": "le Masque", "ru": "Маска"},
    "interface": {"en": "the Interface", "fr": "l’Interface", "ru": "Интерфейс"},
    "kernel": {"en": "the Kernel", "fr": "le Noyau", "ru": "Ядро"},
}
MAX_LAYERS_SAID = 3


def deploy_line(layers: list[str], lang: str, headline: str = "") -> str | None:
    """What the pigeon says when a patch lands, or None (nothing a citizen sees).

    English reads the patch's own headline (already world-voiced and
    brand-guarded by Schema); French and Russian name the layers it touched —
    the headline exists only in English. Russian lists them after a colon, so
    the nominative names never need declining.
    """
    names = [LAYER_NAMES[k].get(lang) or LAYER_NAMES[k]["en"] for k in layers if k in LAYER_NAMES]
    if lang == "en" and headline:
        return f"SCHEMA updated. {headline.rstrip('.')}."
    if not names:
        return None
    shown, more = names[:MAX_LAYERS_SAID], len(names) - MAX_LAYERS_SAID
    if lang == "fr":
        listed = shown[0] if len(shown) == 1 else ", ".join(shown[:-1]) + " et " + shown[-1]
        tail = f", et {more} autre{'s' if more > 1 else ''}" if more > 0 else ""
        return f"SCHEMA est mis à jour. L’Architecte a touché {listed}{tail}."
    if lang == "ru":
        tail = f" и ещё {more}" if more > 0 else ""
        return f"SCHEMA обновлена. Архитектор изменил: {', '.join(shown)}{tail}."
    listed = shown[0] if len(shown) == 1 else ", ".join(shown[:-1]) + " and " + shown[-1]
    tail = f", and {more} more" if more > 0 else ""
    return f"SCHEMA updated. The Architect touched {listed}{tail}."


DEPLOY = {
    "en": "SCHEMA deployed.",
    "fr": "SCHEMA est déployé.",
    "ru": "SCHEMA обновлена.",
}
