"""navig telegram-exports notebook — turn a chat export into a browsable Markdown library.

``organize`` files export *folders*; this renders what is *inside* one. It was
built for **Saved Messages** — years of links, notes, photos, circles and voice
notes dumped into one chat — but reads any ``result.json`` export:

    <out>/
      README.md              stats, topic map, coverage status — start here
      Timeline/<year>.md     EVERY message, by month, anchor ``#m-<id>``
      Topics/NN-<topic>.md   the same messages regrouped by theme
      Links/                 every link, canonicalised and deduped, with refs
      Media/                 galleries: photos, videos, circles, voice, audio, files
      Sources/Forwards.md    what came from which forwarded channel / chat
      Similar.md             repeated texts, links saved twice, identical media
      _sensitive/            credentials / finance / leaks — full text, kept apart
      _export/<date>/        the raw export, copied byte-for-byte (sha256)
      _duplicates/           extra byte-identical media, moved by ``quarantine``
      _state/                manifests, coverage, topic overrides

Nothing is ever deleted. ``build`` *copies* the export (the source stays where it
is), ``quarantine`` *moves* extra copies of identical media aside with a manifest
that ``restore`` replays in reverse, and the Markdown is regenerated from
``result.json`` on every run, so it can always be thrown away and rebuilt.

Topics come from rules (link domain, forward source, entity types, hashtags,
keywords). Anything the rules cannot place lands in ``_state/unclassified.tsv``;
decisions made about those — by a person or an agent — go in
``_state/topics.overrides.csv`` (``id,topic,reason``) and win over the rules.
"""

from __future__ import annotations

import csv
import hashlib
import json
import os
import re
import shutil
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit

import typer
from rich.console import Console

from .dedup import full_hash

console = Console()

_STATE = "_state"
_EXPORT = "_export"
_DUPES = "_duplicates"
_SENSITIVE = "_sensitive"
_GENERATED_LIST = "generated.txt"
_MANIFEST_SUFFIX = ".sha256"
_SKIP_EXPORT_DIRS = {"css", "js", "images"}          # Telegram's own HTML viewer assets
_BURST_SECONDS = 120                                  # messages this close form one "note"

# --------------------------------------------------------------------------- #
# Topics
# --------------------------------------------------------------------------- #
# (slug, title, emoji). Order is the order of Topics/NN-*.md.
TOPICS: list[tuple[str, str, str]] = [
    ("ideas", "Ideas & plans", "💡"),
    ("content", "Content drafts", "🎬"),
    ("tasks", "Tasks & notes", "✅"),
    ("dev", "Dev & code", "💻"),
    ("ai", "AI & tools", "🤖"),
    ("apps", "Apps & software", "📱"),
    ("security", "Security & OSINT", "🛡"),
    ("business", "Business & money", "💼"),
    ("learning", "Books & learning", "📚"),
    ("music", "Music & audio", "🎵"),
    ("tiktok", "TikTok & short video", "📲"),
    ("video", "YouTube & video", "📺"),
    ("telegram", "Telegram channels & bots", "✈️"),
    ("social", "Social & people", "👥"),
    ("family", "Family & personal", "🏠"),
    ("voice", "Voice notes & circles", "🎙"),
    ("health", "Health & fitness", "💪"),
    ("shopping", "Shopping & products", "🛒"),
    ("travel", "Travel & places", "🧭"),
    ("admin", "Admin & paperwork", "🗂"),
    ("quotes", "Quotes & reflections", "💬"),
    ("fun", "Fun & memes", "😂"),
    ("images", "Images (unsorted)", "🖼"),
    ("misc", "Misc", "📦"),
    ("system", "System", "⚙️"),
]
TOPIC_SLUGS = [t[0] for t in TOPICS]
_TOPIC = {t[0]: t for t in TOPICS}
_WEAK = {"images", "misc"}                           # may inherit a neighbour's topic

SENSITIVE_KINDS = {
    "credentials": ("Credentials", "🔑", "logins, passwords, tokens, keys"),
    "finance": ("Finance", "💳", "IBANs, card numbers"),
    "leaks": ("Leaks & dumps", "☣️", "leaked databases, combolists, leak channels"),
}

# domain (suffix match) -> link group. Groups double as topic hints.
_DOMAIN_GROUPS: list[tuple[str, tuple[str, ...]]] = [
    ("tiktok", ("tiktok.com",)),
    ("youtube", ("youtube.com", "youtu.be")),
    ("telegram", ("t.me", "telegram.me", "telegram.org", "tg")),
    ("dev", ("github.com", "gitlab.com", "leetcode.com", "stackoverflow.com", "npmjs.com",
             "pypi.org", "w3.org", "developer.mozilla.org", "docs.python.org", "codepen.io",
             "huggingface.co", "vercel.app", "netlify.app", "dev.to", "medium.com",
             "readthedocs.io", "gist.github.com", "techtarget.com", "cloudflare.com")),
    ("ai", ("openai.com", "chatgpt.com", "anthropic.com", "claude.ai", "quillbot.com",
            "midjourney.com", "perplexity.ai", "gemini.google.com", "suno.com", "runwayml.com")),
    ("apps", ("apps.apple.com", "play.google.com", "chromewebstore.google.com",
              "chrome.google.com", "microsoft.com", "addons.mozilla.org", "producthunt.com")),
    ("security", ("ddosecrets.com", "vxug.s3.eu-west-1.amazonaws.com", "exploit-db.com",
                  "hackerone.com", "shodan.io", "haveibeenpwned.com", "virustotal.com",
                  "veeam.com")),
    ("music", ("spotify.com", "shazam.com", "soundcloud.com", "music.youtube.com",
               "deezer.com", "music.apple.com", "bandcamp.com", "genius.com")),
    ("social", ("instagram.com", "facebook.com", "vk.com", "twitter.com", "x.com",
                "reddit.com", "linkedin.com", "lnkd.in", "threads.net", "pinterest.com")),
    ("shopping", ("amazon.fr", "amazon.com", "aliexpress.com", "aliexpress.ru", "ebay.com",
                  "ebay.fr", "leboncoin.fr", "kufar.by", "wildberries.ru", "ozon.ru",
                  "cdiscount.com", "temu.com", "vinted.fr", "fbags.by", "myfocalize.com",
                  "mapetiteoreille.com")),
    ("business", ("revolut.com", "wallstreetzen.com", "binance.com", "huobi.ug", "paypal.com",
                  "stripe.com", "kodepay.io", "tradingview.com", "coinmarketcap.com")),
    ("health", ("nutrimuscle.com", "theproteinworks.com", "myprotein.com", "doctolib.fr")),
    ("travel", ("ryanair.com", "booking.com", "airbnb.com", "google.com/maps", "maps.app.goo.gl",
                "sncf-connect.com", "blablacar.fr", "skyscanner.net")),
    ("video", ("rezka.ag", "netflix.com", "vimeo.com", "twitch.tv", "kinopoisk.ru")),
    ("files", ("mega.nz", "drive.google.com", "dropbox.com", "wetransfer.com", "disk.yandex.ru")),
]
_GROUP_TOPIC = {"tiktok": "tiktok", "youtube": "video", "telegram": "telegram", "dev": "dev",
                "ai": "ai", "apps": "apps", "security": "security", "music": "music",
                "social": "social", "shopping": "shopping", "business": "business",
                "health": "health", "travel": "travel", "video": "video", "files": "misc"}

# keyword -> topic (RU / FR / EN). First hit in list order wins.
_KEYWORDS: list[tuple[str, re.Pattern]] = [(t, re.compile(p, re.I)) for t, p in [
    ("dev", r"\b(python|javascript|typescript|powershell|bash|docker|git|api|sql|regex|"
            r"npm|node\.?js|react|css|html|json|linux|windows|kubernetes|nginx|backend|frontend)\b"),
    # AI / ИИ only in capitals: lowercase "ai" is French ("j'ai"), "ии" a word fragment
    ("ai", r"\b(gpt|chatgpt|openai|claude|llm|prompts?|midjourney|нейросет\w*|(?-i:AI|ИИ))\b"),
    ("security", r"(osint|хак|hack|exploit|malware|vpn|pentest|cyber|кибер|phishing|фишинг)"),
    ("business", r"(бизнес|business|клиент|client|продаж|vente|стартап|startup|инвест|invest|"
                 r"крипт|crypto|bitcoin|биткоин|бирж|bourse|stock|доход|revenue|freelance|"
                 r"фриланс|entrepreneur|предприним|маркетинг|marketing)"),
    ("health", r"(спорт|трениров|workout|muscu|протеин|protein|whey|диет|régime|здоров|santé|"
               r"врач|médecin|\bсон\b|sommeil)"),
    ("learning", r"(книг|livre|book|курс|cours|course|урок|lesson|учить|apprendre|learn)"),
    ("travel", r"(аэропорт|airport|aéroport|вокзал|gare|отель|hotel|hôtel|билет|billet|"
               r"поездк|voyage|\btrip\b)"),
    ("admin", r"(caf\b|ameli|impots|impôts|préfecture|prefecture|urssaf|pôle emploi|"
              r"france travail|документ|document|паспорт|passeport|титр de séjour|"
              r"titre de séjour|attestation|справк|contrat|договор|facture|счёт)"),
    ("shopping", r"(купить|acheter|buy|заказ|commande|order|цена|prix|price|€|₽)"),
    ("quotes", r"(«|»|цитат|citation|quote|мудрост|sagesse)"),
]]

_CONTENT_TAGS = {"#sdm", "#foryou", "#fyp", "#tiktok", "#tiktokfr", "#foryoupage", "#pourtoi",
                 "#viral", "#trending"}
_FAMILY_SOURCES = re.compile(r"(мам|мама|мамуся|папа|айза|aiza|сестр|брат|бабушк)", re.I)
_TASK_SOURCES = re.compile(r"(notes\\tasks|\\notes|todo|tasks)", re.I)
_DOWNLOADER_SOURCES = re.compile(r"(tik ?tok|тик ?ток|downloader|скачать видео|loader)", re.I)
_LEAK_SOURCES = re.compile(r"(leak|утечк|слив|ddosecrets|data1eaks|combo|дамп|dump|базы данных|"
                           r"база данных|darknet|dark net)", re.I)
_LEAK_FILES = re.compile(r"(\.sql(\.gz)?$|combo|dump|leak|утечк|слив|\bdb\.)", re.I)
_CRED_RE = re.compile(
    r"(?i)\b(pass(?:word)?|passwd|pwd|mot de passe|mdp|пароль|парол|login|логин|username|"
    r"user ?name|token|api[_ -]?key|secret|seed(?: phrase)?|mnemonic|cvv|cvc|pin(?: code)?|"
    r"код доступа)\b\s*[:=]\s*\S+")
_EMAIL_PW_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+\s*[:;|/]\s*\S{4,}")
_IBAN_RE = re.compile(r"\b[A-Z]{2}\d{2}(?:\s?[A-Z0-9]){11,30}\b")
_CARD_RE = re.compile(r"(?<!\d)(?:\d[ -]?){13,19}(?!\d)")

_TRACKING = re.compile(r"^(utm_.*|fbclid|gclid|igshid|igsh|si|_t|_r|k|feature|ref|ref_src|"
                       r"referrer|is_from_webapp|sender_device|share_.*|mibextid|s|t_|"
                       r"offsetinmilliseconds|timeskew|trackingid|aem_.*|__.*)$", re.I)
_EMOJI_RE = re.compile("[\U0001F000-\U0001FAFF\u2600-\u27BF\uFE0F\u200D]")


# --------------------------------------------------------------------------- #
# Loading
# --------------------------------------------------------------------------- #
@dataclass
class Msg:
    raw: dict
    export: str                      # export folder name under _export/ (its date)
    id: int = 0
    date: datetime = field(default_factory=lambda: datetime(1970, 1, 1))
    text: str = ""                   # plain text, entities flattened
    topic: str = "misc"
    reason: str = ""
    sensitive: str = ""              # "" | credentials | finance | leaks

    @property
    def year(self) -> str:
        return f"{self.date:%Y}"


def _flatten(t) -> str:
    if t is None:
        return ""
    if isinstance(t, str):
        return t
    if isinstance(t, list):
        return "".join(_flatten(p) for p in t)
    if isinstance(t, dict):
        if "blocks" in t:
            return "\n".join(_flatten(b) for b in t["blocks"])
        if "items" in t:
            return "\n".join(_flatten(i) for i in t["items"])
        return _flatten(t.get("text", ""))
    return str(t)


def plain_text(raw: dict) -> str:
    """Every human-readable string in the message, flattened (text, rich blocks, poll)."""
    parts = [_flatten(raw.get("text"))]
    if raw.get("rich_message"):
        parts.append(_flatten(raw["rich_message"]))
    if raw.get("poll"):
        p = raw["poll"]
        parts.append(p.get("question", ""))
        parts += [a.get("text", "") for a in p.get("answers", [])]
    for k in ("title", "performer"):
        if raw.get(k):
            parts.append(str(raw[k]))
    return "\n".join(s for s in parts if s)


def list_exports(out: Path) -> list[Path]:
    base = out / _EXPORT
    if not base.is_dir():
        return []
    return sorted(p for p in base.iterdir() if p.is_dir() and (p / "result.json").exists())


def load_messages(out: Path) -> tuple[dict, list[Msg]]:
    """Merge every export under ``_export/`` by message id — a newer export wins."""
    by_id: dict[int, Msg] = {}
    meta: dict = {}
    for exp in list_exports(out):
        data = json.loads((exp / "result.json").read_text(encoding="utf-8"))
        meta = {k: v for k, v in data.items() if k != "messages"} | {"exports": meta.get("exports", []) + [exp.name]}
        for raw in data.get("messages", []):
            m = Msg(raw=raw, export=exp.name, id=int(raw["id"]))
            m.date = datetime.fromisoformat(raw["date"])
            m.text = plain_text(raw)
            by_id[m.id] = m
    msgs = sorted(by_id.values(), key=lambda m: (m.date, m.id))
    return meta, msgs


# --------------------------------------------------------------------------- #
# Media
# --------------------------------------------------------------------------- #
MEDIA_KINDS = {
    "photos": ("Photos", "🖼"), "videos": ("Videos", "🎞"), "circles": ("Circles", "⭕"),
    "voice": ("Voice", "🎙"), "audio": ("Audio", "🎵"), "files": ("Files", "📎"),
    "stickers": ("Stickers", "🏷"),
}


def media_kind(raw: dict) -> str:
    if raw.get("photo"):
        return "photos"
    mt = raw.get("media_type")
    if not raw.get("file"):
        return ""
    return {"video_file": "videos", "animation": "videos", "video_message": "circles",
            "voice_message": "voice", "audio_file": "audio", "sticker": "stickers"}.get(mt, "files")


def media_path(raw: dict) -> str:
    """Export-relative media path, or '' (none) / '!' (Telegram could not export it)."""
    p = raw.get("photo") or raw.get("file") or ""
    if p.startswith("("):
        return "!"
    return p


def _nice_size(n: int | None) -> str:
    n = int(n or 0)
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return ""


def _dur(s) -> str:
    s = int(s or 0)
    return f"{s // 3600}:{s % 3600 // 60:02d}:{s % 60:02d}" if s >= 3600 else f"{s // 60}:{s % 60:02d}"


# --------------------------------------------------------------------------- #
# Markdown rendering
# --------------------------------------------------------------------------- #
_MD_SPECIAL = re.compile(r"([\\`*_\[\]<>|~])")


def esc(s: str) -> str:
    """Escape user text so it renders literally (no stray HTML, emphasis, tables)."""
    s = _MD_SPECIAL.sub(r"\\\1", s)
    # a line starting with '#', '-', '+', '=' or 'N.' would become structure
    s = re.sub(r"(?m)^(\s*)([#=+-]|\d+\.)(?=\s|$)", r"\1\\\2", s)
    return s


def _wrap(s: str, left: str, right: str | None = None) -> str:
    """Apply an inline marker per line, keeping edge whitespace outside it."""
    right = left if right is None else right
    out = []
    for line in s.split("\n"):
        core = line.strip()
        if not core:
            out.append(line)
            continue
        lead = line[: len(line) - len(line.lstrip())]
        trail = line[len(line.rstrip()):]
        out.append(f"{lead}{left}{core}{right}{trail}")
    return "\n".join(out)


def _code_span(s: str) -> str:
    ticks = max((len(r) for r in re.findall(r"`+", s)), default=0) + 1
    pad = " " if s.startswith("`") or s.endswith("`") else ""
    return f"{'`' * ticks}{pad}{s}{pad}{'`' * ticks}"


def _fence(s: str, lang: str = "") -> str:
    ticks = max(3, max((len(r) for r in re.findall(r"`+", s)), default=0) + 1)
    return f"\n\n{'`' * ticks}{lang}\n{s.strip(chr(10))}\n{'`' * ticks}\n\n"


def href(url: str) -> str:
    u = url.strip()
    if not re.match(r"^[a-z][a-z0-9+.-]*:", u, re.I):
        u = "https://" + u
    return u.replace(" ", "%20").replace("(", "%28").replace(")", "%29").replace("|", "%7C")


def render_entities(entities: list[dict]) -> str:
    out: list[str] = []
    for e in entities:
        t, s = e.get("type"), e.get("text", "")
        if t in ("plain", "phone", "bank_card", "cashtag", "custom_emoji", "email", "bot_command"):
            out.append(esc(s))
        elif t == "bold":
            out.append(_wrap(esc(s), "**"))
        elif t == "italic":
            out.append(_wrap(esc(s), "*"))
        elif t == "underline":
            out.append(_wrap(esc(s), "<u>", "</u>"))
        elif t == "strikethrough":
            out.append(_wrap(esc(s), "~~"))
        elif t == "spoiler":
            out.append(_wrap(esc(s), "‖"))
        elif t == "code":
            out.append(_code_span(s) if "\n" not in s else _fence(s))
        elif t == "pre":
            out.append(_fence(s, e.get("language", "") or ""))
        elif t == "blockquote":
            out.append("\n\n" + "\n".join("> " + ln for ln in esc(s).split("\n")) + "\n\n")
        elif t == "link":
            out.append(f"[{esc(s)}]({href(s)})")
        elif t == "text_link":
            out.append(f"[{esc(s) or esc(e.get('href', ''))}]({href(e.get('href', ''))})")
        elif t == "mention":
            out.append(f"[{esc(s)}](https://t.me/{s.lstrip('@')})")
        elif t == "hashtag":
            out.append(s)                       # left raw: a tag in Obsidian
        else:                                   # mention_name and anything new
            out.append(esc(s))
    return "".join(out)


def _rich_inline(t) -> str:
    if t is None:
        return ""
    if isinstance(t, str):
        return esc(t)
    if isinstance(t, list):
        return "".join(_rich_inline(x) for x in t)
    kind, inner = t.get("type"), t.get("text")
    body = _rich_inline(inner)
    return {"bold": lambda: _wrap(body, "**"), "italic": lambda: _wrap(body, "*"),
            "code": lambda: _code_span(_flatten(inner)),
            "strikethrough": lambda: _wrap(body, "~~"),
            "underline": lambda: _wrap(body, "<u>", "</u>")}.get(kind, lambda: body)()


def render_rich(rich: dict) -> str:
    out: list[str] = []
    for b in rich.get("blocks", []):
        kind = b.get("type")
        if kind == "heading":
            out.append(f"**{_rich_inline(b.get('text')).strip()}**")
        elif kind == "divider":
            out.append("* * *")
        elif kind == "list":
            ordered = b.get("kind") == "ordered"
            for i, it in enumerate(b.get("items", []), 1):
                mark = f"{it.get('num') or i}." if ordered else "-"
                box = {"checked": "[x] ", "unchecked": "[ ] "}.get(it.get("task_state"), "")
                out.append(f"{mark} {box}{_rich_inline(it.get('text'))}")
        else:
            out.append(_rich_inline(b.get("text")))
    return "\n\n".join(out)


def hard_breaks(md: str) -> str:
    """Telegram newlines are line breaks; Markdown needs them spelled out (outside fences)."""
    out, fenced = [], False
    lines = md.split("\n")
    for i, line in enumerate(lines):
        if re.match(r"^`{3,}", line):
            fenced = not fenced
            out.append(line)
            continue
        nxt = lines[i + 1] if i + 1 < len(lines) else ""
        if not fenced and line.strip() and nxt.strip() and not re.match(r"^`{3,}", nxt) \
                and not line.startswith(("> ", "- ", "* * *")) and not re.match(r"^\d+\. ", line):
            line = line.rstrip() + "  "
        out.append(line)
    return re.sub(r"\n{3,}", "\n\n", "\n".join(out)).strip()


def body_md(raw: dict) -> str:
    parts: list[str] = []
    ents = raw.get("text_entities")
    if ents:
        parts.append(render_entities(ents))
    elif isinstance(raw.get("text"), str) and raw["text"]:
        parts.append(esc(raw["text"]))
    if raw.get("rich_message"):
        parts.append(render_rich(raw["rich_message"]))
    if raw.get("poll"):
        p = raw["poll"]
        lines = [f"📊 **Poll:** {esc(p.get('question', ''))} ({p.get('total_voters', 0)} voter(s))"]
        for a in p.get("answers", []):
            lines.append(f"- {'✔ ' if a.get('chosen') else ''}{esc(a.get('text', ''))} — {a.get('voters', 0)}")
        parts.append("\n".join(lines))
    if raw.get("contact_information"):
        c = raw["contact_information"]
        parts.append(f"👤 **Contact:** {esc(c.get('first_name', ''))} {esc(c.get('last_name', ''))} "
                     f"{esc(c.get('phone_number', ''))}")
    if raw.get("location_information"):
        loc = raw["location_information"]
        lat, lon = loc.get("latitude"), loc.get("longitude")
        parts.append(f"📍 [{lat}, {lon}](https://www.openstreetmap.org/?mlat={lat}&mlon={lon})")
    if raw.get("inline_bot_buttons"):
        btns = [b for row in raw["inline_bot_buttons"] for b in row]
        parts.append(" · ".join(
            f"[{esc(b.get('text', ''))}]({href(b['data'])})" if str(b.get("data", "")).startswith("http")
            else f"`{b.get('text', '')}`" for b in btns))
    if raw.get("type") == "service":
        parts.append(f"*service:* `{raw.get('action', '')}` by {esc(raw.get('actor', '') or '')}")
    return hard_breaks("\n\n".join(p for p in parts if p))


# --------------------------------------------------------------------------- #
# Links
# --------------------------------------------------------------------------- #
def message_links(raw: dict) -> list[str]:
    urls: list[str] = []
    for e in raw.get("text_entities") or []:
        if e.get("type") == "link":
            urls.append(e.get("text", ""))
        elif e.get("type") == "text_link":
            urls.append(e.get("href", ""))
    for row in raw.get("inline_bot_buttons") or []:
        for b in row:
            if str(b.get("data", "")).startswith("http"):
                urls.append(b["data"])
    seen, out = set(), []
    for u in urls:
        u = u.strip().rstrip(".,)…")
        if u and u not in seen:
            seen.add(u)
            out.append(u)
    return out


def canonical_url(url: str) -> str:
    """Scheme-less, tracking-free identity of a link: saved-twice detection key."""
    u = url.strip()
    if not re.match(r"^[a-z][a-z0-9+.-]*:", u, re.I):
        u = "https://" + u
    try:
        parts = urlsplit(u)
    except ValueError:
        return url.strip().lower()
    if parts.scheme.lower() not in ("http", "https"):
        return u.lower()                               # tg:, mailto:, … compared as-is
    host = (parts.hostname or "").lower().removeprefix("www.").removeprefix("m.")
    path = parts.path.rstrip("/")
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if not _TRACKING.match(k)]
    if host == "youtu.be" and path:
        host, query, path = "youtube.com", [("v", path.lstrip("/"))] + query, "/watch"
    if host == "youtube.com" and path.startswith("/shorts/"):
        query, path = [("v", path.split("/")[2])] + query, "/watch"
    return urlunsplit(("", host, path, urlencode(sorted(query)), "")).lstrip("/")


def link_group(url: str) -> str:
    c = canonical_url(url)
    if c.startswith("tg:"):
        return "telegram"
    host_path = c.split("?")[0]
    host = host_path.split("/")[0]
    for group, doms in _DOMAIN_GROUPS:
        for d in doms:
            if "/" in d and host_path.startswith(d):
                return group
            if host == d or host.endswith("." + d):
                return group
    return "web"


# --------------------------------------------------------------------------- #
# Sensitive routing + topics
# --------------------------------------------------------------------------- #
def _iban_ok(s: str) -> bool:
    s = re.sub(r"\s", "", s).upper()
    if not 15 <= len(s) <= 34:
        return False
    n = "".join(str(int(c, 36)) for c in s[4:] + s[:4])
    return int(n) % 97 == 1


def _luhn_ok(digits: str) -> bool:
    d = [int(c) for c in digits][::-1]
    return (sum(d[0::2]) + sum(sum(divmod(2 * x, 10)) for x in d[1::2])) % 10 == 0


def sensitive_kind(raw: dict, text: str) -> str:
    src = f"{raw.get('forwarded_from') or ''} {raw.get('saved_from') or ''}"
    fname = raw.get("file_name") or raw.get("file") or ""
    if _LEAK_SOURCES.search(src) or (fname and _LEAK_FILES.search(fname)) \
            or any(link_group(u) == "security" and "ddosecrets" in u for u in message_links(raw)):
        return "leaks"
    if _CRED_RE.search(text) or _EMAIL_PW_RE.search(text):
        return "credentials"
    ents = raw.get("text_entities") or []
    if any(e.get("type") == "bank_card" for e in ents):
        return "finance"
    if any(_iban_ok(m.group(0)) for m in _IBAN_RE.finditer(text)):
        return "finance"
    for m in _CARD_RE.finditer(text):
        digits = re.sub(r"\D", "", m.group(0))
        if 13 <= len(digits) <= 19 and digits[0] in "3456" and _luhn_ok(digits):
            return "finance"
    return ""


def rule_topic(m: Msg) -> tuple[str, str]:
    raw, text = m.raw, m.text
    if raw.get("type") == "service":
        return "system", "service message"
    src = f"{raw.get('forwarded_from') or ''} {raw.get('saved_from') or ''}".strip()
    kind = media_kind(raw)
    if src and _TASK_SOURCES.search(src):
        return "tasks", f"from {src}"
    if src and _FAMILY_SOURCES.search(src):
        return "family", f"from {src}"
    if kind in ("voice", "circles"):
        return "voice", kind
    tags = {e.get("text", "").lower() for e in raw.get("text_entities") or [] if e.get("type") == "hashtag"}
    if tags & _CONTENT_TAGS or len(tags) >= 4:
        return "content", "hashtags"
    links = message_links(raw)
    if links:
        groups = Counter(link_group(u) for u in links)
        for g, _ in groups.most_common():
            if g in _GROUP_TOPIC and _GROUP_TOPIC[g] != "misc":
                return _GROUP_TOPIC[g], f"link: {g}"
    if src and _DOWNLOADER_SOURCES.search(src) and kind == "videos":
        return "tiktok", f"from {src}"
    if kind == "audio":
        return "music", "audio file"
    if kind == "stickers" or raw.get("media_type") == "animation":
        return "fun", kind or "animation"
    if any(e.get("type") in ("pre", "code") for e in raw.get("text_entities") or []):
        return "dev", "code block"
    if kind == "files":
        mime = raw.get("mime_type") or ""
        name = (raw.get("file_name") or "").lower()
        if mime in ("application/epub+zip", "application/pdf") or name.endswith((".epub", ".fb2", ".djvu")):
            return "learning", "document"
        if mime == "application/x-bittorrent":
            return "video", "torrent"
    for topic, rx in _KEYWORDS:
        if rx.search(text):
            return topic, f"keyword: {rx.search(text).group(0)[:24]}"
    if links:
        return "misc", "link: web"
    if kind == "photos" and not text.strip():
        return "images", "photo without text"
    if kind == "videos":
        return "video", "video file"
    if len(text) >= 280 and not raw.get("forwarded_from"):
        return "ideas", "own long note"
    return "misc", "no rule matched"


def load_overrides(out: Path) -> dict[int, tuple[str, str]]:
    f = out / _STATE / "topics.overrides.csv"
    if not f.exists():
        return {}
    res = {}
    with f.open(encoding="utf-8", newline="") as fh:
        for row in csv.DictReader(fh):
            try:
                mid, topic = int(row["id"]), row["topic"].strip()
            except (KeyError, ValueError):
                continue
            if topic in _TOPIC or topic.startswith("sensitive/"):
                res[mid] = (topic, (row.get("reason") or "override").strip())
    return res


def classify(msgs: list[Msg], overrides: dict[int, tuple[str, str]]) -> None:
    for m in msgs:
        m.sensitive = sensitive_kind(m.raw, m.text)
        m.topic, m.reason = rule_topic(m)
    # a link + its comment, sent seconds apart, are one thought: weak topics inherit
    group: list[Msg] = []

    def flush():
        strong = next((g.topic for g in group if g.topic not in _WEAK and g.topic != "system"), None)
        if strong:
            for g in group:
                if g.topic in _WEAK:
                    g.topic, g.reason = strong, "same note (sent together)"

    prev = None
    for m in msgs:
        if prev and (m.date - prev.date).total_seconds() > _BURST_SECONDS:
            flush()
            group = []
        group.append(m)
        prev = m
    flush()
    for m in msgs:
        if m.id in overrides:
            topic, reason = overrides[m.id]
            if topic.startswith("sensitive/"):
                m.sensitive = topic.split("/", 1)[1] if topic.split("/", 1)[1] in SENSITIVE_KINDS else "credentials"
            else:
                m.topic, m.sensitive = topic, ""
            m.reason = f"override: {reason}"


# --------------------------------------------------------------------------- #
# Copy + hashing
# --------------------------------------------------------------------------- #
def _sha256_copy(src: Path, dst: Path) -> str:
    h = hashlib.sha256()
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_name(dst.name + ".partial")
    with src.open("rb") as fi, tmp.open("wb") as fo:
        for chunk in iter(lambda: fi.read(4 << 20), b""):
            h.update(chunk)
            fo.write(chunk)
    shutil.copystat(src, tmp)
    os.replace(tmp, dst)
    return h.hexdigest()


def read_manifest(path: Path) -> dict[str, str]:
    """``sha256  relpath`` lines → {relpath: sha256}."""
    out: dict[str, str] = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            if "  " in line:
                h, rel = line.split("  ", 1)
                out[rel] = h
    return out


def copy_export(src: Path, out: Path, log=console.print) -> tuple[Path, dict[str, str]]:
    """Copy ``src`` to ``out/_export/<date>`` hashing on the way, then re-hash the copy.

    Resumable: files already in the manifest with a matching size are skipped."""
    m = re.search(r"(\d{4}-\d{2}-\d{2})", src.name)
    dest = out / _EXPORT / (m.group(1) if m else src.name)
    manifest_path = out / _STATE / f"manifest-{dest.name}{_MANIFEST_SUFFIX}"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    done = read_manifest(manifest_path)
    files = sorted(p for p in src.rglob("*") if p.is_file())
    total = sum(p.stat().st_size for p in files)
    copied = 0
    with manifest_path.open("a", encoding="utf-8") as mf:
        for i, f in enumerate(files, 1):
            rel = f.relative_to(src).as_posix()
            target = dest / rel
            copied += f.stat().st_size
            if rel in done and target.exists() and target.stat().st_size == f.stat().st_size:
                continue
            h = _sha256_copy(f, target)
            check = full_hash(str(target))
            if check != h:
                raise RuntimeError(f"hash mismatch after copy: {rel}")
            mf.write(f"{h}  {rel}\n")
            mf.flush()
            done[rel] = h
            if i % 200 == 0 or f.stat().st_size > 200 << 20:
                log(f"  copied {i}/{len(files)} files · {copied / 1e9:.1f}/{total / 1e9:.1f} GB")
    missing = [f.relative_to(src).as_posix() for f in files if f.relative_to(src).as_posix() not in done]
    if missing:
        raise RuntimeError(f"{len(missing)} file(s) not copied, e.g. {missing[:3]}")
    return dest, done


# --------------------------------------------------------------------------- #
# Duplicates
# --------------------------------------------------------------------------- #
def _dupe_rank(rel: str) -> tuple:
    name = rel.rsplit("/", 1)[-1]
    clone = bool(re.search(r"\s\(\d+\)(\.[^.]+)?$", name))
    return (clone, len(rel), rel)


def duplicate_map(out: Path, msgs: list[Msg]) -> dict[str, str]:
    """{"<export>/<rel>": "<export>/<rel> canonical"} for byte-identical referenced media."""
    referenced = {f"{m.export}/{media_path(m.raw)}" for m in msgs if media_path(m.raw) not in ("", "!")}
    by_hash: dict[str, list[str]] = defaultdict(list)
    for exp in list_exports(out):
        for rel, h in read_manifest(out / _STATE / f"manifest-{exp.name}{_MANIFEST_SUFFIX}").items():
            key = f"{exp.name}/{rel}"
            if key in referenced:
                by_hash[h].append(key)
    res: dict[str, str] = {}
    for keys in by_hash.values():
        if len(keys) < 2:
            continue
        keep = min(keys, key=_dupe_rank)
        for k in keys:
            if k != keep:
                res[k] = keep
    return res


def _hash_of(out: Path) -> dict[str, str]:
    res: dict[str, str] = {}
    for exp in list_exports(out):
        for rel, h in read_manifest(out / _STATE / f"manifest-{exp.name}{_MANIFEST_SUFFIX}").items():
            res[f"{exp.name}/{rel}"] = h
    return res


# --------------------------------------------------------------------------- #
# Building the library
# --------------------------------------------------------------------------- #
class Lib:
    """Everything the renderers share: messages, where each lives, and how to link."""

    def __init__(self, out: Path, meta: dict, msgs: list[Msg], dupes: dict[str, str], hashes: dict[str, str]):
        self.out, self.meta, self.msgs, self.dupes, self.hashes = out, meta, msgs, dupes, hashes
        self.by_id = {m.id: m for m in msgs}
        self.files: dict[str, list[str]] = {}          # relpath -> lines

    # -- paths ----------------------------------------------------------------
    def topic_file(self, slug: str) -> str:
        return f"Topics/{TOPIC_SLUGS.index(slug) + 1:02d}-{slug}.md"

    def home(self, m: Msg) -> str:
        """The one file (besides the timeline) that owns this message."""
        return f"{_SENSITIVE}/{m.sensitive}.md" if m.sensitive else self.topic_file(m.topic)

    @staticmethod
    def timeline_file(m: Msg) -> str:
        return f"Timeline/{m.year}.md"

    def rel(self, from_file: str, target: str) -> str:
        return quote(os.path.relpath(target, os.path.dirname(from_file) or ".").replace("\\", "/"), safe="/")

    def media_target(self, m: Msg, path: str | None = None) -> str:
        key = f"{m.export}/{path or media_path(m.raw)}"
        return f"{_EXPORT}/{self.dupes.get(key, key)}"

    def anchor_link(self, from_file: str, m: Msg, where: str = "timeline") -> str:
        target = self.timeline_file(m) if where == "timeline" else self.home(m)
        return f"{self.rel(from_file, target)}#m-{m.id}"

    # -- one message ------------------------------------------------------------
    def cover(self, f: str, m: Msg, height: int = 64) -> str:
        """Telegram's preview image for audio covers, documents and stickers ('' if none)."""
        t = m.raw.get("thumbnail")
        if not t or media_kind(m.raw) not in ("audio", "files", "stickers"):
            return ""
        return f'<img src="{self.rel(f, self.media_target(m, t))}" height="{height}" alt="cover"> '

    def media_md(self, f: str, m: Msg) -> str:
        raw, kind, p = m.raw, media_kind(m.raw), media_path(m.raw)
        if p == "!":
            return "⚠ *media unavailable — Telegram could not export this file*"
        if not kind:
            return ""
        src = self.rel(f, self.media_target(m))
        size = _nice_size(raw.get("photo_file_size") or raw.get("file_size"))
        name = esc(raw.get("file_name") or p.rsplit("/", 1)[-1])
        if kind == "photos":
            return f'<a href="{src}"><img src="{src}" width="360" alt="photo"></a>'
        if kind in ("videos", "circles"):
            poster = ""
            if raw.get("thumbnail"):
                poster = f' poster="{self.rel(f, self.media_target(m, raw["thumbnail"]))}"'
            w = 240 if kind == "circles" else 360
            label = "⭕ circle" if kind == "circles" else f"🎞 {name}"
            return (f'<video src="{src}"{poster} controls width="{w}"></video>\n\n'
                    f"[{label}]({src}) · {_dur(raw.get('duration_seconds'))} · {size}")
        if kind in ("voice", "audio"):
            title = "🎙 voice" if kind == "voice" else "🎵 " + esc(
                " – ".join(x for x in (raw.get("performer"), raw.get("title")) if x) or name)
            return (f'<audio src="{src}" controls></audio>\n\n'
                    f"{self.cover(f, m)}[{title}]({src}) · {_dur(raw.get('duration_seconds'))} · {size}")
        if kind == "stickers":
            if p.endswith(".webp"):
                return f'<img src="{src}" width="128" alt="sticker {raw.get("sticker_emoji", "")}">'
            return f"{self.cover(f, m)}[🏷 sticker {raw.get('sticker_emoji', '')}]({src})"
        mime = raw.get("mime_type") or ""
        if mime.startswith("image/") and not p.endswith((".heic", ".svg")):
            return f'<a href="{src}"><img src="{src}" width="360" alt="{name}"></a>'
        return f"{self.cover(f, m)}📎 [{name}]({src}) · {mime or 'file'} · {size}"

    def header(self, f: str, m: Msg, where: str) -> str:
        raw = m.raw
        bits = [f"{m.date:%Y-%m-%d %H:%M}"]
        fwd = raw.get("forwarded_from") or raw.get("saved_from")
        if fwd:
            bits.append(f"↪ {esc(str(fwd))}")
        if raw.get("edited"):
            bits.append("✎")
        if raw.get("reply_to_message_id") in self.by_id:
            r = self.by_id[raw["reply_to_message_id"]]
            bits.append(f"[↩ reply]({self.anchor_link(f, r, 'timeline')})")
        if where == "timeline":
            if m.sensitive:
                t = SENSITIVE_KINDS[m.sensitive]
                bits.append(f"[{t[1]} {t[0]}]({self.anchor_link(f, m, 'home')})")
            else:
                t = _TOPIC[m.topic]
                bits.append(f"[{t[2]} {t[1]}]({self.anchor_link(f, m, 'home')})")
        else:
            bits.append(f"[🕓 timeline]({self.anchor_link(f, m, 'timeline')})")
        return f'<a id="m-{m.id}"></a>\n#### {" · ".join(bits)}'

    def message_md(self, f: str, m: Msg, where: str) -> str:
        head = self.header(f, m, where)
        if where == "timeline" and m.sensitive:
            return f"{head}\n\n🔒 *sensitive — kept in `{_SENSITIVE}/{m.sensitive}.md`*\n"
        parts = [head]
        media = self.media_md(f, m)
        if media:
            parts.append(media)
        body = body_md(m.raw)
        if body:
            parts.append(body)
        if raw_reacts := m.raw.get("reactions"):
            parts.append(" ".join(f"{r.get('emoji', '')}×{r.get('count', 1)}" for r in raw_reacts))
        return "\n\n".join(parts) + "\n"

    def by_month(self, f: str, msgs: list[Msg], where: str, lines: list[str]) -> None:
        cur_year = cur_month = None
        for m in msgs:
            if f"{m.year}" != cur_year and where != "timeline":
                cur_year = m.year
                lines.append(f"\n## {cur_year}\n")
            if f"{m.date:%Y-%m}" != cur_month:
                cur_month = f"{m.date:%Y-%m}"
                lines.append(f"\n{'##' if where == 'timeline' else '###'} {m.date:%B %Y}\n")
            lines.append(self.message_md(f, m, where))

    # -- files --------------------------------------------------------------------
    def build(self) -> None:
        self.build_timeline()
        self.build_topics()
        self.build_sensitive()
        self.build_links()
        self.build_media()
        self.build_sources()
        self.build_similar()
        self.build_readme()
        self.build_checklist()

    def build_checklist(self) -> None:
        last = self.msgs[-1].date if self.msgs else None
        unavailable = [m for m in self.msgs if media_path(m.raw) == "!"]
        lines = [
            "# 🧹 Before you clear the chat in Telegram\n",
            "Clearing the chat history in Telegram **cannot be undone**. This folder becomes the only copy, so do "
            "these in order and tick them off.\n",
            f"- [ ] **Catch up.** The newest message archived here is from **{last:%d %b %Y %H:%M}**. "
            "Anything saved after that is not here yet. Export the chat again from Telegram Desktop "
            "(⋮ → Export chat history → *Machine-readable JSON*, all media ticked), then run:\n"
            "  `navig telegram-exports notebook build \"<new ChatExport folder>\" --out \"<this folder>\" --apply`\n"
            "  It adds only what is new, matched by message id." if last else "",
            f"- [ ] **Missing files.** {len(unavailable)} file(s) could not be exported "
            + (f"({', '.join(f'[{m.date:%Y-%m-%d}]({self.anchor_link(self.CHECKLIST, m)})' for m in unavailable[:10])}). "
               "Open each one in Telegram and download it if it still matters." if unavailable else "— nothing to do."),
            "- [ ] **Check it.** `navig telegram-exports notebook verify \"<this folder>\" --deep` must end in **PASS**.",
            "- [ ] **Second copy.** Copy this whole folder to another drive. Until you do, it is the only copy.",
            "- [ ] **Open it.** Open [README](README.md), one year in Timeline, and Media → Circles, and check that "
            "pictures and videos open.",
            "- [ ] **Clear it.** In Telegram: Saved Messages → ⋮ → *Clear history*.\n",
            "> Stickers, reactions and the look of Telegram's own viewer are the only things not carried over as-is; "
            "Telegram's original HTML export is kept in `_export/`.",
        ]
        self.files[self.CHECKLIST] = [ln for ln in lines if ln]

    CHECKLIST = "CLEANUP-CHECKLIST.md"

    def build_timeline(self) -> None:
        years = sorted({m.year for m in self.msgs})
        for y in years:
            f = f"Timeline/{y}.md"
            ms = [m for m in self.msgs if m.year == y]
            nav = " · ".join(f"**{x}**" if x == y else f"[{x}]({x}.md)" for x in years)
            lines = [f"# {y} — {len(ms)} messages\n", f"[🏠 README](../README.md) · {nav}\n"]
            self.by_month(f, ms, "timeline", lines)
            self.files[f] = lines

    def build_topics(self) -> None:
        for slug, title, emoji in TOPICS:
            ms = [m for m in self.msgs if not m.sensitive and m.topic == slug]
            if not ms:
                continue
            f = self.topic_file(slug)
            lines = [f"# {emoji} {title} — {len(ms)} messages\n",
                     f"[🏠 README](../README.md) · {ms[0].date:%Y-%m-%d} → {ms[-1].date:%Y-%m-%d}\n"]
            reasons = Counter(re.sub(r":.*", "", m.reason) for m in ms)
            lines.append("<sub>sorted by: " + ", ".join(f"{k} ({v})" for k, v in reasons.most_common()) + "</sub>\n")
            self.by_month(f, ms, "topic", lines)
            self.files[f] = lines

    def build_sensitive(self) -> None:
        for kind, (title, emoji, what) in SENSITIVE_KINDS.items():
            ms = [m for m in self.msgs if m.sensitive == kind]
            if not ms:
                continue
            f = f"{_SENSITIVE}/{kind}.md"
            lines = [f"# {emoji} {title} — {len(ms)} messages\n",
                     f"> 🔒 **Private.** {what}. Kept verbatim so nothing is lost; do not share this folder.\n",
                     "[🏠 README](../README.md)\n"]
            self.by_month(f, ms, "topic", lines)
            self.files[f] = lines

    def link_index(self) -> dict[str, dict]:
        idx: dict[str, dict] = {}
        for m in self.msgs:
            for u in message_links(m.raw):
                c = canonical_url(u)
                e = idx.setdefault(c, {"url": u, "refs": [], "group": link_group(u)})
                if m.id not in e["refs"]:
                    e["refs"].append(m.id)
        return idx

    def refs_md(self, f: str, ids: list[int], limit: int = 6) -> str:
        shown = [f"[{self.by_id[i].date:%Y-%m-%d}]({self.anchor_link(f, self.by_id[i])})" for i in ids[:limit]]
        more = f" +{len(ids) - limit}" if len(ids) > limit else ""
        return ", ".join(shown) + more

    def build_links(self) -> None:
        idx = self.link_index()
        groups: dict[str, list[tuple[str, dict]]] = defaultdict(list)
        for c, e in idx.items():
            groups[e["group"]].append((c, e))
        readme = ["# 🔗 Links\n", f"[🏠 README](../README.md) · **{len(idx)}** unique links "
                  f"({sum(len(e['refs']) for e in idx.values())} mentions)\n",
                  "| group | links | saved more than once |", "|---|--:|--:|"]
        for g in sorted(groups, key=lambda g: -len(groups[g])):
            items = groups[g]
            readme.append(f"| [{g}]({g}.md) | {len(items)} | {sum(len(e['refs']) > 1 for _, e in items)} |")
            f = f"Links/{g}.md"
            doms = Counter(c.split("/")[0] for c, _ in items)
            lines = [f"# 🔗 {g} — {len(items)} links\n", "[↑ Links](README.md) · [🏠 README](../README.md)\n",
                     "Top domains: " + ", ".join(f"`{d}` {n}" for d, n in doms.most_common(12)) + "\n",
                     "| link | saved | first | last | messages |", "|---|--:|---|---|---|"]
            items.sort(key=lambda ce: (self.by_id[ce[1]["refs"][0]].date, ce[0]))
            for c, e in items:
                first, last = self.by_id[e["refs"][0]], self.by_id[e["refs"][-1]]
                shown = esc(c if len(c) <= 90 else c[:87] + "…")
                sensitive = any(self.by_id[i].sensitive for i in e["refs"])
                lock = "🔒 " if sensitive else ""
                lines.append(f"| {lock}[{shown}]({href(e['url'])}) | {len(e['refs'])} | {first.date:%Y-%m-%d} "
                             f"| {last.date:%Y-%m-%d} | {self.refs_md(f, e['refs'], 4)} |")
            self.files[f] = lines
        self.files["Links/README.md"] = readme

    def build_media(self) -> None:
        by_kind: dict[str, list[Msg]] = defaultdict(list)
        for m in self.msgs:
            k = media_kind(m.raw)
            if k and media_path(m.raw) != "!":
                by_kind[k].append(m)
        nav = " · ".join(f"[{v[1]} {v[0]}]({v[0]}.md)" for v in MEDIA_KINDS.values())
        for kind, (title, emoji) in MEDIA_KINDS.items():
            f = f"Media/{title}.md"
            ms = by_kind.get(kind, [])
            size = sum(int(m.raw.get("photo_file_size") or m.raw.get("file_size") or 0) for m in ms)
            lines = [f"# {emoji} {title} — {len(ms)} ({_nice_size(size)})\n", f"[🏠 README](../README.md) · {nav}\n"]
            year = None
            for m in ms:
                if m.year != year:
                    year = m.year
                    lines.append(f"\n## {year}\n")
                lines.append(self.gallery_item(f, m, kind))
            if kind == "stickers":
                lines += self.unreferenced_section(f)
            self.files[f] = lines

    def gallery_item(self, f: str, m: Msg, kind: str) -> str:
        raw = m.raw
        src = self.rel(f, self.media_target(m))
        link = f"[{m.date:%Y-%m-%d}]({self.anchor_link(f, m)})"
        lock = "🔒 " if m.sensitive else ""
        if kind == "photos" and m.sensitive:          # linked, never shown inline
            thumb_rel = re.sub(r"(\.[^.]+)$", r"_thumb\1", media_path(raw))
            thumb = f" · [thumb]({self.rel(f, self.media_target(m, thumb_rel))})" \
                if f"{m.export}/{thumb_rel}" in self.hashes else ""
            return f"\n🔒 [photo]({src}){thumb} · {link}\n"
        if kind == "photos":
            thumb_rel = re.sub(r"(\.[^.]+)$", r"_thumb\1", media_path(raw))
            if f"{m.export}/{thumb_rel}" in self.hashes:
                thumb = self.rel(f, self.media_target(m, thumb_rel))
            else:
                thumb = src
            return f'<a href="{src}"><img src="{thumb}" height="120" title="{m.date:%Y-%m-%d} #{m.id}"></a>'
        if kind in ("videos", "circles"):
            name = esc(raw.get("file_name") or "circle")
            poster = ""
            if raw.get("thumbnail"):
                poster = f'<img src="{self.rel(f, self.media_target(m, raw["thumbnail"]))}" height="90"> '
            if kind == "circles":
                return f'<video src="{src}" controls width="200"></video> {link}\n'
            return (f"- {poster}{lock}[{name}]({src}) · {_dur(raw.get('duration_seconds'))} · "
                    f"{_nice_size(raw.get('file_size'))} · {link}")
        if kind == "voice":
            return f'- {lock}{link} · {_dur(raw.get("duration_seconds"))} <audio src="{src}" controls></audio>'
        if kind == "audio":
            t = " – ".join(x for x in (raw.get("performer"), raw.get("title")) if x) or raw.get("file_name", "")
            return f"- {self.cover(f, m, 40)}{lock}[{esc(t)}]({src}) · {_dur(raw.get('duration_seconds'))} · " \
                   f"{_nice_size(raw.get('file_size'))} · {link}"
        if kind == "stickers":
            p = media_path(raw)
            if p.endswith(".webp"):
                img = f'<img src="{src}" height="96" title="{raw.get("sticker_emoji", "")}">'
                if raw.get("thumbnail"):               # click through to Telegram's preview
                    return f'<a href="{self.rel(f, self.media_target(m, raw["thumbnail"]))}">{img}</a>'
                return img
            return f"- {self.cover(f, m, 40)}[{raw.get('sticker_emoji', '')} {esc(p.rsplit('/', 1)[-1])}]({src}) · {link}"
        mime = raw.get("mime_type") or "file"
        return f"- {self.cover(f, m, 40)}{lock}📎 [{esc(raw.get('file_name') or media_path(raw))}]({src}) · " \
               f"`{mime}` · {_nice_size(raw.get('file_size'))} · {link}"

    def unreferenced(self) -> list[str]:
        """Export files no message points at (Telegram writes extra sticker/thumb files)."""
        used = set()
        for m in self.msgs:
            for k in ("photo", "file", "thumbnail"):
                if isinstance(m.raw.get(k), str):
                    used.add(f"{m.export}/{m.raw[k]}")
            p = media_path(m.raw)
            if media_kind(m.raw) == "photos":
                used.add(f"{m.export}/" + re.sub(r"(\.[^.]+)$", r"_thumb\1", p))
        res = []
        for key in self.hashes:
            exp, rel = key.split("/", 1)
            if rel.split("/", 1)[0] in _SKIP_EXPORT_DIRS or "/" not in rel:
                continue
            if key not in used:
                res.append(key)
        return sorted(res)

    def unreferenced_section(self, f: str) -> list[str]:
        un = self.unreferenced()
        if not un:
            return []
        lines = [f"\n## Files in the export no message points to ({len(un)})\n",
                 "Telegram writes these alongside stickers and previews; kept and listed so nothing is invisible.\n"]
        for key in un:
            lines.append(f"- [{esc(key.split('/', 1)[1])}]({self.rel(f, f'{_EXPORT}/{key}')})")
        return lines

    def build_sources(self) -> None:
        f = "Sources/Forwards.md"
        src: dict[str, list[Msg]] = defaultdict(list)
        for m in self.msgs:
            s = m.raw.get("forwarded_from") or m.raw.get("saved_from")
            if s:
                src[str(s)].append(m)
        lines = ["# ↪ Forward sources\n", f"[🏠 README](../README.md) · {len(src)} sources, "
                 f"{sum(len(v) for v in src.values())} forwarded messages\n",
                 "| source | messages | first | last | mostly |", "|---|--:|---|---|---|"]
        for s, ms in sorted(src.items(), key=lambda kv: (-len(kv[1]), kv[0])):
            top = Counter(("🔒 " + m.sensitive) if m.sensitive else _TOPIC[m.topic][1] for m in ms).most_common(1)[0][0]
            lines.append(f"| {esc(s)} | {len(ms)} | {ms[0].date:%Y-%m-%d} | {ms[-1].date:%Y-%m-%d} | {top} |")
        lines.append("\n## Messages by source\n")
        for s, ms in sorted(src.items(), key=lambda kv: (-len(kv[1]), kv[0])):
            lines.append(f"### {esc(s)} ({len(ms)})\n")
            lines.append(self.refs_md(f, [m.id for m in ms], 40) + "\n")
        self.files[f] = lines

    def build_similar(self) -> None:
        f = "Similar.md"
        norm: dict[str, list[int]] = defaultdict(list)
        for m in self.msgs:
            t = _EMOJI_RE.sub("", m.text).casefold()
            t = re.sub(r"\s+", " ", t).strip()
            if len(t) >= 12:
                norm[t].append(m.id)
        texts = sorted(((t, ids) for t, ids in norm.items() if len(ids) > 1), key=lambda x: (-len(x[1]), x[0]))
        links = sorted(((c, e) for c, e in self.link_index().items() if len(e["refs"]) > 1),
                       key=lambda x: (-len(x[1]["refs"]), x[0]))
        groups: dict[str, list[str]] = defaultdict(list)
        for dup, keep in self.dupes.items():
            groups[keep].append(dup)
        wasted = sum(int(self.size_of(d)) for ds in groups.values() for d in ds)
        lines = ["# 🪞 Similar & repeated\n", "[🏠 README](README.md)\n",
                 f"- **{len(texts)}** texts saved more than once",
                 f"- **{len(links)}** links saved more than once",
                 f"- **{len(groups)}** media files stored more than once "
                 f"({sum(len(v) for v in groups.values())} extra copies, {_nice_size(wasted)})\n",
                 "## Repeated texts\n"]
        for t, ids in texts:
            snippet = "🔒 (sensitive)" if any(self.by_id[i].sensitive for i in ids) else esc(t[:140])
            lines.append(f"- **{len(ids)}×** {snippet} — {self.refs_md(f, ids)}")
        lines.append("\n## Links saved more than once\n")
        for c, e in links:
            lines.append(f"- **{len(e['refs'])}×** [{esc(c[:100])}]({href(e['url'])}) — {self.refs_md(f, e['refs'])}")
        lines.append("\n## Byte-identical media\n")
        lines.append("The library links every message to the kept copy. "
                     "`notebook quarantine --apply` moves the extra copies to `_duplicates/` (restorable).\n")
        lines.append("| kept copy | size | extra copies |")
        lines.append("|---|--:|---|")
        for keep, ds in sorted(groups.items(), key=lambda kv: (-int(self.size_of(kv[0])), kv[0])):
            extra = ", ".join(f"[{esc(d.split('/', 1)[1])}]({self.rel(f, self.where(d))})" for d in sorted(ds))
            lines.append(f"| [{esc(keep.split('/', 1)[1])}]({self.rel(f, f'{_EXPORT}/{keep}')}) | "
                         f"{_nice_size(self.size_of(keep))} | {extra} |")
        self.files[f] = lines

    def where(self, key: str) -> str:
        """Library-relative location of an export file: in _export, or quarantined."""
        if not (self.out / _EXPORT / key).exists() and (self.out / _DUPES / key).exists():
            return f"{_DUPES}/{key}"
        return f"{_EXPORT}/{key}"

    def size_of(self, key: str) -> int:
        for p in (self.out / _EXPORT / key, self.out / _DUPES / key):
            if p.exists():
                return p.stat().st_size
        return 0

    def build_readme(self) -> None:
        msgs = self.msgs
        kinds = Counter(media_kind(m.raw) or "text" for m in msgs)
        sens = Counter(m.sensitive for m in msgs if m.sensitive)
        topics = Counter(m.topic for m in msgs if not m.sensitive)
        idx_links = self.link_index()
        name = self.meta.get("name") or ("Saved Messages" if self.meta.get("type") == "saved_messages" else "Chat")
        lines = [f"# ⭐ {name} — the organized archive\n",
                 f"**{len(msgs)}** messages · {msgs[0].date:%d %b %Y} → {msgs[-1].date:%d %b %Y} · "
                 f"exports: {', '.join(self.meta.get('exports', []))}\n" if msgs else "",
                 "Every message is here, in order, in **Timeline**. The same messages are regrouped by "
                 "theme in **Topics**. Nothing was deleted: the original export sits untouched in "
                 f"`{_EXPORT}/`.\n",
                 "## Start here\n",
                 "| | |", "|---|---|",
                 "| 🕓 Timeline | " + " · ".join(f"[{y}](Timeline/{y}.md)" for y in sorted({m.year for m in msgs})) + " |",
                 f"| 🔗 Links | [{len(idx_links)} unique links](Links/README.md) |",
                 "| 🖼 Media | " + " · ".join(f"[{t} ({kinds.get(k, 0)})](Media/{t}.md)" for k, (t, _) in MEDIA_KINDS.items()) + " |",
                 "| ↪ Sources | [where forwarded things came from](Sources/Forwards.md) |",
                 "| 🪞 Similar | [repeats and duplicates](Similar.md) |",
                 "| 🧹 Cleanup | [before you clear the chat in Telegram](CLEANUP-CHECKLIST.md) |",
                 "| 🔒 Sensitive | " + (" · ".join(f"[{SENSITIVE_KINDS[k][0]} ({n})]({_SENSITIVE}/{k}.md)" for k, n in sorted(sens.items())) or "none") + " |",
                 "\n## Topics\n", "| topic | messages |", "|---|--:|"]
        for slug, title, emoji in TOPICS:
            if topics.get(slug):
                lines.append(f"| {emoji} [{title}]({self.topic_file(slug)}) | {topics[slug]} |")
        lines += ["\n## What's inside\n", "| kind | count |", "|---|--:|"]
        for k, n in kinds.most_common():
            lines.append(f"| {k} | {n} |")
        lines += ["\n## How this is kept\n",
                  "- Rebuild after a new export: `navig telegram-exports notebook build <ChatExport folder> --out <this folder> --apply` — "
                  "the new export is copied into `_export/` and merged by message id.",
                  "- Move a message to another topic: add a row to `_state/topics.overrides.csv` "
                  "(`id,topic,reason`; topic is a slug below, or `sensitive/credentials|finance|leaks`) and rebuild.",
                  "- Messages no rule could place are listed in `_state/unclassified.tsv`.",
                  "- Check nothing is missing: `navig telegram-exports notebook verify <this folder>`.",
                  "\nTopic slugs: " + ", ".join(f"`{s}`" for s in TOPIC_SLUGS),
                  "\n> 🔒 `_sensitive/` holds passwords, bank details and leak material verbatim. Keep this folder offline."]
        self.files["README.md"] = lines


def write_files(out: Path, files: dict[str, list[str]]) -> int:
    """Replace the previous generation (only files we wrote), then write the new one."""
    listing = out / _STATE / _GENERATED_LIST
    if listing.exists():
        for rel in listing.read_text(encoding="utf-8").splitlines():
            p = out / rel
            if rel and p.is_file() and p.suffix == ".md":
                p.unlink()
    for rel, lines in files.items():
        p = out / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8", newline="\n")
    listing.parent.mkdir(parents=True, exist_ok=True)
    listing.write_text("\n".join(sorted(files)) + "\n", encoding="utf-8", newline="\n")
    return len(files)


def write_unclassified(out: Path, msgs: list[Msg]) -> int:
    """Messages worth a second look: nothing placed them, or only a keyword guess did."""
    rows = [m for m in msgs if not m.sensitive and not m.reason.startswith("override")
            and (m.topic in _WEAK or m.reason.startswith(("keyword", "own long note")))]
    p = out / _STATE / "unclassified.tsv"
    with p.open("w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh, delimiter="\t", lineterminator="\n")
        w.writerow(["id", "date", "topic", "reason", "media", "source", "links", "text"])
        for m in rows:
            w.writerow([m.id, f"{m.date:%Y-%m-%d %H:%M}", m.topic, m.reason, media_kind(m.raw),
                        m.raw.get("forwarded_from") or m.raw.get("saved_from") or "",
                        " ".join(canonical_url(u) for u in message_links(m.raw))[:300],
                        re.sub(r"\s+", " ", m.text)[:400]])
    return len(rows)


def render(out: Path) -> tuple[Lib, int]:
    meta, msgs = load_messages(out)
    classify(msgs, load_overrides(out))
    lib = Lib(out, meta, msgs, duplicate_map(out, msgs), _hash_of(out))
    lib.build()
    n = write_files(out, lib.files)
    write_unclassified(out, msgs)
    return lib, n


# --------------------------------------------------------------------------- #
# Verification
# --------------------------------------------------------------------------- #
_ANCHOR_RE = re.compile(r'<a id="m-(\d+)"></a>')
# Only links the library itself wrote: an unescaped tag / "](" — user text has "\<" and "\]"
_TAG_RE = re.compile(r'(?<!\\)<(?:img|video|audio|a)\b[^>]*>')
_ATTR_RE = re.compile(r'(?:src|href|poster)="([^"]+)"')
_MDLINK_RE = re.compile(r'(?<!\\)\]\(([^)\s]+)\)')
_CODE_RE = re.compile(r"^(`{3,}).*?^\1\s*$|(?<![\\`])(`+)(?!`).+?(?<!`)\2(?!`)", re.S | re.M)


def library_links(md_text: str) -> list[str]:
    """Link targets in a generated page, ignoring code blocks and escaped user text."""
    text = _CODE_RE.sub("", md_text)
    out = [a for tag in _TAG_RE.findall(text) for a in _ATTR_RE.findall(tag)]
    return out + _MDLINK_RE.findall(text)


def verify_library(out: Path, deep: bool = False) -> dict:
    from urllib.parse import unquote

    meta, msgs = load_messages(out)
    ids = {m.id for m in msgs}
    res: dict = {"messages": len(msgs), "problems": []}

    def anchors(paths) -> Counter:
        c: Counter = Counter()
        for p in paths:
            c.update(int(x) for x in _ANCHOR_RE.findall(p.read_text(encoding="utf-8")))
        return c

    tl = anchors(sorted((out / "Timeline").glob("*.md")))
    homes = anchors(sorted((out / "Topics").glob("*.md")) + sorted((out / _SENSITIVE).glob("*.md")))
    for name, c in (("timeline", tl), ("topics+sensitive", homes)):
        missing = ids - set(c)
        extra = {i for i, n in c.items() if n > 1}
        stray = set(c) - ids
        res[f"{name}_missing"], res[f"{name}_repeated"] = len(missing), len(extra)
        if missing or extra or stray:
            res["problems"].append(f"{name}: {len(missing)} missing, {len(extra)} repeated, {len(stray)} unknown "
                                   f"(e.g. {sorted(missing or extra or stray)[:5]})")

    # every link target inside the library must exist; every export file must be linked
    linked: set[Path] = set()
    broken: list[str] = []
    for md in out.rglob("*.md"):
        parts = md.relative_to(out).parts
        if parts[0] in (_EXPORT, _DUPES) or any(part.startswith(".") for part in parts):
            continue
        for target in library_links(md.read_text(encoding="utf-8")):
            if re.match(r"^[a-z][a-z0-9+.-]*:", target, re.I) or target.startswith("#"):
                continue
            path = (md.parent / unquote(target.split("#", 1)[0])).resolve()
            if not path.exists():
                broken.append(f"{md.relative_to(out)} → {target}")
            else:
                linked.add(path)
    res["broken_links"] = len(broken)
    if broken:
        res["problems"].append(f"{len(broken)} broken link(s), e.g. {broken[:3]}")

    # integrity: every manifest file is either in _export or quarantined in _duplicates
    moved = {row["moved_from"]: row for row in _read_quarantine(out)}
    lost, unlinked, bad_hash = [], [], []
    hashes = _hash_of(out)
    for key, h in hashes.items():
        exp, rel = key.split("/", 1)
        p = out / _EXPORT / key
        if not p.exists():
            q = out / _DUPES / key
            if key in moved and q.exists():
                p = q
            else:
                lost.append(key)
                continue
        if deep and full_hash(str(p)) != h:
            bad_hash.append(key)
        top = rel.split("/", 1)[0]
        is_media = "/" in rel and top not in _SKIP_EXPORT_DIRS
        if is_media and p.resolve() not in linked:
            unlinked.append(key)
    res.update(files=len(hashes), lost=len(lost), unlinked_media=len(unlinked), bad_hash=len(bad_hash))
    if lost:
        res["problems"].append(f"{len(lost)} export file(s) missing from _export and _duplicates: {lost[:3]}")
    if unlinked:
        res["problems"].append(f"{len(unlinked)} export media file(s) not linked from any page: {unlinked[:3]}")
    if bad_hash:
        res["problems"].append(f"{len(bad_hash)} file(s) no longer match their sha256: {bad_hash[:3]}")
    res["status"] = "PASS" if not res["problems"] else "FAIL"
    res["checked_at"] = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
    (out / _STATE / "coverage.json").write_text(json.dumps(res, ensure_ascii=False, indent=2), encoding="utf-8")
    return res


# --------------------------------------------------------------------------- #
# Quarantine
# --------------------------------------------------------------------------- #
_QLOG = "manifest.tsv"
_QCOLS = ["timestamp", "action", "moved_from", "moved_to", "canonical", "sha256"]


def _read_quarantine(out: Path) -> list[dict]:
    p = out / _DUPES / _QLOG
    if not p.exists():
        return []
    rows = list(csv.DictReader(p.open(encoding="utf-8", newline=""), delimiter="\t"))
    state: dict[str, dict] = {}
    for r in rows:                                   # last action per file wins
        state[r["moved_from"]] = r
    return [r for r in state.values() if r["action"] == "quarantine"]


def _qlog(out: Path, row: dict) -> None:
    p = out / _DUPES / _QLOG
    p.parent.mkdir(parents=True, exist_ok=True)
    new = not p.exists()
    with p.open("a", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=_QCOLS, delimiter="\t", lineterminator="\n")
        if new:
            w.writeheader()
        w.writerow(row)


def quarantine_dupes(out: Path, apply: bool) -> tuple[int, int]:
    _, msgs = load_messages(out)
    dupes = duplicate_map(out, msgs)
    moved = size = 0
    for dup, keep in sorted(dupes.items()):
        src, keep_p = out / _EXPORT / dup, out / _EXPORT / keep
        if not src.exists() or not keep_p.exists():
            continue
        size += src.stat().st_size
        if not apply:
            console.print(f"  WOULD MOVE {dup}  (same bytes as {keep})")
            moved += 1
            continue
        h = full_hash(str(src))
        if h != full_hash(str(keep_p)):          # re-verify at move time
            console.print(f"  [yellow]SKIP (bytes differ now):[/] {dup}")
            continue
        dst = out / _DUPES / dup
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dst))
        _qlog(out, {"timestamp": datetime.now().isoformat(timespec="seconds"), "action": "quarantine",
                    "moved_from": dup, "moved_to": f"{_DUPES}/{dup}", "canonical": keep, "sha256": h})
        moved += 1
    return moved, size


def restore_dupes(out: Path) -> int:
    n = 0
    for r in _read_quarantine(out):
        src, dst = out / r["moved_to"], out / _EXPORT / r["moved_from"]
        if src.exists() and not dst.exists():
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(src), str(dst))
            _qlog(out, r | {"timestamp": datetime.now().isoformat(timespec="seconds"), "action": "restore"})
            n += 1
    return n


# --------------------------------------------------------------------------- #
# CLI: navig telegram-exports notebook …
# --------------------------------------------------------------------------- #
notebook_app = typer.Typer(
    name="notebook",
    help="📓 Render a chat export (e.g. Saved Messages) into an organized Markdown library.",
    no_args_is_help=True,
)


@notebook_app.command("build")
def build_cmd(
    export: Path = typer.Argument(None, help="A ChatExport_* folder to add. Omit to just re-render."),
    out: Path = typer.Option(..., "--out", "-o", help="Library folder (e.g. D:\\_telegram\\Favorites)."),
    apply: bool = typer.Option(False, "--apply", help="Copy + write (default: dry-run summary)."),
):
    """Copy EXPORT into OUT/_export/<date> (sha256-verified) and (re)generate the Markdown."""
    out = out.resolve()
    if export:
        export = export.resolve()
        if not (export / "result.json").exists():
            console.print(f"[red]No result.json in {export}[/] — export with the JSON format ticked.")
            raise typer.Exit(1)
        files = [p for p in export.rglob("*") if p.is_file()]
        gb = sum(p.stat().st_size for p in files) / 1e9
        console.print(f"Export: {export.name} — {len(files)} files, {gb:.1f} GB")
        if not apply:
            data = json.loads((export / "result.json").read_text(encoding="utf-8"))
            msgs = [Msg(raw=r, export=export.name, id=int(r["id"]), text=plain_text(r)) for r in data["messages"]]
            for m in msgs:
                m.date = datetime.fromisoformat(m.raw["date"])
            classify(msgs, load_overrides(out))
            topics = Counter(("🔒 " + m.sensitive) if m.sensitive else m.topic for m in msgs)
            console.print(f"{len(msgs)} messages → " + ", ".join(f"{k} {v}" for k, v in topics.most_common()))
            console.print(f"\n[cyan](dry-run — re-run with --apply to copy into {out / _EXPORT} and render)[/]")
            return
        console.print(f"Copying into {out / _EXPORT} (source is left untouched)…")
        dest, man = copy_export(export, out)
        console.print(f"[green]Copied + verified[/] {len(man)} files → {dest}")
    if not list_exports(out):
        console.print(f"[red]Nothing under {out / _EXPORT}[/] — pass an export folder.")
        raise typer.Exit(1)
    if not apply and not export:
        console.print("[cyan](dry-run — pass --apply to regenerate the Markdown)[/]")
        return
    lib, n = render(out)
    un = sum(1 for m in lib.msgs if not m.sensitive and m.topic in _WEAK)
    console.print(f"[green]Wrote {n} Markdown files[/] for {len(lib.msgs)} messages · "
                  f"{len(lib.dupes)} duplicate media linked to one copy · {un} left in images/misc "
                  f"(see _state/unclassified.tsv)")


@notebook_app.command("verify")
def verify_cmd(
    out: Path = typer.Argument(..., help="Library folder."),
    deep: bool = typer.Option(False, "--deep", help="Also re-hash every file against its sha256."),
):
    """Coverage gate: prove every message, link and file made it into the library.

    Every message once in Timeline and once in Topics/_sensitive, no broken links,
    every export file present (or quarantined) and linked; --deep re-hashes them all."""
    res = verify_library(out.resolve(), deep=deep)
    for k in ("messages", "files", "timeline_missing", "timeline_repeated", "topics+sensitive_missing",
              "topics+sensitive_repeated", "broken_links", "lost", "unlinked_media", "bad_hash"):
        console.print(f"  {k:28} {res.get(k)}")
    for p in res["problems"]:
        console.print(f"  [red]✗[/] {p}")
    ok = res["status"] == "PASS"
    console.print(f"[{'green' if ok else 'red'}]{res['status']}[/]")
    raise typer.Exit(0 if ok else 1)


@notebook_app.command("quarantine")
def quarantine_cmd(
    out: Path = typer.Argument(..., help="Library folder."),
    apply: bool = typer.Option(False, "--apply", help="Move the extra copies (default: dry-run)."),
):
    """Move extra byte-identical media copies from _export/ to _duplicates/ (logged, restorable)."""
    n, size = quarantine_dupes(out.resolve(), apply)
    if apply:
        render(out.resolve())                   # links follow the files to _duplicates/
    verb = "Moved" if apply else "Would move"
    console.print(f"{verb} {n} duplicate file(s), {size / 1e9:.2f} GB"
                  + ("" if apply else " — re-run with --apply"))


@notebook_app.command("restore")
def restore_cmd(out: Path = typer.Argument(..., help="Library folder.")):
    """Put every quarantined duplicate back where the export had it."""
    n = restore_dupes(out.resolve())
    render(out.resolve())
    console.print(f"Restored {n} file(s); pages re-rendered.")
