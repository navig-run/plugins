"""Filing the handoff manifest into the spaces that own those documents."""

from __future__ import annotations

import json

import pytest
from navig_cabinet.paperwork import handoff_apply
from navig_cabinet.paperwork.handoff_apply import apply_handoff, destination_for
from navig_cabinet.paperwork.space import PaperworkPaths


@pytest.fixture
def spaces(tmp_path, monkeypatch):
    """A company space plus two destination spaces, wired to the resolver."""
    company = tmp_path / "company-space"
    human = tmp_path / "human-space"
    health = tmp_path / "human-health-space"
    for p in (company, human, health):
        (p / ".navig").mkdir(parents=True)

    roots = {"human-space": human, "human-health-space": health}

    def fake_resolve(name):
        if name not in roots:
            raise ValueError(f"space not found: {name!r}")
        return roots[name]

    monkeypatch.setattr(handoff_apply, "resolve_space_root", fake_resolve)
    return PaperworkPaths(company), human, health


def write_manifest(paths, entries):
    paths.base.mkdir(parents=True, exist_ok=True)
    with paths.handoff_jsonl.open("w", encoding="utf-8") as f:
        for e in entries:
            f.write(json.dumps(e) + "\n")


def entry(src, subclass, space, tool=None, sha="a" * 64):
    return {
        "row_id": src.stem, "src": str(src), "subclass": subclass,
        "suggested_space": space, "suggested_tool": tool,
        "original_name": src.name, "sha256": sha, "never_migrate": True,
    }


def make(tmp_path, name, payload=b"x"):
    src = tmp_path / "src"
    src.mkdir(exist_ok=True)
    f = src / name
    f.write_bytes(payload)
    return f


def test_a_health_document_is_encrypted_into_the_cabinet(spaces, tmp_path, cab):
    paths, human, health = spaces
    f = make(tmp_path, "ordonnance.pdf", b"medical")
    from navig_cabinet.paperwork.extract import sha256_file
    write_manifest(paths, [entry(f, "health", None, tool="navig cabinet", sha=sha256_file(f))])

    st = apply_handoff(paths, cabinet=cab, read_text=False)

    assert (st.to_cabinet, st.moved) == (1, 0)
    [item] = cab.items()
    assert (item.category, item.tags) == ("medical", ["paperwork"])
    assert f.read_bytes() == b"medical", "the original is left in place"
    assert not list(health.rglob("*.pdf")) and not list(human.rglob("*.pdf"))


def test_an_identity_document_is_encrypted_into_the_cabinet(spaces, tmp_path, cab):
    paths, human, _health = spaces
    f = make(tmp_path, "Carte_Identite.pdf", b"id")
    from navig_cabinet.paperwork.extract import sha256_file
    write_manifest(paths, [entry(f, "identity", None, tool="navig cabinet", sha=sha256_file(f))])

    st = apply_handoff(paths, cabinet=cab, read_text=False)

    assert st.to_cabinet == 1 and cab.items()[0].category == "identity"
    assert not list(human.rglob("*.pdf"))


def test_an_old_manifest_naming_a_space_still_goes_to_the_cabinet(spaces, tmp_path, cab):
    """A manifest written before the cabinet existed routed medical records to a
    plaintext space. The document TYPE decides now, not the stale destination."""
    paths, _human, health = spaces
    f = make(tmp_path, "bilan-sanguin.pdf", b"blood")
    from navig_cabinet.paperwork.extract import sha256_file
    write_manifest(paths, [entry(f, "health", "human-health-space", sha=sha256_file(f))])

    st = apply_handoff(paths, cabinet=cab, read_text=False)

    assert st.to_cabinet == 1 and st.moved == 0
    assert not list(health.rglob("*.pdf"))


def test_without_a_cabinet_personal_documents_wait_and_are_never_filed(spaces, tmp_path):
    paths, human, health = spaces
    f = make(tmp_path, "passeport.pdf", b"p")
    write_manifest(paths, [entry(f, "identity", "human-space")])

    st = apply_handoff(paths)

    assert st.awaiting_cabinet == 1 and st.moved == 0 and st.to_cabinet == 0
    assert not list(human.rglob("*.pdf")) and not list(health.rglob("*.pdf"))


def test_the_cabinet_half_is_idempotent_and_checks_the_scan_hash(spaces, tmp_path, cab):
    paths, _human, _health = spaces
    from navig_cabinet.paperwork.extract import sha256_file
    good = make(tmp_path, "cni.pdf", b"cni")
    changed = make(tmp_path, "ordo.pdf", b"now different")
    write_manifest(paths, [
        entry(good, "identity", None, tool="navig cabinet", sha=sha256_file(good)),
        entry(changed, "health", None, tool="navig cabinet", sha="0" * 64),
    ])

    st = apply_handoff(paths, cabinet=cab, read_text=False)
    assert st.to_cabinet == 1 and st.failed, "a file changed since the scan is reported"
    again = apply_handoff(paths, cabinet=cab, read_text=False)
    assert again.to_cabinet == 0 and again.already_in_cabinet == 1


def test_a_credential_is_never_written_into_any_space(spaces, tmp_path):
    """A recovery code in a markdown tree is wrong regardless of which tree."""
    paths, human, health = spaces
    f = make(tmp_path, "2fa-codes.txt", b"123456")
    write_manifest(paths, [entry(f, "secret", None, tool="navig vault")])

    st = apply_handoff(paths)

    assert st.moved == 0 and st.kept_for_tool == 1
    assert f.read_bytes() == b"123456"
    assert not list(human.rglob("*2fa*")) and not list(health.rglob("*2fa*"))


def test_another_persons_document_is_never_moved(spaces, tmp_path):
    paths, human, health = spaces
    f = make(tmp_path, "id_siadova.pdf", b"third party")
    write_manifest(paths, [entry(f, "thirdparty", None)])

    st = apply_handoff(paths)

    assert st.moved == 0 and st.no_destination == 1
    assert f.exists()
    assert not list(human.rglob("*siadova*")) and not list(health.rglob("*siadova*"))


def test_a_dry_run_moves_nothing(spaces, tmp_path):
    paths, _human, health = spaces
    f = make(tmp_path, "bilan.pdf")
    write_manifest(paths, [entry(f, "benefits", "human-health-space")])

    st = apply_handoff(paths, dry_run=True)

    assert st.moved == 1
    assert f.exists() and not list(health.rglob("bilan.pdf"))


def test_the_source_is_recoverable_in_the_destination_space(spaces, tmp_path):
    paths, _human, health = spaces
    f = make(tmp_path, "analyse.pdf", b"blood")
    from navig_cabinet.paperwork.extract import sha256_file
    write_manifest(paths, [entry(f, "housing", "human-health-space", sha=sha256_file(f))])

    apply_handoff(paths)

    assert not f.exists()
    assert list((health / ".navig" / "paperwork" / ".trash").rglob("analyse.pdf"))


def test_a_second_run_is_a_no_op(spaces, tmp_path):
    paths, _human, health = spaces
    f = make(tmp_path, "suivi.pdf", b"data")
    from navig_cabinet.paperwork.extract import sha256_file
    write_manifest(paths, [entry(f, "benefits", "human-health-space", sha=sha256_file(f))])

    apply_handoff(paths)
    st = apply_handoff(paths)

    assert st.moved == 0 and st.skipped == 1


def test_an_unknown_destination_space_is_reported_not_guessed(spaces, tmp_path):
    paths, _human, _health = spaces
    f = make(tmp_path, "x.pdf")
    write_manifest(paths, [entry(f, "benefits", "no-such-space")])

    st = apply_handoff(paths)

    assert st.moved == 0 and st.errors and f.exists()


def test_only_the_named_space_is_touched_when_filtered(spaces, tmp_path):
    paths, human, health = spaces
    from navig_cabinet.paperwork.extract import sha256_file
    a = make(tmp_path, "med.pdf", b"a")
    b = make(tmp_path, "cni.pdf", b"b")
    write_manifest(paths, [
        entry(a, "housing", "human-health-space", sha=sha256_file(a)),
        entry(b, "benefits", "human-space", sha=sha256_file(b)),
    ])

    st = apply_handoff(paths, only_space="human-health-space")

    assert st.moved == 1
    assert list(health.rglob("med.pdf")) and not list(human.rglob("cni.pdf"))
    assert b.exists()


def test_the_destination_follows_the_records_convention(tmp_path):
    dest = destination_for(
        {"subclass": "health", "original_name": "Résultats d'analyse (1).pdf", "src": "x",
         "sha256": "f" * 64},
        tmp_path, batch_date="2026-09",
    )
    parts = dest.relative_to(tmp_path).parts
    assert parts[0] == "records"
    assert parts[1].startswith("2026-09_")
    assert parts[2] == "health"
    assert dest.suffix == ".pdf" and " " not in dest.name


def test_two_documents_that_slug_to_the_same_name_do_not_overwrite(spaces, tmp_path):
    """`avis_de_situation.pdf` and `avis de situation (1).pdf` slug identically.

    Overwriting silently lost four real documents on the first live run.
    """
    from navig_cabinet.paperwork.extract import sha256_file
    paths, human, _health = spaces
    a = make(tmp_path, "avis_de_situation.pdf", b"first document")
    b = make(tmp_path, "avis de situation.pdf", b"second, different document")
    write_manifest(paths, [
        entry(a, "benefits", "human-space", sha=sha256_file(a)),
        entry(b, "benefits", "human-space", sha=sha256_file(b)),
    ])

    st = apply_handoff(paths)

    assert st.moved == 2
    filed = sorted(p.read_bytes() for p in human.rglob("*.pdf") if ".trash" not in p.parts)
    assert b"first document" in filed
    assert b"second, different document" in filed


def test_a_collision_rerun_is_still_a_no_op(spaces, tmp_path):
    from navig_cabinet.paperwork.extract import sha256_file
    paths, human, _health = spaces
    a = make(tmp_path, "avis_de_situation.pdf", b"one")
    b = make(tmp_path, "avis de situation.pdf", b"two")
    write_manifest(paths, [
        entry(a, "benefits", "human-space", sha=sha256_file(a)),
        entry(b, "benefits", "human-space", sha=sha256_file(b)),
    ])
    apply_handoff(paths)
    st = apply_handoff(paths)
    assert st.moved == 0 and st.skipped == 2
