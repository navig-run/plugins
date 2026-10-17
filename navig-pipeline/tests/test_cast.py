"""Casts — real output, captured once, never leaking the operator's identifiers.

The format tests pin round-trip stability (a cast re-saved must be byte-identical). The
leak tests pin the refusal that makes a capture safe to publish, including the shape that
slipped past the first version: a table column too narrow for an address truncates it to
``83.228.…``, and the prefix still maps the network.
"""

from __future__ import annotations

import sys

import pytest

from navig_pipeline import cast as C


def _cast() -> C.Cast:
    return C.Cast(
        header={"width": 44, "height": 40, "command": "navig devnet ssl navig.run"},
        events=[(0.5, "o", "\x1b[32m✓\x1b[0m host  navig.run\r\n"), (0.75, "o", "  issuer ünïcode\r\n")],
    )


# ── format ──────────────────────────────────────────────────────────────────


def test_round_trip_is_stable() -> None:
    text = C.serialize(_cast())
    again = C.serialize(C.parse(text))
    assert again == text
    assert C.parse(text).events == _cast().events


def test_header_is_asciicast_v2() -> None:
    first = C.serialize(_cast()).splitlines()[0]
    assert '"version": 2' in first


def test_save_writes_lf_utf8(tmp_path) -> None:
    path = C.save(_cast(), tmp_path / "x.cast")
    raw = path.read_bytes()
    assert b"\r\n" not in raw.replace(b"\\r\\n", b"")  # escaped CRLF inside JSON is data
    assert C.load(path).events == _cast().events


@pytest.mark.parametrize("bad", ["", "not json\n", '{"version": 1}\n', '{"version": 2}\n[1, "o"]\n'])
def test_parse_rejects_what_it_cannot_replay(bad: str) -> None:
    with pytest.raises(C.CastError):
        C.parse(bad)


def test_plain_strips_escapes() -> None:
    assert C.parse(C.serialize(_cast())).plain() == "✓ host  navig.run\r\n  issuer ünïcode\r\n"


# ── grouping ────────────────────────────────────────────────────────────────


def test_coalesce_merges_chunks_into_lines() -> None:
    out = C.coalesce([(0.1, "ab"), (0.15, "c\nde"), (0.2, "f\n")])
    assert out == [(0.15, "abc\n"), (0.2, "def\n")]


def test_coalesce_flushes_a_partial_line_after_silence() -> None:
    out = C.coalesce([(0.1, "Downloading"), (2.0, " done\n")])
    assert out == [(0.1, "Downloading"), (2.0, " done\n")]


# ── redaction + leaks ───────────────────────────────────────────────────────


def test_redact_replaces_longest_key_first() -> None:
    text = r"C:\Users\alice\navig and alice"
    out = C.redact(text, {"alice": "operator", r"C:\Users\alice": "~"})
    assert out == r"~\navig and operator"


@pytest.mark.parametrize("text, leaked", [
    ("wifi 192.168.1.100 up", ["192.168.1.100"]),
    ("Host  83.228.…  ubuntu", ["83.228.…"]),          # truncated by a narrow column
    ("Host  192.168…  operator", ["192.168…"]),
    ("mac aa:bb:cc:dd:ee:ff", ["aa:bb:cc:dd:ee:ff"]),
])
def test_leaks_finds_addresses_whole_and_truncated(text: str, leaked: list[str]) -> None:
    assert C.leaks(text) == leaked


@pytest.mark.parametrize("text", [
    "loopback 127.0.0.1", "navig 3.25.0 · python 3.13", "device-01 at 10.0.0.1", "\x1b[32m✓\x1b[0m ok",
])
def test_leaks_ignores_what_identifies_nothing(text: str) -> None:
    assert C.leaks(text) == []


def test_allow_lets_a_public_address_through() -> None:
    assert C.leaks("dns 1.1.1.1", allow={"1.1.1.1"}) == []


def test_adb_serials_are_found_and_named() -> None:
    listing = "List of devices attached\nR58N91ABCDE\tdevice\n192.168.1.50:5555\tdevice\n\n"
    out = C.adb_redactions(runner=lambda _argv: listing)
    assert out["R58N91ABCDE"] == "device-01"
    assert out["192.168.1.50:5555"] == "device-02"
    assert out["192.168.1.50"] == "10.0.0.2"  # the bare LAN address goes too


def test_capture_env_points_navig_at_a_staged_config(tmp_path) -> None:
    env = C.capture_env(44, 40, config_dir=tmp_path)
    assert env["NAVIG_CONFIG_DIR"] == str(tmp_path.resolve())
    assert env["COLUMNS"] == "44" and env["PYTHONUNBUFFERED"] == "1"
    assert "NO_COLOR" not in env


# ── capture (real subprocesses) ─────────────────────────────────────────────


def test_capture_records_real_output_and_exit_code() -> None:
    cast, code = C.capture(
        [sys.executable, "-c", "import sys; print('hello from a real process'); sys.exit(3)"],
        auto_redact=False,
    )
    assert code == 3
    assert "hello from a real process" in cast.plain()
    assert cast.header["navig"]["exit"] == 3


def test_capture_refuses_output_that_still_shows_an_address() -> None:
    with pytest.raises(C.CastError, match="192.168.1.50"):
        C.capture([sys.executable, "-c", "print('peer 192.168.1.50')"], auto_redact=False)


def test_capture_applies_redactions_before_the_scan() -> None:
    cast, _ = C.capture(
        [sys.executable, "-c", "print('peer 192.168.1.50 serial ZX1')"],
        auto_redact=False, redactions={"192.168.1.50": "10.0.0.10", "ZX1": "device-01"},
    )
    assert "10.0.0.10" in cast.plain() and "device-01" in cast.plain()
    assert "ZX1" not in cast.output()


@pytest.mark.skipif(C.pty_backend() == "pipe", reason="needs a pseudo-terminal backend")
def test_colour_survives_through_the_pseudo_terminal() -> None:
    # Rich decides colour from the console it sees. Through a pipe on Windows it colours via
    # Win32 calls the capture never receives; through a PTY it emits SGR codes.
    code_ = "from rich.console import Console; Console().print('[green]ok[/green]')"
    cast, _ = C.capture([sys.executable, "-c", code_], auto_redact=False)
    assert "\x1b[" in cast.output() and "ok" in cast.plain()


def test_a_prompt_reads_eof_instead_of_hanging_the_capture() -> None:
    # Under ConPTY stdin is the console, not /dev/null: a confirmation prompt once hung a
    # capture for its whole timeout. A capture has no keyboard — nobody answers.
    # the marker is assembled at runtime: a traceback echoes the SOURCE line, which must not
    # count as the code after the prompt having run
    code_ = "import typer; typer.confirm('Proceed?', default=False, abort=True); print('R' + 'AN-PAST-PROMPT')"
    cast, code = C.capture([sys.executable, "-c", code_], auto_redact=False, timeout=60)
    assert code != 0
    assert "Proceed?" in cast.plain() and "RAN-PAST-PROMPT" not in cast.plain()


# ── truncated / folded identifiers ──────────────────────────────────────────


def test_a_truncated_serial_is_redacted_and_caught() -> None:
    # A no_wrap table cell CUT the 13-char serial to `2G0YC5ZH9G…`; the full-string replace
    # missed it and the scan passed it — the device shipped in a "clean" cast.
    raw = "Device Quest 3 (2G0YC5ZH9G…)\n"
    mapping = {"2G0YC5ZH9G0609": "device-01"}
    assert C.id_leaks(raw, mapping) == ["2G0YC5ZH9G0609"]
    clean = C.redact(raw, mapping)
    assert "2G0YC5" not in clean and "device-01…" in clean
    assert C.id_leaks(clean, mapping) == []


def test_a_key_folded_across_lines_is_still_a_leak() -> None:
    # `overflow="fold"` wraps a long key mid-token: no single-line substring matches, so the
    # check strips whitespace first.
    folded = "handle\n  epic:cb733f3432db46bc\n  a94965b0"
    assert C.id_leaks(folded, {"epic:cb733f3432db46bca94965b0": "game-01"}) == [
        "epic:cb733f3432db46bca94965b0"]


def test_path_prefixes_are_not_treated_as_identifier_leaks() -> None:
    # `C:\Users` is far too generic to refuse on; paths are replaced whole, not by prefix.
    other_user = r"C:\Users\someoneelse\notes.txt"
    assert C.id_leaks(other_user, {r"C:\Users\alice": "~"}) == []


def test_a_prompt_is_answered_without_ejecting_a_control_glyph() -> None:
    # The old fix wrote EOF at spawn; the console echoed it, so `^Z` opened every cast.
    code_ = "print('Sure? [y/n]:', flush=True); import sys; sys.exit(7)"
    # ends like a prompt while the program is actually alive → Enter lands (harmless here),
    # and nothing injects before output exists.
    cast, code = C.capture([sys.executable, "-c", code_], auto_redact=False, timeout=60)
    assert "^Z" not in cast.output()
    assert code == 7


def test_a_version_number_is_not_refused_as_an_address() -> None:
    # Samsung's `Version 4.4.45.37` is a release number: the shape alone cannot tell it
    # apart from an address, so context is the classifier (and without that context the
    # same quad is still refused below).
    assert C.leaks("Version 4.4.45.37") == []
    assert C.leaks("  Build  8.8.4.4") == []
    assert C.leaks("x11 release: 1.2.3.4") == []


def test_an_address_without_version_context_is_still_refused() -> None:
    # The classifier reads the word in front, never the shape: drop the keyword and the
    # same quad is a leak again.
    assert C.leaks("4.4.45.37") == ["4.4.45.37"]
    assert C.leaks("host 4.4.45.37") == ["4.4.45.37"]
