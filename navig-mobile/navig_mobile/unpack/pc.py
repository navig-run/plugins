"""PC drop-folder ingest: group archives into games, extract, bucket, localize.

A drop folder (e.g. ``D:\\pc_games_extract``) holds one flat layer of game archives —
multi-volume sets, plain archives, ISOs, installer EXEs. This module turns it into
per-game folders without ever moving or renaming an original archive:

  <root>/_games/<Game>/data      the extracted game tree (videos excluded by default)
  <root>/_games/<Game>/extract   readable exports: text/, data/, loc/<lang>/
  <root>/_games/<Game>/media     images, audio (+ .ogg conversions), fonts, models
  <root>/_games/<Game>/REPORT.md per-game report
  <root>/_manifest.json          resumable state for every game (ingest → run)

Stages: ``ingest()`` classifies (read-only), ``extract_one()`` unpacks one game into
``data.partial`` and renames it to ``data`` on success (an interrupted game is simply
re-done on the next run), ``run_one()`` runs the shared extraction engine plus the
language index. Extraction never executes installer payloads unless ``allow_install``.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Callable, Optional

from navig_sdk.host import decode_console_result

from navig_mobile.unpack import localization, pipeline, tools, unity, xnb

Log = Callable[[str], None]

MANIFEST_NAME = "_manifest.json"
GAMES_DIR = "_games"
VOLUME_RE = re.compile(r"^(?P<stem>.+)\.(?P<part>\d{3})$")
ARCHIVE_EXT = {".7z", ".zip", ".rar", ".iso", ".tar", ".gz", ".bz2", ".xz"}
INSTALLER_EXT = {".exe", ".msi"}
SKIP_EXT = {".apk", ".ipa", ".dmg", ".xapk", ".aab", ".obb"}
CONSOLE_PREFIXES = ("[ps vita", "[ps4", "[ps3", "[psp", "[x360", "[xbox", "[nintendo",
                    "[3ds", "[wii", "[switch")
# Video stays out of the extraction entirely (owner decision: everything but video).
VIDEO_EXT = {".mp4", ".avi", ".mkv", ".webm", ".mov", ".wmv", ".mpg", ".mpeg", ".m4v",
             ".bik", ".bk2", ".usm", ".roq", ".smk", ".ogv", ".flv", ".asf", ".3gp",
             ".vob", ".m2ts", ".mts"}
PACKER_TAG = re.compile(r"[-_.\s]*RSLOAD\.NET[-_.\s]*", re.I)
DUP_MARKER = re.compile(r"\s*\([1-9]\)(?=\s|\.|$)")
DONE = ("done", "duplicate", "skipped")


# ── naming ──────────────────────────────────────────────────────────────────

def clean(stem: str) -> str:
    """Readable game name: drop packer decoration and ``(1)`` download markers."""
    s = PACKER_TAG.sub(" ", stem)
    s = DUP_MARKER.sub("", s)
    s = s.replace("_", " ")
    if " " not in s:
        s = s.replace(".", " ")
    s = re.sub(r"\s+", " ", s).strip(" .-_")
    return s or stem


def dup_key(name: str) -> str:
    return re.sub(r"[^0-9a-zа-яё]+", "", name.lower())


def head_hash(path: Path, limit: int = 65536) -> str:
    with open(path, "rb") as fh:
        return hashlib.sha256(fh.read(limit)).hexdigest()


def safe_name(name: str) -> str:
    return unity.safe(name)


# ── classification (read-only) ──────────────────────────────────────────────

def classify(root: Path) -> list[dict]:
    """Every top-level file → one game record (volume sets joined, junk flagged)."""
    groups: dict[str, dict] = {}
    for f in sorted(root.iterdir()):
        if not f.is_file():
            continue
        ext = f.suffix.lower()
        m = VOLUME_RE.match(f.name)
        if m and int(m.group("part")) >= 1:
            stem = re.sub(r"\.(7z|zip|rar|tar|gz|bz2|xz)$", "", m.group("stem"), flags=re.I)
            rec = groups.setdefault(stem, {"name": clean(stem), "kind": "volume",
                                           "parts": [], "size": 0})
            rec["parts"].append(f.name)
            rec["size"] += f.stat().st_size
        elif ext in SKIP_EXT:
            groups[f.name] = {"name": clean(f.stem), "kind": "skip", "parts": [f.name],
                              "size": f.stat().st_size, "reason": f"not a Windows game ({ext})"}
        elif f.name.lower().startswith(CONSOLE_PREFIXES):
            groups[f.name] = {"name": clean(f.stem), "kind": "skip", "parts": [f.name],
                              "size": f.stat().st_size, "reason": "console platform"}
        elif ext in ARCHIVE_EXT:
            groups[f.name] = {"name": clean(f.stem), "kind": "iso" if ext == ".iso" else "archive",
                              "parts": [f.name], "size": f.stat().st_size}
        elif ext in INSTALLER_EXT:
            groups[f.name] = {"name": clean(f.stem), "kind": "installer",
                              "parts": [f.name], "size": f.stat().st_size}
        else:
            groups[f.name] = {"name": clean(f.stem), "kind": "skip", "parts": [f.name],
                              "size": f.stat().st_size, "reason": f"unhandled extension ({ext})"}

    out = list(groups.values())
    for rec in out:
        rec["parts"].sort(key=lambda n: int(VOLUME_RE.match(n).group("part"))
                          if VOLUME_RE.match(n) else 0)
        if rec["kind"] == "volume" and not any(VOLUME_RE.match(p) and
                                               int(VOLUME_RE.match(p).group("part")) == 1
                                               for p in rec["parts"]):
            rec["kind"], rec["reason"] = "skip", "incomplete volume set (no .001)"
    _dedupe(out, root)
    _unique_names(out)
    return out


def _dedupe(recs: list[dict], root: Path) -> None:
    """Exact duplicate downloads (``Game (1).zip``) → ``duplicate_of`` the larger original."""
    by_key: dict[str, list[dict]] = {}
    for rec in recs:
        if rec["kind"] == "skip":
            continue
        by_key.setdefault(dup_key(rec["name"]), []).append(rec)
    for group in by_key.values():
        if len(group) < 2:
            continue
        # bigger first; on a tie the original (no ``(1)`` marker) wins
        group.sort(key=lambda r: (any(" (1)" in p or "(1)" in p for p in r["parts"]), -r["size"]))
        primary = group[0]
        for other in group[1:]:
            a, b = root / primary["parts"][0], root / other["parts"][0]
            if a.stat().st_size == b.stat().st_size and head_hash(a) == head_hash(b):
                other["kind"], other["duplicate_of"] = "skip", primary["name"]
                other["reason"] = f"duplicate download of “{primary['name']}”"


def _unique_names(recs: list[dict]) -> None:
    seen: dict[str, int] = {}
    # primaries (non-duplicates) claim the plain name first; duplicates get the suffix
    for rec in sorted(recs, key=lambda r: (bool(r.get("duplicate_of")), r["name"])):
        base = rec["name"]
        n = seen.get(dup_key(base), 0)
        seen[dup_key(base)] = n + 1
        if n:
            rec["name"] = f"{base} [{n + 1}]"


# ── manifest ────────────────────────────────────────────────────────────────

def load_manifest(root: Path) -> dict:
    path = root / MANIFEST_NAME
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except ValueError:
            pass
    return {"created": time.strftime("%Y-%m-%d %H:%M"), "games": {}}


def save_manifest(root: Path, manifest: dict) -> None:
    manifest["updated"] = time.strftime("%Y-%m-%d %H:%M:%S")
    path = root / MANIFEST_NAME
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(manifest, indent=1, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def ingest(root: Path, log: Log = print) -> dict:
    """Classify the drop folder and merge into ``_manifest.json`` (read-only otherwise)."""
    root = root.resolve()
    recs = classify(root)
    manifest = load_manifest(root)
    games = manifest.setdefault("games", {})
    fresh: dict[str, dict] = {}
    for rec in recs:
        if rec["kind"] == "skip":
            status = "duplicate" if rec.get("duplicate_of") else "skipped"
        elif rec.get("reason"):
            status = "incomplete"
        else:
            status = "pending"
        parts_key = ",".join(rec["parts"])
        rec["parts_key"] = parts_key
        rec["status"] = status
        old = games.get(rec["name"])
        if old and old.get("parts_key") == parts_key and old.get("status") in \
                ("done", "failed", "needs-install"):
            # same sources, already processed once — keep the established outcome
            rec.update({k: old[k] for k in ("status", "method", "error", "finished")
                        if k in old})
        fresh[rec["name"]] = rec
    # keep records for files that vanished (marked, not deleted)
    for name, old in games.items():
        if name not in fresh:
            old["status"] = "missing-source"
            fresh[name] = old
    manifest["games"] = fresh
    save_manifest(root, manifest)
    counts: dict[str, int] = {}
    for rec in fresh.values():
        counts[rec["status"]] = counts.get(rec["status"], 0) + 1
    log(f"ingest: {len(fresh)} groups — " +
        " · ".join(f"{v} {k}" for k, v in sorted(counts.items())))
    return manifest


# ── extraction ──────────────────────────────────────────────────────────────

def _video_excludes() -> list[str]:
    out = []
    for ext in sorted(VIDEO_EXT):
        out += [f"-x!*{ext}", f"-x!*{ext.upper()}"]
    return out


def _run(cmd: list[str], timeout: int = 6 * 3600) -> tuple[int, str]:
    """Run a tool and decode its output (UTF-8 first, then the console page — 7z, innoextract)."""
    try:
        p = decode_console_result(subprocess.run(cmd, capture_output=True, timeout=timeout))
        return p.returncode, ((p.stdout or "") + (p.stderr or ""))[-4000:]
    except subprocess.TimeoutExpired:
        return 124, "timeout"
    except OSError as exc:
        return 127, str(exc)


# A single wrapping folder is hoisted, but a folder that IS game content never is.
WRAPPER_GUARD = {"localization", "locale", "locales", "lang", "language", "languages",
                 "data", "assets", "content", "resources", "media", "textures", "sounds",
                 "audio", "images", "fonts", "saved", "savegames", "saves"}


def _flatten(target: Path) -> None:
    """Hoist a single wrapping folder (``Game/…`` → ``…``), at most 2 levels, never content dirs.

    The wrapper is renamed aside first: an archive whose folder nests a same-named child
    (``Game/Game/…``) would otherwise rename a directory onto itself (WinError 183).
    """
    for _ in range(2):
        children = list(target.iterdir())
        if len(children) != 1 or not children[0].is_dir() or \
                children[0].name.lower() in WRAPPER_GUARD:
            break
        inner = children[0]
        tmp = target / f".wrap-{inner.name}"
        if tmp.exists():  # leftover from an earlier crash — stop, never delete content
            break
        try:
            inner.rename(tmp)
        except OSError:
            break
        failed = False
        for c in list(tmp.iterdir()):
            try:
                c.rename(target / c.name)
            except OSError:  # keep the child under the wrapper; the loop then stops
                failed = True
        if failed:
            if not (target / inner.name).exists():
                tmp.rename(target / inner.name)
            break
        try:
            tmp.rmdir()  # empty now
        except OSError:
            break


def _purge_videos(target: Path) -> int:
    n = 0
    for f in target.rglob("*"):
        if f.is_file() and f.suffix.lower() in VIDEO_EXT:
            try:
                f.chmod(0o666)  # archives carry read-only attributes; unlink refuses those
                f.unlink(missing_ok=True)
                n += 1
            except OSError:  # a locked/read-only video stays; never fail the game over it
                pass
    return n


# Site-branded protection on repack zips (the zip comment carries the site name). Harmless
# on unencrypted archives — 7-Zip ignores -p when there is nothing to decrypt.
ZIP_PASSWORDS = ["RSLOAD.NET", "-RSLOAD.NET-", "rsload.net", "www.rsload.net", "rsload"]


def _extract_nested(target: Path, skip_video: bool, log: Log) -> int:
    """Archives inside the extracted tree (dump-in-archive) are unpacked too, up to 3 levels.

    Installer EXEs whose name says so (``setup_*``) are tried with innoextract only — it never
    executes anything; on failure the file stays put. Nested volumes are joined at ``.001``.
    """
    seven = tools.find("7z")
    if not seven:
        return 0
    inno = tools.find("innoextract")
    extracted = 0
    done: set[Path] = set()
    for _ in range(3):
        nested: list[Path] = []
        for f in sorted(target.rglob("*")):
            if not f.is_file() or f in done:
                continue
            m = VOLUME_RE.match(f.name)
            if m:
                if int(m.group("part")) == 1:  # join at the first part; 7z auto-finds the rest
                    nested.append(f)
            elif f.suffix.lower() in ARCHIVE_EXT:
                nested.append(f)
            elif inno and re.match(r"(setup|install)", f.stem, re.I) and f.suffix.lower() == ".exe":
                nested.append(f)
        if not nested:
            break
        for f in nested:
            done.add(f)
            name = re.sub(r"\.\d{3}$", "", f.name)
            base = re.sub(r"\.(7z|zip|rar|iso|tar|gz|bz2|xz|exe)$", "", name, flags=re.I)
            out = f.parent / unity.safe(base)
            n = 1
            while out.exists():
                n += 1
                out = f.parent / unity.safe(f"{base} ({n})")
            if f.suffix.lower() == ".exe":
                cmd = [inno, "-d", str(out), str(f)]
            else:
                out.mkdir(parents=True, exist_ok=True)
                cmd = [seven, "x", "-y", f"-o{out}"]
                if skip_video:
                    cmd += _video_excludes()
                cmd.append(str(f))
            rc, tail = _run(cmd, timeout=3600)
            if rc != 0 or not any(out.rglob("*")):
                shutil.rmtree(out, ignore_errors=True)
                if f.suffix.lower() == ".exe":
                    log(f"nested installer not readable (left as-is): {f.name}")
                continue
            _flatten(out)
            if skip_video:
                _purge_videos(out)
            extracted += 1
    return extracted


def extract_one(rec: dict, root: Path, *, skip_video: bool = True,
                allow_install: bool = False, log: Log = print) -> None:
    """Unpack one game record into ``_games/<name>/data`` (atomic: ``data.partial`` → ``data``)."""
    game_dir = root / GAMES_DIR / safe_name(rec["name"])
    game_dir.mkdir(parents=True, exist_ok=True)
    target = game_dir / "data.partial"
    shutil.rmtree(target, ignore_errors=True)
    target.mkdir(parents=True, exist_ok=True)
    first = root / rec["parts"][0]
    started = time.strftime("%Y-%m-%d %H:%M:%S")
    method, rc, tail = "?", 1, ""

    if rec["kind"] in ("archive", "volume", "iso"):
        seven = tools.require("7z")
        cmd = [seven, "x", "-y", f"-o{target}"]
        if skip_video:
            cmd += _video_excludes()
        cmd.append(str(first))
        rc, tail = _run(cmd)
        method = "7z"
        if (rc != 0 or not any(target.rglob("*"))) and "password" in tail.lower():
            for pw in ZIP_PASSWORDS:  # encrypted repack zip — retry with known site passwords
                rc, tail = _run(cmd + [f"-p{pw}"])
                if rc <= 1 and any(target.rglob("*")):
                    method = f"7z password={pw}"
                    break
    elif rec["kind"] == "installer":
        if first.suffix.lower() == ".msi":
            rc, tail = _run(["msiexec", "/a", str(first), "/qn", f"TARGETDIR={target}"])
            method = "msi-extract"
        else:
            inno = tools.find("innoextract")
            if inno:
                rc, tail = _run([inno, "-d", str(target), str(first)])
                method = "innoextract"
                if rc != 0 or not any(target.iterdir()):
                    rc, tail = _run([tools.require("7z"), "x", "-y", f"-o{target}", str(first)])
                    method = "7z"
            else:
                rc, tail = _run([tools.require("7z"), "x", "-y", f"-o{target}", str(first)])
                method = "7z"
            if (rc != 0 or not any(target.iterdir())) and allow_install:
                if first.suffix.lower() == ".exe":
                    rc, tail = _run([str(first), "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NOICONS",
                                     "/NORESTART", f"/DIR={target}"])
                    method = "silent-install"
    else:
        rec.update({"status": "skipped", "error": rec.get("reason", "unhandled kind")})
        return

    has_files = any(target.rglob("*"))
    if rc > 1 or not has_files:
        if rec["kind"] == "installer" and not allow_install:
            rec.update({"status": "needs-install", "method": method, "error": tail[-800:]})
            shutil.rmtree(target, ignore_errors=True)
            log(f"{rec['name']}: installer needs execution (re-run with --allow-install)")
            return
        rec.update({"status": "failed", "method": method, "error": tail[-800:]})
        shutil.rmtree(target, ignore_errors=True)
        log(f"{rec['name']}: extraction failed (rc={rc})")
        return

    _flatten(target)
    removed = _purge_videos(target) if skip_video else 0
    nested = _extract_nested(target, skip_video, log)
    dest = game_dir / "data"
    shutil.rmtree(dest, ignore_errors=True)
    os.replace(target, dest)
    rec.update({"status": "done", "method": method, "error": "",
                "finished": time.strftime("%Y-%m-%d %H:%M:%S")})
    (game_dir / "_source.json").write_text(json.dumps({
        "name": rec["name"], "parts": rec["parts"], "bytes": rec["size"], "method": method,
        "started": started, "finished": rec["finished"], "videos_removed": removed,
        "nested_archives": nested, "output": "data/"}, indent=1, ensure_ascii=False),
        encoding="utf-8")
    log(f"{rec['name']}: extracted via {method}"
        + (f" ({removed} video files excluded)" if removed else "")
        + (f" ({nested} nested archives unpacked)" if nested else ""))


# ── post-extraction passes ──────────────────────────────────────────────────

def run_one(game_dir: Path, *, include_video: bool = False, log: Log = print) -> dict:
    """Engine passes on an extracted game: Unity/XNB dumps, bucketing, audio, language index."""
    data = game_dir / "data"
    engines = pipeline.detect(game_dir)
    report: dict = {"engines": engines}
    log(f"{game_dir.name}: engines = {', '.join(engines)}")
    if "unity" in engines:
        report["unity_data"] = unity.dump_data(game_dir)
        report["classes_csv"] = unity.classes_to_csv(game_dir)
        report["unity_media"] = unity.export_media(game_dir)
        if not include_video:
            shutil.rmtree(game_dir / "media" / "video", ignore_errors=True)
    if "xnb" in engines and data.exists():
        content = next((p for p in data.rglob("Content")
                        if p.is_dir() and next(p.rglob("*.xnb"), None) is not None), None)
        if content:
            report["xnb"] = xnb.export(content, game_dir / "media")
    report["generic"] = pipeline.generic(game_dir, log, roots=[data],
                                         skip_video=not include_video)
    report["localization"] = localization.collect(game_dir, log)
    return report


def _count(root: Path) -> int:
    return sum(1 for f in root.rglob("*") if f.is_file()) if root.exists() else 0


def write_report(game_dir: Path, rec: dict) -> None:
    loc_index = game_dir / "extract" / "loc" / "_index.json"
    langs_info = {}
    if loc_index.exists():
        langs_info = json.loads(loc_index.read_text(encoding="utf-8")).get("langs", {})
    rows = "\n".join(
        f"| {k} | {v['files']} | {v['bytes']:,} |" for k, v in sorted(langs_info.items()))
    lines = [
        f"# {rec['name']} — extraction report", "",
        f"- source: `{'`, `'.join(rec['parts'])}` ({rec['size']:,} bytes)",
        f"- method: {rec.get('method', '-')} · status: **{rec.get('status', '?')}** "
        f"· finished: {rec.get('finished', '-')}",
        f"- files: data {_count(game_dir / 'data'):,} · extract {_count(game_dir / 'extract'):,}"
        f" · media {_count(game_dir / 'media'):,}",
        "",
        "## Languages", "",
        "| Lang | Files | Bytes |", "|---|---|---|"] + ([rows] if rows else ["| - | - | - |"]) + [
        "",
        "Folders: `data/` raw game tree · `extract/` readable exports (text, data, `loc/<lang>/`)"
        " · `media/` images, audio (`.ogg` conversions in `media/audio-converted/`), fonts,"
        " models. Browse with `navig explore " + str(game_dir) + "`.",
    ]
    (game_dir / "REPORT.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


# ── batch ───────────────────────────────────────────────────────────────────

def run_all(root: Path, *, include_video: bool = False, allow_install: bool = False,
            retry_failed: bool = False, only: Optional[str] = None,
            log: Log = print) -> dict:
    """Process every pending game one by one; the manifest makes the run resumable."""
    root = root.resolve()
    manifest = load_manifest(root)
    games = manifest["games"]
    todo = [r for r in games.values()
            if (r["name"] == only if only else
                r["status"] == "pending" or (retry_failed and r["status"] in ("failed", "needs-install")))]
    todo.sort(key=lambda r: r["size"])
    log(f"{len(todo)} game(s) to process")
    summary = {"done": 0, "failed": 0, "needs-install": 0, "skipped": 0}
    for i, rec in enumerate(todo, 1):
        log(f"[{i}/{len(todo)}] {rec['name']} ({rec['size']:,} bytes, {rec['kind']})")
        if rec["kind"] == "skip":
            rec["status"] = "skipped"
            summary["skipped"] += 1
            save_manifest(root, manifest)
            continue
        rec["status"] = "extracting"
        save_manifest(root, manifest)
        try:
            extract_one(rec, root, skip_video=not include_video,
                        allow_install=allow_install, log=log)
            if rec["status"] == "done":
                game_dir = root / GAMES_DIR / safe_name(rec["name"])
                run_one(game_dir, include_video=include_video, log=log)
                write_report(game_dir, rec)
                summary["done"] += 1
            else:
                summary[rec["status"] if rec["status"] in summary else "failed"] += 1
        except Exception as exc:  # one broken game must never kill the batch
            rec.update({"status": "failed",
                        "error": f"{type(exc).__name__}: {exc}"[:800]})
            summary["failed"] += 1
            log(f"{rec['name']}: FAILED — {type(exc).__name__}: {exc}")
        save_manifest(root, manifest)
    log("batch: " + " · ".join(f"{v} {k}" for k, v in summary.items()))
    return summary
