"""Casts — a real command's output, captured once, replayable at any size.

A reel's terminal shots must show what NAVIG actually printed. So the picture is never typed
by hand: :func:`capture` runs the real command at a phone-sized width and records every
chunk of output with the time it arrived, as an `asciicast v2`_ file. The Reel Stage
(:mod:`navig_pipeline.stage`) replays those bytes verbatim, so a re-render — new caption,
faster pacing, a zoom — never needs the device again.

**Width is decided at capture, not at render.** Rich lays tables out for the width it is
given (``COLUMNS``). Shrinking a 120-column capture onto a phone makes 14px text; capturing
at ~44 columns makes Rich produce a layout that was designed to be that narrow.

**Redaction happens here, before anything is written.** Device commands print serials, Wi-Fi
IPs, the operator's username and home path — none of which belongs in a public video. Known
values are replaced (connected adb serials are found automatically), and :func:`leaks` then
scans the result; :func:`capture` refuses to return a cast that still contains an address it
was not told is fine.

.. _asciicast v2: https://docs.asciinema.org/manual/asciicast/v2/
"""

from __future__ import annotations

import codecs
import getpass
import json
import os
import platform
import re
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

# A phone frame holds ~44 monospace columns at a legible size (see stage.css `.term`).
DEFAULT_COLS = 44
DEFAULT_ROWS = 40
# Chunks of a line arriving within this window are one event: output is replayed per line,
# which is how Rich writes, and it keeps a redaction target from straddling two events.
LINE_FLUSH_S = 0.25

_IPV4 = re.compile(r"(?<![\d.])(?:(?:25[0-5]|2[0-4]\d|1?\d?\d)\.){3}(?:25[0-5]|2[0-4]\d|1?\d?\d)(?![\d.])")
# A table column too narrow for the address truncates it — "83.228.…" — and the prefix
# still maps the network. Found the hard way: a 44-column `host list` passed a scan that
# only knew whole addresses.
_IPV4_PARTIAL = re.compile(r"(?<![\w.])(?:\d{1,3}\.){1,3}\d{0,3}\s*(?:…|\.\.\.)")
_MAC = re.compile(r"(?<![0-9A-Fa-f:])(?:[0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}(?![0-9A-Fa-f:])")
_ANSI = re.compile(r"\x1b\[[0-9;?]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")
# Addresses that identify nothing: loopback, unspecified, the adb wireless default gateway
# pattern is NOT here on purpose — a LAN address still maps a home network.
_HARMLESS_IPS = {"127.0.0.1", "0.0.0.0", "255.255.255.255"}
# A dotted quad right after a version word is a release number, not an address — Samsung's
# `Version 4.4.45.37` refused a capture for five minutes (the `--allow` escape worked, but
# every such device re-typed it). Context is the only signal: the shape alone can't tell
# `4.4.45.37` from an address, so a quad NOT preceded by these words is still refused.
_VERSION_CTX = re.compile(r"(?i)\b(?:version|ver|build|release)\s*[:#=]?\s*$")


class CastError(RuntimeError):
    """A cast could not be captured, parsed, or would leak an identifier."""


@dataclass
class Cast:
    """An asciicast v2 recording: a header and ``[seconds, "o", text]`` events."""

    header: dict
    events: list[tuple[float, str, str]] = field(default_factory=list)

    @property
    def width(self) -> int:
        return int(self.header.get("width") or DEFAULT_COLS)

    @property
    def command(self) -> str:
        return str(self.header.get("command") or "")

    def output(self) -> str:
        """Everything the command printed, concatenated."""
        return "".join(data for _t, kind, data in self.events if kind == "o")

    def plain(self) -> str:
        """:meth:`output` with escape sequences stripped — what a reader actually sees."""
        return _ANSI.sub("", self.output())


# ── format ──────────────────────────────────────────────────────────────────


def serialize(cast: Cast) -> str:
    """One JSON header line, then one JSON array per event (asciicast v2)."""
    header = {"version": 2, **{k: v for k, v in cast.header.items() if k != "version"}}
    lines = [json.dumps(header, ensure_ascii=False)]
    for t, kind, data in cast.events:
        lines.append(json.dumps([round(float(t), 6), kind, data], ensure_ascii=False))
    return "\n".join(lines) + "\n"


def parse(text: str) -> Cast:
    """Read asciicast v2 text. Raises :class:`CastError` on anything else."""
    rows = [ln for ln in text.splitlines() if ln.strip()]
    if not rows:
        raise CastError("empty cast")
    try:
        header = json.loads(rows[0])
    except json.JSONDecodeError as exc:
        raise CastError(f"cast header is not JSON: {exc}") from exc
    if not isinstance(header, dict) or header.get("version") != 2:
        raise CastError("not an asciicast v2 file (header needs \"version\": 2)")
    events: list[tuple[float, str, str]] = []
    for n, row in enumerate(rows[1:], start=2):
        try:
            t, kind, data = json.loads(row)
        except (json.JSONDecodeError, ValueError, TypeError) as exc:
            raise CastError(f"cast line {n} is not a [time, kind, data] event") from exc
        events.append((float(t), str(kind), str(data)))
    return Cast(header=header, events=events)


def load(path: Path) -> Cast:
    return parse(Path(path).read_text(encoding="utf-8"))


def save(cast: Cast, path: Path) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    # LF only, UTF-8 — a cast is data, never subject to the platform's text mode.
    path.write_bytes(serialize(cast).encode("utf-8"))
    return path


# ── redaction ───────────────────────────────────────────────────────────────


def default_redactions() -> dict[str, str]:
    """Identifiers of THIS machine that every capture should hide."""
    out: dict[str, str] = {}
    home = str(Path.home())
    if len(home) > 3:
        out[home] = "~"
        out[home.replace("\\", "/")] = "~"
    try:
        user = getpass.getuser()
    except Exception:  # noqa: BLE001 — no login name (service account): nothing to hide
        user = ""
    if user and len(user) >= 3:
        out[user] = "operator"
    host = platform.node()
    if host and len(host) >= 3:
        out[host] = "workstation"
    return out


def adb_redactions(runner: Callable[[list[str]], str] | None = None) -> dict[str, str]:
    """Serials (and wireless ``ip:port`` ids) of every connected adb device → neutral names.

    Device commands print the serial in their tables; asking the operator to remember to
    redact it is how one ships. Absent adb, or no devices, contributes nothing.
    """
    def _run(argv: list[str]) -> str:
        if not shutil.which(argv[0]):
            return ""
        try:
            return subprocess.run(argv, capture_output=True, encoding="utf-8",
                                  errors="replace", timeout=5).stdout
        except (OSError, subprocess.SubprocessError):
            return ""

    # encoding named, never text=True alone: adb's bytes decode with the codec we say here,
    # not with whatever code page the console happens to run (the repo-wide rule).
    text = (runner or _run)(["adb", "devices"])
    out: dict[str, str] = {}
    n = 0
    for line in text.splitlines()[1:]:
        parts = line.split()
        if len(parts) >= 2 and parts[1] in {"device", "unauthorized", "offline"}:
            n += 1
            serial = parts[0]
            out[serial] = f"device-{n:02d}"
            if ":" in serial:  # a wireless id also names the LAN address on its own
                out.setdefault(serial.rsplit(":", 1)[0], f"10.0.0.{n}")
    return out


def redact(text: str, mapping: dict[str, str]) -> str:
    """Replace every key with its value, longest key first (a path before its username).

    A table cell that is too narrow TRUNCATES instead of wrapping when the column is
    ``no_wrap`` — `2G0YC5ZH9G…` — which no full-string replace can match, and the prefix
    still identifies the device. Truncated forms of identifier keys (not paths: those are
    replaced whole) are folded to the placeholder too.
    """
    for key in sorted(mapping, key=len, reverse=True):
        if key:
            text = text.replace(key, mapping[key])
    for key, repl in mapping.items():
        if len(key) >= 8 and "/" not in key and "\\" not in key:
            prefixes = "|".join(re.escape(key[:k]) for k in range(8, len(key) + 1))
            text = re.sub(rf"(?:{prefixes})\s*(?:…|\.\.\.)", repl + "…", text)
    return text


def id_leaks(text: str, mapping: dict[str, str]) -> list[str]:
    """Known identifier keys still visible, whole or cut — the backstop after :func:`redact`.

    Whitespace-stripped, so a key FOLDED across lines counts as present. Paths are skipped:
    their 8-char prefix (``C:\\Users``) is far too generic to refuse on.
    """
    joined = re.sub(r"\s+", "", _ANSI.sub("", text))
    return [k for k in mapping
            if len(k) >= 8 and "/" not in k and "\\" not in k and re.sub(r"\s+", "", k)[:8] in joined]


def leaks(text: str, allow: set[str] | frozenset[str] = frozenset()) -> list[str]:
    """Identifiers still present in ``text``: IPv4 and MAC addresses not explicitly allowed."""
    plain = _ANSI.sub("", text)
    found = []
    for m in _IPV4.finditer(plain):
        ip = m.group(0)
        if ip in _HARMLESS_IPS or ip in allow or ip.startswith("10.0.0."):
            continue  # 10.0.0.x is what adb_redactions substitutes
        if _VERSION_CTX.search(plain[:m.start()]):
            continue  # `Version 4.4.45.37` — a release number the shape cannot tell apart
        found.append(ip)
    found.extend(m.group(0) for m in _MAC.finditer(plain) if m.group(0) not in allow)
    for m in _IPV4_PARTIAL.finditer(plain):
        stub = m.group(0)
        if not any(ip.startswith(stub.rstrip(" .…")) for ip in allow | _HARMLESS_IPS):
            found.append(stub)
    return sorted(set(found))


# ── capture ─────────────────────────────────────────────────────────────────


def capture_env(cols: int, rows: int, config_dir: Path | None = None) -> dict[str, str]:
    """The environment that makes a piped CLI render as if it were a phone-sized TTY."""
    env = dict(os.environ)
    if config_dir is not None:
        # Hosts, vault entries and history are the operator's real infrastructure; a public
        # capture points navig at a staged config instead. Names cannot be scanned for the
        # way addresses can, so this — not redaction — is what keeps them out.
        env["NAVIG_CONFIG_DIR"] = str(Path(config_dir).resolve())
    env.pop("NO_COLOR", None)
    env.update({
        "COLUMNS": str(cols), "LINES": str(rows),
        "FORCE_COLOR": "1", "CLICOLOR_FORCE": "1", "TERM": "xterm-256color",
        "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1",
        # A piped Python child block-buffers stdout, which would land every line at exit
        # and erase the real pacing the replay exists to keep.
        "PYTHONUNBUFFERED": "1",
    })
    # A capture has no keyboard: every backend makes a prompt read EOF instead of hanging
    # (/dev/null for pipe and POSIX pty, an injected end-of-input for ConPTY — see there).
    return env


def capture(
    argv: list[str],
    *,
    cols: int = DEFAULT_COLS,
    rows: int = DEFAULT_ROWS,
    redactions: dict[str, str] | None = None,
    allow: set[str] | None = None,
    auto_redact: bool = True,
    title: str | None = None,
    timeout: float | None = 600,
    cwd: Path | None = None,
    config_dir: Path | None = None,
) -> tuple[Cast, int]:
    """Run ``argv`` for real and record what it prints. Returns ``(cast, exit_code)``.

    stdout and stderr are merged, as on a terminal. Output is grouped per line
    (:data:`LINE_FLUSH_S`), redacted, then checked by :func:`leaks`; a leak raises
    :class:`CastError` and nothing is returned, so nothing can be saved.
    """
    if not argv:
        raise CastError("no command to capture")
    exe = shutil.which(argv[0]) or argv[0]
    mapping = {**(default_redactions() if auto_redact else {}),
               **(adb_redactions() if auto_redact else {}),
               **(redactions or {})}
    env = capture_env(cols, rows, config_dir)
    backend = pty_backend()
    if backend == "conpty":
        chunks, code = _run_conpty([exe, *argv[1:]], env, cols, timeout, cwd)
    elif backend == "pty":
        chunks, code = _run_posix_pty([exe, *argv[1:]], env, cols, timeout, cwd)
    else:
        chunks, code = _run_pipe([exe, *argv[1:]], env, timeout, cwd)

    events = [(t, "o", redact(text, mapping)) for t, text in coalesce(chunks)]
    cast = Cast(
        header={
            "width": cols, "height": rows, "timestamp": int(time.time()),
            "title": title or " ".join(argv),
            "command": redact(" ".join(argv), mapping),
            "env": {"TERM": "xterm-256color", "SHELL": "navig"},
            "navig": {"exit": code, "redactions": len(mapping), "backend": backend},
        },
        events=events,
    )
    found = leaks(cast.output() + cast.command, allow or set())
    found += [k for k in id_leaks(cast.output(), mapping) if k not in (allow or set())]
    if found:
        raise CastError(
            "refusing to keep a capture that still shows "
            + ", ".join(found)
            + " — redact each with --redact VALUE=REPLACEMENT, or --allow VALUE if it is public"
        )
    return cast, code


# ── backends ────────────────────────────────────────────────────────────────
#
# A pseudo-terminal, not a pipe, wherever one exists. On Windows a piped Rich console
# detects "legacy Windows" and colours through Win32 calls that never reach the pipe — the
# capture comes out grey — and there is no environment override for that detection. A
# ConPTY is a real console, so every program (Rich, adb, git) renders exactly as it would
# in Windows Terminal. ConPTY is given a tall screen so it never scrolls: a scrolling ConPTY
# repaints with absolute cursor moves, which replay fine but bloat the cast.
PTY_ROWS = 300
# How long output must be silent, and how often, before a prompt gets answered.
PROMPT_STALL_S = 2.5
MAX_INJECTS = 4
# The tail must END like a prompt: "[y/n] (n):" · "Proceed? [y/n]:" · "ok? " · "> ".
_PROMPT_TAIL = re.compile(r"(?:\[[a-z]/[a-z]\](?:\s*\([^)]*\))?|\?|:|>)\s*$", re.IGNORECASE)


def _watch_input(write: Callable[[], None], recent: list[str],
                 alive: Callable[[], bool], stop: threading.Event) -> None:
    """Answer a PROMPT — and only a prompt — when the program stops talking.

    A pseudo-console's stdin is the console itself: a confirmation prompt waits for a key
    until the capture times out (measured: a destructive-op gate hung a whole 600s), while
    a capture has no keyboard. Writing EOF up front was the other wrong answer — the
    console ECHOES it, so ``^Z`` landed in every cast, prompt or not. So: silent for
    :data:`PROMPT_STALL_S` AND the last output ends like a prompt → press Enter (the default
    answer, what a watching human would do). A merely slow command is never fed input,
    because its last line does not end in a prompt; if a prompt is never seen, the backend's
    timeout reports it.
    """
    last_len, changed, fired = -1, time.monotonic(), 0
    while not stop.is_set():
        time.sleep(0.25)
        if not alive():
            return
        tail = "".join(recent[-24:])
        if len(tail) != last_len:
            last_len, changed = len(tail), time.monotonic()
            continue
        if time.monotonic() - changed < PROMPT_STALL_S:
            continue
        changed = time.monotonic()
        if fired < MAX_INJECTS and _PROMPT_TAIL.search(_ANSI.sub("", tail)[-400:]):
            try:
                write()
            except Exception:  # noqa: BLE001 — the process ended between check and write
                return
            fired += 1


def pty_backend() -> str:
    """``conpty`` (Windows + pywinpty) · ``pty`` (POSIX) · ``pipe`` (colour may be lost)."""
    if os.name == "nt":
        try:
            import winpty  # noqa: F401
        except ImportError:
            return "pipe"
        return "conpty"
    try:
        import pty  # noqa: F401
    except ImportError:  # pragma: no cover - every POSIX Python ships it
        return "pipe"
    return "pty"


def _stamp(chunks: list[tuple[float, str]], start: float, text: str) -> None:
    if text:
        chunks.append((time.monotonic() - start, text))


def _run_conpty(argv: list[str], env: dict[str, str], cols: int, timeout: float | None,
                cwd: Path | None) -> tuple[list[tuple[float, str]], int]:
    from winpty import PtyProcess

    try:
        proc = PtyProcess.spawn(argv, dimensions=(PTY_ROWS, cols), env=env,
                                cwd=str(cwd) if cwd else None)
    except Exception as exc:  # noqa: BLE001 — winpty raises its own error types
        raise CastError(f"could not start {argv[0]!r} in a pseudo-console: {exc}") from exc
    chunks: list[tuple[float, str]] = []
    recent: list[str] = []
    start = time.monotonic()
    stop = threading.Event()

    def _pump() -> None:
        while True:
            try:
                text = proc.read(4096)
            except EOFError:
                stop.set()
                return
            recent.append(text)   # last ~24 chunks, for the prompt watchdog
            del recent[:-24]
            _stamp(chunks, start, text)

    def alive() -> bool:
        return bool(proc.isalive())

    reader = threading.Thread(target=_pump, daemon=True)
    reader.start()
    watchdog = threading.Thread(
        target=_watch_input, args=(lambda: proc.write("\r\n"), recent, alive, stop),
        daemon=True,
    )
    watchdog.start()
    reader.join(timeout)
    stop.set()
    if reader.is_alive():
        proc.terminate(force=True)
        raise CastError(f"{argv[0]} did not finish within {timeout:.0f}s")
    code = proc.exitstatus
    if code is None:
        proc.wait()
        code = proc.exitstatus
    return chunks, int(code or 0)


def _run_posix_pty(argv: list[str], env: dict[str, str], cols: int, timeout: float | None,
                   cwd: Path | None) -> tuple[list[tuple[float, str]], int]:  # pragma: no cover - POSIX
    import fcntl
    import pty
    import struct
    import termios

    master, slave = pty.openpty()
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", PTY_ROWS, cols, 0, 0))
    try:
        proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=slave, stderr=slave,
                                env=env, cwd=str(cwd) if cwd else None, close_fds=True)
    except OSError as exc:
        os.close(master)
        os.close(slave)
        raise CastError(f"could not start {argv[0]!r}: {exc}") from exc
    os.close(slave)
    chunks: list[tuple[float, str]] = []
    start = time.monotonic()
    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")

    def _pump() -> None:
        while True:
            try:
                blob = os.read(master, 4096)
            except OSError:  # EIO: the child closed its side
                break
            if not blob:
                break
            _stamp(chunks, start, decoder.decode(blob))
        _stamp(chunks, start, decoder.decode(b"", final=True))

    reader = threading.Thread(target=_pump, daemon=True)
    reader.start()
    try:
        code = proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        proc.kill()
        raise CastError(f"{argv[0]} did not finish within {timeout:.0f}s") from exc
    finally:
        reader.join(timeout=5)
        os.close(master)
    return chunks, code


def _run_pipe(argv: list[str], env: dict[str, str], timeout: float | None,
              cwd: Path | None) -> tuple[list[tuple[float, str]], int]:
    try:
        proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                stdin=subprocess.DEVNULL, env=env, cwd=str(cwd) if cwd else None)
    except OSError as exc:
        raise CastError(f"could not start {argv[0]!r}: {exc}") from exc
    chunks: list[tuple[float, str]] = []
    start = time.monotonic()
    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")

    def _pump() -> None:
        assert proc.stdout is not None
        while True:
            blob = proc.stdout.read1(4096) if hasattr(proc.stdout, "read1") else proc.stdout.read(4096)
            if not blob:
                break
            _stamp(chunks, start, decoder.decode(blob))
        _stamp(chunks, start, decoder.decode(b"", final=True))

    reader = threading.Thread(target=_pump, daemon=True)
    reader.start()
    try:
        code = proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        proc.kill()
        raise CastError(f"{argv[0]} did not finish within {timeout:.0f}s") from exc
    reader.join(timeout=5)
    return chunks, code


def coalesce(chunks: list[tuple[float, str]], window: float = LINE_FLUSH_S) -> list[tuple[float, str]]:
    """Merge chunks into whole lines; a partial line flushes after ``window`` of silence.

    The merged event keeps the time its LAST piece arrived — the moment the line was
    complete on screen.
    """
    out: list[tuple[float, str]] = []
    buf, last = "", 0.0
    for t, text in chunks:
        if buf and t - last > window:
            out.append((last, buf))
            buf = ""
        buf += text
        last = t
        if "\n" in buf:
            head, _, rest = buf.rpartition("\n")
            out.append((t, head + "\n"))
            buf = rest
    if buf:
        out.append((last, buf))
    return out
