"""The seal was decorative: it reported a state it did not enforce.

`seal.py` documents "A sealed bundle cannot be appended to ... recorders check
:func:`is_sealed`", and `navig blackbox seal` prints "recording is now frozen
until `unseal`". Nothing checked it. `is_sealed` had exactly two consumers — a
status line and a test asserting the marker file round-trips — so an
investigator who sealed the state to preserve it watched the daemon keep
appending to the evidence, with `navig blackbox status` reporting `sealed yes`
the whole time.

Also covered: `export_bundle` folded "remove the plaintext ZIP" into the
encryption `try`, so a failed delete (an antivirus or indexer holding the file
is enough on Windows) reported "Encryption failed" and returned the PLAINTEXT
path while the .enc file sat there unmentioned.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

import navig_blackbox.recorder as recorder_mod
from navig_blackbox.bundle import create_bundle
from navig_blackbox.export import export_bundle
from navig_blackbox.recorder import BlackboxRecorder
from navig_blackbox.seal import is_sealed, seal_bundle, unseal
from navig_blackbox.types import EventType


@pytest.fixture(autouse=True)
def _isolate_singleton():
    original = recorder_mod._recorder
    recorder_mod._recorder = None
    yield
    recorder_mod._recorder = original


def _seal(d: Path) -> None:
    seal_bundle(create_bundle(since_hours=24, blackbox_dir=d), blackbox_dir=d)


# ── the seal must actually freeze the stream ─────────────────────────────────


def test_sealed_recorder_refuses_to_append(tmp_path):
    rec = BlackboxRecorder(tmp_path)
    rec.record(EventType.COMMAND, {"command": "before the incident"})
    _seal(tmp_path)
    assert is_sealed(tmp_path)

    result = rec.record(EventType.COMMAND, {"command": "after sealing"})

    assert result is None, "a sealed blackbox accepted an append"
    assert rec.event_count() == 1
    assert rec.tail(1)[0].payload["command"] == "before the incident"


def test_the_sealed_evidence_does_not_change(tmp_path):
    """What sealing is FOR: the bytes on disk stay put."""
    rec = BlackboxRecorder(tmp_path)
    for i in range(5):
        rec.record(EventType.COMMAND, {"command": f"cmd-{i}"})
    _seal(tmp_path)

    frozen = rec._events_path.read_bytes()
    for i in range(20):
        rec.record(EventType.ERROR, {"message": f"noise-{i}"})

    assert rec._events_path.read_bytes() == frozen


def test_unseal_lets_recording_resume(tmp_path):
    """Anti-vacuity: the seal must be a gate, not an off switch."""
    rec = BlackboxRecorder(tmp_path)
    _seal(tmp_path)
    assert rec.record(EventType.COMMAND, {"command": "blocked"}) is None

    assert unseal(tmp_path) is True
    result = rec.record(EventType.COMMAND, {"command": "allowed again"})

    assert result is not None
    assert rec.tail(1)[0].payload["command"] == "allowed again"


def test_recording_works_normally_when_not_sealed(tmp_path):
    """Anti-vacuity: 'always return None' would pass every test above."""
    rec = BlackboxRecorder(tmp_path)
    assert rec.record(EventType.COMMAND, {"command": "ordinary"}) is not None
    assert rec.event_count() == 1


def test_seal_is_scoped_to_its_own_directory(tmp_path):
    """A seal in one blackbox must not freeze another."""
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    b.mkdir()
    ra, rb = BlackboxRecorder(a), BlackboxRecorder(b)
    _seal(a)

    assert ra.record(EventType.COMMAND, {"command": "x"}) is None
    assert rb.record(EventType.COMMAND, {"command": "y"}) is not None


def test_recorder_reports_its_own_seal_state(tmp_path):
    rec = BlackboxRecorder(tmp_path)
    assert rec.is_sealed() is False
    _seal(tmp_path)
    assert rec.is_sealed() is True


# ── the CLI must not print a receipt for an event it dropped ─────────────────


def test_cli_record_does_not_claim_success_on_a_sealed_blackbox(tmp_path, monkeypatch):
    import typer

    from navig_blackbox.commands import blackbox as cmds

    monkeypatch.setattr("navig_blackbox._compat.blackbox_dir", lambda: tmp_path)
    recorder_mod._recorder = BlackboxRecorder(tmp_path)
    _seal(tmp_path)

    with pytest.raises(typer.Exit) as exc:
        cmds.record("command", "should not be recorded", source="cli")

    assert exc.value.exit_code == 1, "a dropped event must not exit 0"
    assert BlackboxRecorder(tmp_path).event_count() == 0


def test_cli_record_still_succeeds_when_unsealed(tmp_path, monkeypatch):
    """Anti-vacuity: the honest path must stay a success."""
    from navig_blackbox.commands import blackbox as cmds

    monkeypatch.setattr("navig_blackbox._compat.blackbox_dir", lambda: tmp_path)
    recorder_mod._recorder = BlackboxRecorder(tmp_path)

    cmds.record("command", "recorded fine", source="cli")  # must not raise

    assert BlackboxRecorder(tmp_path).event_count() == 1


# ── export: a successful encryption must not be reported as a failure ────────


def _bundle(d: Path):
    BlackboxRecorder(d).record(EventType.COMMAND, {"command": "secret-ish"})
    return create_bundle(since_hours=24, blackbox_dir=d)


def test_encryption_success_survives_a_failed_plaintext_cleanup(tmp_path, capsys):
    bundle = _bundle(tmp_path)
    out = tmp_path / "incident.navbox"

    real_unlink = Path.unlink

    def unlink_fails_for_the_plaintext(self, *a, **kw):
        if self.name == "incident.navbox":
            raise OSError(13, "Permission denied")
        return real_unlink(self, *a, **kw)

    with (
        patch("navig.vault.core.get_vault"),
        patch("navig.vault.crypto.CryptoEngine.seal", return_value=b"ciphertext"),
        patch.object(Path, "unlink", unlink_fails_for_the_plaintext),
    ):
        result = export_bundle(bundle, out, encrypted=True)

    assert result.suffix == ".enc", "encryption succeeded but a plaintext path was returned"
    assert result.read_bytes() == b"ciphertext"

    printed = capsys.readouterr().out
    assert "Encryption failed" not in printed, "a successful encryption was reported as failed"
    assert "not encrypted" in printed, "the leftover plaintext must be called out"


def test_encryption_failure_still_falls_back_to_plaintext(tmp_path, capsys):
    """Anti-vacuity: the documented fallback (`--encrypt ... falls back to
    plaintext`) must keep working."""
    bundle = _bundle(tmp_path)
    out = tmp_path / "incident.navbox"

    with patch("navig.vault.core.get_vault", side_effect=RuntimeError("no vault")):
        result = export_bundle(bundle, out, encrypted=True)

    assert result.suffix == ".navbox"
    assert result.exists()
    assert "Encryption failed" in capsys.readouterr().out


def test_unencrypted_export_is_untouched(tmp_path):
    bundle = _bundle(tmp_path)
    result = export_bundle(bundle, tmp_path / "plain.navbox", encrypted=False)
    assert result.suffix == ".navbox"
    assert result.exists()
