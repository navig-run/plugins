"""The `navig cabinet` verbs end to end, through Typer, with --json where it exists."""

from __future__ import annotations

import json
import os
from datetime import date, timedelta
from pathlib import Path

from navig_cabinet.commands.cabinet import cabinet_app
from typer.testing import CliRunner

runner = CliRunner()


def run(*args, env=None):
    return runner.invoke(cabinet_app, [str(a) for a in args], env=env)


def _json(res):
    return json.loads(res.output[res.output.index(next(c for c in res.output if c in "[{")):])


def _src(tmp_path, name, data):
    p = tmp_path / "src" / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)
    return p


def test_status_before_anything_exists():
    res = run("status", "--json")
    assert res.exit_code == 0 and _json(res)["exists"] is False
    assert run("list").exit_code == 1  # nothing to list is a clear error, not an empty success


def test_add_list_search_show_export(tmp_path):
    soon = (date.today() + timedelta(days=20)).isoformat()
    pdf = _src(tmp_path, "passport.pdf", b"%PDF fake")
    res = run("add", pdf, "--no-ocr", "--expires", soon, "-t", "travel,family", "--json")
    assert res.exit_code == 0, res.output
    item = _json(res)["added"][0]
    assert item["category"] == "identity" and item["tags"] == ["travel", "family"]
    assert pdf.exists()

    folder = tmp_path / "src" / "more"
    folder.mkdir()
    (folder / "song.mp3").write_bytes(os.urandom(300))
    (folder / ".hidden").write_bytes(b"x")
    res = run("add", folder, "--no-ocr", "--json")
    assert res.exit_code == 0 and len(_json(res)["added"]) == 1

    listed = _json(run("list", "--json"))
    assert {i["original_name"] for i in listed} == {"passport.pdf", "song.mp3"}
    assert [i["id"] for i in _json(run("list", "--category", "identity", "--json"))] == [item["id"]]

    hits = _json(run("search", "FAMILY", "--json"))
    assert [h["id"] for h in hits] == [item["id"]]
    assert _json(run("search", "nothing-like-this", "--json")) == []

    shown = _json(run("show", item["id"][:4], "--json"))
    assert shown["title"] == "passport"

    due = _json(run("expiring", "--within", "30", "--json"))
    assert [d["id"] for d in due] == [item["id"]] and due[0]["days_left"] == 20

    out = tmp_path / "out"
    res = run("export", item["id"], "-o", out, "--json")
    assert res.exit_code == 0, res.output
    assert (out / "passport.pdf").read_bytes() == b"%PDF fake"

    res = run("export", "--all", "--by-category", "-o", out / "all")
    assert res.exit_code == 0
    assert (out / "all" / "identity" / "passport.pdf").exists()
    assert (out / "all" / "recordings" / "song.mp3").exists()


def test_duplicate_is_skipped_not_failed(tmp_path):
    p = _src(tmp_path, "a.pdf", b"same")
    assert run("add", p, "--no-ocr").exit_code == 0
    res = run("add", _src(tmp_path, "b.pdf", b"same"), "--no-ocr", "--json")
    assert res.exit_code == 0
    assert len(_json(res)["duplicates"]) == 1


def test_move_deletes_the_original_only_after_storing(tmp_path):
    p = _src(tmp_path, "a.pdf", b"move me")
    assert run("add", p, "--no-ocr", "--move").exit_code == 0
    assert not p.exists()
    item = _json(run("list", "--json"))[0]
    run("export", item["id"], "-o", tmp_path / "o")
    assert (tmp_path / "o" / "a.pdf").read_bytes() == b"move me"


def test_usage_errors_exit_2(tmp_path):
    assert run("add", tmp_path / "missing.pdf").exit_code == 2
    p = _src(tmp_path, "a.pdf", b"a")
    assert run("add", p, "--expires", "next week").exit_code == 2
    assert run("add", p, "--category", "nonsense").exit_code == 2
    assert run("add", p, _src(tmp_path, "b.pdf", b"b"), "--title", "x").exit_code == 2


def test_unknown_id_exits_1(tmp_path):
    run("add", _src(tmp_path, "a.pdf", b"a"), "--no-ocr")
    res = run("show", "ffffffff")
    assert res.exit_code == 1


def test_edit_remove_undelete(tmp_path):
    run("add", _src(tmp_path, "a.pdf", b"a"), "--no-ocr")
    iid = _json(run("list", "--json"))[0]["id"]
    assert run("edit", iid, "--title", "Carte vitale", "-c", "medical", "-t", "x", "--expires", "2030-01-01").exit_code == 0
    got = _json(run("show", iid, "--json"))
    assert (got["title"], got["category"], got["tags"], got["expires"]) == ("Carte vitale", "medical", ["x"], "2030-01-01")
    assert run("edit", iid, "--untag", "x", "--expires", "none").exit_code == 0
    got = _json(run("show", iid, "--json"))
    assert got["tags"] == [] and got["expires"] is None

    assert run("remove", iid).exit_code == 2      # no terminal to confirm → refuse, do nothing
    assert run("remove", iid, "--yes").exit_code == 0
    assert _json(run("list", "--json")) == []
    assert len(_json(run("list", "--trash", "--json"))) == 1
    assert run("undelete", iid).exit_code == 0
    assert run("remove", iid, "--purge", "--yes").exit_code == 0
    assert _json(run("list", "--trash", "--json")) == []


def test_passphrase_mode_via_cli(tmp_path):
    run("add", _src(tmp_path, "a.pdf", b"a"), "--no-ocr")
    assert run("passphrase", "set", env={"NAVIG_CABINET_NEW_PASSPHRASE": "s3cret"}).exit_code == 0
    res = run("list", "--json")
    assert res.exit_code == 2, "without a terminal or the env var, it must refuse — not open"
    assert run("list", env={"NAVIG_CABINET_PASSPHRASE": "wrong"}).exit_code == 1
    ok = run("list", "--json", env={"NAVIG_CABINET_PASSPHRASE": "s3cret"})
    assert ok.exit_code == 0 and len(_json(ok)) == 1
    assert run("passphrase", "clear", env={"NAVIG_CABINET_PASSPHRASE": "s3cret"}).exit_code == 0
    assert run("list").exit_code == 0


def test_backup_restore_via_cli(tmp_path, monkeypatch):
    run("add", _src(tmp_path, "a.pdf", b"aaa"), "--no-ocr")
    env = {"NAVIG_CABINET_BACKUP_PASSPHRASE": "bpw"}
    bak = tmp_path / "not-yet" / "bk"   # a folder that does not exist yet
    res = run("backup", "-o", bak, env=env)
    assert res.exit_code == 0, res.output
    assert (tmp_path / "not-yet" / "bk.ncab").exists()
    assert run("backup", "-o", bak, env=env).exit_code == 2  # never overwrites
    assert _json(run("status", "--json"))["last_backup_at"]

    monkeypatch.setenv("NAVIG_CABINET_DIR", str(tmp_path / "new-machine"))
    res = run("restore", tmp_path / "not-yet" / "bk.ncab", env=env)
    assert res.exit_code == 0, res.output
    assert [i["original_name"] for i in _json(run("list", "--json"))] == ["a.pdf"]
    assert run("restore", tmp_path / "not-yet" / "bk.ncab", env={"NAVIG_CABINET_BACKUP_PASSPHRASE": "no"}).exit_code == 1


def test_verify_reports_damage(tmp_path):
    run("add", _src(tmp_path, "a.pdf", b"a" * 100), "--no-ocr")
    assert run("verify").exit_code == 0
    obj = next(Path(os.environ["NAVIG_CABINET_DIR"], "objects").glob("*.bin"))
    raw = bytearray(obj.read_bytes())
    raw[-1] ^= 1
    obj.write_bytes(bytes(raw))
    res = run("verify", "--json")
    assert res.exit_code == 1 and len(_json(res)["bad"]) == 1


def test_import_paperwork_via_cli(tmp_path):
    import hashlib

    cni = _src(tmp_path, "cni.pdf", b"cni")
    m = tmp_path / "handoff.jsonl"
    m.write_text(json.dumps({"src": str(cni), "subclass": "identity",
                             "sha256": hashlib.sha256(b"cni").hexdigest()}) + "\n", encoding="utf-8")
    dry = run("import-paperwork", m, "--dry-run", "--json")
    assert dry.exit_code == 0 and _json(dry)[0]["outcome"] == "imported"
    assert run("status", "--json").output.count('"exists": false') == 1, "dry run creates nothing"
    res = run("import-paperwork", m, "--no-ocr")
    assert res.exit_code == 0, res.output
    assert _json(run("list", "--json"))[0]["category"] == "identity"
    assert run("import-paperwork").exit_code == 2
