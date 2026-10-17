"""The stage speaks its own language: voices per language, fx, lines, the card's art."""

from __future__ import annotations

import io
import json
import subprocess
import urllib.error
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from navig_obs.commands.obs import obs_app
from navig_obs.show import civic, voices
from navig_obs.show import say as say_mod
from navig_obs.show.settings import ShowSettings

runner = CliRunner()
VENDOR_OK = {
    "CallVendorRequest": {},
    "SetInputSettings": {},
    "TriggerMediaInputAction": {},
}


# ── language from the stage ──────────────────────────────────────────────────
@pytest.mark.parametrize(
    ("name", "lang"),
    [
        ("SCHEMA BROADCAST (ru)", "ru"),
        ("SCHEMA BROADCAST MOBILE (fr)", "fr"),
        ("SCHEMA BROADCAST (EN)", "en"),
        ("SCHEMA BROADCAST", None),
        ("Untitled", None),
        (None, None),
    ],
)
def test_the_collection_names_the_language(name, lang):
    assert voices.lang_of(name) == lang


def test_every_language_has_a_male_voice_by_default():
    assert set(voices.DEFAULTS) == set(voices.LANGS)
    # The old defaults were women's voices (MiMo Mia, Edge Jenny).
    assert all(v.voice not in (None, "mimo_default", "Mia", "en-US-JennyNeural")
               for v in voices.DEFAULTS.values())


def test_config_overrides_one_language_and_keeps_the_rest():
    t = voices.table({"ru": {"provider": "elevenlabs", "voice": "abc", "fx": "pitch=+2"}})
    assert t["ru"] == voices.Voice("elevenlabs", "abc", None, "pitch=+2")
    assert t["fr"] == voices.DEFAULTS["fr"]


def test_config_may_arrive_as_json_text_and_bad_text_is_ignored():
    assert voices.table('{"fr": {"provider": "mimo", "voice": "Milo"}}')["fr"].voice == "Milo"
    assert voices.table("{not json") == voices.DEFAULTS


def test_a_new_provider_never_inherits_the_old_voice_id():
    t = voices.table({"fr": {"provider": "mimo"}})
    assert t["fr"].provider == "mimo" and t["fr"].voice is None


def test_resolve_lets_one_line_override_provider_or_voice():
    t = voices.table()
    assert voices.resolve("ru", t).voice == "ru-RU-DmitryNeural"
    assert voices.resolve("ru", t, voice="ru-RU-SvetlanaNeural").provider == "edge"
    swapped = voices.resolve("en", t, provider="edge")
    assert swapped.provider == "edge" and swapped.voice is None


# ── fx ───────────────────────────────────────────────────────────────────────
def test_fx_pitch_keeps_the_speed_and_evens_loudness():
    chain = voices.fx_filter("pitch=+6", 24000)  # half an octave up
    assert chain.startswith("asetrate=33941,aresample=24000,atempo=0.70711")
    assert chain.endswith("loudnorm=I=-16:TP=-1.5:LRA=11")


def test_fx_tempo_alone_and_nothing_at_all():
    assert voices.fx_filter("tempo=1.1", 24000).startswith("atempo=1.10000,")
    assert voices.fx_filter("", 24000) is None
    assert voices.fx_filter("pitch=0,tempo=1", 24000) is None


@pytest.mark.parametrize("spec", ["pitch=+9", "tempo=3", "speed=2", "pitch"])
def test_a_bad_fx_is_loud(spec):
    with pytest.raises(ValueError):
        voices.parse_fx(spec)


def test_apply_fx_runs_ffmpeg_with_the_chain(tmp_path, monkeypatch):
    clip = tmp_path / "c.mp3"
    clip.write_bytes(b"ID3raw")
    seen = []

    def run(argv, **_kw):
        seen.append(argv)
        if "-af" in argv:
            open(argv[-1], "wb").write(b"ID3fx")
            return subprocess.CompletedProcess(argv, 0, "", "")
        return subprocess.CompletedProcess(argv, 0, "24000\n", "")  # ffprobe

    monkeypatch.setattr("navig_obs.show.say.subprocess.run", run)
    monkeypatch.setattr("navig_obs.show.say.shutil.which", lambda n: n)
    say_mod.apply_fx(clip, "pitch=+2")
    ff = [a for a in seen if "-af" in a][0]
    assert ff[ff.index("-af") + 1].startswith("asetrate=26939,")
    assert clip.read_bytes() == b"ID3fx"


def test_apply_fx_without_ffmpeg_fails_loudly(tmp_path, monkeypatch):
    clip = tmp_path / "c.mp3"
    clip.write_bytes(b"ID3raw")
    monkeypatch.setattr("navig_obs.show.say.shutil.which", lambda n: None)
    with pytest.raises(RuntimeError, match="ffmpeg"):
        say_mod.apply_fx(clip, "pitch=+2")


# ── the cache and the card ───────────────────────────────────────────────────
def test_language_style_and_fx_each_make_a_new_clip():
    base = say_mod.clip_key("mimo", "Dean", "hi")
    keys = {
        say_mod.clip_key("mimo", "Dean", "hi", "en"),
        say_mod.clip_key("mimo", "Dean", "hi", "en", "wry"),
        say_mod.clip_key("mimo", "Dean", "hi", "en", "wry", "pitch=+2"),
        say_mod.clip_key("mimo", "Dean", "hi", "fr"),
    }
    assert len(keys) == 4 and base not in keys


@pytest.mark.parametrize(
    ("url", "kept"),
    [
        ("https://cdn.example/art.jpg", True),
        ("http://cdn.example/art.jpg", False),
        ("file:///C:/x.png", False),
        ("javascript:alert(1)", False),
        ('https://x/"onerror=', False),
        ("", False),
    ],
)
def test_only_an_https_picture_reaches_the_card(url, kept):
    p = say_mod.plan("A game is free", image=url, voiced=False)
    assert ("image" in p.toast()) is kept


# ── lines in every language ──────────────────────────────────────────────────
def _ev(kind, city="Kazan", **payload):
    return {"kind": kind, "zone": {"name": city, "slug": city.lower()}, "payload": payload}


def test_every_civic_line_exists_in_every_language():
    for kind, variants in {**civic.LINES, **civic.PERSONA_LINES}.items():
        assert set(variants) == set(voices.LANGS), kind


@pytest.mark.parametrize(
    ("lang", "line"),
    [
        ("en", "A flash mission opened in KAZAN."),
        ("fr", "Une mission éclair s'ouvre dans la Zone KAZAN."),
        ("ru", "В Зоне KAZAN появилась миссия-вспышка."),
    ],
)
def test_a_flash_on_air_is_announced_in_the_stage_language(lang, line):
    assert civic.line(_ev("flash_live"), lang) == line
    assert civic.tone(_ev("flash_live")) == "game"


def test_an_arrival_names_the_citizen_only_when_the_feed_carries_the_name():
    assert civic.line(_ev("passport_forged", personaName="Nova Kite"), "en") == (
        "Nova Kite arrived in KAZAN."
    )
    assert civic.line(_ev("passport_forged"), "en") == "A Passport was forged in KAZAN."
    assert civic.tone(_ev("passport_forged")) == "passport"


def test_an_unknown_language_falls_back_to_english():
    assert civic.line(_ev("ignition"), "de") == "KAZAN has ignited."


# ── the command, end to end against a fake OBS ───────────────────────────────
@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr("navig_obs.show.settings._cfg", lambda _k: None)
    monkeypatch.setattr("navig_obs.show.settings._cfg_raw", lambda _k: None)
    monkeypatch.setattr("navig_obs.show.say.state_dir", lambda: tmp_path)
    for k in ("NAVIG_OBS_SHOW_SAY_PROVIDER", "NAVIG_OBS_SHOW_SAY_VOICE",
              "NAVIG_OBS_SHOW_SAY_VOICES", "NAVIG_OBS_SHOW_SAY_LANG"):
        monkeypatch.delenv(k, raising=False)
    calls: list[list[str]] = []

    def run(argv, **_kw):
        calls.append(list(argv))
        if "voice" in argv:
            open(argv[argv.index("--output") + 1], "wb").write(b"ID3fake")
            return subprocess.CompletedProcess(argv, 0, "", "")
        if "-af" in argv:
            open(argv[-1], "wb").write(b"ID3fx")
            return subprocess.CompletedProcess(argv, 0, "", "")
        return subprocess.CompletedProcess(argv, 0, "2.0\n", "")

    monkeypatch.setattr("navig_obs.show.say.subprocess.run", run)
    monkeypatch.setattr("navig_obs.show.say.shutil.which", lambda n: n)
    return SimpleNamespace(calls=calls)


def _stage(obs_factory, collection):
    return obs_factory(
        requests={**VENDOR_OK,
                  "GetSceneCollectionList": {"currentSceneCollectionName": collection,
                                             "sceneCollections": [collection]}}
    )


def test_the_russian_stage_speaks_russian_and_the_card_keeps_the_text(obs_factory, env):
    server = _stage(obs_factory, "SCHEMA BROADCAST (ru)")
    res = runner.invoke(obs_app, ["--port", str(server.port), "show", "say",
                                  "SCHEMA deployed.", "--tone", "deploy", "--json"])
    assert res.exit_code == 0, res.output
    synth = [c for c in env.calls if "voice" in c][0]
    assert synth[synth.index("--lang") + 1] == "ru"
    assert synth[synth.index("--voice") + 1] == "ru-RU-DmitryNeural"
    out = json.loads(res.output)
    assert out["lang"] == "ru" and out["stageLang"] == "ru"
    assert out["toast"]["title"] == "SCHEMA deployed."  # the screen is never rewritten


def test_lang_flag_beats_the_stage(obs_factory, env):
    server = _stage(obs_factory, "SCHEMA BROADCAST (ru)")
    res = runner.invoke(obs_app, ["--port", str(server.port), "show", "say", "hi",
                                  "--lang", "fr", "--json"])
    assert res.exit_code == 0, res.output
    assert json.loads(res.output)["lang"] == "fr"


def test_the_deploy_template_is_said_in_the_stage_language(obs_factory, env):
    server = _stage(obs_factory, "SCHEMA BROADCAST MOBILE (fr)")
    res = runner.invoke(obs_app, ["--port", str(server.port), "show", "say",
                                  "--template", "deploy", "--json"])
    assert res.exit_code == 0, res.output
    out = json.loads(res.output)
    assert out["text"] == "SCHEMA est déployé." and out["toast"]["tone"] == "deploy"


def test_a_voice_with_fx_is_processed_after_synthesis(obs_factory, env, monkeypatch):
    monkeypatch.setenv("NAVIG_OBS_SHOW_SAY_VOICES",
                       json.dumps({"en": {"provider": "edge", "voice": "x", "fx": "pitch=+2"}}))
    server = _stage(obs_factory, "SCHEMA BROADCAST (en)")
    res = runner.invoke(obs_app, ["--port", str(server.port), "show", "say", "hi"])
    assert res.exit_code == 0, res.output
    assert any("-af" in c for c in env.calls)


def test_say_without_text_explains_itself(env):
    res = runner.invoke(obs_app, ["show", "say", "--dry-run"])
    assert res.exit_code == 2
    assert "--template deploy" in res.output


@pytest.mark.parametrize("bad", [["--lang", "de"], ["--template", "nope"],
                                 ["--image", "http://x/a.png"]])
def test_bad_options_are_usage_errors(env, bad):
    res = runner.invoke(obs_app, ["show", "say", "x", "--dry-run", *bad])
    assert res.exit_code == 2


def test_the_card_carries_the_game_art(obs_factory, env):
    server = _stage(obs_factory, "SCHEMA BROADCAST (en)")
    res = runner.invoke(obs_app, ["--port", str(server.port), "show", "say",
                                  "Bad Cheese is free.", "--tone", "game",
                                  "--image", "https://cdn.example/bc.jpg"])
    assert res.exit_code == 0, res.output
    toast = [r for r in server.received if r[0] == "CallVendorRequest"][0][1]
    assert toast["requestData"]["event_data"]["image"] == "https://cdn.example/bc.jpg"


# ── voices: show / keep ──────────────────────────────────────────────────────
def test_voices_lists_the_voice_each_language_uses(env):
    res = runner.invoke(obs_app, ["show", "voices", "--json"])
    assert res.exit_code == 0, res.output
    rows = json.loads(res.output)["rows"]
    assert [r["lang"] for r in rows] == list(voices.LANGS)


def test_voices_use_writes_the_choice_through_navig_config(env, monkeypatch):
    seen = []

    def run(argv, **_kw):
        seen.append(argv)
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr("subprocess.run", run)
    monkeypatch.setattr("shutil.which", lambda n: n)
    res = runner.invoke(obs_app, ["show", "voices", "--use", "B-cartoon"])
    assert res.exit_code == 0, res.output
    argv = seen[-1]
    assert argv[1:4] == ["config", "set", "obs.show.say_voices"]
    stored = json.loads(argv[4])
    assert stored["ru"]["fx"] == "pitch=+3,tempo=1.08"


# ── flash on air ─────────────────────────────────────────────────────────────
class _Res:
    def __init__(self, body):
        self.status = 200
        self._b = json.dumps(body).encode()

    def read(self):
        return self._b

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_flash_needs_an_architect_session(env, monkeypatch):
    monkeypatch.delenv("NAVIG_SCHEMA_SESSION", raising=False)
    monkeypatch.setattr("navig_obs.commands.show._operator_session", lambda: None)
    res = runner.invoke(obs_app, ["show", "flash", "kazan"])
    assert res.exit_code == 2 and "schema_operator_session" in res.output


def test_flash_posts_the_zone_with_the_session(env, monkeypatch):
    monkeypatch.setenv("NAVIG_SCHEMA_SESSION", "tok")
    sent = {}

    def urlopen(req, timeout=0):
        sent["url"], sent["auth"] = req.full_url, req.get_header("Authorization")
        sent["body"] = json.loads(req.data)
        return _Res({"flash": {"id": "m1"}})

    monkeypatch.setattr("urllib.request.urlopen", urlopen)
    res = runner.invoke(obs_app, ["show", "flash", "kazan", "--api", "http://127.0.0.1:3001"])
    assert res.exit_code == 0, res.output
    assert sent == {"url": "http://127.0.0.1:3001/api/admin/flash/deal",
                    "auth": "Bearer tok", "body": {"slug": "kazan"}}


def test_flash_refusal_is_said_in_plain_words(env, monkeypatch):
    monkeypatch.setenv("NAVIG_SCHEMA_SESSION", "tok")

    def urlopen(req, timeout=0):
        raise urllib.error.HTTPError(req.full_url, 409, "Conflict", {},
                                     io.BytesIO(b'{"refused":"too-quiet"}'))

    monkeypatch.setattr("urllib.request.urlopen", urlopen)
    res = runner.invoke(obs_app, ["show", "flash", "kazan", "--api", "http://x"])
    assert res.exit_code == 1
    assert "too few active citizens" in res.output


def test_settings_defaults_are_untouched_by_a_bare_show_settings():
    s = ShowSettings()
    assert s.say_provider is None and s.say_lang == "en"


# ── fallbacks: one engine failing never leaves the card silent ───────────────
def test_every_chain_ends_on_the_free_voice_of_its_language():
    for lang in voices.LANGS:
        chain = voices.chain(voices.DEFAULTS[lang], lang)
        assert chain[-1] == voices.SAFETY[lang]
        assert chain[-1].provider == "edge"
    # Already the free voice: not listed twice.
    assert voices.chain(voices.SAFETY["fr"], "fr") == [voices.SAFETY["fr"]]


def test_mimo_is_not_shipped_for_french_or_russian():
    # Measured: MiMo slurs French and cannot speak Russian.
    assert voices.DEFAULTS["fr"].provider != "mimo"
    assert voices.DEFAULTS["ru"].provider != "mimo"


def test_config_fallbacks_are_read_in_order():
    t = voices.table({"ru": {"provider": "elevenlabs", "voice": "stan",
                             "fallback": [{"provider": "elevenlabs", "voice": "maxim"}]}})
    assert [v.label() for v in voices.chain(t["ru"], "ru")] == [
        "elevenlabs/stan", "elevenlabs/maxim", "edge/ru-RU-DmitryNeural"]


def test_a_one_line_provider_falls_back_to_the_languages_own_voice():
    v = voices.resolve("en", voices.table(), provider="openai")
    assert [x.label() for x in voices.chain(v, "en")] == [
        "openai/default", "mimo/Dean", "edge/en-US-GuyNeural"]


def test_a_failed_voice_falls_through_and_the_line_still_speaks(env, monkeypatch):
    s = ShowSettings(say_voices={"fr": {"provider": "elevenlabs", "voice": "guillaume"}})
    tried = []

    def run(argv, **_kw):
        if "voice" in argv:
            tried.append(argv[argv.index("--provider") + 1])
            if argv[argv.index("--provider") + 1] == "elevenlabs":
                return subprocess.CompletedProcess(argv, 1, "", "quota_exceeded")
            open(argv[argv.index("--output") + 1], "wb").write(b"ID3edge")
            return subprocess.CompletedProcess(argv, 0, "", "")
        return subprocess.CompletedProcess(argv, 0, "2.0\n", "")

    monkeypatch.setattr("navig_obs.show.say.subprocess.run", run)
    p = say_mod.prepare(say_mod.plan_for(s, "Bonjour la Grille", lang="fr"))
    assert tried == ["elevenlabs", "edge"]
    assert p.clip is not None and p.spoke == "edge/fr-FR-HenriNeural"
    assert any("quota_exceeded" in w for w in p.warnings)
    assert any("fallback edge/fr-FR-HenriNeural" in w for w in p.warnings)
    # Next time the first voice is tried again (its quota may be back).
    p2 = say_mod.plan_for(s, "Bonjour la Grille", lang="fr")
    assert p2.provider == "elevenlabs" and p2.cached is False


def test_when_every_voice_fails_the_card_shows_silently(env, monkeypatch):
    monkeypatch.setattr(
        "navig_obs.show.say.subprocess.run",
        lambda argv, **_kw: subprocess.CompletedProcess(argv, 1, "", "offline"),
    )
    p = say_mod.prepare(say_mod.plan_for(ShowSettings(), "hi", lang="en"))
    assert p.clip is None and p.spoke is None
    assert len([w for w in p.warnings if "offline" in w]) == 2  # mimo, then edge


# ── OBS busy loading a collection (207) ──────────────────────────────────────
class _BusyThen(dict):
    """Answers 207 'not ready' for the first `busy` vendor requests."""

    def __init__(self, busy: int, base: dict):
        super().__init__(base)
        self.busy = busy

    def get(self, key, default=None):
        if key == "CallVendorRequest" and self.busy > 0:
            self.busy -= 1
            return RuntimeError("OBS is not ready to perform the request.")
        return super().get(key, default)


def test_say_waits_out_a_collection_switch(obs_factory, env, monkeypatch):
    monkeypatch.setattr("navig_obs.commands.show.NOT_READY_WAIT_S", 0.01)
    server = obs_factory(requests=_BusyThen(2, VENDOR_OK))
    res = runner.invoke(obs_app, ["--port", str(server.port), "show", "say", "hi", "--lang", "en"])
    assert res.exit_code == 0, res.output
    assert [r[0] for r in server.received].count("TriggerMediaInputAction") == 1


def test_a_sample_run_survives_a_busy_obs(obs_factory, env, monkeypatch):
    monkeypatch.setattr("navig_obs.commands.show.NOT_READY_WAIT_S", 0.01)
    monkeypatch.setattr("time.sleep", lambda _s: None)
    server = obs_factory(requests={**VENDOR_OK, "CallVendorRequest": RuntimeError("not ready")})
    res = runner.invoke(obs_app, ["--port", str(server.port), "show", "voices", "--sample",
                                  "--play", "--direction", "B-cartoon", "--json"])
    assert res.exit_code == 0, res.output
    rows = json.loads(res.output)["rows"]
    assert len(rows) == 3  # every language still rendered
    assert all(any("not played" in w for w in r["warnings"]) for r in rows)


def test_the_eleven_sample_is_a_male_voice_not_the_providers_default():
    from navig_obs.commands import show

    d = show._sample_directions()["D-eleven"]
    assert {v.voice for v in d.values()} == {show.ELEVEN_PIGEON}


def test_set_keeps_one_language_with_its_free_fallback(env, monkeypatch):
    seen = []
    monkeypatch.setattr("subprocess.run",
                        lambda argv, **_kw: seen.append(argv) or subprocess.CompletedProcess(argv, 0, "", ""))
    monkeypatch.setattr("shutil.which", lambda n: n)
    res = runner.invoke(obs_app, ["show", "voices", "--set", "ru=elevenlabs:stan",
                                  "--set", "fr=elevenlabs:guillaume"])
    assert res.exit_code == 0, res.output
    stored = voices.table(seen[-1][4])
    assert stored["ru"].label() == "elevenlabs/stan"
    assert stored["fr"].label() == "elevenlabs/guillaume"
    assert stored["en"] == voices.DEFAULTS["en"]
    assert voices.chain(stored["ru"], "ru")[-1] == voices.SAFETY["ru"]
