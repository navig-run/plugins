"""A user-configured ICS feed is fetched through the SSRF guard (moved here from
core/tests/net/test_ssrf_wire.py with the ICS provider itself).

The URL is followed with redirects, so a legit-looking feed that 302s to cloud metadata or a
LAN host must be refused — and the refusal must be REPORTED (last_error), not read as an
empty calendar.
"""

from __future__ import annotations

import asyncio

import navig_sdk.ssrf as ssrf

from navig_calendar.ics import ICSCalendarProvider


def test_ics_calendar_url_blocked(monkeypatch):
    # The secure default policy, whatever this machine's config says.
    monkeypatch.setattr(ssrf, "policy_from_config", lambda: ssrf.SsrfPolicy())
    provider = ICSCalendarProvider(url="http://169.254.169.254/cal.ics")
    # safe_fetch validates before any network I/O → blocked → no data, and the reason kept
    assert asyncio.run(provider._fetch_ics()) is None
    assert provider.last_error and "169.254.169.254" in provider.last_error
