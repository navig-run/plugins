"""A transient read lock must never wipe the games claim ledger.

`claimed.json` is the claim-idempotency memory ("the memory that makes claiming
safe to re-run"). `record()`/`forget()` do reload → mutate → save. Before adopting
`navig.core.json_io`, `_load` swallowed a transient `OSError` (a Windows AV/backup
sharing violation, a read landing mid-`os.replace`) into an empty ledger — so a
lock during a write persisted that emptiness, wiping every OTHER claim record. The
mutating reload now RAISES `JsonReadError` and `record`/`forget` skip-not-wipe.

(The shared cron store `schedule.py` is covered by tests/test_schedule.py.)
"""

from __future__ import annotations

from navig_games.engine.claim.base import STATUS_CLAIMED
from navig_games.engine.ledger import Ledger
from navig_games.engine.models import FreeGame


def _game(idx: str) -> FreeGame:
    return FreeGame(store="epic", title=f"Game {idx}", offer_id=idx, url=f"http://x/{idx}")


def test_ledger_transient_lock_does_not_wipe_records(tmp_path, monkeypatch):
    path = tmp_path / "claimed.json"
    ledger = Ledger(path=path)
    a, b = _game("A"), _game("B")
    ledger.record(a, STATUS_CLAIMED)
    ledger.record(b, STATUS_CLAIMED)
    assert {r["key"] for r in ledger.all()} == {a.key, b.key}

    # A second live ledger (the claim runs in a subprocess) tries to record C while
    # every read of the file hits a sharing violation.
    other = Ledger(path=path)
    real_read = type(path).read_text

    def _locked(*_a, **_k):
        raise PermissionError("The process cannot access the file (sharing violation)")

    monkeypatch.setattr(type(path), "read_text", _locked)
    other.record(_game("C"), STATUS_CLAIMED)  # must NOT raise, must NOT wipe A/B
    monkeypatch.setattr(type(path), "read_text", real_read)

    # A and B survive on disk; C was skipped (claiming is idempotent → retried next run).
    disk = Ledger(path=path)
    assert {r["key"] for r in disk.all()} == {a.key, b.key}


def test_ledger_forget_refuses_on_locked_store(tmp_path, monkeypatch):
    path = tmp_path / "claimed.json"
    ledger = Ledger(path=path)
    ledger.record(_game("A"), STATUS_CLAIMED)

    real_read = type(path).read_text
    monkeypatch.setattr(
        type(path), "read_text",
        lambda *_a, **_k: (_ for _ in ()).throw(PermissionError("locked")),
    )
    assert ledger.forget(_game("A").key) is False  # refuses rather than risk a wipe
    monkeypatch.setattr(type(path), "read_text", real_read)

    assert {r["key"] for r in Ledger(path=path).all()} == {_game("A").key}
