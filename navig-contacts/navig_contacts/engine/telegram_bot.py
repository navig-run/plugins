"""
Telegram *bot chat* exports — the dating-bot profile cards.

Not the same thing as navig's Telegram importer, which reads a Desktop
export's ``contacts.json``: your address book. This reads a **chat** export
and pulls out the profile cards a matchmaking bot sent —
``Name, 22, Minsk`` followed by a ``t.me`` link — which is how a thousand of
these contacts arrived in the first place.

The parsing below is unchanged from the tool that produced them, including
the two bugs it had already been through: `MATCH_MARKER_RE` keys on
"Начинай общаться" rather than "симпатия" (which appears in only one of the
bot's three match phrasings, and keying on it silently dropped ~90 real
people), and a card is accepted on a later line when every line before it is
a header ending in ":".
"""
from __future__ import annotations

import json
import re
import shutil
from collections import deque
from datetime import datetime, timezone
from html import unescape
from pathlib import Path
from typing import Optional, Any, Iterable

import logging

from .identity import name_key
from .models import ImportSummary

logger = logging.getLogger(__name__)

# -----------------------------------------------------------------------
# Constants
# -----------------------------------------------------------------------

BOT_ID = "user1234060895"
MATCH_MARKER = "симпатия"  # legacy single-phrase marker (kept for reference)
MATCH_MARKER_RE = re.compile(r"Начинай общаться|симпатия")
EXCLUDE_USERNAMES = frozenset({"leoday", "leomatchbot", "leoday_bot", "Leomatchglobal"})
PROFILE_RE = re.compile(
    r"^(.+?),\s*(\d+),\s*([^,–—\n]+?)(?:\s*[–—].*)?$",
    re.DOTALL,
)
T_ME_RE = re.compile(r"https://t\.me/([A-Za-z0-9_]{3,32})")



# -----------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------

def _now() -> str:
    return datetime.now(tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _text_of(msg: dict) -> str:
    t = msg.get("text", "")
    if isinstance(t, list):
        return "".join(
            chunk if isinstance(chunk, str) else chunk.get("text", "")
            for chunk in t
        )
    return t if isinstance(t, str) else ""


def _entities_of(msg: dict) -> list[dict]:
    ents = msg.get("text_entities", [])
    if ents:
        return ents
    raw = msg.get("text", [])
    if isinstance(raw, list):
        return [c for c in raw if isinstance(c, dict)]
    return []


def _extract_username(msg: dict) -> Optional[str]:
    """Return the first t.me/<username> href in a message, or None."""
    for ent in _entities_of(msg):
        if ent.get("type") == "text_link":
            m = T_ME_RE.match(ent.get("href", ""))
            if m and m.group(1) not in EXCLUDE_USERNAMES:
                return m.group(1)
    return None


def _extract_link_label(msg: dict) -> Optional[str]:
    """Return the display text of the first profile text_link."""
    raw = msg.get("text", [])
    if isinstance(raw, list):
        for chunk in raw:
            if isinstance(chunk, dict) and chunk.get("type") == "text_link":
                href = chunk.get("href", "")
                m = T_ME_RE.match(href)
                if m and m.group(1) not in EXCLUDE_USERNAMES:
                    return chunk.get("text")
    for ent in msg.get("text_entities", []):
        if ent.get("type") == "text_link":
            m = T_ME_RE.match(ent.get("href", ""))
            if m and m.group(1) not in EXCLUDE_USERNAMES:
                return ent.get("text")
    return None


def _parse_card(text: str) -> tuple[Optional[str], Optional[str], Optional[str]]:
    """
    Parse 'Name, Age, City [– desc]' → (name, age, city).

    The card is normally the first line, but the bot also prefixes it with a
    header line, e.g.:

        Кому-то понравилась твоя анкета:

        Лера), 22, Минск – Напиши мне ))

    so a card is also accepted on a later line as long as every line before
    it is a header ending in ':'.  That keeps free-text description lines
    (which can look like "…, 24, …") from being mistaken for a card.
    """
    lines = [ln.strip() for ln in text.strip().split("\n") if ln.strip()]
    for line in lines[:3]:
        m = PROFILE_RE.match(line)
        if m:
            return m.group(1).strip(), m.group(2).strip(), m.group(3).strip()
        if not line.endswith(":"):
            break
    return None, None, None


def _source_name(path: Path) -> str:
    if path.stem.lower() == "result":
        return path.parent.name
    return path.stem


def _valid_photo(val: Any) -> Optional[str]:
    """Return photo path only if it's an actual file ref (not 'File not included')."""
    if isinstance(val, str) and val and "File not included" not in val:
        return val
    return None


# -----------------------------------------------------------------------
# Parse one Telegram export file → list of profile dicts
# -----------------------------------------------------------------------

def parse_telegram_export(path: Path, source: Optional[str] = None) -> list[dict[str, Any]]:
    """
    Extract all mutual-match profiles from one Telegram bot export.

    The bot profile-card message always has a `photo` field like
    `"photos/photo_567@24-10-2024_14-51-31.jpg"` — a path relative to the
    export folder.  We capture this directly (no guessing from filenames).

    Returns a list of dicts:
      uid, full_name, age, city, username, profile_url, source_profile,
      photo_rel_path  (relative to json parent dir, or None)
    """
    with path.open(encoding="utf-8") as f:
        data = json.load(f)

    messages: list[dict] = data.get("messages", [])
    source = source or _source_name(path)
    profiles: list[dict] = []

    for idx, msg in enumerate(messages):
        if msg.get("from_id") != BOT_ID:
            continue
        flat = _text_of(msg)
        if not MATCH_MARKER_RE.search(flat):
            continue

        username = _extract_username(msg)
        if not username:
            continue

        label = _extract_link_label(msg) or username

        # -----------------------------------------------------------------
        # Scan backwards through bot messages for the profile card.
        # Typical sequence before the match notification:
        #   msg N-2: [bot] photo + "Name, Age, City" text  ← profile card
        #   msg N-1: [bot] extra photo, empty text          ← optional
        #   msg N  : [bot] "симпатия" + t.me link           ← we're here
        # We use `continue` (not `break`) on user messages so reactions
        # between bot messages don't stop the scan.
        # -----------------------------------------------------------------
        name = age = city = None
        photo_rel_path: Optional[str] = None
        fallback_photo: Optional[str] = None

        for back in range(1, 10):
            prev_idx = idx - back
            if prev_idx < 0:
                break
            prev = messages[prev_idx]

            # Skip user messages (reactions etc.) but keep scanning
            if prev.get("from_id") != BOT_ID:
                continue

            prev_text = _text_of(prev)
            photo_val = _valid_photo(prev.get("photo"))

            # Stop at an earlier match announcement — its card belongs to that
            # person.  Without this, a match that has no card of its own
            # inherits the previous match's name, age, city and photo.
            if prev_text and MATCH_MARKER_RE.search(prev_text):
                break

            # Track the most recent bot photo seen (fallback)
            if photo_val and fallback_photo is None:
                fallback_photo = photo_val

            if prev_text:
                n, a, c = _parse_card(prev_text)
                if n:
                    name, age, city = n, a, c
                    photo_rel_path = photo_val or fallback_photo
                    break
                # Non-card text ("✨🔍", ads, etc.) — keep scanning

        if not name:
            name = label

        profiles.append({
            "uid": username,
            "full_name": name,
            "age": age,
            "city": city,
            "username": username,
            "profile_url": f"https://t.me/{username}",
            "source_profile": source,
            "photo_rel_path": photo_rel_path,
        })

    # Deduplicate within the same file (keep first occurrence)
    seen: set[str] = set()
    unique: list[dict] = []
    for p in profiles:
        if p["uid"] not in seen:
            seen.add(p["uid"])
            unique.append(p)

    return unique


# -----------------------------------------------------------------------
# HTML export parsing (Telegram Desktop "HTML" format)
# -----------------------------------------------------------------------
#
# Layout of one exported message:
#
#   <div class="message default clearfix" id="message334">
#     <div class="body">
#       <div class="from_name"> Дайвинчик | Leo … </div>   ← absent when "joined"
#       <div class="media_wrap clearfix">
#         <a class="photo_wrap …" href="photos/photo_88@…jpg">
#       </div>
#       <div class="text">Даша, 20, Минск – Ищу мужа🙂</div>
#     </div>
#   </div>
#
# A message with class "joined" omits from_name and inherits the previous
# sender, so the sender has to be tracked as a running state.  The bot is
# identified by the chat title in the page header, which makes this work for
# both "Дайвинчик | Leo …" and "Leo – match and meet new friends".

HTML_MSG_OPEN_RE = re.compile(r'<div class="(message[^"]*)"\s+id="([^"]*)"\s*>')
HTML_HEADER_TITLE_RE = re.compile(
    r'<div class="page_header">.*?<div class="text bold">\s*(.*?)\s*</div>', re.S
)
HTML_FROM_NAME_RE = re.compile(r'<div class="from_name">\s*(.*?)\s*</div>', re.S)
HTML_TEXT_RE = re.compile(r'<div class="text">\s*(.*?)\s*</div>', re.S)
HTML_PHOTO_RE = re.compile(r'<a class="photo_wrap[^"]*"\s+href="([^"]+)"')
HTML_LINK_RE = re.compile(r'<a\s+href="([^"]*)"[^>]*>(.*?)</a>', re.S)
HTML_BR_RE = re.compile(r"<br\s*/?>", re.I)
HTML_TAG_RE = re.compile(r"<[^>]+>")
HTML_FILE_RE = re.compile(r"^messages(\d*)\.html$", re.I)

# How many preceding bot messages to scan for the profile card
CARD_LOOKBACK = 10


def _html_to_text(fragment: str) -> str:
    """Flatten a Telegram HTML text fragment to plain text."""
    s = HTML_BR_RE.sub("\n", fragment)
    s = HTML_TAG_RE.sub("", s)
    return unescape(s).strip()


def html_message_files(export_dir: Path) -> list[Path]:
    """Return messages*.html in chat order (messages.html, messages2.html, …)."""
    files: list[tuple[int, Path]] = []
    for p in export_dir.iterdir():
        m = HTML_FILE_RE.match(p.name)
        if m:
            files.append((int(m.group(1) or 1), p))
    return [p for _, p in sorted(files)]


def _iter_html_messages(path: Path, bot_title: Optional[str]) -> Iterable[dict]:
    """
    Yield one dict per message in a messages*.html file:
      {id, is_bot, text, photo}
    `is_bot` is carried across "joined" messages that omit from_name.
    """
    html = path.read_text(encoding="utf-8", errors="replace")

    if bot_title is None:
        m = HTML_HEADER_TITLE_RE.search(html)
        bot_title = unescape(m.group(1)).strip() if m else None

    sender_is_bot = False
    opens = list(HTML_MSG_OPEN_RE.finditer(html))

    for i, mo in enumerate(opens):
        classes = mo.group(1)
        end = opens[i + 1].start() if i + 1 < len(opens) else len(html)
        chunk = html[mo.end():end]

        if "service" in classes:
            continue  # date separators etc. — never change the sender

        fn = HTML_FROM_NAME_RE.search(chunk)
        if fn:
            name = unescape(_html_to_text(fn.group(1)))
            sender_is_bot = bool(bot_title) and name == bot_title
        # else: "joined" — inherit sender_is_bot from the previous message

        text_m = HTML_TEXT_RE.search(chunk)
        photo_m = HTML_PHOTO_RE.search(chunk)

        yield {
            "id": mo.group(2),
            "is_bot": sender_is_bot,
            "text_html": text_m.group(1) if text_m else "",
            "text": _html_to_text(text_m.group(1)) if text_m else "",
            "photo": unescape(photo_m.group(1)) if photo_m else None,
        }


def _html_extract_username(text_html: str) -> tuple[Optional[str], Optional[str]]:
    """Return (username, link_label) of the first profile t.me link, or (None, None)."""
    for href, label in HTML_LINK_RE.findall(text_html):
        m = T_ME_RE.match(unescape(href))
        if m and m.group(1) not in EXCLUDE_USERNAMES:
            return m.group(1), _html_to_text(label) or None
    return None, None


def parse_telegram_html_export(
    export_dir: Path, source: Optional[str] = None
) -> list[dict[str, Any]]:
    """
    Extract all mutual-match profiles from a Telegram Desktop HTML export
    directory.  Returns the same dict shape as `parse_telegram_export`.

    Photo paths are relative to `export_dir` (e.g. "photos/photo_88@….jpg").
    """
    src = source or export_dir.name
    files = html_message_files(export_dir)
    if not files:
        raise FileNotFoundError(f"No messages*.html found in {export_dir}")

    # Chat title from the first file — identifies the bot for every file
    first_html = files[0].read_text(encoding="utf-8", errors="replace")
    m = HTML_HEADER_TITLE_RE.search(first_html)
    bot_title = unescape(m.group(1)).strip() if m else None

    profiles: list[dict] = []
    seen: set[str] = set()
    # Rolling window of recent messages so the card scan crosses file boundaries
    recent: deque[dict] = deque(maxlen=CARD_LOOKBACK + 5)

    for path in files:
        for msg in _iter_html_messages(path, bot_title):
            text = msg["text"]

            is_match = (
                msg["is_bot"]
                and text
                and MATCH_MARKER_RE.search(text) is not None
            )
            if not is_match:
                recent.append(msg)
                continue

            username, label = _html_extract_username(msg["text_html"])
            if not username:
                # Match with no public username (mention-only link) — not linkable
                recent.append(msg)
                continue

            name = age = city = None
            photo_rel_path: Optional[str] = None
            fallback_photo: Optional[str] = None

            for prev in list(reversed(recent))[:CARD_LOOKBACK]:
                if not prev["is_bot"]:
                    continue  # skip our own replies/reactions, keep scanning
                if prev["text"] and MATCH_MARKER_RE.search(prev["text"]):
                    break  # previous announcement — its card is not ours
                if prev["photo"] and fallback_photo is None:
                    fallback_photo = prev["photo"]
                if prev["text"]:
                    n, a, c = _parse_card(prev["text"])
                    if n:
                        name, age, city = n, a, c
                        photo_rel_path = prev["photo"] or fallback_photo
                        break

            recent.append(msg)

            if not name:
                name = label or username
            if username in seen:
                continue
            seen.add(username)

            profiles.append({
                "uid": username,
                "full_name": name,
                "age": age,
                "city": city,
                "username": username,
                "profile_url": f"https://t.me/{username}",
                "source_profile": src,
                "photo_rel_path": photo_rel_path,
            })

    return profiles



def _copy_photo(profile: dict, media_dir: Path,
                photos_dir: Path) -> Optional[str]:
    """
    Copy the profile's photo out of the export into the book's photos/ folder.

    Returns the path relative to the book, always with forward slashes: a
    Windows-style ``photos\alias.jpg`` does not resolve on Linux or macOS, and
    the stored path has to survive being read on either.
    """
    rel = profile.get("photo_rel_path")
    if not rel:
        return None
    src = media_dir / rel
    if not src.exists():
        return None
    alias = profile.get("username") or profile.get("uid")
    if not alias:
        return None
    photos_dir.mkdir(parents=True, exist_ok=True)
    dest = photos_dir / f"{alias}{src.suffix or '.jpg'}"
    if not dest.exists():
        try:
            shutil.copy2(src, dest)
        except OSError:
            return None
    return f"{photos_dir.name}/{dest.name}"




# ---------------------------------------------------------------------------
# Writing them into the shared contact book
# ---------------------------------------------------------------------------

def import_bot_export(path: Path, conn, source: Optional[str] = None,
                      photos_dir: Optional[Path] = None) -> ImportSummary:
    """
    Import one bot chat export (``result.json``) or HTML export folder.

    A profile becomes a contact keyed on its Telegram username, with the
    username recorded as both an identifier and a ``telegram`` route — so
    ``navig dispatch send`` can reach them, which the tool this came from could
    not do.
    """
    summary = ImportSummary(source=source or _source_name(path))
    if path.is_dir():
        profiles = parse_telegram_html_export(path, source=source)
        media_dir = path
    else:
        profiles = parse_telegram_export(path, source=source)
        media_dir = path.parent

    summary.total = len(profiles)
    for profile in profiles:
        try:
            _write_profile(conn, profile, media_dir, photos_dir, summary)
        except Exception as exc:  # one bad card must not abort the export
            summary.errors += 1
            _log_error(f"{profile.get('uid')}: {exc}")
    return summary


def _write_profile(conn, profile: dict, media_dir: Path,
                   photos_dir: Optional[Path], summary: ImportSummary) -> None:
    username = (profile.get("username") or profile.get("uid") or "").strip()
    if not username:
        summary.skipped_no_uid += 1
        return
    handle = username.lower()
    now = _now()

    # Attach to whoever already owns the handle, however they were filed.
    row = conn.execute(
        "SELECT contact_id FROM contact_identifiers "
        "WHERE kind = 'telegram' AND value_norm = ?", (handle,)).fetchone()
    if row is None:
        row = conn.execute(
            "SELECT contact_id FROM contact_routes "
            "WHERE network = 'telegram' COLLATE NOCASE AND address = ?",
            (handle,)).fetchone()
    if row is None:
        row = conn.execute(
            "SELECT id FROM contacts WHERE alias = ? COLLATE NOCASE",
            (username,)).fetchone()

    age = profile.get("age")
    note = f"Age at import: {age}" if age else None

    if row is not None:
        contact_id = row[0]
        summary.duplicates += 1
        conn.execute(
            "UPDATE contacts SET display_name = COALESCE(NULLIF(display_name, ''), ?), "
            "city = COALESCE(city, ?), notes = COALESCE(notes, ?), "
            "tier = COALESCE(tier, 'handle'), updated_at = ? WHERE id = ?",
            (profile.get("full_name") or "", profile.get("city"), note, now,
             contact_id),
        )
    else:
        cur = conn.execute(
            "INSERT INTO contacts (alias, display_name, city, source_profile, "
            "is_deleted, created_at, updated_at, tier, notes) "
            "VALUES (?, ?, ?, ?, 0, ?, ?, 'handle', ?)",
            (username, profile.get("full_name") or "", profile.get("city"),
             profile.get("source_profile"), now, now, note),
        )
        contact_id = cur.lastrowid
        summary.inserted += 1

    conn.execute(
        "INSERT OR IGNORE INTO contact_identifiers "
        "(contact_id, kind, value_norm, value_raw, is_primary, source_file, created_at) "
        "VALUES (?, 'telegram', ?, ?, 1, ?, ?)",
        (contact_id, handle, username, "telegram-bot-export", now),
    )
    conn.execute(
        "INSERT OR IGNORE INTO contact_routes (contact_id, network, address, priority) "
        "VALUES (?, 'telegram', ?, 0)",
        (contact_id, handle),
    )
    if profile.get("full_name"):
        conn.execute(
            "INSERT OR IGNORE INTO contact_aliases "
            "(contact_id, name, name_key, source_file, is_primary, created_at) "
            "VALUES (?, ?, ?, 'telegram-bot-export', 1, ?)",
            (contact_id, profile["full_name"], name_key(profile["full_name"]), now),
        )

    if photos_dir is not None:
        rel = _copy_photo(profile, media_dir, photos_dir)
        if rel:
            summary.photos_matched += 1
            conn.execute(
                "UPDATE contacts SET photo_path = ? WHERE id = ? "
                "AND (photo_path IS NULL OR photo_path = '')", (rel, contact_id))
        elif profile.get("photo_rel_path"):
            summary.photos_unmatched += 1


def _log_error(message: str) -> None:
    logger.warning("telegram bot import: %s", message)
