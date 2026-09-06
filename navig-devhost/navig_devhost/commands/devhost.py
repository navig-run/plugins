"""navig devhost — local dev domains with trusted HTTPS.

Give any local dev server a real `.test` domain over trusted HTTPS in one command:

    navig devhost add cybesis.test --port 7645
    navig devhost up                 # → https://cybesis.test

devhost adds the hosts entry (on a dedicated loopback, coexisting with your other
.test sites), issues a trusted mkcert certificate, and runs a raw TLS relay in
front of your plain-HTTP dev server. No per-project proxy scripts.
"""

from __future__ import annotations

import json as _json
import time
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from navig_devhost.engine import certs, hosts, net
from navig_devhost.engine.paths import registry_path
from navig_devhost.engine.proxy import Relay, Site
from navig_devhost.engine.registry import DevHost, JsonReadError, Registry

console = Console()
err = Console(stderr=True)

devhost_app = typer.Typer(
    name="devhost",
    help="🌐 Dev Host: local .test domains with trusted HTTPS for any dev server.",
    no_args_is_help=True,
)


# ── helpers ─────────────────────────────────────────────────────────────────
def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def _load_for_write(json_out: bool) -> Registry:
    """Load the registry for a read-MODIFY-write (add / remove).

    Refuses to continue if registry.json exists but is transiently unreadable —
    saving after a failed read would persist an empty registry over every other
    host (the config-wipe class). The read-only views use ``Registry.load()``.
    """
    try:
        return Registry.load_for_update()
    except JsonReadError as e:
        _fail(json_out, f"registry unreadable — not saving, to avoid wiping hosts: {e}")
        raise typer.Exit(2) from e


def _sites_from(entries: list[DevHost]) -> tuple[list[Site], list[str]]:
    """Build relay Sites from registry entries; collect skip reasons."""
    sites, skipped = [], []
    for dh in entries:
        if not dh.tls:
            skipped.append(f"{dh.domain}: TLS disabled")
            continue
        if not dh.cert or not dh.key or not Path(dh.cert).exists() or not Path(dh.key).exists():
            skipped.append(f"{dh.domain}: cert missing (re-run `navig devhost add {dh.domain}`)")
            continue
        sites.append(Site(dh.domain, dh.ip, dh.https_port, dh.target_host, dh.target_port, dh.cert, dh.key))
    return sites, skipped


# ── add ─────────────────────────────────────────────────────────────────────
@devhost_app.command()
def add(
    domain: str = typer.Argument(..., help="The .test domain, e.g. cybesis.test"),
    port: int = typer.Option(..., "--port", "-p", help="Dev server port to proxy to."),
    ip: Optional[str] = typer.Option(None, "--ip", help="Loopback IP (default: next free 127.0.0.x)."),
    target_host: str = typer.Option("127.0.0.1", "--target-host", help="Where the dev server listens."),
    tls: bool = typer.Option(True, "--tls/--no-tls", help="Issue an mkcert cert and serve HTTPS."),
    json_out: bool = typer.Option(False, "--json", help="Emit JSON."),
) -> None:
    """Register a dev domain: hosts entry + (optional) trusted cert."""
    domain = domain.strip().lower()
    reg = _load_for_write(json_out)

    # resolve the loopback IP: explicit → existing hosts mapping → existing registry → next free
    used_ips = [d.ip for d in reg.all()]
    if ip:
        chosen_ip = ip
    elif (existing := hosts.entry_for(domain)):
        chosen_ip = existing
    elif reg.get(domain):
        chosen_ip = reg.get(domain).ip  # type: ignore[union-attr]
    else:
        chosen_ip = net.next_free_loopback(hosts.read(), *used_ips)

    # hosts entry (idempotent; needs admin only if a write is required)
    hres = hosts.add(chosen_ip, domain)
    if not hres.ok:
        _fail(json_out, f"hosts: {hres.message}")
        raise typer.Exit(2)

    # certificate
    cert = key = None
    cert_msg = "TLS disabled"
    if tls:
        cres = certs.generate(domain, chosen_ip)
        if not cres.ok:
            _fail(json_out, f"cert: {cres.message}")
            raise typer.Exit(2)
        cert, key, cert_msg = cres.cert, cres.key, cres.message
        if not certs.ca_installed():
            (err if not json_out else console).print(
                "[yellow]![/yellow] mkcert CA not detected — run [cyan]mkcert -install[/cyan] once so browsers trust it."
            )

    dh = DevHost(
        domain=domain, ip=chosen_ip, target_host=target_host, target_port=port,
        tls=tls, cert=cert, key=key, created=(reg.get(domain).created if reg.get(domain) else _now()),
    )
    reg.put(dh)
    reg.save()

    if json_out:
        console.print_json(_json.dumps({"ok": True, "domain": domain, "ip": chosen_ip,
                                        "url": dh.url, "target": dh.target, "tls": tls, "cert": cert}))
        return
    console.print(Panel.fit(
        f"[bold]{dh.url}[/bold]  →  {dh.target}\n"
        f"[dim]{chosen_ip}  ·  {hres.message}  ·  {cert_msg}[/dim]",
        title="devhost added", border_style="green",
    ))
    console.print(f"Start it:  [cyan]navig devhost up {domain}[/cyan]   (run your dev server on :{port} first)")


# ── list ────────────────────────────────────────────────────────────────────
@devhost_app.command("list")
def list_cmd(json_out: bool = typer.Option(False, "--json", help="Emit JSON.")) -> None:
    """List registered dev domains and their live status."""
    reg = Registry.load()
    entries = reg.all()
    if json_out:
        console.print_json(_json.dumps({"domains": [
            {"domain": d.domain, "url": d.url, "ip": d.ip, "target": d.target, "tls": d.tls,
             "hosts_ok": hosts.entry_for(d.domain) == d.ip,
             "cert_ok": bool(d.cert and Path(d.cert).exists()),
             "target_up": net.target_reachable(d.target_host, d.target_port),
             "serving": not net.can_bind(d.ip, d.https_port)} for d in entries]}))
        return
    if not entries:
        console.print("[dim]No dev domains yet.[/dim]  Add one:  [cyan]navig devhost add app.test --port 3000[/cyan]")
        return
    table = Table(title="devhost domains", header_style="bold cyan")
    for col in ("Domain", "URL", "→ Target", "Hosts", "Cert", "Dev up", "Serving"):
        table.add_column(col)
    for d in entries:
        table.add_row(
            d.domain, d.url, d.target,
            _yn(hosts.entry_for(d.domain) == d.ip),
            _yn(bool(d.cert and Path(d.cert).exists())) if d.tls else "[dim]—[/dim]",
            _yn(net.target_reachable(d.target_host, d.target_port)),
            _yn(not net.can_bind(d.ip, d.https_port)),
        )
    console.print(table)


# ── up ──────────────────────────────────────────────────────────────────────
@devhost_app.command()
def up(
    domain: Optional[str] = typer.Argument(None, help="Domain to serve (default: all registered)."),
    all_: bool = typer.Option(False, "--all", help="Serve every registered domain."),
) -> None:
    """Run the HTTPS relay (foreground). Ctrl+C to stop."""
    reg = Registry.load()
    if domain:
        dh = reg.get(domain.strip().lower())
        if not dh:
            err.print(f"[red]✗[/red] '{domain}' is not registered. Add it:  navig devhost add {domain} --port <PORT>")
            raise typer.Exit(2)
        entries = [dh]
    else:
        entries = reg.all()
        if not entries:
            err.print("[red]✗[/red] no domains registered. Add one:  navig devhost add app.test --port 3000")
            raise typer.Exit(2)

    sites, skipped = _sites_from(entries)
    for reason in skipped:
        err.print(f"[yellow]skip[/yellow] {reason}")
    if not sites:
        err.print("[red]✗[/red] nothing to serve (no TLS domains with valid certs).")
        raise typer.Exit(2)

    relay = Relay(sites, on_log=lambda m: console.print(m))
    console.print(Panel.fit("devhost relay — [dim]Ctrl+C to stop[/dim]", border_style="cyan"))
    errors = relay.start()
    for e in errors:
        err.print(f"[red]✗[/red] {e}")
    if len(errors) == len(sites):
        raise typer.Exit(1)
    for s in sites:
        if not net.target_reachable(s.target_host, s.target_port):
            err.print(f"[yellow]![/yellow] {s.domain}: dev server not up yet on :{s.target_port} "
                      f"— start it; the relay is ready and will connect on reload.")
    relay.serve_forever()
    console.print("\n[dim]devhost relay stopped.[/dim]")


# ── status ──────────────────────────────────────────────────────────────────
@devhost_app.command()
def status(json_out: bool = typer.Option(False, "--json", help="Emit JSON.")) -> None:
    """One-line health per domain (hosts · cert · dev-up · serving)."""
    list_cmd(json_out=json_out)


# ── remove ──────────────────────────────────────────────────────────────────
@devhost_app.command()
def remove(
    domain: str = typer.Argument(..., help="Domain to remove."),
    keep_cert: bool = typer.Option(False, "--keep-cert", help="Leave the mkcert files on disk."),
    json_out: bool = typer.Option(False, "--json", help="Emit JSON."),
) -> None:
    """Remove a dev domain: hosts entry + cert + registry record."""
    domain = domain.strip().lower()
    reg = _load_for_write(json_out)
    dh = reg.get(domain)

    hres = hosts.remove(domain)
    if not hres.ok:
        # The hosts entry could not be removed (needs admin, or a write error) — do NOT
        # delete the cert files or drop the registry record. Tearing those down while the
        # live hosts entry survives orphans the domain (it still resolves to the loopback)
        # and throws away the state needed to retry. `add` aborts on a hosts failure too;
        # remove must mirror it. Re-run in an elevated terminal to complete the removal.
        _fail(json_out, f"hosts: {hres.message}")
        raise typer.Exit(2)

    removed_certs = []
    if dh and not keep_cert:
        for p in (dh.cert, dh.key):
            if p and Path(p).exists():
                try:
                    Path(p).unlink()
                    removed_certs.append(p)
                except OSError:
                    pass
    reg.remove(domain)
    reg.save()

    if json_out:
        console.print_json(_json.dumps({"ok": hres.ok, "domain": domain, "hosts": hres.message,
                                        "certs_removed": removed_certs}))
        return
    mark = "[green]✓[/green]" if hres.ok else "[yellow]![/yellow]"
    console.print(f"{mark} {domain} — {hres.message}"
                  + (f"; removed {len(removed_certs)} cert file(s)" if removed_certs else ""))


# ── doctor ──────────────────────────────────────────────────────────────────
@devhost_app.command()
def doctor() -> None:
    """Check prerequisites: mkcert, its CA, admin for hosts edits."""
    console.print(Panel.fit("navig devhost · environment check", border_style="cyan"))
    ok = True

    exe = certs.find_mkcert()
    if exe:
        console.print(f"[green]✓[/green] mkcert: [dim]{exe}[/dim]")
    else:
        console.print("[red]✗[/red] mkcert not found — [cyan]winget install FiloSottile.mkcert[/cyan]")
        ok = False

    if certs.ca_installed():
        console.print(f"[green]✓[/green] local CA present: [dim]{certs.caroot()}[/dim]")
    else:
        console.print("[yellow]![/yellow] local CA not detected — run [cyan]mkcert -install[/cyan] once")

    if hosts.can_edit():
        console.print("[green]✓[/green] can edit hosts file (admin) — needed for add/remove")
    else:
        console.print("[yellow]![/yellow] not elevated — run add/remove in an Administrator terminal")

    console.print(f"[green]✓[/green] registry: [dim]{registry_path()}[/dim]")
    console.print()
    if ok:
        console.print("[bold green]Ready.[/bold green]  Try:  [cyan]navig devhost add app.test --port 3000[/cyan]")
    else:
        raise typer.Exit(1)


# ── tiny formatters ─────────────────────────────────────────────────────────
def _yn(v: bool) -> str:
    return "[green]✓[/green]" if v else "[red]✗[/red]"


def _fail(json_out: bool, msg: str) -> None:
    if json_out:
        console.print_json(_json.dumps({"ok": False, "error": msg}))
    else:
        err.print(f"[red]✗[/red] {msg}")


if __name__ == "__main__":  # python -m navig_devhost.commands.devhost
    devhost_app()
