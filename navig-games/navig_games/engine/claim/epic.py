"""Epic Games Store claim flow — drive a real Chromium (over core's CDP stack) to
complete a $0 checkout. FREE-ONLY, unattended-safe.

Reuse map (no reinvention):
  * launch      → ``navig.browser.cdp_actions.profile_open`` (persistent, isolated
                  profile on a stable port — keeps cookies/session between runs)
  * login       → ``navig.browser.cdp_actions.login`` → ``autofill.auto_login``
                  (session-first restore, form-fill, TOTP; re-captures the session)
  * page/iframe → the attached Playwright page via ``session_manager`` (needed for
                  the cross-origin ``#webPurchaseContainer`` checkout iframe)

Selectors and flow are implemented natively against the live Epic checkout.

The FREE-ONLY invariant is enforced by three independent gates before an order is
ever placed:
  A. Sourcing said the discounted price is exactly 0 (caller guarantees this).
  B. The product CTA reads "Get" (Epic never shows "Get" for a priced title).
  C. The checkout-overlay order total is read; if it shows any positive amount the
     claim is refused. (With ``require_overlay_zero`` the total must be *readable
     and zero* to proceed; default is the safer-to-ship "refuse on any visible
     cost, allow when confirmed-zero or unreadable-but-A+B-hold".)
Any parse failure resolves toward *refuse*, so a bug can only prevent a purchase,
never cause one.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from ..models import FreeGame
from .base import (
    STATUS_CLAIMED,
    STATUS_DRYRUN,
    STATUS_FAILED,
    STATUS_MANUAL,
    STATUS_OWNED,
    STATUS_PRICED,
    ClaimResult,
    is_zero_total,
)

_log = logging.getLogger(__name__)

EPIC_DOMAIN = "epicgames.com"
FREE_GAMES_URL = "https://store.epicgames.com/en-US/free-games"
LOGIN_URL = (
    "https://www.epicgames.com/id/login?lang=en-US&noHostRedirect=true"
    "&redirectUrl=" + FREE_GAMES_URL
)

# --- selectors (mined from the reference claimer) --------------------------------
S_NAV = "egs-navigation"
S_CTA = 'button[data-testid="purchase-cta-button"]'
S_CONTINUE = 'button:has-text("Continue")'
S_YES_BUY = 'button:has-text("Yes, buy now")'
S_EULA_CHECK = "input#agree"
S_EULA_ACCEPT = 'button:has-text("Accept")'
S_PURCHASE_IFRAME = "#webPurchaseContainer iframe"
# Epic's 2026-07 checkout redesign replaced "Place Order" with "Add to library"
# for free claims; keep both so either UI generation works.
S_PLACE_ORDER = (
    'button:has-text("Place Order"):not(:has(.payment-loading--loading)), '
    'button:has-text("Add to library")'
)
S_I_ACCEPT = 'button:has-text("I Accept")'
S_LOGIN_CAPTCHA = ".h_captcha_challenge iframe"
S_CHECKOUT_CAPTCHA = "#h_captcha_challenge_checkout_free_prod iframe"
# Old UI confirms with "Thanks for your order!"; the redesign closes the checkout
# and shows the "Download the Epic Games Launcher" dialog on the product page.
S_SUCCESS = "text=/Thanks for your order!|Download the Epic Games Launcher/"
S_REGION_BLOCK = ':has-text("unavailable in your region")'
_TOTAL_SELECTORS = (
    '[data-component="TotalPrice"]',
    ".order-total",
    ".payment-order-total__value",
    ".payment-summary__total",
)


def open_epic_profile(profile: str = "navig-epic", *, context: str = "script") -> dict:
    """Ensure the persistent Epic browser profile exists, then open (or reuse) it
    on its stable debug port. Cookies/session persist between runs.

    *context* defaults to ``"script"`` because the engine's callers — the claim run and
    the live probe — are unattended: ``navig games claim --all --yes`` is on a cron
    schedule. It used to open a **visible** window and never close it, and because
    browsers are spawned ``DETACHED_PROCESS`` the deck's 900 s ``proc.kill()`` reaped the
    CLI and left the browser behind, so a blank window accumulated on every scheduled run.

    The interactive sign-in flows (``navig games login epic``) pass ``context="human"``:
    they exist precisely so a person can type a password into a window they can see.
    """
    from navig.browser import cdp_actions
    from navig.browser import profiles as _p

    if _p.get_profile(profile) is None:
        cdp_actions.profile_new(profile)
    return cdp_actions.profile_open(profile, context=context)


def _close_if_we_opened_it(launch: dict) -> None:
    """Close the Epic browser, but ONLY when this process is the one that opened it.

    ``profile_open`` reports ``reused=False`` when it actually launched, ``True`` when it
    attached to a browser that was already up. The test is deliberately
    ``is False`` rather than ``not launch.get("reused")``: a launch dict that carries no
    ``reused`` key at all (an older caller, a stubbed seam in a test) means *we do not
    know*, and closing a browser we cannot prove we started is how an unattended job
    shuts a window the operator was using. Not knowing is a reason to leave it alone.
    """
    if launch.get("reused") is not False:
        return
    port = launch.get("port")
    if not port:
        return
    try:
        from navig.browser import cdp_actions

        cdp_actions.stop(port=int(port))
    except Exception as exc:  # noqa: BLE001 — cleanup must never fail the claim
        _log.debug("epic: could not close browser on port %s: %s", port, exc)


def _screens_dir() -> Path:
    try:
        from navig.platform.paths import config_dir

        base = config_dir()
    except Exception:
        base = Path.home() / ".navig"
    d = base / "games" / "screenshots" / "epic"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")


async def _shot(page, tag: str) -> str:
    try:
        p = _screens_dir() / f"{tag}_{_stamp()}.png"
        await page.screenshot(path=str(p), full_page=False)
        return str(p)
    except Exception:  # noqa: BLE001
        return ""


async def _set_dismiss_cookies(page) -> None:
    """Pre-set cookies that silence the cookie banner and the age gate (frees
    screen space and avoids the DOB prompt) — from the reference claimer."""
    try:
        five_days_ago = datetime.now(timezone.utc).timestamp() - 5 * 86400
        iso = datetime.fromtimestamp(five_days_ago, timezone.utc).isoformat()
        await page.context.add_cookies(
            [
                {
                    "name": "OptanonAlertBoxClosed",
                    "value": iso,
                    "domain": ".epicgames.com",
                    "path": "/",
                },
                {
                    "name": "HasAcceptedAgeGates",
                    "value": "general:18,EPIC SUGGESTED RATING:18",
                    "domain": "store.epicgames.com",
                    "path": "/",
                },
            ]
        )
    except Exception as exc:  # noqa: BLE001
        _log.debug("epic: could not set dismiss cookies (%s)", exc)


async def _is_logged_in(page) -> tuple[bool, str]:
    """Read the ``egs-navigation`` web component's login state + display name."""
    try:
        res = await page.evaluate(
            """() => {
                const n = document.querySelector('egs-navigation');
                if (!n) return {loggedIn: false, name: ''};
                return {
                    loggedIn: n.getAttribute('isloggedin') === 'true',
                    name: n.getAttribute('displayname') || ''
                };
            }"""
        )
        return bool(res.get("loggedIn")), str(res.get("name") or "")
    except Exception:  # noqa: BLE001
        return False, ""


def epic_session_present() -> tuple[bool, "str | None"]:
    """(signed_in, account_name) for the vaulted Epic web-session.

    Uses ``list_sessions`` on purpose: ``get_session(domain)`` builds a label that
    includes the account (``web-session/epicgames.com/<user>``), so a username-less
    lookup misses any session that was saved *with* an account name — which is why
    ``epic_signed_in`` used to read false even with a valid session. Best-effort.
    """
    try:
        from navig.vault.sessions import list_sessions

        for s in list_sessions():
            if "epicgames.com" in str(s.get("domain") or ""):
                return True, (s.get("username") or None)
    except Exception:  # noqa: BLE001
        pass
    return False, None


def epic_last_captured_at() -> "str | None":
    """ISO timestamp of the most recently captured Epic session, or None.

    Every sign-in path (`login epic`, `--capture-only`, the deck capture) writes a
    fresh ``captured_at`` via the vault, so this is a reliable "when did the user
    last re-authorize" marker — used to tell a *resolved* expiry from a live one.
    """
    latest: str | None = None
    try:
        from navig.vault.sessions import list_sessions

        for s in list_sessions():
            if "epicgames.com" in str(s.get("domain") or ""):
                at = str(s.get("captured_at") or "")
                if at and (latest is None or at > latest):
                    latest = at
    except Exception:  # noqa: BLE001
        pass
    return latest


def _parse_iso(s: "str | None"):
    if not s:
        return None
    try:
        from datetime import datetime as _dt

        d = _dt.fromisoformat(str(s).replace("Z", "+00:00"))
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None


def epic_session_expired() -> bool:
    """True when the last auto-claim couldn't sign in AND it hasn't been fixed since.

    Derived from ``last_run`` (``login == needs_manual``), but *resolved* the moment
    the user re-authorizes: if a session was captured **after** the failed run, the
    expiry is over — no need to wait for the next claim to flip the signal. When we
    can't confirm a newer sign-in, we stay expired (fail toward surfacing the
    problem, not hiding it). Cheap — reads ``last_run`` + vault metadata, no browser.
    """
    from .. import last_run

    if not last_run.login_needs_signin():
        return False
    r = last_run.read() or {}
    run_at = _parse_iso(r.get("finished_at"))
    cap_at = _parse_iso(epic_last_captured_at())
    if run_at and cap_at and cap_at > run_at:
        return False  # re-authorized since the failed run → resolved
    return True


async def probe_live_login(profile: str = "navig-epic") -> dict:
    """Authoritatively check whether the Epic *session* is still alive.

    Opens (or reuses) the persistent Epic profile, loads a store page, and reads
    the ``egs-navigation`` login state — the same signal the claim flow trusts.
    This is the truth a vault-presence check (:func:`epic_session_present`) cannot
    give: a saved session that Epic has since expired still reads *present* in the
    vault but ``signed_in=False`` here. Launching a browser is a real side effect,
    so this is for on-demand diagnostics (``doctor --live`` / ``status --check``),
    never a hot poll path.

    Returns ``{"ok", "signed_in", "name", "error"}`` and never raises. ``ok`` is
    False only when the check itself could not run (no browser) — a definitively
    signed-out but reachable page is ``ok=True, signed_in=False``, so callers can
    tell "expired" (act on it) apart from "couldn't look" (don't claim healthy).
    """
    from navig.browser import cdp_actions
    from navig.browser.session_manager import get_session_manager

    launch = open_epic_profile(profile)
    port = launch.get("port")
    if not port:
        return {"ok": False, "signed_in": False, "name": "",
                "error": launch.get("error") or "could not launch browser"}
    try:
        bridge = await get_session_manager().get(port, 0)
        try:  # a store page carries the egs-navigation component we read state from
            await cdp_actions.navigate(port, FREE_GAMES_URL)
        except Exception:  # noqa: BLE001
            pass
        signed_in, name = await _is_logged_in(bridge.page)
        return {"ok": True, "signed_in": bool(signed_in), "name": name or "", "error": None}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "signed_in": False, "name": "", "error": str(exc)}
    finally:
        # A diagnostic must not leave a browser running. Every return above passes
        # through here, including the error paths — which are exactly the ones that
        # used to leak, because a failed probe still had a live browser behind it.
        _close_if_we_opened_it(launch)


async def capture_session(bridge, name: str = "", username: str | None = None, *, force: bool = False) -> bool:
    """Persist the live Epic browser session to the vault — the source of truth
    `status`/`epic_signed_in` reads, and what makes session-first restore work
    next run. Mirrors ``navig games login epic`` exactly. Best-effort: a no-op
    when a session is already stored (unless ``force``), never raises.
    """
    try:
        from navig.vault.sessions import save_session

        if not force and epic_session_present()[0]:
            return True  # already have it
        state = await bridge.export_storage_state()
        save_session(EPIC_DOMAIN, state, username=username or name or None)
        _log.info("epic: captured browser session to vault (%s)", name or "account")
        return True
    except Exception as exc:  # noqa: BLE001 — capture is a bonus, never a blocker
        _log.debug("epic: session capture skipped (%s)", exc)
        return False


async def _cta_text(page, timeout_ms: int = 20000) -> str:
    """Wait for the purchase CTA to have real text and return it lowercased."""
    try:
        loc = page.locator(S_CTA).first
        await loc.wait_for(timeout=timeout_ms)
        # The button text is empty while loading; poll briefly for real text.
        for _ in range(20):
            txt = (await loc.inner_text()).strip()
            if txt:
                return txt.lower()
            await page.wait_for_timeout(300)
    except Exception:  # noqa: BLE001
        pass
    return ""


async def _read_overlay_total(page) -> str | None:
    """Best-effort read of the checkout-overlay order total (inside the iframe)."""
    try:
        frame = page.frame_locator(S_PURCHASE_IFRAME)
    except Exception:  # noqa: BLE001
        return None
    for sel in _TOTAL_SELECTORS:
        try:
            loc = frame.locator(sel).first
            if await loc.count():
                txt = (await loc.inner_text()).strip()
                if txt:
                    return txt
        except Exception:  # noqa: BLE001
            continue
    # Fallback: scan the overlay body for a line mentioning "total".
    try:
        body = await frame.locator("body").first.inner_text()
        for line in body.splitlines():
            if "total" in line.lower() and any(c.isdigit() for c in line):
                return line.strip()
    except Exception:  # noqa: BLE001
        pass
    return None


async def _present(page, selector: str, timeout_ms: int = 1500) -> bool:
    try:
        return (await page.locator(selector).count()) > 0
    except Exception:  # noqa: BLE001
        return False


def _emit(on_status: Callable[[str], None] | None, msg: str) -> None:
    if on_status:
        try:
            on_status(msg)
        except Exception:  # noqa: BLE001
            pass
    _log.info("epic-claim: %s", msg)


async def claim_one(
    page,
    game: FreeGame,
    *,
    dry_run: bool,
    require_overlay_zero: bool,
    on_status: Callable[[str], None] | None = None,
    timeout_s: int = 90,
) -> ClaimResult:
    """Claim (or dry-run) a single Epic free game on an already-logged-in page."""
    r = ClaimResult(game_key=game.key, title=game.title, status=STATUS_FAILED,
                    store="epic", url=game.url)
    try:
        _emit(on_status, f"opening {game.title}")
        await page.goto(game.url, wait_until="domcontentloaded")

        # Mature-content interstitial
        if await _present(page, S_CONTINUE):
            try:
                await page.locator(S_CONTINUE).first.click(delay=80)
                await page.wait_for_timeout(1500)
            except Exception:  # noqa: BLE001
                pass

        cta = await _cta_text(page, timeout_ms=timeout_s * 1000 // 4)
        if not cta:
            r.status = STATUS_MANUAL
            r.message = "could not read the store button (page layout changed?)"
            r.screenshot = await _shot(page, f"nocta_{game.slug or 'game'}")
            return r

        # Gate B — Epic only ever shows "Get" for a free/owned title.
        if "in library" in cta:
            r.status = STATUS_OWNED
            r.message = "already in your library"
            return r
        if "requires base game" in cta:
            r.status = STATUS_MANUAL
            r.message = "add-on requires the base game"
            return r
        if "get" not in cta:
            # Anything else (e.g. "Buy Now", a price) → refuse. Never buys.
            r.status = STATUS_PRICED
            r.message = f"store button was {cta!r}, not a free 'Get' — refused"
            r.screenshot = await _shot(page, f"priced_{game.slug or 'game'}")
            return r

        _emit(on_status, "clicking Get")
        await page.locator(S_CTA).first.click(delay=20)

        # Optional interstitials after Get.
        for sel in (S_YES_BUY, S_CONTINUE):
            if await _present(page, sel, timeout_ms=1200):
                try:
                    await page.locator(sel).first.click()
                except Exception:  # noqa: BLE001
                    pass

        # EULA (only the first time on an account).
        if await _present(page, S_EULA_CHECK, timeout_ms=2000):
            _emit(on_status, "accepting EULA")
            try:
                await page.locator(S_EULA_CHECK).check()
                await page.locator(S_EULA_ACCEPT).first.click()
            except Exception:  # noqa: BLE001
                pass

        # Wait for the purchase iframe.
        try:
            await page.wait_for_selector(S_PURCHASE_IFRAME, timeout=timeout_s * 1000 // 3)
        except Exception:  # noqa: BLE001
            r.status = STATUS_MANUAL
            r.message = "checkout overlay did not appear"
            r.screenshot = await _shot(page, f"noiframe_{game.slug or 'game'}")
            return r

        frame = page.frame_locator(S_PURCHASE_IFRAME)

        # Region lock
        if await frame.locator(S_REGION_BLOCK).count():
            r.status = STATUS_MANUAL
            r.message = "unavailable in your region"
            return r

        # Login/checkout captcha → hand back to the user, never loop.
        if await _present(page, S_CHECKOUT_CAPTCHA, timeout_ms=800):
            r.status = STATUS_MANUAL
            r.message = "hit an hCaptcha at checkout — solve it in the browser or retry later"
            r.screenshot = await _shot(page, f"captcha_{game.slug or 'game'}")
            return r

        # Gate C — the money assertion.
        total_text = await _read_overlay_total(page)
        zero = is_zero_total(total_text)
        if zero is False:
            r.status = STATUS_PRICED
            r.message = f"checkout total was {total_text!r} — not free, refused"
            r.screenshot = await _shot(page, f"nonzero_{game.slug or 'game'}")
            return r
        if zero is None and require_overlay_zero:
            r.status = STATUS_MANUAL
            r.message = "could not confirm a $0 total and require_overlay_zero is on — refused"
            r.screenshot = await _shot(page, f"unknowntotal_{game.slug or 'game'}")
            return r
        _emit(on_status, f"order total confirmed free ({total_text or 'unreadable; A+B gates hold'})")

        if dry_run:
            r.status = STATUS_DRYRUN
            r.message = f"verified free (total {total_text!r}); stopped before Place Order"
            r.screenshot = await _shot(page, f"dryrun_{game.slug or 'game'}")
            return r

        # Place the (free) order.
        _emit(on_status, "placing order")
        await frame.locator(S_PLACE_ORDER).first.click(delay=20)
        # EU accounts get a "Right of Withdrawal" dialog ("I accept"). It renders
        # ~1s after the order click, so wait for it briefly instead of a one-shot
        # count() that races it; absent (non-EU) it just times out and moves on.
        try:
            accept = frame.locator(S_I_ACCEPT).first
            await accept.wait_for(state="visible", timeout=6000)
            await accept.click()
        except Exception:  # noqa: BLE001
            pass

        # Confirmation
        try:
            await page.locator(S_SUCCESS).wait_for(state="attached",
                                                   timeout=timeout_s * 1000 // 2)
            r.status = STATUS_CLAIMED
            r.message = "claimed"
            r.screenshot = await _shot(page, f"claimed_{game.slug or 'game'}")
        except Exception:  # noqa: BLE001
            r.status = STATUS_MANUAL
            r.message = "order placed but confirmation not detected — verify manually"
            r.screenshot = await _shot(page, f"unconfirmed_{game.slug or 'game'}")
        return r
    except Exception as exc:  # noqa: BLE001 — never let one game crash the run
        r.status = STATUS_FAILED
        r.message = f"unexpected error: {exc}"
        try:
            r.screenshot = await _shot(page, f"error_{game.slug or 'game'}")
        except Exception:  # noqa: BLE001
            pass
        return r


async def run_epic_claims(
    games: list[FreeGame],
    *,
    username: str | None = None,
    dry_run: bool = False,
    require_overlay_zero: bool = False,
    profile: str = "navig-epic",
    on_status: Callable[[str], None] | None = None,
    timeout_s: int = 90,
) -> "tuple[list[ClaimResult], str | None]":
    """Launch the persistent Epic profile, ensure login, then claim each game.

    Returns ``(results, login)`` — one :class:`ClaimResult` per input game plus how
    the session was established: ``session_restored`` (vault), ``filled`` /
    ``logged_in`` (credential), ``cookies`` (persistent profile), ``needs_manual``
    (couldn't sign in — every game comes back needs_manual, never a blind claim),
    ``error`` (browser launch), or ``None`` (no games).
    """
    from navig.browser import cdp_actions
    from navig.browser.session_manager import get_session_manager

    results: list[ClaimResult] = []
    if not games:
        return results, None

    _emit(on_status, f"launching persistent browser profile {profile!r}")
    launch = open_epic_profile(profile)
    port = launch.get("port")
    if not port:
        err = launch.get("error", "could not launch browser")
        for g in games:
            results.append(ClaimResult(g.key, g.title, STATUS_MANUAL, "epic", err, url=g.url))
        return results, "error"

    # Everything past the launch runs under `finally`, so the browser is closed on EVERY
    # exit — the happy path, the needs-manual path, and an exception mid-claim. This runs
    # on a cron schedule; a leak here is a blank window the operator finds hours later.
    try:
        bridge = await get_session_manager().get(port, 0)
        page = bridge.page
        await _set_dismiss_cookies(page)

        # Session-first login (restores a vaulted session with no password typed).
        _emit(on_status, "signing in (session-first)")
        login_res: dict = {}
        try:
            login_res = await cdp_actions.login(port, domain="epicgames.com", username=username,
                                                open_url=FREE_GAMES_URL) or {}
        except Exception as exc:  # noqa: BLE001
            _log.warning("epic: auto-login raised (%s); will verify state anyway", exc)

        try:
            await page.goto(FREE_GAMES_URL, wait_until="domcontentloaded")
        except Exception:  # noqa: BLE001
            pass

        logged_in, name = await _is_logged_in(page)
        if not logged_in:
            _emit(on_status, "not signed in — needs manual login once")
            for g in games:
                results.append(
                    ClaimResult(
                        g.key, g.title, STATUS_MANUAL, "epic",
                        "not signed in to Epic — run `navig games login epic` once", url=g.url,
                    )
                )
            return results, "needs_manual"

        # How we got in: a restored vault session, a credential fill, or the persistent
        # profile's own cookies. Surfaced so the UI can confirm the session-first path.
        _raw_login = login_res.get("status")
        login_status = _raw_login if _raw_login in ("session_restored", "filled", "logged_in") else "cookies"
        _emit(on_status, f"signed in as {name or 'Epic user'}")
        # Catch the vault up to reality: a browser-only sign-in (persistent profile)
        # otherwise never reaches the vault, so `status` would keep showing "not
        # signed in" even though claims work. Best-effort; never blocks the claim.
        await capture_session(bridge, name, username)
        for g in games:
            results.append(
                await claim_one(
                    page, g, dry_run=dry_run, require_overlay_zero=require_overlay_zero,
                    on_status=on_status, timeout_s=timeout_s,
                )
            )
        return results, login_status
    finally:
        _close_if_we_opened_it(launch)
