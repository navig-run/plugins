"""The fallback User-Agent pool: single-source, current, and curl_cffi-coherent.

There used to be three copies of this list across the plugin (anti_detect + two in the
downloader), drifting to different, stale Chrome versions. These pin that it is now ONE
source, Chrome-only, and that every advertised Chrome major is one curl_cffi can also
impersonate — so a fallback UA never claims a Chrome the TLS layer can't produce.
"""

from __future__ import annotations

import importlib
import importlib.util
import re

import pytest

import navig_download.anti_detect as ad

# `navig_download.downloader` re-exports a `main` FUNCTION that shadows the `main`
# submodule, so both `from ... import main` and `import ...main as m` grab the function.
# import_module returns the real module from sys.modules, unaffected by that shadow.
dl_main = importlib.import_module("navig_download.downloader.main")

_CHROME_MAJOR = re.compile(r"Chrome/(\d+)\.")


def _majors(uas) -> set[int]:
    out: set[int] = set()
    for ua in uas:
        m = _CHROME_MAJOR.search(ua)
        assert m, f"fallback UA is not Chrome (Chrome-only pool expected): {ua}"
        out.add(int(m.group(1)))
    return out


def test_downloader_reuses_the_single_source():
    """main.get_random_user_agent() must draw from anti_detect — no private copy."""
    for _ in range(30):
        assert dl_main.get_random_user_agent() in ad.USER_AGENTS


def test_pool_is_non_empty_and_chrome_only():
    assert ad.USER_AGENTS
    majors = _majors(ad.USER_AGENTS)
    assert majors, "no Chrome majors parsed"


def test_no_stale_majors():
    """Guards the exact regression: Chrome 120/130/131 were shipping in mid-2026."""
    majors = _majors(ad.USER_AGENTS)
    assert min(majors) >= 142, f"stale fallback Chrome majors: {sorted(majors)}"


def _curl_cffi_chrome_targets() -> set[int] | None:
    if importlib.util.find_spec("curl_cffi") is None:
        return None
    try:
        import typing

        from curl_cffi.requests.impersonate import BrowserTypeLiteral

        out: set[int] = set()
        for name in typing.get_args(BrowserTypeLiteral):
            if name.startswith("chrome") and "android" not in name:
                digits = "".join(c for c in name if c.isdigit())
                if digits:
                    out.add(int(digits))
        return out or None
    except Exception:  # pragma: no cover - API drift → don't block the suite
        return None


def test_every_fallback_major_is_a_curl_cffi_target():
    """A fallback UA must never claim a Chrome the TLS layer can't impersonate."""
    supported = _curl_cffi_chrome_targets()
    if supported is None:
        pytest.skip("curl_cffi not installed / target list unreadable")
    unsupported = sorted(_majors(ad.USER_AGENTS) - supported)
    assert not unsupported, (
        f"fallback UAs advertise Chrome {unsupported} which curl_cffi can't impersonate "
        f"(supported: {sorted(supported)}) — UA/TLS could disagree."
    )
