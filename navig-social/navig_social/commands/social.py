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

from navig.lazy_loader import lazy_import

ch = lazy_import("navig.console_helper")

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
    from navig.console_helper import Table
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
                     f"connect one with[/dim] [cyan]navig social connect <network>[/cyan]")


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
                   "Set one:  navig config set adapters.social.tracking.base_url https://<your-lighthouse-url>")
    results = asyncio.run(fan_out(brief, platforms=platforms, campaign=campaign or None, dry_run=dry_run, track=track))

    if as_json:
        ch.console.print_json(json.dumps([r.to_dict() for r in results]))
        return

    from navig.console_helper import Table

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

    from navig.console_helper import Table
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
                    "Publish with:  navig social fan-out --campaign <slug> …")
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
                "Fan out something:  navig social fan-out --file brief.json --to x,telegram --campaign launch")
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
    hint = "navig social receipts --campaign <slug> for detail"
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
               "Joined view:  navig social receipts --with-engagement")


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

    from navig.console_helper import Table
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
                "Publish something first:  navig social fan-out --file brief.json --to x,devto --campaign launch")
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
    hint = "navig social report" + (f" --campaign {campaign}" if campaign else "") + " to see the CTR"
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

    from navig.console_helper import Table
    from navig_social.social.labels import display_label
    from navig_social.social.report import campaign_scorecard, leaderboard

    if campaign:
        card = campaign_scorecard(campaign)
        if as_json:
            ch.raw_print(json.dumps(card, indent=2, ensure_ascii=False))
            return
        if not card["networks"]:
            ch.info(f"Nothing recorded for campaign '{campaign}'",
                    "Publish with:  navig social fan-out --campaign <slug> …")
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
                "Fan out something:  navig social fan-out --file brief.json --to x,telegram --campaign launch")
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
    ch.dim(f"{len(board)} campaign(s) ranked by clicks · navig social report --campaign <slug> for detail")


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

    from navig.console_helper import Table

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
        ch.dim("usage:  navig social stats <platform> <handle> …   ·   --only-public to skip the browser")
        return

    if not handles:
        ch.error("No handle given", "e.g.  navig social stats youtube @mkbhd")
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


# alias target so `navig connect …` could map here if desired later
connect_app = social_app
