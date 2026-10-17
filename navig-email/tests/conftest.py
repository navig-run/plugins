import sys
from pathlib import Path

import pytest

# navig_email is editable-installed against the MAIN checkout; put THIS worktree's
# package first so tests exercise the code under test, not the installed copy.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))  # `from fake_gmail import …`


@pytest.fixture(autouse=True)
def _isolated_mailroom(tmp_path, monkeypatch):
    """No test may touch the operator's real mailroom store or notify router.

    Without this a CLI test that sends through the outbox wrote ``~/.navig/data/mailroom.db``
    and posted "Reply sent" into the live notify feed. Every test gets its own store, and the
    outbox notifier records events instead of dispatching them (``mailroom_events``).
    """
    from navig_email import outbox

    monkeypatch.setenv("NAVIG_MAILROOM_DB", str(tmp_path / "mailroom-test.db"))
    events: list[tuple[str, str, str]] = []
    monkeypatch.setattr(outbox, "NOTIFIER", lambda ev, job, note="": events.append((ev, job["job_id"], note)))
    yield events


@pytest.fixture
def mailroom_events(_isolated_mailroom):
    return _isolated_mailroom
