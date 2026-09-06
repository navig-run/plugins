"""navig mstore -- Microsoft Store publishing verbs for NAVIG.

A standalone first-party plugin (lives in ``plugins/``, NOT in core). It mounts
as ``navig mstore`` via the ``navig.commands`` entry point -- no core edit. It is
deliberately thin: the real, verifiable work is the ``msstore-publish`` **block**
(one source of truth). ``navig mstore publish`` is ergonomic sugar over
``navig apply msstore-publish``.

Credentials (Azure AD service principal for ``msstore``) resolve from **explicit
NAVIG_MSSTORE_* env, then the NAVIG vault, then generic AZURE_* env** -- never
hardcoded. Generic ``AZURE_*`` is last on purpose: it is shared by az-cli /
Terraform / etc. and may be a *different* Azure app, so the deliberate vault creds
must win over it. The vault is read through the official
``navig vault get <provider>/<field> --raw`` seam (the one navig-echo uses), which
correctly handles the ``provider/data_key`` path form AND the credential
**profile** (``get_secret(label)`` defaults to profile ``default`` and would miss
connector-stored creds).

Vault source (first match wins): providers ``partner_center`` (the Microsoft
Partner Center App-Only credential from the navig-harbor connector) then ``azure``;
profiles ``connector`` then ``default``. Override the first-tried pair with
``NAVIG_MSSTORE_VAULT_PROVIDER`` / ``NAVIG_MSSTORE_VAULT_PROFILE``.

    field          env vars                                  vault path
    tenant_id      NAVIG_MSSTORE_TENANT_ID | AZURE_TENANT_ID   <provider>/tenant_id
    client_id      NAVIG_MSSTORE_CLIENT_ID | AZURE_CLIENT_ID   <provider>/client_id
    client_secret  NAVIG_MSSTORE_CLIENT_SECRET | AZURE_CLIENT_SECRET  <provider>/client_secret

Verbs:
    navig mstore auth      # resolve creds (env/vault) -> msstore reconfigure
    navig mstore creds     # what resolves + from where (never reveals a value)
    navig mstore publish -p <9P...> -k <pkg.msix>
    navig mstore status  -p <9P...>
    navig mstore addon   ...        # in-app products (DevCenter REST; see addons.py)
    navig mstore info
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import webbrowser
from pathlib import Path
from navig.core.proc_text import decode_console_result

import typer

__version__ = "0.6.0"

app = typer.Typer(
    name="mstore",
    help="Publish apps to the Microsoft Store (thin wrapper over the msstore-publish block).",
    no_args_is_help=True,
)

_FIELDS = ("tenant_id", "client_id", "client_secret")
# Explicit, this-tool-specific env -- a deliberate override, preferred over the vault.
_ENV_PRIMARY: dict[str, tuple[str, ...]] = {
    "tenant_id": ("NAVIG_MSSTORE_TENANT_ID",),
    "client_id": ("NAVIG_MSSTORE_CLIENT_ID",),
    "client_secret": ("NAVIG_MSSTORE_CLIENT_SECRET",),
}
# Generic Azure env -- LAST resort. AZURE_* is shared by az-cli / Terraform / etc.
# and may belong to a DIFFERENT Azure app, so the deliberate vault creds win over it.
_ENV_FALLBACK: dict[str, tuple[str, ...]] = {
    "tenant_id": ("AZURE_TENANT_ID",),
    "client_id": ("AZURE_CLIENT_ID",),
    "client_secret": ("AZURE_CLIENT_SECRET",),
}
# providers/profiles tried in order; an env override is tried first.
_PROVIDERS = ("partner_center", "azure")
_PROFILES = ("connector", "default")

_vault_src_cache: list[tuple[str, str] | None] = []  # memoize the resolved (provider, profile)


def _run(argv: list[str]) -> int:
    """Run a child process, streaming output; return its exit code (127 if missing)."""
    if shutil.which(argv[0]) is None:
        typer.secho(f"'{argv[0]}' not found on PATH.", fg=typer.colors.RED)
        return 127
    return subprocess.run(argv).returncode


def _run_in(argv: list[str], cwd: Path) -> int:
    """Like _run but with a working directory (for per-app npm/scripts)."""
    if shutil.which(argv[0]) is None:
        typer.secho(f"'{argv[0]}' not found on PATH.", fg=typer.colors.RED)
        return 127
    return subprocess.run(argv, cwd=str(cwd)).returncode


# ── App-target helpers (the plugin operates on any app dir via --dir) ──────────
def _app_root(dir_opt: str | None) -> Path:
    return Path(dir_opt).resolve() if dir_opt else Path.cwd()


def _read_identity(root: Path) -> dict:
    """Read <app>/store/identity.json (Store Product ID + MSIX identity).

    Degrades to {} for read-ONLY consumers (auth/package/publish just read). A
    read-MODIFY-write (configure) must use :func:`_read_identity_for_update` instead."""
    p = root / "store" / "identity.json"
    try:
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
    except Exception:  # noqa: BLE001
        return {}


class _IdentityUnreadable(RuntimeError):
    """identity.json exists but couldn't be read right now (e.g. a transient AV/backup
    sharing lock). A read-modify-write must ABORT, never overwrite good data with {}."""


def _read_identity_for_update(root: Path) -> dict:
    """Read identity.json for the read-MODIFY-write in `configure`.

    Missing → {} (first run). Genuinely CORRUPT → {} (already unrecoverable; a fresh
    configure may overwrite it). But an EXISTING file that is transiently unreadable
    RAISES :class:`_IdentityUnreadable` — so a lock can never let configure silently wipe
    the user's real Store identity with an empty {} (the "a failed READ must never become
    a destructive WRITE" class)."""
    p = root / "store" / "identity.json"
    if not p.exists():
        return {}
    try:
        text = p.read_text(encoding="utf-8")
    except OSError as exc:
        raise _IdentityUnreadable(f"{p} exists but is unreadable ({exc})") from exc
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def _write_identity(idf: Path, data: dict) -> None:
    """Atomically write identity.json (temp in the same dir + replace), so a crash mid-write
    can't truncate the user's Store identity into a corrupt/empty file."""
    idf.parent.mkdir(parents=True, exist_ok=True)
    tmp = idf.with_name(idf.name + ".navig-tmp")
    try:
        tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        tmp.replace(idf)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def _latest_msix(root: Path) -> Path | None:
    """The newest .msix anywhere under src-tauri/target.

    ``glob`` (non-recursive) looked ONLY at ``src-tauri/target/*.msix`` — but every
    ``create-msix.ps1`` in the fleet writes to
    ``src-tauri/target/release/bundle/msix/``. So ``publish`` could never find the
    package ``package`` had just built: it reported "No .msix (--package, or build
    one)" straight after a successful build, and the only way out was to pass
    ``--package`` by hand. ``rglob`` is what the layout has always required.
    """
    target = root / "src-tauri" / "target"
    if not target.exists():
        return None
    files = sorted(target.rglob("*.msix"), key=lambda p: p.stat().st_mtime, reverse=True)
    return files[0] if files else None


def _providers() -> list[str]:
    extra = os.environ.get("NAVIG_MSSTORE_VAULT_PROVIDER")
    return ([extra] if extra else []) + [p for p in _PROVIDERS if p != extra]


def _profiles() -> list[str]:
    extra = os.environ.get("NAVIG_MSSTORE_VAULT_PROFILE")
    return ([extra] if extra else []) + [p for p in _PROFILES if p != extra]


def _vault_get(path: str, profile: str, *, raw: bool):
    """Call `navig vault get`. Returns the CompletedProcess, or None if navig absent."""
    navig = shutil.which("navig")
    if navig is None:
        return None
    argv = [navig, "vault", "get", path, "--profile", profile] + (["--raw"] if raw else [])
    try:
        return subprocess.run(argv, capture_output=True, text=True, timeout=20)
    except Exception:  # noqa: BLE001
        return None


def _vault_has(path: str, profile: str) -> bool:
    """Masked existence probe -- never reveals the value. The masked output shows
    ``************`` for a present secret and nothing for a missing one, so the
    ``*`` marker is the signal (masked mode returns rc=0 either way)."""
    r = _vault_get(path, profile, raw=False)
    return r is not None and "*" in (r.stdout or "")


def _vault_source() -> tuple[str, str] | None:
    """Find the (provider, profile) that holds the creds -- probed once (masked, no
    reveal) with the secret field, then memoized."""
    if _vault_src_cache:
        return _vault_src_cache[0]
    found: tuple[str, str] | None = None
    for prov in _providers():
        for prof in _profiles():
            if _vault_has(f"{prov}/client_secret", prof):
                found = (prov, prof)
                break
        if found:
            break
    _vault_src_cache.append(found)
    return found


def _from_vault(field: str) -> str | None:
    """Reveal one field's value from the resolved vault source (for auth only)."""
    src = _vault_source()
    if src is None:
        return None
    prov, prof = src
    r = _vault_get(f"{prov}/{field}", prof, raw=True)
    if r is not None and r.returncode == 0 and (r.stdout or "").strip():
        return r.stdout.strip()
    return None


def _resolve_value(field: str) -> tuple[str | None, str]:
    """Real value for auth. Order: explicit env -> vault -> generic Azure env."""
    for env_var in _ENV_PRIMARY[field]:
        v = os.environ.get(env_var)
        if v:
            return v, f"env:{env_var}"
    v = _from_vault(field)
    if v:
        src = _vault_source()
        return v, (f"vault:{src[0]}/{field}@{src[1]}" if src else "vault")
    for env_var in _ENV_FALLBACK[field]:
        v = os.environ.get(env_var)
        if v:
            return v, f"env:{env_var} (generic)"
    return None, "missing"


def _resolve_source(field: str) -> tuple[bool, str]:
    """For `creds`: whether it resolves + from where, WITHOUT revealing the value."""
    for env_var in _ENV_PRIMARY[field]:
        if os.environ.get(env_var):
            return True, f"env:{env_var}"
    src = _vault_source()
    if src is not None:
        prov, prof = src
        # Confirm this field exists at the source -- masked, so `creds` never reveals.
        if _vault_has(f"{prov}/{field}", prof):
            return True, f"vault:{prov}/{field}@{prof}"
    for env_var in _ENV_FALLBACK[field]:
        if os.environ.get(env_var):
            return True, f"env:{env_var} (generic)"
    return False, "missing"


def _cred_help() -> None:
    typer.echo("Store the Azure AD service-principal creds in the vault (preferred) or env:")
    typer.echo("  navig vault set partner_center/tenant_id <id>      --profile connector")
    typer.echo("  navig vault set partner_center/client_id <id>      --profile connector")
    typer.echo("  navig vault set partner_center/client_secret <sec> --profile connector")
    typer.echo("  (or set NAVIG_MSSTORE_TENANT_ID / _CLIENT_ID / _CLIENT_SECRET)")


@app.command("auth")
def auth(
    dry_run: bool = typer.Option(False, "--dry-run", help="Show what would run (secret redacted)."),
) -> None:
    """Configure the `msstore` CLI from credentials in the vault (or env)."""
    resolved = {name: _resolve_value(name) for name in _FIELDS}
    missing = [name for name, (val, _) in resolved.items() if not val]
    if missing:
        typer.secho("Missing credential(s): " + ", ".join(missing), fg=typer.colors.RED)
        _cred_help()
        raise typer.Exit(1)

    tenant, client, secret = (resolved[f][0] for f in _FIELDS)

    if dry_run:
        typer.echo("would run:  msstore reconfigure --tenantId <t> --clientId <c> --clientSecret ***")
        for name, (_, source) in resolved.items():
            typer.echo(f"  {name:14} from {source}")
        raise typer.Exit(0)

    if shutil.which("msstore") is None:
        typer.secho(
            "'msstore' not found. Install: dotnet tool install --global MSStore.CLI",
            fg=typer.colors.RED,
        )
        raise typer.Exit(127)

    # msstore takes --clientSecret as an argument (its design), so the secret is in
    # this ONE child process's argv. We read it from the vault and never log it.
    rc = subprocess.run(
        ["msstore", "reconfigure", "--tenantId", tenant, "--clientId", client,
         "--clientSecret", secret]
    ).returncode
    if rc == 0:
        typer.secho("msstore configured.", fg=typer.colors.GREEN)
    raise typer.Exit(rc)


@app.command("creds")
def creds() -> None:
    """Show which Store credentials resolve, and from where (values never shown)."""
    typer.echo("Microsoft Store credentials (NAVIG_MSSTORE_* env -> vault -> AZURE_* env):\n")
    any_missing = False
    for name in _FIELDS:
        ok, source = _resolve_source(name)
        if ok:
            typer.echo(f"  {name:14} {source}")
        else:
            any_missing = True
            typer.secho(f"  {name:14} missing", fg=typer.colors.YELLOW)
    typer.echo("")
    if any_missing:
        _cred_help()
    else:
        typer.secho("All set -- run: navig mstore auth", fg=typer.colors.GREEN)


@app.command("configure")
def configure(
    dir_: str = typer.Option(None, "--dir", "-C", help="App directory (default: cwd)."),
) -> None:
    """Write the Store identity (store/identity.json) for an app -- interactive."""
    root = _app_root(dir_)
    idf = root / "store" / "identity.json"
    # Read-guard: if an EXISTING identity.json is transiently unreadable (AV/backup lock),
    # abort — proceeding would show empty defaults and silently wipe the fields the user
    # doesn't re-type. (Missing/corrupt → {} is fine.)
    try:
        data = _read_identity_for_update(root)
    except _IdentityUnreadable as exc:
        typer.secho(f"{exc} — not overwriting; try again in a moment.", fg=typer.colors.RED)
        raise typer.Exit(2) from exc
    typer.echo("Enter values from Partner Center (blank = keep current).")
    for label, key in (
        ("Store Product ID (9P...)", "storeProductId"),
        ("Package Identity Name", "packageIdentityName"),
        ("Publisher CN (CN=...)", "publisherCN"),
        ("Version (blank = from tauri.conf.json)", "version"),
    ):
        cur = data.get(key, "")
        v = typer.prompt(label, default=cur, show_default=bool(cur)).strip()
        if v:
            data[key] = v
    _write_identity(idf, data)
    typer.secho(f"Saved {idf} (gitignored)", fg=typer.colors.GREEN)


@app.command("package")
def package_cmd(
    dir_: str = typer.Option(None, "--dir", "-C", help="App directory (default: cwd)."),
    build: bool = typer.Option(False, "--build", help="Run `npm run tauri:build` first."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Validate + stage; don't pack."),
) -> None:
    """Build the MSIX by driving the app's scripts/create-msix.ps1."""
    root = _app_root(dir_)
    script = root / "scripts" / "create-msix.ps1"
    if not script.exists():
        typer.secho(
            f"No packaging script at {script} (the app needs scripts/create-msix.ps1).",
            fg=typer.colors.RED,
        )
        raise typer.Exit(1)
    if build and not dry_run:
        rc = _run_in(["npm", "run", "tauri:build"], root)
        if rc != 0:
            raise typer.Exit(rc)
    argv = ["powershell", "-ExecutionPolicy", "Bypass", "-File", str(script)]
    if dry_run:
        argv.append("-DryRun")
    raise typer.Exit(_run(argv))


@app.command("publish")
def publish(
    product_id: str = typer.Option(None, "--product-id", "-p", help="Store Product ID (default: store/identity.json)."),
    package: Path = typer.Option(
        None, "--package", "-k", exists=True, dir_okay=False,
        help="MSIX to publish (default: latest under src-tauri/target).",
    ),
    dir_: str = typer.Option(None, "--dir", "-C", help="App directory (default: cwd)."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Show the plan; publish nothing."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip the confirmation prompt."),
) -> None:
    """Upload + commit a Store submission by applying the msstore-publish block.

    Resolves product_id + package from store/identity.json + src-tauri/target when
    omitted. Run `navig mstore auth` first so `msstore` is authenticated.
    """
    root = _app_root(dir_)
    ident = _read_identity(root)
    pid = product_id or ident.get("storeProductId")
    pkg = package or _latest_msix(root)
    if not pid:
        typer.secho(
            "No Store Product ID (--product-id or store/identity.json). Run: navig mstore configure",
            fg=typer.colors.RED,
        )
        raise typer.Exit(1)
    if not pkg:
        typer.secho(
            "No .msix (--package, or build one: navig mstore package --build).",
            fg=typer.colors.RED,
        )
        raise typer.Exit(1)
    argv = [
        "navig", "apply", "msstore-publish",
        "--input", f"product_id={pid}",
        "--input", f"package_path={pkg}",
    ]
    if dry_run:
        argv.append("--dry-run")
    else:
        if not yes and not typer.confirm(
            f"Publish {Path(pkg).name} to Store product {pid}?", default=False
        ):
            typer.echo("Cancelled.")
            raise typer.Exit(0)
        argv += ["--approve", "publish"]
    raise typer.Exit(_run(argv))


@app.command("status")
def status(
    product_id: str = typer.Option(None, "--product-id", "-p", help="Store Product ID (default: store/identity.json)."),
    dir_: str = typer.Option(None, "--dir", "-C", help="App directory (default: cwd)."),
) -> None:
    """Show the current submission status via the Microsoft Store Developer CLI."""
    pid = product_id or _read_identity(_app_root(dir_)).get("storeProductId")
    if not pid:
        typer.secho("No Store Product ID (--product-id or store/identity.json).", fg=typer.colors.RED)
        raise typer.Exit(1)
    raise typer.Exit(_run(["msstore", "submission", "status", pid]))


@app.command("open")
def open_dashboard() -> None:
    """Open the Microsoft Partner Center dashboard in a browser."""
    url = "https://partner.microsoft.com/dashboard"
    try:
        webbrowser.open(url)
    except Exception:  # noqa: BLE001
        pass
    typer.echo(url)


@app.command("show")
def show(
    dir_: str = typer.Option(None, "--dir", "-C", help="App directory (default: cwd)."),
) -> None:
    """Show an app's Store identity + latest built MSIX."""
    root = _app_root(dir_)
    ident = _read_identity(root)
    msix = _latest_msix(root)
    typer.echo(f"App: {root}")
    for key, lbl in (
        ("storeProductId", "Store Product ID"),
        ("packageIdentityName", "Package Identity"),
        ("publisherCN", "Publisher CN"),
        ("version", "Version"),
    ):
        typer.echo(f"  {lbl:18} {ident.get(key) or '(not set)'}")
    typer.echo(f"  {'Built MSIX':18} {msix.name if msix else '(none)'}")


@app.command("info")
def info() -> None:
    """Explain how `navig mstore` relates to the menu and the block."""
    typer.echo(
        "navig mstore -- the full Microsoft Store toolkit (a PLUGIN, not core).\n"
        "  Block  (msstore-publish): the portable, verifiable publish primitive.\n"
        "  Plugin (navig mstore):    the operator surface -> wraps the block + workers.\n"
        "  Menu   (npm run menu):    a thin interactive front-end over these verbs.\n"
        "\n"
        "Workflow (every verb accepts --dir <app>, default cwd):\n"
        "  configure  write store/identity.json (Product ID + MSIX identity)\n"
        "  auth       resolve creds -> msstore reconfigure\n"
        "  creds      what resolves + from where (never reveals a value)\n"
        "  package    build the MSIX (--build tauri-builds first, --dry-run stages)\n"
        "  publish    apply the msstore-publish block (auto product_id + latest .msix)\n"
        "  status     msstore submission status\n"
        "  show       identity + latest built MSIX\n"
        "  open       Partner Center dashboard\n"
        "  doctor     preflight the whole chain (tooling, creds, block, identity)\n"
        "  addon      in-app products: list/create/show/price-probe/submit/status/delete\n"
        "             (the `msstore` CLI cannot do add-ons -- these speak DevCenter REST)\n"
        "\n"
        "Credentials: explicit NAVIG_MSSTORE_* env -> vault (partner_center@connector)\n"
        "-> generic AZURE_* env (last, so an unrelated Azure app can't shadow the vault).\n"
    )


def _block_available() -> bool:
    """True if the msstore-publish block resolves (`navig block show` exit 0)."""
    navig = shutil.which("navig")
    if navig is None:
        return False
    try:
        r = decode_console_result(subprocess.run(
            [navig, "block", "show", "msstore-publish"],
            capture_output=True, timeout=30,
        ))
        return r.returncode == 0
    except Exception:  # noqa: BLE001
        return False


def _has_makeappx() -> bool:
    """True if MakeAppx.exe (Windows SDK) is on PATH or under Windows Kits."""
    if shutil.which("MakeAppx.exe") or shutil.which("makeappx"):
        return True
    import glob

    for root in (os.environ.get("ProgramFiles(x86)", ""), os.environ.get("ProgramFiles", "")):
        if root and glob.glob(os.path.join(root, "Windows Kits", "10", "bin", "*", "*", "makeappx.exe")):
            return True
    return False


@app.command("doctor")
def doctor(
    dir_: str = typer.Option(None, "--dir", "-C", help="Also check an app's identity + packaging."),
) -> None:
    """Preflight the whole Store chain: tooling, credentials, block, and (per app) readiness."""
    failed = False

    def check(label: str, ok: bool, hint: str = "", *, warn: bool = False) -> None:
        nonlocal failed
        if ok:
            typer.secho(f"  [ ok ] {label}", fg=typer.colors.GREEN)
        elif warn:
            tail = f"  -- {hint}" if hint else ""
            typer.secho(f"  [warn] {label}{tail}", fg=typer.colors.YELLOW)
        else:
            failed = True
            tail = f"  -- {hint}" if hint else ""
            typer.secho(f"  [FAIL] {label}{tail}", fg=typer.colors.RED)

    typer.echo("navig mstore doctor\n")

    check("navig CLI on PATH", shutil.which("navig") is not None)
    check(
        "msstore-publish block installed", _block_available(),
        "navig install add block:navig-run/community/blocks/msstore-publish",
    )
    check(
        "msstore CLI installed", shutil.which("msstore") is not None,
        "dotnet tool install --global MSStore.CLI",
    )
    check(
        "MakeAppx (Windows SDK) present", _has_makeappx(),
        "install the Windows 10/11 SDK (needed to build the MSIX)",
    )
    missing = [f for f in _FIELDS if not _resolve_source(f)[0]]
    check(
        "Store credentials resolve (tenant/client/secret)", not missing,
        (f"missing: {', '.join(missing)} — see `navig mstore creds`") if missing else "",
    )

    if dir_ is not None:
        root = _app_root(dir_)
        ident = _read_identity(root)
        check(
            f"Store identity set ({root.name})", bool(ident.get("storeProductId")),
            f"navig mstore configure --dir {root}",
        )
        script = root / "scripts" / "create-msix.ps1"
        check(
            f"packaging script present ({root.name})", script.exists(),
            "the app needs scripts/create-msix.ps1",
        )
        check(
            f"built MSIX present ({root.name})", _latest_msix(root) is not None,
            f"navig mstore package --dir {root} --build", warn=True,
        )
    else:
        typer.secho("  [info] pass --dir <app> to also check identity + packaging", fg=typer.colors.CYAN)

    typer.echo("")
    if failed:
        typer.secho("Some checks failed — fix the [FAIL] items above.", fg=typer.colors.RED)
        raise typer.Exit(1)
    typer.secho("All hard checks passed.", fg=typer.colors.GREEN)


# Add-on (in-app product) verbs live in their own module: the `msstore` CLI has no
# add-on support, so they speak the DevCenter REST API directly. Mounted last so the
# deferred import in addons._resolve_creds cannot create a cycle at import time.
from navig_msstore.addons import addon_app  # noqa: E402

app.add_typer(addon_app)


def register() -> None:  # pragma: no cover - entry-point seam (routes/hooks); CLI verbs auto-mount
    return None


def main() -> None:  # console-script convenience
    sys.exit(app())
