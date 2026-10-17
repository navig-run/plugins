"""Netscape HTML: folders survive parse -> export -> parse."""

from __future__ import annotations

from navig_bookmarks.models import join_folder, split_folder
from navig_bookmarks.netscape import export_html, looks_like_netscape, parse


def _by_url(items):
    return {b.url: b for b in items}


def test_parse_keeps_the_folder_stack(netscape_fixture: str) -> None:
    items = _by_url(parse(netscape_fixture))
    assert len(items) == 5
    asyncio = items["https://docs.python.org/3/library/asyncio.html"]
    assert asyncio.folder == "Bookmarks bar"
    assert asyncio.title == "asyncio — Asynchronous I/O"  # entity decoded
    assert asyncio.tags == ["python", "async"]
    assert asyncio.notes == "Event loop reference"
    assert asyncio.added == 1700000100
    assert items["https://github.com/encode/httpx"].folder == "Bookmarks bar/Dev"
    ab = items["https://example.com/ab?utm_source=news&b=2&a=1"]
    assert split_folder(ab.folder) == ["Bookmarks bar", "Dev", "A/B testing"]
    assert ab.title == "AB guide & tips"
    # A folder's own <DD> is not attached to the next bookmark.
    bread = items["https://www.example.org/bread/"]
    assert bread.folder == "Recipes" and bread.notes == ""
    top = items["https://example.net/top-level"]
    assert top.folder == "" and top.added == 1700000300  # milliseconds normalised


def test_round_trip_is_stable(netscape_fixture: str) -> None:
    first = parse(netscape_fixture)
    html = export_html([
        {"url": b.url, "title": b.title, "notes": b.notes, "folder": b.folder, "tags": b.tags, "added": b.added}
        for b in first
    ])
    second = parse(html)
    key = lambda b: (b.url, b.title, b.folder, tuple(b.tags), b.notes, b.added)  # noqa: E731
    # Subfolders are written before a folder's own links, so compare as sets of rows.
    assert sorted(map(key, second), key=repr) == sorted(map(key, first), key=repr)
    # And exporting the re-parsed data gives byte-identical HTML.
    again = export_html([
        {"url": b.url, "title": b.title, "notes": b.notes, "folder": b.folder, "tags": b.tags, "added": b.added}
        for b in second
    ])
    assert again == html


def test_export_writes_nested_h3_folders() -> None:
    html = export_html([
        {"url": "https://a.example", "title": "A", "folder": "Dev/Python", "tags": []},
        {"url": "https://b.example", "title": "B <b>", "folder": "", "tags": ["x"]},
    ])
    assert looks_like_netscape(html)
    assert html.index("<H3>Dev</H3>") < html.index("<H3>Python</H3>") < html.index('HREF="https://a.example"')
    assert "B &lt;b&gt;" in html and 'TAGS="x"' in html


def test_folder_names_with_slashes_round_trip() -> None:
    path = join_folder(["Work", "A/B tests", r"back\slash"])
    assert split_folder(path) == ["Work", "A/B tests", r"back\slash"]


def test_malformed_html_does_not_raise() -> None:
    items = parse("<DL><DT><A HREF='https://x.example'>unterminated <DT><A>no href</A><DL>")
    assert [b.url for b in items] == ["https://x.example"]
