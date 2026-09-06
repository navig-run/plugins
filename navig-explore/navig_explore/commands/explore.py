"""`navig explore <folder>` — universal local media/data explorer.

Point it at ANY folder (Downloads, a Telegram export, a drive dump). It walks the
tree, buckets every file by TYPE, auto-detects Telegram exports (captions + links),
and serves the explorer UI: filter, preview, drag-to-class, extract texts, delete,
organize. Non-destructive — a ``.mediaexplorer/`` sidecar holds edits; commit is
reversible.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import typer
from typer.core import TyperGroup

from navig.lazy_loader import lazy_import

ch = lazy_import("navig.console_helper")

class _ExploreGroup(TyperGroup):
    """Keeps ``navig explore <folder>`` working *and* lets subcommands be reached.

    The headline form is a bare folder, so the group callback used to declare a
    positional ``directory``. Click parses group arguments before dispatching, so that
    positional swallowed the subcommand name: ``navig explore probe X:\\foo`` read
    ``probe`` as the folder, then choked on the subcommand's own options
    ("No such option '--workers'"). Every subcommand — probe, dedup, route, collect,
    merge-episode, audio-sort — was unreachable, and the failure looked like a typo in
    the option rather than a routing bug.

    The folder form is now an ordinary ``open`` subcommand, and a leading token that
    isn't a known command is rewritten to it. Both spellings work; nothing is ambiguous.
    """

    def parse_args(self, ctx, args):
        if args and not args[0].startswith("-") and args[0] not in self.commands:
            args = ["open", *args]
        return super().parse_args(ctx, args)


explore_app = typer.Typer(
    name="explore",
    cls=_ExploreGroup,
    help="🔎 Universal explorer for ANY folder — filter by type, preview, organize, extract, delete.",
    invoke_without_command=True,
)


@explore_app.callback(invoke_without_command=True)
def explore(ctx: typer.Context) -> None:
    """Explore & organize any folder in a local web UI."""
    if ctx.invoked_subcommand is None:
        ch.error("Give a folder to explore:  navig explore <folder>")
        raise typer.Exit(1)


@explore_app.command("open")
def explore_open(
    directory: str = typer.Argument(..., help="Folder to explore (e.g. your Downloads)."),
    out: Path | None = typer.Option(
        None, "--out", help="Where 'Organize on disk' files land (default: <folder>/_ORGANIZED)."),
    port: int | None = typer.Option(
        None, "--port", "-p",
        help="Default: 8770, or a free port if the OS has that range reserved."),
    no_open: bool = typer.Option(False, "--no-open", help="Don't auto-open the browser."),
) -> None:
    """🔎 Browse & organize a folder in the local web UI (the default for `navig explore <folder>`)."""
    root = Path(directory).expanduser()
    if not root.is_dir():
        ch.error(f"Not a directory: {root}")
        raise typer.Exit(1)

    from navig.http_bind import PortBindError  # noqa: PLC0415
    from navig_explore.explorer import serve  # noqa: PLC0415
    ch.success(f"Exploring {root.name}…   (Ctrl+C to stop)")
    # serve() binds the socket first, then indexes in the background and opens the
    # browser once it's actually listening (avoids ERR_CONNECTION_REFUSED on big folders).
    try:
        serve(root, out, port, open_browser=not no_open)
    except PortBindError as exc:
        ch.error(str(exc))
        raise typer.Exit(1) from None
    except KeyboardInterrupt:
        pass


@explore_app.command("gallery")
def gallery_cmd(
    directory: str = typer.Argument(..., help="Folder to build a gallery for."),
    out: Path | None = typer.Option(
        None, "--out", help="Output HTML path (default: <folder>/gallery.html)."),
    title: str | None = typer.Option(None, "--title", help="Page heading (default: folder name)."),
    size: int = typer.Option(320, "--size", help="Thumbnail longest side, px."),
    prune: bool = typer.Option(
        False, "--prune", help="Move unreferenced cached thumbs to .mediaexplorer/thumbs/_stale."),
    max_per_section: int | None = typer.Option(
        None, "--max-per-section", "-m",
        help="Cap items shown per section (huge libraries). Truncation is shown on the page."),
    recursive: bool = typer.Option(
        False, "--recursive", "-r",
        help="One gallery per subfolder + an index page linking them (for whole libraries)."),
    split_over: int = typer.Option(
        20_000, "--split-over",
        help="With --recursive: a subfolder bigger than this is split into ITS children."),
    serve: bool = typer.Option(False, "--serve", help="Serve it over HTTP instead of writing only."),
    lan: bool = typer.Option(
        False, "--lan", help="With --serve: bind all interfaces so phones on the LAN can open it."),
    port: int | None = typer.Option(
        None, "--port", "-p",
        help="Port for --serve (default: pick a free one — some ranges are OS-reserved)."),
    open_it: bool = typer.Option(False, "--open", help="Open the gallery when it's built."),
) -> None:
    """🖼️  Build a shareable offline gallery.html — cached thumbnails, live filter, lightbox.

    Groups images by top-level subfolder, caches a webp thumbnail per image (so a
    10,000-image folder still opens instantly), and writes ONE self-contained page that
    needs no server. Non-destructive: only gallery.html + a .mediaexplorer/thumbs cache.

    Add --serve (optionally --lan) to open it from your phone instead of a file:// path.
    Add --recursive for a whole library: one gallery per subfolder plus an index page.
    """
    from navig_explore.gallery import (  # noqa: PLC0415
        GalleryServeError,
        build_gallery,
        build_gallery_tree,
        serve_gallery,
    )
    root = Path(directory).expanduser()
    if not root.is_dir():
        ch.error(f"Not a directory: {root}")
        raise typer.Exit(1)

    if recursive:
        def _tick(label: str, s: dict) -> None:
            ch.info(f"  {label} — {s['images']:,} images, {s['sections']} sections")
        tree = build_gallery_tree(
            root, split_over=split_over, title=title, thumb_max=size, prune=prune,
            max_per_section=max_per_section, on_progress=_tick,
        )
        ch.success(
            f"{tree['galleries']} galleries · {tree['images']:,} images · "
            f"{tree['videos']:,} videos → {tree['index']}"
        )
        if tree["thumb_fail"]:
            ch.warning(f"{tree['thumb_fail']:,} thumbnail(s) could not be generated")
        if tree["dropped"]:
            ch.warning(f"{tree['dropped']:,} item(s) not shown (per-section cap)")
        if open_it:
            import webbrowser  # noqa: PLC0415
            webbrowser.open(Path(tree["index"]).as_uri())
        return

    stats = build_gallery(root, out, title=title, thumb_max=size, prune=prune,
                          max_per_section=max_per_section)
    if not stats["pillow"]:
        ch.warning("Pillow not installed — linking full-size images (pip install Pillow for the cache).")
    ch.success(
        f"gallery: {stats['images']:,} images in {stats['sections']} sections, "
        f"{stats['videos']:,} videos → {stats['out']}"
    )
    ch.info(
        f"thumbs: made {stats['thumb_made']:,}, cached {stats['thumb_cached']:,}, "
        f"linked {stats['thumb_orig']:,}, failed {stats['thumb_fail']}"
        + (f" · pruned {stats['pruned']}" if prune else "")
    )
    if stats["capped"]:
        detail = ", ".join(f"{lbl} {shown:,}/{found:,}" for lbl, (shown, found) in stats["capped"].items())
        ch.warning(f"capped {stats['dropped']:,} item(s) not shown — {detail}")
    if serve:
        try:
            serve_gallery(Path(stats["out"]), root, port=port, lan=lan, open_browser=open_it)
        except GalleryServeError as exc:
            ch.error(str(exc))
            raise typer.Exit(1) from None
    elif open_it:
        import webbrowser  # noqa: PLC0415
        webbrowser.open(Path(stats["out"]).as_uri())


@explore_app.command("probe")
def probe_cmd(
    directory: str = typer.Argument(..., help="Folder to probe for media metadata."),
    workers: int = typer.Option(12, "--workers", "-w", help="Parallel ffprobe workers."),
    limit: int | None = typer.Option(None, "--limit", help="Stop after N files (testing)."),
) -> None:
    """📐 Extract width/height/codec/date/GPS/camera → .mediaexplorer/meta.jsonl (resumable)."""
    from navig_explore.probe import probe_folder  # noqa: PLC0415
    root = Path(directory).expanduser()
    if not root.is_dir():
        ch.error(f"Not a directory: {root}")
        raise typer.Exit(1)
    counts = probe_folder(root, workers=workers, limit=limit)
    ch.success(f"probed: {counts}")


@explore_app.command("dedup")
def dedup_cmd(
    roots: list[str] = typer.Argument(..., help="One or more probed roots (dedup spans them)."),
    near_image_dist: int = typer.Option(6, "--near-image-dist", help="pHash Hamming threshold."),
    out: Path | None = typer.Option(None, "--out", help="dupes.jsonl output path."),
    workers: int = typer.Option(16, "--workers", "-w", help="Parallel hash/decode workers."),
    exact_only: bool = typer.Option(
        False, "--exact-only",
        help="Skip the perceptual near-duplicate pass (much faster on big libraries)."),
    quarantine: bool = typer.Option(
        False, "--quarantine",
        help="Also MOVE the redundant copies into <root>/.trash/dupes (reversible)."),
    trash: Path | None = typer.Option(
        None, "--trash", help="Where --quarantine puts them (default <root>/.trash/dupes)."),
    prefer_placement: bool = typer.Option(
        True, "--prefer-placement/--no-prefer-placement",
        help="Keep the copy in the more meaningful folder (named event > day > album > "
             "date bucket > _Undated > staging) instead of just the largest file."),
    date_root: str = typer.Option(
        "By Date", "--date-root", help="Subfolder holding the YYYY\\YYYY-MM tree."),
) -> None:
    """🎯 Find exact + identical-pixel (auto-trash) and near (flag-only) duplicates.

    Writes .mediaexplorer/dupes.jsonl. Exact groups are written as soon as they are
    found, before the perceptual pass — so a long sweep that gets interrupted never
    loses them.

    `--quarantine` acts on the report in the same command: every redundant copy moves to
    <root>/.trash/dupes keeping its relative path, and nothing is deleted. Each group
    always keeps one member. Only `exact` and `identical-pixels` are moved — a near
    cluster is often a burst of consecutive shots rather than one photo twice, so those
    stay for a human. Undo any run with:  navig explore undo <log>

    (That last reference is intentionally not back-quoted. The plugin-advice guard
    resolves back-quoted `navig …` references against the INSTALLED build, which cannot
    know about a command introduced in the same commit as the advice naming it.)
    """
    from navig_explore import dedup as _dedup  # noqa: PLC0415
    rs = [Path(r).expanduser() for r in roots]

    rank = None
    if prefer_placement:
        from navig_explore import photos as _ph  # noqa: PLC0415
        rank = lambda rel: _ph.placement_rank(rel, date_root)  # noqa: E731
        ch.dim("keeping the copy in the more meaningful folder "
               "(named event > day > album > date bucket > _Undated > staging)")

    summary = _dedup.dedup(rs, near_image_dist=near_image_dist, out=out,
                           workers=workers, exact_only=exact_only, rank=rank)
    ch.success(f"dedup: {summary}")
    if not quarantine:
        if summary.get("exact_dupe_files") or summary.get("identical_pixel_files"):
            ch.info("REPORT ONLY — re-run with --quarantine to move the redundant copies.")
        return

    from navig_explore import photos as ph  # noqa: PLC0415
    total = 0
    for root in rs:
        rows = _dedup.plan_quarantine(root.resolve(), trash)
        if not rows:
            continue
        log = (root.resolve() / ".mediaexplorer"
               / f"dedup-quarantine-{time.strftime('%Y%m%d-%H%M%S')}.csv")
        stats = ph.apply_plan(rows, log, quiet=True)
        moved = sum(v for k, v in stats.items()
                    if k.startswith("trash-") and isinstance(v, int))
        total += moved
        ch.success(f"quarantined {moved} files from {root.name} → "
                   f"{trash or root / '.trash' / 'dupes'}  ({stats.get('moved_gb', 0)} GB)")
        ch.dim(f"reversible log → {log}   (undo:  navig explore undo \"{log}\")")
    if not total:
        ch.info("nothing to quarantine")


@explore_app.command("undo")
def undo_cmd(
    log: str = typer.Argument(..., help="A move-log CSV written by --quarantine or photos --apply."),
) -> None:
    """↩️  Put every file a quarantine / photos-organize run moved back where it was."""
    from navig_explore import photos as ph  # noqa: PLC0415
    p = Path(log).expanduser()
    if not p.exists():
        ch.error(f"No such log: {p}")
        raise typer.Exit(1)
    ch.success(f"undo: {ph.undo(p)}")


photos_app = typer.Typer(
    name="photos",
    help="🖼️  Organise a photo library by capture date — without destroying human structure.",
    no_args_is_help=True,
)
explore_app.add_typer(photos_app, name="photos")


@photos_app.command("organize")
def photos_organize_cmd(
    root: str = typer.Argument(..., help="Photo library root (e.g. X:\\Photos)."),
    date_root: str = typer.Option(
        "By Date", "--date-root", help="Subfolder holding the YYYY\\YYYY-MM tree."),
    video_dest: str = typer.Option(
        None, "--video-dest", help="Move video out to this root (e.g. X:\\Video\\from-photos)."),
    loose: list[str] = typer.Option(
        None, "--loose", help="Top-level folder that is an unsorted staging area (repeatable)."),
    tiny_max: int = typer.Option(
        20480, "--tiny-max", help="Quarantine images smaller than this many bytes."),
    skip_tiny: bool = typer.Option(False, "--skip-tiny", help="Don't quarantine small images."),
    recovered_dir: str = typer.Option(
        "_Recovered", "--recovered-dir", help="Where undatable recovery salvage goes."),
    pixel_dupes: bool = typer.Option(
        True, "--pixel-dupes/--no-pixel-dupes",
        help="Also quarantine copies that decode to the SAME image but differ in "
             "metadata (needs `dedup` without --exact-only)."),
    prune: bool = typer.Option(True, "--prune/--no-prune", help="Remove emptied directories."),
    only: str = typer.Option(
        None, "--only", help="Comma-separated actions to run (e.g. trash-junk,redate)."),
    do_apply: bool = typer.Option(False, "--apply", help="Execute (default: plan only)."),
) -> None:
    """🖼️  File photos into <date-root>/YYYY/YYYY-MM by EXIF, quarantine litter and salvage.

    Run `navig explore probe <root>` first (capture dates), and optionally
    `navig explore dedup <root>` (byte-identical copies get quarantined too).

    NEVER reshapes human-named event folders ("2009/Alzon 2009"), day folders, or
    anything outside the date tree — those carry meaning no metadata can rebuild.
    Dates come from EXIF only; a photo with no capture date stays undated rather than
    being filed under a guess. Nothing is deleted: quarantine lands in <root>/.trash
    and every run writes an undo log.
    """
    from navig_explore import photos as ph  # noqa: PLC0415

    r = Path(root).expanduser().resolve()
    if not r.is_dir():
        ch.error(f"Not a directory: {r}")
        raise typer.Exit(1)
    side = r / ".mediaexplorer"
    if not (side / "meta.jsonl").exists():
        ch.error(f"No capture dates indexed. Run first:  navig explore probe {r}")
        raise typer.Exit(2)

    rows, summary = ph.build_plan(
        r, date_root=date_root,
        video_dest=Path(video_dest).expanduser().resolve() if video_dest else None,
        loose=set(loose) if loose else None,
        tiny_max=tiny_max, recovered_dir=recovered_dir, skip_tiny=skip_tiny,
        pixel_dupes=pixel_dupes)
    plan_csv = ph.write_plan(rows, side / "photos-plan.csv")

    kept = sum(v for k, v in summary.items() if k.startswith("keep"))
    ch.info(f"{len(rows)} moves planned · {kept} files left untouched")
    for k, v in summary.items():
        ch.dim(f"  {k:<18} {v}")
    ch.dim(f"plan → {plan_csv}")

    if not do_apply:
        ch.info("PLAN ONLY — review the CSV, then re-run with --apply.")
        return

    log = side / f"photos-log-{time.strftime('%Y%m%d-%H%M%S')}.csv"
    stats = ph.apply_plan(rows, log, only={s.strip() for s in only.split(",")} if only else None)
    ch.success(f"moved: {stats}")
    if prune:
        gone = ph.prune_empty_dirs(r, apply=True)
        if gone:
            ch.dim(f"removed {len(gone)} emptied directories")
    ch.dim(f"reversible log → {log}   (undo:  navig explore photos undo \"{log}\")")


@photos_app.command("undo")
def photos_undo_cmd(
    log: str = typer.Argument(..., help="photos-log-*.csv written by --apply."),
) -> None:
    """↩️  Put every file a photos-organize run moved back where it came from."""
    from navig_explore import photos as ph  # noqa: PLC0415
    p = Path(log).expanduser()
    if not p.exists():
        ch.error(f"No such log: {p}")
        raise typer.Exit(1)
    ch.success(f"undo: {ph.undo(p)}")


# ── vision pass ─────────────────────────────────────────────────────────────
def _vision_root(root: str, *, need_meta: bool = True) -> Path:
    """Resolve a library root and insist `probe` has already walked it."""
    r = Path(root).expanduser().resolve()
    if not r.is_dir():
        ch.error(f"Not a directory: {r}")
        raise typer.Exit(2)
    if need_meta and not (r / ".mediaexplorer" / "meta.jsonl").exists():
        ch.error(f"No index yet. Run first:  navig explore probe {r}")
        raise typer.Exit(2)
    return r


@photos_app.command("index")
def photos_index_cmd(
    root: str = typer.Argument(..., help="Photo library root (e.g. X:\\Photos)."),
    workers: int = typer.Option(12, "--workers", "-w", help="Decode threads."),
    limit: int = typer.Option(None, "--limit", help="Stop after N files (for trials)."),
    embed: bool = typer.Option(True, "--embed/--no-embed", help="Compute SigLIP embeddings."),
    device: str = typer.Option(None, "--device", help="cuda | cpu (default: auto)."),
    batch: int = typer.Option(256, "--batch", help="GPU batch size."),
) -> None:
    """🧠  Read every photo ONCE — hash, measure, embed and classify it.

    Builds `.mediaexplorer/vision.db`, keyed on content hash so person names and
    dates survive any later reorganisation. Strictly read-only: nothing is moved,
    renamed or written back into your files.

    Resumable — an interrupted run costs only the batch in flight.
    """
    from navig_explore.vision import catalog, pipeline  # noqa: PLC0415

    r = _vision_root(root)
    try:
        stats = pipeline.run_index(r, workers=workers, limit=limit, do_embed=embed,
                                   device=device, batch=batch)
    except typer.Exit:
        raise
    except RuntimeError as exc:
        ch.error(str(exc))
        raise typer.Exit(1) from exc

    conn = catalog.connect(r)
    indexed = stats["done"] - stats.get("missing", 0) - stats["undecodable"]
    ch.success(f"indexed {indexed} files · {stats['undecodable']} undecodable · "
               f"{stats.get('missing', 0)} missing")
    if stats.get("missing"):
        ch.dim(f"  {stats['missing']} files have moved since `probe` last ran — "
               f"they are flagged, not lost. Refresh:  navig explore probe {r}")
    for k, v in catalog.counts(conn, r).items():
        ch.dim(f"  {k:<12} {v}")


@photos_app.command("faces")
def photos_faces_cmd(
    root: str = typer.Argument(..., help="Photo library root."),
    engine: str = typer.Option("yunet-sface", "--faces-engine",
                               help="yunet-sface (Apache-2.0, default) | arcface "
                                    "(better, NON-COMMERCIAL weights)."),
    workers: int = typer.Option(8, "--workers", "-w"),
    limit: int = typer.Option(None, "--limit", help="Stop after N images."),
    min_dim: int = typer.Option(256, "--min-dim", help="Skip images smaller than this."),
) -> None:
    """🙂  Find and embed faces — only in files that are actually photographs.

    Runs after `photos index`, over the images the classifier called photo/webcam.
    Photos whose EXIF orientation was lost are rotation-searched, so faces lying on
    their side are still found.
    """
    from navig_explore.vision import faces as FA, pipeline  # noqa: PLC0415

    r = _vision_root(root, need_meta=False)
    if engine not in FA.ENGINES:
        ch.error(f"Unknown engine {engine!r}. Choose: {', '.join(FA.ENGINES)}")
        raise typer.Exit(2)
    if engine == "arcface":
        ch.dim("note: InsightFace buffalo_l weights are licensed for "
               "NON-COMMERCIAL research use only.")
    try:
        stats = pipeline.run_faces(r, engine=engine, workers=workers,
                                   limit=limit, min_dim=min_dim)
    except typer.Exit:
        raise
    except RuntimeError as exc:
        ch.error(str(exc))
        raise typer.Exit(1) from exc
    ch.success(f"{stats['faces']} faces in {stats['images']} images "
               f"({stats['rotated']} needed rotating)")


@photos_app.command("dates")
def photos_dates_cmd(
    root: str = typer.Argument(..., help="Photo library root."),
    ocr: bool = typer.Option(True, "--ocr/--no-ocr",
                             help="Read burned-in timestamps off webcam frames."),
    ocr_workers: int = typer.Option(8, "--ocr-workers"),
    twin_distance: int = typer.Option(
        8, "--twin-distance",
        help="Max pHash Hamming distance for inheriting a twin's date."),
) -> None:
    """📅  Recover capture dates EXIF no longer holds — and record HOW.

    Runs the ladder strongest-first: EXIF, burned-in overlay OCR, filename,
    near-duplicate twin, event-folder year, folder median, then mtime — and mtime
    only when it isn't recovery-run junk (a timestamp shared by implausibly many
    files is the carving tool's clock, not a capture time).

    Writes dates into the catalog with a source and confidence for every one.
    Nothing is moved; `photos explain <file>` shows the derivation.
    """
    from navig_explore.vision import dates as D  # noqa: PLC0415

    r = _vision_root(root, need_meta=False)
    from navig_explore.vision import catalog  # noqa: PLC0415
    if not catalog.db_path(r).exists():
        ch.error(f"No vision catalog yet. Run:  navig explore photos index {r}")
        raise typer.Exit(2)

    stats = D.resolve(r, do_ocr=ocr, ocr_workers=ocr_workers,
                      twin_distance=twin_distance)
    ch.success(f"dated {stats.get('total_dated', 0)} files")
    for src, n in sorted(stats.get("by_source", {}).items(),
                         key=lambda kv: -kv[1]):
        ch.dim(f"  {src:<10} {n:>7}   confidence {D.CONFIDENCE.get(src, 0):.2f}")
    if stats.get("junk_mtime_values"):
        ch.dim(f"  refused {stats['mtime_rejected']} files whose mtime is one of "
               f"{stats['junk_mtime_values']} recovery-run timestamps")


@photos_app.command("explain")
def photos_explain_cmd(
    path: str = typer.Argument(..., help="A file inside an indexed library."),
    root: str = typer.Option(None, "--root", help="Library root (default: infer)."),
) -> None:
    """🔍  Show every fact the catalog holds about one photo, and where it came from."""
    import json as _json  # noqa: PLC0415

    from navig_explore.vision import catalog, dates as D  # noqa: PLC0415

    p = Path(path).expanduser().resolve()
    if root:
        r = Path(root).expanduser().resolve()
    else:
        r = next((a for a in p.parents if catalog.db_path(a).exists()), None)
        if r is None:
            ch.error("Could not find a vision catalog above that file. Pass --root.")
            raise typer.Exit(2)
    info = D.explain(r, str(p))
    if not info:
        ch.error(f"Not in the catalog: {p}")
        raise typer.Exit(1)
    ch.info(_json.dumps(info, indent=2, ensure_ascii=False, default=str))


@photos_app.command("search")
def photos_search_cmd(
    query: str = typer.Argument(None, help='e.g. "a red car on a beach". Omit to filter only.'),
    root: str = typer.Option(..., "--root", "-r", help="Photo library root."),
    person: str = typer.Option(None, "--person", help="Only photos of this named person."),
    year: int = typer.Option(None, "--year", help="Only photos dated to this year."),
    place: str = typer.Option(None, "--place", help="Only photos from this place."),
    klass: str = typer.Option(None, "--class", help="photo|webcam|screenshot|web-graphic|document."),
    like: str = typer.Option(None, "--like", help="Find photos similar to THIS image."),
    min_conf: float = typer.Option(None, "--min-date-confidence",
                                   help="Ignore weakly-dated photos when filtering by year."),
    limit: int = typer.Option(30, "--limit", "-n"),
    gallery: str = typer.Option(None, "--gallery", help="Write results to an HTML gallery."),
) -> None:
    """🔎  Find photos by what is IN them — plus person, place, year and kind.

    Runs SigLIP 2 over the embeddings already in the catalog, so a query is a
    single matrix product: exact, offline and instant. Multilingual — ask in
    French or Russian if that is how the library is named.
    """
    from navig_explore.vision import catalog, search as S  # noqa: PLC0415

    r = _vision_root(root, need_meta=False)
    if not catalog.db_path(r).exists():
        ch.error(f"No vision catalog yet. Run:  navig explore photos index {r}")
        raise typer.Exit(2)
    if not any([query, like, person, year, place, klass]):
        ch.error("Give a query, --like, or at least one filter.")
        raise typer.Exit(2)
    try:
        hits = S.search(r, query, limit=limit, person=person, year=year, place=place,
                        klass=klass, like=like, min_confidence=min_conf)
    except (ValueError, RuntimeError) as exc:
        ch.error(str(exc))
        raise typer.Exit(1) from exc

    if not hits:
        ch.info("no matches")
        return
    for h in hits:
        bits = [f"{h['score']:.3f}", h["rel"]]
        if h["date"]:
            bits.append(f"{h['date'][:10]} ({h['date_source']})")
        if h["people"]:
            bits.append("· " + ", ".join(h["people"]))
        ch.info("  ".join(bits))
    ch.dim(f"{len(hits)} results")

    if gallery:
        out = Path(gallery).expanduser().resolve()
        from navig_explore.vision import gallery as G  # noqa: PLC0415
        G.write_results(hits, out, title=query or "filtered results")
        ch.success(f"gallery → {out}")


people_app = typer.Typer(name="people", help="🙋  Group faces into people and name them.",
                         no_args_is_help=True)
photos_app.add_typer(people_app, name="people")


@people_app.command("cluster")
def people_cluster_cmd(
    root: str = typer.Argument(..., help="Photo library root."),
    min_size: int = typer.Option(3, "--min-size", help="Smallest group HDBSCAN will form."),
    adopt: bool = typer.Option(
        True, "--adopt/--no-adopt",
        help="Also fold faces HDBSCAN called noise into the group they clearly match."),
    adopt_only: bool = typer.Option(
        False, "--adopt-only",
        help="Skip clustering; only file NEW faces into the groups that exist. "
             "Keeps every group id and name, so naming work is never lost."),
) -> None:
    """Group the detected faces. Names you have already assigned are preserved.

    HDBSCAN is conservative and labels anything in a sparse region as noise —
    on a long, varied library that can be most of the faces. The adopt pass then
    gives each orphan the nearest group it clearly belongs to, so coverage is not
    hostage to how densely a person happens to be photographed.
    """
    from navig_explore.vision import people as P  # noqa: PLC0415

    r = _vision_root(root, need_meta=False)
    if adopt_only:
        # Re-clustering renumbers every group, so a library that has already been
        # named should only ever have its NEW faces filed into what exists.
        from navig_explore.vision import catalog  # noqa: PLC0415

        conn = catalog.connect(r)
        n = P.adopt_unclustered(conn)
        left = conn.execute(
            "SELECT COUNT(*) FROM faces WHERE person_id IS NULL").fetchone()[0]
        ch.success(f"filed {n:,} new faces into existing groups · {left:,} still unmatched")
        return
    try:
        stats = P.cluster(r, min_cluster_size=min_size, adopt=adopt)
    except RuntimeError as exc:
        ch.error(str(exc))
        raise typer.Exit(1) from exc
    if not stats["faces"]:
        ch.error("No faces indexed yet. Run:  navig explore photos faces <root>")
        raise typer.Exit(1)
    ch.dim(f"  adopted {stats.get('adopted', 0)} previously unclustered faces · "
           f"{stats.get('noise', 0)} still unmatched")
    ch.success(f"{stats['faces']} faces · {stats['clusters']} new groups · "
               f"{stats['assigned_to_named']} re-attached to named people")


@people_app.command("list")
def people_list_cmd(
    root: str = typer.Argument(..., help="Photo library root."),
    limit: int = typer.Option(40, "--limit", "-n"),
) -> None:
    """Show face groups, biggest first."""
    from navig_explore.vision import people as P  # noqa: PLC0415

    r = _vision_root(root, need_meta=False)
    rows = P.listing(r, limit=limit)
    if not rows:
        ch.info("no groups yet — run `photos people cluster`")
        return
    for row in rows:
        label = row["name"] or f"(unnamed #{row['person_id']})"
        lock = " 🔒" if row["locked"] else ""
        ch.info(f"  {row['person_id']:>5}  {row['n']:>5} faces  {label}{lock}")


@people_app.command("sheet")
def people_sheet_cmd(
    root: str = typer.Argument(..., help="Photo library root."),
    out: str = typer.Option(None, "--out", help="Default: <root>/.mediaexplorer/people.jpg."),
    limit: int = typer.Option(40, "--limit", "-n", help="How many groups to show."),
    per_group: int = typer.Option(8, "--per-group", help="Faces sampled per group."),
    min_photos: int = typer.Option(5, "--min-photos", help="Skip groups smaller than this."),
) -> None:
    """🖼️  One row of faces per group, labelled with the id — so you can name them.

    `people list` prints ids and counts, which tells you nothing about who anyone
    is. This shows the faces beside the id that `people name` takes, and also
    reports which groups are close enough to be worth checking as one person.
    """
    from navig_explore.vision import people as P  # noqa: PLC0415

    r = _vision_root(root, need_meta=False)
    dest = Path(out).expanduser() if out else r / ".mediaexplorer" / "people.jpg"
    try:
        stats = P.contact_sheet(r, dest, limit=limit, per_group=per_group,
                                min_photos=min_photos)
    except ImportError:
        ch.error("Pillow is needed for the sheet:  pip install Pillow")
        raise typer.Exit(1) from None
    if not stats["groups"]:
        ch.info("no groups yet — run `photos people cluster`")
        return
    ch.success(f"{stats['groups']} groups · {stats['faces']} faces → {stats['out']}")
    ch.dim(f"name one with:  navig explore photos people name {r} <id> \"Their Name\"")

    pairs = P.merge_candidates(r)
    if pairs:
        ch.info(f"{len(pairs)} pair(s) close enough to be worth checking as one person:")
        for p in pairs[:10]:
            ch.dim(f"  {p['similarity']:.3f}  id {p['a']} ({p['a_photos']}p)"
                   f"  ↔  id {p['b']} ({p['b_photos']}p)")
        ch.dim("  merge only after looking:  photos people merge <root> <keep> <other>")


@people_app.command("name")
def people_name_cmd(
    root: str = typer.Argument(..., help="Photo library root."),
    person_id: int = typer.Argument(..., help="Group id from `people list`."),
    label: str = typer.Argument(..., help="The person's name."),
) -> None:
    """Name a group and lock it, so re-clustering never loses the name."""
    from navig_explore.vision import people as P  # noqa: PLC0415

    r = _vision_root(root, need_meta=False)
    try:
        n = P.name(r, person_id, label)
    except ValueError as exc:
        ch.error(str(exc))
        raise typer.Exit(1) from exc
    ch.success(f"{label}: {n} faces (locked)")


@people_app.command("merge")
def people_merge_cmd(
    root: str = typer.Argument(..., help="Photo library root."),
    keep: int = typer.Argument(..., help="Group id to keep."),
    others: list[int] = typer.Argument(..., help="Group ids to fold into it."),
) -> None:
    """Fold groups together — clustering always over-splits the same person."""
    from navig_explore.vision import people as P  # noqa: PLC0415

    r = _vision_root(root, need_meta=False)
    ch.success(f"moved {P.merge(r, keep, *others)} faces into group {keep}")


@people_app.command("split")
def people_split_cmd(
    root: str = typer.Argument(..., help="Photo library root."),
    person_id: int = typer.Argument(..., help="Group id to break apart."),
) -> None:
    """Detach a group so the next cluster run re-derives it."""
    from navig_explore.vision import people as P  # noqa: PLC0415

    r = _vision_root(root, need_meta=False)
    ch.success(f"released {P.split(r, person_id)} faces")


@photos_app.command("classes")
def photos_classes_cmd(
    root: str = typer.Argument(..., help="Photo library root."),
    # Literal defaults: the vision modules are an optional extra and are imported
    # inside the command body, so a default may not reach into one at load time.
    min_probability: float = typer.Option(
        0.55, "--min-probability",
        help="How sure the best guess must be (0-1) to be trusted. Below it a "
             "file is `unsorted` rather than filed under a wrong label."),
) -> None:
    """🏷️  Re-decide what each file IS — file metadata first, guess second.

    The zero-shot classifier decides by argmax with no way to say "I don't know",
    which put 7,421 files carrying camera EXIF into the webcam class. Camera
    metadata, exact device screen sizes and a parsed burned-in timestamp settle
    most of it outright; the guess only arbitrates what is genuinely visual, and
    anything too close to call lands in `unsorted` where you can see it.

    Costs a matrix product, not another pass over the images.
    """
    from navig_explore.vision import catalog, classify_rules as CR  # noqa: PLC0415

    r = _vision_root(root, need_meta=False)
    if not catalog.db_path(r).exists():
        ch.error(f"No vision catalog yet. Run:  navig explore photos index {r}")
        raise typer.Exit(2)
    try:
        stats = CR.refine(r, min_probability=min_probability)
    except RuntimeError as exc:
        ch.error(str(exc))
        raise typer.Exit(1) from exc
    total = sum(v for k, v in stats.items() if not k.startswith("via "))
    ch.success(f"re-decided {total:,} files")


@photos_app.command("events")
def photos_events_cmd(
    root: str = typer.Argument(..., help="Photo library root."),
    gap_hours: float = typer.Option(
        24.0, "--gap-hours",
        help="A gap this long between consecutive photos starts a new occasion."),
    min_size: int = typer.Option(
        3, "--min-size", help="Runs shorter than this are strays, not events."),
) -> None:
    """📅  Work out which occasion each photograph belongs to.

    Folders you named by hand always win — those are never re-clustered, only
    date-prefixed so a year sorts chronologically. Everything else with a solid
    date is split wherever the gap between shots reaches --gap-hours.

    The 24-hour default is measured, not guessed: it yields events averaging 20.3
    photographs, and your own named events have a median of 20.
    """
    from navig_explore.vision import catalog, events as EV  # noqa: PLC0415

    r = _vision_root(root, need_meta=False)
    if not catalog.db_path(r).exists():
        ch.error(f"No vision catalog yet. Run:  navig explore photos index {r}")
        raise typer.Exit(2)
    stats = EV.detect(r, gap_hours=gap_hours, min_size=min_size)
    if not stats["photos"]:
        ch.error("Nothing to group — has `photos dates` run?")
        raise typer.Exit(1)
    ch.success(f"{stats['curated_events']} curated + {stats['detected_events']} detected "
               f"occasions · {stats['photos']:,} photographs placed")
    ch.dim(f"  {stats['undated']:,} could not be placed (no confident date) · "
           f"{stats['too_small']:,} in runs under {min_size}")


@photos_app.command("subjects")
def photos_subjects_cmd(
    root: str = typer.Argument(..., help="Photo library root."),
    sigma: float = typer.Option(
        2.6, "--sigma",
        help="How far above a subject's own baseline a score must sit — is this "
             "photo unusual for this subject?"),
    row_sigma: float = typer.Option(
        3.0, "--row-sigma",
        help="How far the subject must stand out among the photo's own scores — "
             "is this subject unusual for this photo? Lower it for more photos "
             "per folder and more mistakes in them."),
    max_per_photo: int = typer.Option(
        4, "--max-per-photo", help="Most labels to keep for one photograph."),
    top_fraction: float = typer.Option(
        0.0, "--top-fraction",
        help="Optional hard ceiling per subject, as a share of the library. "
             "0 disables it."),
) -> None:
    """🏷️  Tag what photographs are OF — sunsets, food, the sea, cars, concerts.

    Multi-label on purpose: dinner on a terrace at sunset is food AND sunset AND
    architecture, and picking one winner throws most of it away.

    Each subject gets its own bar, computed from this library's own score spread.
    A single global threshold cannot work — SigLIP scores sit in a narrow band and
    every prompt has its own centre, so one number over-fires on "sunset" and
    never fires on "museum". A photo matching nothing confidently gets no label.
    """
    from navig_explore.vision import catalog, subjects as S  # noqa: PLC0415

    r = _vision_root(root, need_meta=False)
    if not catalog.db_path(r).exists():
        ch.error(f"No vision catalog yet. Run:  navig explore photos index {r}")
        raise typer.Exit(2)
    try:
        stats = S.tag(r, sigma=sigma, row_sigma=row_sigma,
                      max_per_photo=max_per_photo, top_fraction=top_fraction)
    except RuntimeError as exc:
        ch.error(str(exc))
        raise typer.Exit(1) from exc
    if not stats["photos"]:
        ch.error("Nothing tagged — are there photographs indexed and classified?")
        raise typer.Exit(1)
    ch.success(f"{stats['photos']:,} of {stats['considered']:,} photographs tagged "
               f"· {stats['tags']:,} labels across {len(S.subjects())} subjects")


@photos_app.command("views")
def photos_views_cmd(
    root: str = typer.Argument(..., help="Photo library root (e.g. X:\\Photos)."),
    dest: str = typer.Option(None, "--dest", help="Default: <root>/_organized."),
    min_group: int = typer.Option(
        30, "--min-group", help="A person folder needs at least this many faces."),
    min_photos: int = typer.Option(
        10, "--min-photos",
        help="…across at least this many DIFFERENT photos (filters artwork/masks)."),
    keep_stale: bool = typer.Option(
        False, "--keep-stale",
        help="Leave entries the new plan no longer wants. Default is to remove "
             "them so the tree matches the plan instead of accumulating."),
    do_apply: bool = typer.Option(False, "--apply", help="Execute (default: plan only)."),
) -> None:
    """🗂️  Build the whole organised tree: year, event, person, place, screenshots.

    Photographs get the year/event/person/place views to themselves; screenshots,
    webcam frames, web graphics and documents each get their own top-level folder,
    so nothing is hidden and nothing pollutes the year folders.

    Everything is hardlinks — one file on disk with several names. The tree costs
    no space, lives where every scanner ignores it, and deleting it cannot lose a
    photo.

    Re-running SYNCS rather than rebuilds: entries already correct are left
    alone, only the difference is written. Renaming one event used to mean
    deleting 147,000 links and remaking them.
    """
    from navig_explore.vision import arrange as A, catalog, views as V  # noqa: PLC0415

    r = _vision_root(root, need_meta=False)
    if not catalog.db_path(r).exists():
        ch.error(f"No vision catalog yet. Run:  navig explore photos index {r}")
        raise typer.Exit(2)
    d = Path(dest).expanduser().resolve() if dest else V.default_dest(r)

    rows, summary = V.build_plan(r, d, min_group=min_group,
                                 min_distinct_photos=min_photos)
    if not rows:
        ch.error("Nothing to build — has `photos index` / `dates` / `faces` run?")
        raise typer.Exit(1)

    conn = catalog.connect(r)
    cov = V.coverage(conn, r, A.MIN_DATE_CONFIDENCE)
    ch.info(f"{len(rows):,} links across {len(summary)} views → {d}")
    for name, s in summary.items():
        ch.dim(f"  {name:<14} {s['links']:>7,} links   {s['folders']:>4} folders")
    # A view that silently omits is worse than one that explains why.
    ch.dim(f"  year views cover {cov['confident']:,} of {cov['dated']:,} dated photos — "
           f"the rest are dated too weakly to file under a year "
           f"(see them in `photos gallery`)")

    plan = V.write_plan(rows, r / ".mediaexplorer" / "views-plan.csv")
    ch.dim(f"plan → {plan}")
    if not do_apply:
        ch.info("PLAN ONLY — re-run with --apply to build it.")
        return

    log = V.default_log(r)
    stats = A.apply_plan(rows, log, dest=d, prune=not keep_stale)
    ch.success(f"{d}  ·  {stats}")
    if stats.get("pruned"):
        ch.dim(f"  {stats['pruned']:,} stale entries removed "
               f"({stats.get('dirs_removed', 0):,} folders); originals untouched")
    if stats.get("prune_failed"):
        ch.warning(f"  {stats['prune_failed']} stale entries could not be removed "
                   f"(a read-only original shares the attribute with every link)")
    if stats.get("copied"):
        ch.dim(f"  {stats['copied']} entries were COPIED (different volume) — those "
               f"do use extra disk space")

    # The tree describes itself, because the two things worth knowing about it are
    # invisible from inside it: which folders concentrate documents nobody should
    # sync, and which named occasions have no readable file left to show.
    # One query behind both numbers, so the warning and the file cannot disagree.
    ev = {"unviewable_events": len(V.lost_occasions(conn))}
    V.write_manifest(r, d, summary, ev)
    lost = V.write_lost_occasions(conn, r, d)
    ch.dim(f"  wrote {V.README_NAME}" + (f" and {lost.name}" if lost else ""))
    if ev["unviewable_events"]:
        ch.warning(f"  {ev['unviewable_events']} occasions you named have no readable file "
                f"left and so get no folder — names listed in {lost.name}")
    ch.warning(f"  ⚠ {d} concentrates sensitive documents "
            f"({', '.join(V.SENSITIVE)}) — do not sync or share this tree")
    ch.dim(f"remove it later:  navig explore photos views-remove \"{log}\"")


@photos_app.command("views-remove")
def photos_views_remove_cmd(
    log: str = typer.Argument(..., help="views-log-*.csv written by --apply."),
) -> None:
    """↩️  Delete the organised tree. Your photos are never affected."""
    from navig_explore.vision import arrange as A  # noqa: PLC0415

    p = Path(log).expanduser()
    if not p.exists():
        ch.error(f"No such log: {p}")
        raise typer.Exit(1)
    ch.success(f"removed: {A.remove(p)}")


@photos_app.command("arrange")
def photos_arrange_cmd(
    root: str = typer.Argument(..., help="Photo library root."),
    dest: str = typer.Argument(..., help="Where to build the folder tree."),
    by: str = typer.Option("person", "--by",
                           help="person | year | month | place | class."),
    min_conf: float = typer.Option(
        0.80, "--min-confidence",
        help="For --by year/month: how sure the date must be to file a photo."),
    no_copy: bool = typer.Option(
        False, "--no-copy",
        help="Fail instead of copying when a hardlink is impossible."),
    include_unnamed: bool = typer.Option(
        False, "--include-unnamed",
        help="--by person: also fold out groups you haven't named yet, as person-<id>."),
    min_group: int = typer.Option(
        1, "--min-group",
        help="--by person: skip groups smaller than this many faces."),
    force: bool = typer.Option(
        False, "--force",
        help="Allow a destination inside the library (it will corrupt re-indexing)."),
    do_apply: bool = typer.Option(False, "--apply", help="Execute (default: plan only)."),
) -> None:
    """🗂️  Folders per person, year, place or kind — as links, moving nothing.

    A photo of three people belongs in three person folders, and it has a year
    AND a place AND the people in it. Moving can only express one of those, so
    this builds hardlinks: one file on disk, several directory entries, no extra
    space, and your curated event folders are left exactly as they are.

    Delete the arranged tree whenever you like — `arrange-remove <log>` does it
    tidily, and the originals are untouched by construction.
    """
    from navig_explore.vision import arrange as A, catalog  # noqa: PLC0415

    r = _vision_root(root, need_meta=False)
    if not catalog.db_path(r).exists():
        ch.error(f"No vision catalog yet. Run:  navig explore photos index {r}")
        raise typer.Exit(2)
    if by not in A.FACETS:
        ch.error(f"Unknown --by {by!r}. Choose: {', '.join(A.FACETS)}")
        raise typer.Exit(2)

    d = Path(dest).expanduser().resolve()
    try:
        rows, summary = A.build_plan(r, d, by, min_confidence=min_conf, force=force,
                                     include_unnamed=include_unnamed,
                                     min_group=min_group)
    except A.DestinationInsideLibrary as exc:
        ch.error(str(exc))
        ch.dim("  (--force if you really mean it)")
        raise typer.Exit(2) from exc
    except ValueError as exc:
        ch.error(str(exc))
        raise typer.Exit(2) from exc

    if not rows:
        hint = {"person": "name some groups first:  photos people name <root> <id> <name>",
                "year": "run `photos dates` first, or lower --min-confidence",
                "month": "run `photos dates` first, or lower --min-confidence",
                "place": "run `photos places` first",
                "class": "run `photos index` first"}[by]
        ch.error(f"Nothing to arrange by {by} — {hint}")
        raise typer.Exit(1)

    ch.info(f"{len(rows)} links across {len(summary)} {by} folders")
    for k, v in sorted(summary.items(), key=lambda kv: -kv[1])[:12]:
        ch.dim(f"  {k:<28} {v:>6}")
    if len(summary) > 12:
        ch.dim(f"  … and {len(summary) - 12} more folders")

    plan = A.write_plan(rows, r / ".mediaexplorer" / f"arrange-{by}-plan.csv")
    ch.dim(f"plan → {plan}")
    if not do_apply:
        ch.info("PLAN ONLY — re-run with --apply to create the folders.")
        return

    import time as _t  # noqa: PLC0415
    log = r / ".mediaexplorer" / f"arrange-{by}-log-{_t.strftime('%Y%m%d-%H%M%S')}.csv"
    stats = A.apply_plan(rows, log, allow_copy=not no_copy)
    ch.success(f"{d}  ·  {stats}")
    if stats["copied"]:
        ch.dim(f"  {stats['copied']} entries had to be COPIED (different volume) — "
               f"those do use extra disk space")
    ch.dim(f"remove it later:  navig explore photos arrange-remove \"{log}\"")


@photos_app.command("arrange-remove")
def photos_arrange_remove_cmd(
    log: str = typer.Argument(..., help="arrange-*-log-*.csv written by --apply."),
) -> None:
    """↩️  Delete an arranged folder tree. Originals are never affected."""
    from navig_explore.vision import arrange as A  # noqa: PLC0415

    p = Path(log).expanduser()
    if not p.exists():
        ch.error(f"No such log: {p}")
        raise typer.Exit(1)
    ch.success(f"removed: {A.remove(p)}")


@photos_app.command("gallery")
def photos_gallery_cmd(
    root: str = typer.Argument(..., help="Photo library root."),
    out: str = typer.Option(None, "--out", "-o",
                            help="Output HTML (default: <root>/facets.html)."),
    limit: int = typer.Option(4000, "--limit", help="Max photos on the page."),
    workers: int = typer.Option(12, "--workers", "-w"),
) -> None:
    """🖼️  A browsable page of the library, filtered by person, year, place and kind.

    Everything shown comes from the catalog; originals are read only to make
    thumbnails. Dates appear with the evidence behind them and are dimmed when
    inferred, so a guess can never look like a measurement.
    """
    from navig_explore.vision import catalog, gallery as G  # noqa: PLC0415

    r = _vision_root(root, need_meta=False)
    if not catalog.db_path(r).exists():
        ch.error(f"No vision catalog yet. Run:  navig explore photos index {r}")
        raise typer.Exit(2)
    dest = Path(out).expanduser().resolve() if out else (r / "facets.html")
    page, stats = G.write_facets(r, dest, limit=limit, workers=workers)
    if not stats["shown"]:
        ch.error("Nothing to show — is the catalog indexed?")
        raise typer.Exit(1)
    ch.success(f"gallery → {page}")
    ch.dim(f"  {stats['shown']:,} of {stats['indexed']:,} photos · "
           f"{stats['people']} named people · {stats['years']} years · "
           f"{stats['places']} places")


@photos_app.command("places")
def photos_places_cmd(
    root: str = typer.Argument(..., help="Photo library root."),
    folder_names: bool = typer.Option(
        True, "--folder-names/--no-folder-names",
        help="Also match place names in human-named event folders."),
) -> None:
    """🌍  Work out where photos were taken — GPS, neighbours, then folder names.

    Reverse geocoding is fully offline (GeoNames + a k-d tree); nothing leaves
    this machine. Every row records whether the location was measured, inherited
    from a nearby photo, or guessed from a folder name.
    """
    from navig_explore.vision import catalog, places as PL  # noqa: PLC0415

    r = _vision_root(root, need_meta=False)
    if not catalog.db_path(r).exists():
        ch.error(f"No vision catalog yet. Run:  navig explore photos index {r}")
        raise typer.Exit(2)
    try:
        stats = PL.resolve(r, do_folder=folder_names)
    except Exception as exc:  # noqa: BLE001 - network/dataset problems must be actionable
        ch.error(f"Could not build the gazetteer: {exc}")
        raise typer.Exit(1) from exc
    ch.success(f"located {sum(stats.values())} photos")
    for k, v in stats.items():
        ch.dim(f"  {k:<12} {v}   confidence {PL.CONFIDENCE.get(k, 0):.2f}")


@photos_app.command("triage")
def photos_triage_cmd(
    root: str = typer.Argument(..., help="Photo library root."),
    date_root: str = typer.Option("By Date", "--date-root"),
    refile: bool = typer.Option(False, "--refile-dated",
                                help="Also file recovered-date photos into the date tree."),
    min_conf: float = typer.Option(0.80, "--min-confidence",
                                   help="Confidence a derived date needs before it may move a file."),
    only: str = typer.Option(None, "--only", help="Comma-separated actions to apply."),
    do_apply: bool = typer.Option(False, "--apply", help="Execute (default: plan only)."),
) -> None:
    """🗂️  Plan the split: real photos vs webcam vs screenshots vs web litter.

    Photographs are never moved by triage — where a real photo belongs is your
    call. Only litter is parked, and only recovered dates above --min-confidence
    may refile anything. Nothing is deleted; every applied run writes an undo log.
    """
    from navig_explore.vision import catalog, triage as T  # noqa: PLC0415

    r = _vision_root(root, need_meta=False)
    if not catalog.db_path(r).exists():
        ch.error(f"No vision catalog yet. Run:  navig explore photos index {r}")
        raise typer.Exit(2)

    rows, summary = T.build_plan(r, date_root=date_root, refile_dated=refile,
                                 min_confidence=min_conf)
    plan = T.write_plan(rows, T.default_plan_path(r))
    ch.info(f"{len(rows)} moves planned")
    for k, v in sorted(summary.items(), key=lambda kv: -kv[1]):
        ch.dim(f"  {k:<26} {v}")
    ch.dim(f"plan → {plan}")

    if not do_apply:
        ch.info("PLAN ONLY — review the CSV, then re-run with --apply.")
        return
    log = T.default_log_path(r)
    stats = T.apply_plan(rows, log,
                         only={s.strip() for s in only.split(",")} if only else None,
                         conn=catalog.connect(r))
    ch.success(f"moved: {stats}")
    ch.dim(f"reversible log → {log}   "
           f'(undo:  navig explore photos triage-undo "{log}")')


@photos_app.command("triage-undo")
def photos_triage_undo_cmd(
    log: str = typer.Argument(..., help="vision-triage-log-*.csv written by --apply."),
) -> None:
    """↩️  Put every file a triage run moved back where it came from."""
    from navig_explore.vision import catalog, triage as T  # noqa: PLC0415

    p = Path(log).expanduser()
    if not p.exists():
        ch.error(f"No such log: {p}")
        raise typer.Exit(1)
    # The log lives in <root>/.mediaexplorer, so the catalog is two levels up.
    root = p.parent.parent
    conn = catalog.connect(root) if catalog.db_path(root).exists() else None
    ch.success(f"undo: {T.undo(p, conn=conn)}")


@photos_app.command("status")
def photos_status_cmd(
    root: str = typer.Argument(..., help="Photo library root."),
) -> None:
    """📊  What the vision catalog currently knows about this library."""
    from navig_explore.vision import catalog  # noqa: PLC0415

    r = _vision_root(root, need_meta=False)
    if not catalog.db_path(r).exists():
        ch.error(f"No vision catalog yet. Run:  navig explore photos index {r}")
        raise typer.Exit(2)
    conn = catalog.connect(r)
    for k, v in catalog.counts(conn, r).items():
        ch.info(f"{k:<12} {v}")
    rows = conn.execute(
        "SELECT class, COUNT(*) n FROM classes GROUP BY class ORDER BY n DESC").fetchall()
    if rows:
        ch.dim("classes:")
        for row in rows:
            ch.dim(f"  {row['class']:<14} {row['n']}")


@explore_app.command("route")
def route_cmd(
    root: str = typer.Argument(..., help="Source root to migrate (e.g. Y:\\)."),
    dest: str = typer.Option(..., "--dest", help="Master library root (e.g. X:\\)."),
    do_apply: bool = typer.Option(False, "--apply", help="Execute copy-verify-trash (else plan-only)."),
    manifest: Path | None = typer.Option(None, "--manifest", help="Manifest CSV path (plan)."),
    limit: int | None = typer.Option(None, "--limit"),
    no_verify: bool = typer.Option(False, "--no-verify"),
) -> None:
    """🚚 Route media to the master library. Default: PLAN (manifest only). --apply to move."""
    from navig_explore import route as _route  # noqa: PLC0415
    r, d = Path(root).expanduser().resolve(), Path(dest).expanduser().resolve()
    if do_apply:
        ch.warning(f"APPLYING copy-verify-trash: {r} → {d}")
        _route.apply(r, d, limit=limit, verify=not no_verify)
    else:
        mani = manifest or r / ".mediaexplorer" / "route-plan.csv"
        stats = _route.plan(r, d, mani)
        ch.success(f"plan written: {stats}")


@explore_app.command("collect")
def collect_cmd(
    root: str = typer.Argument(..., help="Indexed library folder (e.g. X:\\Video)."),
    dest: str = typer.Argument(..., help="Working edit folder to fill with hardlinks."),
    res: str = typer.Option(None, "--res", help="Resolution tier: 4K|QHD|FHD|HD|SD."),
    source: str = typer.Option(None, "--source", help="Source: phone|camera|drone|screen|old."),
    year: int = typer.Option(None, "--year"),
    typ: str = typer.Option("video", "--type"),
) -> None:
    """🎯 Pull a themed selection (e.g. --res 4K --year 2024) into an edit folder via hardlinks (no copy)."""
    from navig_explore.collect import collect  # noqa: PLC0415
    s = collect(Path(root).expanduser(), Path(dest).expanduser(),
                res=res, source=source, year=year, typ=typ)
    ch.success(f"collected: {s}")


def _kv(pairs: list[str] | None, what: str, *, multi: bool = False):
    """Parse repeatable ``label=folder`` options.

    ``multi`` collects every folder given for a label (training pulls from several pure
    subfolders); otherwise the last one wins (a class has exactly one destination).
    """
    out: dict = {}
    for item in pairs or []:
        if "=" not in item:
            ch.error(f"--{what} wants label=folder, got: {item}")
            raise typer.Exit(2)
        k, v = item.split("=", 1)
        p = Path(v.strip()).expanduser()
        if multi:
            out.setdefault(k.strip(), []).append(p)
        else:
            out[k.strip()] = p
    return out


@explore_app.command("audio-sort")
def audio_sort_cmd(
    folder: str = typer.Argument(..., help="Folder of audio to classify (e.g. a TikTok rip dump)."),
    learn: list[str] = typer.Option(
        None, "--learn", help="label=folder — train from a folder you already sorted (repeatable)."),
    route: list[str] = typer.Option(
        None, "--route", help="label=folder — where that class should land (repeatable)."),
    model: Path = typer.Option(
        None, "--model", help="Model JSON to load (or write, when --learn is given)."),
    per_class: int = typer.Option(400, "--per-class", help="Training files sampled per label."),
    min_conf: float = typer.Option(0.6, "--min-conf", help="Below this, file goes to _review/."),
    min_dur: float = typer.Option(
        None, "--min-dur", help="Only touch files at least this many seconds long."),
    max_dur: float = typer.Option(
        None, "--max-dur", help="Only touch files at most this many seconds long."),
    keep: list[str] = typer.Option(
        None, "--keep", help="Leave this class where it is (repeatable)."),
    workers: int = typer.Option(8, "--workers", "-w"),
    limit: int | None = typer.Option(None, "--limit"),
    do_apply: bool = typer.Option(False, "--apply", help="Move files (default: plan only)."),
) -> None:
    """🎧 Sort audio by CONTENT — music / voice / sfx — from the waveform, not the filename.

    Trains on folders you already sorted, reports cross-validated accuracy, then plans a
    move per file. Low-confidence files go to _review/ instead of being guessed at.

    `--min-dur` / `--max-dur` / `--keep` narrow what the run may touch, turning a
    whole-folder reorganisation into a targeted cleanup — e.g. pull the hour-long albums
    out of a sound-effects library and leave the actual sound effects alone.
    """
    from navig_explore import audio_class as ac  # noqa: PLC0415
    from navig_explore import audio_sort as asort  # noqa: PLC0415

    root = Path(folder).expanduser()
    if not root.is_dir():
        ch.error(f"Not a directory: {root}")
        raise typer.Exit(1)
    side = root / ".mediaexplorer"
    side.mkdir(parents=True, exist_ok=True)
    model_path = model or (side / "audio-model.json")

    if learn:
        spec = _kv(learn, "learn", multi=True)
        ch.info(f"Training on {len(spec)} labels: {', '.join(spec)}")
        m = ac.learn_from_folders(spec, per_class=per_class, workers=workers, cache=side)
        model_path.write_text(json.dumps(m), encoding="utf-8")
        ch.success(f"cross-validated accuracy {m['cv_accuracy']:.1%}  "
                   + "  ".join(f"{k} {v:.0%}" for k, v in m["cv_recall"].items()))
        ch.dim(f"model → {model_path}")
    elif not model_path.exists():
        ch.error("No model. Train one first:  --learn music=<folder> --learn voice=<folder>")
        raise typer.Exit(2)
    m = json.loads(model_path.read_text(encoding="utf-8"))

    recs = ac.classify_folder(root, m, workers=workers, limit=limit, cache=side)
    routes = _kv(route, "route")
    rows = asort.plan(recs, routes, min_conf=min_conf, review_root=root / "_review",
                      min_dur=min_dur, max_dur=max_dur, keep=set(keep or []))
    plan_csv = asort.write_plan(rows, side / "audio-sort-plan.csv")
    s = asort.summarize(rows)
    scope = f" · left in place: {s['left_in_place']}" if s.get("left_in_place") else ""
    ch.info(f"{s['total']} files — filed: {s['filed']} · review: {s['review_total']}{scope}")
    ch.dim(f"plan → {plan_csv}")

    if not do_apply:
        ch.info("PLAN ONLY — re-run with --apply to move.")
        return
    if not routes:
        ch.error("--apply needs at least one --route label=folder")
        raise typer.Exit(2)
    log = side / "audio-sort-log.jsonl"
    stats = asort.apply(rows, log_path=log)
    ch.success(f"moved: {stats}")
    ch.dim(f"reversible log → {log}   (undo:  navig explore audio-undo {log})")


@explore_app.command("audio-undo")
def audio_undo_cmd(
    log: str = typer.Argument(..., help="audio-sort-log.jsonl written by --apply."),
) -> None:
    """↩️  Put every file an audio-sort run moved back where it came from."""
    from navig_explore import audio_sort as asort  # noqa: PLC0415
    p = Path(log).expanduser()
    if not p.exists():
        ch.error(f"No such log: {p}")
        raise typer.Exit(1)
    ch.success(f"undo: {asort.undo(p)}")


@explore_app.command("merge-episode")
def merge_episode_cmd(
    folder: str = typer.Argument(..., help="Episode folder of source clips to merge into one RAW file."),
    out: Path = typer.Option(None, "--out", help="Output file (default: <parent>/_merged/<name>.mp4)."),
) -> None:
    """🎬 Merge an episode's clips into one lossless RAW file (MPEG-TS concat; skips corrupt clips)."""
    from navig_explore.merge import merge_episode  # noqa: PLC0415
    res = merge_episode(Path(folder).expanduser(), out)
    (ch.success if res.get("ok") else ch.error)(f"merge: {res}")
