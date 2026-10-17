"""DevNet lookup engine — hermetic (no network). Patches the socket/ssl/whois
seams so the validation, fallback and parsing logic is exercised offline."""

from __future__ import annotations

import sys

import pytest

from navig_devnet import lookups
from navig_devnet.lookups import (
    LookupError,
    clean_domain,
    dns_lookup,
    ssl_inspect,
    whois_lookup,
)


# ── clean_domain ─────────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    "raw,expected",
    [
        ("example.com", "example.com"),
        ("HTTPS://Example.com/some/path", "example.com"),
        ("sub.example.co.uk.", "sub.example.co.uk"),
        ("user@mail.example.com", "mail.example.com"),
    ],
)
def test_clean_domain_normalises(raw, expected):
    assert clean_domain(raw) == expected


@pytest.mark.parametrize("bad", ["", "   ", "a b", "a; rm -rf /", "..", "http://"])
def test_clean_domain_rejects(bad):
    with pytest.raises(LookupError):
        clean_domain(bad)


# ── dns_lookup ───────────────────────────────────────────────────────────────
def test_dns_rejects_unknown_type():
    with pytest.raises(LookupError):
        dns_lookup("example.com", record_type="BOGUS")


def test_dns_stdlib_fallback_for_a(monkeypatch):
    # Hide dnspython → force the stdlib A path; stub getaddrinfo.
    monkeypatch.setitem(sys.modules, "dns.resolver", None)
    monkeypatch.setattr(
        lookups.socket, "getaddrinfo",
        lambda *a, **k: [(2, 1, 6, "", ("93.184.216.34", 0))],
    )
    out = dns_lookup("example.com", "A")
    assert out == {
        "domain": "example.com", "type": "A",
        "records": ["93.184.216.34"], "engine": "stdlib",
    }


def test_dns_stdlib_cannot_do_mx(monkeypatch):
    monkeypatch.setitem(sys.modules, "dns.resolver", None)
    with pytest.raises(LookupError, match="dnspython"):
        dns_lookup("example.com", "MX")


# ── whois_lookup ─────────────────────────────────────────────────────────────
def test_whois_follows_referral(monkeypatch):
    calls = []

    def fake_query(server, domain, timeout=6.0):
        calls.append(server)
        if server == "whois.iana.org":
            return "refer: whois.verisign-grs.com\n% iana stub\n"
        return "Domain Name: EXAMPLE.COM\nRegistrar: Example Registrar\n"

    monkeypatch.setattr(lookups, "_whois_query", fake_query)
    out = whois_lookup("example.com")
    assert out["server"] == "whois.verisign-grs.com"
    assert "Example Registrar" in out["raw"]
    assert calls == ["whois.iana.org", "whois.verisign-grs.com"]


def test_whois_keeps_iana_answer_when_no_referral(monkeypatch):
    monkeypatch.setattr(
        lookups, "_whois_query",
        lambda server, domain, timeout=6.0: "no referral here\n",
    )
    out = whois_lookup("example.org")
    assert out["server"] == "whois.iana.org"


# ── ssl_inspect ──────────────────────────────────────────────────────────────
def test_ssl_inspect_parses_cert(monkeypatch):
    fake_cert = {
        "subject": [[("commonName", "example.com")]],
        "issuer": [[("organizationName", "Example CA")]],
        "notBefore": "Jan  1 00:00:00 2026 GMT",
        "notAfter": "Jan  1 00:00:00 2099 GMT",
        "subjectAltName": [("DNS", "example.com"), ("DNS", "www.example.com")],
    }
    monkeypatch.setattr(lookups, "_fetch_peer_cert", lambda host, port: fake_cert)
    out = ssl_inspect("example.com")
    assert out["common_name"] == "example.com"
    assert out["issuer"] == "Example CA"
    assert out["san"] == ["example.com", "www.example.com"]
    assert out["days_left"] > 0  # 2099 is comfortably in the future


def test_ssl_inspect_rejects_bad_port(monkeypatch):
    monkeypatch.setattr(lookups, "_fetch_peer_cert", lambda host, port: {})
    with pytest.raises(LookupError):
        ssl_inspect("example.com", port=99999)
