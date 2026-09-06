"""
navig github — the full GitHub toolbox, wired natively into navig.

The GitHub engine (`navig_github.engine`) is bundled and runs
**in-process**: every engine command is mounted as a `navig github`
subcommand (backup, exports, analytics, scheduling …), plus navig's own
curated wrappers (`backup`, `search`, `clone`) and vault-backed `token`
management. A group callback injects the resolved token into GITHUB_TOKEN,
which every engine command reads via its `envvar` — so the whole surface is
vault-wired with zero per-command glue.

Token resolution order:
  1. navig vault  (secret name: github_token)
  2. GITHUB_TOKEN env var
  3. ~/.navig/config.yaml  → github.token
  4. No token → public repos only (curated `clone` falls back to plain git)
"""

import os
import subprocess
from pathlib import Path
from typing import Annotated

import typer

from navig.core.yaml_io import load_yaml_for_update, safe_load_yaml
from navig.lazy_loader import lazy_import
from navig.platform.paths import config_dir

ch = lazy_import("navig.console_helper")

github_app = typer.Typer(
    name="github",
    help="🐙 GitHub: backup · mirror · export · manage (full GitHub engine, vault-wired)",
    no_args_is_help=True,
)


@github_app.callback()
def _github_root(ctx: typer.Context) -> None:
    # Vault → GITHUB_TOKEN before any subcommand runs: every mounted engine
    # command declares envvar="GITHUB_TOKEN", so this one hook authenticates
    # the entire surface. An explicitly exported GITHUB_TOKEN always wins.
    if not os.environ.get("GITHUB_TOKEN"):
        tok = _resolve_github_token()
        if tok:
            os.environ["GITHUB_TOKEN"] = tok

# ---------------------------------------------------------------------------
# Token helpers
# ---------------------------------------------------------------------------


def _resolve_github_token() -> str | None:
    """
    Resolve a GitHub token with the full navig credential chain.

    Priority:
      1. navig vault  (secret name: github_token)
      2. GITHUB_TOKEN environment variable
      3. ~/.navig/config.yaml → github.token

    Returns None if not found anywhere.
    """
    # 1 — navig vault
    try:
        from navig.vault import get_vault  # type: ignore

        vault = get_vault()
        secret = vault.get("github_token", caller="navig-github")
        if secret is not None:
            # Credential exposes the decrypted payload via `.data` / `.get_secret()`
            # — there is NO `.value` attribute. The token is stored under "value"
            # (also accept "token"/"api_key" for older entries).
            tok = None
            data = getattr(secret, "data", None)
            if isinstance(data, dict):
                tok = data.get("value") or data.get("token") or data.get("api_key")
            if not tok and hasattr(secret, "get_secret"):
                tok = secret.get_secret("value") or secret.get_secret("token")
            if not tok:
                tok = getattr(secret, "value", None)
            if tok:
                return str(tok)
    except Exception:  # noqa: BLE001
        pass  # best-effort; failure is non-critical

    # 2 — env var
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    if token:
        return token

    # 3 — ~/.navig/config.yaml
    try:
        cfg_path = config_dir() / "config.yaml"
        cfg = safe_load_yaml(cfg_path) or {}
        token = cfg.get("github", {}).get("token", "").strip()
        if token:
            return token
    except Exception:  # noqa: BLE001
        pass  # best-effort; failure is non-critical

    return None


def _engine_available() -> bool:
    """Return True if the bundled GitHub engine is importable."""
    try:
        import navig_github.engine  # noqa: F401

        return True
    except ImportError:
        return False


def _require_engine() -> bool:
    """
    Ensure the GitHub engine is importable; print help if missing.
    Returns True if available.
    """
    if _engine_available():
        return True
    ch.error(
        "the navig-github engine is missing — reinstall the plugin:\n"
        "  pip install navig-github"
    )
    return False


def _run_engine(args: list[str], token: str | None) -> None:
    """Dispatch into the bundled GitHub engine's CLI **in-process** (no subprocess).

    An explicit *token* (from a --token flag) wins over whatever the group
    callback already injected into GITHUB_TOKEN.
    """
    if not _require_engine():
        raise typer.Exit(1)
    if token:
        os.environ["GITHUB_TOKEN"] = token

    import click
    from navig_github.engine.cli import app as engine_cli
    from typer.main import get_command

    cmd = get_command(engine_cli)
    try:
        cmd.main(list(args), prog_name="navig github", standalone_mode=False)
    except click.exceptions.Exit as exc:  # typer.Exit inside the engine lands here
        code = exc.exit_code or 0
        if code:
            raise typer.Exit(code) from None
    except click.exceptions.Abort:
        ch.warning("Aborted.")
        raise typer.Exit(1) from None
    except click.ClickException as exc:
        exc.show()
        raise typer.Exit(exc.exit_code) from None
    except KeyboardInterrupt:
        pass  # user interrupted; clean exit
    except SystemExit as exc:  # defensive: bare sys.exit() inside the engine
        code = exc.code if isinstance(exc.code, int) else 1
        if code:
            raise typer.Exit(code) from None


# ---------------------------------------------------------------------------
# Fallback: navig-native git clone (no token required)
# ---------------------------------------------------------------------------


def _fallback_git_clone(repo_url: str, dest: str) -> None:
    """
    Minimal git-based clone used when no GitHub token is available.
    Leverages the git binary already required by navig.
    """
    ch.warning("No GitHub token found — using plain `git clone` (public repos only).")
    dest_path = Path(dest)
    dest_path.mkdir(parents=True, exist_ok=True)

    repo_name = repo_url.rstrip("/").split("/")[-1].removesuffix(".git")
    clone_dest = dest_path / repo_name

    if clone_dest.exists():
        ch.info(f"Updating existing repo at {clone_dest} …")
        result = subprocess.run(
            ["git", "-C", str(clone_dest), "pull", "--ff-only"],
            capture_output=True,
            text=True, encoding="utf-8", errors="replace",
        )
    else:
        ch.info(f"Cloning {repo_url} → {clone_dest} …")
        result = subprocess.run(
            ["git", "clone", repo_url, str(clone_dest)], capture_output=True, text=True, encoding="utf-8", errors="replace"
        )

    if result.returncode == 0:
        ch.success(f"Done: {clone_dest}")
    else:
        ch.error(result.stderr.strip() or "git clone failed")
        raise typer.Exit(1)


# ---------------------------------------------------------------------------
# sub-commands
# ---------------------------------------------------------------------------


@github_app.command("search", rich_help_panel="Backup & mirror")
def gh_search(
    query: Annotated[str, typer.Argument(help="Search keyword (e.g. 'agent soul')")],
    output_dir: Annotated[
        Path | None, typer.Option("--output-dir", "-o", help="Destination directory")
    ] = None,
    limit: Annotated[
        int, typer.Option("--limit", "-l", min=1, max=100, help="Max repos to clone")
    ] = 20,
    language: Annotated[str | None, typer.Option("--language", help="Filter by language")] = None,
    min_stars: Annotated[int | None, typer.Option("--min-stars", help="Minimum star count")] = None,
    sort: Annotated[
        str, typer.Option("--sort", help="Sort: stars|forks|updated|best-match")
    ] = "stars",
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Auto-confirm, no prompt")] = False,
    workers: Annotated[int, typer.Option("--workers", "-w", help="Parallel clone workers")] = 4,
    token: Annotated[
        str | None,
        typer.Option("--token", "-t", help="GitHub token (overrides auto-resolve)"),
    ] = None,
    flat: Annotated[
        bool,
        typer.Option(
            "--flat/--no-flat",
            help="Clone FLAT (<dir>/<repo>, collisions suffixed -<owner>) vs nested by owner",
        ),
    ] = True,
):
    """
    🔍 Search GitHub and clone matching repositories.

    Token resolution: vault → GITHUB_TOKEN env → ~/.navig/config.yaml.
    Without a token only public repos at low rate limits are accessible.

    Downloads are FLAT by default (repos land directly as <dir>/<repo>, so a
    second run updates them in place); use --no-flat for the owner-nested layout.

    Examples:
        navig github search "agent soul" -o ./mirrors --limit 50 -y
        navig github search "machine learning" --language python --min-stars 500
        navig github search "cli tools" -o ./tools --no-flat   # nested by owner
    """
    resolved_token = token or _resolve_github_token()

    if not resolved_token:
        ch.warning(
            "No GitHub token found. Using unauthenticated access (60 req/hour).\n"
            "Set one with:  navig github token set <your-token>"
        )

    if not _require_engine():
        raise typer.Exit(1)

    args = [
        "search",
        query,
        "--limit",
        str(limit),
        "--sort",
        sort,
        "--workers",
        str(workers),
    ]
    if output_dir:
        args += ["--output-dir", str(output_dir)]
    if language:
        args += ["--language", language]
    if min_stars is not None:
        args += ["--min-stars", str(min_stars)]
    if yes:
        args += ["--yes"]
    if flat:
        args += ["--flat-structure"]

    _run_engine(args, resolved_token)


@github_app.command("backup", rich_help_panel="Backup & mirror")
def gh_backup(
    target: Annotated[str, typer.Argument(help="GitHub username (or org with --org) to back up")],
    dest: Annotated[Path | None, typer.Option("--dest", "-d", help="Destination directory")] = None,
    visibility: Annotated[str, typer.Option("--visibility", help="all|public|private")] = "all",
    org: Annotated[bool, typer.Option("--org", help="Treat target as an organisation, not a user")] = False,
    workers: Annotated[int, typer.Option("--workers", "-w", help="Parallel clone workers")] = 4,
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Auto-confirm")] = False,
    flat: Annotated[
        bool,
        typer.Option("--flat/--no-flat", help="Clone FLAT (<dir>/<repo>) vs nested by visibility/owner"),
    ] = True,
    token: Annotated[str | None, typer.Option("--token", "-t", help="GitHub token")] = None,
):
    """
    📦 Clone / mirror every repo for a user (or --org organisation).

    Downloads are FLAT by default (<dir>/<repo>, updates in place); --no-flat
    keeps the repos/<visibility>/<owner> layout.

    Examples:
        navig github backup myuser -d ./mirrors -y
        navig github backup myorg --org --visibility public
        navig github backup myuser --no-flat       # nested by visibility/owner
    """
    resolved_token = token or _resolve_github_token()

    if not resolved_token:
        ch.warning(
            "No GitHub token found — only public repos accessible.\n"
            "Set a token with:  navig github token set <your-token>"
        )

    if not _require_engine():
        raise typer.Exit(1)

    # the engine exposes per-target commands `user` / `org` (there is no `backup`).
    # 0.11 flags: concurrency is --max-workers; user/org don't prompt (no --yes).
    # Full engine surface (incremental, --include-issues, --bare, --lfs …) is
    # available directly:  navig github user|org <name> --help
    subcommand = "org" if org else "user"
    args = [subcommand, target, "--visibility", visibility, "--max-workers", str(workers)]
    if dest:
        args += ["--dest", str(dest)]
    if flat:
        args += ["--flat"]

    _run_engine(args, resolved_token)


@github_app.command("clone", rich_help_panel="Backup & mirror")
def gh_clone(
    repo: Annotated[str, typer.Argument(help="owner/repo or full GitHub URL")],
    dest: Annotated[Path | None, typer.Option("--dest", "-d", help="Destination directory")] = None,
    token: Annotated[str | None, typer.Option("--token", "-t", help="GitHub token")] = None,
):
    """
    ⬇️  Clone a single repository (with the full toolbox if available, else plain git).

    Always FLAT — lands directly as <dest>/<repo> (like `git clone`). Falls back
    to a plain `git clone` when no token is configured and the full toolbox is unavailable.

    Examples:
        navig github clone owner/my-repo -d ./mirrors
        navig github clone https://github.com/owner/repo
    """
    resolved_token = token or _resolve_github_token()

    # Normalise URL
    if not repo.startswith("http") and "/" in repo:
        repo_url = f"https://github.com/{repo}"
    else:
        repo_url = repo

    if not _engine_available():
        # Graceful fallback: plain git clone (always flat into dest/<repo>).
        _fallback_git_clone(repo_url, str(dest or Path.cwd()))
        return

    # `engine clone <repo> [dest]` — dest is a POSITIONAL arg, and the clone is
    # already flat (lands as <dest>/<repo>), so no extra flags needed.
    args = ["clone", repo_url]
    if dest:
        args += [str(dest)]

    _run_engine(args, resolved_token)


# ---------------------------------------------------------------------------
# Token management sub-commands
# ---------------------------------------------------------------------------

token_app = typer.Typer(
    name="token",
    help="Manage the GitHub token used by the GitHub engine",
    no_args_is_help=True,
)
github_app.add_typer(token_app, name="token", rich_help_panel="Setup")


@token_app.command("set")
def token_set(
    value: Annotated[str, typer.Argument(help="GitHub Personal Access Token")],
    use_vault: Annotated[
        bool, typer.Option("--vault/--no-vault", help="Store in navig vault (default)")
    ] = True,
):
    """
    🔑 Save a GitHub token for navig github to use automatically.

    Stores in the navig vault by default (preferred). Falls back to
    writing to ~/.navig/config.yaml when the vault is unavailable.

    Examples:
        navig github token set ghp_xxxxxxxxxxxxxxxx
    """
    if use_vault:
        try:
            from navig.vault import get_vault  # type: ignore

            vault = get_vault()
            existing = vault.get("github_token", caller="navig-github.token_set")
            if existing:
                vault.update(existing.id, data={"value": value})
                ch.success("GitHub token updated in navig vault.")
            else:
                vault.add(
                    provider="github_token",
                    credential_type="token",
                    data={"value": value},
                    metadata={"description": "GitHub PAT used by the GitHub engine"},
                )
                ch.success("GitHub token saved to navig vault.")
            return
        except Exception as exc:
            ch.warning(f"Vault unavailable ({exc}), falling back to config file.")

    # Fallback: write to ~/.navig/config.yaml
    try:
        cfg_path = config_dir() / "config.yaml"
        cfg_path.parent.mkdir(parents=True, exist_ok=True)
        cfg = load_yaml_for_update(cfg_path)
        cfg.setdefault("github", {})["token"] = value
        from navig.core.yaml_io import atomic_write_yaml

        atomic_write_yaml(cfg, cfg_path)
        ch.success(f"GitHub token saved to {cfg_path}")
    except Exception as exc:
        ch.error(f"Failed to save token: {exc}")
        raise typer.Exit(1) from exc


@token_app.command("show")
def token_show():
    """
    🔍 Display where the current GitHub token comes from (masked).

    Examples:
        navig github token show
    """
    token = _resolve_github_token()
    if token:
        masked = token[:6] + "..." + token[-4:]
        ch.success(f"GitHub token found: {masked}")
        # Identify source
        try:
            from navig.vault import get_vault  # type: ignore

            v = get_vault()
            s = v.get("github_token", caller="navig-github.token_show")
            if s and getattr(s, "value", None) == token:
                ch.info("Source: navig vault")
                return
        except Exception:  # noqa: BLE001
            pass  # best-effort; failure is non-critical
        if os.environ.get("GITHUB_TOKEN", "").strip() == token:
            ch.info("Source: GITHUB_TOKEN env var")
        else:
            ch.info("Source: ~/.navig/config.yaml")
    else:
        ch.warning(
            "No GitHub token configured.\n"
            "Set one with:  navig github token set <your-token>\n"
            "Without a token, navig github only accesses public repos at low rate limits."
        )


@token_app.command("remove")
def token_remove():
    """
    🗑  Remove the stored GitHub token.

    Examples:
        navig github token remove
    """
    removed = False

    # Vault
    try:
        from navig.vault import get_vault  # type: ignore

        vault = get_vault()
        secret = vault.get("github_token", caller="navig-github.token_remove")
        if secret:
            vault.delete(secret.id)
            ch.success("GitHub token removed from navig vault.")
            removed = True
    except Exception:  # noqa: BLE001
        pass  # best-effort; failure is non-critical

    # Config file
    try:
        cfg_path = config_dir() / "config.yaml"
        if cfg_path.exists():
            cfg = load_yaml_for_update(cfg_path)
            if cfg.get("github", {}).get("token"):
                cfg["github"].pop("token")
                from navig.core.yaml_io import atomic_write_yaml

                atomic_write_yaml(cfg, cfg_path)
                ch.success(f"GitHub token removed from {cfg_path}")
                removed = True
    except Exception:  # noqa: BLE001
        pass  # best-effort; failure is non-critical

    if not removed:
        ch.info("No stored GitHub token found.")


@github_app.command("status", rich_help_panel="Setup")
def gh_status():
    """
    ℹ️  Show engine installation status and token configuration.

    Examples:
        navig github status
    """
    # engine availability (bundled engine — a hard dependency of navig-github)
    engine_missing = False
    if _engine_available():
        try:
            import navig_github.engine as fm  # type: ignore

            version = getattr(fm, "__version__", "unknown")
            ch.success(f"GitHub engine bundled — version {version} (in-process)")
        except Exception:
            ch.success("GitHub engine bundled (in-process)")
    else:
        ch.error("GitHub engine MISSING (it is a dependency of navig-github).")
        ch.info("Reinstall the plugin:  pip install navig-github")
        engine_missing = True
        
    # Token check
    token = _resolve_github_token()
    if token:
        masked = token[:6] + "..." + token[-4:]
        ch.success(f"GitHub token configured: {masked}")
    else:
        ch.warning(
            "No GitHub token — only public repos accessible at low rate limits.\n"
            "Set one with:  navig github token set <your-token>"
        )

    if engine_missing:
        # The engine is a hard dependency, so this is a BROKEN install, not a
        # state the operator can be in on purpose. Raised at the end so the token
        # check above still prints — a status command should show everything it
        # knows before it exits.
        raise typer.Exit(1)


# ---------------------------------------------------------------------------
# Full engine mount — every engine command becomes `navig github <command>`
# ---------------------------------------------------------------------------

# Curated navig-side names that must not be shadowed by the engine's versions
# (`search`/`clone` keep navig's friendlier flags + git fallback; `backup`
# wraps user/org; `token`/`status` are navig-only).
_CURATED_CMDS = {"search", "backup", "clone", "token", "status"}

# --help organization for the mounted engine commands.
_ENGINE_PANELS: dict[str, str] = {
    # backup & mirror
    "user": "Backup & mirror", "org": "Backup & mirror", "repo": "Backup & mirror",
    "starred": "Backup & mirror", "watched": "Backup & mirror", "gists": "Backup & mirror",
    # exports
    "issues": "Export", "pulls": "Export", "releases": "Export", "wiki": "Export",
    "labels": "Export", "milestones": "Export", "workflows": "Export",
    "attachments": "Export", "discussions": "Export", "followers": "Export",
    "webhooks": "Export", "secrets": "Export", "profile": "Export",
    # integrity & analytics
    "verify": "Integrity & analytics", "diff": "Integrity & analytics",
    "snapshot": "Integrity & analytics", "analytics": "Integrity & analytics",
    "analytics-history": "Integrity & analytics",
    # automation & config
    "schedule-add": "Automation", "schedule-list": "Automation",
    "schedule-remove": "Automation", "schedule-run": "Automation",
    "notify-test": "Automation", "notify-status": "Automation",
    "config-save": "Automation", "config-load": "Automation",
    "config-list": "Automation", "config-delete": "Automation",
    "config-export": "Automation", "config-import": "Automation",
    "templates": "Automation", "template-show": "Automation",
    "template-use": "Automation", "template-create": "Automation",
    "template-delete": "Automation",
    # destructive remote operations
    "delete": "Danger zone (remote)", "transfer": "Danger zone (remote)",
}


def _mount_engine_commands() -> None:
    """Mount every engine command (minus curated overrides) into `navig github`.

    In-process: the engine's Typer CommandInfo objects are attached directly, so
    `navig github issues owner/repo` runs the engine's own implementation. The
    group callback above has already placed the vault token in GITHUB_TOKEN,
    which each engine command reads via envvar. Import failure degrades to the
    curated commands only (never blocks the CLI).
    """
    try:
        from navig_github.engine.cli import app as engine_cli
    except Exception:  # noqa: BLE001 — degraded, never blocks boot
        return
    existing = {
        (c.name or (c.callback.__name__.replace("_", "-") if c.callback else ""))
        for c in github_app.registered_commands
    }
    for info in engine_cli.registered_commands:
        name = info.name or (info.callback.__name__.replace("_", "-") if info.callback else "")
        if not name or name in _CURATED_CMDS or name in existing:
            continue
        # unset = typer's DefaultPlaceholder, not None — only respect real strings
        if not isinstance(getattr(info, "rich_help_panel", None), str):
            info.rich_help_panel = _ENGINE_PANELS.get(name, "More (engine)")
        github_app.registered_commands.append(info)


_mount_engine_commands()
