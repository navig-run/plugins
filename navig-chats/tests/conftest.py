"""Shared fixtures.

navig, navig-sdk and (once installed) this plugin are editable-installed against the MAIN
checkout. Put THIS tree's copies first so a worktree tests the code under change.

``make_export`` builds conversations in the exact shape of ChatGPT's account export: a
``mapping`` tree with a hidden system root, regenerated answers as sibling branches, and
``current_node`` pointing at the shown leaf.
"""

from __future__ import annotations

import json
import sys
import zipfile
from pathlib import Path

import pytest

_PLUGIN = Path(__file__).resolve().parents[1]
_REPO = _PLUGIN.parents[1]
for _root in (_REPO / "core", _REPO / "registry" / "sdk" / "python", _PLUGIN):
    if _root.is_dir() and str(_root) not in sys.path:
        sys.path.insert(0, str(_root))


def _node(nid, parent, children, role=None, text=None, t=0.0, *, content=None, meta=None):
    msg = None
    if role is not None:
        msg = {
            "id": nid,
            "author": {"role": role, "name": None, "metadata": {}},
            "create_time": t,
            "update_time": None,
            "content": content or {"content_type": "text", "parts": [text]},
            "status": "finished_successfully",
            "end_turn": True,
            "weight": 1.0,
            "metadata": meta or {},
            "recipient": "all",
        }
    return {"id": nid, "message": msg, "parent": parent, "children": children}


def make_conversation(cid: str, title: str, turns: list[tuple[str, str]], *, t0: float = 1_700_000_000.0,
                      branch_at: int | None = None, branch_text: str = "an older answer", project: str | None = None):
    """A ChatGPT export object. ``turns`` alternate user/assistant; ``branch_at`` adds a
    regenerated (NOT shown) sibling answer next to that assistant turn."""
    mapping = {"root": _node("root", None, ["sys"])}
    mapping["sys"] = _node("sys", "root", [], "system", "", t0,
                           meta={"is_visually_hidden_from_conversation": True})
    parent = "sys"
    for i, (role, text) in enumerate(turns):
        nid = f"{cid}-m{i}"
        mapping[nid] = _node(nid, parent, [], role, text, t0 + i * 10)
        mapping[parent]["children"].append(nid)
        if branch_at == i:
            alt = f"{cid}-alt{i}"
            mapping[alt] = _node(alt, parent, [], role, branch_text, t0 + i * 10 - 5)
            mapping[parent]["children"].insert(0, alt)
        parent = nid
    conv = {
        "title": title,
        "create_time": t0,
        "update_time": t0 + len(turns) * 10,
        "mapping": mapping,
        "moderation_results": [],
        "current_node": parent,
        "conversation_id": cid,
        "id": cid,
        "is_archived": False,
        "default_model_slug": "gpt-4o",
    }
    if project:
        conv["conversation_template_id"] = project
        conv["gizmo_type"] = "snorlax"
    return conv


def write_export_zip(path: Path, conversations: list[dict], *, split: int | None = None, user_id: str = "user-abc") -> Path:
    with zipfile.ZipFile(path, "w") as zf:
        if split:
            for n, start in enumerate(range(0, len(conversations), split)):
                zf.writestr(f"conversations-{n:03d}.json", json.dumps(conversations[start : start + split]))
        else:
            zf.writestr("conversations.json", json.dumps(conversations))
        zf.writestr("user.json", json.dumps({"id": user_id, "email": "someone@example.com"}))
        zf.writestr("chat.html", "<html></html>")
    return path


@pytest.fixture()
def db_path(tmp_path, monkeypatch):
    """A throwaway chats.db, and no reach into the real navig data dir."""
    path = tmp_path / "chats.db"
    monkeypatch.setenv("NAVIG_CHATS_DB", str(path))
    monkeypatch.setenv("NAVIG_DATA_DIR", str(tmp_path / "data"))
    from navig_chats import deck_routes
    from navig_chats.commands import chats as cmd

    cmd._STORES.clear()
    deck_routes._STORE.clear()
    yield path
    for store in [*cmd._STORES.values(), *deck_routes._STORE.values()]:
        store.close()
    cmd._STORES.clear()
    deck_routes._STORE.clear()


@pytest.fixture()
def store(db_path):
    from navig_chats.store import ChatStore

    s = ChatStore(db_path)
    assert s.db_path == db_path  # the resolved path, before the first write
    yield s
    s.close()


@pytest.fixture()
def sample_export(tmp_path) -> Path:
    convs = [
        make_conversation(
            "c-rust", "Rust lifetimes",
            [("user", "How do lifetimes work in Rust borrow checking?"),
             ("assistant", "Lifetimes describe how long references are valid.\n\n```rust\nfn longest<'a>(x: &'a str) -> &'a str { x }\n```"),
             ("user", "And elision?"),
             ("assistant", "Elision rules let the compiler infer lifetimes.")],
            branch_at=1, branch_text="A regenerated answer about ownership",
        ),
        make_conversation(
            "c-db", "Database choice",
            [("user", "Should we use Postgres or SQLite for the local cache?"),
             ("assistant", "We decided: SQLite with FTS5 for the local index, Postgres for the server.")],
            t0=1_710_000_000.0, project="g-p-abc123",
        ),
        make_conversation(
            "c-trip", "Trip planning",
            [("user", "Plan a weekend in Lisbon"), ("assistant", "Day one: Alfama and the tram.")],
            t0=1_720_000_000.0,
        ),
    ]
    return write_export_zip(tmp_path / "export.zip", convs)
