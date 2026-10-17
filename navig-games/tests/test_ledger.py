"""Ledger idempotence — the guarantee that a game is never claimed twice."""

from navig_games.engine.claim.base import STATUS_CLAIMED, STATUS_GRABBED, STATUS_MANUAL
from navig_games.engine.ledger import Ledger
from navig_games.engine.models import FreeGame


def _game(idx: str) -> FreeGame:
    return FreeGame(store="epic", title=f"Game {idx}", offer_id=idx, url=f"http://x/{idx}")


def test_unseen_and_settle(tmp_path):
    ledger = Ledger(path=tmp_path / "claimed.json")
    a, b = _game("A"), _game("B")

    assert ledger.unseen([a, b]) == [a, b]

    ledger.record(a, STATUS_CLAIMED)
    # A is now settled; only B remains unseen.
    unseen = ledger.unseen([a, b])
    assert [g.key for g in unseen] == [b.key]


def test_needs_manual_is_retryable(tmp_path):
    ledger = Ledger(path=tmp_path / "claimed.json")
    a = _game("A")
    ledger.record(a, STATUS_MANUAL, "captcha")
    # A "needs_manual" game is NOT settled → still attempted next run.
    assert ledger.unseen([a]) == [a]


def test_persistence_roundtrip(tmp_path):
    path = tmp_path / "claimed.json"
    Ledger(path=path).record(_game("A"), STATUS_CLAIMED)
    reloaded = Ledger(path=path)
    assert reloaded.is_settled("epic:a")
    assert len(reloaded.all()) == 1


def test_corrupt_file_recovers(tmp_path):
    path = tmp_path / "claimed.json"
    path.write_text("{ this is not json", encoding="utf-8")
    ledger = Ledger(path=path)  # must not raise
    assert ledger.all() == []
    ledger.record(_game("A"), STATUS_CLAIMED)  # and is usable
    assert ledger.is_settled("epic:a")


def test_two_live_ledgers_do_not_clobber_each_other(tmp_path):
    """Regression: the claim runs in a SUBPROCESS while the daemon records a grab.

    Both hold a Ledger over the same file. A load-once snapshot + whole-file save
    meant the later writer erased everything the other had recorded.
    """
    path = tmp_path / "claimed.json"
    daemon, subprocess_ = Ledger(path=path), Ledger(path=path)  # both loaded empty

    a, b = _game("A"), _game("B")
    subprocess_.record(a, STATUS_CLAIMED)  # the claim lands first…
    daemon.record(b, STATUS_GRABBED)  # …then a grab from the other process

    fresh = Ledger(path=path)
    assert {r["key"] for r in fresh.all()} == {a.key, b.key}  # both survive
    assert fresh.get(a.key)["status"] == STATUS_CLAIMED


def test_forget_only_drops_its_own_key(tmp_path):
    path = tmp_path / "claimed.json"
    ledger = Ledger(path=path)
    a, b = _game("A"), _game("B")
    ledger.record(a, STATUS_GRABBED)
    ledger.record(b, STATUS_CLAIMED)

    assert ledger.forget(a.key) is True
    assert ledger.forget(a.key) is False  # already gone
    assert [r["key"] for r in Ledger(path=path).all()] == [b.key]
