"""SSO-linked website logins (`navig vault login capture`).

The three invariants that must hold:

  * a LEGACY payload (no SSO keys) still reads as a password login — the new
    fields are defaults, never a reinterpretation of old data;
  * a session capture can NEVER replace a stored password with an empty one;
  * the SSO login record and the captured session share one account label, or
    autofill's session lookup misses a session that was captured.
"""

from __future__ import annotations

import json

import pytest

from navig_vault.core import Vault
from navig_vault.logins import (
    WebLogin,
    add_login,
    add_sso_login,
    detect_api_ref,
    get_login,
    infer_idp,
    list_logins,
)
from navig_vault.sessions import get_session, save_session, session_label


@pytest.fixture
def vault(tmp_path):
    v = Vault(tmp_path)
    try:
        yield v
    finally:
        v._store.close()


# ── legacy payload compatibility ────────────────────────────────────────────


def test_legacy_payload_reads_as_password_login(vault):
    legacy = json.dumps(
        {
            "domain": "github.com",
            "username": "you",
            "password": "s3cret",
            "url": None,
            "totp_secret": None,
        }
    ).encode()
    login = WebLogin.from_payload(legacy)
    assert login.auth_method == "password"
    assert login.idp is None
    assert login.idp_subject is None
    assert login.session_ref is None
    assert login.password == "s3cret"  # untouched


def test_legacy_login_stored_without_auth_method_metadata_lists_as_password(vault):
    # Simulate a row written before this feature: metadata has no auth_method key.
    from navig_vault.logins import LOGIN_PROVIDER
    from navig_vault.types import VaultItemKind

    v = vault
    v.put(
        "web/old.example/you",
        WebLogin(domain="old.example", username="you", password="x").to_payload(),
        kind=VaultItemKind.PASSWORD,
        provider=LOGIN_PROVIDER,
        metadata={"domain": "old.example", "username": "you", "type": "web_login"},
    )
    infos = list_logins(vault=v)
    assert len(infos) == 1
    assert infos[0].auth_method == "password"
    assert infos[0].idp is None


# ── SSO record round-trip ───────────────────────────────────────────────────


def test_sso_login_roundtrip(vault):
    sid = add_sso_login(
        "https://Notion.so/app",
        "you@example.com",
        idp="Google",
        idp_subject="you@gmail.com",
        url="https://notion.so/login",
        vault=vault,
    )
    assert sid

    login = get_login("notion.so", "you@example.com", vault=vault)
    assert login is not None
    assert login.auth_method == "sso"
    assert login.password == ""  # nothing to hold — there IS no password
    assert login.idp == "google"  # normalised
    assert login.idp_subject == "you@gmail.com"
    assert login.session_ref == session_label("notion.so", "you@example.com")

    infos = list_logins(vault=vault)
    assert len(infos) == 1
    assert infos[0].auth_method == "sso"
    assert infos[0].idp == "google"  # plaintext metadata — no decrypt to list


def test_sso_login_refuses_to_clobber_a_stored_password(vault):
    add_login(WebLogin(domain="github.com", username="you", password="s3cret"), vault=vault)
    with pytest.raises(ValueError, match="password login already exists"):
        add_sso_login("github.com", "you", idp="github", vault=vault)
    kept = get_login("github.com", "you", vault=vault)
    assert kept is not None
    assert kept.password == "s3cret"
    assert kept.auth_method == "password"


def test_sso_record_and_session_share_one_account_label(vault):
    # autofill resolves the session by the login's username; a label mismatch
    # would make restore miss a session that was captured seconds ago.
    add_sso_login("example.com", None, idp="google", vault=vault)
    save_session("example.com", {"cookies": []}, username=None, vault=vault)
    login = get_login("example.com", "default", vault=vault)
    assert login is not None
    assert login.session_ref == session_label("example.com", "default")
    assert get_session("example.com", login.username, vault=vault) is not None


# ── IdP inference (best-effort metadata, never an auth decision) ────────────


@pytest.mark.parametrize(
    "state, expect",
    [
        ({"cookies": [{"host": "accounts.google.com", "name": "SID"}]}, "google"),
        ({"cookies": [{"host": ".login.live.com", "name": "uaid"}]}, "microsoft"),
        ({"cookies": [{"host": "login.microsoftonline.com", "name": "esctx"}]}, "microsoft"),
        ({"cookies": [{"host": "github.com", "name": "logged_in"}]}, "github"),
        ({"cookies": [{"host": ".apple.com", "name": "myacinfo.2u"}]}, "apple"),
        # anonymous GitHub session cookie — NOT a sign-in signal
        ({"cookies": [{"host": "github.com", "name": "_gh_sess"}]}, None),
        # analytics on a google-ish host must not claim google
        ({"cookies": [{"host": "www.google-analytics.com", "name": "_ga"}]}, None),
        ({"cookies": [{"host": "example.com", "name": "session"}]}, None),
        ({}, None),
        (None, None),
    ],
)
def test_infer_idp(state, expect):
    assert infer_idp(state) == expect


# ── api_ref: the pointer to a stored API credential ──────────────────────────


def test_detect_api_ref_finds_the_brand_prefix_provider(vault):
    vault.add("notion", "api_key", {"token": "secret-not-the-pointer"})
    assert detect_api_ref("https://notion.so/app", vault=vault) == "notion"


def test_detect_api_ref_is_none_without_a_credential(vault):
    assert detect_api_ref("example.com", vault=vault) is None


def test_detect_api_ref_ignores_disabled_only_credentials(vault):
    vault.add("ghost", "api_key", {"token": "x"})
    # disable every credential under the provider
    for it in vault.list(provider="ghost"):
        vault.update(it.id, metadata={"enabled": False})
    assert detect_api_ref("ghost.io", vault=vault) is None


def test_detect_api_ref_never_returns_a_secret(vault):
    vault.add("github", "token", {"token": "ghp_observable"})
    ref = detect_api_ref("github.com", vault=vault)
    assert ref == "github"  # a provider id, never the token value
    assert "ghp_" not in str(ref)


def test_sso_login_roundtrips_api_ref(vault):
    add_sso_login("notion.so", "you", idp="google", api_ref="notion", vault=vault)
    login = get_login("notion.so", "you", vault=vault)
    assert login is not None and login.api_ref == "notion"
    infos = list_logins(vault=vault)
    assert infos[0].api_ref == "notion"  # plaintext metadata — no decrypt to list


def test_legacy_payload_has_no_api_ref():
    legacy = json.dumps({"domain": "x.com", "username": "u", "password": "p"}).encode()
    assert WebLogin.from_payload(legacy).api_ref is None
