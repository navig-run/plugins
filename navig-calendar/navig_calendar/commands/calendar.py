"""
Calendar Commands

List, view, and manage calendar events from configured providers.
"""

import asyncio
import json
from datetime import datetime, timedelta

import typer

from navig_sdk import console as ch
from navig_sdk.host import coerce_bool, command_name

# The command a user types here: `navig calendar` inside navig, `navig-calendar` on its own.
CMD = command_name("calendar")


def _calendar_config() -> dict:
    """``proactive.calendar`` from navig's config — navig's own reader inside navig, the same
    config.yaml read directly on its own (read-only)."""
    try:
        from navig.config import get_config_manager
    except ImportError:
        from navig_sdk import files
        from navig_sdk.host import config_dir

        config = files.safe_load_yaml(config_dir() / "config.yaml") or {}
    else:
        # `get_global_config()`, NOT `_load_global_config()`: the latter returns the
        # PYDANTIC-VALIDATED view, which does not declare `proactive` -- so this was ALWAYS
        # {} and the command reported the calendar as disabled no matter what the operator
        # configured. Found by core/tests/quality/test_validated_config_view_keys.py.
        config = get_config_manager().get_global_config() or {}
    node = config.get("proactive", {}) if isinstance(config, dict) else {}
    node = node.get("calendar", {}) if isinstance(node, dict) else {}
    return node if isinstance(node, dict) else {}

calendar_app = typer.Typer(help="Calendar operations")


@calendar_app.command("list")
def list_events(
    hours: int = typer.Option(24, "--hours", "-h", help="Hours to look ahead"),
    limit: int = typer.Option(10, "--limit", "-n", help="Max number of events"),
    json_output: bool = typer.Option(False, "--json", help="Output as JSON"),
    ics: str | None = typer.Option(
        None, "--ics", help="Read this ICS feed URL directly (e.g. Google's private address), ignoring config."
    ),
):
    """
    List upcoming calendar events.

    Fetches events from your configured calendar provider, or from an ICS feed given with --ics.
    """

    async def _fetch():
        from navig_calendar.ics import CalDAVProvider, ICSCalendarProvider
        from navig_sdk.pim import MockCalendar

        calendar_cfg = _calendar_config()

        if ics:
            provider = ICSCalendarProvider(url=ics)
        elif not coerce_bool(calendar_cfg.get("enabled"), default=False):
            if not json_output:
                ch.warning("Calendar not configured. Using mock data.")
            provider = MockCalendar()
        else:
            provider_type = calendar_cfg.get("provider", "mock")

            if provider_type == "google":
                # Google Calendar rides navig's OAuth connector framework: navig-only. On its
                # own, the front door turns the missing navig module into one line; Google's
                # private ICS address works standalone with --ics.
                from navig.agent.proactive import GoogleCalendar

                creds_path = calendar_cfg.get("credentials_path")
                provider = GoogleCalendar(credentials_path=creds_path)
            elif provider_type == "ics":
                url = calendar_cfg.get("url")
                provider = ICSCalendarProvider(url=url)
            elif provider_type == "caldav":
                url = calendar_cfg.get("url")
                username = calendar_cfg.get("username")
                password = calendar_cfg.get("password")
                provider = CalDAVProvider(url=url, username=username, password=password)
            else:
                provider = MockCalendar()

        # tz-aware bounds so a CalDAV/ICS date-search over a real feed gets an absolute
        # window (a naive "now" would be read as UTC by the server and skew the range).
        start = datetime.now().astimezone()
        end = start + timedelta(hours=hours)
        events = await provider.list_events(start, end)
        failure = getattr(provider, "last_error", None)
        if failure:
            # A feed that could not be read is not "no upcoming events".
            ch.error("Could not read the calendar", failure)
            raise typer.Exit(1)

        # Providers differ — Google sorts, but ICS returns file order and CalDAV server
        # order — so "upcoming"/`-n` must sort by start here to really show the SOONEST.
        # Coerce naive → local-aware so a feed mixing floating and tz-aware times sorts
        # without a TypeError.
        events.sort(key=lambda e: e.start if e.start.tzinfo is not None else e.start.astimezone())
        return events[:limit]

    events = asyncio.run(_fetch())

    if json_output:
        # Convert to JSON-serializable format
        events_data = [
            {
                "id": e.id,
                "title": e.title,
                "start": e.start.isoformat(),
                "end": e.end.isoformat() if e.end else None,
                "location": e.location,
                "description": e.description,
            }
            for e in events
        ]
        print(json.dumps(events_data, indent=2))
    else:
        if not events:
            ch.info("No upcoming events")
            return

        ch.info(f"Upcoming Events (next {hours} hours)")
        ch.console.print()

        for event in events:
            time_str = event.start.strftime("%a %b %d, %I:%M %p")
            ch.console.print(f"  [cyan]•[/cyan] {event.title}")
            ch.console.print(f"    [dim]{time_str}[/dim]")
            if event.location:
                ch.console.print(f"    [dim]📍 {event.location}[/dim]")
            ch.console.print()


@calendar_app.command("auth")
def authenticate(
    provider: str = typer.Argument("google", help="Provider to authenticate: google"),
):
    """
    Authenticate with a calendar provider.

    Opens OAuth flow for cloud providers like Google Calendar.
    """

    async def _auth():
        if provider == "google":
            from navig.agent.proactive import GoogleCalendar

            ch.info("Google Calendar Authentication")
            ch.console.print("This will open your browser to authorize NAVIG.")
            ch.console.print()

            creds_path = "~/.navig/credentials/google_calendar.json"
            cal = GoogleCalendar(credentials_path=creds_path)

            # This should trigger the OAuth flow (tz-aware bounds, like `list`).
            start = datetime.now().astimezone()
            end = start + timedelta(days=1)
            await cal.list_events(start, end)

            ch.success("✓ Authentication successful!")
            ch.info(f"Credentials saved to {creds_path}")
        else:
            ch.error(f"Unknown provider: {provider}", "Supported: google")
            raise typer.Exit(1)

    asyncio.run(_auth())


@calendar_app.command("add")
def add_event(
    title: str = typer.Argument(..., help="Event title"),
    start: str = typer.Option(None, "--start", "-s", help="Start time (ISO format, e.g. 2026-07-20T14:30)"),
    duration: int = typer.Option(60, "--duration", "-d", min=1, help="Duration in minutes"),
    location: str | None = typer.Option(None, "--location", "-l", help="Location"),
):
    """
    Add a new calendar event.

    Requires a calendar provider that supports write operations (CalDAV).
    """
    # Parse + validate the start time UP FRONT — a bad value must fail fast with a
    # clean message, not a traceback deep inside the async provider call.
    if start:
        try:
            start_dt = datetime.fromisoformat(start)
        except ValueError as exc:
            ch.error("Invalid --start time", f"{start!r} is not ISO format (e.g. 2026-07-20T14:30)")
            raise typer.Exit(2) from exc
    else:
        start_dt = datetime.now()
    # Pin a naive (floating) time to the user's LOCAL instant. Otherwise icalendar
    # serializes a naive datetime as a *floating* DTSTART (no Z/TZID) that renders at a
    # different wall-clock for every viewer's timezone — the event would drift off the
    # time the user actually meant. An explicit offset in --start is preserved as-is.
    if start_dt.tzinfo is None:
        start_dt = start_dt.astimezone()
    end_dt = start_dt + timedelta(minutes=duration)

    async def _add():
        from navig_calendar.ics import CalDAVProvider

        calendar_cfg = _calendar_config()

        provider_type = calendar_cfg.get("provider", "mock")

        if provider_type != "caldav":
            ch.error("Adding events requires CalDAV provider")
            ch.info("Configure with: navig proactive setup --calendar caldav"
                    if CMD == f"{CMD}"
                    else "Set proactive.calendar.provider: caldav (+ url, username, password) in ~/.navig/config.yaml")
            raise typer.Exit(1)

        url = calendar_cfg.get("url")
        username = calendar_cfg.get("username")
        password = calendar_cfg.get("password")

        provider = CalDAVProvider(url=url, username=username, password=password)

        from navig_sdk.pim import CalendarEvent

        event = CalendarEvent(
            id="",  # Will be generated
            title=title,
            start=start_dt,
            end=end_dt,
            location=location or "",
            description="",
        )

        # The provider API is create_event (the abstract method on CalendarProvider that
        # every provider implements) — there is no add_event, so the old call raised
        # AttributeError and `navig calendar add` never worked with a real CalDAV server.
        await provider.create_event(event)
        ch.success(f"✓ Event added: {title}")

    asyncio.run(_add())


@calendar_app.command("sync")
def sync_calendar():
    """
    Nothing to sync: events are read live from your provider on every `list`.
    """
    # This printed "Syncing calendar..." then "✓ Calendar synced" and did nothing: there
    # is no cache to refresh. A success line over work that never happened tells the user
    # to trust a state nothing produced — so it now says what is actually true.
    ch.info("Nothing to sync — navig-calendar keeps no cache; every `list` reads your calendar live.")
