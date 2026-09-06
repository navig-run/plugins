"""The standalone CLI: exit codes, stdout discipline, and no secret on argv.

Each test drives ``main(argv)`` in-process against a vault isolated by NAVIG_CONFIG_DIR.
That isolation matters more than usual here: ``get_vault()`` runs ``_auto_migrate`` on first
use, so a test pointed at the real config dir would COPY the developer's actual legacy
credentials into its scratch vault. Observed while smoke-testing this CLI -- six real
credentials were migrated into a temp directory. Pointing NAVIG_CONFIG_DIR at tmp_path makes
the legacy source path resolve inside tmp_path too, where nothing exists.
"""

from __future__ import annotations

import json

import pytest

from navig_vault.cli import EXIT_NOT_FOUND, EXIT_OK, EXIT_USAGE, build_parser, main


@pytest.fixture
def vault_env(tmp_path, monkeypatch):
    """An empty vault with nothing to migrate into it."""
    monkeypatch.setenv("NAVIG_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.delenv("NAVIG_VAULT_DIR", raising=False)
    import navig_vault.core as core

    monkeypatch.setattr(core, "_vault", None, raising=False)
    yield tmp_path
    monkeypatch.setattr(core, "_vault", None, raising=False)


# ── the property that shapes the whole design ────────────────────────────────


def test_a_secret_cannot_be_passed_on_argv() -> None:
    """There must be no flag that puts a secret in shell history and in `ps` output."""
    parser = build_parser()
    add = next(
        a for a in parser._subparsers._group_actions[0].choices.items() if a[0] == "add"
    )[1]
    flags = {s for action in add._actions for s in action.option_strings}
    for forbidden in ("--value", "--secret", "--key", "--password", "--token"):
        assert forbidden not in flags, (
            f"`add` accepts {forbidden} -- command-line arguments are visible in shell "
            f"history and to every process on the machine. Use --from-env/--from-file/--stdin."
        )
    assert {"--from-env", "--from-file", "--stdin"} <= flags


def test_add_rejects_a_value_argument_at_the_parser(capsys) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["add", "openai", "--value", "hunter2"])
    assert exc.value.code == EXIT_USAGE
    assert "unrecognized arguments" in capsys.readouterr().err


# ── round trip ───────────────────────────────────────────────────────────────


def test_add_then_get_round_trips(vault_env, monkeypatch, capsys) -> None:
    monkeypatch.setenv("PROBE_SECRET", "sk-round-trip")
    assert main(["add", "openai", "--from-env", "PROBE_SECRET"]) == EXIT_OK
    capsys.readouterr()
    assert main(["get", "openai"]) == EXIT_OK
    assert capsys.readouterr().out == "sk-round-trip"


def test_get_writes_only_the_secret_to_stdout(vault_env, monkeypatch, capsys) -> None:
    """`$(nv get x)` and `nv get x | ...` must not pick up chatter."""
    monkeypatch.setenv("PROBE_SECRET", "value-only")
    main(["add", "openai", "--from-env", "PROBE_SECRET"])
    capsys.readouterr()
    main(["get", "openai"])
    out = capsys.readouterr()
    assert out.out == "value-only", "stdout carried something besides the secret"
    assert "value-only" not in out.err, "the secret leaked to stderr as well"


def test_add_reports_progress_on_stderr_not_stdout(vault_env, monkeypatch, capsys) -> None:
    monkeypatch.setenv("PROBE_SECRET", "x")
    main(["add", "openai", "--from-env", "PROBE_SECRET"])
    out = capsys.readouterr()
    assert out.out == "", "add wrote to stdout; that stream belongs to values only"
    assert "stored openai" in out.err


def test_from_file_reads_the_value(vault_env, tmp_path, capsys) -> None:
    f = tmp_path / "secret.txt"
    f.write_text("from-a-file\n", encoding="utf-8")
    assert main(["add", "openai", "--from-file", str(f)]) == EXIT_OK
    capsys.readouterr()
    main(["get", "openai"])
    assert capsys.readouterr().out == "from-a-file", "trailing newline was not stripped"


# ── exit codes: a caller chaining on && must not be misled ────────────────────


def test_missing_credential_is_not_success(vault_env, capsys) -> None:
    assert main(["get", "nothing-here"]) == EXIT_NOT_FOUND
    assert capsys.readouterr().out == "", "a miss must not print anything to stdout"


def test_delete_of_a_missing_credential_is_not_success(vault_env) -> None:
    assert main(["delete", "nothing-here", "--yes"]) == EXIT_NOT_FOUND


def test_delete_refuses_without_yes_when_not_a_tty(vault_env, monkeypatch, capsys) -> None:
    """Non-interactive deletion must be explicit, not assumed."""
    monkeypatch.setattr("sys.stdin.isatty", lambda: False, raising=False)
    monkeypatch.setenv("PROBE_SECRET", "x")
    main(["add", "openai", "--from-env", "PROBE_SECRET"])
    assert main(["delete", "openai"]) == EXIT_USAGE
    assert main(["get", "openai"]) == EXIT_OK, "the credential was deleted despite the refusal"


def test_add_with_an_empty_env_var_fails(vault_env, monkeypatch) -> None:
    monkeypatch.setenv("EMPTY_ONE", "")
    assert main(["add", "openai", "--from-env", "EMPTY_ONE"]) != EXIT_OK


# ── listing never prints secrets ─────────────────────────────────────────────


def test_list_json_carries_no_secret_material(vault_env, monkeypatch, capsys) -> None:
    monkeypatch.setenv("PROBE_SECRET", "sk-must-not-appear")
    main(["add", "openai", "--from-env", "PROBE_SECRET"])
    capsys.readouterr()
    assert main(["list", "--json"]) == EXIT_OK
    out = capsys.readouterr().out
    assert "sk-must-not-appear" not in out, "list leaked the secret"
    rows = json.loads(out)
    assert rows and rows[0]["provider"] == "openai"


def test_bare_invocation_prints_help_and_succeeds(capsys) -> None:
    assert main([]) == EXIT_OK
    assert "usage:" in capsys.readouterr().out.lower()
