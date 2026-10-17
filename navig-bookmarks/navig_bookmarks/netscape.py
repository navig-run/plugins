"""Netscape bookmark HTML — the format every browser imports and exports.

Adapted from linkding's ``services/parser.py`` and ``services/exporter.py`` (MIT, see
THIRD_PARTY_NOTICES.md). linkding flattens folders away; this keeps them. The structure is::

    <DL><p>
      <DT><H3>Folder</H3>
      <DL><p>
        <DT><A HREF="…" ADD_DATE="…" TAGS="a,b">Title</A>
        <DD>notes
      </DL><p>
    </DL><p>

so the parser keeps a stack: an ``<H3>`` names the folder, the ``<DL>`` that follows opens
it, and its ``</DL>`` closes it. A ``<DL>`` with no ``<H3>`` before it (the document root)
pushes an unnamed level. Browsers are sloppy here (``<p>`` never closed, ``<DT>`` never
closed), so nothing relies on end tags except ``</A>``, ``</H3>`` and ``</DL>``.
"""

from __future__ import annotations

import html
from html.parser import HTMLParser
from typing import Iterable

from navig_bookmarks.models import ParsedBookmark, join_folder, split_folder


class _Parser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.bookmarks: list[ParsedBookmark] = []
        self._stack: list[str | None] = []
        self._pending_folder: str | None = None
        self._in_h3 = False
        self._h3_text: list[str] = []
        self._in_a = False
        self._a_text: list[str] = []
        self._in_dd = False
        self._dd_text: list[str] = []
        self._current: ParsedBookmark | None = None

    # -- helpers -----------------------------------------------------------------
    def _folder(self) -> str:
        return join_folder([name for name in self._stack if name])

    def _flush(self) -> None:
        if self._current is not None:
            if self._dd_text:
                self._current.notes = " ".join("".join(self._dd_text).split())
            self.bookmarks.append(self._current)
        self._current = None
        self._in_dd = False
        self._dd_text = []

    # -- tags --------------------------------------------------------------------
    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        a = {k.lower(): (v or "") for k, v in attrs}
        if tag == "dt":
            self._flush()
        elif tag == "dl":
            self._flush()
            self._stack.append(self._pending_folder)
            self._pending_folder = None
        elif tag == "h3":
            self._flush()
            self._in_h3 = True
            self._h3_text = []
        elif tag == "a":
            self._flush()
            href = a.get("href", "").strip()
            if not href:
                return
            added = None
            raw_added = a.get("add_date", "").strip()
            if raw_added.isdigit():
                added = int(raw_added)
                if added > 10**11:  # some exporters write microseconds or milliseconds
                    added //= 1000 if added < 10**14 else 1_000_000
            tags = [t.strip() for t in a.get("tags", "").split(",") if t.strip()]
            self._current = ParsedBookmark(url=href, folder=self._folder(), tags=tags, added=added)
            self._in_a = True
            self._a_text = []
        elif tag == "dd":
            # A <DD> describes the bookmark just closed; after an <H3> it describes the
            # folder, which has nowhere to go and is dropped.
            if self._current is not None:
                self._in_dd = True
                self._dd_text = []

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag == "h3" and self._in_h3:
            self._in_h3 = False
            self._pending_folder = " ".join("".join(self._h3_text).split()) or "Untitled"
        elif tag == "a" and self._in_a:
            self._in_a = False
            if self._current is not None:
                self._current.title = " ".join("".join(self._a_text).split())
        elif tag == "dl":
            self._flush()
            if self._stack:
                self._stack.pop()
            self._pending_folder = None

    def handle_data(self, data: str) -> None:
        if self._in_h3:
            self._h3_text.append(data)
        elif self._in_a:
            self._a_text.append(data)
        elif self._in_dd:
            self._dd_text.append(data)

    def close(self) -> None:
        super().close()
        self._flush()


def parse(text: str) -> list[ParsedBookmark]:
    """Every bookmark in a Netscape HTML export, with its folder path."""
    parser = _Parser()
    parser.feed(text)
    parser.close()
    return parser.bookmarks


def looks_like_netscape(text: str) -> bool:
    head = text.lstrip()[:400].upper()
    return "NETSCAPE-BOOKMARK-FILE" in head or ("<DL" in head and "<DT" in text[:4000].upper())


# ── export ─────────────────────────────────────────────────────────────────────


class _Node:
    __slots__ = ("children", "items", "name")

    def __init__(self, name: str) -> None:
        self.name = name
        self.children: dict[str, _Node] = {}
        self.items: list[dict] = []


def export_html(bookmarks: Iterable[dict], *, title: str = "Bookmarks") -> str:
    """Netscape HTML for *bookmarks*, folders written as nested ``<H3>`` lists.

    Each bookmark is a dict with ``url``, ``title``, ``notes``, ``folder``, ``tags``
    (a list of names) and an optional ``added`` (unix seconds). Folder order and bookmark
    order follow first appearance, so an import followed by an export keeps the layout.
    """
    root = _Node("")
    for bm in bookmarks:
        node = root
        for part in split_folder(bm.get("folder") or ""):
            node = node.children.setdefault(part, _Node(part))
        node.items.append(bm)

    out = [
        "<!DOCTYPE NETSCAPE-Bookmark-file-1>",
        "<!-- This is an automatically generated file.",
        "     It will be read and overwritten.",
        "     DO NOT EDIT! -->",
        '<META HTTP-EQUIV="Content-Type" CONTENT="text/html; charset=UTF-8">',
        f"<TITLE>{html.escape(title)}</TITLE>",
        f"<H1>{html.escape(title)}</H1>",
        "<DL><p>",
    ]
    _write_node(root, out, 1)
    out.append("</DL><p>")
    return "\n".join(out) + "\n"


def _write_node(node: _Node, out: list[str], depth: int) -> None:
    pad = "    " * depth
    for child in node.children.values():
        out.append(f"{pad}<DT><H3>{html.escape(child.name, quote=False)}</H3>")
        out.append(f"{pad}<DL><p>")
        _write_node(child, out, depth + 1)
        out.append(f"{pad}</DL><p>")
    for bm in node.items:
        attrs = [f'HREF="{html.escape(str(bm.get("url") or ""), quote=True)}"']
        added = bm.get("added")
        if isinstance(added, int) and added > 0:
            attrs.append(f'ADD_DATE="{added}"')
        tags = [str(t) for t in (bm.get("tags") or []) if str(t).strip()]
        if tags:
            attrs.append(f'TAGS="{html.escape(",".join(tags), quote=True)}"')
        label = html.escape(str(bm.get("title") or bm.get("url") or ""), quote=False)
        out.append(f"{pad}<DT><A {' '.join(attrs)}>{label}</A>")
        notes = str(bm.get("notes") or "").strip()
        if notes:
            out.append(f"{pad}<DD>{html.escape(' '.join(notes.split()), quote=False)}")
