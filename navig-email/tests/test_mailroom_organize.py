"""Labels & filters organisation, and email attachments → the paperwork intake."""

from __future__ import annotations

import asyncio
import base64
import json

import pytest

from fake_gmail import FakeGmail, raw_message
from navig_email import attachments as A, organize as O
from navig_email.space import MailroomPaths

IDENTITIES = """
version: 1
identities:
  cybesis:
    display: Sam
    company: Cybesis Studios
    match:
      domains: [cybesis.test]
  personal:
    display: Sam
    language: French
"""


@pytest.fixture
def paths(tmp_path) -> MailroomPaths:
    root = tmp_path / "space"
    (root / ".navig").mkdir(parents=True)
    (root / "mailroom").mkdir(parents=True)
    (root / ".navig" / "config.yaml").write_text("mailroom: {}\n", encoding="utf-8")
    (root / "mailroom" / "identities.yaml").write_text(IDENTITIES, encoding="utf-8")
    return MailroomPaths(root)


def mailbox() -> FakeGmail:
    box = FakeGmail("box@gmail.com")
    for name, lid in (("Jobs", "L_jobs"), ("jobs ", "L_jobs2"), ("Old Stuff", "L_old"), ("GitHub notifs", "L_gh"),
                      ("Mailroom/Digested", "L_dig")):
        box.labels[lid] = {"id": lid, "name": name, "type": "user"}
    n = 0
    for frm, labels in (("jobs@free-work.com", ["INBOX", "L_jobs"]), ("jobs@free-work.com", ["L_jobs2"]),
                        ("jobs@free-work.com", ["L_jobs"]), ("noreply@github.com", ["L_gh"]),
                        ("noreply@github.com", ["L_gh"]), ("noreply@github.com", ["INBOX"]),
                        ("noreply@github.com", ["INBOX"])):
        n += 1
        box.add(raw_message(f"m{n}", thread=f"t{n}", frm=frm, to="box@gmail.com", subject="s",
                            date=f"2026-10-0{min(n, 9)}T09:00:00", labels=labels, body="x"))
    return box


def run(c):
    return asyncio.run(c)


def test_audit_finds_equivalent_unused_and_reads_counts():
    a = run(O.audit(mailbox()))
    counts = {l.name: l.messages for l in a.labels if not l.system}
    assert counts["Jobs"] == 2 and counts["GitHub notifs"] == 2
    assert ["Jobs", "jobs "] in a.equivalent
    assert "Old Stuff" in a.unused and "Mailroom/Digested" not in a.unused
    assert a.filters_access == "needs_scope"  # the token lacks the settings scope
    assert ("github.com", 4) in a.top_senders


def test_filters_conflicts_redundant_forwarding_and_dangling():
    box = mailbox()
    box.filters = [
        {"id": "f1", "criteria": {"from": "a@x.test"}, "action": {"addLabelIds": ["L_jobs"]}},
        {"id": "f2", "criteria": {"from": "a@x.test"}, "action": {"addLabelIds": ["L_jobs"]}},
        {"id": "f3", "criteria": {"from": "b@x.test"}, "action": {"addLabelIds": ["L_jobs"]}},
        {"id": "f4", "criteria": {"from": "b@x.test"}, "action": {"removeLabelIds": ["INBOX"]}},
        {"id": "f5", "criteria": {"to": "me"}, "action": {"forward": "someone@else.test"}},
        {"id": "f6", "criteria": {"from": "c@x.test"}, "action": {"addLabelIds": ["Label_gone"]}},
    ]
    a = run(O.audit(box))
    assert a.filters_access == "ok"
    assert any("f1, f2" in r for r in a.redundant) and any("f3, f4" in c for c in a.conflicts)
    assert any("someone@else.test" in f for f in a.forwarding)
    assert any("Label_gone" in d for d in a.dangling)


def test_preview_is_driven_by_the_mailbox_not_by_a_wishlist(paths):
    a = run(O.audit(mailbox()))
    plan = O.preview(a, paths=paths)
    targets = {m.target for m in plan.moves}
    assert "Opportunities/Jobs" in targets and "NAVIG/GitHub" in targets
    # a target with no mail behind it is not proposed
    assert "Opportunities/Events" not in targets and "Personal/Administration" not in plan.new_labels
    assert any(m["into"] == "Jobs" for m in plan.merges)
    assert any(f["criteria"] == {"from": "github.com"} for f in plan.filters)
    assert any("filter permission" in n for n in plan.notes)


def test_identity_targets_come_from_the_space(paths):
    t = O._identity_targets(paths)
    assert "Cybesis/Business" in t and not any(k.startswith("Sam") for k in t)


def test_system_labels_are_never_moved(paths):
    a = run(O.audit(mailbox()))
    plan = O.preview(a, paths=paths)
    assert not any(m.source in O.SYSTEM_IDS for m in plan.moves)


def test_apply_is_additive_snapshot_first_and_rollback_undoes_it(paths, tmp_path):
    box = mailbox()
    box.filters = []
    a = run(O.audit(box))
    plan = O.preview(a, paths=paths)
    before = {mid: set(m["labelIds"]) for mid, m in box.messages.items()}
    out = run(O.apply(box, plan, a, snapshot_dir=paths.reports_dir))
    snap = json.loads(open(out["snapshot"], encoding="utf-8").read())
    assert snap["labels"] and snap["added"]
    for mid, m in box.messages.items():
        assert before[mid] <= set(m["labelIds"])  # nothing was removed
    jobs_id = next(k for k, v in box.labels.items() if v["name"] == "Opportunities/Jobs")
    assert jobs_id in box.messages["m1"]["labelIds"] and out["filters_created"] >= 1
    run(O.rollback(box, __import__("pathlib").Path(out["snapshot"])))
    for mid, m in box.messages.items():
        assert set(m["labelIds"]) == before[mid]
    assert box.filters == []


def test_retire_takes_old_labels_off_only_migrated_messages(paths):
    box = mailbox()
    a = run(O.audit(box))
    plan = O.preview(a, paths=paths)
    run(O.apply(box, plan, a, snapshot_dir=paths.reports_dir))
    run(O.retire(box, plan, a))
    assert "L_gh" not in box.messages["m4"]["labelIds"]
    gh_new = next(k for k, v in box.labels.items() if v["name"] == "NAVIG/GitHub")
    assert gh_new in box.messages["m4"]["labelIds"]
    assert "INBOX" in box.messages["m6"]["labelIds"]  # system label untouched


# ── attachments ────────────────────────────────────────────────────────────


def _with_pdf(box: FakeGmail, mid: str, payload: bytes = b"%PDF-1.4 invoice"):
    m = raw_message(mid, thread=mid, frm="Billing <billing@vendor.test>", to="box@gmail.com", subject="Invoice 42",
                    date="2026-10-10T09:00:00", labels=["INBOX"], body="see attached", attachment=True)
    m["payload"]["parts"][1]["body"] = {"data": base64.urlsafe_b64encode(payload).decode(), "size": len(payload)}
    m["payload"]["parts"].append({"mimeType": "application/x-msdownload", "filename": "run.exe",
                                  "body": {"data": base64.urlsafe_b64encode(b"MZ").decode()}})
    box.add(m)


def test_attachments_go_to_the_intake_with_provenance_once(paths):
    box = FakeGmail("box@gmail.com")
    _with_pdf(box, "a1")
    res = run(A.save_from_message(box, "a1", paths=paths, account="box@gmail.com"))
    assert len(res.saved) == 1 and any("run.exe" in s for s in res.skipped)
    saved = paths.space_root / res.saved[0]["saved_as"]
    assert saved.read_bytes() == b"%PDF-1.4 invoice" and saved.parent.name == "inbox"
    row = json.loads(paths.ledger("attachments").read_text(encoding="utf-8").splitlines()[0])
    assert row["message_id"] == "a1" and row["from"] == "billing@vendor.test" and len(row["sha256"]) == 64
    again = run(A.save_from_message(box, "a1", paths=paths, account="box@gmail.com"))
    assert again.saved == [] and any("already saved" in s for s in again.skipped)


def test_a_message_with_a_filed_document_is_never_cleaned_up(paths):
    from navig_email import cleanup
    from navig_email.store import get_store

    box = FakeGmail("box@gmail.com")
    _with_pdf(box, "a2")
    run(A.save_from_message(box, "a2", paths=paths, account="box@gmail.com"))
    reason = cleanup.why_not({"id": "a2", "from": "x@y.z", "subject": "", "labels": ["INBOX"]},
                             account="box@gmail.com", store=get_store())
    assert reason.startswith("kept")


def test_safe_names():
    assert A.safe_name("../../etc/passwd") == "passwd"
    assert A.safe_name("facture <mars>.pdf") == "facture _mars_.pdf"
