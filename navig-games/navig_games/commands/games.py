"""navig games — find & auto-claim FREE games (Epic now; GOG/Amazon/Steam next).

House style: Rich tables + a ``--json`` twin for scripts/agents. FREE-ONLY — the
claim flow completes only a $0 checkout and never spends real money.
"""

from __future__ import annotations

import json as _json
import sys
from typing import Annotated

import typer
from navig.lazy_loader import lazy_import
from rich.table import Table

ch = lazy_import("navig.console_helper")

games_app = typer.Typer(
    name="games",
    help="🎮 Free games: Epic (auto-claimed) · Steam free-to-keep · cross-store giveaways. FREE-ONLY, never buys.",
    no_args_is_help=True,
)

EPIC_DOMAIN = "epicgames.com"

_STATUS_STYLE = {
    "claimed": "[green]● claimed[/green]",
    "already_owned": "[dim]○ owned[/dim]",
    "grabbed": "[green]● got it[/green]",
    "dry_run": "[cyan]✓ free (dry-run)[/cyan]",
    "needs_manual": "[yellow]⚠ needs you[/yellow]",
    "skipped_priced": "[red]✗ not free[/red]",
    "failed": "[red]✗ failed[/red]",
}


def _fmt_status(status: str) -> str:
    return _STATUS_STYLE.get(status, status)


def _ends_cell(g: dict) -> str:
    """The 'Ends' column: a colour-graded countdown so imminent freebies stand out.

    Red == "ending soon" (the same threshold the scheduled reminder uses).
    """
    from navig_games.engine.models import ENDING_SOON_DAYS

    d = g.get("ends_in_days")
    if d is None:
        raw = (g.get("ends_at") or "")[:10]
        return raw or "[dim]—[/dim]"   # open-ended / unknown
    if d == 0:
        return "[red]today[/red]"
    if d == 1:
        return "[red]tomorrow[/red]"
    if d <= ENDING_SOON_DAYS:
        return f"[red]in {d}d[/red]"
    if d <= 7:
        return f"[yellow]in {d}d[/yellow]"
    return f"[dim]in {d}d[/dim]"


def _ago(iso: str | None) -> str:
    """A short ' · Nh ago' suffix from an ISO timestamp; '' if unparseable."""
    if not iso:
        return ""
    try:
        from datetime import datetime, timezone

        then = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
        if then.tzinfo is None:
            then = then.replace(tzinfo=timezone.utc)
        secs = (datetime.now(timezone.utc) - then).total_seconds()
        if secs < 90:
            return " · just now"
        if secs < 5400:
            return f" · {round(secs / 60)}m ago"
        if secs < 172800:
            return f" · {round(secs / 3600)}h ago"
        return f" · {round(secs / 86400)}d ago"
    except (ValueError, TypeError):
        return ""


def _last_claim_row() -> "str | None":
    """The most recent real auto-claim outcome — a cheap health line (no browser).

    Surfaces the ``last_run`` rollup the scheduled claim writes; most importantly it
    flags an expired sign-in (``login == needs_manual``) so a failing daily claim is
    visible without ``--check``. Returns None when no real claim has run yet.
    """
    from navig_games.engine import last_run

    r = last_run.read()
    if not r:
        return None
    age = _ago(r.get("finished_at"))
    if r.get("login") == "needs_manual":
        from navig_games.engine.claim.epic import epic_session_expired

        if epic_session_expired():
            return f"[yellow]⚠ couldn't sign in{age} — run navig games login epic[/yellow]"
        # The failed run has since been resolved by a re-login; drop the stale nudge.
        return f"[dim]○ couldn't sign in{age} · re-authorized, claim due next run[/dim]"
    claimed = int(r.get("claimed") or 0)
    needs = int(r.get("needs_manual") or 0)
    if claimed:
        return f"[green]● {claimed} claimed{age}[/green]"
    if needs:
        return f"[yellow]○ {needs} need you{age}[/yellow]"
    return f"[dim]○ up to date{age}[/dim]"


def _epic_login_row(live: bool) -> str:
    """Render the 'Epic login' status cell.

    Default is the cheap vault-presence check. A saved session is shown as *saved*
    — never as "signed in" — because a session Epic has since expired still sits in
    the vault. ``live=True`` opens the browser and reads the real login state, so it
    can distinguish a working session from an expired one (the case that silently
    made every scheduled claim fail with a green light on this row).
    """
    try:
        from navig_games.engine.claim.epic import epic_session_present
    except Exception as exc:  # noqa: BLE001
        return f"[dim]vault unavailable ({exc})[/dim]"

    present, name = epic_session_present()
    who = f" ({name})" if name else ""
    if not present:
        return "[yellow]○ not signed in (navig games login epic)[/yellow]"
    if not live:
        return f"[green]● session saved[/green]{who} [dim]· --check to verify[/dim]"

    from navig.browser.cdp_runtime import run

    from navig_games.engine.claim.epic import probe_live_login

    probe = run(probe_live_login())
    if not probe.get("ok"):
        return f"[yellow]● session saved{who} · couldn't verify ({probe.get('error')})[/yellow]"
    if probe.get("signed_in"):
        nm = probe.get("name") or name or ""
        return f"[green]● live[/green]{f' · {nm}' if nm else ''}"
    return f"[red]✗ session expired{who} — run navig games login epic[/red]"


def _emit_json(obj) -> None:
    ch.console.print_json(_json.dumps(obj, default=str))


# ---------------------------------------------------------------------------
# check — sourcing only (no browser, no login)
# ---------------------------------------------------------------------------
@games_app.command("check")
def cmd_check(
    upcoming: Annotated[bool, typer.Option("--upcoming", "-u", help="Also list upcoming freebies")] = False,
    store: Annotated[str, typer.Option("--store", "-s",
        help="Store: all | epic | steam | giveaways")] = "all",
    json_out: Annotated[bool, typer.Option("--json", help="Machine-readable output")] = False,
):
    """🔎 Show what's free right now (and, with --upcoming, what's next)."""
    from navig_games.engine import runner

    data = runner.check(store=store)
    if json_out:
        _emit_json(data)
        return

    if data.get("error"):
        ch.error(data["error"])
        raise typer.Exit(1)

    current = data["current"]
    if not current:
        ch.warning(f"No free games on {store.title()} right now.")
    else:
        table = Table(box=None, show_header=True, padding=(0, 2),
                      title=f"[bold]Free now on {store.title()}[/bold]", title_justify="left")
        table.add_column("Store", no_wrap=True, style="dim")
        table.add_column("Title", no_wrap=False, style="bold")  # the one wrappable column
        table.add_column("Was", no_wrap=True, justify="right")
        table.add_column("Ends", no_wrap=True)
        table.add_column("Status", no_wrap=True)
        table.add_column("Key", no_wrap=True, style="dim")  # the handle for claim --game / grab
        for g in current:
            status = g.get("claim_status")
            table.add_row(
                g.get("store", "—"),
                g["title"],
                g.get("original_price") or "—",
                _ends_cell(g),
                _fmt_status(status) if status else "[dim]—[/dim]",
                g.get("key", ""),
            )
        ch.console.print(table)

    if upcoming and data.get("upcoming"):
        up = Table(box=None, show_header=True, padding=(0, 2),
                   title="[bold]Coming soon[/bold]", title_justify="left")
        up.add_column("Title", style="dim")
        up.add_column("Starts", no_wrap=True)
        for g in data["upcoming"]:
            up.add_row(g["title"], (g.get("starts_at") or "")[:10])
        ch.console.print(up)

    n = len(current)
    if not n:
        ch.console.print("[dim]check back Thursday — Epic rotates weekly[/dim]")
        return
    # Grab-before-gone: warn about anything un-settled ending within the window.
    from navig_games.engine.models import ENDING_SOON_DAYS

    ending = sum(1 for g in current
                 if g.get("ends_in_days") is not None and g["ends_in_days"] <= ENDING_SOON_DAYS
                 and not g.get("claim_status"))
    if ending:
        it = "it" if ending == 1 else "them"
        ch.console.print(
            f"[red]⏰ {ending} ending within {ENDING_SOON_DAYS} days — grab {it} now[/red]")

    # Only Epic is auto-claimable; everything else settles with `grab`, so say both.
    manual = sum(1 for g in current if g.get("store") != "epic" and not g.get("claim_status"))
    nudge = f"[dim]{n} free now · auto-claim Epic with[/dim] [bold]navig games claim[/bold]"
    if manual:
        nudge += (f"[dim] · {manual} to grab yourself — mark one done with[/dim] "
                  "[bold]navig games grab <key>[/bold]")
    ch.console.print(nudge)


# ---------------------------------------------------------------------------
# claim — the FREE-ONLY auto-claim
# ---------------------------------------------------------------------------
@games_app.command("claim")
def cmd_claim(
    store: Annotated[str, typer.Option("--store", "-s", help="Store (epic)")] = "epic",
    all_: Annotated[bool, typer.Option("--all", help="Re-attempt every current freebie, even if already recorded")] = False,
    game: Annotated[str | None, typer.Option("--game", help="Claim only this game (by its ledger key, e.g. epic:<slug>)")] = None,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Verify free & stop before placing the order")] = False,
    strict: Annotated[bool, typer.Option("--strict", help="Require a readable $0 total in the overlay (else refuse)")] = False,
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Don't prompt before a real claim")] = False,
    json_out: Annotated[bool, typer.Option("--json", help="Machine-readable output")] = False,
):
    """🕹  Claim the currently-free games into your account.

    FREE-ONLY: only a $0 checkout is ever completed; a priced title is refused.
    Use --dry-run to watch it verify the free total and stop before ordering.
    """
    from navig_games.engine import runner

    # Real claims touch your live account — confirm unless --yes / --dry-run / non-tty piped.
    if not dry_run and not yes and sys.stdin.isatty() and not json_out:
        preview = runner.check(store=store)
        pending = preview.get("current", [])
        if not pending:
            ch.warning("No free games to claim right now.")
            raise typer.Exit(0)
        titles = ", ".join(g["title"] for g in pending)
        ch.info(f"About to claim into your {store.title()} account: {titles}")
        if not typer.confirm("Proceed?", default=False):
            ch.warning("Cancelled.")
            raise typer.Exit(0)

    def _status(msg: str) -> None:
        if not json_out:
            ch.info(f"  … {msg}")

    result = runner.run_claim_sync(
        store=store, dry_run=dry_run, force=all_, only=game,
        require_overlay_zero=strict, on_status=_status,
    )

    if json_out:
        _emit_json(result)
        return

    if result.get("error"):
        ch.error(result["error"])
        raise typer.Exit(1)

    results = result.get("results", [])
    if not results:
        ch.warning(result.get("message", "Nothing to claim."))
        return

    table = Table(box=None, show_header=True, padding=(0, 2))
    table.add_column("Game", style="bold", no_wrap=False)
    table.add_column("Result", no_wrap=True)
    table.add_column("Note", no_wrap=False, style="dim")
    for r in results:
        table.add_row(r["title"], _fmt_status(r["status"]), r.get("message", ""))
    ch.console.print(table)

    claimed = result.get("claimed", 0)
    if dry_run:
        ch.success(f"Dry-run complete — {sum(1 for r in results if r['status'] == 'dry_run')} verified free, none ordered.")
    elif claimed:
        ch.success(f"Claimed {claimed} free game(s). 🎮")
    else:
        ch.info("No new games claimed (see notes above).")


# ---------------------------------------------------------------------------
# login — one-time onboarding so unattended claims work via session restore
# ---------------------------------------------------------------------------
login_app = typer.Typer(name="login", help="Sign in to a store once (session saved to the vault)",
                        no_args_is_help=True)
games_app.add_typer(login_app, name="login", rich_help_panel="Setup")


@login_app.command("epic")
def login_epic(
    user: Annotated[str | None, typer.Option("--user", "-u", help="Epic email (optional; for stored auto-fill)")] = None,
    capture_only: Annotated[bool, typer.Option("--capture-only", help="Non-interactive: capture an already signed-in profile into the vault, or open the login page. Used by the OS app.", hidden=True)] = False,
    json_out: Annotated[bool, typer.Option("--json", help="Machine-readable output")] = False,
):
    """🔐 Open Epic in a persistent browser, sign in once, and save the session.

    After this, `navig games claim` restores the session with no password typed.
    Your credentials never leave the machine (navig vault).
    """
    from navig.browser import cdp_actions
    from navig.browser.cdp_runtime import run
    from navig.browser.session_manager import get_session_manager

    from navig_games.engine.claim.epic import (
        FREE_GAMES_URL,
        LOGIN_URL,
        _is_logged_in,
        capture_session,
        open_epic_profile,
    )

    if capture_only:
        # One-shot, non-interactive: connect the persistent profile and, if it's
        # already signed in, capture the session to the vault; otherwise open the
        # login page so the user can sign in and re-run. Drives the OS app's
        # "Sign in to Epic" button (spawned as a subprocess off the daemon loop).
        async def _capture_only(username: str | None) -> dict:
            # `human`: when the session turns out to be dead this navigates to LOGIN_URL
            # and tells the operator to "sign in there, then run this again" — advice that
            # is only true if there is a window for them to sign in to.
            launch = open_epic_profile("navig-epic", context="human")
            port = launch.get("port")
            if not port:
                return {"ok": False, "error": launch.get("error") or "could not launch browser"}
            bridge = await get_session_manager().get(port, 0)
            try:  # a store page carries the egs-navigation component we read state from
                await cdp_actions.navigate(port, FREE_GAMES_URL)
            except Exception:  # noqa: BLE001
                pass
            logged_in, name = await _is_logged_in(bridge.page)
            if logged_in:
                ok = await capture_session(bridge, name, username, force=True)
                return {"ok": ok, "name": name}
            try:
                await cdp_actions.navigate(port, LOGIN_URL)
            except Exception:  # noqa: BLE001
                pass
            return {"ok": False, "opened": True}

        result = run(_capture_only(user))
        if json_out:
            _emit_json(result)
        elif result.get("ok"):
            ch.success(f"Epic session captured for {result.get('name') or 'your account'}.")
        elif result.get("opened"):
            ch.info("Opened the Epic login page — sign in there, then run this again.")
        else:
            ch.warning(result.get("error") or "Couldn't capture an Epic session.")
        return

    ch.info("Opening a persistent Epic browser profile (navig-epic) …")
    # `human`: the whole command is "complete the Epic sign-in in the opened browser
    # window (solve any 2FA/captcha)" — it cannot work headless.
    launch = open_epic_profile("navig-epic", context="human")
    port = launch.get("port")
    if not port:
        ch.error(launch.get("error") or "Could not launch the browser. Is Chrome/Chromium installed?")
        raise typer.Exit(1)

    async def _open_and_try():
        await cdp_actions.navigate(port, LOGIN_URL)
        # If a vault login exists, let autofill attempt it (session-first + TOTP).
        try:
            await cdp_actions.login(port, domain=EPIC_DOMAIN, username=user, open_url=LOGIN_URL)
        except Exception:  # noqa: BLE001
            pass

    run(_open_and_try())
    ch.info("Complete the Epic sign-in in the opened browser window (solve any 2FA/captcha).")
    typer.confirm("Signed in?  Press Enter to capture the session", default=True)

    async def _capture():
        bridge = await get_session_manager().get(port, 0)
        logged_in, name = await _is_logged_in(bridge.page)
        if not logged_in:
            return {"ok": False}
        state = await bridge.export_storage_state()
        from navig.vault.sessions import save_session

        save_session(EPIC_DOMAIN, state, username=user or name or None)
        return {"ok": True, "name": name}

    res = run(_capture())
    if res.get("ok"):
        ch.success(f"Epic session saved for {res.get('name') or 'your account'}. "
                   "Unattended claims are ready.")
    else:
        ch.warning("Didn't detect a signed-in Epic session. Try again, then re-run this.")


@login_app.command("list")
def login_list():
    """List stored store sessions/logins."""
    try:
        from navig.vault.sessions import list_sessions
    except Exception as exc:  # noqa: BLE001
        ch.error(f"Vault unavailable: {exc}")
        raise typer.Exit(1) from exc
    sessions = list_sessions()
    epic = [s for s in sessions if EPIC_DOMAIN in str(getattr(s, "domain", s))]
    if not epic:
        ch.warning("No store sessions saved. Run: navig games login epic")
        return
    table = Table(box=None, show_header=True, padding=(0, 2))
    table.add_column("Store")
    table.add_column("Account")
    for s in epic:
        table.add_row("Epic", str(getattr(s, "username", "") or "—"))
    ch.console.print(table)


@login_app.command("remove")
def login_remove():
    """Remove the saved Epic session."""
    try:
        from navig.vault.sessions import remove_session

        remove_session(EPIC_DOMAIN)
        ch.success("Removed the saved Epic session.")
    except Exception as exc:  # noqa: BLE001
        ch.error(f"Could not remove session: {exc}")
        raise typer.Exit(1) from exc


# ---------------------------------------------------------------------------
# schedule — run the claim automatically
# ---------------------------------------------------------------------------
schedule_app = typer.Typer(name="schedule", help="Auto-claim / deal-watch on a schedule",
                           no_args_is_help=True)
games_app.add_typer(schedule_app, name="schedule", rich_help_panel="Automation")

_JOB_LABEL = {"claim": "Auto-claim free games", "deals": "Steam deal watch",
              "unify": "Auto-unify to Steam"}


def _schedule_fns(job: str):
    from navig_games.engine import schedule

    job = job.lower()
    fns = {
        "claim": (schedule.enable, schedule.disable, schedule.status),
        "deals": (schedule.enable_deals, schedule.disable_deals, schedule.deals_status),
        "unify": (schedule.enable_unify, schedule.disable_unify, schedule.unify_status),
    }
    if job not in fns:
        ch.error("--job must be 'claim', 'deals', or 'unify'.")
        raise typer.Exit(1)
    return fns[job]


@schedule_app.command("enable")
def schedule_enable(
    when: Annotated[str, typer.Option("--when", "-w", help="'daily' | 'weekly' | cron expr")] = "daily",
    job: Annotated[str, typer.Option("--job", "-j", help="claim | deals | unify")] = "claim",
):
    """▶ Register a recurring job (auto-claim · deal alerts · unify to Steam)."""
    enable, _, _ = _schedule_fns(job)
    res = enable(when)
    if not res.get("ok"):
        ch.error(res.get("error", "could not update the schedule store."))
        raise typer.Exit(1)
    ch.success(f"{_JOB_LABEL[job.lower()]} scheduled ({res['schedule']}, {res['action']}).")
    ch.info("Restart the daemon to activate:  navig gateway start")


@schedule_app.command("disable")
def schedule_disable(
    job: Annotated[str, typer.Option("--job", "-j", help="claim | deals | unify")] = "claim",
):
    """⏹ Remove a recurring job."""
    _, disable, _ = _schedule_fns(job)
    res = disable()
    if not res.get("ok"):
        ch.error(res.get("error", "could not update the schedule store."))
        raise typer.Exit(1)
    if res["removed"]:
        ch.success(f"{_JOB_LABEL[job.lower()]} schedule removed.")
    else:
        ch.info(f"No {_JOB_LABEL[job.lower()].lower()} schedule was set.")


@schedule_app.command("status")
def schedule_status():
    """Show all NAVIG-games scheduled jobs."""
    from navig_games.engine import schedule

    rows = [("Auto-claim", schedule.status()), ("Steam deals", schedule.deals_status()),
            ("Auto-unify", schedule.unify_status())]
    table = Table(box=None, show_header=True, padding=(0, 2))
    table.add_column("Job", style="bold")
    table.add_column("State", no_wrap=True)
    table.add_column("Schedule", no_wrap=True)
    table.add_column("Next run", style="dim")
    for label, s in rows:
        state = "[green]● on[/green]" if s.get("enabled") else "[dim]○ off[/dim]"
        table.add_row(label, state, str(s.get("schedule") or "—"), str(s.get("next_run") or "—"))
    ch.console.print(table)


# ---------------------------------------------------------------------------
# deals — Steam wishlist price-drop + free-to-keep watcher
# ---------------------------------------------------------------------------
deals_app = typer.Typer(name="deals", help="Steam wishlist deals & free-to-keep alerts",
                        no_args_is_help=False, invoke_without_command=True)
games_app.add_typer(deals_app, name="deals", rich_help_panel="Steam")


@deals_app.callback(invoke_without_command=True)
def deals_root(
    ctx: typer.Context,
    threshold: Annotated[int, typer.Option("--threshold", "-t", help="Min discount %% to show")] = 20,
    cc: Annotated[str, typer.Option("--cc", help="Country / currency code")] = "us",
    no_wishlist: Annotated[bool, typer.Option("--no-wishlist", help="Only the manual watchlist")] = False,
    json_out: Annotated[bool, typer.Option("--json", help="Machine-readable output")] = False,
):
    """🏷  Show current deals on your Steam wishlist + watchlist."""
    if ctx.invoked_subcommand is not None:
        return
    from navig_games.engine.sources import steam

    if not json_out:
        ch.info("Checking Steam prices … (your wishlist + watchlist)")
    r = steam.check_deals(threshold=threshold, cc=cc, include_wishlist=not no_wishlist)
    if json_out:
        _emit_json({**r, "deals": [d.to_dict() for d in r["deals"]]})
        return
    if not r.get("steamid") and no_wishlist is False:
        ch.warning("No Steam user detected. Watch games manually: navig games deals watch <appid>")
    deals = r["deals"]
    if not deals:
        ch.warning(f"No deals ≥ {threshold}% right now (checked {r['checked']} game(s)).")
        return
    table = Table(box=None, show_header=True, padding=(0, 2))
    table.add_column("Game", style="bold", no_wrap=False)
    table.add_column("Deal", no_wrap=True)
    table.add_column("Price", no_wrap=True, justify="right")
    table.add_column("", no_wrap=True)
    for d in deals:
        deal = "[magenta]🎁 FREE[/magenta]" if d.is_free else f"[green]-{d.discount_pct}%[/green]"
        table.add_row(d.name, deal, d.final_formatted or "—", "[dim]♥[/dim]" if d.on_wishlist else "")
    ch.console.print(table)
    ch.console.print(f"[dim]{len(deals)} deal(s){' · +more capped' if r.get('capped') else ''} · "
                     f"get alerts: navig games schedule enable --job deals[/dim]")


@deals_app.command("watch")
def deals_watch(app: Annotated[str, typer.Argument(help="Steam appid or store URL")]):
    """Add an appid to the manual watchlist (checked alongside your wishlist)."""
    from navig_games.engine.sources import steam

    appid = steam.parse_appid(app)
    if not appid:
        ch.error("Give a numeric Steam appid or a store URL (…/app/<id>/).")
        raise typer.Exit(1)
    if steam.add_watch(appid):
        ch.success(f"Watching Steam appid {appid} for deals.")
    else:
        ch.info(f"appid {appid} is already on your watchlist.")


@deals_app.command("unwatch")
def deals_unwatch(app: Annotated[str, typer.Argument(help="Steam appid or store URL")]):
    """Remove an appid from the manual watchlist."""
    from navig_games.engine.sources import steam

    appid = steam.parse_appid(app)
    if not appid:
        ch.error("Give a numeric Steam appid or a store URL (…/app/<id>/).")
        raise typer.Exit(1)
    if steam.remove_watch(appid):
        ch.success(f"Removed appid {appid} from your watchlist.")
    else:
        ch.info(f"appid {appid} wasn't on your watchlist.")


@deals_app.command("list")
def deals_list():
    """Show your manual watchlist (your wishlist is watched automatically)."""
    from navig_games.engine.sources import steam

    wl = steam.get_watchlist()
    if not wl:
        ch.warning("Manual watchlist is empty. Your Steam wishlist is watched automatically.\n"
                   "Add a game: navig games deals watch <appid>")
        return
    ch.console.print("Watchlist: " + ", ".join(str(a) for a in wl))


@deals_app.command("notify")
def deals_notify(
    threshold: Annotated[int, typer.Option("--threshold", "-t", help="Min discount %%")] = 20,
    cc: Annotated[str, typer.Option("--cc", help="Country / currency code")] = "us",
    json_out: Annotated[bool, typer.Option("--json", help="Machine-readable output")] = False,
):
    """🔔 Check deals and fire notifications for anything new (the scheduler target)."""
    from navig_games.engine import runner

    result = runner.run_deals_notify(threshold=threshold, cc=cc)
    if json_out:
        _emit_json(result)
        return
    if result.get("notified"):
        ch.success(f"Notified {result['notified']} new deal(s): {', '.join(result['titles'])}")
    else:
        ch.info(f"No new deals to notify (checked {result.get('checked', 0)} game(s)).")


# ---------------------------------------------------------------------------
# grab — "I got it myself" (the terminal state for anything we can't claim)
# ---------------------------------------------------------------------------
@games_app.command("grab")
def cmd_grab(
    key: Annotated[str, typer.Argument(help="Game key from `navig games check` (e.g. itch:3697)")],
    undo: Annotated[bool, typer.Option("--undo", help="Un-mark it (put it back on the list)")] = False,
    json_out: Annotated[bool, typer.Option("--json", help="Machine-readable output")] = False,
):
    """Mark a free game as grabbed — it settles, so it stops nagging you.

    For everything we can't claim headlessly (itch / IndieGala / Stove / GOG /
    Steam free-to-keep) this is the only way a game ever settles. On an Epic game
    it doubles as "skip this one" (a per-game `claim --game <key>` still overrides).
    """
    from ..engine import runner

    r = runner.mark_grabbed(key, grabbed=not undo)
    if json_out:
        _emit_json(r)
        raise typer.Exit(0 if not r.get("error") else 1)
    if r.get("error"):
        ch.error(r["error"])
        raise typer.Exit(1)
    title = r.get("title") or key
    if not r.get("changed"):
        ch.info(f"{title} — {r.get('message', 'nothing to do')}")
        return
    if undo:
        ch.success(f"Un-marked {title} — it's back on the free list.")
    else:
        ch.success(f"Marked {title} as grabbed ✓")
        # Be exact: the row still lists (marked "got it") — what stops is the nagging.
        ch.info("It's settled — no more alerts, and the claim run skips it. Undo with --undo.")


# ---------------------------------------------------------------------------
# history / status / doctor
# ---------------------------------------------------------------------------
@games_app.command("history")
def cmd_history(
    json_out: Annotated[bool, typer.Option("--json", help="Machine-readable output")] = False,
):
    """📜 What has been claimed / attempted."""
    from navig_games.engine.ledger import Ledger

    records = Ledger().all()
    if json_out:
        _emit_json(records)
        return
    if not records:
        ch.warning("No claim history yet.")
        return
    table = Table(box=None, show_header=True, padding=(0, 2))
    table.add_column("Game", style="bold")
    table.add_column("Store", no_wrap=True)
    table.add_column("Result", no_wrap=True)
    table.add_column("When", no_wrap=True, style="dim")
    for r in records:
        table.add_row(r.get("title", ""), r.get("store", ""),
                      _fmt_status(r.get("status", "")), (r.get("updated") or "")[:10])
    ch.console.print(table)


def _store_counts(games) -> str:
    by: dict[str, int] = {}
    for g in games:
        by[g.store] = by.get(g.store, 0) + 1
    return ", ".join(f"{k} {v}" for k, v in sorted(by.items())) or "none"


@games_app.command("status")
def cmd_status(
    check: Annotated[bool, typer.Option(
        "--check", "-c",
        help="Verify the Epic session is still alive (opens a browser).")] = False,
):
    """ℹ️  Plugin, login, library, Steam & schedule status."""
    from navig_games import __version__
    from navig_games.engine import schedule, settings

    table = Table(box=None, show_header=False, padding=(0, 2))
    table.add_row("navig-games", f"v{__version__}")
    table.add_row("Region", f"{settings.country()} · {settings.locale()}")
    table.add_row("Epic login", _epic_login_row(check))
    last_claim = _last_claim_row()
    if last_claim:
        table.add_row("Last claim", last_claim)

    try:
        from navig_games.engine import library

        games = library.scan()
        table.add_row("Installed games", f"{len(games)} ({_store_counts(games)})" if games else "0")
    except Exception:  # noqa: BLE001
        table.add_row("Installed games", "[dim]scan failed[/dim]")

    try:
        from navig_games.engine.library.steam import steam_path
        from navig_games.engine.steam.shortcuts import list_shortcuts

        if steam_path():
            table.add_row("Steam", f"[green]● detected[/green] · "
                                   f"{len(list_shortcuts())} non-Steam shortcut(s)")
        else:
            table.add_row("Steam", "[dim]○ not installed[/dim]")
    except Exception:  # noqa: BLE001
        table.add_row("Steam", "[dim]?[/dim]")

    table.add_row("SteamGridDB key", "[green]● set[/green]" if settings.get("steamgriddb_key")
                  else "[dim]○ not set (GOG-local art only)[/dim]")
    wl = settings.get("steam_watch", []) or []
    table.add_row("Deal watchlist", f"{len(wl)} manual + your wishlist")

    for label, s in (("Auto-claim", schedule.status()), ("Deal alerts", schedule.deals_status()),
                     ("Auto-unify", schedule.unify_status())):
        table.add_row(label, f"[green]● {s['schedule']}[/green]" if s.get("enabled")
                      else "[dim]○ off[/dim]")
    ch.console.print(table)


@games_app.command("doctor")
def cmd_doctor(
    live: Annotated[bool, typer.Option(
        "--live", "-l",
        help="Also verify the Epic session is still alive in a browser (catches an expired login).")] = False,
):
    """🩺 Full self-check across every subsystem (sourcing, browser, vault, Steam, library, art, deals).

    Pass --live to open the Epic profile and confirm the saved session still works
    — a vault-present session can be expired, which is what silently breaks claims.
    """
    ok = True

    def check(label: str, fn) -> None:
        nonlocal ok
        try:
            level, msg = fn()
        except Exception as exc:  # noqa: BLE001
            level, msg = "error", str(exc)
        if level == "success":
            ch.success(f"{label}: {msg}")
        elif level == "warning":
            ch.warning(f"{label}: {msg}")
        else:
            ok = False
            ch.error(f"{label}: {msg}")

    def _epic():
        from navig_games.engine.sources import epic as e

        d = e.fetch_free_games()
        return "success", f"{len(d['current'])} free now, {len(d['upcoming'])} upcoming"

    def _browser():
        import navig.browser.cdp_actions  # noqa: F401

        return "success", "importable"

    def _vault():
        from navig.vault.sessions import get_session

        get_session(EPIC_DOMAIN)
        return "success", "reachable"

    def _epic_session():
        from navig_games.engine.claim.epic import epic_session_present

        present, name = epic_session_present()
        who = f" ({name})" if name else ""
        if not present:
            return "warning", "no saved session — run `navig games login epic`"
        if not live:
            # Honest: a vault-present session may be expired. Don't claim it's
            # healthy — say what we know and how to actually verify it.
            return "success", f"session saved{who} · pass --live to verify it still works"
        from navig.browser.cdp_runtime import run

        from navig_games.engine.claim.epic import probe_live_login

        probe = run(probe_live_login())
        if not probe.get("ok"):
            # Could-not-verify is a warn, never a green tick (doctor-honesty rule).
            return "warning", f"session saved{who} but couldn't verify ({probe.get('error')})"
        if probe.get("signed_in"):
            nm = probe.get("name") or name or ""
            return "success", f"live · signed in{f' as {nm}' if nm else ''}"
        return "error", f"session EXPIRED{who} — re-run `navig games login epic`"

    def _steam():
        from navig_games.engine.library.steam import active_user_id3, steam_path

        sp = steam_path()
        if not sp:
            return "warning", "not installed (library/unify/deals limited)"
        return "success", f"{sp} · user {active_user_id3(sp) or '?'}"

    def _library():
        from navig_games.engine import library

        games = library.scan()
        return "success", f"{len(games)} installed ({_store_counts(games)})"

    def _grid():
        from navig_games.engine.steam.grid import grid_dir

        g = grid_dir()
        return ("success", f"writable ({g})") if g else ("warning", "no Steam grid folder")

    def _deals():
        from navig_games.engine.sources import steam as s

        sid = s.resolve_steamid64()
        if not sid:
            return "warning", "no Steam user detected — watch appids manually"
        return "success", f"wishlist reachable ({len(s.wishlist_appids(sid))} items)"

    for label, fn in (("Epic sourcing", _epic), ("Browser stack", _browser), ("Vault", _vault),
                      ("Epic session", _epic_session),
                      ("Steam", _steam), ("Library scan", _library), ("Cover-art grid", _grid),
                      ("Steam deals", _deals)):
        check(label, fn)
    ch.console.print("[green]All systems go.[/green]" if ok
                     else "[yellow]Some checks FAILED (see above).[/yellow]")
    if not ok:
        # `ok` is cleared only on the error branch of check(), never on a warning, so
        # this means a real failure. A doctor that reports failures and exits 0 cannot
        # gate anything: `navig games doctor && <run it>` proceeded regardless.
        raise typer.Exit(1)


# ---------------------------------------------------------------------------
# library — installed games across launchers
# ---------------------------------------------------------------------------
_STORE_GLYPH = {"steam": "🟦 Steam", "epic": "⬛ Epic", "gog": "🟣 GOG", "amazon": "🟠 Amazon"}


def _parse_stores(store: str | None) -> list[str] | None:
    if not store:
        return None
    return [s.strip().lower() for s in store.split(",") if s.strip()]


@games_app.command("library", rich_help_panel="Library")
def cmd_library(
    store: Annotated[str | None, typer.Option("--store", "-s", help="Comma list: steam,epic,gog,amazon")] = None,
    json_out: Annotated[bool, typer.Option("--json", help="Machine-readable output")] = False,
):
    """🗂  List installed games across Steam, Epic, GOG and Amazon (local scan)."""
    from navig_games.engine import library

    games = library.scan(_parse_stores(store))
    if json_out:
        _emit_json([g.to_dict() for g in games])
        return
    if not games:
        ch.warning("No installed games found (no supported launcher detected).")
        return
    table = Table(box=None, show_header=True, padding=(0, 2))
    table.add_column("Store", no_wrap=True)
    table.add_column("Game", style="bold", no_wrap=False)
    table.add_column("Launch", no_wrap=False, style="dim")
    for g in sorted(games, key=lambda x: (x.store, x.title.lower())):
        launch = g.launch_exe or ("[dim]via " + g.store + "[/dim]" if g.store == "steam" else "—")
        table.add_row(_STORE_GLYPH.get(g.store, g.store), g.title, launch)
    ch.console.print(table)
    n_ext = sum(1 for g in games if g.store != "steam")
    ch.console.print(f"[dim]{len(games)} installed · {n_ext} non-Steam · "
                     f"add them to Steam with[/dim] [bold]navig games unify[/bold]")


def _steamgriddb_key(explicit: str | None = None) -> str | None:
    if explicit:
        return explicit
    from navig_games.engine import settings

    key = settings.get("steamgriddb_key")
    return str(key) if key else None


@games_app.command("unify", rich_help_panel="Library")
def cmd_unify(
    store: Annotated[str | None, typer.Option("--store", "-s", help="Comma list: epic,gog,amazon")] = None,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Preview without writing shortcuts.vdf")] = False,
    force: Annotated[bool, typer.Option("--force", help="Write even if Steam is running (risky)")] = False,
    no_art: Annotated[bool, typer.Option("--no-art", help="Skip fetching cover art")] = False,
    steamgriddb_key: Annotated[str | None, typer.Option("--steamgriddb-key", help="SteamGridDB API key (better art)")] = None,
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Don't prompt")] = False,
    json_out: Annotated[bool, typer.Option("--json", help="Machine-readable output")] = False,
):
    """➕ Add your Epic/GOG/Amazon games to the Steam library (one library for all).

    Fetches cover art automatically (launcher-local art keyless; add a SteamGridDB
    key for full grids). Non-destructive: backs up shortcuts.vdf, dedupes, and
    refuses while Steam runs.
    """
    from navig_games.engine import library
    from navig_games.engine.steam import grid, shortcuts

    games = library.scan_non_steam(_parse_stores(store))
    launchable = [g for g in games if g.launchable]
    if not launchable:
        msg = "No non-Steam games with a resolvable executable were found."
        (ch.warning if not json_out else lambda m: _emit_json({"added": [], "message": m}))(msg)
        return

    if not dry_run and not yes and sys.stdin.isatty() and not json_out:
        titles = ", ".join(g.title for g in launchable)
        ch.info(f"About to add to Steam: {titles}")
        ch.info("(Steam must be closed; a backup of shortcuts.vdf is made.)")
        if not typer.confirm("Proceed?", default=False):
            ch.warning("Cancelled.")
            raise typer.Exit(0)

    # Fetch art for games not already present, before the write, so the icon field
    # can be set in a single shortcuts.vdf update.
    icons: dict[str, str] = {}
    art_set: dict[str, list[str]] = {}
    if not no_art and not dry_run:
        key = _steamgriddb_key(steamgriddb_key)
        present = shortcuts.present_exes()
        fresh = [g for g in launchable if g.launch_exe.strip().lower() not in present]
        for g in fresh:
            if not json_out:
                ch.info(f"  … art for {g.title}")
            r = grid.apply_for_game(g, steamgriddb_key=key)
            if r.get("set"):
                art_set[g.title] = r["set"]
            ic = grid.icon_path_for(g)
            if ic:
                icons[g.key] = ic

    result = shortcuts.add_games(launchable, dry_run=dry_run, force=force, icons=icons)
    result["art"] = art_set
    if json_out:
        _emit_json(result)
        return
    if not result.get("ok"):
        ch.error(result.get("error", "unify failed"))
        if "running" in result.get("error", ""):
            ch.info("Close Steam and re-run, or pass --force (Steam may overwrite on next exit).")
        raise typer.Exit(1)

    added = result.get("added", [])
    if dry_run:
        ch.info(f"Dry-run — would add {len(added)} game(s): {', '.join(added) or '(none)'}")
    elif added:
        ch.success(f"Added {len(added)} game(s) to Steam: {', '.join(added)}")
        if art_set:
            for title, roles in art_set.items():
                ch.console.print(f"[dim]  art · {title}: {', '.join(roles)}[/dim]")
        elif not no_art:
            ch.console.print("[dim]  no cover art found (add a SteamGridDB key for full grids: "
                             "navig games art --set-key <key>)[/dim]")
        ch.info("Restart Steam to see them in your library. 🎮")
    else:
        ch.info("Nothing to add — everything is already in your Steam library.")
    if result.get("skipped_present"):
        ch.console.print(f"[dim]already in Steam: {', '.join(result['skipped_present'])}[/dim]")
    if result.get("skipped_unlaunchable"):
        ch.console.print(f"[dim]no executable resolved: {', '.join(result['skipped_unlaunchable'])}[/dim]")


@games_app.command("art", rich_help_panel="Library")
def cmd_art(
    game: Annotated[str | None, typer.Argument(help="Match a game by (partial) title; omit for --all")] = None,
    all_: Annotated[bool, typer.Option("--all", help="Apply art to every non-Steam game")] = False,
    portrait: Annotated[str | None, typer.Option("--portrait", help="Local image for the vertical tile")] = None,
    hero: Annotated[str | None, typer.Option("--hero", help="Local image for the hero banner")] = None,
    logo: Annotated[str | None, typer.Option("--logo", help="Local image for the logo")] = None,
    image: Annotated[str | None, typer.Option("--image", help="Local image for the main tile (alias of --portrait)")] = None,
    set_key: Annotated[str | None, typer.Option("--set-key", help="Save a SteamGridDB API key and exit")] = None,
    steamgriddb_key: Annotated[str | None, typer.Option("--steamgriddb-key", help="SteamGridDB key for this run")] = None,
    json_out: Annotated[bool, typer.Option("--json", help="Machine-readable output")] = False,
):
    """🖼  Fetch/refresh Steam cover art for your unified (non-Steam) games."""
    from navig_games.engine import library, settings
    from navig_games.engine.steam import grid

    if set_key is not None:
        settings.set("steamgriddb_key", set_key)
        ch.success("SteamGridDB key saved. Cover art will use it from now on.")
        return

    candidates = [g for g in library.scan_non_steam() if g.launchable]
    if game:
        needle = game.lower()
        candidates = [g for g in candidates if needle in g.title.lower()]
    elif not all_:
        ch.error("Name a game, or pass --all.")
        raise typer.Exit(1)
    if not candidates:
        ch.warning("No matching non-Steam games found.")
        return

    key = _steamgriddb_key(steamgriddb_key)
    results = []
    for g in candidates:
        r = grid.apply_for_game(g, image=image, portrait=portrait, hero=hero, logo=logo,
                                steamgriddb_key=key)
        results.append(r)
        if not json_out:
            roles = ", ".join(r.get("set", [])) or "nothing found"
            ch.console.print(f"  {'✓' if r.get('set') else '·'} {g.title}: {roles}")
    if json_out:
        _emit_json(results)
        return
    ch.info("Restart Steam to see the new art. 🎨")


# ---------------------------------------------------------------------------
# steam — Steam Guard authenticator + shortcut list
# ---------------------------------------------------------------------------
steam_app = typer.Typer(name="steam", help="Steam Guard codes + non-Steam shortcuts",
                        no_args_is_help=True)
games_app.add_typer(steam_app, name="steam", rich_help_panel="Steam")


@steam_app.command("auth")
def steam_auth(
    account: Annotated[str | None, typer.Option("--account", "-a", help="Steam account name")] = None,
    secret: Annotated[str | None, typer.Option("--shared-secret", help="Base64 shared_secret")] = None,
    mafile: Annotated[str | None, typer.Option("--mafile", help="Path to a SDA .maFile")] = None,
):
    """🔐 Store a Steam Guard secret (from --shared-secret or a --mafile) in the vault."""
    from pathlib import Path

    from navig_games.engine.steam import guard

    if mafile:
        res = guard.import_mafile(Path(mafile))
        if res.get("ok"):
            ch.success(f"Imported Steam Guard for '{res['account']}'.")
        else:
            ch.error(res.get("error", "import failed"))
            raise typer.Exit(1)
        return
    if not account or not secret:
        ch.error("Provide --account and --shared-secret, or --mafile <path>.")
        raise typer.Exit(1)
    if not guard.is_valid_secret(secret):
        ch.error("That doesn't look like a valid base64 shared_secret (must decode to 20 bytes).")
        raise typer.Exit(1)
    if guard.store_secret(account, secret):
        ch.success(f"Steam Guard secret saved for '{account}'. Get a code: navig games steam code")
    else:
        ch.error("Could not save the secret (vault unavailable).")
        raise typer.Exit(1)


@steam_app.command("code")
def steam_code(
    account: Annotated[str | None, typer.Argument(help="Account name (defaults to the only one)")] = None,
):
    """🔑 Print the current Steam Guard code."""
    from navig_games.engine.steam import guard

    accounts = guard.list_accounts()
    if not account:
        if len(accounts) == 1:
            account = accounts[0]
        elif not accounts:
            ch.warning("No Steam Guard secret stored. Add one: navig games steam auth --mafile <file>")
            return
        else:
            ch.error(f"Multiple accounts — pick one: {', '.join(accounts)}")
            raise typer.Exit(1)
    code = guard.code_for(account)
    if not code:
        ch.error(f"No stored secret for '{account}'.")
        raise typer.Exit(1)
    ch.console.print(f"[bold green]{code}[/bold green]  [dim]({guard.seconds_remaining()}s left · {account})[/dim]")


@steam_app.command("accounts")
def steam_accounts():
    """List stored Steam Guard accounts (+ live codes)."""
    from navig_games.engine.steam import guard

    accts = guard.list_accounts()
    if not accts:
        ch.warning("No Steam Guard accounts. Add one: navig games steam auth --mafile <file>")
        return
    table = Table(box=None, show_header=True, padding=(0, 2))
    table.add_column("Account", style="bold")
    table.add_column("Code", no_wrap=True)
    for a in accts:
        table.add_row(a, guard.code_for(a) or "[dim]—[/dim]")
    ch.console.print(table)


@steam_app.command("shortcuts")
def steam_shortcuts():
    """List the non-Steam shortcuts NAVIG (or you) added to Steam."""
    from navig_games.engine.steam import shortcuts

    rows = shortcuts.list_shortcuts()
    if not rows:
        ch.warning("No non-Steam shortcuts found.")
        return
    table = Table(box=None, show_header=True, padding=(0, 2))
    table.add_column("Game", style="bold")
    table.add_column("Executable", style="dim", no_wrap=False)
    table.add_column("Tags", no_wrap=True)
    for r in rows:
        table.add_row(r["name"], r["exe"], ", ".join(r["tags"]))
    ch.console.print(table)
