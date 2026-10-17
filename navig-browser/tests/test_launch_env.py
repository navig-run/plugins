"""`launch_with_cdp` must not hand an Electron host's environment to the app it launches.

A shell opened inside VS Code (or any Electron app) carries ``ELECTRON_RUN_AS_NODE=1`` and a
set of ``VSCODE_*`` IPC variables. A ``Code.exe`` / ``Discord.exe`` started with
``ELECTRON_RUN_AS_NODE=1`` runs as plain Node: it rejects every editor flag
(``bad option: --user-data-dir=…``), the debug port never opens, and the launch times out with
no hint why. Measured 2026-10-16 launching an isolated VS Code from a VS Code terminal.

These stub ``subprocess.Popen`` and read the ``env`` it was given, so no app is started.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from navig_browser import targets as t

HOST_ENV = {
    "ELECTRON_RUN_AS_NODE": "1",
    "VSCODE_IPC_HOOK": "\\\\.\\pipe\\vscode-1234-main-sock",
    "VSCODE_PID": "1234",
    "VSCODE_NLS_CONFIG": "{}",
}


@pytest.fixture
def captured(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> dict:
    exe = tmp_path / "app.exe"
    exe.write_text("", encoding="utf-8")
    seen: dict = {"exe": str(exe)}

    def fake_popen(args, **kwargs):  # noqa: ANN001
        seen["args"] = args
        seen["env"] = kwargs.get("env")
        raise OSError("stop here: the env is all this test needs")

    monkeypatch.setattr(t.subprocess, "Popen", fake_popen)
    return seen


def _launch(captured: dict) -> None:
    assert t.launch_with_cdp(captured["exe"], 9555, user_data_dir=str(Path(captured["exe"]).parent / "udd")) is None


def test_electron_host_variables_are_not_inherited(monkeypatch: pytest.MonkeyPatch, captured: dict) -> None:
    for key, value in HOST_ENV.items():
        monkeypatch.setenv(key, value)
    _launch(captured)
    env = captured["env"]
    assert env is not None, "Popen must be given an explicit environment, not inherit the host's"
    leaked = sorted(k for k in env if k in HOST_ENV)
    assert not leaked, f"an Electron host's variables reached the launched app: {leaked}"


def test_unrelated_environment_is_preserved(monkeypatch: pytest.MonkeyPatch, captured: dict) -> None:
    monkeypatch.setenv("NAVIG_TEST_MARKER", "kept")
    monkeypatch.setenv("ELECTRON_RUN_AS_NODE", "1")
    _launch(captured)
    assert captured["env"]["NAVIG_TEST_MARKER"] == "kept"
    assert captured["env"].get("PATH") or captured["env"].get("Path"), "PATH must survive"


def test_vscode_portable_is_the_users_intent_and_survives(monkeypatch: pytest.MonkeyPatch, captured: dict) -> None:
    # VSCODE_PORTABLE is set on purpose to run a portable install - it is not host plumbing.
    monkeypatch.setenv("VSCODE_PORTABLE", "D:\\portable")
    monkeypatch.setenv("VSCODE_IPC_HOOK", HOST_ENV["VSCODE_IPC_HOOK"])
    _launch(captured)
    assert captured["env"].get("VSCODE_PORTABLE") == "D:\\portable"
    assert "VSCODE_IPC_HOOK" not in captured["env"]


def test_the_scrub_is_not_vacuous() -> None:
    # Anti-vacuity: the helper must actually remove something from a polluted environment.
    polluted = {**HOST_ENV, "KEEP": "1"}
    assert set(polluted) - set(t._launch_env(polluted)) == set(HOST_ENV)
