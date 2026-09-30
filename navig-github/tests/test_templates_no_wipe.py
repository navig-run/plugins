"""A transient/unreadable read of templates.json must NOT wipe user-authored templates.

TemplateManager loads templates.json in __init__; the old `_load_custom_templates` caught
only (JSONDecodeError, KeyError), so a truncated file (from the old NON-atomic write) read
back as corrupt → `[]`, and the next `template create`/`delete` saved just that one over the
whole file — wiping the rest. A pure transient lock (PermissionError) wasn't caught at all
and crashed construction.

Now the load routes through json_io.load_json_for_update (RAISES JsonReadError on an
unreadable-but-populated file, quarantines a corrupt one), sets _load_failed, and
_save_custom_templates REFUSES while flagged. _save is atomic, so it can't truncate.
"""

from __future__ import annotations

import navig.core.json_io as jio
from navig_github.engine.templates import BackupTemplate, TemplateManager


def _tpl(tid: str) -> BackupTemplate:
    return BackupTemplate(id=tid, name=tid.upper(), description="d", category="custom")


def _seed_two(tmp_path):
    m = TemplateManager(config_dir=tmp_path)
    m.add_custom(_tpl("alpha"))
    m.add_custom(_tpl("beta"))
    return m


def test_add_custom_is_additive(tmp_path):
    _seed_two(tmp_path)
    fresh = TemplateManager(config_dir=tmp_path)
    assert {t.id for t in fresh.list_custom()} == {"alpha", "beta"}


def test_transient_lock_at_load_refuses_save_without_wiping(monkeypatch, tmp_path):
    _seed_two(tmp_path)
    store = tmp_path / TemplateManager.CUSTOM_TEMPLATES_FILE
    before = store.read_text(encoding="utf-8")
    assert "alpha" in before and "beta" in before

    def _locked(*_a, **_k):
        raise OSError("file is locked")

    monkeypatch.setattr(jio, "read_text_retrying", _locked)

    mgr = TemplateManager(config_dir=tmp_path)   # construction load hits the lock
    assert mgr._load_failed is True
    mgr.add_custom(_tpl("gamma"))                # append + save must be REFUSED

    # Store on disk intact (read directly — Path.read_text, not the patched json_io).
    assert store.read_text(encoding="utf-8") == before

    monkeypatch.undo()
    fresh = TemplateManager(config_dir=tmp_path)  # lock lifted → real templates return
    assert {t.id for t in fresh.list_custom()} == {"alpha", "beta"}  # gamma never persisted


def test_remove_custom_during_lock_does_not_wipe(monkeypatch, tmp_path):
    _seed_two(tmp_path)
    store = tmp_path / TemplateManager.CUSTOM_TEMPLATES_FILE
    before = store.read_text(encoding="utf-8")

    def _locked(*_a, **_k):
        raise OSError("file is locked")

    monkeypatch.setattr(jio, "read_text_retrying", _locked)

    mgr = TemplateManager(config_dir=tmp_path)
    assert mgr._load_failed is True
    assert mgr.remove_custom("alpha") is False   # empty list → nothing removed, no save
    assert store.read_text(encoding="utf-8") == before  # both survive
