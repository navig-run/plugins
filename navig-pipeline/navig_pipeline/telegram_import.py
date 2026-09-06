"""Telegram channel → local media library, as one composed flow.

Chains three existing navig capabilities so nothing is reinvented:

    navig.telegram.media   → download the bot's media bytes into a staging folder
    <archive pipeline>     → dedupe · route · transcribe · OCR · classify → _LIBRARY
    navig.telegram.organize→ delete ONLY the messages whose files are verified local

The archive pipeline (``ingest.py``) currently lives in the media-export tree, not in
navig, so it is shelled by a resolvable path (env ``NAVIG_ARCHIVE_PIPELINE`` → known
default). Porting its pure-stdlib steps into this plugin is a tracked follow-up.

SAFETY: deletion is revoke-for-all and irreversible. A message is deleted only when its
downloaded file is proven to sit in ``_LIBRARY`` (or is an exact duplicate whose keeper
is), and only when ``delete_after`` AND ``confirm_delete`` are both set. Default is a
no-delete import; the caller previews counts first.
"""

from __future__ import annotations

import asyncio
import csv
import json
import os
import re
import shutil
import subprocess
import sys

# Environment-specific: where the archive pipeline's ingest.py lives. Override with
# NAVIG_ARCHIVE_PIPELINE (path to ingest.py or its directory).
_DEFAULT_PIPELINE_DIR = (
    r"C:\Users\subdose\Downloads\Telegram Desktop\ChatExport_2026-07-06\_catalog\pipeline"
)
_MEDIA_EXT = {
    ".mp4": ("video_files", "video"), ".mov": ("video_files", "video"),
    ".webm": ("video_files", "video"), ".mkv": ("video_files", "video"),
    ".jpg": ("photos", "image"), ".jpeg": ("photos", "image"),
    ".png": ("photos", "image"), ".webp": ("photos", "image"),
    ".mp3": ("files", "audio"), ".m4a": ("files", "audio"),
    ".aac": ("files", "audio"), ".wav": ("files", "audio"), ".opus": ("files", "audio"),
}

_SETUP_HINT = "Run:  navig telegram setup --api-id <id> --api-hash <hash>  then  navig telegram login <+phone>"


def _slug(s: str) -> str:
    s = re.sub(r"[^\w.-]+", "-", str(s)).strip("-").lower()
    return s or "channel"


def _staging_dir(channel, title: str) -> str:
    base = os.path.join(os.path.expanduser("~"), ".navig", "telegram-import")
    os.makedirs(base, exist_ok=True)
    return os.path.join(base, _slug(title or channel))


def _resolve_ingest() -> str | None:
    p = os.environ.get("NAVIG_ARCHIVE_PIPELINE") or _DEFAULT_PIPELINE_DIR
    if p and os.path.isfile(p):
        return p
    cand = os.path.join(p, "ingest.py") if p else ""
    return cand if cand and os.path.isfile(cand) else None


def _auth_ok() -> tuple[bool, str]:
    from navig.telegram import config as tgcfg
    if not tgcfg.have_api_credentials():
        return False, "Telegram api_id/api_hash not set. " + _SETUP_HINT
    if not tgcfg.is_logged_in():
        return False, "Telegram user account not logged in. " + _SETUP_HINT
    return True, ""


async def preview_import(channel, *, mode="auto", from_sender=None, limit=300) -> dict:
    """Auth guard + a light read-only scan. No downloads, no deletes."""
    ok, hint = _auth_ok()
    if not ok:
        return {"error": "auth", "setup_hint": hint}
    from navig.telegram import media
    scan = await media.scan_channel(channel, limit=limit, from_sender=from_sender)
    detected = _detect_mode(mode, scan)
    would = scan["media"] if detected == "files" else scan["links"]
    return {"ok": True, "channel": str(channel), "title": scan["title"],
            "media_count": scan["media"], "link_count": scan["links"],
            "detected_mode": detected, "would_import": would, "would_delete": would}


def _detect_mode(mode: str, scan: dict) -> str:
    if mode in ("files", "links"):
        return mode
    # auto: prefer files whenever the channel actually holds media (the common case);
    # fall back to link re-download only for a links-only channel.
    return "files" if scan.get("media", 0) >= max(1, scan.get("links", 0)) else "links"


async def _import_links(channel, dest, *, limit, progress=None) -> dict:
    """Links-only channel: re-download each TikTok URL via `navig tiktok download`,
    flatten results into the staging dirs, mapping every file to its LINK message id."""
    from navig.telegram import organize
    res = await organize.links(channel, limit=limit)
    tiktoks = [lk for lk in res["links"] if lk.get("provider") == "tiktok"]
    navig = shutil.which("navig")
    man_p = os.path.join(dest, "_catalog", "manifest.jsonl")
    map_p = os.path.join(dest, "_catalog", "tg_media_map.jsonl")
    media_n, msg_ids = 0, []
    with open(man_p, "a", encoding="utf-8") as man, open(map_p, "a", encoding="utf-8") as mapf:
        for lk in tiktoks:
            mid, url = lk["message_id"], lk["url"]
            tmp = os.path.join(dest, "_tiktok_tmp", str(mid))
            os.makedirs(tmp, exist_ok=True)
            if navig:
                try:
                    # OFF the loop, and once PER LINK: this shells out to a
                    # downloader with a 300s timeout, so inline it froze the
                    # daemon for up to five minutes for every link in the batch.
                    await asyncio.to_thread(
                        subprocess.run,
                        [navig, "tiktok", "download", url, "-o", tmp, "--no-metadata"],
                        capture_output=True,
                        text=True,
                        timeout=300,
                    )
                except Exception:  # noqa: BLE001 — a failed URL just yields no files
                    pass
            for root, _dirs, files in os.walk(tmp):
                for fn in files:
                    sub_typ = _MEDIA_EXT.get(os.path.splitext(fn)[1].lower())
                    if not sub_typ:
                        continue
                    sub, typ = sub_typ
                    newname = f"{mid}_{fn}"
                    rel = f"{sub}/{newname}"
                    shutil.move(os.path.join(root, fn), os.path.join(dest, sub, newname))
                    man.write(json.dumps({"msg_id": mid, "type": typ, "path": rel,
                                          "caption": "", "links": [url]}, ensure_ascii=False) + "\n")
                    mapf.write(json.dumps({"message_id": mid, "rel": rel, "kind": typ,
                                           "source_url": url}, ensure_ascii=False) + "\n")
                    media_n += 1
            if mid not in msg_ids:
                msg_ids.append(mid)
            if progress:
                progress(media_n, "link")
    shutil.rmtree(os.path.join(dest, "_tiktok_tmp"), ignore_errors=True)
    return {"media": media_n, "message_ids": msg_ids, "title": res.get("chat", str(channel))}


def _pipeline_timeout() -> float:
    """The archive pipeline's wall-clock cap (transcribe/OCR over a whole batch can be long).
    A cap is still mandatory: without one a wedged ingest.py hangs the import forever. Raise
    it for very large channels with ``NAVIG_ARCHIVE_TIMEOUT`` (seconds); default 1h."""
    try:
        return max(1.0, float(os.environ.get("NAVIG_ARCHIVE_TIMEOUT", "3600")))
    except ValueError:
        return 3600.0


def _run_pipeline(stage: str, *, skip_ocr: bool, timeout: float | None = None) -> tuple[int, str]:
    ingest = _resolve_ingest()
    if not ingest:
        return 1, ("archive pipeline not found — set NAVIG_ARCHIVE_PIPELINE to the folder "
                   "containing ingest.py")
    argv = [sys.executable, ingest, stage] + (["--skip-ocr"] if skip_ocr else [])
    limit = _pipeline_timeout() if timeout is None else timeout
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=limit,
                              env={**os.environ, "ARCHIVE_EXP": stage, "PYTHONUTF8": "1"})
    except subprocess.TimeoutExpired:
        # A non-zero rc means run_import returns 'pipeline_failed' and NEVER deletes — so a
        # hung/slow pipeline can't lead to a delete, and doesn't wedge the caller forever.
        return 124, (f"archive pipeline exceeded {limit:.0f}s and was stopped "
                     f"(raise NAVIG_ARCHIVE_TIMEOUT if the channel is very large) — nothing deleted")
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


async def run_import(channel, *, mode="auto", from_sender=None, limit=None,
                     delete_after=False, confirm_delete=False, skip_ocr=False,
                     progress=None) -> dict:
    """Full flow: stage media → run the archive pipeline → optional verified delete."""
    ok, hint = _auth_ok()
    if not ok:
        return {"error": "auth", "setup_hint": hint}

    from navig.telegram import media
    scan = await media.scan_channel(channel, limit=min(limit or 300, 300), from_sender=from_sender)
    detected = _detect_mode(mode, scan)
    stage = _staging_dir(channel, scan["title"])
    for d in ("photos", "video_files", "files", "_catalog"):
        os.makedirs(os.path.join(stage, d), exist_ok=True)

    # 1. stage the media into a ChatExport-shaped folder
    if detected == "links":
        dl = await _import_links(channel, stage, limit=limit or 500, progress=progress)
    else:
        dl = await media.download_channel_media(channel, dest=stage, from_sender=from_sender,
                                                limit=limit, progress=progress)
    if not dl.get("media"):
        return {"status": "empty", "detected_mode": detected, "stage": stage,
                "media": 0, "note": "no media found to import"}

    # 2. run the archive pipeline (dedupe · route · transcribe · OCR · classify · register).
    # OFF the loop: it shells out to a batch that transcribes/OCRs every file (minutes), so a
    # direct call would freeze the daemon/CLI loop for the whole run — the same reason
    # `_import_links` offloads its per-link download. Bounded by `_run_pipeline`'s timeout.
    rc, log = await asyncio.to_thread(_run_pipeline, stage, skip_ocr=skip_ocr)
    report = {"detected_mode": detected, "stage": stage, "media": dl["media"],
              "by_kind": dl.get("by_kind", {}), "skipped": len(dl.get("skipped", [])),
              "pipeline_rc": rc, "pipeline_log": log[-1500:]}
    if rc != 0:
        report["status"] = "pipeline_failed"
        return report                                   # never delete if the pipeline failed

    # 3. verified delete (irreversible — gated)
    report["delete"] = await verify_and_delete(
        channel, stage, confirm=bool(delete_after and confirm_delete))
    report["status"] = "ok"
    return report


# ── Stage 3 — safe verified delete ────────────────────────────────────────────

def build_landed_map(stage: str) -> dict:
    """rel → ('library', new_rel) for every routed file, plus rel → ('duplicate', keep_rel)
    for every exact duplicate. Keys are the ORIGINAL staged rel (what tg_media_map records)."""
    landed: dict[str, tuple[str, str]] = {}
    cat = os.path.join(stage, "_catalog")
    catp = os.path.join(cat, "catalog.jsonl")
    if os.path.exists(catp):
        for line in open(catp, encoding="utf-8"):
            try:
                r = json.loads(line)
            except Exception:
                continue
            if r.get("rel") and r.get("new_rel"):
                landed[r["rel"]] = ("library", r["new_rel"])
    dedup = os.path.join(cat, "dedup_plan.csv")
    if os.path.exists(dedup):
        with open(dedup, encoding="utf-8", newline="") as f:
            for row in csv.DictReader(f):
                dup = row.get("dup_rel") or row.get("dup") or ""
                keep = row.get("keep_rel") or row.get("keep") or ""
                if dup and dup not in landed:
                    landed[dup] = ("duplicate", keep)
    return landed


async def verify_and_delete(channel, stage: str, *, confirm: bool = False) -> dict:
    """Delete only messages whose downloaded bytes are provably local. Dry-run unless
    ``confirm``. Never deletes an unmapped or missing-on-disk message."""
    map_p = os.path.join(stage, "_catalog", "tg_media_map.jsonl")
    if not os.path.exists(map_p):
        return {"found": 0, "verified": 0, "unverified": [], "deleted": 0, "dry_run": not confirm}

    landed = build_landed_map(stage)

    def _exists(rel: str) -> bool:
        return os.path.exists(os.path.join(stage, rel.replace("/", os.sep)))

    verified_ids, unverified, found = [], [], 0
    for line in open(map_p, encoding="utf-8"):
        try:
            m = json.loads(line)
        except Exception:
            continue
        found += 1
        mid, rel = m.get("message_id"), m.get("rel")
        info = landed.get(rel)
        if info and info[0] == "library" and _exists(info[1]):
            verified_ids.append(mid)
        elif info and info[0] == "duplicate":
            keep = info[1]
            keep_lib = landed.get(keep)
            if keep_lib and keep_lib[0] == "library" and _exists(keep_lib[1]):
                verified_ids.append(mid)      # bytes preserved in the kept copy
            else:
                unverified.append({"message_id": mid, "reason": "duplicate keeper not found"})
        else:
            unverified.append({"message_id": mid, "reason": "not found in _LIBRARY"})

    verified_ids = sorted(set(i for i in verified_ids if i is not None))
    result = {"found": found, "verified": len(verified_ids),
              "unverified": unverified[:50], "unverified_count": len(unverified),
              "dry_run": not confirm, "deleted": 0}
    if confirm and verified_ids:
        from navig.telegram import organize
        res = await organize.delete_messages(channel, verified_ids, confirm=True)
        result["deleted"] = res.get("deleted", 0)
    return result
