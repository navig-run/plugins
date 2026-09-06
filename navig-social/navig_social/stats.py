"""Universal public follower/view-count fetching for `navig social stats`.

Pulls **public** follower / subscriber / view counts for ANY handle on a social
platform — no space, no folder, no OAuth-connected account required. Point it at a
platform + handle and it returns the number:

    from navig_social.stats import crawl_account
    crawl_account({"platform": "youtube", "handle": "@mkbhd"})

Three backends, tried best-first per platform:
  - ``public``  — plain HTTPS reads, no auth, no browser (github, telegram, soundcloud, kick).
    Fully deterministic; the parsers are unit-tested offline.
  - ``api``     — official platform APIs where a key/token is available (youtube, vk,
    facebook, twitch, instagram). Keys/tokens come from the environment or the navig vault.
  - ``cdp``     — drives a real browser via ``navig cdp`` for JS/consent/login-walled sites.
    Public-but-JS sites need no login; login-walled ones (tiktok, instagram, x, linkedin…)
    use ``navig cdp login <domain>`` which reads a **vaulted** website credential.

No secrets are logged. Login is delegated to the navig vault via ``navig cdp``.
(This engine was extracted from the retired navig-presence plugin; the space-bound
registry/report layer was dropped — this is the reusable stats core only.)
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Callable

HTTP_TIMEOUT = 20
CDP_TIMEOUT = 90
_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"


# --------------------------------------------------------------------------- result type

@dataclass
class StatResult:
    platform: str
    handle: str
    followers: int | None = None
    extra: dict = field(default_factory=dict)   # e.g. {"public_repos": 7, "videos": 12}
    status: str = "pending"                       # ok | needs-login | needs-cdp | error | unsupported | no-handle
    source: str = ""
    error: str | None = None

    def to_dict(self) -> dict:
        d = {"platform": self.platform, "handle": self.handle, "followers": self.followers,
             "status": self.status, "source": self.source}
        if self.extra:
            d["extra"] = self.extra
        if self.error:
            d["error"] = self.error
        return d


# --------------------------------------------------------------------------- http helpers

def _http_get(url: str, *, accept: str = "text/html", headers: dict | None = None) -> str:
    hdrs = {"User-Agent": _UA, "Accept": accept, "Accept-Language": "en-US,en;q=0.9"}
    if headers:
        hdrs.update(headers)
    req = urllib.request.Request(url, headers=hdrs)
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:  # noqa: S310 — fixed https hosts
        charset = resp.headers.get_content_charset() or "utf-8"
        return resp.read().decode(charset, errors="replace")


# --------------------------------------------------------------------------- parsers (pure, tested)

def parse_github(payload: str) -> dict:
    """Parse the GitHub REST user payload → {followers, public_repos}."""
    data = json.loads(payload)
    return {"followers": data.get("followers"), "public_repos": data.get("public_repos")}


_TG_TAG_RE = re.compile(r"<[^>]+>")
_TG_SUBS_RE = re.compile(r"([\d][\d\s.,]*?)\s*(?:subscribers|members)", re.IGNORECASE)


def parse_telegram_subscribers(html: str) -> int | None:
    """Extract a subscriber count from a public t.me channel page. Strips markup first (real
    t.me pages put the number and the word 'subscribers' in separate <span>s), and is tolerant
    of NBSP/thin-space grouping and 'subscribers'/'members' wording. None if not a public channel."""
    text = _TG_TAG_RE.sub(" ", html)
    m = _TG_SUBS_RE.search(text)
    if not m:
        return None
    digits = re.sub(r"[^\d]", "", m.group(1))
    return int(digits) if digits else None


# --------------------------------------------------------------------------- public fetchers

def fetch_github(handle: str) -> StatResult:
    h = handle.lstrip("@").strip("/").split("/")[-1]
    r = StatResult(platform="github", handle=handle, source="github-api")
    try:
        payload = _http_get(f"https://api.github.com/users/{h}", accept="application/vnd.github+json")
        parsed = parse_github(payload)
        r.followers = parsed["followers"]
        if parsed["public_repos"] is not None:
            r.extra["public_repos"] = parsed["public_repos"]
        r.status = "ok" if r.followers is not None else "error"
    except urllib.error.HTTPError as exc:
        r.status, r.error = ("error", f"HTTP {exc.code}")
    except Exception as exc:  # noqa: BLE001
        r.status, r.error = ("error", str(exc))
    return r


_SC_FOLLOWERS_RE = re.compile(r'"followers_count":\s*(\d+)')


def parse_soundcloud_followers(html: str) -> int | None:
    """SoundCloud embeds the profile in a __sc_hydration script; the profile user is first, so the
    first followers_count is the account's. Returns None if not found (private/blocked)."""
    m = _SC_FOLLOWERS_RE.search(html)
    return int(m.group(1)) if m else None


def fetch_soundcloud(handle: str) -> StatResult:
    h = handle.lstrip("@").strip("/").split("/")[-1]
    r = StatResult(platform="soundcloud", handle=handle, source="soundcloud-public")
    try:
        n = parse_soundcloud_followers(_http_get(f"https://soundcloud.com/{h}"))
        if n is not None:
            r.followers, r.status = n, "ok"
        else:
            r.status, r.error = "error", "no follower count in public page (private or changed markup)"
    except Exception as exc:  # noqa: BLE001
        r.status, r.error = "error", str(exc)
    return r


def fetch_telegram(handle: str) -> StatResult:
    h = handle.lstrip("@").strip("/").split("/")[-1]
    r = StatResult(platform="telegram", handle=handle, source="t.me-public")
    try:
        # /s/ is the public channel preview; falls back to the plain page.
        html = _http_get(f"https://t.me/s/{h}")
        subs = parse_telegram_subscribers(html)
        if subs is None:
            html = _http_get(f"https://t.me/{h}")
            subs = parse_telegram_subscribers(html)
        if subs is None:
            r.status = "error"
            r.error = "no public subscriber count (private account, user, or not a channel)"
        else:
            r.followers, r.status = subs, "ok"
    except Exception as exc:  # noqa: BLE001
        r.status, r.error = ("error", str(exc))
    return r


# --------------------------------------------------------------------------- official APIs

_YT_KEY_RE = re.compile(r"^AIza[0-9A-Za-z_\-]{35}$")  # Google API key shape


def youtube_api_key() -> str | None:
    """Resolve a YouTube Data API key. env YOUTUBE_API_KEY first, then the navig vault:
    scans the 'youtube_api_key' and 'youtube' providers for a value shaped like a Google API
    key ('AIza…'). The shape check means OAuth client creds (…apps.googleusercontent.com)
    stored under 'youtube' are never mistaken for an API key. Never logs the key."""
    import os
    key = os.environ.get("YOUTUBE_API_KEY")
    if key:
        return key.strip()
    try:  # lazy — keep this module importable without navig installed
        from navig.vault import get_vault
        vault = get_vault()
        for provider in ("youtube_api_key", "youtube"):
            try:
                cred = vault.get(provider, caller="navig-social")
            except Exception:  # noqa: BLE001 — one provider missing → try the next
                cred = None
            if cred is None:
                continue
            data = getattr(cred, "data", {}) or {}
            for val in data.values():
                if isinstance(val, str) and _YT_KEY_RE.match(val.strip()):
                    return val.strip()
    except Exception:  # noqa: BLE001 — vault absent/locked → just fall through
        return None
    return None


def parse_youtube_api(payload: str) -> dict:
    """Parse a YouTube Data API v3 channels?part=statistics response."""
    data = json.loads(payload)
    items = data.get("items") or []
    if not items:
        return {"followers": None, "hidden": False}
    st = items[0].get("statistics", {})

    def _int(v):
        try:
            return int(v)
        except (TypeError, ValueError):
            return None

    return {
        "followers": _int(st.get("subscriberCount")),
        "videos": _int(st.get("videoCount")),
        "views": _int(st.get("viewCount")),
        "hidden": bool(st.get("hiddenSubscriberCount", False)),
    }


def fetch_youtube_api(handle: str, api_key: str) -> StatResult:
    """Official YouTube Data API v3 — public channel stats by @handle. No login, robust,
    beats CDP. (Quota: ~1 unit/call; a free key covers thousands of crawls/day.)"""
    h = handle.lstrip("@").strip("/").split("/")[-1]
    r = StatResult(platform="youtube", handle=handle, source="youtube-data-api")
    try:
        url = ("https://www.googleapis.com/youtube/v3/channels"
               f"?part=statistics&forHandle={h}&key={api_key}")
        parsed = parse_youtube_api(_http_get(url, accept="application/json"))
        r.followers = parsed["followers"]
        for k in ("videos", "views"):
            if parsed.get(k) is not None:
                r.extra[k] = parsed[k]
        if parsed.get("hidden"):
            r.extra["subscriber_count_hidden"] = True
        if r.followers is not None:
            r.status = "ok"
        elif parsed.get("hidden"):
            r.status, r.error = "ok", "subscriber count hidden by channel"
        else:
            r.status, r.error = "error", "channel not found for @handle (check the handle)"
    except urllib.error.HTTPError as exc:
        r.status, r.error = "error", f"HTTP {exc.code} (bad or over-quota API key?)"
    except Exception as exc:  # noqa: BLE001
        r.status, r.error = "error", str(exc)
    return r


# ---- VK (official VK API) ----

def _vault_value(providers: tuple[str, ...], fields: tuple[str, ...]) -> str | None:
    """Return the first non-empty `fields` value found across `providers` in the navig vault."""
    try:
        from navig.vault import get_vault
        vault = get_vault()
    except Exception:  # noqa: BLE001
        return None
    for provider in providers:
        try:
            cred = vault.get(provider, caller="navig-social")
        except Exception:  # noqa: BLE001
            cred = None
        if cred is None:
            continue
        data = getattr(cred, "data", {}) or {}
        for f in fields:
            v = data.get(f)
            if isinstance(v, str) and v.strip():
                return v.strip()
    return None


def vk_token() -> str | None:
    import os
    return (os.environ.get("VK_TOKEN")
            or _vault_value(("vk_token", "vk"), ("value", "token", "access_token", "service_token", "api_key")))


def parse_vk(payload: str) -> tuple[int | None, str]:
    """Parse a VK API response. Returns (count, kind) where kind is 'group'|'user'|''."""
    data = json.loads(payload)
    resp = data.get("response")
    if isinstance(resp, dict) and isinstance(resp.get("groups"), list) and resp["groups"]:
        g = resp["groups"][0]
        if g.get("members_count") is not None:
            return int(g["members_count"]), "group"
    if isinstance(resp, list) and resp:
        item = resp[0]
        if item.get("members_count") is not None:
            return int(item["members_count"]), "group"
        if item.get("followers_count") is not None:
            return int(item["followers_count"]), "user"
    return None, ""


def fetch_vk(handle: str, token: str) -> StatResult:
    """VK API: community members_count (groups.getById) or user followers_count (users.get)."""
    h = handle.lstrip("@").strip("/").split("/")[-1]
    r = StatResult(platform="vk", handle=handle, source="vk-api")
    ver = "5.199"
    try:
        # Try community first, then user.
        for method, params in (
            ("groups.getById", f"group_id={h}&fields=members_count"),
            ("users.get", f"user_ids={h}&fields=followers_count"),
        ):
            url = f"https://api.vk.com/method/{method}?{params}&access_token={token}&v={ver}"
            payload = _http_get(url, accept="application/json")
            if '"error"' in payload and method == "groups.getById":
                continue  # not a community → try user
            count, _kind = parse_vk(payload)
            if count is not None:
                r.followers, r.status = count, "ok"
                return r
        r.status, r.error = "error", "no members/followers count (private, or bad VK token)"
    except Exception as exc:  # noqa: BLE001
        r.status, r.error = "error", str(exc)
    return r


# ---- Facebook (official Graph API) ----

def facebook_token() -> str | None:
    import os
    return (os.environ.get("FACEBOOK_TOKEN") or os.environ.get("FB_TOKEN")
            or _vault_value(("facebook",), ("value", "token", "access_token", "page_token", "api_key")))


def parse_facebook_graph(payload: str) -> int | None:
    data = json.loads(payload)
    if data.get("error"):
        return None
    v = data.get("followers_count")
    if v is None:
        v = data.get("fan_count")
    return int(v) if v is not None else None


def fetch_facebook_graph(handle: str, token: str) -> StatResult:
    """Facebook Graph API — Page followers_count/fan_count. Works for Pages the token can read
    (not personal profiles). Handle = the page username or id."""
    h = handle.lstrip("@").strip("/").split("/")[-1]
    r = StatResult(platform="facebook", handle=handle, source="facebook-graph")
    try:
        url = (f"https://graph.facebook.com/v21.0/{h}"
               f"?fields=followers_count,fan_count&access_token={token}")
        n = parse_facebook_graph(_http_get(url, accept="application/json"))
        if n is not None:
            r.followers, r.status = n, "ok"
        else:
            r.status, r.error = "error", "no follower count (personal profile, or token lacks this Page)"
    except urllib.error.HTTPError as exc:
        r.status, r.error = "error", f"HTTP {exc.code} (token expired/invalid or wrong Page?)"
    except Exception as exc:  # noqa: BLE001
        r.status, r.error = "error", str(exc)
    return r


# ---- Kick (public API) ----

def parse_kick(payload: str) -> int | None:
    d = json.loads(payload)
    v = d.get("followers_count", d.get("followersCount"))
    return int(v) if v is not None else None


def fetch_kick(handle: str) -> StatResult:
    slug = handle.lstrip("@").strip("/").split("/")[-1].lower()
    r = StatResult(platform="kick", handle=handle, source="kick-api")
    try:
        n = parse_kick(_http_get(f"https://kick.com/api/v2/channels/{slug}", accept="application/json"))
        if n is not None:
            r.followers, r.status = n, "ok"
        else:
            r.status, r.error = "error", "no followers_count in Kick channel payload"
    except urllib.error.HTTPError as exc:
        r.status, r.error = "error", f"HTTP {exc.code} (channel not found or Cloudflare-blocked)"
    except Exception as exc:  # noqa: BLE001
        r.status, r.error = "error", str(exc)
    return r


# ---- Twitch (official Helix API) ----

def twitch_creds() -> tuple[str, str] | None:
    """(client_id, user_token). Follower total needs a USER token with moderator:read:followers."""
    import os
    cid = os.environ.get("TWITCH_CLIENT_ID") or _vault_value(("twitch",), ("client_id", "id"))
    tok = os.environ.get("TWITCH_TOKEN") or _vault_value(("twitch",), ("token", "access_token", "oauth", "value"))
    return (cid, tok) if cid and tok else None


def fetch_twitch(handle: str, creds: tuple[str, str]) -> StatResult:
    client_id, token = creds
    login = handle.lstrip("@").strip("/").split("/")[-1]
    hdrs = {"Client-Id": client_id, "Authorization": f"Bearer {token}"}
    r = StatResult(platform="twitch", handle=handle, source="twitch-helix")
    try:
        users = json.loads(_http_get(f"https://api.twitch.tv/helix/users?login={login}",
                                     accept="application/json", headers=hdrs))
        items = users.get("data") or []
        if not items:
            r.status, r.error = "error", "twitch user not found"
            return r
        bid = items[0]["id"]
        foll = json.loads(_http_get(f"https://api.twitch.tv/helix/channels/followers?broadcaster_id={bid}&first=1",
                                    accept="application/json", headers=hdrs))
        total = foll.get("total")
        if total is not None:
            r.followers, r.status = int(total), "ok"
        else:
            r.status, r.error = "error", "no total (token needs moderator:read:followers for this channel)"
    except urllib.error.HTTPError as exc:
        r.status, r.error = "error", f"HTTP {exc.code} (token/scope: needs moderator:read:followers)"
    except Exception as exc:  # noqa: BLE001
        r.status, r.error = "error", str(exc)
    return r


# ---- Instagram (Graph API, Business accounts) ----

def instagram_token() -> str | None:
    import os
    return (os.environ.get("INSTAGRAM_TOKEN") or os.environ.get("FACEBOOK_TOKEN")
            or _vault_value(("instagram", "facebook"),
                            ("value", "token", "access_token", "page_token", "api_key")))


def fetch_instagram_graph(handle: str, token: str) -> StatResult:
    """IG Graph: resolve the IG Business account linked to the token's Page(s) and match the username.
    Only works for Business/Creator IG accounts linked to a Facebook Page."""
    h = handle.lstrip("@").strip("/").split("/")[-1].lower()
    r = StatResult(platform="instagram", handle=handle, source="instagram-graph")
    try:
        # A page token → /me?fields=instagram_business_account{...}; a user token → /me/accounts{...}.
        for path in ("me?fields=instagram_business_account{username,followers_count}",
                     "me/accounts?fields=instagram_business_account{username,followers_count}"):
            url = f"https://graph.facebook.com/v21.0/{path}&access_token={token}"
            data = json.loads(_http_get(url, accept="application/json"))
            candidates = data.get("data", [data])  # /me/accounts has 'data' list; /me is a single object
            for node in candidates:
                iba = node.get("instagram_business_account") if isinstance(node, dict) else None
                if iba and (iba.get("username", "").lower() == h or len(candidates) == 1):
                    if iba.get("followers_count") is not None:
                        r.followers, r.status = int(iba["followers_count"]), "ok"
                        return r
        r.status, r.error = "error", "no linked IG Business account matched (personal IG has no API → use CDP)"
    except urllib.error.HTTPError as exc:
        r.status, r.error = "error", f"HTTP {exc.code} (token scope: needs instagram_basic + pages_show_list)"
    except Exception as exc:  # noqa: BLE001
        r.status, r.error = "error", str(exc)
    return r


# --------------------------------------------------------------------------- cdp backend

# platform -> (url_template, login_domain_or_None, extractor_js)
# extractor_js must `return` a number (or a string of digits). Selectors are best-effort
# starting points — tune per platform against a live logged-in browser.
CDP_PLATFORMS: dict[str, dict] = {
    "youtube":   {"login": None, "js": r"""
        var m = document.body.innerText.match(/([\d.,]+[KMB]?)\s*subscribers/i); return m ? m[1] : null;"""},
    "twitch":    {"login": None, "js": r"""
        var m = document.body.innerText.match(/([\d.,]+[KMB]?)\s*followers/i); return m ? m[1] : null;"""},
    "tiktok":    {"login": "tiktok.com",    "js": r"""
        var el=document.querySelector('[data-e2e=followers-count]'); return el?el.innerText:null;"""},
    "instagram": {"login": "instagram.com", "js": r"""
        var m=document.body.innerText.match(/([\d.,]+[KMB]?)\s*followers/i); return m?m[1]:null;"""},
    "facebook":  {"login": "facebook.com",  "js": r"""
        var m=document.body.innerText.match(/([\d.,]+[KMB]?)\s*followers/i); return m?m[1]:null;"""},
    "vk":        {"login": "vk.com",        "js": r"""
        var m=document.body.innerText.match(/([\d\s.,]+)\s*followers/i); return m?m[1]:null;"""},
    "x":         {"login": "x.com",         "js": r"""
        var m=document.body.innerText.match(/([\d.,]+[KMB]?)\s*Followers/i); return m?m[1]:null;"""},
    "linkedin":  {"login": "linkedin.com",  "js": r"""
        var m=document.body.innerText.match(/([\d.,]+[KMB]?)\s*followers/i); return m?m[1]:null;"""},
}


def _navig_exe() -> str:
    return shutil.which("navig") or "navig"


def _run_navig(args: list[str], timeout: int = CDP_TIMEOUT) -> subprocess.CompletedProcess:
    return subprocess.run([_navig_exe(), *args], capture_output=True, text=True,
                          timeout=timeout, encoding="utf-8", errors="replace")


def _parse_port(text: str) -> int | None:
    # navig cdp prints an endpoint like http://127.0.0.1:PORT ; accept JSON {"port": N} too.
    try:
        obj = json.loads(text)
        if isinstance(obj, dict) and obj.get("port"):
            return int(obj["port"])
    except (json.JSONDecodeError, TypeError, ValueError):
        pass
    m = re.search(r"127\.0\.0\.1:(\d{4,5})", text) or re.search(r"\bport[\"':=\s]+(\d{4,5})", text, re.I)
    return int(m.group(1)) if m else None


def _humanish_to_int(val: str | None) -> int | None:
    """'1.2K'/'3,400'/'2 345' -> int. Returns None if unparseable."""
    if val is None:
        return None
    s = str(val).strip()
    mult = 1
    if s and s[-1] in "KkMmBb":
        mult = {"k": 1_000, "m": 1_000_000, "b": 1_000_000_000}[s[-1].lower()]
        s = s[:-1]
    s = re.sub(r"[^\d.]", "", s)
    if not s:
        return None
    try:
        return int(round(float(s) * mult))
    except ValueError:
        return None


class CdpUnavailable(RuntimeError):
    pass


# Ports of browsers THIS process launched via navig cdp. Teardown stops exactly these — never
# `--all`, which would also kill browsers launched by other navig/agent sessions on this machine.
_OPENED_PORTS: set[int] = set()


def _cdp_open(profile: str) -> int:
    """Ensure a reusable navig-cdp browser for `profile`; return its port. Raises CdpUnavailable."""
    for args in (["cdp", "profile", "use", profile, "--json"],
                 ["cdp", "open", profile, "--json"],
                 ["cdp", "new", "--profile", profile, "--json"]):
        try:
            res = _run_navig(args, timeout=60)
        except (subprocess.TimeoutExpired, FileNotFoundError):
            continue
        port = _parse_port((res.stdout or "") + (res.stderr or ""))
        if port:
            _OPENED_PORTS.add(port)
            return port
    raise CdpUnavailable("could not obtain a navig cdp browser/port (is `navig cdp` available?)")


def fetch_via_cdp(platform: str, url: str, handle: str, *, profile: str = "social-stats",
                  do_login: bool = True) -> StatResult:
    """Best-effort: drive `navig cdp` to read a public/logged-in stat. Never raises — folds
    failures into a status so a crawl of many accounts always completes."""
    cfg = CDP_PLATFORMS.get(platform)
    r = StatResult(platform=platform, handle=handle, source="navig-cdp")
    if cfg is None:
        r.status = "unsupported"
        return r
    try:
        port = _cdp_open(profile)
    except CdpUnavailable as exc:
        r.status, r.error = "needs-cdp", str(exc)
        return r

    login_domain = cfg.get("login")
    if login_domain and do_login:
        lr = _run_navig(["cdp", "login", login_domain, "--json"], timeout=60)
        out = (lr.stdout or "") + (lr.stderr or "")
        if re.search(r"no[_\s-]?credential|needs[_\s-]?disambiguation", out, re.I):
            r.status = "needs-login"
            r.error = f"no vaulted login for {login_domain} — add: navig vault login add {login_domain} -u <user>"
            return r

    nav = _run_navig(["cdp", "nav", url, "-p", str(port), "--json"], timeout=60)
    if nav.returncode != 0:
        # retry without explicit port (uses the launched browser)
        nav = _run_navig(["cdp", "nav", url, "--json"], timeout=60)

    js = cfg["js"].strip()
    ev = _run_navig(["cdp", "eval", js, "-p", str(port), "--yes", "--json"], timeout=60)
    raw = (ev.stdout or "").strip()
    value = None
    try:
        obj = json.loads(raw)
        value = obj.get("result", obj) if isinstance(obj, dict) else obj
    except json.JSONDecodeError:
        value = raw or None
    n = _humanish_to_int(value if isinstance(value, str) else (str(value) if value is not None else None))
    if n is not None:
        r.followers, r.status = n, "ok"
    else:
        r.status = "needs-login" if login_domain else "error"
        r.error = r.error or "no count extracted (selector may need tuning, or login required)"
    return r


def cdp_stop(profile: str = "social-stats") -> None:
    """Tear down ONLY the browsers this process launched (by port). Never `--all` — that would
    kill other sessions' navig browsers. Leak-safe; ignores errors."""
    for port in list(_OPENED_PORTS):
        try:
            _run_navig(["cdp", "stop", "--port", str(port)], timeout=30)
        except Exception:  # noqa: BLE001
            pass
        _OPENED_PORTS.discard(port)


# --------------------------------------------------------------------------- orchestration

PUBLIC_FETCHERS: dict[str, Callable[[str], StatResult]] = {
    "github": fetch_github,
    "telegram": fetch_telegram,
    "soundcloud": fetch_soundcloud,
    "kick": fetch_kick,
}


# Platforms with an official API path (tried before CDP): (token-resolver, fetcher) BY NAME, so
# both are looked up live in this module's namespace (respects monkeypatching / reassignment).
API_FETCHERS: dict[str, tuple[str, str]] = {
    "youtube": ("youtube_api_key", "fetch_youtube_api"),
    "vk": ("vk_token", "fetch_vk"),
    "facebook": ("facebook_token", "fetch_facebook_graph"),
    "twitch": ("twitch_creds", "fetch_twitch"),
    "instagram": ("instagram_token", "fetch_instagram_graph"),
}


def platform_capability(platform: str) -> str:
    """'public' | 'api' | 'cdp-public' | 'cdp-login' | 'unsupported' — how a platform is crawled."""
    if platform in API_FETCHERS:
        return "api"  # official API (key/token), with CDP as fallback where applicable
    if platform in PUBLIC_FETCHERS:
        return "public"
    cfg = CDP_PLATFORMS.get(platform)
    if cfg is None:
        return "unsupported"
    return "cdp-login" if cfg.get("login") else "cdp-public"


def crawl_account(acc: dict, *, allow_cdp: bool = True, do_login: bool = True,
                  profile: str = "social-stats") -> StatResult:
    platform = (acc.get("platform") or "").strip()
    handle = (acc.get("handle") or "").strip()
    url = (acc.get("url") or "").strip()
    if not handle or handle.upper() == "TODO":
        return StatResult(platform=platform, handle=handle, status="no-handle",
                          error="handle not set in registry")
    # Official API first (robust, no browser); fall back to CDP if no credential.
    if platform in API_FETCHERS:
        resolver_name, fetcher_name = API_FETCHERS[platform]
        resolver, fetcher = globals()[resolver_name], globals()[fetcher_name]
        cred = resolver()
        if cred:
            return fetcher(handle, cred)
        if not allow_cdp or platform not in CDP_PLATFORMS:
            return StatResult(platform=platform, handle=handle, status="needs-token",
                              error=f"no API token for {platform} (see README); and no CDP fallback available"
                              if platform not in CDP_PLATFORMS else
                              f"no API token for {platform}, and --only-public")
        # else: fall through to the CDP branch below
    elif platform in PUBLIC_FETCHERS:
        return PUBLIC_FETCHERS[platform](handle)
    if platform in CDP_PLATFORMS:
        if not allow_cdp:
            cap = platform_capability(platform)
            return StatResult(platform=platform, handle=handle,
                              status="needs-cdp" if cap == "cdp-public" else "needs-login",
                              error="cdp disabled (--only-public)")
        target = url or f"https://{platform}.com/{handle.lstrip('@')}"
        return fetch_via_cdp(platform, target, handle, profile=profile, do_login=do_login)
    return StatResult(platform=platform, handle=handle, status="unsupported")


def crawl_handles(
    pairs: list[tuple[str, str]],
    *,
    allow_cdp: bool = True,
    do_login: bool = True,
    profile: str = "social-stats",
) -> list[StatResult]:
    """Fetch stats for a batch of ``(platform, handle)`` pairs — universal, no space.

    This is the reusable batch entry point behind ``navig social stats``: it fetches
    each handle, and tears down any ``navig cdp`` browser this call launched exactly
    once at the end (never ``--all``, so other sessions' browsers are untouched).
    """
    results: list[StatResult] = []
    used_cdp = False
    try:
        for platform, handle in pairs:
            plat = (platform or "").strip().lower()
            if allow_cdp and platform_capability(plat).startswith("cdp"):
                used_cdp = True
            results.append(
                crawl_account(
                    {"platform": plat, "handle": (handle or "").strip()},
                    allow_cdp=allow_cdp,
                    do_login=do_login,
                    profile=profile,
                )
            )
    finally:
        if used_cdp:
            cdp_stop(profile)
    return results
