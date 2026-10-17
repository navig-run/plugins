"""DevNet lookup engine — DNS, WHOIS and SSL/TLS inspection.

Pure, standalone functions (no navig import): the CLI, the agent tools and any
future deck route all call these. DNS uses dnspython when present (MX/TXT/NS/…),
falling back to the stdlib resolver for A/AAAA so the package still answers with
no extra dependency. WHOIS and SSL are stdlib-only.

Everything here is READ-ONLY — it resolves names and reads a server's own
certificate. Nothing sends a payload or mutates a target.
"""

from __future__ import annotations

import re
import socket
import ssl
from datetime import datetime, timezone
from typing import Any

# A hostname/domain label the lookups accept. Deliberately strict: these values
# reach socket calls and (for DNS) a subprocess/resolver, so an unvalidated
# string is an injection surface. No scheme, no path, no spaces.
_DOMAIN_RE = re.compile(r"^(?=.{1,253}$)([A-Za-z0-9_-]{1,63}\.)*[A-Za-z0-9_-]{1,63}$")
_RESOLVER_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,253}$")

#: DNS record types the lookup will accept (upper-cased before the check).
DNS_RECORD_TYPES = ("A", "AAAA", "MX", "TXT", "NS", "CNAME", "SOA", "CAA", "PTR", "SRV")


class LookupError(ValueError):
    """A lookup input was rejected, or a lookup could not be completed."""


def clean_domain(value: str) -> str:
    """Normalise and validate a hostname; raise :class:`LookupError` if invalid.

    Strips a scheme and anything from the first ``/``, lower-cases, and checks
    the label grammar — so ``https://Example.com/path`` becomes ``example.com``
    and ``a; rm -rf`` is refused before it can reach a socket or subprocess.
    """
    s = (value or "").strip()
    if "://" in s:
        s = s.split("://", 1)[1]
    s = s.split("/", 1)[0].split("@")[-1].strip().rstrip(".").lower()
    if not s or not _DOMAIN_RE.match(s):
        raise LookupError(f"invalid domain/host: {value!r}")
    return s


def dns_lookup(
    domain: str, record_type: str = "A", resolver: str | None = None
) -> dict[str, Any]:
    """Resolve *record_type* for *domain*. Returns
    ``{domain, type, records: [...], resolver?, engine}``.

    ``engine`` is ``"dnspython"`` for the full record set, or ``"stdlib"`` for the
    A/AAAA fallback when dnspython is not installed (and a non-address type was
    asked for → :class:`LookupError`, since the stdlib cannot answer it).
    """
    domain = clean_domain(domain)
    rtype = (record_type or "A").strip().upper()
    if rtype not in DNS_RECORD_TYPES:
        raise LookupError(f"unsupported record type {record_type!r}")
    if resolver is not None:
        resolver = resolver.strip()
        if not _RESOLVER_RE.match(resolver):
            raise LookupError(f"invalid resolver: {resolver!r}")

    try:
        import dns.resolver  # type: ignore

        res = dns.resolver.Resolver()
        if resolver:
            res.nameservers = [socket.gethostbyname(resolver)]
        answers = res.resolve(domain, rtype, raise_on_no_answer=False)
        records = [r.to_text() for r in answers] if answers.rrset is not None else []
        out: dict[str, Any] = {
            "domain": domain, "type": rtype, "records": records, "engine": "dnspython",
        }
        if resolver:
            out["resolver"] = resolver
        return out
    except ImportError:
        # No dnspython — the stdlib resolver only does address records.
        if rtype not in ("A", "AAAA"):
            raise LookupError(
                f"{rtype} records need dnspython (pip install dnspython); "
                "stdlib fallback only resolves A/AAAA"
            ) from None
        family = socket.AF_INET if rtype == "A" else socket.AF_INET6
        infos = socket.getaddrinfo(domain, None, family)
        records = sorted({str(i[4][0]) for i in infos})
        return {"domain": domain, "type": rtype, "records": records, "engine": "stdlib"}


def _whois_query(server: str, domain: str, timeout: float = 6.0) -> str:
    with socket.create_connection((server, 43), timeout=timeout) as s:
        s.sendall((domain + "\r\n").encode())
        data = b""
        while True:
            chunk = s.recv(4096)
            if not chunk:
                break
            data += chunk
            if len(data) > 65536:
                break
    return data.decode(errors="replace")


def whois_lookup(domain: str) -> dict[str, Any]:
    """Bare-TCP WHOIS (no system ``whois`` binary needed). Queries IANA, then
    follows one ``refer:`` to the TLD registry. Returns
    ``{domain, server, raw}`` (raw capped at 32 KiB)."""
    domain = clean_domain(domain)
    raw = _whois_query("whois.iana.org", domain)
    server = "whois.iana.org"
    refer = next(
        (ln.split(":", 1)[1].strip()
         for ln in raw.splitlines() if ln.lower().startswith("refer:")),
        None,
    )
    if refer and _RESOLVER_RE.match(refer):
        try:
            raw = _whois_query(refer, domain)
            server = refer
        except OSError:
            pass  # referral unreachable — keep the IANA answer
    return {"domain": domain, "server": server, "raw": raw[:32768]}


def _fetch_peer_cert(host: str, port: int) -> dict[str, Any]:
    """Blocking TLS handshake → the leaf cert dict. The network seam (tests patch it)."""
    ctx = ssl.create_default_context()
    with socket.create_connection((host, port), timeout=6) as sock:
        with ctx.wrap_socket(sock, server_hostname=host) as ssock:
            return ssock.getpeercert() or {}


def ssl_inspect(host: str, port: int = 443) -> dict[str, Any]:
    """Read a server's TLS leaf certificate. Returns
    ``{host, port, common_name, issuer, not_before, not_after, days_left, san}``.

    A blocking connect + handshake — a caller on an event loop must offload it
    (``asyncio.to_thread``)."""
    host = clean_domain(host)
    if not (0 < int(port) < 65536):
        raise LookupError(f"invalid port: {port!r}")
    cert = _fetch_peer_cert(host, int(port))
    subject = dict(x[0] for x in cert.get("subject", []))
    issuer = dict(x[0] for x in cert.get("issuer", []))
    san = [v for k, v in cert.get("subjectAltName", []) if k == "DNS"]
    not_after = cert.get("notAfter", "")
    days_left: int | None = None
    try:
        exp = datetime.strptime(not_after, "%b %d %H:%M:%S %Y %Z").replace(
            tzinfo=timezone.utc
        )
        days_left = (exp - datetime.now(timezone.utc)).days
    except ValueError:
        pass
    return {
        "host": host,
        "port": int(port),
        "common_name": subject.get("commonName", ""),
        "issuer": issuer.get("organizationName", "") or issuer.get("commonName", ""),
        "not_before": cert.get("notBefore", ""),
        "not_after": not_after,
        "days_left": days_left,
        "san": san,
    }
