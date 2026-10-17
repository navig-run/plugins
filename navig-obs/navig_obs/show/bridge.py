"""The show-events service: runs inside the navig gateway, feeds OBS.

Started by the plugin's ``gateway:register_routes`` hook when
``obs.show.enabled`` is on. Every OBS call is a short-lived
:class:`ObsClient` connection run in a worker thread, so the gateway loop never
blocks; an OBS that is closed makes each moment a logged no-op (the feed state
still advances — a later start never replays a backlog onto the screen).
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from typing import Any, Callable

from navig_obs.engine.client import ObsClient
from navig_obs.engine.config import resolve_target
from navig_obs.engine.errors import ObsAuthError, ObsConnectionError, ObsRequestError
from navig_obs.show import actions, civic, feed, frames, moments, voices
from navig_obs.show import say as say_mod
from navig_obs.show.cues import CueBook, CueBookError
from navig_obs.show.settings import ShowSettings, state_dir

_log = logging.getLogger(__name__)

SALE_TYPES = frozenset({"sale"})
CUE_SALE = "sale-ring"
WINDFALL_COOLDOWN_S = 90.0  # one celebration per burst of sales
CUE_PATCH = "patch-landed"
_OBS_ERRORS = (ObsAuthError, ObsConnectionError, ObsRequestError)
NOT_READY = 207  # obs-websocket: a collection/profile is loading
NOT_READY_TRIES = 6
NOT_READY_WAIT_S = 1.5


def _default_client() -> ObsClient:
    t = resolve_target()
    return ObsClient(
        t.host, t.port, t.password, connect_timeout=2.0, request_timeout=5.0
    )


class ShowBridge:
    def __init__(
        self,
        settings: ShowSettings,
        *,
        client_factory: Callable[[], Any] = _default_client,
        fetcher: Callable[[str], dict[str, Any]] = feed.fetch,
        state_path: Any = None,
        voice_prepare: Callable[[say_mod.SayPlan], say_mod.SayPlan] = say_mod.prepare,
    ) -> None:
        self.settings = settings
        self._client_factory = client_factory
        self._fetcher = fetcher
        self._state_path = state_path or (state_dir() / "feed-state.json")
        self._book = CueBook()
        self._book_error: str | None = None
        self._task: asyncio.Task | None = None
        self._civic_task: asyncio.Task | None = None
        self._frames_task: asyncio.Task | None = None
        self._frames = frames.FrameWatch()
        self._director = frames.Director(brb_scene=settings.brb_scene)
        self._frames_client: Any = None
        # The one held connection is shared by the frames watch and the moment
        # writer; each runs in a worker thread, so they take turns on it.
        self._held_lock = threading.Lock()
        self._moments = moments.Recorder()
        self._moment_rate = moments.RateLimit()
        self._moment_token: str | None = None
        self._obs_down_until = 0.0  # monotonic: skip connecting until then
        self._moment_tasks: set[asyncio.Task] = set()
        self._voice_prepare = voice_prepare
        self._civic_path = (
            state_path.parent if state_path else state_dir()
        ) / "civic-state.json"
        self._queue: Any = None
        self.last: dict[str, Any] = {"obs": None, "feed": None, "fired": []}
        self._last_windfall = float("-inf")

    # ── lifecycle ────────────────────────────────────────────────────────────
    async def start(self) -> None:
        self.reload_cues()
        try:
            self._moment_token = moments.ensure_token()
        except OSError as exc:  # no state dir: moments stay off, the show does not
            _log.warning("obs-show: no moment token (%s) - pigeon moments off", exc)
        try:
            from navig.gateway.system_events import get_system_events

            self._queue = get_system_events()
        except Exception:  # noqa: BLE001 — standalone: no gateway bus
            self._queue = None
        if self._queue is not None:
            self._queue.subscribe("notification", self.on_notification)
        if self.settings.patch_feed:
            self._task = asyncio.get_running_loop().create_task(
                self._poll_forever(), name="obs-show-feed"
            )
        if self.settings.civic_feed:
            self._civic_task = asyncio.get_running_loop().create_task(
                self._poll_civic_forever(), name="obs-show-civic"
            )
        if self.settings.frames_watch:
            self._frames_task = asyncio.get_running_loop().create_task(
                self._watch_frames_forever(), name="obs-show-frames"
            )
        _log.info(
            "obs-show: on (cues=%d, feed=%s, civic=%s, frames=%s)",
            len(self._book.cues),
            self.settings.patch_feed or "off",
            self.settings.civic_feed or "off",
            "on" if self.settings.frames_watch else "off",
        )

    async def stop(self) -> None:
        if self._queue is not None:
            try:
                self._queue.unsubscribe("notification", self.on_notification)
            except Exception:  # noqa: BLE001
                pass
        for name in ("_task", "_civic_task", "_frames_task"):
            task = getattr(self, name)
            if task is not None:
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):  # noqa: BLE001
                    pass
                setattr(self, name, None)

    def reload_cues(self) -> None:
        try:
            self._book = CueBook.load(self.settings.cues_file)
            self._book_error = None
        except CueBookError as exc:
            self._book = CueBook()
            self._book_error = str(exc)
            _log.warning("obs-show: %s — sounds off until it exists", exc)

    # ── moments ──────────────────────────────────────────────────────────────
    async def on_notification(self, event: Any) -> None:
        payload = getattr(event, "payload", None) or {}
        if payload.get("type") in SALE_TYPES:
            await self.cue(CUE_SALE)
            await self.windfall()

    async def windfall(self, now: float | None = None) -> bool:
        """A sale → the pigeon's `windfall` beat (a coin, hops, one of his own
        `sale` lines). The act travels ALONE: no store, buyer, amount or title
        from the notification is ever sent, so nothing about a sale can reach
        the screen. Cooled down so a burst of sales is one celebration."""
        t = time.monotonic() if now is None else now
        if t - self._last_windfall < WINDFALL_COOLDOWN_S:
            return False
        self._last_windfall = t
        return await self._obs(
            lambda c: actions.emit_event(
                c, self.settings.pigeon_event, {"act": "windfall"}
            ),
            "windfall",
        )

    async def cue(self, cue_id: str) -> bool:
        cue = self._book.get(cue_id)
        if cue is None or not self._book.ready(cue_id):
            return False
        return await self._obs(
            lambda c: actions.play_cue(c, self.settings.cue_input, cue), f"cue {cue_id}"
        )

    async def toast(self, toast: dict[str, Any]) -> bool:
        if toast.get("kind") == "patch" and await self._speak_patch(toast):
            return True
        shown = await self._obs(
            lambda c: actions.emit_toast(c, self.settings.toast_event, toast), "toast"
        )
        if shown:
            await self.cue(CUE_PATCH)
        return shown

    async def _speak_patch(self, toast: dict[str, Any]) -> bool:
        """A published patch, SPOKEN (obs.show.deploy_say): one alert card with
        the voice instead of the silent card + sting — both use the one cue
        input, so the sting would cut the voice. False = fall back to the card."""
        if not self.settings.deploy_say or not await self.may_speak():
            return False
        lang = await self.stage_lang()
        code, _ = say_mod.voice_for(self.settings, lang)
        text = voices.deploy_line(
            list(toast.get("layers") or []), code, str(toast.get("headline") or "")
        )
        if not text:
            return False  # nothing a citizen can see changed: card only
        version = str(toast.get("version") or "").strip()
        return await self.say(
            say_mod.plan_for(
                self.settings,
                text,
                lang=code,
                title=str(toast.get("headline") or "") or text,
                kicker=f"SCHEMA {version}".strip(),
                tone="deploy",
            )
        )

    async def may_speak(self) -> bool:
        """The bridge's own voice waits for an audience: with
        obs.show.say_on_air_only (default on) it speaks only while OBS is
        streaming or recording — no line at 3 a.m. into an empty room, and no
        paid voice spent on nobody."""
        if not self.settings.say_on_air_only:
            return True
        found: list[bool] = []

        def look(c: Any) -> None:
            stream = (c.request("GetStreamStatus", {}) or {}).get("outputActive")
            rec = (c.request("GetRecordStatus", {}) or {}).get("outputActive")
            found.append(bool(stream or rec))

        ok = await self._obs(look, "on air?")
        return bool(ok and found and found[0])

    async def poll_once(self) -> list[dict[str, Any]]:
        try:
            doc = await asyncio.to_thread(self._fetcher, self.settings.patch_feed)
        except Exception as exc:  # noqa: BLE001 — a flaky feed is a skipped beat
            if self.last.get("feed") != str(exc):
                _log.warning("obs-show: feed unreachable (%s)", exc)
            self.last["feed"] = str(exc)
            return []
        self.last["feed"] = "ok"
        toasts, state = feed.diff(feed.load_state(self._state_path), doc)
        feed.save_state(self._state_path, state)
        for t in toasts:
            await self.toast(t)
        return toasts

    async def poll_civic_once(self) -> list[dict[str, Any]]:
        prev = civic.load_state(self._civic_path)
        url = civic.feed_url(self.settings.civic_feed, prev)
        try:
            doc = await asyncio.to_thread(self._fetcher, url)
        except Exception as exc:  # noqa: BLE001 — a flaky feed is a skipped beat
            if self.last.get("civic") != str(exc):
                _log.warning("obs-show: civic feed unreachable (%s)", exc)
            self.last["civic"] = str(exc)
            return []
        self.last["civic"] = "ok"
        events, state = civic.diff(prev, doc)
        civic.save_state(self._civic_path, state)
        speak = bool(events) and self.settings.civic_say and await self.may_speak()
        lang = await self.stage_lang() if speak else None
        for ev in events:
            shown = await self._obs(
                lambda c, ev=ev: actions.emit_event(c, self.settings.civic_event, ev),
                f"civic {ev.get('kind')}",
            )
            if not (shown and speak):
                continue
            code, _ = say_mod.voice_for(self.settings, lang)
            text = civic.line(ev, code)
            if text:
                await self.say(
                    say_mod.plan_for(
                        self.settings,
                        text,
                        lang=code,
                        kicker=voices.KICKER.get(code, "THE GRID"),
                        tone=civic.tone(ev),
                    )
                )
        return events

    async def stage_lang(self) -> str | None:
        """The loaded collection's language, or None (OBS closed / unnamed)."""
        found: list[str | None] = []
        ok = await self._obs(
            lambda c: found.append(voices.lang_of(actions.current_collection(c))),
            "stage language",
        )
        return found[0] if ok and found else None

    async def say(self, p: say_mod.SayPlan) -> bool:
        """Speak one line: synthesis off the loop, then card -> pigeon -> clip."""
        p = await asyncio.to_thread(self._voice_prepare, p)
        for w in p.warnings:
            _log.warning("obs-show: %s", w)
        return await self._obs(
            lambda c: say_mod.perform(
                c,
                p,
                toast_event=self.settings.toast_event,
                pigeon_event=self.settings.pigeon_event,
                cue_input=self.settings.cue_input,
            ),
            "say",
        )

    async def check_frames_once(self, now: float | None = None) -> str | None:
        """One look at OBS's dropped-frame counters; a burst -> the pigeon's lag line.

        Returns the kind that burst (render / encode / network), else None.
        OBS closed or loading is a quiet skip; the watch keeps its baseline.
        """

        # ONE connection for the whole watch, opened lazily and dropped only
        # when it fails. A fresh connect per 5 s look wrote two lines into
        # OBS's own log every time — 1,411 of 2,090 lines on 2026-10-16,
        # burying everything else — and paid a handshake per sample.
        scene_box: list[str] = []

        def run() -> dict[str, tuple[int, int]]:
            with self._held_lock:
                c = self._held()
                stats = c.request("GetStats") or {}
                try:
                    stream = c.request("GetStreamStatus") or {}
                except ObsRequestError as exc:
                    if exc.code == NOT_READY:
                        raise
                    stream = {}
                if self.settings.health_director and stream.get("outputActive"):
                    scene_box.append(
                        (c.request("GetCurrentProgramScene") or {}).get("currentProgramSceneName", "")
                    )
            return frames.sample_from(stats, stream)

        if time.monotonic() < self._obs_down_until:
            return None
        try:
            sample = await asyncio.to_thread(run)
        except _OBS_ERRORS:
            self._obs_failed()
            return None
        except Exception:  # noqa: BLE001 — a dead socket raises whatever it likes
            self._obs_failed()
            return None
        at = now if now is not None else asyncio.get_running_loop().time()
        kind = self._frames.update(sample, at)
        if self.settings.health_director:
            await self._direct(sample, scene_box[0] if scene_box else "", at)
        if kind:
            _log.info("obs-show: dropped frames (%s) -> pigeon lag", kind)
            await self._obs(
                lambda c: actions.emit_event(
                    c, self.settings.pigeon_event, {"notice": "lag", "kind": kind}
                ),
                f"frames:{kind}",
            )
        return kind

    def _held(self) -> Any:
        """The held OBS connection (call with _held_lock taken)."""
        if self._frames_client is None:
            client = self._client_factory()
            client.connect()
            self._frames_client = client
        return self._frames_client

    async def moment(self, payload: Any, now: float | None = None) -> dict[str, Any]:
        """One report from the pigeon page -> a marker or a caption line.

        Returns {"ok": True, ...} or {"ok": False, "why": ...}; never raises to
        the route (a bad report is the page's problem, not the gateway's).
        """
        try:
            m = moments.validate(payload, self._moment_token or "")
        except moments.MomentRejected as exc:
            return {"ok": False, "why": str(exc)}
        if not self._moment_rate.allow(now if now is not None else time.monotonic()):
            return {"ok": False, "why": "rate limited"}
        # Heard, whatever happens next: "the page reports" and "OBS records"
        # are two different questions, and the status route answers both.
        heard = self.last.setdefault("moments", {"heard": 0, "stamped": 0})
        heard["heard"] += 1
        heard["lastKind"] = m["kind"]
        if time.monotonic() < self._obs_down_until:
            heard["lastSkip"] = "obs unavailable"
            return {"ok": False, "why": "obs unavailable"}

        def run() -> dict[str, Any] | None:
            with self._held_lock:
                c = self._held()
                status = c.request("GetRecordStatus") or {}
                if not status.get("outputActive"):
                    return None
                start = int(time.time() * 1000) - int(status.get("outputDuration") or 0)
                ctx: dict[str, Any] = {}
                if self._moments.find_session(start) is None:
                    ctx = self._session_context(c)
                return self._moments.record(m, status, ctx)

        try:
            out = await asyncio.to_thread(run)
        except _OBS_ERRORS as exc:
            self._obs_failed()
            heard["lastSkip"] = "obs unavailable"
            return {"ok": False, "why": f"obs: {exc}"}
        except OSError as exc:
            heard["lastSkip"] = "disk"
            return {"ok": False, "why": f"disk: {exc}"}
        except Exception as exc:  # noqa: BLE001 — a dead socket raises whatever it likes
            self._obs_failed()
            heard["lastSkip"] = "obs unavailable"
            return {"ok": False, "why": f"obs: {exc}"}
        if out is None:
            heard["lastSkip"] = "not recording"
            return {"ok": False, "why": "not recording"}
        heard["stamped"] += 1
        return {"ok": True, **out}

    @staticmethod
    def _session_context(c: Any) -> dict[str, Any]:
        """What a new session records about the stage (best effort, never fatal)."""
        def ask(rtype: str) -> dict[str, Any]:
            try:
                return c.request(rtype) or {}
            except ObsRequestError:
                return {}
        video = ask("GetVideoInfo")
        return {
            "scene": ask("GetCurrentProgramScene").get("currentProgramSceneName", ""),
            "collection": ask("GetSceneCollectionList").get("currentSceneCollectionName", ""),
            "portrait": int(video.get("baseCX") or 0) < int(video.get("baseCY") or 0),
            "recordDirectory": ask("GetRecordDirectory").get("recordDirectory"),
        }

    async def _direct(self, sample: dict[str, Any], scene: str, at: float) -> None:
        """The stream health director's move, if it has one (opt-in)."""
        act = self._director.observe(
            self._frames.deltas, streaming="network" in sample, scene=scene, now=at,
        )
        if act is None:
            return
        verb, target = act
        _log.info("obs-show: stream health %s -> %s", verb, target)
        say = {"notice": "lag", "kind": "network"} if verb == "cut" else {"say": "back"}
        done = await self._obs(
            lambda c: (
                c.request("SetCurrentProgramScene", {"sceneName": target}),
                actions.emit_event(c, self.settings.pigeon_event, say),
            ),
            f"health:{verb}",
        )
        self.last["director"] = {"verb": verb, "scene": target, "ok": done}

    def moment_soon(self, payload: Any) -> None:
        """Record a moment in the background: the route answers at once (a
        beacon never reads the reply, and a closed OBS costs a 2 s connect)."""
        task = asyncio.get_running_loop().create_task(self.moment(payload))
        self._moment_tasks.add(task)
        task.add_done_callback(self._moment_tasks.discard)

    def _obs_failed(self) -> None:
        """OBS did not answer: drop the held connection and stop trying for a
        moment, so a burst of reports never queues a connect each."""
        self._drop_frames_client()
        self._obs_down_until = time.monotonic() + 5.0

    def _drop_frames_client(self) -> None:
        c, self._frames_client = self._frames_client, None
        if c is not None:
            try:
                c.close()
            except Exception:  # noqa: BLE001 — already gone
                pass

    async def _watch_frames_forever(self) -> None:
        try:
            while True:
                await self.check_frames_once()
                await asyncio.sleep(frames.POLL_SECONDS)
        finally:
            self._drop_frames_client()

    async def _poll_civic_forever(self) -> None:
        while True:
            await self.poll_civic_once()
            await asyncio.sleep(self.settings.poll_seconds)

    async def _poll_forever(self) -> None:
        while True:
            await self.poll_once()
            await asyncio.sleep(self.settings.poll_seconds)

    async def _obs(self, move: Callable[[Any], None], label: str) -> bool:
        def run() -> None:
            client = self._client_factory()
            try:
                client.connect()
                move(client)
            finally:
                client.close()

        try:
            for attempt in range(NOT_READY_TRIES):
                try:
                    await asyncio.to_thread(run)
                    break
                except ObsRequestError as exc:
                    # 207: OBS is loading a collection/profile — wait it out
                    # instead of dropping the beat.
                    if exc.code != NOT_READY or attempt == NOT_READY_TRIES - 1:
                        raise
                    await asyncio.sleep(NOT_READY_WAIT_S)
        except _OBS_ERRORS as exc:
            if self.last.get("obs") != str(exc):
                _log.info("obs-show: %s skipped (%s)", label, exc)
            self.last["obs"] = str(exc)
            return False
        self.last["obs"] = "ok"
        self.last["fired"] = ([label] + list(self.last.get("fired") or []))[:10]
        return True

    def status(self) -> dict[str, Any]:
        return {
            "enabled": self.settings.enabled,
            "feed": self.settings.patch_feed or None,
            "cueInput": self.settings.cue_input,
            "cues": sorted(self._book.cues),
            "cueBookError": self._book_error,
            "running": self._task is not None and not self._task.done(),
            "civicFeed": self.settings.civic_feed or None,
            "civicSay": self.settings.civic_say,
            "civicRunning": self._civic_task is not None
            and not self._civic_task.done(),
            "framesWatch": self._frames_task is not None and not self._frames_task.done(),
            "healthDirector": self.settings.health_director,
            "momentsReady": bool(self._moment_token),
            "last": self.last,
        }
