"""Tests for navig_explore.gallery.

These cover the promises the gallery makes: it is non-destructive, it never lets one
bad image kill the build, its thumbnail cache is reused rather than rebuilt, and
"prune" moves stale entries aside instead of deleting them.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from navig_explore import gallery as gal

pytest.importorskip("PIL", reason="thumbnail behaviour needs Pillow")
from PIL import Image  # noqa: E402


def _img(path, size=(64, 48), color=(200, 30, 30), mode="RGB"):
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new(mode, size, color if mode == "RGB" else 0).save(path)
    return path


@pytest.fixture
def tree(tmp_path):
    _img(tmp_path / "clients" / "a.png")
    _img(tmp_path / "clients" / "b.jpg")
    _img(tmp_path / "design" / "c.png")
    _img(tmp_path / "loose.png")               # root-level file -> "Root" section
    (tmp_path / "notes.txt").write_text("not an image", encoding="utf-8")
    return tmp_path


def test_builds_page_and_groups_by_top_level_folder(tree):
    stats = gal.build_gallery(tree)
    out = tree / "gallery.html"
    assert out.is_file()
    assert stats["images"] == 4                      # 3 in folders + 1 loose
    html = out.read_text(encoding="utf-8")
    for sid in ('id="clients"', 'id="design"', 'id="root"'):
        assert sid in html
    assert "notes.txt" not in html                   # non-image ignored


def test_is_non_destructive(tree):
    before = {p.relative_to(tree) for p in tree.rglob("*") if p.is_file()}
    gal.build_gallery(tree)
    after = {p.relative_to(tree) for p in tree.rglob("*") if p.is_file()}
    assert before <= after                           # nothing removed or renamed
    added = {p.parts[0] for p in after - before}
    assert added <= {"gallery.html", ".mediaexplorer"}


def test_thumbnail_cache_is_reused_on_rebuild(tree):
    first = gal.build_gallery(tree)
    assert first["thumb_made"] == 4 and first["thumb_cached"] == 0
    second = gal.build_gallery(tree)
    assert second["thumb_made"] == 0 and second["thumb_cached"] == 4


def test_undecodable_file_is_counted_but_left_off_the_page(tree):
    """A file with an image extension that isn't an image (a RAR named .JPG, a carver
    fragment) can't render in a browser either — a tile for it is always broken."""
    (tree / "clients" / "broken.png").write_bytes(b"not a real png")
    (tree / "clients" / "archive.jpg").write_bytes(b"Rar!\x1a\x07\x00" + b"\x00" * 64)

    stats = gal.build_gallery(tree)
    assert stats["thumb_fail"] == 2       # counted, so the operator learns about them...
    assert stats["images"] == 4           # ...but not listed as images
    html = (tree / "gallery.html").read_text(encoding="utf-8")
    assert "broken.png" not in html and "archive.jpg" not in html


def test_a_valid_image_survives_a_cache_write_failure(tree, monkeypatch):
    """Failing to CACHE a good image must fall back to the original, not drop it —
    only an undecodable file is dropped."""
    real = gal.Image.Image.save

    def boom(self, fp, *a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(gal.Image.Image, "save", boom)
    stats = gal.build_gallery(tree)
    monkeypatch.setattr(gal.Image.Image, "save", real)
    assert stats["images"] == 4           # every real image still on the page
    assert stats["thumb_fail"] == 0       # not a decode failure
    assert stats["thumb_orig"] >= 4       # served full-size instead


def test_exotic_mode_image_still_thumbnails(tree):
    """16-bit greyscale (I;16) can't be saved as WEBP directly — mode must be
    normalised BEFORE resizing, or every such file silently loses its thumbnail."""
    _img(tree / "design" / "qr.png", mode="I;16")
    stats = gal.build_gallery(tree)
    assert stats["thumb_fail"] == 0


def test_prune_moves_stale_thumbs_aside_never_deletes(tree):
    gal.build_gallery(tree)
    cache = tree / ".mediaexplorer" / "thumbs"
    orphan = cache / "deadbeef.webp"
    orphan.write_bytes((next(cache.glob("*.webp"))).read_bytes())
    stats = gal.build_gallery(tree, prune=True)
    assert stats["pruned"] == 1
    assert not orphan.exists()
    assert (cache / "_stale" / "deadbeef.webp").is_file()


def test_the_thumbnail_cache_has_exactly_ONE_root(tree):
    """Every cached byte must live under .mediaexplorer/thumbs — including pruned ones.

    Regression: the pruned copy used to sit beside the cache at
    `.mediaexplorer/stale-thumbs`, so the cache had two roots. A cleanup that matched the
    documented `.mediaexplorer/thumbs` path on a real 121k-photo library found 97,748
    files and silently missed 18,852 more. One root makes "remove the thumbnail cache" a
    complete instruction.
    """
    gal.build_gallery(tree)
    cache = tree / ".mediaexplorer" / "thumbs"
    orphan = cache / "deadbeef.webp"
    orphan.write_bytes((next(cache.glob("*.webp"))).read_bytes())
    gal.build_gallery(tree, prune=True)

    side = tree / ".mediaexplorer"
    everywhere = {p.resolve() for p in side.rglob("*.webp")}
    under_cache = {p.resolve() for p in cache.rglob("*.webp")}
    assert everywhere == under_cache, (
        "cached thumbnails found outside .mediaexplorer/thumbs: "
        f"{sorted(str(p) for p in everywhere - under_cache)}"
    )
    # and deleting that one directory really does remove the whole cache
    import shutil
    shutil.rmtree(cache)
    assert not list(side.rglob("*.webp"))


def test_pruning_twice_does_not_re_prune_already_stale_entries(tree):
    """The stale dir now nests inside the cache; iterating must not pick it back up."""
    gal.build_gallery(tree)
    cache = tree / ".mediaexplorer" / "thumbs"
    (cache / "deadbeef.webp").write_bytes((next(cache.glob("*.webp"))).read_bytes())
    first = gal.build_gallery(tree, prune=True)["pruned"]
    second = gal.build_gallery(tree, prune=True)["pruned"]
    assert first == 1
    assert second == 0, "an already-pruned entry was moved again"
    assert (cache / "_stale" / "deadbeef.webp").is_file()


def test_macos_appledouble_stubs_are_not_media(tree):
    """`._name.jpg` sidecars from Mac-made zips carry an image extension but hold no
    image data — listing them fills the grid with tiles that can never render."""
    (tree / "clients" / "._a.png").write_bytes(b"\x00\x05\x16\x07 AppleDouble")
    (tree / "._loose.png").write_bytes(b"\x00\x05\x16\x07 AppleDouble")
    macosx = tree / "__MACOSX" / "clients"
    macosx.mkdir(parents=True)
    (macosx / "._b.png").write_bytes(b"\x00\x05\x16\x07 AppleDouble")

    stats = gal.build_gallery(tree)
    assert stats["images"] == 4          # unchanged: the four real images
    assert stats["thumb_fail"] == 0      # and nothing failed trying to render junk
    html = (tree / "gallery.html").read_text(encoding="utf-8")
    assert "._a.png" not in html and "._loose.png" not in html
    assert "__MACOSX" not in html


# ── --recursive: one gallery per subfolder, with a size-aware split ──────────
def test_recursive_builds_one_gallery_per_subfolder_plus_an_index(tree):
    res = gal.build_gallery_tree(tree)
    assert res["galleries"] == 2                       # clients + design
    assert res["images"] == 3                          # the loose root file isn't a subfolder
    for sub in ("clients", "design"):
        assert (tree / sub / "gallery.html").is_file()
    index = Path(res["index"])
    assert index.name == "galleries-index.html"
    html = index.read_text(encoding="utf-8")
    assert 'href="clients/gallery.html"' in html and 'href="design/gallery.html"' in html


def test_recursive_splits_a_folder_that_is_too_big_into_its_children(tmp_path):
    """The reason this exists: a 97k-image folder must not become one document."""
    for year in ("2009", "2010"):
        for i in range(3):
            _img(tmp_path / "by-date" / year / f"{i}.png")
    _img(tmp_path / "small" / "a.png")

    res = gal.build_gallery_tree(tmp_path, split_over=5)   # by-date has 6 > 5
    labels = [lbl for lbl, _ in res["built"]]
    assert "by-date/2009" in labels and "by-date/2010" in labels
    assert "by-date" not in labels                          # expanded, not galleryized
    assert "small" in labels                                # under the threshold: left whole
    assert not (tmp_path / "by-date" / "gallery.html").exists()


def test_recursive_leaves_a_big_folder_whole_when_it_has_no_subfolders(tmp_path):
    """Nothing to split into — one big page beats no page at all."""
    for i in range(6):
        _img(tmp_path / "flat" / f"{i}.png")
    res = gal.build_gallery_tree(tmp_path, split_over=2)
    assert [lbl for lbl, _ in res["built"]] == ["flat"]


def test_recursive_skips_empty_and_hidden_folders(tree):
    (tree / "empty").mkdir()
    (tree / ".hidden").mkdir()
    _img(tree / ".hidden" / "x.png")
    res = gal.build_gallery_tree(tree)
    labels = [lbl for lbl, _ in res["built"]]
    assert "empty" not in labels and ".hidden" not in labels


def test_recursive_reports_progress_per_gallery(tree):
    seen = []
    gal.build_gallery_tree(tree, on_progress=lambda lbl, s: seen.append((lbl, s["images"])))
    assert sorted(seen) == [("clients", 2), ("design", 1)]


def test_zero_byte_media_is_skipped(tree):
    """An interrupted download leaves 0-byte files with real extensions. They can never
    render, so listing them fills the grid with broken tiles — one asset folder was
    5,779 of them (99.9% of that folder)."""
    (tree / "clients" / "empty.png").touch()
    (tree / "empty-loose.jpg").touch()
    (tree / "clients" / "empty.mp4").touch()

    stats = gal.build_gallery(tree)
    assert stats["images"] == 4          # the four real images, unchanged
    assert stats["videos"] == 0          # the empty video is not listed either
    assert stats["thumb_fail"] == 0
    html = (tree / "gallery.html").read_text(encoding="utf-8")
    assert "empty.png" not in html and "empty-loose.jpg" not in html


def test_page_carries_what_a_staleness_warning_needs(tree):
    """A gallery stores paths relative to itself, so a reorg turns every tile into a
    broken image. The page can't stat the filesystem over file://, but it can notice its
    own images failing — and needs the build date + root to tell the user how to fix it."""
    gal.build_gallery(tree)
    html = (tree / "gallery.html").read_text(encoding="utf-8")
    assert 'data-generated="' in html
    assert f'data-root="{tree}"'.replace("\\", "\\") in html or str(tree) in html
    assert 'id="stale"' in html
    # the handler must use capture (error does not bubble) and ignore the lightbox img
    assert "'error'" in html and "true);   // capture" in html
    assert "lb-img" in html
    assert "--recursive" in html          # tells the user the exact fix


def test_rejects_non_directory(tmp_path):
    f = tmp_path / "x.txt"
    f.write_text("hi", encoding="utf-8")
    with pytest.raises(NotADirectoryError):
        gal.build_gallery(f)


# ── caller hooks: one engine, callers inject their own structure ─────────────
def test_explicit_sections_override_discovery(tree):
    stats = gal.build_gallery(
        tree, sections=[("Only Design", "design")], include_root=False)
    html = (tree / "gallery.html").read_text(encoding="utf-8")
    assert stats["images"] == 1
    assert 'id="only-design"' in html
    assert 'id="clients"' not in html and 'id="root"' not in html


def test_extra_sections_render_first_and_share_the_thumb_cache(tree):
    seen = {}

    def curated(ctx):
        src = tree / "clients" / "a.png"
        seen["thumb"] = ctx.thumb_href(src)     # same cache as the scanned sections
        card = (f'<div class="pcard"><a class="lb" href="{ctx.rel_href(src)}" '
                f'data-cap="Hero"><img src="{seen["thumb"]}" alt=""></a>'
                f'<div class="pbody"><b>Hero</b></div></div>')
        return [gal.ExtraSection(id="curated", label="Curated",
                                 html=f'<div class="pgrid">{card}</div>', count=1)]

    stats = gal.build_gallery(tree, extra_sections=curated)
    html = (tree / "gallery.html").read_text(encoding="utf-8")
    assert stats["extra_sections"] == 1
    assert html.index('id="curated"') < html.index('id="clients"')   # pinned to the top
    assert ".mediaexplorer/thumbs" in seen["thumb"].replace("\\", "/")
    # a curated card must be reachable by the lightbox + filter
    assert 'class="pcard"' in html and 'class="lb"' in html


def test_extra_sections_accepts_a_plain_sequence(tree):
    stats = gal.build_gallery(
        tree, extra_sections=[gal.ExtraSection("x", "X", "<div class='grid'></div>", 3)])
    assert stats["extra_sections"] == 1
    assert 'id="x"' in (tree / "gallery.html").read_text(encoding="utf-8")


def test_thumbs_and_stale_dirs_can_be_relocated(tree, tmp_path):
    cache, stale = tmp_path / "cache", tmp_path / "stale"
    gal.build_gallery(tree, thumbs_dir=cache, stale_dir=stale)
    assert any(cache.glob("*.webp"))
    assert not (tree / ".mediaexplorer" / "thumbs").exists()
    (cache / "orphan.webp").write_bytes(next(cache.glob("*.webp")).read_bytes())
    stats = gal.build_gallery(tree, thumbs_dir=cache, stale_dir=stale, prune=True)
    assert stats["pruned"] == 1 and (stale / "orphan.webp").is_file()


# ── caps must never be silent ────────────────────────────────────────────────
def test_max_per_section_caps_and_says_so(tree):
    for i in range(5):
        _img(tree / "clients" / f"extra{i}.png")
    stats = gal.build_gallery(tree, max_per_section=2)
    assert stats["capped"]["clients"] == (2, 7)
    assert stats["dropped"] == 5
    assert "capped at 2" in (tree / "gallery.html").read_text(encoding="utf-8")


def test_no_cap_reports_nothing_dropped(tree):
    stats = gal.build_gallery(tree)
    assert stats["capped"] == {} and stats["dropped"] == 0


# ── serve: the served root must contain BOTH the page and the media ──────────
def test_serve_root_is_the_common_ancestor(tmp_path):
    root = tmp_path / "lib"
    (root / "sub").mkdir(parents=True)
    out = root / "sub" / "gallery.html"
    out.write_text("x", encoding="utf-8")
    # page sits below the library -> serve the library, so ../images/x.jpg resolves
    assert gal._serve_root(out, root) == root.resolve()


def test_serve_falls_back_when_the_preferred_port_is_unusable(tree, monkeypatch):
    """Windows/Hyper-V reserve whole port ranges, so a fixed default cannot be trusted:
    with no explicit --port we must retry rather than fail."""
    gal.build_gallery(tree)
    tried: list[int] = []

    class FakeServer:
        server_address = ("127.0.0.1", 54321)

        def __init__(self, addr, _handler):
            tried.append(addr[1])
            if addr[1] != 0:              # pretend every named port is reserved
                raise OSError(10013, "forbidden by its access permissions")

        def serve_forever(self):
            raise KeyboardInterrupt      # stop immediately

        def server_close(self):
            pass

    # patch where it is *used* — navig.http_bind imported the name at module load,
    # so patching http.server would bind a real socket and block in serve_forever()
    monkeypatch.setattr("navig.http_bind.ThreadingHTTPServer", FakeServer)
    gal.serve_gallery(tree / "gallery.html", tree, open_browser=False)
    assert tried == [gal.DEFAULT_PORT, 0]   # preferred first, then OS-assigned


def test_serve_reports_clearly_when_an_explicit_port_is_taken(tree, monkeypatch):
    gal.build_gallery(tree)

    class Boom:
        def __init__(self, *_a, **_k):
            raise OSError(10048, "address already in use")

    monkeypatch.setattr("navig.http_bind.ThreadingHTTPServer", Boom)  # patch at the use site
    with pytest.raises(gal.GalleryServeError, match="9999"):
        gal.serve_gallery(tree / "gallery.html", tree, port=9999, open_browser=False)


def test_serve_root_falls_back_when_page_is_elsewhere(tmp_path):
    root = tmp_path / "lib"
    root.mkdir()
    out = tmp_path / "elsewhere" / "gallery.html"
    out.parent.mkdir()
    out.write_text("x", encoding="utf-8")
    assert gal._serve_root(out, root) == tmp_path.resolve()
