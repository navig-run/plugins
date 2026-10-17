"""Voice remote: a voice assistant (Alexa) drives the stage — a fixed menu.

The assistant's skill signs each intent and posts it to NAVIG's ingest door
(``POST /api/ingest/<source>``, reachable through Lighthouse as
``/ingest/<tenant>/<source>``). The gateway checks the HMAC, the timestamp
window and replays, then hands the verified payload here (an *ingest action*,
``navig.gateway.ingest_actions``). This module maps it onto ONE of a fixed set
of moves and returns the sentence the assistant says back.

Payload: ``{"intent": "SwitchScene", "slots": {"scene": "the architect"},
"locale": "fr-FR"}``.

The menu (nothing else is reachable — there is no free-form command):

| intent         | does                                                   |
|----------------|--------------------------------------------------------|
| SwitchScene    | program scene = the closest scene name / number        |
| EmergencyCut   | program scene = the Emergency Cut                      |
| Say            | the pigeon says {text} on stream (card + voice)        |
| RecordStart    | start recording                                        |
| RecordStop     | stop recording                                         |
| Status         | which scene is on, live or not, recording or not       |
| FreeGames      | reads back today's free games                          |
| OpenPage       | opens an allow-listed page in the default browser      |

Going LIVE is deliberately not on the menu: a stream start is a point of no
return and stays a hand on the button.
"""

from __future__ import annotations

import difflib
import json
import re
import shutil
import subprocess
import webbrowser
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

DEFAULT_SOURCE = "alexa"
DEFAULT_PAGES = {
    "schema": "https://schema.cx",
    "grid": "https://schema.cx",
    "outpost": "https://schema.cx/outpost",
    "broadcast": "https://schema.cx/broadcast",
}
INTENTS = (
    "SwitchScene",
    "EmergencyCut",
    "Say",
    "RecordStart",
    "RecordStop",
    "Status",
    "FreeGames",
    "OpenPage",
)
MAX_SAY = 200

_NUMBERS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
    "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13,
    "fourteen": 14, "fifteen": 15, "sixteen": 16, "seventeen": 17, "eighteen": 18,
    "un": 1, "une": 1, "deux": 2, "trois": 3, "quatre": 4, "cinq": 5, "sept": 7,
    "huit": 8, "neuf": 9, "dix": 10, "onze": 11, "douze": 12, "treize": 13,
    "quatorze": 14, "quinze": 15, "seize": 16,
}

# What the assistant says back (its locale's language; English otherwise).
REPLIES: dict[str, dict[str, str]] = {
    "switched": {"en": "On air: {scene}.", "fr": "À l'antenne : {scene}."},
    "no_scene": {"en": "I can't find a scene called {q}.", "fr": "Je ne trouve pas de scène appelée {q}."},
    "cut": {"en": "Emergency cut.", "fr": "Coupure d'urgence."},
    "said": {"en": "The pigeon said it.", "fr": "Le pigeon l'a dit."},
    "say_what": {"en": "What should the pigeon say?", "fr": "Que doit dire le pigeon ?"},
    "rec_on": {"en": "Recording.", "fr": "Enregistrement lancé."},
    "rec_off": {"en": "Recording stopped.", "fr": "Enregistrement arrêté."},
    "status": {
        "en": "On air: {scene}. {live}. {rec}.",
        "fr": "À l'antenne : {scene}. {live}. {rec}.",
    },
    "live_yes": {"en": "You are live", "fr": "Vous êtes en direct"},
    "live_no": {"en": "Not live", "fr": "Pas en direct"},
    "rec_yes": {"en": "Recording", "fr": "Enregistrement en cours"},
    "rec_no": {"en": "Not recording", "fr": "Pas d'enregistrement"},
    "games": {"en": "Free today: {list}.", "fr": "Gratuit aujourd'hui : {list}."},
    "no_games": {"en": "No free games right now.", "fr": "Aucun jeu gratuit pour l'instant."},
    "opened": {"en": "Opened {page}.", "fr": "{page} est ouvert."},
    "no_page": {"en": "I can only open: {pages}.", "fr": "Je peux seulement ouvrir : {pages}."},
    "obs_down": {"en": "OBS is not answering.", "fr": "OBS ne répond pas."},
    "unknown": {"en": "I can't do that from here.", "fr": "Je ne peux pas faire ça d'ici."},
}


def lang_of_locale(locale: str | None) -> str:
    code = (locale or "").split("-")[0].lower()
    return code if code in ("en", "fr") else "en"


def reply(key: str, lang: str, **kw: str) -> str:
    variants = REPLIES[key]
    return (variants.get(lang) or variants["en"]).format(**kw)


def match_scene(query: str, scenes: list[str]) -> str | None:
    """The scene a spoken name means: a number ("two", "2") or the closest name."""
    q = re.sub(r"[^\w\s]", " ", (query or "").lower()).strip()
    if not q or not scenes:
        return None
    words = q.split()
    for w in words:
        n = int(w) if w.isdigit() else _NUMBERS.get(w)
        if n:
            for s in scenes:
                if re.match(rf"^0*{n}\b", s):
                    return s
    names = {re.sub(r"^\d+\s*", "", s).lower(): s for s in scenes}
    q = re.sub(r"^(the|la|le|les|l)\s+", "", q)
    for name, s in names.items():
        if q == name or q in name.split() or (len(q) > 3 and q in name):
            return s
    close = difflib.get_close_matches(q, list(names), n=1, cutoff=0.55)
    return names[close[0]] if close else None


class Stage(Protocol):
    def scenes(self) -> list[str]: ...
    def switch(self, scene: str) -> None: ...
    def record(self, start: bool) -> None: ...
    def status(self) -> dict[str, Any]: ...
    def say(self, text: str, lang: str) -> None: ...


@dataclass
class Remote:
    stage: Stage
    pages: dict[str, str] = field(default_factory=lambda: dict(DEFAULT_PAGES))
    open_url: Callable[[str], Any] = webbrowser.open
    games: Callable[[], list[str]] | None = None

    def handle(self, payload: dict[str, Any]) -> dict[str, Any]:
        """One verified intent → one move → ``{"speech": …, "done": intent}``.

        Raises only when OBS itself failed (the route then answers 502 so the
        skill can say so); a request it does not understand is a polite no.
        """
        intent = str(payload.get("intent") or "")
        slots = payload.get("slots") if isinstance(payload.get("slots"), dict) else {}
        lang = lang_of_locale(str(payload.get("locale") or ""))
        moves: dict[str, Callable[[dict, str], str]] = {
            "SwitchScene": self._switch_scene,
            "EmergencyCut": self._emergency_cut,
            "Say": self._say,
            "RecordStart": self._record_start,
            "RecordStop": self._record_stop,
            "Status": self._status,
            "FreeGames": self._free_games,
            "OpenPage": self._open_page,
        }
        move = moves.get(intent) if intent in INTENTS else None
        if move is None:
            return {"speech": reply("unknown", lang), "done": None}
        return {"speech": move(slots, lang), "done": intent}

    # ── the menu ───────────────────────────────────────────────────────────
    def _switch_scene(self, slots: dict, lang: str) -> str:
        q = str(slots.get("scene") or "")
        scene = match_scene(q, self.stage.scenes())
        if scene is None:
            return reply("no_scene", lang, q=q or "—")
        self.stage.switch(scene)
        return reply("switched", lang, scene=_spoken(scene))

    def _emergency_cut(self, slots: dict, lang: str) -> str:
        scene = next((s for s in self.stage.scenes() if "emergency" in s.lower()), None)
        if scene is None:
            return reply("no_scene", lang, q="Emergency Cut")
        self.stage.switch(scene)
        return reply("cut", lang)

    def _say(self, slots: dict, lang: str) -> str:
        text = re.sub(r"\s+", " ", str(slots.get("text") or "")).strip()[:MAX_SAY]
        if not text:
            return reply("say_what", lang)
        self.stage.say(text, lang)
        return reply("said", lang)

    def _record_start(self, slots: dict, lang: str) -> str:
        self.stage.record(True)
        return reply("rec_on", lang)

    def _record_stop(self, slots: dict, lang: str) -> str:
        self.stage.record(False)
        return reply("rec_off", lang)

    def _status(self, slots: dict, lang: str) -> str:
        st = self.stage.status()
        return reply(
            "status",
            lang,
            scene=_spoken(str(st.get("scene") or "—")),
            live=reply("live_yes" if st.get("streaming") else "live_no", lang),
            rec=reply("rec_yes" if st.get("recording") else "rec_no", lang),
        )

    def _free_games(self, slots: dict, lang: str) -> str:
        titles = (self.games or free_game_titles)()[:3]
        if not titles:
            return reply("no_games", lang)
        return reply("games", lang, list=", ".join(titles))

    def _open_page(self, slots: dict, lang: str) -> str:
        name = str(slots.get("page") or "").strip().lower()
        url = self.pages.get(name)
        if not url or not url.startswith("https://"):
            return reply("no_page", lang, pages=", ".join(sorted(self.pages)))
        self.open_url(url)
        return reply("opened", lang, page=name)


def _spoken(scene: str) -> str:
    """'02 The Architect' → 'The Architect' (the number is for the operator)."""
    return re.sub(r"^\d+\s*", "", scene) or scene


def free_game_titles() -> list[str]:
    exe = shutil.which("navig")
    if not exe:
        return []
    try:
        res = subprocess.run(  # noqa: S603 — fixed argv, no shell
            [exe, "games", "check", "--json"],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=90,
        )
        doc = json.loads(res.stdout) if res.returncode == 0 else {}
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return []
    return [str(g.get("title")) for g in doc.get("current", []) if g.get("title")]


class ObsStage:
    """The real stage: short obs-websocket sessions (the bridge's pattern)."""

    def __init__(self, client_factory: Callable[[], Any]) -> None:
        self._client = client_factory

    def _run(self, move: Callable[[Any], Any]) -> Any:
        client = self._client()
        try:
            client.connect()
            return move(client)
        finally:
            client.close()

    def scenes(self) -> list[str]:
        res = self._run(lambda c: c.request("GetSceneList", {}))
        return [s["sceneName"] for s in reversed(res.get("scenes", [])) if s.get("sceneName")]

    def switch(self, scene: str) -> None:
        self._run(lambda c: c.request("SetCurrentProgramScene", {"sceneName": scene}))

    def record(self, start: bool) -> None:
        self._run(lambda c: c.request("StartRecord" if start else "StopRecord", {}))

    def status(self) -> dict[str, Any]:
        def look(c: Any) -> dict[str, Any]:
            scene = c.request("GetCurrentProgramScene", {}).get("currentProgramSceneName")
            stream = c.request("GetStreamStatus", {}).get("outputActive")
            rec = c.request("GetRecordStatus", {}).get("outputActive")
            return {"scene": scene, "streaming": bool(stream), "recording": bool(rec)}

        return self._run(look)

    def say(self, text: str, lang: str) -> None:
        from navig_obs.show import say as say_mod
        from navig_obs.show import voices
        from navig_obs.show.settings import load_settings

        s = load_settings()
        p = say_mod.prepare(
            say_mod.plan_for(s, text, lang=lang, kicker=voices.KICKER.get(lang, "THE GRID"))
        )
        self._run(
            lambda c: say_mod.perform(
                c, p, toast_event=s.toast_event, pigeon_event=s.pigeon_event,
                cue_input=s.cue_input,
            )
        )
