"""NAVIG Social CLI — one-command OAuth connect for every publishing network.

`navig social connect <provider>` reads the app credentials you stored in the
vault, runs the provider's browser OAuth (or a paste-back flow for providers
that reject localhost redirects), and writes the resulting access token back
into the vault where the Studio publishers read it — so connecting an account
is a single command.

  navig social connect <provider>   authorize + store the access token
  navig social status [--check]     what's connected (optionally live-verified)
  navig social refresh <provider>   renew an expiring token in place
  navig social disconnect <provider>  forget the stored token
  navig social redirect-uri         the URL to register in each app's settings
"""
from __future__ import annotations

from typing import Annotated, Optional

import typer
from navig_sdk import console as ch

from navig_social._hints import CMD, config_set, vault_set

social_app = typer.Typer(
    name="social",
    help="🔗 Connect social accounts (LinkedIn · YouTube · Threads · Pinterest · Meta · dev.to)",
    no_args_is_help=True,
)

_PROVIDERS = ("facebook", "instagram", "threads", "linkedin", "youtube", "pinterest", "devto")


def _oauth():
    from navig_social.social import oauth

    return oauth


@social_app.command("connect")
def social_connect(
    provider: Annotated[str, typer.Argument(help=f"One of: {', '.join(_PROVIDERS)}")],
    manual: Annotated[bool, typer.Option("--manual", help="Paste the redirect URL instead of using the localhost callback")] = False,
    timeout: Annotated[float, typer.Option("--timeout", help="Seconds to wait for browser authorization")] = 240.0,
) -> None:
    """Authorize an account and store its access token in the vault."""
    oauth = _oauth()
    name = provider.lower().strip()
    if name not in oauth.CONNECTORS:
        ch.error(f"Unknown provider '{provider}'.", f"Choose one of: {', '.join(_PROVIDERS)}")
        raise typer.Exit(1)
    use_manual = manual or name in oauth.MANUAL_ONLY
    ch.info(f"Connecting {name} …" + ("  (paste-back mode)" if use_manual else "  (a browser window will open)"))
    try:
        result = oauth.connect_devto() if name == "devto" else oauth.CONNECTORS[name](
            manual=use_manual, timeout=timeout)
    except oauth.SocialOAuthError as exc:
        ch.error(f"Could not connect {name}.", str(exc))
        raise typer.Exit(1) from None
    ch.success(f"{name} connected → {result.account}")
    ch.console.print(f"  [dim]token lifetime[/dim]  {result.lifetime}")
    for note in result.notes:
        ch.console.print(f"  [yellow]note[/yellow]  {note}")
    ch.console.print(f"  [dim]stored in vault as[/dim]  {name}")


@social_app.command("status")
def social_status(
    check: Annotated[bool, typer.Option("--check", help="Live-verify each stored token against its API")] = False,
) -> None:
    """Show which networks are connected (and, with --check, whether tokens work)."""
    from rich.table import Table

    from navig_social.social.labels import display_label

    oauth = _oauth()
    if check:
        ch.info("Verifying stored tokens against each API …")
    rows = oauth.provider_status(check=check)

    table = Table(box=None, show_header=True, padding=(0, 2))
    table.add_column("Network", style="bold", no_wrap=True)
    table.add_column("Token", no_wrap=True)
    table.add_column("App creds", no_wrap=True)
    if check:
        table.add_column("Live")  # account name / error — the one column allowed to wrap
    table.add_column("Next step", no_wrap=True)

    for r in rows:
        token = bool(r["token"])
        tok_cell = "[green]● connected[/green]" if token else "[dim]○ —[/dim]"

        creds = r["app_creds"]
        if creds == "ok":
            creds_cell = "[green]ok[/green]"
        elif creds == "—":
            creds_cell = "[dim]n/a[/dim]"
        else:  # "missing: <provider>/<field>[, …]" → show just the field name(s)
            fields = [p.split("/")[-1] for p in creds.replace("missing:", "").split(",") if p.strip()]
            creds_cell = f"[yellow]needs {', '.join(fields)}[/yellow]"

        # concise per-row guidance (the footer carries the full command form)
        if "missing" in creds:
            nxt = "[yellow]add vault key[/yellow]"
        elif not token:
            nxt = "[cyan]→ connect[/cyan]"
        elif r.get("live", "").startswith("FAIL"):
            nxt = "[red]reconnect[/red]"
        else:
            nxt = "[green]ready[/green]"

        cells = [display_label(r["provider"]), tok_cell, creds_cell]
        if check:
            live = r.get("live", "")
            if not live:
                live_cell = "[dim]—[/dim]"
            elif live.startswith("FAIL"):
                live_cell = f"[red]✗ {(live[6:].strip() or 'failed')[:24]}[/red]"
            else:
                live_cell = f"[green]✓ {live}[/green]"
            cells.append(live_cell)
        cells.append(nxt)
        table.add_row(*cells)

    ch.console.print(table)
    connected = sum(1 for r in rows if r["token"])
    ch.console.print(f"\n  [dim]{connected}/{len(rows)} connected · "
                     f"connect one with[/dim] [cyan]{CMD} connect <network>[/cyan]")


@social_app.command("refresh")
def social_refresh(
    provider: Annotated[str, typer.Argument(help="Provider whose token to renew")],
) -> None:
    """Renew an expiring access token in place (uses stored refresh/app creds)."""
    oauth = _oauth()
    name = provider.lower().strip()
    try:
        summary = oauth.refresh_provider(name)
    except oauth.SocialOAuthError as exc:
        ch.error(f"Could not refresh {name}.", str(exc))
        raise typer.Exit(1) from None
    ch.success(f"{name}: {summary}")


@social_app.command("disconnect")
def social_disconnect(
    provider: Annotated[str, typer.Argument(help="Provider to forget")],
    yes: Annotated[bool, typer.Option("--yes", help="Skip confirmation")] = False,
) -> None:
    """Remove a provider's stored access token (app credentials are kept)."""
    oauth = _oauth()
    name = provider.lower().strip()
    if not yes and not typer.confirm(f"Forget the stored {name} access token?"):
        ch.warning("Aborted.")
        raise typer.Exit(0)
    removed = oauth.delete_secret(name)
    oauth.delete_secret(f"{name}/refresh_token")
    if removed:
        ch.success(f"{name} disconnected (access token removed; app credentials kept).")
    else:
        ch.info(f"No stored {name} access token found.")


@social_app.command("redirect-uri")
def social_redirect_uri() -> None:
    """Print the redirect URLs to register in each provider's app settings."""
    oauth = _oauth()
    ch.info("Register these redirect URIs in your apps (once per provider):")
    ch.console.print("  [bold]localhost callback[/bold] (LinkedIn, Google/YouTube, Pinterest, Meta dev mode)")
    ch.console.print(f"    [cyan]{oauth.REDIRECT_URI}[/cyan]")
    ch.console.print("  [bold]https redirect[/bold] (Threads — page content irrelevant, you paste it back)")
    ch.console.print(f"    [cyan]{oauth._threads_redirect()}[/cyan]")


@social_app.command("fan-out")
def social_fanout(
    file: Annotated[str, typer.Option("--file", "-f", help="Brief JSON: {title, body, url, image?}")],
    to: Annotated[str, typer.Option("--to", help="Comma-separated platforms")] = "x,facebook,devto,telegram",
    campaign: Annotated[str, typer.Option("--campaign", help="Campaign slug for UTM (default: brief title)")] = "",
    track: Annotated[Optional[bool], typer.Option("--track/--no-track", help="Wrap links in click-tracking redirects (default: on when adapters.social.tracking.base_url is set).")] = None,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Preview per-platform payloads without publishing")] = False,
    as_json: Annotated[bool, typer.Option("--json", help="Machine-readable output")] = False,
) -> None:
    """Publish ONE brief to many networks (X · Facebook · Dev.to · Telegram) with UTM tags."""
    import asyncio
    import json
    from pathlib import Path

    from navig_social.social.fanout import Brief, _tracking_base, fan_out

    try:
        brief = Brief.from_dict(json.loads(Path(file).read_text(encoding="utf-8")))
    except (OSError, ValueError, TypeError) as exc:  # unreadable / bad JSON / invalid brief
        ch.error("Could not read the brief", str(exc)[:200])
        raise typer.Exit(1) from exc

    platforms = [p.strip() for p in to.split(",") if p.strip()]
    if not platforms:
        ch.error("--to had no valid platforms", "e.g. --to x,facebook,devto,telegram")
        raise typer.Exit(1)
    if track and not _tracking_base():
        ch.warning("--track requested but no tracking base URL is set",
                   f"Set one:  {config_set('adapters.social.tracking.base_url', 'https://<your-lighthouse-url>')}")
    results = asyncio.run(fan_out(brief, platforms=platforms, campaign=campaign or None, dry_run=dry_run, track=track))

    if as_json:
        ch.console.print_json(json.dumps([r.to_dict() for r in results]))
        return

    from rich.table import Table

    if dry_run:
        table = Table(box=None, show_header=True, padding=(0, 2))
        table.add_column("platform", no_wrap=True)
        table.add_column("chars", no_wrap=True, justify="right")
        table.add_column("text (adapted · UTM'd link)")
        for r in results:
            table.add_row(r.platform, str(r.chars), (r.text or "").replace("\n", " ⏎ "))
        ch.console.print(table)
        ch.dim(f"dry-run · {len(results)} platform(s) · nothing published")
        return

    table = Table(box=None, show_header=True, padding=(0, 2))
    table.add_column("network", no_wrap=True)
    table.add_column("result", no_wrap=True)
    table.add_column("id / error")
    ok = 0
    for r in results:
        if r.ok:
            ok += 1
            table.add_row(r.network, "[green]● ok[/green]", r.url or r.id or "")
        else:
            table.add_row(r.network, "[red]✗ fail[/red]", (r.error or "")[:80])
    ch.console.print(table)
    ch.info(f"{ok}/{len(results)} published")


@social_app.command("receipts")
def social_receipts(
    campaign: Annotated[Optional[str], typer.Option("--campaign", "-c", help="Show one campaign's receipts (default: a summary of all campaigns).")] = None,
    with_engagement: Annotated[bool, typer.Option("--with-engagement", "-e", help="Join recorded engagement (clicks / views / …) per campaign.")] = False,
    limit: Annotated[int, typer.Option("--limit", help="Max rows to show.")] = 50,
    as_json: Annotated[bool, typer.Option("--json", help="Emit the raw records as JSON.")] = False,
) -> None:
    """What we've published — a campaign-tagged ledger of every live fan-out.

    Each row carries the UTM'd URL (the join key for engagement), so this is the
    durable record behind the create → publish → measure loop. It's written
    automatically on every live `navig social fan-out` / `navig pipeline` publish.
    Add `--with-engagement` to join the metrics recorded via `navig social engagement`
    (or the POST /api/deck/social/engagement ingest).
    """
    import json

    from rich.table import Table

    from navig_social.social.labels import display_label
    from navig_social.social.receipts import get_publish_receipts

    store = get_publish_receipts()
    eng = _engagement_store() if with_engagement else None

    if campaign:
        rows = store.list(campaign=campaign, limit=limit)
        metrics = eng.campaign_metrics(campaign) if eng else {}
        if as_json:
            ch.raw_print(json.dumps({"receipts": rows, "engagement": metrics}, indent=2, ensure_ascii=False))
            return
        if not rows:
            ch.info(f"No receipts for campaign '{campaign}'",
                    f"Publish with:  {CMD} fan-out --campaign <slug> …")
            return
        table = Table(box=None, show_header=True, padding=(0, 2))
        table.add_column("when", no_wrap=True)
        table.add_column("network", no_wrap=True)
        table.add_column("result", no_wrap=True)
        table.add_column("id / link")  # free-text column
        ok = 0
        for r in rows:
            when = (r["created_at"] or "")[:19].replace("T", " ")
            if r["ok"]:
                ok += 1
                table.add_row(when, display_label(r["network"]), "[green]● ok[/green]", r["url"] or r["post_id"] or "")
            else:
                table.add_row(when, display_label(r["network"]), "[red]✗ fail[/red]", (r["error"] or "")[:80])
        ch.console.print(table)
        ch.info(f"campaign '{campaign}' · {ok}/{len(rows)} ok")
        if with_engagement:
            ch.dim(f"engagement · {_fmt_eng(metrics)}")
        return

    camps = store.campaigns(limit=limit)
    rollup = eng.rollup() if eng else {}
    if as_json:
        if with_engagement:
            for c in camps:
                c["engagement"] = rollup.get(c["campaign"] or "", {})
        ch.raw_print(json.dumps(camps, indent=2, ensure_ascii=False))
        return
    if not camps:
        ch.info("No publishes recorded yet",
                f"Fan out something:  {CMD} fan-out --file brief.json --to x,telegram --campaign launch")
        return
    table = Table(box=None, show_header=True, padding=(0, 2))
    table.add_column("campaign", no_wrap=True)
    table.add_column("posts", no_wrap=True, justify="right")
    table.add_column("ok", no_wrap=True, justify="right")
    if with_engagement:
        table.add_column("engagement", no_wrap=True)
    table.add_column("last")  # free-text column
    for c in camps:
        last = (c["last"] or "")[:19].replace("T", " ")
        row = [c["campaign"] or "—", str(c["count"]), str(c["ok"])]
        if with_engagement:
            row.append(_fmt_eng(rollup.get(c["campaign"] or "", {})))
        row.append(last)
        table.add_row(*row)
    ch.console.print(table)
    hint = f"{CMD} receipts --campaign <slug> for detail"
    if not with_engagement:
        hint += " · add --with-engagement to join metrics"
    ch.dim(f"{len(camps)} campaign(s) · {hint}")


def _engagement_store():
    """The engagement ledger (imported lazily — keeps `navig social` fast)."""
    from navig_social.social.engagement import get_engagement

    return get_engagement()


def _fmt_eng(metrics: dict) -> str:
    """Compact per-campaign metrics, e.g. ``clicks:42 · views:120`` (or ``—``)."""
    return " · ".join(f"{k}:{v}" for k, v in metrics.items()) if metrics else "—"


@social_app.command("engagement")
def social_engagement(
    campaign: Annotated[str, typer.Argument(help="Campaign slug (matches `navig social receipts`).")],
    metric: Annotated[str, typer.Option("--metric", "-m", help="clicks | views | likes | replies | shares | reach (or a custom name).")] = "clicks",
    value: Annotated[int, typer.Option("--value", "-v", help="Count to add (additive; send deltas).")] = 1,
    network: Annotated[Optional[str], typer.Option("--network", "-n", help="Which network (optional).")] = None,
    post_id: Annotated[Optional[str], typer.Option("--post-id", help="Which post (optional).")] = None,
    source: Annotated[str, typer.Option("--source", help="Where the number came from.")] = "manual",
) -> None:
    """Record engagement for a campaign — the *measure* side of the loop.

    Also ingestable over HTTP (POST /api/deck/social/engagement) for beacons /
    webhooks / platform polls. View it joined to what you published with
    `navig social receipts --with-engagement`.
    """
    from navig_social.social.engagement import KNOWN_METRICS, get_engagement

    m = metric.lower().strip()
    if not m:
        ch.error("Metric required", "e.g. --metric clicks")
        raise typer.Exit(2)
    if m not in KNOWN_METRICS:
        ch.warning(f"'{m}' isn't a standard metric",
                   f"Standard: {', '.join(KNOWN_METRICS)} — recording it anyway.")
    get_engagement().record(
        campaign=campaign, metric=m, value=value,
        network=network, post_id=post_id, source=source,
    )
    ch.success(f"+{value} {m} → campaign '{campaign}'",
               f"Joined view:  {CMD} receipts --with-engagement")


def _sync_status_cell(status: str) -> str:
    """Colourise a per-post sync status for the table (house-style glyphs)."""
    if status == "synced":
        return "[green]● synced[/green]"
    if status.startswith("error"):
        return f"[red]✗ {status}[/red]"
    if status == "not connected":
        return "[yellow]○ not connected[/yellow]"
    return f"[dim]○ {status}[/dim]"


@social_app.command("sync")
def social_sync(
    campaign: Annotated[Optional[str], typer.Option("--campaign", "-c", help="Sync one campaign (default: every campaign with receipts).")] = None,
    limit: Annotated[int, typer.Option("--limit", help="Max receipts to scan.")] = 500,
    as_json: Annotated[bool, typer.Option("--json", help="Emit the sync result as JSON.")] = False,
) -> None:
    """Pull live view/like counts from each network → the report's CTR goes real.

    Walks the publish receipts (posts we actually landed), asks each network's
    public-metrics API for the current **absolute** counts (twitter + dev.to today),
    and upserts them into the engagement ledger under source ``platform``. Re-running
    just refreshes the latest numbers — it never double-counts. Networks with no read
    API (telegram, facebook …) are skipped, not failed.

    The automated sibling of `navig social engagement`. See it land:
      navig social report --campaign <slug>
    """
    import asyncio
    import json

    from rich.table import Table

    from navig_social.social.base import BasePublisher
    from navig_social.social.engagement import get_engagement
    from navig_social.social.labels import display_label
    from navig_social.social.receipts import get_publish_receipts
    from navig_social.social.registry import get_publisher_registry

    receipts = get_publish_receipts().list(campaign=campaign, limit=limit)
    # Only successful posts that carry a platform post_id are syncable. Dedup on
    # (campaign, network, post_id) — the receipt list is newest-first, so the first
    # occurrence is the freshest and any re-publish of the same post collapses to one.
    seen: set[tuple[str, str, str]] = set()
    posts: list[dict] = []
    for r in receipts:
        if not (r["ok"] and r.get("post_id")):
            continue
        key = (r["campaign"] or "", r["network"], r["post_id"])
        if key in seen:
            continue
        seen.add(key)
        posts.append(r)

    if not posts:
        if as_json:
            ch.raw_print(json.dumps({"synced": 0, "results": []}, indent=2))
            return
        ch.info("Nothing to sync",
                f"Publish something first:  {CMD} fan-out --file brief.json --to x,devto --campaign launch")
        return

    registry = get_publisher_registry()
    eng = get_engagement()

    async def _fetch(row: dict) -> tuple[dict, str, dict | None]:
        pub = registry.get(row["network"])
        if pub is None:
            return row, "no publisher", None
        if type(pub).fetch_metrics is BasePublisher.fetch_metrics:
            return row, "no read API", None  # network exposes no public-metrics endpoint
        if not pub.is_configured():
            return row, "not connected", None
        try:
            metrics = await pub.fetch_metrics(row["post_id"])
        except Exception as exc:  # noqa: BLE001 - one post's failure must not abort the sweep
            return row, f"error: {str(exc)[:40]}", None
        if not metrics:
            return row, "no data", None
        return row, "synced", metrics

    # Concurrent fetch (I/O-bound), then sequential upserts on this thread (the store
    # serializes writes; keeping writes off the gather avoids interleaved DELETE/INSERT).
    async def _fetch_all() -> list[tuple[dict, str, dict | None]]:
        return await asyncio.gather(*[_fetch(r) for r in posts])

    fetched = asyncio.run(_fetch_all())

    results: list[dict] = []
    synced = 0
    for row, status, metrics in fetched:
        if status == "synced" and metrics:
            synced += 1
            for metric, value in metrics.items():
                eng.upsert_metric(
                    campaign=row["campaign"] or "", network=row["network"],
                    post_id=row["post_id"], metric=metric, value=int(value), source="platform",
                )
        results.append({
            "campaign": row["campaign"] or "", "network": row["network"],
            "post_id": row["post_id"], "status": status, "metrics": metrics,
        })

    if as_json:
        ch.raw_print(json.dumps({"synced": synced, "results": results}, indent=2, ensure_ascii=False))
        return

    table = Table(box=None, show_header=True, padding=(0, 2))
    table.add_column("network", no_wrap=True)
    table.add_column("post", no_wrap=True)
    table.add_column("status", no_wrap=True)
    table.add_column("metrics")  # free-text column
    for res in results:
        metrics = res["metrics"]
        cells = _fmt_eng(metrics) if metrics else "—"
        table.add_row(display_label(res["network"]), (res["post_id"] or "")[:24], _sync_status_cell(res["status"]), cells)
    ch.console.print(table)
    scope = f"campaign '{campaign}'" if campaign else f"{len(results)} post(s)"
    hint = f"{CMD} report" + (f" --campaign {campaign}" if campaign else "") + " to see the CTR"
    ch.info(f"{scope} · {synced}/{len(results)} synced · {hint}")


def _fmt_ctr(ctr) -> str:
    """A CTR ratio as a percentage, or ``—`` when there are no views to divide by."""
    return f"{ctr * 100:.1f}%" if ctr is not None else "—"


@social_app.command("report")
def social_report(
    campaign: Annotated[Optional[str], typer.Option("--campaign", "-c", help="Scorecard for one campaign (default: a leaderboard of all).")] = None,
    limit: Annotated[int, typer.Option("--limit", help="Max campaigns in the leaderboard.")] = 50,
    as_json: Annotated[bool, typer.Option("--json", help="Emit the raw scorecard as JSON.")] = False,
) -> None:
    """Campaign scorecard — how did it do? Joins what you published (receipts) with
    the clicks it earned (engagement), per network, with a CTR where views exist.

    The capstone of the create → publish → measure loop. `--campaign <slug>` gives a
    per-network breakdown; with no campaign, a leaderboard ranked by clicks.
    """
    import json

    from rich.table import Table

    from navig_social.social.labels import display_label
    from navig_social.social.report import campaign_scorecard, leaderboard

    if campaign:
        card = campaign_scorecard(campaign)
        if as_json:
            ch.raw_print(json.dumps(card, indent=2, ensure_ascii=False))
            return
        if not card["networks"]:
            ch.info(f"Nothing recorded for campaign '{campaign}'",
                    f"Publish with:  {CMD} fan-out --campaign <slug> …")
            return
        table = Table(box=None, show_header=True, padding=(0, 2))
        table.add_column("network", no_wrap=True)
        table.add_column("published", no_wrap=True)
        table.add_column("clicks", no_wrap=True, justify="right")
        table.add_column("views", no_wrap=True, justify="right")
        table.add_column("CTR", no_wrap=True, justify="right")
        table.add_column("link")  # free-text column
        for e in card["networks"]:
            if e["ok"]:
                status = "[green]● ok[/green]"
            elif e["posts"]:
                status = "[red]✗ fail[/red]"
            else:
                status = "[dim]○ clicks only[/dim]"
            table.add_row(display_label(e["network"]) or "(unattributed)", status, str(e["clicks"]),
                          str(e["views"] or "—"), _fmt_ctr(e["ctr"]), e["link"] or "—")
        ch.console.print(table)
        t = card["totals"]
        summary = f"campaign '{campaign}' · {t['ok']}/{t['posts']} posts ok · {t['clicks']} clicks"
        if t["ctr"] is not None:
            summary += f" · CTR {_fmt_ctr(t['ctr'])}"
        ch.info(summary)
        return

    board = leaderboard(limit=limit)
    if as_json:
        ch.raw_print(json.dumps(board, indent=2, ensure_ascii=False))
        return
    if not board:
        ch.info("No campaigns recorded yet",
                f"Fan out something:  {CMD} fan-out --file brief.json --to x,telegram --campaign launch")
        return
    table = Table(box=None, show_header=True, padding=(0, 2))
    table.add_column("campaign", no_wrap=True)
    table.add_column("posts", no_wrap=True, justify="right")
    table.add_column("clicks", no_wrap=True, justify="right")
    table.add_column("CTR", no_wrap=True, justify="right")
    table.add_column("last")  # free-text column
    for e in board:
        table.add_row(e["campaign"] or "—", str(e["posts"]), str(e["clicks"]),
                      _fmt_ctr(e["ctr"]), (e["last"] or "")[:19].replace("T", " "))
    ch.console.print(table)
    ch.dim(f"{len(board)} campaign(s) ranked by clicks · {CMD} report --campaign <slug> for detail")


def _fmt_count(n) -> str:
    """1234567 → '1,234,567'; None → '—'."""
    return f"{n:,}" if isinstance(n, int) else "—"


@social_app.command("stats")
def social_stats(
    platform: Annotated[
        str,
        typer.Argument(help="Platform (youtube, github, telegram, …) or 'list' to see what's supported"),
    ],
    handles: Annotated[
        Optional[list[str]],
        typer.Argument(help="One or more handles/usernames, e.g. @mkbhd  (omit for --json errors)"),
    ] = None,
    only_public: Annotated[
        bool, typer.Option("--only-public", help="Public/API paths only; never launch a browser (CDP)")
    ] = False,
    no_login: Annotated[
        bool, typer.Option("--no-login", help="Allow CDP but don't attempt a vaulted login")
    ] = False,
    as_json: Annotated[bool, typer.Option("--json", help="Machine-readable output")] = False,
) -> None:
    """Public follower / subscriber / view counts for ANY handle — no space, no linked account.

    Reads only PUBLIC numbers (plain HTTPS, official APIs where a key is present, or a vaulted
    browser via `navig cdp` for login-walled sites). Nothing is published; no account needs to
    be connected.

    Examples:
      navig social stats github torvalds
      navig social stats youtube @mkbhd @mrbeast
      navig social stats list                     # which platforms, and how each is fetched
    """
    import json

    from rich.table import Table

    from navig_social.stats import (
        API_FETCHERS,
        CDP_PLATFORMS,
        PUBLIC_FETCHERS,
        crawl_handles,
        platform_capability,
    )

    # `navig social stats list` — the capability matrix.
    if platform.lower() in ("list", "platforms", "?"):
        known = sorted(set(PUBLIC_FETCHERS) | set(API_FETCHERS) | set(CDP_PLATFORMS))
        _cap_label = {
            "public": "[green]public[/green] · no auth",
            "api": "[cyan]official API[/cyan] · key/token (CDP fallback)",
            "cdp-public": "[yellow]browser[/yellow] · public, no login",
            "cdp-login": "[yellow]browser[/yellow] · vaulted login",
        }
        if as_json:
            ch.raw_print(json.dumps({p: platform_capability(p) for p in known}, indent=2))
            return
        table = Table(box=None, show_header=True, padding=(0, 2))
        table.add_column("platform", style="bold", no_wrap=True)
        table.add_column("how it's fetched")  # free-text, wrappable
        for p in known:
            table.add_row(p, _cap_label.get(platform_capability(p), platform_capability(p)))
        ch.console.print(table)
        ch.dim(f"usage:  {CMD} stats <platform> <handle> …   ·   --only-public to skip the browser")
        return

    if not handles:
        ch.error("No handle given", f"e.g.  {CMD} stats youtube @mkbhd")
        raise typer.Exit(1)

    pairs = [(platform, h) for h in handles]
    results = crawl_handles(pairs, allow_cdp=not only_public, do_login=not no_login)

    if as_json:
        ch.raw_print(json.dumps([r.to_dict() for r in results], indent=2, ensure_ascii=False))
        return

    table = Table(box=None, show_header=True, padding=(0, 2))
    table.add_column("platform", style="bold", no_wrap=True)
    table.add_column("handle", no_wrap=True)
    table.add_column("followers", no_wrap=True, justify="right")
    table.add_column("status", no_wrap=True)
    table.add_column("detail")  # the one wrappable column (extra stats / error)
    ok = 0
    for r in results:
        if r.status == "ok":
            ok += 1
            status_cell = "[green]● ok[/green]"
            detail = " · ".join(f"{k}={_fmt_count(v) if isinstance(v, int) else v}" for k, v in r.extra.items())
        elif r.status in ("needs-login", "needs-cdp", "needs-token"):
            status_cell = f"[yellow]{r.status}[/yellow]"
            detail = r.error or ""
        elif r.status in ("no-handle", "unsupported"):
            status_cell = f"[dim]{r.status}[/dim]"
            detail = r.error or ""
        else:
            status_cell = "[red]✗ error[/red]"
            detail = r.error or ""
        table.add_row(r.platform or "—", r.handle or "—", _fmt_count(r.followers), status_cell, detail)
    ch.console.print(table)
    ch.dim(f"{ok}/{len(results)} fetched · source is PUBLIC data only · --json for scripts")


presence_app = typer.Typer(
    name="presence",
    help="📡 Track your OWN accounts over time — roster crawl → dated snapshot → deltas",
    no_args_is_help=True,
)
social_app.add_typer(presence_app, name="presence")


def _presence_space(space: str):
    """Resolve a space + its roster, or exit with the reason."""
    from navig_social import roster

    try:
        space_dir = roster.resolve_space_dir(space)
    except ValueError as exc:
        ch.error("Space not found", str(exc))
        raise typer.Exit(1) from exc
    return space_dir


@presence_app.command("crawl")
def presence_crawl(
    space: Annotated[str, typer.Option("--space", "-s", help="Space holding the account registry")],
    brand: Annotated[Optional[str], typer.Option("--brand", "-b", help="Only this brand")] = None,
    platform: Annotated[Optional[str], typer.Option("--platform", "-p", help="Only this platform")] = None,
    only_public: Annotated[
        bool, typer.Option("--only-public", help="Public/API paths only; never launch a browser (CDP)")
    ] = False,
    no_login: Annotated[
        bool, typer.Option("--no-login", help="Allow CDP but don't attempt a vaulted login")
    ] = False,
    write_registry: Annotated[
        bool, typer.Option("--write-registry/--no-write-registry",
                           help="Also refresh public_followers in the registry (ok results only)")
    ] = True,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Crawl and show, but write nothing")] = False,
    as_json: Annotated[bool, typer.Option("--json", help="Machine-readable output")] = False,
) -> None:
    """Crawl every account in a space's registry and record what moved.

    Reads `<space>/.navig/memory/account-registry.json`, fetches each account's public
    count, writes `<space>/analytics/raw/_snapshots/presence-YYYY-MM-DD.json`, and diffs
    it against the previous snapshot — so nobody decides anything on numbers of unknown
    age. Public metadata only; no secrets are read or written.

    Examples:
      navig social presence crawl --space human-presence --only-public
      navig social presence crawl --space human-presence --brand somaleto --dry-run
    """
    import json as _json
    from datetime import date

    from rich.table import Table

    from navig_social import roster

    space_dir = _presence_space(space)
    try:
        accounts = roster.load_roster(space_dir, brand=brand, platform=platform)
    except (FileNotFoundError, ValueError) as exc:
        ch.error("No roster to crawl", str(exc))
        raise typer.Exit(1) from exc
    if not accounts:
        ch.error("No accounts matched", "check --brand / --platform against the registry")
        raise typer.Exit(1)

    paired = roster.crawl_roster(accounts, allow_cdp=not only_public, do_login=not no_login)
    today = date.today()
    payload = roster.snapshot_payload(paired, generated=today)
    prev_path = roster.previous_snapshot(space_dir, excluding=roster.snapshot_path(space_dir, today))
    previous = roster.read_snapshot(prev_path) if prev_path else None
    rows = roster.deltas(previous, payload)

    written = None
    changed = 0
    if not dry_run:
        written = roster.write_snapshot(space_dir, payload, on=today)
        if write_registry:
            changed = roster.update_registry(space_dir, paired, on=today)

    if as_json:
        ch.raw_print(_json.dumps({
            "space": str(space_dir),
            "snapshot": str(written) if written else None,
            "compared_to": str(prev_path) if prev_path else None,
            "registry_rows_updated": changed,
            "counts": payload["counts"],
            "deltas": rows,
        }, indent=2, ensure_ascii=False))
        return

    table = Table(box=None, show_header=True, padding=(0, 2))
    table.add_column("brand", style="bold", no_wrap=True)
    table.add_column("platform", no_wrap=True)
    table.add_column("handle", no_wrap=True)
    table.add_column("followers", no_wrap=True, justify="right")
    table.add_column("change", no_wrap=True, justify="right")
    table.add_column("status", no_wrap=True)
    table.add_column("detail")
    for r in rows:
        d = r["delta"]
        if d is None:
            change = "[dim]new[/dim]" if r["followers"] is not None else "—"
        elif d > 0:
            change = f"[green]+{d}[/green]"
        elif d < 0:
            change = f"[red]{d}[/red]"
        else:
            change = "[dim]0[/dim]"
        status = r["status"] or ""
        if status == "ok":
            status_cell = "[green]● ok[/green]"
        elif status.startswith("needs"):
            status_cell = f"[yellow]{status}[/yellow]"
        elif status in ("no-handle", "unsupported"):
            status_cell = f"[dim]{status}[/dim]"
        else:
            status_cell = "[red]✗ error[/red]"
        table.add_row(r["brand"] or "—", r["platform"] or "—", r["handle"] or "—",
                      _fmt_count(r["followers"]), change, status_cell, r["error"] or "")
    ch.console.print(table)

    c = payload["counts"]
    ch.dim(f"{c['ok']}/{len(rows)} fetched · {c['needs_login']} walled · "
           f"{c['failed']} failed · {c['skipped']} skipped")
    if prev_path:
        ch.dim(f"compared against {prev_path.name}")
    else:
        ch.dim("no earlier snapshot — this is the baseline")
    if dry_run:
        ch.dim("--dry-run: nothing written")
    else:
        ch.dim(f"snapshot → {written}" + (f" · registry rows updated: {changed}" if write_registry else ""))


@presence_app.command("trend")
def presence_trend(
    space: Annotated[str, typer.Option("--space", "-s", help="Space holding the snapshots")],
    brand: Annotated[Optional[str], typer.Option("--brand", "-b", help="Only this brand")] = None,
    platform: Annotated[Optional[str], typer.Option("--platform", "-p", help="Only this platform")] = None,
    as_json: Annotated[bool, typer.Option("--json", help="Machine-readable output")] = None,
) -> None:
    """First → latest movement for every tracked account, across all snapshots.

    Answers "is this growing or dying" without re-crawling anything.
    """
    import json as _json

    from rich.table import Table

    from navig_social import roster

    space_dir = _presence_space(space)
    rows = roster.series(space_dir, brand=brand, platform=platform)
    if not rows:
        ch.error("No snapshots yet", f"run:  {CMD} presence crawl --space {space}")
        raise typer.Exit(1)

    hist: dict = {}
    for r in rows:
        hist.setdefault((r["brand"], r["platform"], r["handle"]), []).append((r["date"], r["followers"]))

    summary = []
    for (b, p, h), points in hist.items():
        points.sort()
        first_date, first = points[0]
        last_date, last = points[-1]
        summary.append({
            "brand": b, "platform": p, "handle": h,
            "first": first, "first_date": first_date,
            "latest": last, "latest_date": last_date,
            "delta": last - first, "points": len(points),
        })
    summary.sort(key=lambda s: (-abs(s["delta"]), s["brand"] or "", s["platform"] or ""))

    if as_json:
        ch.raw_print(_json.dumps(summary, indent=2, ensure_ascii=False))
        return

    table = Table(box=None, show_header=True, padding=(0, 2))
    table.add_column("brand", style="bold", no_wrap=True)
    table.add_column("platform", no_wrap=True)
    table.add_column("handle", no_wrap=True)
    table.add_column("first", no_wrap=True, justify="right")
    table.add_column("latest", no_wrap=True, justify="right")
    table.add_column("change", no_wrap=True, justify="right")
    table.add_column("span", no_wrap=True)
    for s in summary:
        d = s["delta"]
        change = f"[green]+{d}[/green]" if d > 0 else (f"[red]{d}[/red]" if d < 0 else "[dim]0[/dim]")
        span = (f"{s['first_date']}→{s['latest_date']} ({s['points']})"
                if s["points"] > 1 else f"[dim]{s['latest_date']} only[/dim]")
        table.add_row(s["brand"] or "—", s["platform"] or "—", s["handle"] or "—",
                      _fmt_count(s["first"]), _fmt_count(s["latest"]), change, span)
    ch.console.print(table)
    ch.dim(f"{len(summary)} accounts across {len(roster.list_snapshots(space_dir))} snapshots")



@social_app.command("upload")
def social_upload(
    file: str = typer.Argument(..., help="Video file to upload."),
    title: str = typer.Option(None, "--title", "-t", help="Video title (default: the filename)."),
    description: str = typer.Option("", "--description", "-d", help="Description."),
    tags: str = typer.Option("", "--tags", help="Comma-separated tags."),
    privacy: str = typer.Option("private", "--privacy",
                                help="private | unlisted | public. Stays private unless you say otherwise."),
    to: str = typer.Option("youtube", "--to", help="Destination (youtube)."),
    no_shorts: bool = typer.Option(False, "--no-shorts", help="Do not add the #Shorts tag."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Show what would be sent; upload nothing."),
    json_out: bool = typer.Option(False, "--json", help="Machine-readable output."),
) -> None:
    """Upload a video file to YouTube (fan-out publishes text; this publishes a file)."""
    import json as _json
    from pathlib import Path as _P

    from ..social import upload as up

    if to != "youtube":
        ch.error(f"only youtube is supported so far, not {to!r}")
        raise typer.Exit(2)
    p = _P(file)
    if not p.is_file():
        ch.error(f"no such file: {p}")
        raise typer.Exit(2)
    name = title or p.stem.replace("-", " ").replace("_", " ").strip()
    taglist = [t.strip() for t in tags.split(",") if t.strip()]
    size = p.stat().st_size

    if dry_run:
        payload = {"file": str(p), "bytes": size, "title": name,
                   "description": description, "tags": taglist,
                   "privacy": privacy, "shorts": not no_shorts}
        if json_out:
            ch.console.print_json(_json.dumps(payload))
        else:
            ch.info("dry run — nothing uploaded")
            for k, v in payload.items():
                ch.console.print(f"  {k:12s} {v}")
        return

    def prog(sent: int, total: int) -> None:
        ch.dim(f"  {sent / 1048576:6.1f} / {total / 1048576:.1f} MB "
               f"({sent * 100 // total}%)")

    try:
        res = up.upload(p, title=name, description=description, tags=taglist,
                        privacy=privacy, shorts=not no_shorts, on_progress=prog)
    except up.UploadError as e:
        ch.error(str(e))
        raise typer.Exit(1) from e

    if json_out:
        ch.console.print_json(_json.dumps({
            "video_id": res.video_id, "url": res.url, "title": res.title,
            "privacy": res.privacy, "bytes": res.bytes_sent}))
    else:
        ch.ok(f"uploaded {res.title}")
        ch.console.print(f"  {res.url}   ({res.privacy})")
        if res.privacy == "private":
            ch.dim("  private — flip it with --privacy public, or in YouTube Studio")


relay_app = typer.Typer(
    name="relay",
    help="⏱ Schedule Telegram posts through navig-relay (bots can't schedule — the relay does)",
    no_args_is_help=True,
)
social_app.add_typer(relay_app, name="relay")

_RelayUrl = Annotated[Optional[str], typer.Option("--url", help="Relay base URL (else $NAVIG_RELAY_URL, else vault 'relay')")]
_Json = Annotated[bool, typer.Option("--json", help="Machine-readable output")]

_RELAY_STATUS_STYLE = {
    "pending": "[cyan]pending[/cyan]",
    "sending": "[yellow]sending[/yellow]",
    "sent": "[green]● sent[/green]",
    "failed": "[red]✗ failed[/red]",
    "cancelled": "[dim]cancelled[/dim]",
    "stuck": "[bold red]⚠ stuck[/bold red]",
}


def _relay_client(url: Optional[str], *, need_token: bool = True):
    """A configured client, or exit naming the exact setting that is missing."""
    from navig_social import relay

    cfg = relay.resolve_config(url)
    if not cfg.url:
        ch.error("No relay URL",
                 f"pass --url, set NAVIG_RELAY_URL, or store it: {vault_set('relay', '<url>')}")
        raise typer.Exit(1)
    if need_token and not cfg.token:
        ch.error("No relay token",
                 f"set NAVIG_RELAY_TOKEN, or store it: {vault_set('relay', '<token>')}")
        raise typer.Exit(1)
    return relay.RelayClient(cfg)


def _relay_fail(exc) -> None:
    ch.error("Relay request failed", str(exc))
    raise typer.Exit(1) from exc


def _fmt_ts(ts) -> str:
    """Unix seconds → local wall-clock, which is what the operator scheduled in."""
    from datetime import datetime

    if not isinstance(ts, int):
        return "—"
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")


@relay_app.command("status")
def relay_status(url: _RelayUrl = None, as_json: _Json = False) -> None:
    """Is the relay up, which channels may it post to, and does anything need a human?"""
    import json as _json

    from navig_social import relay

    client = _relay_client(url, need_token=False)
    try:
        health = client.health()
        counts: dict[str, int] = {}
        if client.config.token:
            for s in ("pending", "stuck", "failed"):
                counts[s] = len(client.list(status=s, limit=500))
    except relay.RelayError as exc:
        _relay_fail(exc)
        return

    if as_json:
        ch.raw_print(_json.dumps({"url": client.config.url, "health": health, "counts": counts}, indent=2))
        return
    ch.success(f"relay up at {client.config.url}")
    ch.console.print("  channels: " + "  ".join(health.get("allowed_channels") or []))
    if not counts:
        ch.dim(f"  no token — queue counts hidden · {vault_set('relay', '<token>')}")
        return
    ch.console.print(f"  pending {counts['pending']} · failed {counts['failed']} · stuck {counts['stuck']}")
    if counts["stuck"]:
        ch.warning(f"{counts['stuck']} stuck post(s) — the send may or may not have landed",
                   f"check the channel in Telegram, then {CMD} relay queue --status stuck")


@relay_app.command("post")
def relay_post(
    channel: Annotated[str, typer.Argument(help="Target channel, e.g. somaleto or @somaleto")],
    text: Annotated[Optional[str], typer.Option("--text", "-t", help="Post body")] = None,
    file: Annotated[Optional[str], typer.Option("--file", "-f", help="Read the body from a UTF-8 file")] = None,
    at: Annotated[str, typer.Option("--at", help="now · +30m · +2h · +3d · 2026-10-01T09:00 (local time)")] = "now",
    parse_mode: Annotated[Optional[str], typer.Option("--parse-mode", help="HTML or MarkdownV2")] = None,
    no_preview: Annotated[bool, typer.Option("--no-preview", help="Disable the link preview")] = False,
    force: Annotated[bool, typer.Option("--force", help="Queue it even if this exact post is already queued")] = False,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Show what would be queued; send nothing")] = False,
    url: _RelayUrl = None,
    as_json: _Json = False,
) -> None:
    """Queue one post. Running the same command again does not queue it twice.

    With an exact --at, the same text at the same time is one post. With 'now' or
    '+2h', the same text to the same channel is one post per day — a retry a few
    seconds later can't double-post. --force queues a deliberate repeat.

    Examples:
      navig social relay post somaleto --file note.md --at "2026-10-01T09:00"
      navig social relay post miztizm -t "navig 3.26 is out" --at +2h
    """
    import json as _json
    from pathlib import Path

    from navig_social import relay

    if bool(text) == bool(file):
        ch.error("Give exactly one of --text or --file")
        raise typer.Exit(1)
    try:
        body = text if text is not None else Path(file).read_text(encoding="utf-8").strip()
    except OSError as exc:
        ch.error("Can't read --file", str(exc))
        raise typer.Exit(1) from exc
    if not body.strip():
        ch.error("The post body is empty")
        raise typer.Exit(1)
    if len(body) > relay.TELEGRAM_MAX_CHARS:
        ch.error(f"Body is {len(body)} characters — Telegram's limit is {relay.TELEGRAM_MAX_CHARS}")
        raise typer.Exit(1)
    if parse_mode and parse_mode not in ("HTML", "MarkdownV2"):
        ch.error("--parse-mode must be HTML or MarkdownV2")
        raise typer.Exit(1)
    try:
        target = relay.normalize_channel(channel)
        resolved = relay.resolve_when(at)
    except ValueError as exc:
        ch.error(str(exc))
        raise typer.Exit(1) from exc
    when = resolved.at
    key = relay.idempotency_key(target, resolved, body)
    if force:
        import uuid

        key = f"{key[:20]}-force-{uuid.uuid4().hex[:11]}"

    if dry_run:
        preview = {"channel": target, "scheduled_at": when, "local_time": _fmt_ts(when),
                   "chars": len(body), "idempotency_key": key, "body": body}
        if as_json:
            ch.raw_print(_json.dumps(preview, indent=2, ensure_ascii=False))
        else:
            ch.info(f"would queue → {target} at {_fmt_ts(when)} ({len(body)} chars)")
            ch.console.print(body[:400] + ("…" if len(body) > 400 else ""), markup=False)
            ch.dim("--dry-run: nothing sent to the relay")
        return

    client = _relay_client(url)
    try:
        out = client.enqueue(channel=target, body=body, scheduled_at=when,
                             parse_mode=parse_mode, disable_preview=no_preview, key=key)
    except relay.RelayError as exc:
        _relay_fail(exc)
        return

    if as_json:
        ch.raw_print(_json.dumps(out, indent=2, ensure_ascii=False))
    elif out.get("deduplicated"):
        state = out.get("status")
        if state in ("cancelled", "failed"):
            # The earlier copy won't go out, so "already queued" alone would mislead.
            ch.warning(f"this exact post was {state} earlier ({out.get('id')}) — nothing added",
                       "add --force to queue it again")
        elif state == "sent":
            ch.info(f"already sent — {out.get('id')}; nothing added (--force to post it again)")
        else:
            ch.info(f"already queued — {out.get('id')} ({state}); nothing added")
    else:
        ch.success(f"queued {out.get('id')} → {target} at {_fmt_ts(when)}")


@relay_app.command("queue")
def relay_queue(
    status: Annotated[Optional[str], typer.Option("--status", "-s",
                      help="pending · sent · failed · cancelled · stuck")] = "pending",
    channel: Annotated[Optional[str], typer.Option("--channel", "-c", help="Only this channel")] = None,
    all_: Annotated[bool, typer.Option("--all", help="Every status, not just pending")] = False,
    limit: Annotated[int, typer.Option("--limit", "-n")] = 100,
    url: _RelayUrl = None,
    as_json: _Json = False,
) -> None:
    """What is scheduled — the relay's stand-in for Telegram's Scheduled tab."""
    import json as _json

    from rich.table import Table

    from navig_social import relay

    client = _relay_client(url)
    try:
        target = relay.normalize_channel(channel) if channel else None
        posts = client.list(status=None if all_ else status, channel=target, limit=limit)
    except (relay.RelayError, ValueError) as exc:
        _relay_fail(exc)
        return

    if as_json:
        ch.raw_print(_json.dumps(posts, indent=2, ensure_ascii=False))
        return
    if not posts:
        ch.dim(f"nothing {'in the queue' if all_ else status}"
               + (f" for {target}" if target else ""))
        return
    table = Table(box=None, show_header=True, padding=(0, 2))
    table.add_column("when", no_wrap=True)
    table.add_column("channel", style="bold", no_wrap=True)
    table.add_column("status", no_wrap=True)
    table.add_column("id", no_wrap=True, style="dim")
    table.add_column("preview")
    for p in posts:
        detail = (p.get("preview") or "").replace("\n", " ")
        if p.get("last_error") and p.get("status") in ("failed", "stuck"):
            detail = f"[red]{p['last_error']}[/red]"
        table.add_row(_fmt_ts(p.get("scheduled_at")), p.get("channel") or "—",
                      _RELAY_STATUS_STYLE.get(p.get("status"), p.get("status") or "—"),
                      (p.get("id") or "")[:8], detail)
    ch.console.print(table)
    ch.dim(f"{len(posts)} post(s) · times are local · {CMD} relay cancel <id> to drop a pending one")


@relay_app.command("cancel")
def relay_cancel(
    post_id: Annotated[str, typer.Argument(help="Post id — the full id, or a unique prefix from `queue`")],
    url: _RelayUrl = None,
) -> None:
    """Cancel a still-pending post. A sent post can't be un-sent from here."""
    from navig_social import relay

    client = _relay_client(url)
    try:
        full = post_id
        if len(post_id) < 36:  # a prefix copied from `queue` — resolve it against pending posts
            hits = [p["id"] for p in client.list(status="pending", limit=500) if p["id"].startswith(post_id)]
            if len(hits) != 1:
                ch.error(f"'{post_id}' matches {len(hits)} pending post(s)",
                         "use more characters, or the full id from --json")
                raise typer.Exit(1)
            full = hits[0]
        client.cancel(full)
    except relay.RelayError as exc:
        _relay_fail(exc)
        return
    ch.success(f"cancelled {full}")


@relay_app.command("drain")
def relay_drain(url: _RelayUrl = None, as_json: _Json = False) -> None:
    """Send everything that is due now, without waiting for the next cron tick."""
    import json as _json

    from navig_social import relay

    client = _relay_client(url)
    try:
        report = client.drain()
    except relay.RelayError as exc:
        _relay_fail(exc)
        return
    if as_json:
        ch.raw_print(_json.dumps(report, indent=2))
        return
    ch.success(f"sent {report.get('sent', 0)} · failed {report.get('failed', 0)} · "
               f"deferred {report.get('skipped', 0)} · parked stuck {report.get('stuck', 0)}")


# alias target so `navig connect …` could map here if desired later
connect_app = social_app
