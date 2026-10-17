"""``navig contacts`` — both halves of it, and its exit codes.

navig's contract: ``typer.Exit(2)`` for a usage error, ``typer.Exit(1)`` for an
operation failure, and never an error message followed by exit 0.
"""

from __future__ import annotations

from conftest import write_vcf
from typer.testing import CliRunner

from navig_contacts.store import ContactStore
from navig_contacts.commands.contacts import app

runner = CliRunner()

CARDS = [
    "FN:Florian Adnot\nN:Adnot;Florian;;;\nTEL:+33678488684",
    "FN:mailonly\nN:mailonly;;;;\nEMAIL:mail.only@example.com",
]


def _import(tmp_path, cards=CARDS, extra=()):
    path = write_vcf(tmp_path / "a.vcf", cards)
    return runner.invoke(app, ["import", str(path), "--no-reports", *extra])


# ── the routing half ──────────────────────────────────────────

def test_add_list_and_route(book):
    added = runner.invoke(app, ["add", "--alias", "alice", "--name", "Alice B.",
                                "--route", "whatsapp:+33612345678"])
    assert added.exit_code == 0, added.output

    listed = runner.invoke(app, ["list"])
    assert listed.exit_code == 0
    assert "alice" in listed.output
    assert "whatsapp" in listed.output

    routed = runner.invoke(app, ["route", "alice", "add", "sms:+33612345678"])
    assert routed.exit_code == 0, routed.output
    assert "added" in routed.output


def test_a_duplicate_alias_is_refused(book):
    runner.invoke(app, ["add", "--alias", "alice"])
    again = runner.invoke(app, ["add", "--alias", "alice"])
    assert again.exit_code == 1
    assert "already exists" in again.output


def test_add_refuses_a_phone_that_is_not_one(book):
    result = runner.invoke(app, ["add", "--alias", "bob", "--phone", "+666"])
    assert result.exit_code == 2
    assert "Not a usable phone number" in result.output


def test_the_cli_does_not_announce_a_removal_it_did_not_make(book):
    """
    Moved here with the command from core's suite: `remove_route` reports False
    when it removed nothing, and announcing success there would leave the
    operator believing an address they can still be messaged at is gone.
    """
    runner.invoke(app, ["add", "--alias", "alice", "--route", "telegram:111"])

    missing = runner.invoke(app, ["route", "alice", "remove", "telegram:999999"])
    assert missing.exit_code != 0, missing.output
    assert "removed from" not in missing.output

    real = runner.invoke(app, ["route", "alice", "remove", "telegram:111"])
    assert real.exit_code == 0, real.output
    assert "removed from" in real.output


def test_adding_a_route_twice_says_nothing_changed(book):
    """`add_route` is INSERT OR IGNORE and reports True either way."""
    runner.invoke(app, ["add", "--alias", "alice", "--route", "telegram:111"])
    again = runner.invoke(app, ["route", "alice", "add", "telegram:111"])
    assert again.exit_code == 0
    assert "already had" in again.output


def test_route_priority_defaults_to_one_past_the_highest(book):
    """
    The CLI used to default --priority to 0, which made the store's
    MAX(priority)+1 branch dead and left `ORDER BY priority` a tie.
    """
    runner.invoke(app, ["add", "--alias", "alice", "--route", "telegram:111"])
    runner.invoke(app, ["route", "alice", "add", "sms:+33612345678"])
    routes = ContactStore(db_path=book).resolve_alias("alice").routes
    assert len({r.priority for r in routes}) == len(routes), (
        f"routes tied on priority: {[(r.network, r.priority) for r in routes]}")


def test_remove(book):
    runner.invoke(app, ["add", "--alias", "alice"])
    gone = runner.invoke(app, ["remove", "alice", "--yes"])
    assert gone.exit_code == 0
    assert runner.invoke(app, ["remove", "alice", "--yes"]).exit_code == 1


# ── the address-book half ─────────────────────────────────────

def test_import_then_list_and_search(book, tmp_path):
    result = _import(tmp_path)
    assert result.exit_code == 0, result.output

    listed = runner.invoke(app, ["list", "--tier", "verified"])
    assert listed.exit_code == 0
    assert "Florian Adnot" in listed.output
    assert "mailonly" not in listed.output

    for query in ("0678488684", "+33678488684", "Adnot"):
        found = runner.invoke(app, ["search", query])
        assert found.exit_code == 0, query
        assert "p:33678488684" in found.output, query


def test_search_finds_a_hand_made_contact_by_its_route(book):
    runner.invoke(app, ["add", "--alias", "alice", "--name", "Alice B.",
                        "--route", "sms:+33612345678"])
    found = runner.invoke(app, ["search", "+33612345678"])
    assert found.exit_code == 0
    assert "alice" in found.output


def test_dry_run_writes_nothing(book, tmp_path):
    result = _import(tmp_path, extra=["--dry-run"])
    assert result.exit_code == 0
    assert "No contacts in" in runner.invoke(app, ["list"]).output


def test_show_reports_provenance(book, tmp_path):
    _import(tmp_path)
    shown = runner.invoke(app, ["show", "p:33678488684"])
    assert shown.exit_code == 0
    assert "+33678488684" in shown.output
    assert "From:" in shown.output
    assert "verified" in shown.output


def test_export_csv(book, tmp_path):
    _import(tmp_path)
    out = runner.invoke(app, ["export", "--tier", "verified"])
    assert out.exit_code == 0
    assert "alias,display_name,tier,primary_phone" in out.output.replace(" ", "")


def test_stats(book, tmp_path):
    _import(tmp_path)
    out = runner.invoke(app, ["stats"])
    assert out.exit_code == 0
    assert "2 contacts" in out.output


def test_merge_and_undo_through_the_cli(book, tmp_path):
    _import(tmp_path, [
        "FN:Person One\nN:One;Person;;;\nTEL:+33678488684",
        "FN:Person Two\nN:Two;Person;;;\nTEL:+33610438304",
    ])
    merged = runner.invoke(app, ["merge", "p:33610438304", "p:33678488684"])
    assert merged.exit_code == 0, merged.output

    listing = runner.invoke(app, ["merges"])
    assert listing.exit_code == 0 and "applied" in listing.output

    undone = runner.invoke(app, ["undo", "1"])
    assert undone.exit_code == 0, undone.output
    assert runner.invoke(app, ["show", "p:33610438304"]).exit_code == 0


def test_split_through_the_cli(book, tmp_path):
    _import(tmp_path, ["FN:Kirill\nN:Kirill;;;;\nTEL:+33467695457\nTEL:+33671514812"])
    out = runner.invoke(app, ["split", "p:33467695457", "phone:+33671514812",
                              "--as", "Kirill mama"])
    assert out.exit_code == 0, out.output
    shown = runner.invoke(app, ["show", "p:33671514812"])
    assert shown.exit_code == 0
    assert "Kirill mama" in shown.output


# ── two books ─────────────────────────────────────────────────

def test_space_and_global_are_separate_books(space_book, tmp_path):
    _import(tmp_path)                                    # global
    space_import = runner.invoke(app, [
        "import", str(write_vcf(tmp_path / "b.vcf",
                                ["FN:Someone Else\nN:Else;Someone;;;\nTEL:+33610438304"])),
        "--space", "a-space", "--no-reports"])
    assert space_import.exit_code == 0, space_import.output

    globally = runner.invoke(app, ["list"])
    in_space = runner.invoke(app, ["list", "--space", "a-space"])
    assert "Florian Adnot" in globally.output
    assert "Florian Adnot" not in in_space.output
    assert "Someone Else" in in_space.output


def test_an_empty_book_says_which_one_it_read(space_book):
    out = runner.invoke(app, ["list"])
    assert out.exit_code == 0
    assert "No contacts in" in out.output
    assert "--space" in out.output, "must point at the other book"


# ── exit codes ────────────────────────────────────────────────

def test_a_missing_import_path_is_a_usage_error(book):
    assert runner.invoke(app, ["import", "nope.vcf"]).exit_code == 2


def test_an_unrecognisable_import_is_a_usage_error(book, tmp_path):
    junk = tmp_path / "notes.txt"
    junk.write_text("not an address book", encoding="utf-8")
    result = runner.invoke(app, ["import", str(junk)])
    assert result.exit_code == 2
    assert "Cannot tell what" in result.output


def test_an_unknown_tier_is_a_usage_error(book):
    assert runner.invoke(app, ["list", "--tier", "nonsense"]).exit_code == 2
    assert runner.invoke(app, ["export", "--tier", "nonsense"]).exit_code == 2


def test_an_unknown_space_is_a_usage_error(space_book):
    result = runner.invoke(app, ["list", "--space", "no-such-space"])
    assert result.exit_code == 2
    assert "space not found" in result.output


def test_a_bad_route_spec_is_a_usage_error(book):
    runner.invoke(app, ["add", "--alias", "alice"])
    assert runner.invoke(app, ["route", "alice", "add", "nodivider"]).exit_code == 2
    assert runner.invoke(app, ["route", "alice", "sideways",
                               "sms:+33612345678"]).exit_code == 2


def test_operations_on_a_missing_contact_fail(book, tmp_path):
    _import(tmp_path)
    assert runner.invoke(app, ["show", "p:00000000000"]).exit_code == 1
    assert runner.invoke(app, ["route", "nobody", "add", "sms:+331"]).exit_code == 1
    assert runner.invoke(app, ["merge", "nobody", "p:33678488684"]).exit_code == 1
    assert runner.invoke(app, ["undo", "999"]).exit_code == 1


def test_merging_a_contact_into_itself_is_a_usage_error(book, tmp_path):
    _import(tmp_path)
    assert runner.invoke(app, ["merge", "p:33678488684",
                               "p:33678488684"]).exit_code == 2


def test_splitting_the_only_identifier_is_refused(book, tmp_path):
    _import(tmp_path)
    result = runner.invoke(app, ["split", "p:33678488684", "phone:+33678488684"])
    assert result.exit_code == 1
    assert "no way to reach them" in result.output
