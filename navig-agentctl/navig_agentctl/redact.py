"""Secret redaction for the Agent Monitor — stdlib only.

Everything the monitor stores or shows (commands, tool summaries, log lines,
notification bodies) passes through :func:`redact` first. The hook calls it
BEFORE anything leaves its process and the server calls it again on read, so a
bypass in one place is not a leak.

Heuristic by design: it removes what looks like a credential and keeps the rest
readable. It is not a guarantee that arbitrary log content is secret-free, which
is why log files are also redacted line by line on write and capped on read.
"""

from __future__ import annotations

import re

MASK = "[REDACTED]"

# name=value / name: value where the NAME says it is a secret
_SECRET_NAME = r"(?:[A-Za-z0-9_.-]*(?:pass(?:word|wd)?|pwd|secret|token|api[_-]?key|apikey|auth|credential|private[_-]?key|session[_-]?id|cookie)[A-Za-z0-9_.-]*)"

_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    # PEM blocks
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?(?:-----END [A-Z ]*PRIVATE KEY-----|\Z)", re.S), MASK),
    # Authorization / Bearer / Basic
    (re.compile(r"(?i)\b(authorization\s*[:=]\s*)(?:bearer|basic|token)?\s*[^\s'\"]+"), r"\1" + MASK),
    (re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=-]{8,}"), r"\1 " + MASK),
    # --flag value / --flag=value for secret-named flags
    (re.compile(rf"(?i)(--?{_SECRET_NAME}(?:=|\s+))(?:\"[^\"]*\"|'[^']*'|[^\s]+)"), r"\1" + MASK),
    # NAME=value / NAME: value / "NAME": "value"
    (re.compile(rf"(?i)\b({_SECRET_NAME}[\"']?\s*[:=]\s*)(?:\"[^\"]*\"|'[^']*'|[^\s,;&]+)"), r"\1" + MASK),
    # $env:NAME = "value"
    (re.compile(rf"(?i)(\$env:{_SECRET_NAME}\s*=\s*)(?:\"[^\"]*\"|'[^']*'|[^\s;]+)"), r"\1" + MASK),
    # user:pass@host in URLs
    (re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://[^/\s:@]+:)[^@\s/]+(@)"), r"\1" + MASK + r"\2"),
    # well-known token shapes
    (re.compile(r"\b(?:sk|pk|rk)-[A-Za-z0-9_-]{16,}\b"), MASK),
    (re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr|github_pat)_[A-Za-z0-9_]{20,}\b"), MASK),
    (re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}\b"), MASK),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), MASK),
    (re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{4,}\b"), MASK),  # JWT
    (re.compile(r"\b[0-9]{8,10}:[A-Za-z0-9_-]{30,}\b"), MASK),  # Telegram bot token
    # long opaque blobs (hex/base64-ish, 40+)
    (re.compile(r"\b[A-Fa-f0-9]{40,}\b"), MASK),
    # (must mix letters AND digits: a long snake_case identifier is not a secret)
    (re.compile(r"\b(?=[A-Za-z0-9+/_-]*\d)(?=[A-Za-z0-9+/_-]*[A-Za-z])[A-Za-z0-9+/_-]{48,}={0,2}(?![A-Za-z0-9+/_-])"), MASK),
]


def redact(text: object, *, limit: int = 0) -> str:
    """Return *text* with credential-looking spans masked; optionally truncated."""
    out = "" if text is None else str(text)
    for pattern, repl in _PATTERNS:
        out = pattern.sub(repl, out)
    if limit and len(out) > limit:
        out = out[: max(0, limit - 1)] + "…"
    return out
