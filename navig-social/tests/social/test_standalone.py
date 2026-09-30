"""navig-social on its own: advice names real commands, and navig-only paths say so."""

from __future__ import annotations

import asyncio
import importlib
import sys

import pytest


@pytest.fixture
def no_navig(monkeypatch):
    """navig absent, as for `pip install navig-social` alone.

    Blocks rather than evicts: a ``None`` entry makes every ``import navig…`` raise, and
    ``setitem`` restores exactly what was there (an eviction would leave anything imported
    meanwhile orphaned — see core's test_sys_modules_eviction.py).
    """
    for mod in [m for m in sys.modules if m == "navig" or m.startswith("navig.")]:
        monkeypatch.setitem(sys.modules, mod, None)
    monkeypatch.setitem(sys.modules, "navig", None)
    from navig_sdk import host

    assert not host.navig_available()


def _fresh_hints():
    import navig_social._hints as hints

    return importlib.reload(hints)


def test_hints_name_the_standalone_commands(no_navig):
    hints = _fresh_hints()
    assert hints.CMD == "navig-social"
    assert hints.FB == "navig-social facebook"
    assert hints.vault_set("devto", "<API_KEY>") == "navig-vault add devto"
    assert (
        hints.config_set("adapters.social.facebook.page_id", "<PAGE_ID>")
        == "set NAVIG_FACEBOOK_PAGE_ID=<PAGE_ID>"
    )


def test_hints_inside_navig_are_navigs_commands(monkeypatch):
    from navig_sdk import host

    monkeypatch.setattr(host, "navig_available", lambda: True)
    hints = _fresh_hints()
    try:
        assert hints.CMD == "navig social"
        assert hints.FB == "navig facebook"
        assert hints.vault_set("devto", "<API_KEY>") == "navig vault set devto <API_KEY>"
        assert hints.config_set("adapters.social.x.y", "v") == "navig config set adapters.social.x.y v"
    finally:
        monkeypatch.undo()
        _fresh_hints()


def test_app_credentials_fall_back_to_the_environment(monkeypatch):
    """A path item (`linkedin/client_id`) can only be created with `navig vault set`; on its
    own the environment carries it, and the vault still wins when it has the value."""
    from navig_social.social import oauth

    class _Vault:
        def __init__(self, items):
            self.items = items

        def get_secret(self, path):
            if path not in self.items:
                raise KeyError(path)
            return type("S", (), {"reveal": lambda self, v=self.items[path]: v})()

    monkeypatch.setattr(oauth, "_vault", lambda: _Vault({}))
    monkeypatch.setenv("LINKEDIN_CLIENT_ID", "from-env")
    assert oauth.read_secret("linkedin/client_id") == "from-env"
    monkeypatch.setattr(oauth, "_vault", lambda: _Vault({"linkedin/client_id": "from-vault"}))
    assert oauth.read_secret("linkedin/client_id") == "from-vault"


def test_missing_app_credentials_name_the_variables_on_its_own(monkeypatch, no_navig):
    from navig_social.social import oauth

    monkeypatch.setattr(oauth, "_vault", lambda: (_ for _ in ()).throw(RuntimeError("locked")))
    for name in ("LINKEDIN_CLIENT_ID", "NAVIG_LINKEDIN_CLIENT_ID", "LINKEDIN_CLIENT_SECRET",
                 "NAVIG_LINKEDIN_CLIENT_SECRET"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(oauth, "navig_available", lambda: False)
    with pytest.raises(oauth.SocialOAuthError) as exc:
        oauth._require_creds("linkedin/client_id", "linkedin/client_secret")
    assert "set LINKEDIN_CLIENT_ID=…, LINKEDIN_CLIENT_SECRET=…" in str(exc.value)


def test_messaging_networks_say_they_need_navig(no_navig):
    from navig_social.social.dispatcher import PublishDispatcher
    from navig_social.social.types import PostContent

    receipts = asyncio.run(
        PublishDispatcher().publish(PostContent(body="hi"), [{"network": "discord", "target": "c1"}])
    )
    assert len(receipts) == 1 and not receipts[0].ok
    assert receipts[0].error == "discord posting needs navig (pip install navig)"


def test_a_space_folder_works_without_navig(no_navig, tmp_path):
    from navig_social import roster

    assert roster.resolve_space_dir(str(tmp_path)) == tmp_path.resolve()
    with pytest.raises(ValueError, match="pass the space's folder path"):
        roster.resolve_space_dir("human-presence")


def test_browser_stats_use_navig_browser_when_navig_is_absent(monkeypatch):
    """Browser-backed stats shell out to `navig cdp …`; on its own the same commands (and the
    same --json) are `navig-browser …`, so a crawl works with navig-browser alone."""
    import subprocess

    from navig_social import stats

    ran = []
    monkeypatch.setattr(stats.shutil, "which",
                        lambda name: {"navig-browser": "/bin/navig-browser"}.get(name))
    monkeypatch.setattr(subprocess, "run", lambda cmd, **kw: ran.append(cmd))
    stats._run_navig(["cdp", "profile", "use", "social-stats", "--json"])
    assert ran == [["/bin/navig-browser", "profile", "use", "social-stats", "--json"]]

    monkeypatch.setattr(stats.shutil, "which", lambda name: {"navig": "/bin/navig"}.get(name))
    stats._run_navig(["cdp", "stop", "--port", "9301"])
    assert ran[-1] == ["/bin/navig", "cdp", "stop", "--port", "9301"]
