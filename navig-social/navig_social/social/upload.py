"""Upload a video file to YouTube — the piece `fan-out` never had.

`fan-out` publishes a TEXT brief. A finished vertical video needed a
different verb, and the OAuth connection already carried the
``youtube.upload`` scope, so the capability was there and only the
command was missing.

Design notes:
  * **Resumable upload.** A 12 MB reel over a flaky line should not start
    again from zero, and YouTube's resumable endpoint is the documented
    path for anything that is not trivially small.
  * **The access token is refreshed before use, not after a failure.**
    Google's tokens last an hour; a weekly scheduled job will ALWAYS find
    an expired one, so refreshing lazily-on-401 would mean every run
    burns a failed request first.
  * **No refresh token is a hard, named error.** Without it a scheduled
    upload cannot work at all, and the fix is one command. Saying so is
    more useful than a 401 traceback.
  * Nothing here decides *whether* to publish. The caller does.
"""
from __future__ import annotations

import json
import mimetypes
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from .oauth import read_secret, write_provider_token

TOKEN_URL = "https://oauth2.googleapis.com/token"
UPLOAD_URL = ("https://www.googleapis.com/upload/youtube/v3/videos"
              "?uploadType=resumable&part=snippet,status")
CHUNK = 1024 * 1024 * 4          # 4 MiB — YouTube wants a multiple of 256 KiB
MAX_TITLE = 100
MAX_DESC = 5000


class UploadError(RuntimeError):
    pass


@dataclass
class Uploaded:
    video_id: str
    url: str
    title: str
    privacy: str
    bytes_sent: int


# ── auth ──────────────────────────────────────────────────────────────────

def refresh_access_token() -> str:
    """Exchange the stored refresh token for a fresh access token."""
    refresh = read_secret("youtube/refresh_token")
    if not refresh:
        raise UploadError(
            "no YouTube refresh token is stored, so the access token cannot be "
            "renewed and a scheduled upload will always fail.\n"
            "  Fix it once:  navig social connect youtube\n"
            "  (the flow already asks for offline access; the stored "
            "connection simply predates it)")
    cid = read_secret("youtube/client_id")
    secret = read_secret("youtube/client_secret")
    if not (cid and secret):
        raise UploadError("youtube/client_id or youtube/client_secret is missing "
                          "from the vault — run: navig social connect youtube")
    body = urllib.parse.urlencode({
        "grant_type": "refresh_token", "refresh_token": refresh,
        "client_id": cid, "client_secret": secret}).encode()
    req = urllib.request.Request(
        TOKEN_URL, data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            tok = json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        raise UploadError(
            f"refreshing the YouTube token failed ({e.code}): "
            f"{e.read().decode()[:300]}\n"
            "  If the refresh token was revoked, reconnect: "
            "navig social connect youtube") from e
    access = tok.get("access_token")
    if not access:
        raise UploadError(f"token endpoint returned no access_token: {tok}")
    write_provider_token("youtube", access, "YouTube access token")
    return access


def _auth_header(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def channel_of(token: str) -> dict | None:
    req = urllib.request.Request(
        "https://www.googleapis.com/youtube/v3/channels?part=snippet&mine=true",
        headers=_auth_header(token))
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            items = json.loads(r.read().decode()).get("items") or []
        if not items:
            return None
        s = items[0]["snippet"]
        return {"id": items[0]["id"], "title": s.get("title"),
                "handle": s.get("customUrl")}
    except Exception:
        return None


# ── upload ────────────────────────────────────────────────────────────────

def upload(
    path: str | Path,
    *,
    title: str,
    description: str = "",
    tags: list[str] | None = None,
    privacy: str = "private",
    category_id: str = "22",
    made_for_kids: bool = False,
    shorts: bool = True,
    on_progress=None,
) -> Uploaded:
    """Upload *path* and return the created video.

    `privacy` defaults to **private** on purpose: an automated job should
    never make something public without the caller saying so out loud.
    """
    p = Path(path)
    if not p.is_file():
        raise UploadError(f"no such file: {p}")
    size = p.stat().st_size
    if size == 0:
        raise UploadError(f"file is empty: {p}")
    if privacy not in {"private", "unlisted", "public"}:
        raise UploadError(f"privacy must be private, unlisted or public — got {privacy!r}")

    title = (title or p.stem).strip()[:MAX_TITLE]
    desc = (description or "").strip()
    if shorts and "#shorts" not in (title + desc).lower():
        # YouTube keys Shorts off the aspect and length, but the tag is what
        # makes it surface reliably in the Shorts shelf.
        desc = (desc + "\n\n#Shorts").strip()
    desc = desc[:MAX_DESC]

    token = refresh_access_token()

    meta = {
        "snippet": {"title": title, "description": desc,
                    "tags": (tags or [])[:30], "categoryId": category_id},
        "status": {"privacyStatus": privacy, "selfDeclaredMadeForKids": made_for_kids},
    }
    ctype = mimetypes.guess_type(p.name)[0] or "video/mp4"

    # 1 · open a resumable session
    req = urllib.request.Request(
        UPLOAD_URL, data=json.dumps(meta).encode(),
        headers={**_auth_header(token),
                 "Content-Type": "application/json; charset=UTF-8",
                 "X-Upload-Content-Length": str(size),
                 "X-Upload-Content-Type": ctype})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            session = r.headers.get("Location")
    except urllib.error.HTTPError as e:
        raise UploadError(f"could not start the upload ({e.code}): "
                          f"{e.read().decode()[:400]}") from e
    if not session:
        raise UploadError("YouTube did not return a resumable session URL")

    # 2 · send it, one chunk at a time
    sent = 0
    with p.open("rb") as fh:
        while sent < size:
            chunk = fh.read(CHUNK)
            if not chunk:
                break
            last = sent + len(chunk) - 1
            put = urllib.request.Request(
                session, data=chunk, method="PUT",
                headers={"Content-Length": str(len(chunk)),
                         "Content-Range": f"bytes {sent}-{last}/{size}",
                         "Content-Type": ctype})
            try:
                with urllib.request.urlopen(put, timeout=300) as r:
                    payload = r.read().decode()
                    sent += len(chunk)
                    if on_progress:
                        on_progress(sent, size)
                    if r.status in (200, 201):
                        v = json.loads(payload)
                        vid = v.get("id")
                        return Uploaded(vid, f"https://youtu.be/{vid}",
                                        title, privacy, sent)
            except urllib.error.HTTPError as e:
                # 308 = "keep going", which urllib raises rather than returns
                if e.code == 308:
                    rng = e.headers.get("Range")
                    sent = int(rng.split("-")[1]) + 1 if rng else sent + len(chunk)
                    fh.seek(sent)
                    if on_progress:
                        on_progress(sent, size)
                    continue
                raise UploadError(f"upload failed at byte {sent} ({e.code}): "
                                  f"{e.read().decode()[:400]}") from e
    raise UploadError("the upload finished sending but YouTube returned no video id")
