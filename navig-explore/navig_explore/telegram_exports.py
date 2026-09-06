"""navig telegram-exports — organize Telegram *export* folders (batch/offline).

Telegram Desktop's "Export chat history" produces ``ChatExport_YYYY-MM-DD``
folders (HTML and/or ``result.json`` + media). Dropped into one archive they
pile up unsorted. This command files them into a stable scheme, audits the
result, matches stray downloads back to their source chat, and removes verified
duplicates:

    <root>/<Category>/<Chat name>/<YYYY-MM-DD>/…

It is the **offline** counterpart to the live MTProto "Telegram Manager"
(``navig telegram …``): that one talks to your account over the network; this
one only touches export folders on disk. Category is derived from the chat
``type`` (personal_chat→People, bot_chat→Bots, *channel*→Channels,
*supergroup/group*→Groups). Identity comes from ``result.json`` when present;
older HTML-only exports fall back to service-message heuristics.

Every move appends to ``<root>/_reorg-log.tsv`` (a reversible audit trail);
deletions append to ``<root>/_deletions-log.tsv``. Path-agnostic (``--root``),
dry-run by default. Distilled from the field-tested reorg of a 34-chat archive.
"""

from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone
from pathlib import Path

import typer
from rich.console import Console

console = Console()

_MEDIA_DIRS = (
    "files", "photos", "video_files", "voice_messages",
    "round_video_messages", "stickers", "animations", "images",
)
_CATEGORIES = ("People", "Bots", "Channels", "Groups", "Unsorted")
_CHAT_CATEGORIES = ("People", "Bots", "Channels", "Groups")
_DATE_RE = re.compile(r"(\d{4}-\d{2}-\d{2})")
_INVALID = re.compile(r'[:*?"<>|]')
_HREF_RE = re.compile(
    r"(?:files|photos|video_files|voice_messages|round_video_messages|stickers|animations|images)/([^\"']+)"
)


# --------------------------------------------------------------------------- #
# Classification
# --------------------------------------------------------------------------- #
def _category(chat_type: str) -> str:
    t = (chat_type or "").lower()
    if "bot_chat" in t:
        return "Bots"
    if "personal_chat" in t:
        return "People"
    if "channel" in t:
        return "Channels"
    if "supergroup" in t or "group" in t:
        return "Groups"
    return "Unsorted"


def _safe_name(name: str) -> str:
    name = re.sub(r"[\\/]", " - ", name or "")
    name = _INVALID.sub("", name)
    name = re.sub(r"\s+", " ", name)
    return name.strip().rstrip(". ")


def classify_export(folder: Path) -> dict | None:
    """Identify one export folder. Returns dict(name, type, category, id, source)
    or None if the folder holds no recognisable export."""
    j = folder / "result.json"
    if j.exists():
        head = j.read_bytes()[:4096].decode("utf-8", "replace")
        m_type = re.search(r'"type":\s*"([^"]*)"', head)
        if m_type:
            m_name = re.search(r'"name":\s*"((?:[^"\\]|\\.)*)"', head)
            m_id = re.search(r'"id":\s*(\d+)', head)
            t = m_type.group(1)
            return {
                "name": m_name.group(1) if m_name else "",
                "type": t, "category": _category(t),
                "id": m_id.group(1) if m_id else "", "source": "json",
            }
    main = folder / "messages.html"
    if not main.exists():
        return None
    # service messages are authoritative and can appear late -> scan all parts (capped)
    svc: list[str] = []
    for mf in sorted(folder.glob("messages*.html")):
        chunk = mf.open("rb").read(8 * 1024 * 1024).decode("utf-8", "replace")
        svc += [
            re.sub(r"<[^>]+>", "", g).strip()
            for g in re.findall(
                r'<div class="message service"[^>]*>\s*<div class="body details">\s*(.*?)\s*</div>',
                chunk, re.S)
        ]
    svc_join = " ~ ".join(sorted(set(svc)))
    t = main.read_bytes()[:800_000].decode("utf-8", "replace")
    m_title = re.search(r'<div class="text bold">\s*(.*?)\s*</div>', t, re.S)
    name = re.sub(r"<[^>]+>", "", m_title.group(1)).strip() if m_title else folder.name
    senders = {
        s.strip() for s in re.findall(r'<div class="from_name">\s*([^<]+?)\s*(?:<|</div>)', t)
        if s.strip()
    }
    # order: channel-exclusive, then private-exclusive, then group, then count.
    # markers are case-insensitive — Telegram writes both "Channel «X» created"
    # and lowercase "created group «X» with members".
    if re.search(r"channel &laquo;|channel «|created channel", svc_join, re.I):
        ctype = "channel"
    elif re.search(r"sent you a gift|changed chat theme|set a new background|joined Telegram",
                   svc_join, re.I):
        ctype = "personal_chat"
    elif re.search(r"group &laquo;|group «|created group|created the group|migrated|invited|"
                   r"joined the group|group photo|converted .*group", svc_join, re.I):
        ctype = "supergroup"
    elif len(senders) >= 3:
        ctype = "supergroup"
    else:
        ctype = "personal_chat"
    return {"name": name, "type": ctype, "category": _category(ctype),
            "id": "", "source": "html"}


# --------------------------------------------------------------------------- #
# Shared helpers
# --------------------------------------------------------------------------- #
def _ts() -> str:
    return datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%dT%H:%M:%S")


def _log(root: Path, src: Path, dest: Path) -> None:
    with (root / "_reorg-log.tsv").open("a", encoding="utf-8") as fh:
        fh.write(f"{_ts()}\t{src}\t{dest}\n")


def _resolve_final(path: Path, moves: list[tuple[str, str]]) -> Path | None:
    """Follow the move chain: a logged dest that's gone may have been moved again."""
    cur = str(path)
    for _ in range(20):
        if Path(cur).exists():
            return Path(cur)
        nxt = None
        for src, dest in moves:
            if cur == src:
                nxt = dest
                break
            if cur.startswith(src + "\\") or cur.startswith(src + "/"):
                nxt = dest + cur[len(src):]
                break
        if nxt is None:
            return None
        cur = nxt
    return None


def _norm(name: str) -> str:
    """Normalise a loose filename: strip Windows ' (2)' dedup + '__<n>' collision suffixes."""
    stem = Path(name).stem
    stem = re.sub(r"__\d+$", "", stem)
    stem = re.sub(r"__dup$", "", stem)
    stem = re.sub(r"\s*\(\d+\)$", "", stem)
    return (stem + Path(name).suffix).lower()


def _chat_roots(root: Path) -> dict[str, Path]:
    """Map chat-folder leaf name -> its full path, across all chat categories."""
    out: dict[str, Path] = {}
    for cat in _CHAT_CATEGORIES:
        cdir = root / cat
        if cdir.is_dir():
            for chat in cdir.iterdir():
                if chat.is_dir():
                    out[chat.name] = chat
    return out


def _build_name_index(root: Path) -> tuple[dict, dict]:
    """Index every export's original filenames -> {chat labels}. Returns
    (name_only_index, name_plus_size_index). Account exports excluded."""
    name_idx: dict[str, set[str]] = {}
    size_idx: dict[str, set[str]] = {}

    def add(idx, key, label):
        idx.setdefault(key, set()).add(label)

    for cat in _CHAT_CATEGORIES:
        cdir = root / cat
        if not cdir.is_dir():
            continue
        for chat in cdir.iterdir():
            if not chat.is_dir():
                continue
            label = f"{cat}/{chat.name}"
            for ver in chat.iterdir():
                if not ver.is_dir():
                    continue
                j = ver / "result.json"
                if j.exists() and j.stat().st_size <= 60 * 1024 * 1024:
                    txt = j.read_text(encoding="utf-8", errors="replace")
                    for m in re.finditer(
                        r'"file_name":\s*"((?:[^"\\]|\\.)*)"[^}]*?"file_size":\s*(\d+)', txt, re.S
                    ):
                        nm = re.sub(r"\\(.)", r"\1", m.group(1)).lower()
                        add(name_idx, nm, label)
                        add(size_idx, f"{nm}|{m.group(2)}", label)
                    for m in re.finditer(r'"file_name":\s*"((?:[^"\\]|\\.)*)"', txt):
                        add(name_idx, re.sub(r"\\(.)", r"\1", m.group(1)).lower(), label)
                for mf in ver.glob("messages*.html"):
                    html = mf.read_bytes()[:8 * 1024 * 1024].decode("utf-8", "replace")
                    for m in _HREF_RE.finditer(html):
                        add(name_idx, Path(m.group(1)).name.lower(), label)
    return name_idx, size_idx


def _find_in_export(chat_root: Path, norm_name: str) -> Path | None:
    """Locate a media file by (normalised) name inside a chat's export media dirs."""
    for ver in chat_root.iterdir():
        if not ver.is_dir():
            continue
        for md in _MEDIA_DIRS:
            cand = ver / md / norm_name
            if cand.exists():
                return cand
    return None


def _md5(path: Path) -> str:
    h = hashlib.md5()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


# --------------------------------------------------------------------------- #
# Typer surface — a plugin top-level command: navig telegram-exports …
# --------------------------------------------------------------------------- #
telegram_app = typer.Typer(
    name="telegram-exports",
    help="📁 Organize Telegram export folders (ChatExport_*) into a tidy, audited archive.",
    no_args_is_help=True,
)

_ROOT = typer.Option(Path.cwd, "--root", "-r", help="Archive root (default: cwd).")


@telegram_app.command("organize")
def organize(
    root: Path = _ROOT,
    apply: bool = typer.Option(False, "--apply", help="Perform moves (default: dry-run)."),
):
    """File loose ``ChatExport_*`` folders at ROOT into ``Category/Chat/Date``."""
    root = root.resolve()
    exports = sorted(p for p in root.iterdir() if p.is_dir() and p.name.startswith("ChatExport_"))
    if not exports:
        console.print("No loose [bold]ChatExport_*[/] folders at root.")
        raise typer.Exit()
    filed = 0
    for e in exports:
        ident = classify_export(e)
        if not ident or ident["category"] == "Unsorted":
            console.print(f"[yellow]SKIP (unclassified):[/] {e.name}")
            continue
        m = _DATE_RE.search(e.name)
        date = m.group(1) if m else "undated"
        parent = root / ident["category"] / _safe_name(ident["name"])
        dest = parent / date
        if dest.exists():
            console.print(f"[yellow]SKIP (exists):[/] {dest.relative_to(root)}")
            continue
        rel = f"{ident['category']}/{_safe_name(ident['name'])}/{date}"
        if apply:
            parent.mkdir(parents=True, exist_ok=True)
            e.rename(dest)
            _log(root, e, dest)
            console.print(f"[green]FILED:[/] {e.name} → {rel}")
            filed += 1
        else:
            console.print(f"WOULD FILE: {e.name}  ({ident['type']}/{ident['source']}) → {rel}")
    console.print("\n[cyan](dry-run — re-run with --apply to move)[/]" if not apply
                  else f"\nFiled {filed} export(s).")


@telegram_app.command("audit")
def audit(root: Path = _ROOT):
    """Chained-move-aware integrity check: prove no export was lost."""
    root = root.resolve()
    log = root / "_reorg-log.tsv"
    moves: list[tuple[str, str]] = []
    if log.exists():
        for line in log.read_text(encoding="utf-8").splitlines():
            p = line.split("\t")
            if len(p) < 3:
                continue
            if any(c in p[1] or c in p[2] for c in "*<") or re.search(r"\(\d+ .*files\)", p[1]):
                continue  # batch/summary line, not an atomic move
            moves.append((p[1], p[2]))
    lost = [d for _, d in moves if not _resolve_final(Path(d), moves)]
    console.print(f"Folder moves logged: {len(moves)}")
    style = "red" if lost else "green"
    console.print(f"[{style}]Unresolved (possible data loss): {len(lost)}[/]")
    for d in lost:
        console.print(f"   [red]LOST:[/] {d}")
    for cat in _CATEGORIES:
        cdir = root / cat
        if not cdir.is_dir():
            continue
        for chat in cdir.iterdir():
            if not chat.is_dir():
                continue
            for ver in chat.iterdir():
                if ver.is_dir() and not (ver / "result.json").exists() \
                        and not (ver / "messages.html").exists():
                    console.print(f"   [yellow]NO-CONTENT:[/] {cat}/{chat.name}/{ver.name}")
    console.print("[green]Audit OK[/]" if not lost else "[red]Audit found issues[/]")
    raise typer.Exit(1 if lost else 0)


@telegram_app.command("verify")
def verify(root: Path = _ROOT):
    """Re-classify filed exports and report any that no longer match their category."""
    root = root.resolve()
    drift = 0
    for cat in _CHAT_CATEGORIES:
        cdir = root / cat
        if not cdir.is_dir():
            continue
        for chat in cdir.iterdir():
            if not chat.is_dir():
                continue
            vers = [v for v in chat.iterdir() if v.is_dir()]
            if not vers:
                continue
            ver = next((v for v in vers if (v / "result.json").exists()), None) or max(
                vers, key=lambda v: sum(f.stat().st_size for f in v.glob("messages*.html")),
                default=vers[0])
            ident = classify_export(ver)
            if ident and ident["category"] not in ("Unsorted", cat):
                drift += 1
                console.print(f"   [yellow]DRIFT:[/] {cat}/{chat.name} → {ident['category']} "
                              f"({ident['type']}/{ident['source']})")
    style = "yellow" if drift else "green"
    console.print(f"[{style}]Classifier drift: {drift}[/]")


@telegram_app.command("match")
def match(
    root: Path = _ROOT,
    staging: Path = typer.Option(None, "--staging", help="Loose-file dir (default: <root>/_staging/by-type)."),
    apply: bool = typer.Option(False, "--apply", help="Group matches under <staging>/../matched/<chat>/."),
):
    """Pair loose downloaded files back to the source chat by original name (+ exact size)."""
    root = root.resolve()
    staging = (staging or root / "_staging" / "by-type").resolve()
    if not staging.is_dir():
        console.print(f"[red]No staging dir:[/] {staging}")
        raise typer.Exit(1)
    name_idx, size_idx = _build_name_index(root)
    loose = [p for p in staging.rglob("*") if p.is_file()]
    strong, weak = [], []
    for f in loose:
        key = _norm(f.name)
        sk = f"{key}|{f.stat().st_size}"
        if sk in size_idx:
            strong.append((f, sorted(size_idx[sk])))
        elif key in name_idx:
            weak.append((f, sorted(name_idx[key])))
    console.print(f"Loose files: {len(loose)}  |  [green]strong (name+size): {len(strong)}[/]  |  "
                  f"[yellow]name-only: {len(weak)}[/]  |  unmatched: {len(loose) - len(strong) - len(weak)}")
    matched_root = staging.parent / "matched"
    moved = 0
    for f, chats in strong:
        console.print(f"  [green]STRONG[/] {f.name} → {chats[0]}" + (" (+more)" if len(chats) > 1 else ""))
        if apply and len(chats) == 1:
            dest_dir = matched_root / chats[0].split("/")[-1]
            dest_dir.mkdir(parents=True, exist_ok=True)
            target = dest_dir / f.name
            if target.exists():  # never clobber a same-named file already grouped here
                target = dest_dir / f"{f.stem}__{f.stat().st_size}{f.suffix}"
            f.rename(target)
            moved += 1
    for f, chats in weak[:40]:
        console.print(f"  [yellow]name[/]  {f.name} → {'; '.join(chats)}")
    if len(weak) > 40:
        console.print(f"  … +{len(weak) - 40} more name-only matches")
    if apply:
        console.print(f"\nGrouped {moved} strong single-chat match(es) under matched/.")
    else:
        console.print("\n[cyan](report only — re-run with --apply to group strong matches)[/]")


@telegram_app.command("dedupe")
def dedupe(
    root: Path = _ROOT,
    matched: Path = typer.Option(None, "--matched", help="Dir to scan (default: <root>/_staging/matched)."),
    confirm: bool = typer.Option(False, "--confirm", help="Delete the verified byte-duplicates."),
):
    """Find loose files that are byte-identical to a copy already inside their export.

    Dry-run lists DUPLICATE (safe to delete) vs ONLY-COPY (keep). ``--confirm``
    re-hashes each at delete time and removes only proven duplicates, logging to
    ``_deletions-log.tsv``."""
    root = root.resolve()
    matched = (matched or root / "_staging" / "matched").resolve()
    if not matched.is_dir():
        console.print(f"[red]No matched dir:[/] {matched}")
        raise typer.Exit(1)
    chat_roots = _chat_roots(root)
    dup, only, differ, scanned = [], 0, 0, 0
    for f in matched.rglob("*"):
        if not f.is_file():
            continue
        scanned += 1
        leaf = f.relative_to(matched).parts[0]
        chat_root = chat_roots.get(leaf)
        in_export = _find_in_export(chat_root, _norm(f.name)) if chat_root else None
        if not in_export:
            only += 1
            continue
        if _md5(f) == _md5(in_export):
            dup.append((f, in_export))
        else:
            differ += 1
    console.print(f"Scanned {scanned} files — "
                  f"[green]DUPLICATE: {len(dup)}[/]  ONLY-COPY: {only}  name-same/bytes-differ: {differ}")
    for f, keep in dup[:30]:
        console.print(f"  [green]DUP[/] {f.relative_to(matched)}  ⇐keeps⇒ {keep.relative_to(root)}")
    if len(dup) > 30:
        console.print(f"  … +{len(dup) - 30} more")
    if not confirm:
        if dup:
            console.print(f"\n[cyan](dry-run — re-run with --confirm to delete {len(dup)} verified duplicate(s))[/]")
        return
    del_log = root / "_deletions-log.tsv"
    if not del_log.exists():
        del_log.write_text("timestamp\tdeleted_file\tverified_duplicate_of\tmd5\n", encoding="utf-8")
    deleted = 0
    with del_log.open("a", encoding="utf-8") as fh:
        for f, keep in dup:
            if not f.exists() or not keep.exists():
                continue
            h = _md5(f)
            if h != _md5(keep):   # re-verify at delete time
                continue
            fh.write(f"{_ts()}\t{f}\t{keep}\t{h}\n")
            f.unlink()
            deleted += 1
    console.print(f"\n[green]Deleted {deleted} verified duplicate(s)[/] (logged to _deletions-log.tsv).")
