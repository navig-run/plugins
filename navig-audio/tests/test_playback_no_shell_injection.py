"""Windows playback never turns a file name into PowerShell source.

The path used to be pasted inside '…' in a shell command string. Verified 2026-10-16: a
file legally named ``a'); New-Item -ItemType File -Path pwned.txt -Force; #.mp3`` made the
old command create pwned.txt. The script is now fixed and the path arrives in an
environment variable.
"""

from __future__ import annotations

import asyncio
import shutil
import subprocess
import sys

import pytest

from navig_audio.voice import playback


# Closes the Open([uri]'…') call, runs New-Item (relative to the cwd), comments the rest out.
EVIL = "a'); New-Item -ItemType File -Path pwned.txt -Force; #.mp3"


def test_the_script_is_fixed_and_the_path_only_travels_in_env(tmp_path, monkeypatch) -> None:
    evil = tmp_path / EVIL
    seen: dict = {}

    async def fake_exec(*argv, env=None, **_k):
        seen["argv"], seen["env"] = list(argv), env

        class _P:
            returncode = 0

            async def wait(self):
                return 0

        return _P()

    monkeypatch.setattr(playback.asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr(playback, "_wait_or_kill", lambda proc, _t: asyncio.sleep(0, result=0))
    assert asyncio.run(playback._play_windows(evil)) is True
    assert all(str(evil) not in a and "New-Item" not in a for a in seen["argv"]), seen["argv"]
    assert seen["argv"][-1] == playback._PS_PLAY_SCRIPT
    assert seen["env"]["NAVIG_PLAY_PATH"] == str(evil.resolve())


def test_no_shell_is_ever_spawned(tmp_path, monkeypatch) -> None:
    def boom(*_a, **_k):
        raise AssertionError("create_subprocess_shell must not be used for playback")

    monkeypatch.setattr(playback.asyncio, "create_subprocess_shell", boom)
    monkeypatch.setattr(playback.asyncio, "create_subprocess_exec",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("no powershell here")))
    assert asyncio.run(playback._play_windows(tmp_path / "x.mp3")) is False


@pytest.mark.skipif(sys.platform != "win32" or not shutil.which("powershell"),
                    reason="real PowerShell run is Windows-only")
def test_real_powershell_does_not_execute_a_hostile_file_name(tmp_path) -> None:
    evil = tmp_path / EVIL
    evil.write_bytes(b"")  # a legal Windows file name
    subprocess.run(playback._powershell_play_argv(), env=playback._powershell_play_env(evil),
                   capture_output=True, timeout=60, check=False, cwd=tmp_path)
    assert not (tmp_path / "pwned.txt").exists(), "the file name was executed as PowerShell"
