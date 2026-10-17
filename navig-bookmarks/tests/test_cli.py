"""The command surface: real flows through the Typer app, and honest exit codes."""

from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from navig_bookmarks.commands.bookmarks import app

from pathlib import Path

FIXTURES = Path(__file__).parent / "fixtures"

runner = CliRunner()


def run(*args: str, input: str | None = None):  # noqa: A002
    return runner.invoke(app, list(args), input=input)


def ok_json(*args: str):
    result = run(*args, "--json")
    assert result.exit_code == 0, result.output
    return json.loads(result.stdout)


@pytest.fixture()
def imported(db_path):
    result = run("import", str(FIXTURES / "netscape.html"))
    assert result.exit_code == 0, result.output
    return {b["url"]: b for b in ok_json("list", "--limit", "100")}


@pytest.fixture()
def fake_ai(monkeypatch):
    """Replace the AI: `answers(system, user)` decides; records every call."""
    from navig_sdk import ai

    calls = []

    def install(answer):
        def generate(prompt, **kw):
            calls.append({"prompt": prompt, **kw})
            return answer(kw.get("system") or "", prompt)

        monkeypatch.setattr(ai, "generate", generate)
        return calls

    return install


def _ids(user: str) -> list[str]:
    import re

    return re.findall(r"^(\S+) \| ", user.split("Bookmarks (id")[1], flags=re.M)


# ── basics ──────────────────────────────────────────────────────────────────────


def test_add_list_show_edit_delete(db_path) -> None:
    added = ok_json("add", "example.com/page", "--title", "Example", "--tags", "a,b", "--folder", "Dev/Web")
    bid = added["id"]
    assert added["url"] == "https://example.com/page" and not added["duplicate"]
    dup = ok_json("add", "https://www.example.com/page/?utm_source=x")
    assert dup == {"id": bid, "url": "https://example.com/page", "duplicate": True}
    shown = ok_json("show", bid[:5])
    assert shown["folder"] == "Dev/Web" and {t["tag"] for t in shown["tags"]} == {"a", "b"}
    assert run("edit", bid, "--title", "Renamed", "--tags", "c").exit_code == 0
    shown = ok_json("show", bid)
    assert shown["title"] == "Renamed" and [t["tag"] for t in shown["tags"]] == ["c"]
    assert [b["id"] for b in ok_json("search", "renamed")] == [bid]
    assert run("delete", bid, input="n\n").exit_code == 1  # declined -> Abort
    assert run("delete", bid, "--force").exit_code == 0
    assert ok_json("list") == []


def test_a_bare_url_means_add(db_path) -> None:
    result = run("https://example.org/x", "--json")
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["url"] == "https://example.org/x"


@pytest.mark.parametrize(
    "args",
    [("show", "nope0000"), ("edit", "nope0000", "--title", "x"), ("delete", "nope0000", "--force"),
     ("open", "nope0000"), ("add", "not a url"), ("tag", "only-one-arg"), ("tag", "--ai"),
     ("export", "--format", "xml"), ("import", "missing-file.html"), ("import", "--source", "nope"),
     ("organize", "--apply", "--dry-run"), ("organize", "--depth", "5"), ("rules", "add", "nodomain", "x"),
     ("rules", "remove", "42")],
)
def test_usage_errors_exit_2(db_path, args) -> None:
    run("add", "https://example.com")  # so organize has something to look at
    result = run(*args)
    assert result.exit_code == 2, (args, result.output)


def test_an_unreadable_import_exits_1(db_path, tmp_path) -> None:
    bad = tmp_path / "Bookmarks"
    bad.write_text("{ this is not json", encoding="utf-8")
    assert run("import", str(bad)).exit_code == 1


# ── import / export ─────────────────────────────────────────────────────────────


def test_import_keeps_folders_and_skips_repeats(imported, db_path) -> None:
    assert len(imported) == 5
    assert imported["https://github.com/encode/httpx"]["folder"] == "Bookmarks bar/Dev"
    again = ok_json("import", str(FIXTURES / "netscape.html"))
    assert (again["added"], again["skipped"]) == (0, 5)


def test_import_into_a_prefix_with_extra_tags(db_path) -> None:
    res = ok_json("import", str(FIXTURES / "netscape.html"), "--into", "Laptop", "--tag", "old")
    assert res["added"] == 5
    rows = ok_json("list", "--folder", "Laptop/Bookmarks bar")
    assert rows and all("old" in {t["tag"] for t in r["tags"]} for r in rows)


def test_export_html_round_trips_through_import(imported, db_path, tmp_path, monkeypatch) -> None:
    out = tmp_path / "out.html"
    assert run("export", str(out)).exit_code == 0
    monkeypatch.setenv("NAVIG_BOOKMARKS_DB", str(tmp_path / "second.db"))
    from navig_bookmarks.commands import bookmarks as cmd

    cmd._STORES.clear()
    assert ok_json("import", str(out))["added"] == 5
    second = {b["url"]: b for b in ok_json("list", "--limit", "100")}
    assert {u: (b["folder"], b["title"]) for u, b in second.items()} == {
        u: (b["folder"], b["title"]) for u, b in imported.items()
    }


def test_export_json(imported) -> None:
    result = run("export", "--format", "json")
    data = json.loads(result.stdout)
    assert data["format"] == "navig-bookmarks" and len(data["bookmarks"]) == 5


# ── organize ────────────────────────────────────────────────────────────────────


def test_organize_without_ai_says_so_and_exits_2(imported, monkeypatch) -> None:
    from navig_sdk import ai

    def no_ai(*a, **k):
        raise ai.AINotConfigured()

    monkeypatch.setattr(ai, "generate", no_ai)
    result = run("organize")
    assert result.exit_code == 2


def test_organize_provider_failure_exits_1(imported, monkeypatch) -> None:
    from navig_sdk import ai

    def broken(*a, **k):
        raise ai.AIError("HTTP 500")

    monkeypatch.setattr(ai, "generate", broken)
    assert run("organize").exit_code == 1


def test_organize_dry_run_changes_nothing_and_passes_provider_model(imported, fake_ai) -> None:
    calls = fake_ai(lambda s, u: json.dumps({"assignments": [{"id": k, "folder": "Reading"} for k in _ids(u)]}))
    plan = ok_json("organize", "--provider", "ollama", "--model", "llama3")
    assert plan["ok"] and not plan["applied"] and set(plan["plan"].values()) == {"Reading"}
    assert calls[0]["provider"] == "ollama" and calls[0]["model"] == "llama3"
    assert {b["folder"] for b in ok_json("list", "--limit", "100")} != {"Reading"}  # untouched


def test_organize_apply_needs_force_for_a_big_plan_then_undo_restores(imported, fake_ai, db_path) -> None:
    fake_ai(lambda s, u: json.dumps({"assignments": [{"id": k, "folder": "Reading/Later"} for k in _ids(u)]}))
    refused = run("organize", "--apply")
    assert refused.exit_code == 1  # 100 % of bookmarks move: failsafe
    before = {b["id"]: b["folder"] for b in ok_json("list", "--limit", "100")}
    assert set(before.values()) != {"Reading/Later"}
    applied = run("organize", "--apply", "--force")
    assert applied.exit_code == 0, applied.output
    after = {b["id"]: b["folder"] for b in ok_json("list", "--limit", "100")}
    assert set(after.values()) == {"Reading/Later"}
    backups = sorted((db_path.parent / "bookmarks-backups").glob("organize-*.json"))
    assert len(backups) == 1
    assert run("organize", "--undo", str(backups[0])).exit_code == 0
    assert {b["id"]: b["folder"] for b in ok_json("list", "--limit", "100")} == before


def test_organize_refuses_a_plan_with_missing_ids_unless_partial(imported, fake_ai) -> None:
    def half(system, user):
        keys = _ids(user)
        return json.dumps({"assignments": [{"id": k, "folder": "A"} for k in keys[:1]]})

    fake_ai(half)
    assert run("organize").exit_code == 1
    partial = ok_json("organize", "--allow-partial")
    assert partial["ok"]


def test_organize_export_writes_the_planned_tree(imported, fake_ai, tmp_path) -> None:
    fake_ai(lambda s, u: json.dumps({"assignments": [{"id": k, "folder": "Topic/Sub"} for k in _ids(u)]}))
    out = tmp_path / "plan.html"
    assert run("organize", "--export", str(out)).exit_code == 0
    text = out.read_text(encoding="utf-8")
    assert "<H3>Topic</H3>" in text and "<H3>Sub</H3>" in text


# ── tags, rules, dedupe, check ──────────────────────────────────────────────────


def test_ai_tagging_replaces_only_ai_tags(imported, fake_ai) -> None:
    fake_ai(lambda s, u: json.dumps({"tags": [{"id": k, "tags": ["Python", "website", "async"]} for k in _ids(u)]}))
    res = ok_json("tag", "--ai", "--all")
    some_id = imported["https://github.com/encode/httpx"]["id"]  # a bookmark with no tags of its own
    assert res["tags"][some_id] == ["python", "async"]  # cleaned, junk dropped
    fake_ai(lambda s, u: json.dumps({"tags": [{"id": k, "tags": ["reference"]} for k in _ids(u)]}))
    ok_json("tag", "--ai", some_id)
    shown = {t["tag"]: t["source"] for t in ok_json("show", some_id)["tags"]}
    assert shown.get("reference") == "ai" and "async" not in shown
    # The user's tag from the import ("python" on the asyncio page) survives every AI pass.
    asyncio_id = imported["https://docs.python.org/3/library/asyncio.html"]["id"]
    fake_ai(lambda s, u: json.dumps({"tags": [{"id": k, "tags": []} for k in _ids(u)]}))
    ok_json("tag", "--ai", asyncio_id)
    assert {t["tag"]: t["source"] for t in ok_json("show", asyncio_id)["tags"]} == {"python": "user", "async": "user"}


def test_curated_tagging_needs_a_vocabulary(db_path, fake_ai) -> None:
    run("add", "https://example.com")
    fake_ai(lambda s, u: "{}")
    assert run("tag", "--ai", "--all", "--curated").exit_code == 2


def test_ai_tagging_that_answers_nothing_exits_1(imported, fake_ai) -> None:
    fake_ai(lambda s, u: "I cannot help with that.")
    assert run("tag", "--ai", "--all").exit_code == 1


def test_rules_apply_on_add_and_retroactively(imported) -> None:
    assert run("rules", "add", "github.com", "code").exit_code == 0
    assert ok_json("add", "https://github.com/new/repo")["rule_tags"] == ["code"]
    assert run("rules", "apply").exit_code == 0
    httpx = imported["https://github.com/encode/httpx"]["id"]
    assert {t["tag"]: t["source"] for t in ok_json("show", httpx)["tags"]} == {"code": "rule"}
    assert run("rules", "remove", "1").exit_code == 0
    assert run("rules", "apply").exit_code == 0
    assert ok_json("show", httpx)["tags"] == []


def test_dedupe_finds_and_merges(db_path) -> None:
    from navig_bookmarks.commands import bookmarks as cmd

    store = cmd._store()
    a = store.add("https://example.com/page", tags=["one"])
    store.add("http://www.example.com/page/?utm_source=mail", tags=["two"])
    store.add("https://example.com/other")
    groups = ok_json("dedupe")
    assert len(groups) == 1 and groups[0]["keep"] == a
    merged = ok_json("dedupe", "--apply")
    assert merged == {"merged": 1, "groups": 1}
    assert len(ok_json("list")) == 2


def test_check_records_results_and_fail_on_dead(imported, monkeypatch) -> None:
    from navig_bookmarks import checker

    def fake_many(items, **kw):
        return [checker.CheckResult(bid, url, "dead" if "httpx" in url else "ok", status=404 if "httpx" in url else 200)
                for bid, url in items]

    monkeypatch.setattr(checker, "check_many", fake_many)
    data = ok_json("check")
    assert data["counts"] == {"ok": 4, "dead": 1}
    assert run("check").exit_code == 0  # a finding, not a failure
    assert run("check", "--fail-on-dead").exit_code == 1
    httpx = imported["https://github.com/encode/httpx"]["id"]
    assert ok_json("show", httpx)["http_status"] == 404


def test_open_without_a_credential_uses_the_default_browser(imported, monkeypatch) -> None:
    from navig_bookmarks.commands import bookmarks as cmd

    opened = []
    monkeypatch.setattr(cmd, "_open_plain", opened.append)
    bid = imported["https://example.net/top-level"]["id"]
    assert run("open", bid).exit_code == 0
    assert opened == ["https://example.net/top-level"]
    assert ok_json("show", bid)["visit_count"] == 1
    assert run("open", bid, "--headless").exit_code == 2  # nothing to log into
