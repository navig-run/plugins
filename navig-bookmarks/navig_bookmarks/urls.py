"""URL normalisation for display, storage and duplicate detection.

``normalize_url`` is adapted from linkding's ``bookmarks/utils.py`` (MIT, see
THIRD_PARTY_NOTICES.md) and extended: tracking parameters are removed and default ports are
dropped. ``dedupe_key`` goes one step further for matching only (never shown, never written
back as the URL): ``www.`` is ignored and ``http``/``https`` compare equal, because two
bookmarks that differ only there are the same page for every practical purpose.
"""

from __future__ import annotations

import re
import urllib.parse

# Parameters that only identify the click, never the page. Kept deliberately conservative:
# a short generic name like `ref` or `source` is meaningful on many sites and is NOT here.
TRACKING_PARAMS = frozenset({
    "fbclid", "gclid", "dclid", "gbraid", "wbraid", "msclkid", "yclid", "twclid", "ttclid",
    "li_fat_id", "mc_cid", "mc_eid", "igshid", "_hsenc", "_hsmi", "mkt_tok", "vero_id",
    "oly_anon_id", "oly_enc_id", "rb_clickid", "s_cid", "ref_src", "ref_url", "spm",
})
TRACKING_PREFIXES = ("utm_", "pk_", "mtm_", "hsa_")

_DEFAULT_PORTS = {"http": 80, "https": 443}
_SCHEMELESS_HOST = re.compile(r"^(?:[a-z0-9-]+\.)+[a-z]{2,}(?::\d+)?(?:[/?#].*)?$", re.I)


def is_tracking_param(name: str) -> bool:
    low = name.lower()
    return low in TRACKING_PARAMS or low.startswith(TRACKING_PREFIXES)


def looks_like_url(text: str) -> bool:
    """True for ``https://…`` and for a bare ``example.com/path`` (what people type)."""
    t = (text or "").strip()
    if not t or " " in t:
        return False
    if re.match(r"^[a-z][a-z0-9+.-]*://", t, re.I):
        return True
    return bool(_SCHEMELESS_HOST.match(t))


def ensure_scheme(text: str) -> str:
    """``example.com`` -> ``https://example.com``; anything with a scheme is left alone."""
    t = (text or "").strip()
    if re.match(r"^[a-z][a-z0-9+.-]*:", t, re.I):
        return t
    return f"https://{t}" if t else t


def normalize_url(url: str, *, strip_tracking: bool = True) -> str:
    """A canonical spelling of *url*: lowercase scheme and host, no default port, no trailing
    slash, query sorted (and, by default, without tracking parameters). Fragment kept.

    Non-hierarchical URLs (``javascript:``, ``mailto:``) are returned trimmed but otherwise
    untouched. Never raises: an unparsable URL comes back as given.
    """
    if not url or not isinstance(url, str):
        return ""
    url = url.strip()
    if not url:
        return ""
    try:
        parsed = urllib.parse.urlparse(url)
        scheme = parsed.scheme.lower()
        if scheme and not parsed.netloc:
            return url
        host = parsed.hostname.lower() if parsed.hostname else ""
        netloc = host
        port = parsed.port
        if port and _DEFAULT_PORTS.get(scheme) != port:
            netloc += f":{port}"
        if parsed.username:
            auth = parsed.username
            if parsed.password:
                auth += f":{parsed.password}"
            netloc = f"{auth}@{netloc}"
        path = parsed.path.rstrip("/") if parsed.path else ""
        query = ""
        if parsed.query:
            params = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
            if strip_tracking:
                params = [(k, v) for k, v in params if not is_tracking_param(k)]
            params.sort(key=lambda kv: (kv[0], kv[1]))
            query = urllib.parse.urlencode(params, quote_via=urllib.parse.quote)
        return urllib.parse.urlunparse((scheme, netloc, path, parsed.params, query, parsed.fragment))
    except (ValueError, AttributeError):
        return url


def dedupe_key(url: str) -> str:
    """The key two bookmarks are compared on: :func:`normalize_url`, minus ``www.``, with
    ``http`` and ``https`` folded together. Matching only — never stored as the URL."""
    norm = normalize_url(url)
    try:
        parsed = urllib.parse.urlparse(norm)
    except ValueError:
        return norm
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return norm
    netloc = parsed.netloc
    if netloc.startswith("www."):
        netloc = netloc[4:]
    elif "@www." in netloc:
        netloc = netloc.replace("@www.", "@", 1)
    return urllib.parse.urlunparse(("https", netloc, parsed.path, parsed.params, parsed.query, parsed.fragment))


# Second-level labels under a country TLD that act like a TLD (example.co.uk). A heuristic,
# not the Public Suffix List: good enough to group bookmarks, never used for security.
_SECOND_LEVEL = frozenset({"co", "com", "org", "net", "gov", "ac", "edu", "ne", "or", "go"})


def site_of(url: str) -> str:
    """The registrable-ish domain a bookmark belongs to (``docs.python.org`` -> ``python.org``)."""
    try:
        host = (urllib.parse.urlparse(url).hostname or "").lower()
    except ValueError:
        return ""
    if not host:
        return ""
    if host.startswith("www."):
        host = host[4:]
    if re.fullmatch(r"[\d.]+", host) or ":" in host:
        return host
    labels = host.split(".")
    if len(labels) >= 3 and len(labels[-1]) == 2 and labels[-2] in _SECOND_LEVEL:
        return ".".join(labels[-3:])
    return ".".join(labels[-2:])
