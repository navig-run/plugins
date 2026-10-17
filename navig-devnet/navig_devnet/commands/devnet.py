"""`navig devnet` — developer/ops network lookups (DNS · WHOIS · SSL).

House style: a Rich table per view, one wrappable free-text column, semantic
colour, and a `--json` escape hatch for scripts/agents. Standalone-safe — only
rich + the lookup engine, no navig import.
"""

from __future__ import annotations

import json as _json
from pathlib import Path as _Path
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from navig_devnet.lookups import DNS_RECORD_TYPES, LookupError, dns_lookup, ssl_inspect, whois_lookup

devnet_app = typer.Typer(
    name="devnet",
    help="Developer/ops network inspection: DNS, WHOIS, SSL certificate lookups.",
    no_args_is_help=True,
)

_console = Console()


def _fail(msg: str) -> None:
    _console.print(f"[red]✗[/red] {msg}")
    raise typer.Exit(1)


@devnet_app.command()
def dns(
    domain: Annotated[str, typer.Argument(help="Domain or host to resolve.")],
    record_type: Annotated[
        str, typer.Option("--type", "-t", help=f"Record type: {', '.join(DNS_RECORD_TYPES)}.")
    ] = "A",
    resolver: Annotated[
        str, typer.Option("--resolver", "-r", help="Custom DNS resolver (IP or host).")
    ] = "",
    as_json: Annotated[bool, typer.Option("--json", help="Machine-readable JSON.")] = False,
) -> None:
    """Resolve DNS records (MX/TXT/NS/… with dnspython; A/AAAA always)."""
    try:
        result = dns_lookup(domain, record_type=record_type, resolver=resolver or None)
    except LookupError as exc:
        _fail(str(exc))
    except Exception as exc:  # noqa: BLE001 — surface the resolver error cleanly
        _fail(f"lookup failed: {exc}")
    if as_json:
        _console.print_json(_json.dumps(result))
        return
    records = result["records"]
    if not records:
        _console.print(f"[yellow]no {result['type']} records for[/yellow] {result['domain']}")
        return
    table = Table(box=None, show_header=True, padding=(0, 2),
                  title=f"{result['type']} · {result['domain']}", title_justify="left")
    table.add_column(result["type"], style="cyan", no_wrap=True)
    for rec in records:
        table.add_row(rec)
    _console.print(table)
    _console.print(f"[dim]{len(records)} record(s) · via {result['engine']}"
                   + (f" · resolver {result['resolver']}" if result.get("resolver") else "") + "[/dim]")


@devnet_app.command()
def ssl(
    host: Annotated[str, typer.Argument(help="Host whose TLS certificate to inspect.")],
    port: Annotated[int, typer.Option("--port", "-p", help="TLS port.")] = 443,
    as_json: Annotated[bool, typer.Option("--json", help="Machine-readable JSON.")] = False,
) -> None:
    """Read a server's TLS certificate (issuer, validity, SANs, days left)."""
    try:
        cert = ssl_inspect(host, port=port)
    except LookupError as exc:
        _fail(str(exc))
    except Exception as exc:  # noqa: BLE001
        _fail(f"TLS handshake failed: {exc}")
    if as_json:
        _console.print_json(_json.dumps(cert))
        return
    table = Table(box=None, show_header=False, padding=(0, 2))
    table.add_column("field", style="dim", no_wrap=True)
    table.add_column("value")
    dl = cert["days_left"]
    dl_txt = "—" if dl is None else (
        f"[red]{dl} days[/red]" if dl < 0 else
        f"[yellow]{dl} days[/yellow]" if dl < 21 else f"[green]{dl} days[/green]"
    )
    table.add_row("host", f"{cert['host']}:{cert['port']}")
    table.add_row("common name", cert["common_name"] or "—")
    table.add_row("issuer", cert["issuer"] or "—")
    table.add_row("valid from", cert["not_before"] or "—")
    table.add_row("valid to", cert["not_after"] or "—")
    table.add_row("expires in", dl_txt)
    table.add_row("SAN", ", ".join(cert["san"]) or "—")
    _console.print(table)


@devnet_app.command()
def whois(
    domain: Annotated[str, typer.Argument(help="Domain to look up.")],
    as_json: Annotated[bool, typer.Option("--json", help="Machine-readable JSON.")] = False,
) -> None:
    """WHOIS registration record (bare-TCP; no system whois binary needed)."""
    try:
        result = whois_lookup(domain)
    except LookupError as exc:
        _fail(str(exc))
    except Exception as exc:  # noqa: BLE001
        _fail(f"whois failed: {exc}")
    if as_json:
        _console.print_json(_json.dumps(result))
        return
    _console.print(f"[dim]whois {result['domain']} · via {result['server']}[/dim]")
    _console.print(result["raw"].strip() or "[yellow]empty response[/yellow]")


# ── HTTP repeater ─────────────────────────────────────────────────────────────
def _parse_headers(pairs: list[str]) -> dict[str, str]:
    """Parse ``-H "Key: Value"`` pairs. A pair without a colon is rejected."""
    out: dict[str, str] = {}
    for p in pairs or []:
        if ":" not in p:
            _fail(f"bad header {p!r} — use 'Key: Value'")
        k, v = p.split(":", 1)
        out[k.strip()] = v.strip()
    return out


@devnet_app.command()
def http(
    method: Annotated[str, typer.Argument(help="HTTP method: GET, POST, PUT, PATCH, DELETE, HEAD, OPTIONS.")],
    url: Annotated[str, typer.Argument(help="Absolute URL to send the request to.")],
    header: Annotated[list[str], typer.Option("--header", "-H", help="Request header 'Key: Value' (repeatable).")] = None,
    body: Annotated[str, typer.Option("--body", "-d", help="Request body (string).")] = "",
    body_file: Annotated[str, typer.Option("--body-file", help="Read the request body from a file.")] = "",
    no_redirect: Annotated[bool, typer.Option("--no-redirect", help="Do not follow redirects.")] = False,
    allow_private: Annotated[bool, typer.Option("--allow-private", help="Allow private/internal targets (SSRF guard off).")] = False,
    timeout: Annotated[float, typer.Option("--timeout", help="Timeout in seconds.")] = 15.0,
    show_body: Annotated[bool, typer.Option("--show-body/--no-show-body", help="Print the response body.")] = True,
    save: Annotated[bool, typer.Option("--save/--no-save", help="Record in per-space history.")] = True,
    as_json: Annotated[bool, typer.Option("--json", help="Machine-readable JSON.")] = False,
) -> None:
    """Send an HTTP request and read the full response (the repeater)."""
    from navig_devnet import history
    from navig_devnet.http_repeater import send_sync

    payload: str | bytes | None = None
    if body_file:
        try:
            payload = _Path(body_file).read_bytes()
        except OSError as exc:
            _fail(f"cannot read --body-file: {exc}")
    elif body:
        payload = body
    try:
        result = send_sync(
            method, url,
            headers=_parse_headers(header or []),
            body=payload,
            allow_private=allow_private,
            follow_redirects=not no_redirect,
            timeout=timeout,
        )
    except Exception as exc:  # noqa: BLE001 — surface SSRF/transport errors cleanly
        _fail(f"{type(exc).__name__}: {exc}")
    entry_id = history.record(result) if save else None

    if as_json:
        if entry_id is not None:
            result["history_id"] = entry_id
        _console.print_json(_json.dumps(result))
        return

    resp = result["response"]
    status = resp["status"]
    colour = "green" if status < 400 else ("yellow" if status < 500 else "red")
    for hop in result["redirects"]:
        _console.print(f"[dim]{hop['status']} → {hop['to']}[/dim]")
    _console.print(
        f"[{colour}]{status} {resp['reason']}[/{colour}] · {result['elapsed_ms']} ms · "
        f"{resp['body_len']:,} bytes · {resp['content_type'] or '—'}"
        + (f"  [dim](history #{entry_id})[/dim]" if entry_id else "")
    )
    table = Table(box=None, show_header=False, padding=(0, 2))
    table.add_column("h", style="cyan", no_wrap=True)
    table.add_column("v")
    for k, v in resp["headers"]:
        table.add_row(k, v)
    _console.print(table)
    if show_body and resp["body"]:
        _console.print("[dim]── body ──[/dim]")
        _console.print(resp["body"])
        if resp["body_truncated"]:
            _console.print("[dim]… (truncated; --json for the full captured body)[/dim]")


@devnet_app.command()
def history(
    limit: Annotated[int, typer.Option("--limit", "-n", help="How many recent entries to show.")] = 20,
    as_json: Annotated[bool, typer.Option("--json", help="Machine-readable JSON.")] = False,
) -> None:
    """List recent repeater requests in this space."""
    from navig_devnet import history as hist

    rows = hist.load(limit=limit)
    if as_json:
        _console.print_json(_json.dumps(rows))
        return
    if not rows:
        _console.print("[dim]no requests recorded in this space yet — run `navig devnet http …`[/dim]")
        return
    table = Table(box=None, show_header=True, padding=(0, 2))
    table.add_column("#", style="dim", no_wrap=True, justify="right")
    table.add_column("method", style="cyan", no_wrap=True)
    table.add_column("status", no_wrap=True)
    table.add_column("ms", justify="right", no_wrap=True)
    table.add_column("url")
    for e in rows:
        st = e.get("status", 0)
        c = "green" if st < 400 else ("yellow" if st < 500 else "red")
        table.add_row(str(e.get("id", "?")), e.get("method", "?"),
                      f"[{c}]{st}[/{c}]", str(e.get("elapsed_ms", "")), e.get("url", ""))
    _console.print(table)
    _console.print("[dim]diff two with: navig devnet diff <#> <#>[/dim]")


@devnet_app.command()
def diff(
    id_a: Annotated[int, typer.Argument(help="First history id.")],
    id_b: Annotated[int, typer.Argument(help="Second history id.")],
) -> None:
    """Compare two recorded responses (status, headers, body)."""
    import difflib

    from navig_devnet import history as hist

    a, b = hist.get(id_a), hist.get(id_b)
    if a is None or b is None:
        _fail(f"history id not found: {id_a if a is None else id_b}")
    _console.print(f"[dim]#{id_a}[/dim] {a['method']} {a['url']} → [bold]{a['status']}[/bold]"
                   f"   vs   [dim]#{id_b}[/dim] {b['method']} {b['url']} → [bold]{b['status']}[/bold]")
    if a["status"] != b["status"]:
        _console.print(f"status: [yellow]{a['status']} → {b['status']}[/yellow]")
    body_a = (a.get("response_body") or "").splitlines()
    body_b = (b.get("response_body") or "").splitlines()
    delta = list(difflib.unified_diff(body_a, body_b, fromfile=f"#{id_a}", tofile=f"#{id_b}", lineterm=""))
    if not delta:
        _console.print("[green]bodies identical[/green]")
        return
    for line in delta[:400]:
        if line.startswith("+") and not line.startswith("+++"):
            _console.print(f"[green]{line}[/green]")
        elif line.startswith("-") and not line.startswith("---"):
            _console.print(f"[red]{line}[/red]")
        else:
            _console.print(f"[dim]{line}[/dim]")


# ── crawl / API discovery (read-only) ─────────────────────────────────────────
@devnet_app.command()
def crawl(
    url: Annotated[str, typer.Argument(help="Start URL to crawl.")],
    pages: Annotated[int, typer.Option("--pages", "-n", help="Max pages to fetch.")] = 25,
    depth: Annotated[int, typer.Option("--depth", help="Max link depth from the start.")] = 2,
    off_origin: Annotated[bool, typer.Option("--off-origin", help="Follow links to other origins too.")] = False,
    ignore_robots: Annotated[bool, typer.Option("--ignore-robots", help="Do not honour robots.txt.")] = False,
    allow_private: Annotated[bool, typer.Option("--allow-private", help="Allow private/internal targets (SSRF guard off).")] = False,
    timeout: Annotated[float, typer.Option("--timeout", help="Per-request timeout in seconds.")] = 15.0,
    as_json: Annotated[bool, typer.Option("--json", help="Machine-readable JSON.")] = False,
) -> None:
    """Map a site: a polite, same-origin, robots-respecting read-only crawl."""
    from navig_devnet.crawl import CrawlError, crawl_sync

    try:
        result = crawl_sync(
            url, max_pages=pages, max_depth=depth, same_origin=not off_origin,
            respect_robots=not ignore_robots, allow_private=allow_private, timeout=timeout,
        )
    except CrawlError as exc:
        _fail(str(exc))
    except Exception as exc:  # noqa: BLE001
        _fail(f"{type(exc).__name__}: {exc}")

    if as_json:
        _console.print_json(_json.dumps(result))
        return

    rows = result["pages"]
    if not rows:
        _console.print(f"[yellow]no pages crawled from[/yellow] {result['start']}")
        return
    table = Table(box=None, show_header=True, padding=(0, 2),
                  title=f"crawl · {result['origin']}", title_justify="left")
    table.add_column("status", no_wrap=True, justify="right")
    table.add_column("depth", style="dim", no_wrap=True, justify="right")
    table.add_column("links", style="dim", no_wrap=True, justify="right")
    table.add_column("title", no_wrap=True)
    table.add_column("url")
    for p in rows:
        st = p["status"]
        c = "green" if 0 < st < 400 else ("yellow" if 400 <= st < 500 else "red")
        st_txt = f"[{c}]{st or '—'}[/{c}]" if st else f"[red]{p.get('error', 'err')[:18]}[/red]"
        table.add_row(st_txt, str(p["depth"]), str(len(p["links"])),
                      (p["title"] or "—")[:40], p["url"])
    _console.print(table)
    tail = "" if not result["truncated"] else f" · [yellow]truncated at {result['count']}[/yellow]"
    _console.print(f"[dim]{result['count']} page(s)"
                   + (" · robots respected" if result["robots_respected"] else " · robots ignored")
                   + tail + "[/dim]")


@devnet_app.command()
def api(
    url: Annotated[str, typer.Argument(help="Base URL or a direct spec URL.")],
    allow_private: Annotated[bool, typer.Option("--allow-private", help="Allow private/internal targets (SSRF guard off).")] = False,
    timeout: Annotated[float, typer.Option("--timeout", help="Per-request timeout in seconds.")] = 15.0,
    as_json: Annotated[bool, typer.Option("--json", help="Machine-readable JSON.")] = False,
) -> None:
    """Discover and parse an OpenAPI/Swagger spec; list its endpoints."""
    from navig_devnet.crawl import CrawlError, discover_api_sync

    try:
        result = discover_api_sync(url, allow_private=allow_private, timeout=timeout)
    except CrawlError as exc:
        _fail(str(exc))
    except Exception as exc:  # noqa: BLE001
        _fail(f"{type(exc).__name__}: {exc}")

    if as_json:
        _console.print_json(_json.dumps(result))
        return

    ver = f" v{result['version']}" if result["version"] else ""
    _console.print(f"[bold]{result['title'] or 'API'}[/bold]{ver} "
                   f"[dim]· OpenAPI {result['openapi']} · {result['spec_url']}[/dim]")
    if result["servers"]:
        _console.print(f"[dim]servers: {', '.join(result['servers'])}[/dim]")
    endpoints = result["endpoints"]
    if not endpoints:
        _console.print("[yellow]spec found, but it declares no paths[/yellow]")
        return
    table = Table(box=None, show_header=True, padding=(0, 2))
    table.add_column("method", style="cyan", no_wrap=True)
    table.add_column("path", no_wrap=True)
    table.add_column("summary")
    for e in endpoints:
        table.add_row(e["method"], e["path"], e["summary"] or "—")
    _console.print(table)
    _console.print(f"[dim]{result['endpoint_count']} endpoint(s)[/dim]")
