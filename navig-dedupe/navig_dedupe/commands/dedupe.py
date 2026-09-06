"""``navig dedupe`` / ``navig-dedupe`` — find, quarantine & restore duplicate media and files.

One Typer app (`dedupe_app`) drives all four engines. Non-destructive by default:
a bare ``scan`` only reports; ``--move <dir>`` quarantines the extras (move, never
delete) and records a restore manifest, so ``dedupe restore <dir>`` fully undoes it.
Uses plain typer + rich so it never imports navig — the same app is mounted as the
``navig dedupe`` verb and shipped as the standalone ``navig-dedupe``/``ndup`` scripts.

Engines are imported lazily inside the commands so ``--help`` stays instant and the
numpy/Pillow/ffmpeg/fpcalc cost is only paid when a scan actually runs.
"""
from __future__ import annotations

import json as _json
import shutil
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.table import Table

dedupe_app = typer.Typer(
    name="dedupe",
    help="🧹 Find, quarantine & restore duplicate photos, video, audio & files — non-destructive.",
    no_args_is_help=True,
    add_completion=False,
)

_console = Console()

# The manifest a `scan --move` writes into the quarantine dir so `restore` can undo it.
_MANIFEST = ".dedupe-restore.json"

# A normalized cluster is: {"mode", "keep", "drop": [rel...], "reclaim": bytes}


def _fmt_bytes(n: int) -> str:
    f = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if f < 1024 or unit == "TB":
            return f"{f:.0f} {unit}" if unit == "B" else f"{f:.1f} {unit}"
        f /= 1024
    return f"{f:.1f} TB"


def _size(root: Path, rel: str) -> int:
    p = root / rel
    try:
        return p.stat().st_size if p.exists() else 0
    except OSError:
        return 0


def _largest(root: Path, names: list[str]) -> str:
    return max(names, key=lambda n: _size(root, n))


def _norm(root: Path, mode: str, keep: Optional[str], drop: list[str]) -> dict:
    return {"mode": mode, "keep": keep, "drop": drop,
            "reclaim": sum(_size(root, n) for n in drop)}


def _rel_under(rel: str, base: Optional[str]) -> bool:
    """True if *rel* is the *base* dir or lives inside it (both root-relative, posix-ish)."""
    if not base:
        return False
    r, b = Path(rel), Path(base)
    return r == b or b in r.parents


def _finalize(root: Path, clusters: list[dict], exclude_rel: Optional[str]) -> list[dict]:
    """Cross-mode overlap resolution + quarantine-dir exclusion.

    Each file is claimed exactly once (as keep or drop). When several modes surface the
    same file (e.g. an exact-duplicate photo caught by BOTH `--images` and `--files`),
    only the first cluster keeps it — later clusters lose the already-claimed drop, so
    counts aren't doubled and `_quarantine` never tries to move it twice.

    Files under *exclude_rel* (the quarantine dir, when it sits inside the scanned root)
    are stripped from every cluster — as drop AND as keep — so a repeat `--move` run
    never re-quarantines what it already moved, and never mistakes an already-quarantined
    file for the "keeper" and pushes the live original out. A synthetic cluster with
    ``keep is None`` (redundant thumbnails) keeps all its non-excluded drops.
    """
    claimed: set[str] = set()
    out: list[dict] = []
    for c in clusters:
        keep = c["keep"]
        drop = [n for n in c["drop"]
                if n not in claimed and n != keep and not _rel_under(n, exclude_rel)]
        if keep is not None and (keep in claimed or _rel_under(keep, exclude_rel)):
            # the chosen keeper is excluded/already-claimed → promote the largest survivor
            if not drop:
                continue
            keep = _largest(root, drop)
            drop = [n for n in drop if n != keep]
        if not drop:
            continue
        out.append(_norm(root, c["mode"], keep, drop))
        claimed.update(drop)
        if keep is not None:
            claimed.add(keep)
    return out


def _image_clusters(root: Path, near: bool) -> list[dict]:
    from navig_dedupe import image as im

    out: list[dict] = []
    thumbs = im.redundant_thumbs(root)
    thumbset = set(thumbs)
    hashes = im.hash_dir(root, skip=thumbset)
    for g in im.cluster(hashes, threshold=(10 if near else 0)):
        keep = _largest(root, g)
        out.append(_norm(root, "image", keep, [n for n in g if n != keep]))
    if thumbs:
        # keep=None → synthetic "redundant thumbnails" group (many full-res originals kept).
        out.append(_norm(root, "image", None, thumbs))
    return out


def _file_clusters(root: Path, recursive: bool,
                   exclude: tuple[str, ...] = ()) -> list[dict]:
    from navig_dedupe import file as fd

    out: list[dict] = []
    skip = set(fd.DEFAULT_SKIP_DIRS) | {e.strip().lower() for e in exclude if e.strip()}
    hashes = fd.hash_dir(root, recursive=recursive, skip=skip)
    for g in fd.cluster(hashes):  # already sorted; identical bytes, keep the first
        out.append(_norm(root, "file", g[0], list(g[1:])))
    return out


def _audio_clusters(root: Path) -> list[dict]:
    from navig_dedupe import audio as ad

    if not ad.fpcalc_available():
        _console.print("[yellow]⚠ audio dedupe skipped[/yellow] — the [b]fpcalc[/b] "
                       "(Chromaprint) binary was not found on PATH. Install it, or set "
                       "NAVIG_FPCALC, to enable acoustic-fingerprint audio dedupe.")
        return []
    out: list[dict] = []
    fps = ad.fingerprint_dir(root)
    for g in ad.cluster(fps):
        keep = _largest(root, g)
        out.append(_norm(root, "audio", keep, [n for n in g if n != keep]))
    return out


def _video_clusters(root: Path) -> list[dict]:
    from navig_dedupe import video as vd

    if not (shutil.which("ffmpeg") and shutil.which("ffprobe")):
        _console.print("[yellow]⚠ video dedupe skipped[/yellow] — [b]ffmpeg[/b]/[b]ffprobe[/b] "
                       "were not found on PATH. Install ffmpeg to enable perceptual video dedupe.")
        return []
    out: list[dict] = []
    sigs = vd.signature_dir(root)
    for g in vd.cluster(sigs):
        keep = _largest(root, g)
        out.append(_norm(root, "video", keep, [n for n in g if n != keep]))
    return out


def _write_manifest(qdir: Path, root: Path, entries: list[dict]) -> None:
    """Append restore entries to <qdir>/.dedupe-restore.json (atomic, merge-safe)."""
    path = qdir / _MANIFEST
    data: dict = {"version": 1, "root": str(root), "moved": []}
    if path.exists():
        try:
            prev = _json.loads(path.read_text("utf-8"))
            if isinstance(prev, dict) and isinstance(prev.get("moved"), list):
                data["root"] = prev.get("root", str(root))
                data["moved"] = prev["moved"]
        except (OSError, ValueError):
            pass  # unreadable prior manifest → start fresh (never lose the new moves)
    data["moved"].extend(entries)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(_json.dumps(data, indent=2), "utf-8")
    tmp.replace(path)


def _quarantine(root: Path, clusters: list[dict], qdir: Path) -> tuple[int, int]:
    """Move every cluster's extras into *qdir* (nested-safe, collision-safe) and record a
    restore manifest. Reversible — files are MOVED, never deleted. Returns (moved, freed)."""
    qdir.mkdir(parents=True, exist_ok=True)
    moved, freed = 0, 0
    entries: list[dict] = []
    for c in clusters:
        for rel in c["drop"]:
            src = root / rel
            if not src.exists():
                continue
            freed += _size(root, rel)
            dst = qdir / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            if dst.exists():
                dst = dst.with_name(src.stem + "._dup" + src.suffix)
            shutil.move(str(src), str(dst))
            moved += 1
            entries.append({"from": rel,
                            "to": dst.relative_to(qdir).as_posix()})
    if entries:
        _write_manifest(qdir, root, entries)
    return moved, freed


@dedupe_app.callback()
def _root() -> None:
    """Find, quarantine & restore duplicate photos, video, audio & files — non-destructive.

    A no-op root callback that keeps ``scan``/``restore`` explicit subcommands (Typer would
    otherwise collapse a single-command app and break ``navig dedupe scan``).
    """


@dedupe_app.command()
def scan(
    directory: Path = typer.Argument(
        ..., exists=True, file_okay=False, dir_okay=True, readable=True,
        help="Folder to scan for duplicates.",
    ),
    images: bool = typer.Option(False, "--images", help="Perceptual image dedupe (re-saved/resized copies)."),
    files: bool = typer.Option(False, "--files", help="Exact SHA-256 dedupe of any file type."),
    audio: bool = typer.Option(False, "--audio", help="Acoustic-fingerprint audio dedupe (needs fpcalc)."),
    video: bool = typer.Option(False, "--video", help="Perceptual video dedupe (needs ffmpeg)."),
    all_modes: bool = typer.Option(False, "--all", help="Run all four modes."),
    near: bool = typer.Option(False, "--near", help="Looser image matching — also catch resized/re-encoded, not just exact re-saves."),
    recursive: bool = typer.Option(False, "--recursive", "-r", help="Recurse into subfolders (files mode)."),
    exclude: Optional[list[str]] = typer.Option(
        None, "--exclude",
        help="Directory NAME not to descend into (repeatable). Quarantine folders "
             "(.trash, _dupes, _quarantine, ...) are already skipped by default."),
    move: Optional[Path] = typer.Option(None, "--move", help="Quarantine extras into this dir (MOVE, never delete). Omit for a dry run."),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="List every duplicate filename per group, not just counts."),
    fail_on_dupes: bool = typer.Option(False, "--fail-on-dupes", help="Exit non-zero if any duplicates are found (for CI / scripts)."),
    json_out: bool = typer.Option(False, "--json", help="Emit JSON for scripts/agents."),
) -> None:
    """Find near-duplicate photos/video/audio and exact-duplicate files.

    Default (no mode flags) scans images + files. Dry-run by default — pass
    --move <dir> to quarantine the extras (undo with `dedupe restore <dir>`).
    The largest/highest-res file in each group is always kept. Use -v to see the
    duplicate filenames, and --fail-on-dupes to make a dry run gate a CI build.
    """
    root = directory.resolve()
    if not (images or files or audio or video or all_modes):
        images = files = True  # sensible default: the two that need no external binaries
    if all_modes:
        images = files = audio = video = True

    # If the quarantine dir sits inside the scanned root, never re-surface its contents.
    exclude_rel: Optional[str] = None
    if move:
        try:
            exclude_rel = move.resolve().relative_to(root).as_posix()
        except ValueError:
            exclude_rel = None  # quarantine dir is outside root → nothing to exclude

    clusters: list[dict] = []
    if images:
        clusters += _image_clusters(root, near)
    if files:
        clusters += _file_clusters(root, recursive, tuple(exclude or ()))
    if audio:
        clusters += _audio_clusters(root)
    if video:
        clusters += _video_clusters(root)
    clusters = _finalize(root, clusters, exclude_rel)

    total_dupes = sum(len(c["drop"]) for c in clusters)
    total_reclaim = sum(c["reclaim"] for c in clusters)

    moved = freed = 0
    if move and clusters:
        moved, freed = _quarantine(root, clusters, move.resolve())

    if json_out:
        # Plain, guaranteed-parseable JSON (no rich decoration) for scripts/agents.
        typer.echo(_json.dumps({
            "directory": str(root),
            "groups": len(clusters),
            "duplicates": total_dupes,
            "reclaimable_bytes": total_reclaim,
            "moved": moved,
            "freed_bytes": freed,
            "quarantine_dir": str(move.resolve()) if move else None,
            "clusters": clusters,
        }, indent=2))
        if fail_on_dupes and total_dupes:
            raise typer.Exit(1)
        return

    if not clusters:
        _console.print(f"[green]✓ No duplicates found[/green] in [b]{root}[/b].")
        return

    if verbose:
        # Per-group detail: the kept file, then every duplicate that would move.
        for c in clusters:
            keeper = c["keep"] if c["keep"] else "— (redundant thumbnails)"
            _console.print(
                f"[b]{c['mode']}[/b]  keep [green]{keeper}[/green]  "
                f"[dim]({len(c['drop'])} dup · {_fmt_bytes(c['reclaim'])})[/dim]"
            )
            for d in c["drop"]:
                _console.print(f"    [red]dup[/red] {d}")
    else:
        table = Table(box=None, show_header=True, header_style="bold", padding=(0, 2))
        table.add_column("Mode", no_wrap=True)
        table.add_column("Keep", overflow="fold")
        table.add_column("Duplicates", justify="right", no_wrap=True)
        table.add_column("Reclaimable", justify="right", no_wrap=True)
        for c in clusters:
            table.add_row(
                c["mode"],
                c["keep"] if c["keep"] else "—",
                str(len(c["drop"])),
                _fmt_bytes(c["reclaim"]),
            )
        _console.print(table)

    if move:
        _console.print(
            f"\n[green]✓ Quarantined {moved} file(s)[/green] "
            f"([b]{_fmt_bytes(freed)}[/b] reclaimed) → [b]{move.resolve()}[/b]\n"
            f"[dim]  moved, not deleted — undo with:[/dim] [b]dedupe restore {move}[/b]"
        )
    else:
        _console.print(
            f"\n[b]{len(clusters)}[/b] group(s) · [b]{total_dupes}[/b] duplicate(s) · "
            f"[b]{_fmt_bytes(total_reclaim)}[/b] reclaimable  "
            f"[dim]→ re-run with[/dim] [b]--move <dir>[/b] "
            f"[dim]to quarantine them (nothing is deleted).[/dim]"
        )

    if fail_on_dupes and total_dupes:
        raise typer.Exit(1)


@dedupe_app.command()
def restore(
    quarantine_dir: Path = typer.Argument(
        ..., exists=True, file_okay=False, dir_okay=True,
        help="A quarantine dir a previous `scan --move` wrote.",
    ),
    to: Optional[Path] = typer.Option(None, "--to", help="Restore into this root (default: the path recorded in the manifest)."),
    json_out: bool = typer.Option(False, "--json", help="Emit JSON for scripts/agents."),
) -> None:
    """Undo a quarantine — move files back to where they came from.

    Reads the manifest a `scan --move` wrote and restores each file. A file whose original
    path is already occupied is left in quarantine and reported, so nothing is overwritten.
    """
    qdir = quarantine_dir.resolve()
    manifest = qdir / _MANIFEST
    if not manifest.exists():
        _console.print(
            f"[red]✗ No restore manifest[/red] in [b]{qdir}[/b] [dim]({_MANIFEST}). "
            f"Only dirs written by `dedupe scan --move` can be restored.[/dim]"
        )
        raise typer.Exit(1)
    try:
        data = _json.loads(manifest.read_text("utf-8"))
        entries = list(data.get("moved", []))
        root = to.resolve() if to else Path(data["root"])
    except (OSError, ValueError, KeyError, TypeError) as exc:
        _console.print(f"[red]✗ Unreadable manifest[/red]: {exc}")
        raise typer.Exit(1) from exc

    restored, skipped, remaining = 0, [], []
    for ent in entries:
        try:
            src = qdir / ent["to"]
            dst = root / ent["from"]
        except (KeyError, TypeError):
            continue
        if not src.exists():
            continue  # already restored or gone
        if dst.exists():
            skipped.append(ent["from"])
            remaining.append(ent)
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dst))
        restored += 1

    # Keep the manifest only for entries we could not restore (collisions); else remove it.
    if remaining:
        data["moved"] = remaining
        manifest.write_text(_json.dumps(data, indent=2), "utf-8")
    else:
        try:
            manifest.unlink()
        except OSError:
            pass

    if json_out:
        typer.echo(_json.dumps({
            "restored": restored, "skipped": len(skipped),
            "skipped_paths": skipped, "root": str(root),
        }, indent=2))
        return

    if restored:
        _console.print(f"[green]✓ Restored {restored} file(s)[/green] → [b]{root}[/b]")
    if skipped:
        shown = ", ".join(skipped[:5]) + (" …" if len(skipped) > 5 else "")
        _console.print(
            f"[yellow]⚠ {len(skipped)} left in quarantine[/yellow] "
            f"[dim](original path already exists): {shown}[/dim]"
        )
    if not restored and not skipped:
        _console.print("[green]✓ Nothing to restore[/green] [dim](manifest already empty).[/dim]")


def _build_app() -> typer.Typer:
    return dedupe_app
