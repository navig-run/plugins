"""The edge half of the mailroom, seen from navig: the ``cybesis-mailroom`` Worker.

The Worker (source in ``plugins/navig-email/edge/``) sits on the Email Routing rule for
the domain's aliases: forward first, then a Telegram line and a metadata row in D1.
navig talks to it over a small bearer-protected API — stats, recent events, and
``/send`` for replying *as* ``support@<domain>`` through Cloudflare Email Sending
(Gmail has no send-as SMTP for a Cloudflare-routed domain).

Config (space ``.navig/config.yaml``)::

    mailroom:
      edge:
        url: https://cybesis-mailroom.<subdomain>.workers.dev
        worker: cybesis-mailroom

Secret: vault label ``mailroom/edge_token`` (the Worker's ``EDGE_TOKEN``).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

import httpx

from .space import MailroomPaths

from navig_sdk.host import command_name  # noqa: E402

# The command a user types here: `navig email` inside navig, `navig-email` on its own.
CMD = command_name("email")

TIMEOUT = 30.0
#: Full scope — reads *and* sends mail as the domain. Kept for /send and as a read fallback.
TOKEN_LABEL = "mailroom/edge_token"
#: Read-only scope — /stats and /events. Preferred for every read so a leaked dashboard
#: token cannot send mail from the domain.
READ_TOKEN_LABEL = "mailroom/edge_read_token"
#: Cloudflare API token labels tried for wrangler, in order. Needs Workers Scripts:Edit
#: (and D1:Edit for the migrations).
CF_TOKEN_LABELS = ("cloudflare/api_token", "cloudflare/d1_token", "cloudflare/token")


class EdgeNotConfigured(RuntimeError):
    pass


def _npx() -> str:
    """The npx executable. On Windows it is `npx.cmd`, which `CreateProcess` will not find
    from the bare name — resolve it here rather than falling back to `shell=True`."""
    return shutil.which("npx") or ("npx.cmd" if os.name == "nt" else "npx")


def wrangler_env(paths: MailroomPaths | None = None) -> dict[str, str]:
    """Environment for a wrangler subprocess.

    wrangler prefers its own OAuth login, which **expires** — and in a non-interactive
    shell it cannot re-login, so `deploy` and `secret` fail with "necessary to set a
    CLOUDFLARE_API_TOKEN". When the vault holds a Cloudflare API token we hand wrangler
    exactly that, so unattended deploys keep working after the login lapses. The value
    only ever lives in this dict.
    """
    env = dict(os.environ)
    if not env.get("CLOUDFLARE_API_TOKEN"):
        for label in CF_TOKEN_LABELS:
            tok = _vault_secret(label)
            if tok:
                env["CLOUDFLARE_API_TOKEN"] = tok
                break
    account = str((edge_config(paths) or {}).get("account_id") or "").strip()
    if account and not env.get("CLOUDFLARE_ACCOUNT_ID"):
        env["CLOUDFLARE_ACCOUNT_ID"] = account
    return env


def edge_dir() -> Path:
    """Where the Worker source ships inside the plugin."""
    return Path(__file__).resolve().parent.parent / "edge"


def edge_config(paths: MailroomPaths | None) -> dict[str, Any]:
    cfg = (paths.config.get("mailroom") or {}) if paths is not None else {}
    edge = cfg.get("edge") or {}
    if not isinstance(edge, dict) or not edge.get("url"):
        try:
            from navig.config import get_config_manager

            g = (get_config_manager().global_config or {}).get("mailroom") or {}
            edge = g.get("edge") or {}
        except Exception:  # noqa: BLE001
            edge = {}
    return edge if isinstance(edge, dict) else {}


def edge_url(paths: MailroomPaths | None) -> str:
    url = str(edge_config(paths).get("url") or "").strip().rstrip("/")
    if not url:
        raise EdgeNotConfigured(
            "no edge URL — set mailroom.edge.url in the space config (the Worker's workers.dev URL)"
        )
    return url


def _vault_secret(label: str) -> str:
    """The secret behind *label*, or "" — never logged, never returned partially."""
    try:
        from navig_vault.core import get_vault  # the shared vault, with or without navig

        s = get_vault().get_secret(label)
        return (s.reveal() if hasattr(s, "reveal") else str(s or "")).strip()
    except Exception:  # noqa: BLE001 — absent/locked vault = not configured
        return ""


def edge_token() -> str:
    tok = _vault_secret(TOKEN_LABEL)
    if not tok:
        raise EdgeNotConfigured(
            f"no edge token in the vault ({TOKEN_LABEL}) — run `{CMD} edge secrets`"
        )
    return tok


def read_token() -> str:
    """The read-only token when one exists, else the full token."""
    return _vault_secret(READ_TOKEN_LABEL) or edge_token()


def _headers(*, read_only: bool = False) -> dict[str, str]:
    return {"Authorization": f"Bearer {read_token() if read_only else edge_token()}"}


def put_secret(label: str, value: str) -> None:
    """Store *value* under *label* in the vault (the same shape `navig vault set` writes)."""
    import json as _json

    from navig_vault.core import get_vault  # the shared vault, with or without navig

    get_vault().put(label, _json.dumps({"value": value}).encode())


def health(paths: MailroomPaths | None) -> dict[str, Any]:
    r = httpx.get(f"{edge_url(paths)}/health", timeout=TIMEOUT)
    r.raise_for_status()
    return r.json()


def stats(paths: MailroomPaths | None, *, days: int = 7) -> dict[str, Any]:
    r = httpx.get(
        f"{edge_url(paths)}/stats",
        params={"days": days},
        headers=_headers(read_only=True),
        timeout=TIMEOUT,
    )
    r.raise_for_status()
    return r.json()


def events(paths: MailroomPaths | None, *, limit: int = 50) -> list[dict[str, Any]]:
    r = httpx.get(
        f"{edge_url(paths)}/events",
        params={"limit": limit},
        headers=_headers(read_only=True),
        timeout=TIMEOUT,
    )
    r.raise_for_status()
    return list(r.json().get("events") or [])


def send(
    paths: MailroomPaths | None,
    *,
    to: str,
    subject: str,
    text: str,
    from_addr: str | None = None,
    from_name: str | None = None,
    reply_to: str | None = None,
    in_reply_to: str | None = None,
    html: str | None = None,
) -> dict[str, Any]:
    """Send AS an @domain address through the Worker's Email Sending binding."""
    body: dict[str, Any] = {"to": to, "subject": subject, "text": text}
    for k, v in (
        ("from", from_addr),
        ("from_name", from_name),
        ("reply_to", reply_to),
        ("in_reply_to", in_reply_to),
        ("html", html),
    ):
        if v:
            body[k] = v
    r = httpx.post(
        f"{edge_url(paths)}/send", json=body, headers=_headers(), timeout=TIMEOUT
    )
    try:
        payload = r.json()
    except ValueError:
        payload = {"error": r.text[:300]}
    if r.status_code >= 400 or not payload.get("ok", False):
        raise RuntimeError(
            f"edge send failed ({r.status_code}): {payload.get('error') or payload}"
        )
    return payload


def read_accounts(paths: MailroomPaths) -> dict[str, Any]:
    """``<space>/mailroom/accounts.yaml`` as a dict; ``{}`` when absent or unreadable."""
    path = paths.base / "accounts.yaml"
    if not path.exists():
        return {}
    try:
        from navig_sdk.files import safe_load_yaml  # navig's reader, or its twin standalone

        data = safe_load_yaml(path)
    except Exception:  # noqa: BLE001 — a broken file contributes no aliases
        return {}
    return data if isinstance(data, dict) else {}


def default_account(accounts: dict[str, Any]) -> dict[str, Any]:
    """The account marked ``default: true``, else the first one, else ``{}``."""
    rows = [a for a in (accounts.get("accounts") or []) if isinstance(a, dict)]
    return next((a for a in rows if a.get("default")), rows[0] if rows else {})


def notify_aliases(paths: MailroomPaths) -> list[str]:
    """Alias local-parts with ``notify: true`` in accounts.yaml — the ONE source of truth
    for which alias buzzes a phone. The Worker's ``NOTIFY_ALIASES`` var is derived from this
    at deploy time; the value baked into wrangler.jsonc is only a fallback."""
    acct = default_account(read_accounts(paths))
    out: list[str] = []
    for alias in acct.get("aliases") or []:
        if not isinstance(alias, dict) or not alias.get("notify"):
            continue
        addr = str(alias.get("address") or "").strip().lower()
        local = addr.split("@", 1)[0]
        if local and local not in out:
            out.append(local)
    return out


def deploy_vars(paths: MailroomPaths | None) -> dict[str, str]:
    """``--var`` overrides derived from the space: the notify list and the forward target."""
    if paths is None:
        return {}
    accounts = read_accounts(paths)
    acct = default_account(accounts)
    out: dict[str, str] = {}
    aliases = notify_aliases(paths)
    if aliases:
        out["NOTIFY_ALIASES"] = ",".join(aliases)
    address = str(acct.get("address") or "").strip()
    if address:
        out["FORWARD_TO"] = address
    return out


def deploy(
    *, dry_run: bool = False, paths: MailroomPaths | None = None
) -> tuple[int, str, dict[str, str]]:
    """``npx wrangler deploy`` in the plugin's edge dir (wrangler's own login does the auth).

    Returns ``(returncode, output, vars)`` — *vars* are the space-derived overrides passed
    to wrangler, so the caller can show what the Worker was told.
    """
    d = edge_dir()
    if not (d / "wrangler.jsonc").exists():
        raise EdgeNotConfigured(f"edge source not found at {d}")
    overrides = deploy_vars(paths)
    argv = [_npx(), "wrangler", "deploy"]
    for key, value in overrides.items():
        argv += ["--var", f"{key}:{value}"]
    if dry_run:
        argv.append("--dry-run")
    r = subprocess.run(  # noqa: S603 — fixed argv, no shell
        argv,
        cwd=d,
        capture_output=True,
        timeout=600,
        check=False,
        shell=False,
        env=wrangler_env(paths),
    )
    from navig.core.proc_text import decode_console_output

    return int(r.returncode), decode_console_output(r.stdout + r.stderr), overrides


def put_worker_secrets(
    secrets: dict[str, str], *, paths: MailroomPaths | None = None
) -> tuple[int, str]:
    """Upload every secret in one ``wrangler secret bulk`` call. Values never reach argv,
    a log line, or this process's stdout.

    A file rather than stdin on purpose: piping stdin makes wrangler consider the shell
    non-interactive and refuse its own OAuth login ("necessary to set a CLOUDFLARE_API_TOKEN"),
    so `secret put` cannot work unattended. The temp file is written 0600 in the user's private
    temp dir and unlinked in a ``finally`` — including on a crash.
    """
    import tempfile

    d = edge_dir()
    fd, tmp_name = tempfile.mkstemp(prefix="navig-edge-secrets-", suffix=".json")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(secrets, fh)
        r = subprocess.run(  # noqa: S603 — fixed argv, no shell
            [_npx(), "wrangler", "secret", "bulk", str(tmp)],
            cwd=d,
            capture_output=True,
            timeout=300,
            check=False,
            shell=False,
            env=wrangler_env(paths),
        )
    finally:
        try:
            tmp.unlink()
        except OSError:  # pragma: no cover — best effort; the values are already uploaded
            pass
    from navig.core.proc_text import decode_console_output

    return int(r.returncode), decode_console_output(r.stdout + r.stderr)


def stats_markdown(st: dict[str, Any], *, url: str) -> str:
    lines = [
        f"## Edge (Cloudflare) — {st.get('days')} derniers jours",
        "",
        f"Worker `{url}` · {st.get('total', 0)} message(s) · transférés {st.get('forwarded', 0)} · "
        f"notifiés {st.get('notified', 0)}",
        "",
    ]
    if st.get("by_alias"):
        lines += (
            ["| Alias | Messages |", "|---|---|"]
            + [
                f"| {a}@ | {n} |"
                for a, n in sorted(st["by_alias"].items(), key=lambda kv: -kv[1])
            ]
            + [""]
        )
    if st.get("by_tag"):
        lines += (
            ["| Tag | Messages |", "|---|---|"]
            + [
                f"| {t} | {n} |"
                for t, n in sorted(st["by_tag"].items(), key=lambda kv: -kv[1])
            ]
            + [""]
        )
    if st.get("top_domains"):
        lines += (
            ["| Domaine | Messages |", "|---|---|"]
            + [f"| {d} | {n} |" for d, n in st["top_domains"]]
            + [""]
        )
    return "\n".join(lines)


def stats_telegram(st: dict[str, Any]) -> str:
    from navig_email._compat import escape_html as esc

    aliases = " · ".join(
        f"{esc(a)}@ {n}"
        for a, n in sorted((st.get("by_alias") or {}).items(), key=lambda kv: -kv[1])
    )
    tags = " · ".join(
        f"#{esc(t)} {n}"
        for t, n in sorted((st.get("by_tag") or {}).items(), key=lambda kv: -kv[1])[:6]
    )
    return (
        f"🌐 <b>Edge cybesis.com</b> — {st.get('total', 0)} message(s) / {st.get('days')} j"
        + (f"\n{aliases}" if aliases else "")
        + (f"\n{tags}" if tags else "")
    )


def dumps(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, indent=2, default=str)
