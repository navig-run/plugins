"""`navig explore` subcommand routing.

Every subcommand of `navig explore` was unreachable. The group callback declared a
positional `directory` for the headline `navig explore <folder>` form, and Click parses
group arguments *before* dispatching — so the positional ate the subcommand name:

    $ navig explore dedup X:\\Audio --near
    Error: No such option '--near'.

`dedup` was read as the folder, and the subcommand's own options were then parsed
against the group. The error names the option, so it reads like a typo rather than a
routing bug — which is why it survived across probe, dedup, route, collect and
merge-episode.
"""
from __future__ import annotations

from typer.testing import CliRunner

from navig_explore.commands.explore import explore_app

runner = CliRunner()

SUBCOMMANDS = ["probe", "dedup", "route", "collect", "audio-sort", "audio-undo",
               "merge-episode", "open"]


def test_every_subcommand_is_reachable_and_shows_its_own_help():
    for name in SUBCOMMANDS:
        res = runner.invoke(explore_app, [name, "--help"])
        assert res.exit_code == 0, f"{name}: exit {res.exit_code}\n{res.output}"
        assert f"explore {name}" in res.output.replace("\n", " "), \
            f"{name} --help showed the group's help, not its own:\n{res.output}"


def test_a_subcommands_own_options_are_parsed_by_it():
    """The exact regression: an option that only the subcommand defines must parse."""
    res = runner.invoke(explore_app, ["audio-sort", "--help"])
    assert "--min-dur" in res.output
    assert "--keep" in res.output


def test_bare_folder_still_routes_to_open(tmp_path, monkeypatch):
    """`navig explore <folder>` is the headline form and must keep working.

    ``serve`` blocks forever, so it is stubbed — what's under test is the routing and
    the arguments it receives, not the web server.
    """
    seen = {}

    def _fake_serve(root, out, port, open_browser=True):
        seen.update(root=root, out=out, port=port, open_browser=open_browser)

    import navig_explore.explorer as explorer
    monkeypatch.setattr(explorer, "serve", _fake_serve)

    res = runner.invoke(explore_app, [str(tmp_path), "--port", "9999", "--no-open"])
    assert res.exit_code == 0, res.output
    assert seen["root"] == tmp_path
    assert seen["port"] == 9999
    assert seen["open_browser"] is False


def test_explicit_open_subcommand_works_too(tmp_path, monkeypatch):
    called = {}
    import navig_explore.explorer as explorer
    monkeypatch.setattr(explorer, "serve",
                        lambda root, out, port, open_browser=True: called.update(root=root))
    res = runner.invoke(explore_app, ["open", str(tmp_path), "--no-open"])
    assert res.exit_code == 0, res.output
    assert called["root"] == tmp_path


def test_a_missing_folder_is_reported_not_treated_as_a_command(tmp_path):
    res = runner.invoke(explore_app, [str(tmp_path / "nope")])
    assert res.exit_code == 1
    assert "Not a directory" in res.output


def test_no_args_prints_the_usage_hint():
    res = runner.invoke(explore_app, [])
    assert res.exit_code == 1
    assert "navig explore <folder>" in res.output


def test_group_help_still_lists_every_subcommand():
    res = runner.invoke(explore_app, ["--help"])
    assert res.exit_code == 0
    for name in SUBCOMMANDS:
        assert name in res.output
