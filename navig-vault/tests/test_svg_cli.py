"""`nv export --svg | open | import | opener` — the standalone verbs, in-process.

Isolated exactly like test_cli.py: NAVIG_CONFIG_DIR points into tmp_path (so the legacy
auto-migration finds nothing real to copy) and the get_vault() singleton is reset — it
ignores its argument after first use, so switching vaults mid-test resets it too.
"""

from __future__ import annotations

import base64
import json

import pytest

import navig_vault.core as core
from navig_vault import svg
from navig_vault.cli import EXIT_FAIL, EXIT_NOT_FOUND, EXIT_OK, EXIT_USAGE, main

PASS = "a-chosen-passphrase-long-enough"


@pytest.fixture
def vault_env(tmp_path, monkeypatch):
    monkeypatch.setenv("NAVIG_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.delenv("NAVIG_VAULT_DIR", raising=False)
    monkeypatch.delenv("NAVIG_VAULT_SVG_PASSPHRASE", raising=False)
    monkeypatch.setattr(svg, "ITERATIONS", 1000)
    monkeypatch.setattr(core, "_vault", None, raising=False)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("SECRET_IN", "ghp_cli_secret")
    assert main(["add", "github", "--from-env", "SECRET_IN"]) == EXIT_OK
    monkeypatch.setenv("SECRET_IN", "sk-cli-openai")
    assert main(["add", "openai", "--from-env", "SECRET_IN"]) == EXIT_OK
    yield tmp_path
    monkeypatch.setattr(core, "_vault", None, raising=False)


def _json_out(capsys):
    return json.loads(capsys.readouterr().out)


def test_export_requires_choosing_the_svg_format(vault_env, capsys):
    assert main(["export", "github"]) == EXIT_USAGE
    assert "--svg" in capsys.readouterr().err


def test_export_needs_items_or_all_but_not_both(vault_env):
    assert main(["export", "--svg"]) == EXIT_USAGE
    assert main(["export", "--svg", "github", "--all", "--yes"]) == EXIT_USAGE


def test_the_whole_vault_is_never_exported_without_an_explicit_yes(vault_env):
    assert main(["export", "--svg", "--all"]) == EXIT_USAGE  # no terminal to ask on
    assert not list(vault_env.glob("vault-*.svg"))


def test_a_generated_passphrase_is_printed_and_opens_the_file(vault_env, capsys, monkeypatch):
    assert main(["export", "--svg", "github", "--json"]) == EXIT_OK
    [f] = _json_out(capsys)["files"]
    assert f["items"] == ["github"] and "github" not in f["path"].rsplit("\\", 1)[-1]
    monkeypatch.setenv("NAVIG_VAULT_SVG_PASSPHRASE", f["passphrase"])
    assert main(["open", f["path"], "--reveal", "--json"]) == EXIT_OK
    [item] = _json_out(capsys)["items"]
    assert item["label"] == "github" and "ghp_cli_secret" in item["fields"].values()


def test_secrets_stay_hidden_without_reveal(vault_env, capsys, monkeypatch):
    monkeypatch.setenv("NAVIG_VAULT_SVG_PASSPHRASE", PASS)
    assert main(["export", "--svg", "--all", "--yes", "--choose-passphrase", "-o", "all.svg"]) == EXIT_OK
    out = capsys.readouterr().out
    assert "all.svg" in out and PASS not in out  # a passphrase you chose is never echoed
    assert main(["open", "all.svg"]) == EXIT_OK
    shown = capsys.readouterr().out
    assert "github" in shown and "ghp_cli_secret" not in shown and "********" in shown


def test_a_short_chosen_passphrase_is_refused(vault_env, monkeypatch):
    monkeypatch.setenv("NAVIG_VAULT_SVG_PASSPHRASE", "short")
    assert main(["export", "--svg", "github", "--choose-passphrase"]) == EXIT_FAIL
    assert not list(vault_env.glob("vault-*.svg"))


def test_an_unknown_item_is_not_found_and_writes_nothing(vault_env):
    assert main(["export", "--svg", "github", "nope"]) == EXIT_NOT_FOUND
    assert not list(vault_env.glob("vault-*.svg"))


def test_import_moves_secrets_into_another_vault_and_never_overwrites(vault_env, capsys, monkeypatch):
    monkeypatch.setenv("NAVIG_VAULT_SVG_PASSPHRASE", PASS)
    assert main(["export", "--svg", "--all", "--yes", "--choose-passphrase", "-o", "all.svg"]) == EXIT_OK
    capsys.readouterr()
    other = str(vault_env / "other")
    monkeypatch.setattr(core, "_vault", None, raising=False)
    assert main(["--vault-dir", other, "import", "all.svg", "--dry-run", "--json"]) == EXIT_OK
    dry = _json_out(capsys)
    assert sorted(dry["restored"]) == ["github", "openai"] and dry["dry_run"] is True
    assert main(["--vault-dir", other, "list", "--json"]) == EXIT_OK
    assert _json_out(capsys) == []  # the dry run wrote nothing
    assert main(["--vault-dir", other, "import", "all.svg"]) == EXIT_OK
    capsys.readouterr()
    assert main(["--vault-dir", other, "get", "github"]) == EXIT_OK
    assert capsys.readouterr().out.strip() == "ghp_cli_secret"
    assert main(["--vault-dir", other, "import", "all.svg", "--json"]) == EXIT_OK
    again = _json_out(capsys)
    assert again["restored"] == [] and sorted(again["skipped"]) == ["github", "openai"]


def test_a_wrong_passphrase_fails_cleanly(vault_env, capsys, monkeypatch):
    monkeypatch.setenv("NAVIG_VAULT_SVG_PASSPHRASE", PASS)
    assert main(["export", "--svg", "github", "--choose-passphrase", "-o", "g.svg"]) == EXIT_OK
    monkeypatch.setenv("NAVIG_VAULT_SVG_PASSPHRASE", PASS + "-wrong")
    assert main(["open", "g.svg"]) == EXIT_FAIL
    assert "wrong passphrase" in capsys.readouterr().err
    assert main(["import", "g.svg"]) == EXIT_FAIL


def test_without_a_terminal_or_the_env_var_there_is_no_passphrase(vault_env, capsys, monkeypatch):
    monkeypatch.setenv("NAVIG_VAULT_SVG_PASSPHRASE", PASS)
    assert main(["export", "--svg", "github", "--choose-passphrase", "-o", "g.svg"]) == EXIT_OK
    monkeypatch.delenv("NAVIG_VAULT_SVG_PASSPHRASE")
    assert main(["open", "g.svg"]) == EXIT_FAIL
    assert "NAVIG_VAULT_SVG_PASSPHRASE" in capsys.readouterr().err


def test_export_never_overwrites_a_file(vault_env):
    (vault_env / "g.svg").write_text("keep", encoding="utf-8")
    assert main(["export", "--svg", "github", "-o", "g.svg"]) == EXIT_FAIL
    assert (vault_env / "g.svg").read_text(encoding="utf-8") == "keep"


def test_opener_writes_the_page_once(vault_env):
    assert main(["opener"]) == EXIT_OK
    assert "crypto.subtle" in (vault_env / "open-vault-svg.html").read_text(encoding="utf-8")
    assert main(["opener"]) == EXIT_FAIL  # never overwritten


def test_an_old_plain_svg_vault_file_opens_and_saves(vault_env, capsys):
    info = base64.b64encode(json.dumps({"fileName": "../note.txt", "mimeType": "text/plain",
                                        "size": 5, "version": "1.0"}).encode()).decode()
    (vault_env / "old.svg").write_text(
        '<svg xmlns="http://www.w3.org/2000/svg"><metadata id="svgVaultMetadata">'
        f'<data id="svgVaultData" encoding="base64">{base64.b64encode(b"hello").decode()}</data>'
        f'<info id="svgVaultInfo" encoding="base64">{info}</info></metadata></svg>', encoding="utf-8")
    assert main(["open", "old.svg", "--save", "saved"]) == EXIT_OK
    assert (vault_env / "saved" / "note.txt").read_bytes() == b"hello"
    assert "unencrypted" in capsys.readouterr().err
    assert main(["import", "old.svg"]) == EXIT_FAIL  # not a sealed export: nothing to import


def test_open_reports_an_svg_with_nothing_in_it(vault_env):
    (vault_env / "plain.svg").write_text('<svg xmlns="http://www.w3.org/2000/svg"/>', encoding="utf-8")
    assert main(["open", "plain.svg"]) == EXIT_NOT_FOUND
