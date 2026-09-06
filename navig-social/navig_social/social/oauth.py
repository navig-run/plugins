"""Browser OAuth connect flows for the social publishers.

One command connects an account: read the app credentials from the vault
(``<provider>/app_id`` / ``<provider>/client_secret`` …), run the provider's
authorization-code flow, then store the resulting ACCESS TOKEN in the vault's
``<provider>`` default slot — exactly where :mod:`.credentials` resolves it —
plus any refresh token (``<provider>/refresh_token``) and non-secret ids
(``adapters.social.<provider>.*`` config).

Two authorization modes:

- **loopback** (default): a throwaway localhost HTTP server on the same
  port/path the Cloudflare login uses (``http://localhost:8976/oauth/callback``)
  captures the redirect. Register that URL once in each provider's app
  settings (Google desktop clients and Facebook apps in dev mode accept
  localhost automatically).
- **manual** (``--manual``, forced for Threads which rejects http redirects):
  the auth URL is printed, the user authorizes, then pastes the full redirect
  URL (or bare ``code``) back into the prompt — works with ANY registered
  https redirect even if that page 404s.

Module top-level is stdlib-only; navig imports stay lazy (lazy-import law).
"""
from __future__ import annotations

import base64
import hashlib
import json
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, HTTPServer

CALLBACK_PORT = 8976  # same fixed port as navig's Cloudflare OAuth login
REDIRECT_URI = f"http://localhost:{CALLBACK_PORT}/oauth/callback"
GRAPH = "https://graph.facebook.com/v25.0"
THREADS_GRAPH = "https://graph.threads.net"

_FB_SCOPES = [
    "pages_show_list", "pages_read_engagement", "pages_read_user_content",
    "pages_manage_metadata", "pages_manage_posts",
]


class SocialOAuthError(Exception):
    """Authorization / token-exchange failure. Never carries a secret."""


@dataclass
class ConnectResult:
    provider: str
    account: str
    lifetime: str
    notes: list[str] = field(default_factory=list)


# ── vault / config helpers (lazy) ─────────────────────────────────────────────


def _vault():
    from navig.vault.core import get_vault

    return get_vault()


def read_secret(path: str) -> str | None:
    """Read a vault secret by path (``linkedin/client_id``) or provider slot."""
    try:
        return _vault().get_secret(path).reveal() or None
    except Exception:  # noqa: BLE001 — absent/locked → not configured
        return None


def write_provider_token(provider: str, token: str, label: str) -> None:
    """Store *token* in the ``<provider>`` default slot (what get_token reads)."""
    v = _vault()
    existing = v._store.get(provider)  # noqa: SLF001 — label-exact lookup
    if existing is not None:
        v.update(existing.id, data={"value": token}, label=label)
    else:
        v.add(provider=provider, credential_type="token",
              data={"value": token}, label=label)


def write_path_secret(path: str, value: str) -> None:
    """Store a secret at an explicit path item (e.g. ``youtube/refresh_token``)."""
    _vault().put(path, json.dumps({"value": value}).encode())


def delete_secret(path: str) -> bool:
    try:
        return bool(_vault().delete(path))
    except Exception:  # noqa: BLE001
        return False


def set_config(provider: str, key: str, value: str) -> None:
    """Persist a non-secret field at ``adapters.social.<provider>.<key>``.

    Deep-sets only the leaf in the live global config and re-saves — the same
    approach ``navig config set`` uses — so sibling keys (e.g. another provider's
    page_id) are never clobbered by a shallow dict update.
    """
    from navig.config import get_config_manager

    cm = get_config_manager()
    cfg = cm.global_config
    node = cfg
    for part in ("adapters", "social", provider):
        if not isinstance(node.get(part), dict):
            node[part] = {}
        node = node[part]
    node[key] = value
    cm._save_global_config(cfg)


# ── http helpers (stdlib only) ────────────────────────────────────────────────


def _request_json(method: str, url: str, *, form: dict | None = None,
                  headers: dict | None = None, timeout: float = 30.0) -> dict:
    data = urllib.parse.urlencode(form).encode() if form is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
            body = resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")
        try:
            err = json.loads(body)
        except ValueError:
            err = {}
        detail = (err.get("error_description") or err.get("error_message")
                  or (err.get("error") or {}).get("message") if isinstance(err.get("error"), dict)
                  else err.get("error")) or body[:200]
        raise SocialOAuthError(f"HTTP {exc.code}: {detail}") from None
    except urllib.error.URLError as exc:
        raise SocialOAuthError(f"network error: {exc.reason}") from None
    try:
        return json.loads(body)
    except ValueError:
        # some token endpoints answer form-encoded
        return dict(urllib.parse.parse_qsl(body))


def _get_json(url: str, headers: dict | None = None) -> dict:
    return _request_json("GET", url, headers=headers)


def _post_form(url: str, form: dict, *, basic: tuple[str, str] | None = None) -> dict:
    headers = {"Content-Type": "application/x-www-form-urlencoded"}
    if basic:
        raw = f"{basic[0]}:{basic[1]}".encode()
        headers["Authorization"] = "Basic " + base64.b64encode(raw).decode()
    return _request_json("POST", url, form=form, headers=headers)


# ── authorization (loopback + manual) ─────────────────────────────────────────


def _pkce() -> tuple[str, str]:
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(48)).rstrip(b"=").decode()
    digest = hashlib.sha256(verifier.encode()).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    return verifier, challenge


_DONE_HTML = (
    b"<!doctype html><meta charset=utf-8><title>NAVIG</title>"
    b"<body style='font-family:system-ui;background:#050505;color:#eee;"
    b"display:flex;align-items:center;justify-content:center;height:100vh'>"
    b"<div style='text-align:center'><div style='font-size:42px'>\xf0\x9f\x8c\x90</div>"
    b"<h2>Account connected</h2><p>You can close this tab and return to NAVIG.</p></div>"
)


class _CallbackHandler(BaseHTTPRequestHandler):
    result: dict[str, str] = {}

    def do_GET(self):  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path != "/oauth/callback":
            self.send_response(404)
            self.end_headers()
            return
        params = urllib.parse.parse_qs(parsed.query)
        _CallbackHandler.result = {k: v[0] for k, v in params.items()}
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(_DONE_HTML)

    def log_message(self, *args):  # silence default stderr logging
        return


def build_auth_url(base: str, params: dict) -> str:
    return base + "?" + urllib.parse.urlencode(params)


def parse_manual_reply(reply: str, expected_state: str | None = None) -> str:
    """Extract the auth ``code`` from a pasted redirect URL (or bare code)."""
    reply = reply.strip().strip('"').strip("'")
    if not reply:
        raise SocialOAuthError("nothing pasted")
    if "://" in reply or "code=" in reply:
        query = urllib.parse.urlparse(reply).query if "://" in reply else reply
        params = dict(urllib.parse.parse_qsl(query))
        if params.get("error"):
            raise SocialOAuthError(params.get("error_description") or params["error"])
        code = params.get("code")
        if not code:
            raise SocialOAuthError("no ?code= in the pasted URL")
        if expected_state and params.get("state") and params["state"] != expected_state:
            raise SocialOAuthError("OAuth state mismatch — aborting for safety.")
        # Threads appends #_ to redirect URLs; strip any fragment leftovers.
        return code.split("#")[0]
    return reply.split("#")[0]


def _browser_authorize(auth_url: str, state: str, timeout: float) -> str:
    """Open *auth_url*, capture the loopback redirect, return the auth code."""
    _CallbackHandler.result = {}
    try:
        server = HTTPServer(("127.0.0.1", CALLBACK_PORT), _CallbackHandler)
    except OSError as exc:
        raise SocialOAuthError(
            f"Can't bind localhost:{CALLBACK_PORT} for the OAuth callback ({exc}). "
            "Close whatever is using it, or retry with --manual."
        ) from exc
    server.timeout = 1.0

    import webbrowser

    try:
        webbrowser.open(auth_url)
    except Exception:  # noqa: BLE001
        pass

    deadline = time.monotonic() + timeout
    try:
        while not _CallbackHandler.result and time.monotonic() < deadline:
            server.handle_request()
    finally:
        server.server_close()

    result = _CallbackHandler.result
    if not result:
        raise SocialOAuthError("Timed out waiting for authorization in the browser.")
    if result.get("error"):
        raise SocialOAuthError(result.get("error_description") or result["error"])
    if result.get("state") != state:
        raise SocialOAuthError("OAuth state mismatch — aborting for safety.")
    code = result.get("code")
    if not code:
        raise SocialOAuthError("No authorization code returned.")
    return code


def _manual_authorize(auth_url: str, state: str) -> str:
    import typer

    from navig.lazy_loader import lazy_import

    ch = lazy_import("navig.console_helper")
    ch.info("Open this URL, authorize, then paste the URL you were redirected to:")
    ch.console.print(f"\n[bold cyan]{auth_url}[/bold cyan]\n")
    import webbrowser

    try:
        webbrowser.open(auth_url)
    except Exception:  # noqa: BLE001
        pass
    reply = typer.prompt("Paste the redirect URL (or just the code)")
    return parse_manual_reply(reply, expected_state=state)


def _authorize(auth_base: str, params: dict, *, manual: bool, timeout: float) -> str:
    state = secrets.token_urlsafe(24)
    params = {**params, "state": state}
    url = build_auth_url(auth_base, params)
    if manual:
        return _manual_authorize(url, state)
    return _browser_authorize(url, state, timeout)


def _require_creds(*paths: str) -> list[str]:
    vals, missing = [], []
    for p in paths:
        v = read_secret(p)
        vals.append(v)
        if not v:
            missing.append(p)
    if missing:
        raise SocialOAuthError(
            "missing app credentials in the vault: "
            + ", ".join(missing)
            + "  (add with: navig vault set <path> <value>)"
        )
    return vals  # type: ignore[return-value]


# ── providers ─────────────────────────────────────────────────────────────────


def connect_facebook(*, manual: bool = False, timeout: float = 240.0) -> ConnectResult:
    """Full self-serve Facebook Page connect: dialog → long-lived PAGE token."""
    app_id, app_secret = _require_creds("facebook/app_id", "facebook/app_secret")
    code = _authorize(
        "https://www.facebook.com/v25.0/dialog/oauth",
        {"client_id": app_id, "redirect_uri": REDIRECT_URI,
         "response_type": "code", "scope": ",".join(_FB_SCOPES)},
        manual=manual, timeout=timeout,
    )
    tok = _get_json(f"{GRAPH}/oauth/access_token?" + urllib.parse.urlencode({
        "client_id": app_id, "client_secret": app_secret,
        "redirect_uri": REDIRECT_URI, "code": code}))
    long = _get_json(f"{GRAPH}/oauth/access_token?" + urllib.parse.urlencode({
        "grant_type": "fb_exchange_token", "client_id": app_id,
        "client_secret": app_secret, "fb_exchange_token": tok["access_token"]}))
    user_token = long["access_token"]
    page = _pick_page(user_token)
    write_provider_token("facebook", page["access_token"],
                         f"Facebook Page token ({page['name']}, long-lived)")
    set_config("facebook", "page_id", str(page["id"]))
    return ConnectResult("facebook", f"{page['name']} (page {page['id']})",
                         "long-lived (~60 days; page tokens often outlive that)")


def connect_instagram(*, manual: bool = False, timeout: float = 240.0) -> ConnectResult:
    """Instagram professional account via Facebook Login (uses the Meta app)."""
    app_id, app_secret = _require_creds("facebook/app_id", "facebook/app_secret")
    scopes = _FB_SCOPES + ["instagram_basic", "instagram_content_publish"]
    code = _authorize(
        "https://www.facebook.com/v25.0/dialog/oauth",
        {"client_id": app_id, "redirect_uri": REDIRECT_URI,
         "response_type": "code", "scope": ",".join(scopes)},
        manual=manual, timeout=timeout,
    )
    tok = _get_json(f"{GRAPH}/oauth/access_token?" + urllib.parse.urlencode({
        "client_id": app_id, "client_secret": app_secret,
        "redirect_uri": REDIRECT_URI, "code": code}))
    long = _get_json(f"{GRAPH}/oauth/access_token?" + urllib.parse.urlencode({
        "grant_type": "fb_exchange_token", "client_id": app_id,
        "client_secret": app_secret, "fb_exchange_token": tok["access_token"]}))
    user_token = long["access_token"]
    pages = _get_json(f"{GRAPH}/me/accounts?" + urllib.parse.urlencode({
        "fields": "id,name,instagram_business_account{id,username}",
        "access_token": user_token})).get("data", [])
    with_ig = [p for p in pages if p.get("instagram_business_account")]
    if not with_ig:
        raise SocialOAuthError(
            "no Facebook Page with a linked Instagram professional account — "
            "link one at facebook.com/<page> → Settings → Linked accounts.")
    page = _choose("Instagram-linked Pages", with_ig) if len(with_ig) > 1 else with_ig[0]
    ig = page["instagram_business_account"]
    write_provider_token("instagram", user_token,
                         f"Instagram token (@{ig.get('username', ig['id'])}, long-lived)")
    set_config("instagram", "ig_user_id", str(ig["id"]))
    return ConnectResult("instagram", f"@{ig.get('username', ig['id'])} (via {page['name']})",
                         "long-lived (~60 days)")


def connect_threads(*, manual: bool = True, timeout: float = 240.0) -> ConnectResult:
    """Threads connect. Threads rejects http:// redirects → manual mode.

    Register ANY https redirect URI in the Threads app (your Lighthouse URL
    works); after authorizing, paste the redirected URL back here.
    """
    app_id, app_secret = _require_creds("threads/app_id", "threads/app_secret")
    redirect = _threads_redirect()
    code = _authorize(
        "https://threads.net/oauth/authorize",
        {"client_id": app_id, "redirect_uri": redirect,
         "response_type": "code",
         "scope": ",".join(["threads_basic", "threads_content_publish"])},
        manual=True, timeout=timeout,  # loopback impossible: https required
    )
    tok = _post_form(f"{THREADS_GRAPH}/oauth/access_token", {
        "client_id": app_id, "client_secret": app_secret,
        "grant_type": "authorization_code", "redirect_uri": redirect, "code": code})
    long = _get_json(f"{THREADS_GRAPH}/access_token?" + urllib.parse.urlencode({
        "grant_type": "th_exchange_token", "client_secret": app_secret,
        "access_token": tok["access_token"]}))
    token = long.get("access_token") or tok["access_token"]
    me = _get_json(f"{THREADS_GRAPH}/v1.0/me?" + urllib.parse.urlencode({
        "fields": "id,username", "access_token": token}))
    write_provider_token("threads", token,
                         f"Threads token (@{me.get('username', me.get('id'))}, long-lived)")
    if me.get("id"):
        set_config("threads", "user_id", str(me["id"]))
    return ConnectResult("threads", f"@{me.get('username', me.get('id'))}",
                         "long-lived (60 days — `navig social refresh threads` renews)")


def connect_linkedin(*, manual: bool = False, timeout: float = 240.0) -> ConnectResult:
    client_id, client_secret = _require_creds("linkedin/client_id", "linkedin/client_secret")
    code = _authorize(
        "https://www.linkedin.com/oauth/v2/authorization",
        {"response_type": "code", "client_id": client_id,
         "redirect_uri": REDIRECT_URI,
         "scope": " ".join(["openid", "profile", "w_member_social"])},
        manual=manual, timeout=timeout,
    )
    tok = _post_form("https://www.linkedin.com/oauth/v2/accessToken", {
        "grant_type": "authorization_code", "code": code,
        "redirect_uri": REDIRECT_URI,
        "client_id": client_id, "client_secret": client_secret})
    token = tok["access_token"]
    me = _get_json("https://api.linkedin.com/v2/userinfo",
                   headers={"Authorization": f"Bearer {token}"})
    write_provider_token("linkedin", token,
                         f"LinkedIn token ({me.get('name', 'member')})")
    if me.get("sub"):
        set_config("linkedin", "author_urn", f"urn:li:person:{me['sub']}")
    if tok.get("refresh_token"):
        write_path_secret("linkedin/refresh_token", tok["refresh_token"])
    days = int(tok.get("expires_in", 0)) // 86400
    return ConnectResult("linkedin", me.get("name", "member"),
                         f"~{days or 60} days" + (" (+refresh token stored)" if tok.get("refresh_token") else ""),
                         notes=["Register http://localhost:8976/oauth/callback as an "
                                "Authorized redirect URL in the LinkedIn app (once)."])


def connect_youtube(*, manual: bool = False, timeout: float = 240.0) -> ConnectResult:
    client_id, client_secret = _require_creds("youtube/client_id", "youtube/client_secret")
    verifier, challenge = _pkce()
    code = _authorize(
        "https://accounts.google.com/o/oauth2/v2/auth",
        {"response_type": "code", "client_id": client_id,
         "redirect_uri": REDIRECT_URI,
         "scope": " ".join(["https://www.googleapis.com/auth/youtube.upload",
                            "https://www.googleapis.com/auth/youtube.readonly"]),
         "access_type": "offline", "prompt": "consent",
         "code_challenge": challenge, "code_challenge_method": "S256"},
        manual=manual, timeout=timeout,
    )
    tok = _post_form("https://oauth2.googleapis.com/token", {
        "grant_type": "authorization_code", "code": code,
        "redirect_uri": REDIRECT_URI, "client_id": client_id,
        "client_secret": client_secret, "code_verifier": verifier})
    write_provider_token("youtube", tok["access_token"], "YouTube access token")
    if tok.get("refresh_token"):
        write_path_secret("youtube/refresh_token", tok["refresh_token"])
    chan = _get_json(
        "https://www.googleapis.com/youtube/v3/channels?part=snippet&mine=true",
        headers={"Authorization": f"Bearer {tok['access_token']}"})
    items = chan.get("items") or []
    name = items[0]["snippet"]["title"] if items else "channel"
    return ConnectResult(
        "youtube", name,
        "1 hour (auto-renewable: refresh token stored)" if tok.get("refresh_token")
        else "1 hour (NO refresh token returned — re-consent needed)",
        notes=["App in Testing mode is fine — add your own Google account as a test user."])


def connect_pinterest(*, manual: bool = False, timeout: float = 240.0) -> ConnectResult:
    app_id, app_secret = _require_creds("pinterest/app_id", "pinterest/app_secret")
    code = _authorize(
        "https://www.pinterest.com/oauth/",
        {"response_type": "code", "client_id": app_id,
         "redirect_uri": REDIRECT_URI,
         "scope": ",".join(["boards:read", "pins:read", "pins:write", "user_accounts:read"])},
        manual=manual, timeout=timeout,
    )
    tok = _post_form("https://api.pinterest.com/v5/oauth/token", {
        "grant_type": "authorization_code", "code": code,
        "redirect_uri": REDIRECT_URI}, basic=(app_id, app_secret))
    write_provider_token("pinterest", tok["access_token"], "Pinterest access token")
    if tok.get("refresh_token"):
        write_path_secret("pinterest/refresh_token", tok["refresh_token"])
    me = _get_json("https://api.pinterest.com/v5/user_account",
                   headers={"Authorization": f"Bearer {tok['access_token']}"})
    days = int(tok.get("expires_in", 0)) // 86400
    return ConnectResult("pinterest", me.get("username", "account"),
                         f"~{days or 30} days (+refresh token stored)"
                         if tok.get("refresh_token") else f"~{days or 30} days")


def connect_devto(*, manual: bool = False, timeout: float = 240.0) -> ConnectResult:
    """dev.to has no OAuth — the stored API key IS the token; just validate it."""
    key = read_secret("devto")
    if not key:
        raise SocialOAuthError("no dev.to API key — navig vault set devto <API_KEY>")
    me = _get_json("https://dev.to/api/users/me", headers={"api-key": key})
    return ConnectResult("devto", f"@{me.get('username', 'me')}", "does not expire")


def _pick_page(user_token: str) -> dict:
    pages = _get_json(f"{GRAPH}/me/accounts?" + urllib.parse.urlencode({
        "fields": "id,name,access_token", "access_token": user_token})).get("data", [])
    if not pages:
        raise SocialOAuthError("this Facebook user manages no Pages.")
    return _choose("Pages you manage", pages) if len(pages) > 1 else pages[0]


def _choose(title: str, items: list[dict]) -> dict:
    import typer

    from navig.lazy_loader import lazy_import

    ch = lazy_import("navig.console_helper")
    ch.info(f"{title}:")
    for i, it in enumerate(items, 1):
        ch.console.print(f"  {i}. {it.get('name', it.get('id'))}")
    idx = typer.prompt("Number", type=int)
    if not 1 <= idx <= len(items):
        raise SocialOAuthError("selection out of range")
    return items[idx - 1]


def _threads_redirect() -> str:
    """Threads needs a registered https redirect; the page content is irrelevant
    (the user pastes the redirected URL back). Configurable, sane default."""
    try:
        from navig_social.social.credentials import get_config

        return (get_config("threads", "redirect_uri")
                or "https://navig-lighthouse.studio-2bf.workers.dev/oauth/callback")
    except Exception:  # noqa: BLE001
        return "https://navig-lighthouse.studio-2bf.workers.dev/oauth/callback"


CONNECTORS = {
    "facebook": connect_facebook,
    "instagram": connect_instagram,
    "threads": connect_threads,
    "linkedin": connect_linkedin,
    "youtube": connect_youtube,
    "pinterest": connect_pinterest,
    "devto": connect_devto,
}

# providers whose auth dialog can't redirect to http://localhost
MANUAL_ONLY = {"threads"}


# ── refresh ───────────────────────────────────────────────────────────────────


def refresh_provider(provider: str) -> str:
    """Renew *provider*'s access token in place; returns a human summary."""
    token = read_secret(provider)
    if provider in ("facebook", "instagram"):
        app_id, app_secret = _require_creds("facebook/app_id", "facebook/app_secret")
        if not token:
            raise SocialOAuthError(f"no {provider} token to refresh — connect first.")
        long = _get_json(f"{GRAPH}/oauth/access_token?" + urllib.parse.urlencode({
            "grant_type": "fb_exchange_token", "client_id": app_id,
            "client_secret": app_secret, "fb_exchange_token": token}))
        write_provider_token(provider, long["access_token"],
                             f"{provider.title()} token (long-lived, refreshed)")
        return "exchanged for a fresh long-lived token (~60 days)"
    if provider == "threads":
        if not token:
            raise SocialOAuthError("no threads token to refresh — connect first.")
        new = _get_json(f"{THREADS_GRAPH}/refresh_access_token?" + urllib.parse.urlencode({
            "grant_type": "th_refresh_token", "access_token": token}))
        write_provider_token("threads", new["access_token"], "Threads token (refreshed)")
        return "refreshed (~60 days)"
    if provider == "youtube":
        client_id, client_secret = _require_creds("youtube/client_id", "youtube/client_secret")
        rt = read_secret("youtube/refresh_token")
        if not rt:
            raise SocialOAuthError("no stored refresh token — run `navig social connect youtube`.")
        tok = _post_form("https://oauth2.googleapis.com/token", {
            "grant_type": "refresh_token", "refresh_token": rt,
            "client_id": client_id, "client_secret": client_secret})
        write_provider_token("youtube", tok["access_token"], "YouTube access token (refreshed)")
        return "refreshed (1 hour)"
    if provider == "pinterest":
        app_id, app_secret = _require_creds("pinterest/app_id", "pinterest/app_secret")
        rt = read_secret("pinterest/refresh_token")
        if not rt:
            raise SocialOAuthError("no stored refresh token — run `navig social connect pinterest`.")
        tok = _post_form("https://api.pinterest.com/v5/oauth/token", {
            "grant_type": "refresh_token", "refresh_token": rt}, basic=(app_id, app_secret))
        write_provider_token("pinterest", tok["access_token"], "Pinterest token (refreshed)")
        if tok.get("refresh_token"):
            write_path_secret("pinterest/refresh_token", tok["refresh_token"])
        return "refreshed"
    if provider == "linkedin":
        client_id, client_secret = _require_creds("linkedin/client_id", "linkedin/client_secret")
        rt = read_secret("linkedin/refresh_token")
        if not rt:
            raise SocialOAuthError("no stored refresh token — run `navig social connect linkedin`.")
        tok = _post_form("https://www.linkedin.com/oauth/v2/accessToken", {
            "grant_type": "refresh_token", "refresh_token": rt,
            "client_id": client_id, "client_secret": client_secret})
        write_provider_token("linkedin", tok["access_token"], "LinkedIn token (refreshed)")
        return "refreshed"
    raise SocialOAuthError(f"no refresh flow for '{provider}'")


# ── status ────────────────────────────────────────────────────────────────────

_APP_CRED_PATHS = {
    "facebook": ("facebook/app_id", "facebook/app_secret"),
    "instagram": ("facebook/app_id", "facebook/app_secret"),
    "threads": ("threads/app_id", "threads/app_secret"),
    "linkedin": ("linkedin/client_id", "linkedin/client_secret"),
    "youtube": ("youtube/client_id", "youtube/client_secret"),
    "pinterest": ("pinterest/app_id", "pinterest/app_secret"),
    "devto": (),
}

_CHECK_CALLS = {
    "facebook": (f"{GRAPH}/me?fields=name", "query"),
    "instagram": (f"{GRAPH}/me?fields=name", "query"),
    "threads": (f"{THREADS_GRAPH}/v1.0/me?fields=username", "query"),
    "linkedin": ("https://api.linkedin.com/v2/userinfo", "bearer"),
    "youtube": ("https://www.googleapis.com/youtube/v3/channels?part=snippet&mine=true", "bearer"),
    "pinterest": ("https://api.pinterest.com/v5/user_account", "bearer"),
    "devto": ("https://dev.to/api/users/me", "api-key"),
}


def provider_status(check: bool = False) -> list[dict]:
    """Connection state per provider (optionally live-verified)."""
    out = []
    for name in CONNECTORS:
        paths = _APP_CRED_PATHS[name]
        missing = [p for p in paths if not read_secret(p)]
        token = read_secret(name)
        row = {
            "provider": name,
            "app_creds": "—" if not paths else ("missing: " + ", ".join(missing) if missing else "ok"),
            "token": bool(token),
            "live": "",
        }
        if check and token:
            url, style = _CHECK_CALLS[name]
            try:
                if style == "query":
                    sep = "&" if "?" in url else "?"
                    data = _get_json(f"{url}{sep}access_token={urllib.parse.quote(token)}")
                elif style == "bearer":
                    data = _get_json(url, headers={"Authorization": f"Bearer {token}"})
                else:
                    data = _get_json(url, headers={"api-key": token})
                row["live"] = (data.get("name") or data.get("username")
                               or (data.get("items") or [{}])[0].get("snippet", {}).get("title", "")
                               or "ok")
            except SocialOAuthError as exc:
                row["live"] = f"FAIL: {str(exc)[:60]}"
        out.append(row)
    return out
