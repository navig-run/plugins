"""navig antivirus — browser-extension malware/PUP scanning, Chrome registry
auditing, profile-index recovery, and on-demand system malware scans.

Wired natively into navig via the ``navig.commands`` entry point. All output
goes through navig's console_helper when available, with a plain fallback so the
plugin also works standalone.

  navig antivirus extensions           # scan Chrome/Edge/Brave extensions for malware/PUP
  navig antivirus registry             # audit registry-forced extensions (Windows)
  navig antivirus system --type quick  # kick off a Windows Defender scan
  navig antivirus recover-profiles     # rebuild a collapsed Chrome profile list
  navig antivirus report               # all read-only checks in one pass
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

antivirus_app = typer.Typer(
    name="antivirus",
    help="🛡  Antivirus: extension malware/PUP scan · registry audit · profile recovery · system scan",
    no_args_is_help=True,
)


# ── console: prefer navig.console_helper, fall back to typer ─────────────────

class _Console:
    def __init__(self) -> None:
        self._ch = None
        try:
            from navig.lazy_loader import lazy_import
            self._ch = lazy_import("navig.console_helper")
        except Exception:
            try:
                import navig.console_helper as ch  # type: ignore
                self._ch = ch
            except Exception:
                self._ch = None

    def _emit(self, fn: str, msg: str, color) -> None:
        f = getattr(self._ch, fn, None) if self._ch else None
        if callable(f):
            try:
                f(msg); return
            except Exception:
                pass
        typer.secho(msg, fg=color)

    def success(self, m): self._emit("success", m, typer.colors.GREEN)
    def error(self, m): self._emit("error", m, typer.colors.RED)
    def warning(self, m): self._emit("warning", m, typer.colors.YELLOW)
    def info(self, m): self._emit("info", m, typer.colors.CYAN)
    def plain(self, m): typer.echo(m)


c = _Console()
_BAND_COLOR = {"CRITICAL": typer.colors.RED, "HIGH": typer.colors.BRIGHT_RED,
               "MEDIUM": typer.colors.YELLOW, "LOW": typer.colors.WHITE}


def _resolve_ud(browser: str, user_data: Path | None) -> Path:
    from navig_antivirus.engine import browsers
    ud = user_data or browsers.user_data_dir(browser)
    if not ud or not ud.exists():
        c.error(f"{browser} User Data directory not found"
                + (f": {user_data}" if user_data else " (is the browser installed?)"))
        raise typer.Exit(2)
    return ud


# ── extensions ──────────────────────────────────────────────────────────────

@antivirus_app.command("extensions", rich_help_panel="Browser")
def cmd_extensions(
    browser: Annotated[str, typer.Option("--browser", "-b", help="chrome | edge | brave")] = "chrome",
    user_data: Annotated[Path | None, typer.Option("--user-data", help="Override User Data dir")] = None,
    min_score: Annotated[int, typer.Option("--min-score", help="Only show findings at/above this risk score")] = 3,
) -> None:
    """🔎 Scan every installed extension across all profiles for malware/PUP/proxyware signatures.

    Read-only. Flags proxy/traffic-MITM, all-sites cookie/webRequest access, extension-management,
    sideloaded/non-Web-Store sources, remote-eval, and known-bad IDs. Examples:
        navig antivirus extensions
        navig antivirus extensions -b edge --min-score 5
    """
    from navig_antivirus.engine import browsers
    ud = _resolve_ud(browser, user_data)
    findings, total, unique, nprof, unreadable = browsers.scan_extensions(ud, min_score=min_score)
    c.info(f"Scanned {total} installed extensions across {nprof} {browser} profiles ({unique} unique).")
    if unreadable:
        c.warning(f"{unreadable} of {nprof} profile(s) could not be read (is {browser} running? "
                  f"permissions?) — this scan is INCOMPLETE. Close {browser} and re-run for a full result.")
    if not findings:
        if total == 0 and unreadable:
            # Nothing was actually inspected — a green "clean" here would be a lie.
            c.error(f"No extension data could be read ({unreadable} profile(s) unreadable) — this is "
                    f"NOT a clean result. Close {browser} and re-run.")
            raise typer.Exit(1)
        c.success("No extensions at or above the risk threshold. ✓")
        return
    c.warning(f"{len(findings)} flagged (risk ≥ {min_score}):")
    for f in findings:
        typer.secho(f"\n[{f.band} {f.score}] {f.name}  ({f.ext_id})",
                    fg=_BAND_COLOR.get(f.band, typer.colors.WHITE), bold=True)
        c.plain(f"    source: {f.source}  from_webstore={f.from_webstore}  MV{f.manifest_version}")
        c.plain(f"    flags : {', '.join(f.flags)}")
        c.plain(f"    in    : {', '.join(f.profiles)}")
    typer.secho(f"\nTip: free VPNs / coupon extensions are broad by design; a KNOWN-MALWARE flag or "
                f"'NOT-from-webstore'/'non-google update_url' is the real red flag.", fg=typer.colors.BRIGHT_BLACK)


# ── registry ────────────────────────────────────────────────────────────────

@antivirus_app.command("registry", rich_help_panel="Browser")
def cmd_registry() -> None:
    """🗝  Audit registry-forced Chrome extensions + install policies for PUP signatures (Windows).

    Read-only. Surfaces bundleware/affiliate installs, insecure http:// update URLs, and the
    current force/blocklist policies. Example:  navig antivirus registry
    """
    from navig_antivirus.engine import registry_scan
    rep = registry_scan.scan_registry()
    if not rep.available:
        c.warning("Registry audit is Windows-only.")
        return
    if not rep.force_installed:
        if rep.unreadable:
            # Nothing was found, but some locations couldn't be read — a green "clean"
            # here would be a lie (force-installs hide in exactly the HKLM/policy keys a
            # non-elevated run can't open). Refuse the clean verdict, like `extensions`.
            c.error(f"Could not read {len(rep.unreadable)} registry location(s) — this is "
                    "NOT a clean result. Re-run elevated (as Administrator) for a full audit.")
            for loc in rep.unreadable:
                c.plain(f"      unreadable: {loc}")
            raise typer.Exit(1)
        c.success("No registry-forced Chrome extensions. ✓")
    else:
        c.info(f"{len(rep.force_installed)} registry-forced extension(s):")
        for rx in rep.force_installed:
            flagged = bool(rx.flags)
            typer.secho(f"  [{rx.hive}] {rx.ext_id}" + ("  <-- SUSPICIOUS" if flagged else ""),
                        fg=typer.colors.RED if flagged else typer.colors.WHITE, bold=flagged)
            if rx.update_url:        c.plain(f"      update_url: {rx.update_url}")
            if rx.install_parameter: c.plain(f"      install_parameter: {rx.install_parameter}")
            if rx.path:              c.plain(f"      path: {rx.path}")
            if rx.flags:             c.plain(f"      flags: {', '.join(rx.flags)}")
        if rep.unreadable:
            c.warning(f"{len(rep.unreadable)} registry location(s) could not be read — the audit "
                      "may be incomplete. Re-run elevated for full coverage.")
    if rep.forcelist:
        c.info(f"ExtensionInstallForcelist policy: {len(rep.forcelist)} entr(y/ies)")
    if rep.blocklist:
        c.info(f"ExtensionInstallBlocklist policy: {len(rep.blocklist)} blocked")


# ── system scan (Defender) ──────────────────────────────────────────────────

@antivirus_app.command("system", rich_help_panel="System")
def cmd_system(
    scan_type: Annotated[str, typer.Option("--type", "-t", help="quick | full | custom")] = "quick",
    path: Annotated[Path | None, typer.Option("--path", "-p", help="Path for a custom scan")] = None,
) -> None:
    """🦠 Kick off an on-demand system malware scan via Windows Defender.

    (Malwarebytes has no scan CLI in consumer builds, so Defender is the engine.) Examples:
        navig antivirus system --type quick
        navig antivirus system --type full
        navig antivirus system --type custom --path C:\\Users\\me\\Downloads
    """
    from navig_antivirus.engine import system_scan
    st = system_scan.detect()
    if st.note:
        c.warning(st.note)
    if st.malwarebytes_installed and st.malwarebytes_scriptable:
        c.info("Malwarebytes scanner CLI detected (not yet wired — Defender used).")
    if not st.defender:
        c.error("Windows Defender (MpCmdRun.exe) not available — cannot run a system scan here.")
        raise typer.Exit(127)
    if scan_type == "custom" and not path:
        c.error("--type custom requires --path")
        raise typer.Exit(2)
    c.info(f"Starting Windows Defender {scan_type} scan… (this can take a while; output streams below)")
    rc = system_scan.run_defender_scan(scan_type, str(path) if path else None)
    if rc == 0:
        c.success("Defender scan completed — no threats reported.")
    elif rc == 2:
        c.error("Defender scan found threat(s). Open Windows Security to review/quarantine.")
    elif rc == 124:
        c.warning("Defender scan timed out.")
    else:
        c.warning(f"Defender scan exited with code {rc}.")
    raise typer.Exit(0 if rc in (0,) else 1)


# ── profile recovery ────────────────────────────────────────────────────────

@antivirus_app.command("recover-profiles", rich_help_panel="Browser")
def cmd_recover(
    browser: Annotated[str, typer.Option("--browser", "-b", help="chrome | edge | brave")] = "chrome",
    user_data: Annotated[Path | None, typer.Option("--user-data", help="Override User Data dir")] = None,
    apply: Annotated[bool, typer.Option("--apply", help="Write the fix (default: dry-run). Browser must be CLOSED.")] = False,
) -> None:
    """🩹 Rebuild a collapsed Chrome profile list (picker shows only 'Default' but the folders exist).

    Rebuilds Local State's profile index from each profile's own Preferences. Dry-run by default;
    --apply requires the browser fully closed and backs up Local State first. Examples:
        navig antivirus recover-profiles              # preview
        navig antivirus recover-profiles --apply      # restore (close Chrome first)
    """
    from navig_antivirus.engine import browsers
    ud = _resolve_ud(browser, user_data)
    backup_dir = ud.parent / "navig-antivirus-backups"
    try:
        plan, backup = browsers.recover_profiles(ud, apply=apply, backup_dir=backup_dir, browser=browser)
    except RuntimeError as exc:
        c.error(str(exc))
        raise typer.Exit(1) from exc
    c.info(f"Indexed now: {plan.indexed_before} → after rebuild: {plan.indexed_after}")
    c.plain(f"  keep : {', '.join(plan.kept) or '(none)'}")
    for d, name, email in plan.readd:
        c.plain(f"  + {d}  \"{name}\"  {email}")
    if not apply:
        c.warning("DRY RUN — nothing written. Re-run with --apply (browser fully closed) to restore.")
    else:
        c.success(f"Restored {plan.indexed_after} profiles. Backup: {backup}. Reopen the browser.")


# ── report (all read-only checks) ───────────────────────────────────────────

@antivirus_app.command("report", rich_help_panel="System")
def cmd_report(
    browser: Annotated[str, typer.Option("--browser", "-b", help="chrome | edge | brave")] = "chrome",
) -> None:
    """📋 One-pass read-only health report: extensions + registry + AV engine status."""
    from navig_antivirus.engine import browsers, registry_scan, system_scan
    c.info("── navig antivirus report ──")
    # extensions
    ud = browsers.user_data_dir(browser)
    if ud:
        findings, total, unique, nprof, unreadable = browsers.scan_extensions(ud, min_score=8)
        c.plain(f"Extensions: {total} installed / {unique} unique across {nprof} profiles; "
                f"{len(findings)} CRITICAL-risk"
                + (f"; ⚠ {unreadable} profile(s) UNREADABLE — scan INCOMPLETE" if unreadable else "")
                + ".")
        for f in findings[:8]:
            c.plain(f"   [{f.band} {f.score}] {f.name} ({', '.join(f.flags[:3])})")
    else:
        c.plain(f"Extensions: {browser} not found.")
    # registry
    rep = registry_scan.scan_registry()
    if rep.available:
        susp = [r for r in rep.force_installed if r.flags]
        gap = f"; {len(rep.unreadable)} location(s) unreadable — run elevated" if rep.unreadable else ""
        c.plain(f"Registry: {len(rep.force_installed)} forced extension(s), {len(susp)} suspicious; "
                f"blocklist={len(rep.blocklist)}{gap}.")
    # system AV
    st = system_scan.detect()
    c.plain(f"System AV: Defender={'yes' if st.defender else 'no'}, "
            f"Malwarebytes={'installed' if st.malwarebytes_installed else 'no'}"
            f"{' (no CLI)' if st.malwarebytes_installed and not st.malwarebytes_scriptable else ''}.")
    c.success("Report done. Run 'navig antivirus system --type quick' to actively scan the disk.")


def register() -> None:  # pragma: no cover — entry-point seam; the CLI verb auto-mounts
    return None
