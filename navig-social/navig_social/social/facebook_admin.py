"""Facebook Page management (read + admin) for the navig-social layer.

Complements :class:`~navig_social.social.publishers.FacebookPagePublisher`
(publish-only) with the *management* surface Studio lacks: list/back-up photos,
edit page info and photo captions, and delete photos — all through the Meta
Graph API, reusing the same vault-stored ``facebook`` token and
``adapters.social.facebook.page_id`` config the publisher already resolves via
:mod:`navig_social.social.credentials`.

Safety contract: nothing here deletes on its own initiative. :func:`plan_deletions`
only ever marks a *live* photo deletable when a verified, non-empty local backup
of that exact photo exists; the CLI (``navig facebook delete-all``) is
additionally confirm-gated. This mirrors NAVIG's "warn before destructive ops".

Module top-level is stdlib-only (navig imports are lazy, per the lazy-import law),
so the pure helpers below are unit-testable without a running daemon.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import urllib.parse
from typing import Any, Callable

logger = logging.getLogger(__name__)

GRAPH_HOST = "https://graph.facebook.com"
_RATE_LIMIT_CODES = {4, 17, 32, 613}  # Graph "slow down" codes
_PHOTO_FIELDS = "id,name,created_time,link,album{name},images{source,width,height}"
_PAGE_FIELDS = "name,id,about,description,general_info,category,website,emails,phone,fan_count"


class FacebookAdminError(Exception):
    """A Graph API or configuration failure. Never carries the access token."""

    def __init__(self, message: str, *, code: Any = None, subcode: Any = None,
                 http_status: int | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.subcode = subcode
        self.http_status = http_status


# ── pure helpers (no navig imports — unit-testable) ───────────────────────────


def best_image(item: dict) -> dict | None:
    """Return the highest-resolution image variant of a Graph photo node."""
    images = item.get("images") or []
    if not images:
        return None
    return max(images, key=lambda im: (im.get("width", 0) or 0) * (im.get("height", 0) or 0))


def ext_from_url(url: str, default: str = ".jpg") -> str:
    """Best-effort file extension from a photo source URL."""
    path = urllib.parse.urlparse(url).path
    _, ext = os.path.splitext(path)
    return ext if ext and len(ext) <= 5 else default


def plan_deletions(
    manifest: list[dict], live_ids: list[str], backup_dir: str
) -> tuple[list[str], list[str], list[str]]:
    """Decide what is safe to delete.

    Returns ``(deletable, unbacked, missing)``:

    * ``deletable`` — live photo ids with a verified non-empty local file → safe.
    * ``unbacked``  — live ids absent from the manifest → **never** deleted.
    * ``missing``   — manifest ids whose local file is gone/empty → backup is
      incomplete; the caller should refuse unless the user opts in.
    """
    verified: dict[str, bool] = {}
    for rec in manifest:
        pid = rec.get("id")
        if pid is None:
            continue
        rel = rec.get("file")
        full = os.path.join(backup_dir, rel) if rel else None
        verified[pid] = bool(full) and os.path.isfile(full) and os.path.getsize(full) > 0
    deletable = [pid for pid in live_ids if verified.get(pid)]
    unbacked = [pid for pid in live_ids if pid not in verified]
    missing = [pid for pid, ok in verified.items() if not ok]
    return deletable, unbacked, missing


# ── the admin client ──────────────────────────────────────────────────────────


class FacebookPageAdmin:
    """Graph API client for managing one Page you administer.

    Credentials come from the same place as the Studio publisher: the vault
    secret ``facebook`` (token) and config ``adapters.social.facebook.page_id``.
    Pass ``page_id``/``token`` explicitly to override (tests, power users).
    """

    def __init__(self, page_id: str | None = None, token: str | None = None) -> None:
        # Lazy navig imports (keep module top-level daemon-free).
        from navig_social.social.credentials import get_config, get_token

        self.page_id: str | None = page_id or get_config("facebook", "page_id")
        self._token: str | None = token or get_token("facebook")
        version = get_config("facebook", "graph_version")
        self._base = f"{GRAPH_HOST}/{version}" if version else GRAPH_HOST

    def is_configured(self) -> bool:
        return bool(self.page_id and self._token)

    # -- HTTP core ------------------------------------------------------------

    def _url(self, path: str, params: dict | None = None) -> str:
        query = dict(params or {})
        query["access_token"] = self._token or ""
        return f"{self._base}/{path.lstrip('/')}?{urllib.parse.urlencode(query)}"

    def _new_session(self):
        import aiohttp

        return aiohttp.ClientSession()

    async def _request(self, session, method: str, url: str, *, data=None, retries: int = 4):
        import aiohttp

        attempt = 0
        while True:
            attempt += 1
            try:
                async with session.request(method, url, data=data) as resp:
                    body = await resp.text()
                    payload = json.loads(body) if body else {}
                    if resp.status >= 400:
                        err = payload.get("error", {}) if isinstance(payload, dict) else {}
                        code = err.get("code")
                        transient = resp.status in (429, 500, 502, 503) or code in _RATE_LIMIT_CODES
                        if transient and attempt <= retries:
                            wait = min(60, 2 ** attempt)
                            logger.warning("facebook transient (code %s); retry in %ss [%s/%s]",
                                           code, wait, attempt, retries)
                            await asyncio.sleep(wait)
                            continue
                        raise FacebookAdminError(
                            err.get("message") or body or f"HTTP {resp.status}",
                            code=code, subcode=err.get("error_subcode"), http_status=resp.status)
                    return payload
            except aiohttp.ClientError as exc:
                if attempt <= retries:
                    wait = min(30, 2 ** attempt)
                    logger.warning("facebook network error (%s); retry in %ss", exc, wait)
                    await asyncio.sleep(wait)
                    continue
                raise FacebookAdminError(f"network error: {exc}") from exc

    async def _get(self, session, path: str, params: dict | None = None):
        return await self._request(session, "GET", self._url(path, params))

    async def _get_url(self, session, full_url: str):
        return await self._request(session, "GET", full_url)

    async def _post(self, session, path: str, params: dict):
        return await self._request(session, "POST", self._url(path), data=params)

    async def _delete(self, session, path: str):
        return await self._request(session, "DELETE", self._url(path))

    # -- page info ------------------------------------------------------------

    async def get_page_info(self, fields: str = _PAGE_FIELDS) -> dict:
        async with self._new_session() as s:
            return await self._get(s, self.page_id or "", {"fields": fields})

    async def set_page_info(self, **fields: str | None) -> dict:
        params = {k: v for k, v in fields.items() if v is not None}
        if not params:
            raise FacebookAdminError("nothing to update")
        async with self._new_session() as s:
            return await self._post(s, self.page_id or "", params)

    # -- photos ---------------------------------------------------------------

    async def list_photos(self, limit: int | None = None) -> list[dict]:
        out: list[dict] = []
        async with self._new_session() as s:
            url = self._url(f"{self.page_id}/photos",
                            {"type": "uploaded", "fields": _PHOTO_FIELDS, "limit": 50})
            while url:
                data = await self._get_url(s, url)
                for item in data.get("data", []):
                    out.append(item)
                    if limit and len(out) >= limit:
                        return out
                url = (data.get("paging") or {}).get("next")
        return out

    async def set_caption(self, photo_id: str, caption: str) -> dict:
        # Best-effort: some photos reject caption edits — the raw payload is returned.
        # A photo's caption is read back as its `name` field (see _PHOTO_FIELDS), so
        # write `name` first; retry legacy `caption` if Graph rejects the parameter.
        async with self._new_session() as s:
            try:
                return await self._post(s, photo_id, {"name": caption})
            except FacebookAdminError as exc:
                if exc.code != 100:  # 100 = invalid parameter
                    raise
                return await self._post(s, photo_id, {"caption": caption})

    async def delete_photo(self, photo_id: str) -> dict:
        async with self._new_session() as s:
            return await self._delete(s, photo_id)

    async def backup_photos(
        self, out_dir: str, limit: int | None = None, overwrite: bool = False,
        on_progress: Callable[[int, str], None] | None = None,
    ) -> dict:
        """Download every uploaded photo at full resolution + write a manifest.

        Idempotent: re-running skips files already present (unless *overwrite*).
        Returns a summary dict.
        """
        photos_dir = os.path.join(out_dir, "photos")
        os.makedirs(photos_dir, exist_ok=True)
        manifest: list[dict] = []
        downloaded = skipped = failed = processed = 0
        async with self._new_session() as s:
            url = self._url(f"{self.page_id}/photos",
                            {"type": "uploaded", "fields": _PHOTO_FIELDS, "limit": 50})
            stop = False
            while url and not stop:
                data = await self._get_url(s, url)
                for item in data.get("data", []):
                    processed += 1
                    pid = item["id"]
                    img = best_image(item)
                    rec: dict[str, Any] = {
                        "id": pid, "caption": item.get("name"),
                        "created_time": item.get("created_time"),
                        "album": (item.get("album") or {}).get("name"),
                        "link": item.get("link"), "file": None, "source_url": None,
                    }
                    if img and img.get("source"):
                        src = img["source"]
                        fname = f"{pid}{ext_from_url(src)}"
                        dest = os.path.join(photos_dir, fname)
                        rec.update({"file": os.path.join("photos", fname), "source_url": src,
                                    "width": img.get("width"), "height": img.get("height")})
                        if os.path.isfile(dest) and not overwrite and os.path.getsize(dest) > 0:
                            skipped += 1
                        else:
                            try:
                                await self._download(s, src, dest)
                                downloaded += 1
                            except Exception as exc:  # noqa: BLE001 — record & continue
                                logger.warning("download failed for %s: %s", pid, exc)
                                failed += 1
                    else:
                        failed += 1
                    manifest.append(rec)
                    if on_progress:
                        on_progress(processed, pid)
                    if limit and processed >= limit:
                        stop = True
                        break
                url = None if stop else (data.get("paging") or {}).get("next")
        manifest_path = os.path.join(out_dir, "photos.json")
        with open(manifest_path, "w", encoding="utf-8") as fh:
            json.dump(manifest, fh, indent=2, ensure_ascii=False)
        return {"total": len(manifest), "downloaded": downloaded, "skipped": skipped,
                "failed": failed, "manifest": manifest_path, "photos_dir": photos_dir}

    async def _download(self, session, url: str, dest: str) -> None:
        async with session.get(url) as resp:
            if resp.status >= 400:
                raise FacebookAdminError(f"HTTP {resp.status} downloading image")
            tmp = dest + ".part"
            with open(tmp, "wb") as fh:
                async for chunk in resp.content.iter_chunked(65536):
                    fh.write(chunk)
            os.replace(tmp, dest)

    # -- destructive: backup-gated bulk delete --------------------------------

    async def plan_delete_all(self, out_dir: str) -> dict:
        """Compute what a full delete would touch, verified against the backup.

        Raises if no backup manifest exists — you cannot bulk-delete what you
        have not backed up.
        """
        manifest_path = os.path.join(out_dir, "photos.json")
        if not os.path.isfile(manifest_path):
            raise FacebookAdminError(
                f"no backup manifest at {manifest_path} — run 'backup' first")
        with open(manifest_path, encoding="utf-8") as fh:
            manifest = json.load(fh)
        live = await self.list_photos()
        live_ids = [p["id"] for p in live]
        deletable, unbacked, missing = plan_deletions(manifest, live_ids, out_dir)
        return {"live": len(live_ids), "deletable": deletable,
                "unbacked": unbacked, "missing": missing,
                "manifest_count": len(manifest)}

    async def run_deletions(
        self, photo_ids: list[str], delay: float = 1.0,
        on_delete: Callable[[int, int, str], None] | None = None,
    ) -> dict:
        """Delete an explicit list of photo ids, pacing between calls."""
        ok = err = 0
        async with self._new_session() as s:
            for i, pid in enumerate(photo_ids, 1):
                try:
                    await self._delete(s, pid)
                    ok += 1
                except FacebookAdminError as exc:
                    err += 1
                    logger.warning("delete failed for %s: %s", pid, exc)
                if on_delete:
                    on_delete(i, len(photo_ids), pid)
                if delay:
                    await asyncio.sleep(delay)
        return {"deleted": ok, "failed": err, "total": len(photo_ids)}
