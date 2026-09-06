"""The NobiCast publish contract.

Nothing here uploads. These tests pin the shape of ``metadata.json`` and the R2 key
scheme so the upload can be added later without redesigning what a render writes.
"""

from __future__ import annotations

import json

import pytest

from navig_audio.podcast import publish


def _rendered(tmp_path, *, episode=0, size=1024):
    """A minimal rendered-episode directory, as `render` would leave it."""
    root = tmp_path / "ep000-test"
    for lang in ("fr", "en"):
        (root / lang).mkdir(parents=True)
        (root / lang / "01-intro.mp3").write_bytes(b"x" * size)
        (root / lang / "02-outro.mp3").write_bytes(b"x" * size)

    (root / "metadata.json").write_text(
        json.dumps(
            {
                "episode": episode,
                "slug": "test",
                "title": "Test episode",
                "languages": {
                    lang: {
                        "lang": lang,
                        "tracks": [
                            {"number": 1, "title": "Intro", "file": "01-intro.mp3",
                             "duration_s": 12.5, "bytes": size},
                            {"number": 2, "title": "Outro", "file": "02-outro.mp3",
                             "duration_s": 8.0, "bytes": size},
                        ],
                    }
                    for lang in ("fr", "en")
                },
            }
        ),
        encoding="utf-8",
    )
    return root


def test_plan_lists_every_track_in_every_language(tmp_path) -> None:
    plan = publish.plan_upload(_rendered(tmp_path))
    assert plan.languages == ["en", "fr"]
    assert len(plan.uploads) == 4
    assert plan.episode == 0


def test_r2_keys_do_not_collide_across_tracks_or_languages(tmp_path) -> None:
    # The current NobiCast scheme (audio/ep{number}.mp3) would overwrite three of these
    # four files; keeping them distinct is the whole point of the new scheme.
    plan = publish.plan_upload(_rendered(tmp_path))
    keys = [u.r2_key for u in plan.uploads]
    assert len(set(keys)) == len(keys)
    assert "audio/ep0/fr/01-intro.mp3" in keys
    assert "audio/ep0/en/01-intro.mp3" in keys


def test_language_filter_narrows_the_plan(tmp_path) -> None:
    plan = publish.plan_upload(_rendered(tmp_path), languages=["fr"])
    assert plan.languages == ["fr"]
    assert all(u.lang == "fr" for u in plan.uploads)


def test_oversized_track_is_flagged_not_silently_accepted(tmp_path) -> None:
    # nobicast-web rejects audio over 50 MB; discovering that mid-upload is worse.
    plan = publish.plan_upload(_rendered(tmp_path, size=publish.MAX_UPLOAD_BYTES + 1))
    assert any(u.too_large for u in plan.uploads)
    assert any("50 MB" in w for w in plan.warnings)


def test_missing_episode_number_is_warned_about(tmp_path) -> None:
    root = _rendered(tmp_path)
    meta = json.loads((root / "metadata.json").read_text(encoding="utf-8"))
    del meta["episode"]
    (root / "metadata.json").write_text(json.dumps(meta), encoding="utf-8")

    plan = publish.plan_upload(root)
    assert plan.episode is None
    assert any("episode:" in w for w in plan.warnings)


def test_missing_rendered_file_is_reported(tmp_path) -> None:
    root = _rendered(tmp_path)
    (root / "fr" / "01-intro.mp3").unlink()
    plan = publish.plan_upload(root)
    assert any("missing rendered file" in w for w in plan.warnings)
    assert len(plan.uploads) == 3


def test_no_metadata_points_at_the_render_command(tmp_path) -> None:
    (tmp_path / "empty").mkdir()
    with pytest.raises(publish.PublishError, match="render the episode first"):
        publish.plan_upload(tmp_path / "empty")


def test_blockers_are_stated(tmp_path) -> None:
    listed = publish.blockers()
    assert any("episode_assets" in b for b in listed)
    assert any("lang column" in b for b in listed)
