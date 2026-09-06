"""Static, offline, shareable media gallery for ANY folder.

Walks a tree, groups images by top-level subfolder, builds a cached webp thumbnail for
each, and writes a single self-contained ``gallery.html`` you can open (or hand to
someone) with no server: live name filter, per-section lightbox, lazy loading.

Distinct from ``explorer.py`` (a live local web UI for organizing) and ``report.py`` (a
markdown recon report): this is the *artifact* you keep or share.

Non-destructive, like the rest of the plugin — it only ever writes ``gallery.html`` and a
``.mediaexplorer/thumbs`` cache. ``prune=True`` moves unreferenced cache entries to
``.mediaexplorer/thumbs/_stale`` rather than deleting them.

That pruned copy is deliberately kept INSIDE the thumbs directory. It used to live beside
it, at ``.mediaexplorer/stale-thumbs``, which meant the cache had two roots — and anything
inspecting the tree had to know both or silently undercount. It did: a cleanup that
matched ``.mediaexplorer/thumbs`` on a 121k-photo library reported 97,748 cached files and
missed 18,852 more, because a second, equally-cached directory sat somewhere else. One
root means "delete ``.mediaexplorer/thumbs``" is a complete and obvious statement, while
prune still never deletes anything.
"""
from __future__ import annotations

import datetime
import hashlib
import html
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Sequence
from urllib.parse import quote

IMG_EXT = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".svg"}
VID_EXT = {".mp4", ".mov", ".avi", ".webm", ".mkv", ".m4v"}
# gif (animated) and svg (vector) are linked as-is; the rest can be down-sampled
THUMBABLE = {".jpg", ".jpeg", ".png", ".webp", ".bmp"}
SKIP_DIRS = {
    ".git", ".svn", ".hg", "node_modules", "__pycache__", ".mediaexplorer",
    ".thumbs", "$recycle.bin", "system volume information", ".trash", "_trash",
    "__macosx",   # AppleDouble sidecars from Mac-made zips — never real media
    "_organized", # hard-linked views of this same library; including them would
                  # render every photo twice (see navig_explore.vision.arrange)
}

try:  # Pillow is optional — without it we fall back to full-size images
    from PIL import Image, ImageFile, ImageOps

    ImageFile.LOAD_TRUNCATED_IMAGES = True   # salvage thumbnails from truncated jpgs
    Image.MAX_IMAGE_PIXELS = 300_000_000     # allow big art; still guards true bombs
    HAVE_PIL = True
except Exception:  # noqa: BLE001 - pillow missing or broken install
    HAVE_PIL = False


class GalleryServeError(RuntimeError):
    """The gallery HTTP server could not start (port busy, reserved, or denied)."""


@dataclass(frozen=True)
class ExtraSection:
    """A caller-supplied section, rendered above the scanned folders.

    ``html`` is the section body. Emit ``<a class="c">`` cells inside ``<div class="grid">``
    for the plain thumbnail grid, or ``<div class="pcard">`` inside ``<div class="pgrid">``
    for rich cards (title + meta + description) — both are styled and both are picked up
    by the filter and the lightbox. For a card, put the clickable image in an
    ``<a class="lb" href="<full image>" data-cap="<caption>">``.
    """

    id: str
    label: str
    html: str
    count: int
    badge: str | None = None   # h2 badge text; defaults to str(count)


class GalleryContext:
    """Handed to an ``extra_sections`` callback so it can reuse the engine's plumbing.

    ``thumb_href`` shares the very same thumbnail cache as the scanned sections, so a
    caller-supplied section costs nothing extra for images already on the page.
    """

    def __init__(self, root: Path, out_dir: Path, thumbs: "_Thumbs") -> None:
        self.root = root
        self.out_dir = out_dir
        self._thumbs = thumbs

    def thumb_href(self, path: Path | str) -> str | None:
        """Thumbnail href, or ``None`` if the file cannot be decoded as an image —
        render a placeholder rather than an ``<img>`` that is guaranteed to break."""
        return self._thumbs.href(Path(path))

    def rel_href(self, path: Path | str) -> str:
        return _rel(Path(path), self.out_dir)


def _human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def _slug(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return s or "section"


def _skip(name: str) -> bool:
    return name.lower() in SKIP_DIRS or name.startswith(".")


class _Thumbs:
    """Path-keyed webp thumbnail cache, invalidated when the source is newer."""

    def __init__(self, cache_dir: Path, max_px: int, out_dir: Path) -> None:
        self.dir = cache_dir
        self.max_px = max_px
        self.out_dir = out_dir
        self.used: set[str] = set()
        self.stats = {"made": 0, "cached": 0, "orig": 0, "fail": 0}

    def href(self, path: Path) -> str | None:
        """Thumbnail href, or ``None`` when the file cannot be decoded as an image.

        ``None`` means "do not put this on the page": the browser cannot render it
        either, so a tile would just be a broken image. Falling back to the original is
        right only when the file IS a valid image we merely failed to cache.
        """
        if not HAVE_PIL or path.suffix.lower() not in THUMBABLE:
            self.stats["orig"] += 1
            return _rel(path, self.out_dir)
        key = hashlib.sha1(str(path.resolve()).encode("utf-8", "surrogatepass")).hexdigest()
        name = f"{key}.webp"
        tpath = self.dir / name
        try:
            if tpath.exists() and tpath.stat().st_mtime >= path.stat().st_mtime:
                self.stats["cached"] += 1
                self.used.add(name)
                return _rel(tpath, self.out_dir)
        except OSError:
            pass
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
            with Image.open(path) as im:
                im = ImageOps.exif_transpose(im)
                # WEBP only takes RGB/RGBA — normalise the mode BEFORE resizing,
                # otherwise 16-bit grey (I;16), CMYK and paletted images raise.
                if im.mode in ("RGBA", "LA") or (im.mode == "P" and "transparency" in im.info):
                    im = im.convert("RGBA")
                elif im.mode != "RGB":
                    im = im.convert("RGB")
                im.thumbnail((self.max_px, self.max_px))
                try:
                    im.save(tpath, "WEBP", quality=72, method=4)
                except Exception:  # noqa: BLE001 - decoded fine, just couldn't cache it
                    self.stats["orig"] += 1
                    return _rel(path, self.out_dir)
        except Exception:  # noqa: BLE001 - cannot decode: not a displayable image
            self.stats["fail"] += 1
            return None
        self.stats["made"] += 1
        self.used.add(name)
        return _rel(tpath, self.out_dir)

    def prune(self, stale_dir: Path) -> int:
        """Move cache entries not referenced this run out of the way (never delete).

        ``stale_dir`` normally sits inside this cache directory, so the whole cache is
        one subtree. Only files are considered, which is what lets the destination nest
        here safely: the ``_stale`` directory itself has no ``.webp`` suffix, and entries
        already moved into it are no longer at this level to be re-visited.
        """
        if not self.dir.is_dir():
            return 0
        moved = 0
        for entry in self.dir.iterdir():
            if entry.is_file() and entry.suffix == ".webp" and entry.name not in self.used:
                stale_dir.mkdir(parents=True, exist_ok=True)
                os.replace(entry, stale_dir / entry.name)
                moved += 1
        return moved


def _rel(path: Path, out_dir: Path) -> str:
    rel = os.path.relpath(path, out_dir).replace("\\", "/")
    return quote(rel, safe="/()")


def _is_media(name: str, exts: set[str]) -> bool:
    """A real media file — not a macOS AppleDouble stub.

    Unzipping a Mac-made archive leaves ``._picture.jpg`` next to ``picture.jpg``: a
    resource-fork sidecar carrying the image's extension but no image data. Listing them
    doubles the apparent file count and fills the grid with tiles that can never render
    (1,066 of them in one real asset library).

    The other never-renderable case — a 0-byte file, typically left by an interrupted
    download — is filtered in :func:`_walk`, where the size is already known.
    """
    if name.startswith("._"):
        return False
    return os.path.splitext(name)[1].lower() in exts


def _walk(base: Path, exts: set[str]) -> list[tuple[Path, int]]:
    found: list[tuple[Path, int]] = []
    for dirpath, dirnames, filenames in os.walk(base):
        dirnames[:] = [d for d in dirnames if not _skip(d)]
        for fn in filenames:
            if _is_media(fn, exts):
                fp = Path(dirpath) / fn
                try:
                    size = fp.stat().st_size
                except OSError:
                    continue        # vanished or unreadable — not something to show
                if size == 0:
                    continue        # an empty file can never render (see _is_media)
                found.append((fp, size))
    found.sort(key=lambda t: str(t[0]).lower())
    return found


CSS = """
:root{color-scheme:dark}
*{box-sizing:border-box}
body{margin:0;background:#0d0f12;color:#e6e8ec;font:14px/1.4 system-ui,Segoe UI,Roboto,sans-serif}
header{position:sticky;top:0;z-index:5;background:#0d0f12ee;backdrop-filter:blur(6px);
 border-bottom:1px solid #222;padding:14px 18px}
header h1{margin:0 0 8px;font-size:18px;letter-spacing:.3px}
header .meta{color:#8a919b;font-size:12px;margin-bottom:8px}
.tools{display:flex;align-items:center;gap:10px;margin-bottom:9px}
#q{flex:0 1 340px;min-width:0;background:#14171b;border:1px solid #262b32;border-radius:8px;
 color:#e6e8ec;font:13px system-ui,Segoe UI,Roboto,sans-serif;padding:6px 10px}
#q:focus{outline:none;border-color:#3a7bd5;background:#171b21}
#q::placeholder{color:#5b626b}
#qinfo{color:#5db0ff;font-size:12px;white-space:nowrap}
#nores{display:none;padding:26px 18px;color:#8a919b;font-size:13px;text-align:center}
/* Fixed to the bottom, not sticky-in-flow: the element sits at the END of the document,
   so a sticky top would park it below thousands of tiles — invisible exactly when it
   matters. Bottom also avoids fighting the sticky header. */
#stale{display:none;position:fixed;left:0;right:0;bottom:0;z-index:60;background:#3a2a12;
 color:#ffd9a0;border-top:1px solid #6b4a1c;padding:9px 18px;font-size:12.5px;line-height:1.5;
 box-shadow:0 -4px 18px #0009}
#stale.on{display:block}
#stale code{background:#1c1508;border:1px solid #6b4a1c;border-radius:5px;padding:1px 6px;
 color:#ffe9c4;user-select:all}
nav{display:flex;flex-wrap:wrap;gap:6px}
nav a{color:#cdd3da;text-decoration:none;background:#171a1f;border:1px solid #262b32;
 padding:3px 9px;border-radius:999px;font-size:12px}
nav a:hover{background:#1f242b} nav a b{color:#5db0ff;font-weight:600}
section{padding:20px 18px;border-bottom:1px solid #16191d}
h2{margin:0 0 14px;font-size:15px;font-weight:600}
h2 .n,.vids .n{color:#6b7280;font-weight:400;font-size:12px}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:10px}
.c{display:flex;flex-direction:column;background:#14171b;border:1px solid #21262d;
 border-radius:8px;overflow:hidden;text-decoration:none;color:#c3c9d1}
.c:hover{border-color:#3a7bd5}
.c img{width:100%;height:130px;object-fit:cover;background:#0a0c0f;display:block}
.cap{padding:6px 8px;font-size:11px;color:#9aa2ac;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
h2 .capnote{color:#c98a3a;font-weight:400;font-size:11px;margin-left:6px}
.pgrid{display:grid;grid-template-columns:repeat(auto-fill,minmax(230px,1fr));gap:12px}
.pcard{display:flex;flex-direction:column;background:#14171b;border:1px solid #21262d;border-radius:8px;overflow:hidden}
.pcard:hover{border-color:#3a7bd5}
.pcard img,.pcard .noimg{width:100%;height:150px;object-fit:cover;background:#0a0c0f;display:flex;
 align-items:center;justify-content:center;color:#39424d;font-size:22px}
.pbody{padding:9px 11px;display:flex;flex-direction:column;gap:4px}
.pbody b{font-size:13px;color:#e6e8ec;line-height:1.25}
.pmeta{font-size:11px;color:#5db0ff}
.pbody p{margin:0;font-size:11px;color:#9aa2ac;line-height:1.35;max-height:74px;overflow:hidden}
a.plink{text-decoration:none;color:inherit;display:flex;flex-direction:column;gap:4px}
a.plink b{color:#8ec5ff}
.vids{list-style:none;padding:0;margin:0;columns:2;gap:20px}
.vids li{margin:2px 0} .vids a{color:#5db0ff;text-decoration:none}
footer{padding:18px;color:#5b626b;font-size:11px;text-align:center}
#lb{position:fixed;inset:0;background:#000000ee;z-index:50;display:none;align-items:center;justify-content:center}
#lb.on{display:flex}
#lb figure{margin:0;display:flex;flex-direction:column;align-items:center;gap:10px}
#lb img{max-width:94vw;max-height:84vh;object-fit:contain;box-shadow:0 8px 40px #000;background:#0a0c0f}
#lb figcaption{color:#cdd3da;font-size:12px;max-width:90vw;text-align:center;word-break:break-word}
#lb figcaption .idx{color:#6b7280;margin-left:8px}
#lb button{position:fixed;background:#171a1fcc;color:#e6e8ec;border:1px solid #2a2f37;border-radius:8px;
 cursor:pointer;font:24px/1 system-ui;padding:8px 15px;-webkit-user-select:none;user-select:none}
#lb button:hover{background:#232830;border-color:#3a7bd5}
#lb-x{top:14px;right:16px}
#lb-prev{left:14px;top:50%;transform:translateY(-50%)}
#lb-next{right:14px;top:50%;transform:translateY(-50%)}
@media (max-width:640px){#lb-prev{left:6px}#lb-next{right:6px}#lb button{padding:6px 11px}}
@media (prefers-reduced-motion:no-preference){#lb.on img{animation:lbin .12s ease}}
@keyframes lbin{from{opacity:0;transform:scale(.98)}to{opacity:1;transform:none}}
"""

# Lightbox + live filter. Sections are the grouping unit: arrows stay inside the
# section you opened, and skip anything the filter has hidden.
UI = """<div id="stale" role="status"></div>
<div id="nores">No matches — try a different word, or clear the filter (Esc).</div>
<div id="lb" role="dialog" aria-modal="true" aria-label="Image viewer">
 <button id="lb-prev" aria-label="Previous">&lsaquo;</button>
 <figure><img id="lb-img" alt=""><figcaption id="lb-cap"></figcaption></figure>
 <button id="lb-next" aria-label="Next">&rsaquo;</button>
 <button id="lb-x" aria-label="Close">&times;</button>
</div>
<script>
// A gallery stores paths relative to itself, so moving or reorganising the library
// silently turns every tile into a broken image. A page cannot stat the filesystem over
// file://, but it CAN notice its own images failing — which is the same signal, arriving
// exactly when it matters. Say so, with the command that fixes it.
(function(){
  var body=document.body, box=document.getElementById('stale');
  if(!box) return;
  var missing=0, shown=false;
  document.addEventListener('error', function(e){
    var el=e.target;
    if(!el || el.tagName!=='IMG' || el.id==='lb-img') return;   // lightbox has no src until opened
    missing++;
    if(shown) { box.querySelector('b').textContent = missing.toLocaleString(); return; }
    shown=true;
    var gen=body.getAttribute('data-generated')||'', root=body.getAttribute('data-root')||'';
    box.innerHTML='⚠ <b>'+missing.toLocaleString()+'</b> image(s) failed to load — this page was '+
      'generated '+gen+' and the files may have moved since. Regenerate: '+
      '<code>navig explore gallery '+root.replace(/&/g,'&amp;').replace(/</g,'&lt;')+' --recursive</code>';
    box.classList.add('on');
  }, true);   // capture: 'error' does not bubble
})();
(function(){
  var secs = [].slice.call(document.querySelectorAll('section'));
  if(!secs.length) return;
  var lb=document.getElementById('lb'), img=document.getElementById('lb-img'),
      cap=document.getElementById('lb-cap'), nores=document.getElementById('nores'),
      q=document.getElementById('q'), qinfo=document.getElementById('qinfo');

  var units = [];
  secs.forEach(function(sec){
    var h2 = sec.querySelector('h2');
    var label = h2 && h2.firstChild ? h2.firstChild.textContent.trim() : sec.id;
    var badge = sec.querySelector('h2 .n');
    if(badge) badge.setAttribute('data-orig', badge.textContent);
    [].slice.call(sec.querySelectorAll('a.c, .pcard, .vids li')).forEach(function(el){
      // grid cell is its own lightbox link; a rich card carries one inside it
      var lbEl = el.classList.contains('c') ? el : el.querySelector('a.lb');
      var txt  = el.getAttribute('title') || el.textContent || '';
      units.push({el:el, gid:sec.id, label:label, lb:lbEl, hid:false,
                  txt:txt.toLowerCase().replace(/\\s+/g,' ')});
    });
  });
  var pills = {};
  [].slice.call(document.querySelectorAll('nav a')).forEach(function(a){
    var h=a.getAttribute('href')||''; if(h.charAt(0)==='#') pills[h.slice(1)]=a;
  });

  var cur=[], ci=-1;
  function esc(s){ return s.replace(/&/g,'&amp;').replace(/</g,'&lt;'); }
  function render(){
    var u=cur[ci]; if(!u) return;
    img.src=u.lb.getAttribute('href');
    cap.innerHTML=esc(u.lb.getAttribute('data-cap')||u.lb.getAttribute('title')||'')+
      '<span class="idx">'+esc(u.label)+' \\u00b7 '+(ci+1)+' / '+cur.length+'</span>';
    lb.classList.add('on'); document.body.style.overflow='hidden';
  }
  function open(u){
    cur=units.filter(function(x){ return x.gid===u.gid && !x.hid && x.lb; });
    ci=cur.indexOf(u); if(ci<0) return; render();
  }
  function step(d){ if(cur.length){ ci=(ci+d+cur.length)%cur.length; render(); } }
  function close(){ lb.classList.remove('on'); img.removeAttribute('src'); document.body.style.overflow=''; }
  units.forEach(function(u){
    if(!u.lb) return;
    u.lb.addEventListener('click',function(e){
      if(e.metaKey||e.ctrlKey||e.shiftKey||e.button) return;
      e.preventDefault(); open(u);
    });
  });
  lb.addEventListener('click',function(e){ if(e.target===lb) close(); });
  document.getElementById('lb-x').onclick=close;
  document.getElementById('lb-prev').onclick=function(e){ e.stopPropagation(); step(-1); };
  document.getElementById('lb-next').onclick=function(e){ e.stopPropagation(); step(1); };

  function filter(){
    var s=(q?q.value:'').trim().toLowerCase(), shown=0, per={};
    units.forEach(function(u){
      var hit = !s || u.txt.indexOf(s)!==-1;
      u.hid=!hit;
      var want = hit ? '' : 'none';
      if(u.el.style.display!==want) u.el.style.display=want;
      if(hit){ shown++; per[u.gid]=(per[u.gid]||0)+1; }
    });
    secs.forEach(function(sec){
      var n=per[sec.id]||0, want = n ? '' : 'none';
      if(sec.style.display!==want) sec.style.display=want;
      var p=pills[sec.id];
      if(p){
        if(p.style.display!==want) p.style.display=want;
        var pb=p.querySelector('b');
        if(pb){ var po=pb.getAttribute('data-orig');
                if(po===null){ po=pb.textContent; pb.setAttribute('data-orig',po); }
                pb.textContent = s ? n : po; }
      }
      var b=sec.querySelector('h2 .n');
      if(b){ var o=b.getAttribute('data-orig')||''; b.textContent = s ? (n+' of '+o) : o; }
    });
    if(qinfo) qinfo.textContent = s ? (shown+' match'+(shown===1?'':'es')) : '';
    nores.style.display = (s && !shown) ? 'block' : 'none';
  }
  var T;
  if(q){
    q.addEventListener('input',function(){ clearTimeout(T); T=setTimeout(filter,110); });
    q.addEventListener('search',filter);
  }

  document.addEventListener('keydown',function(e){
    if(lb.classList.contains('on')){
      if(e.key==='Escape') close();
      else if(e.key==='ArrowLeft') step(-1);
      else if(e.key==='ArrowRight') step(1);
      return;
    }
    if(!q) return;
    if(e.key==='/' && document.activeElement!==q){ e.preventDefault(); q.focus(); q.select(); }
    else if(e.key==='Escape' && document.activeElement===q){ q.value=''; filter(); q.blur(); }
  });
})();
</script>"""


def build_gallery(
    root: Path,
    out: Path | None = None,
    *,
    title: str | None = None,
    thumb_max: int = 320,
    prune: bool = False,
    sections: Sequence[tuple[str, Path | str]] | None = None,
    include_root: bool = True,
    video_root: Path | str | None = None,
    extra_sections: Callable[[GalleryContext], Sequence[ExtraSection]]
    | Sequence[ExtraSection]
    | None = None,
    extra_css: str = "",
    thumbs_dir: Path | str | None = None,
    stale_dir: Path | str | None = None,
    max_per_section: int | None = None,
    footer: str | None = None,
) -> dict:
    """Build ``gallery.html`` for *root*. Returns a stats dict.

    By default sections are the top-level subfolders (plus a "Root" section for loose
    files), so the grouping matches how the folder is already organized.

    Callers with their own structure can override that: pass ``sections`` for an explicit
    ordered ``(label, folder)`` list, and ``extra_sections`` to inject curated content
    (built from a CSV, a database, anything) above the scanned ones — it receives a
    :class:`GalleryContext` and shares the same thumbnail cache.

    ``max_per_section`` bounds huge folders. Truncation is always visible: the capped
    section says so in its heading and the returned stats list what was dropped.
    """
    root = Path(root).expanduser().resolve()
    if not root.is_dir():
        raise NotADirectoryError(f"not a folder: {root}")
    out_path = Path(out).expanduser().resolve() if out else root / "gallery.html"
    out_dir = out_path.parent
    out_dir.mkdir(parents=True, exist_ok=True)
    side = root / ".mediaexplorer"
    thumbs = _Thumbs(Path(thumbs_dir) if thumbs_dir else side / "thumbs", thumb_max, out_dir)

    if sections is None:
        groups = [
            (d.name, d) for d in sorted(root.iterdir(), key=lambda p: p.name.lower())
            if d.is_dir() and not _skip(d.name)
        ]
        if include_root:
            groups.insert(0, ("Root", root))
    else:
        groups = [(lbl, (root / p) if not Path(p).is_absolute() else Path(p))
                  for lbl, p in sections]

    parts: list[str] = []
    nav: list[str] = []
    total = 0
    capped: dict[str, tuple[int, int]] = {}

    # Root-level files only — a plain listing, never a recursive walk of the whole tree.
    loose: list[tuple[Path, int]] = []
    if any(Path(p) == root for _, p in groups):
        for f in sorted(root.iterdir(), key=lambda p: p.name.lower()):
            if f.is_file() and _is_media(f.name, IMG_EXT):
                try:
                    size = f.stat().st_size
                except OSError:
                    continue
                if size:
                    loose.append((f, size))

    for label, path in groups:
        path = Path(path)
        if not path.is_dir():
            continue
        images = loose if path == root else _walk(path, IMG_EXT)
        if not images:
            continue
        found = len(images)
        note = ""
        if max_per_section and found > max_per_section:
            images = images[:max_per_section]
            capped[label] = (max_per_section, found)
            note = (f' <span class="capnote">of {found:,} &mdash; capped at '
                    f'{max_per_section:,}</span>')
        cells = []
        for fp, size in images:
            src = thumbs.href(fp)
            if src is None:
                continue          # undecodable — a tile here would be a broken image
            name = html.escape(fp.name)
            cells.append(
                f'<a class="c" href="{_rel(fp, out_dir)}" target="_blank" '
                f'title="{name} &middot; {_human(size)}">'
                f'<img loading="lazy" src="{src}" alt="">'
                f'<span class="cap">{name}</span></a>'
            )
        if not cells:
            continue
        sid = _slug(label)
        total += len(cells)
        nav.append(f'<a href="#{sid}">{html.escape(label)} <b>{len(cells)}</b></a>')
        parts.append(
            f'<section id="{sid}"><h2>{html.escape(label)} '
            f'<span class="n">{len(cells)}</span>{note}</h2>'
            f'<div class="grid">{"".join(cells)}</div></section>'
        )

    vroot = Path(video_root) if video_root else root
    videos = _walk(vroot, VID_EXT) if vroot.is_dir() else []
    shown_videos = videos
    if max_per_section and len(videos) > max_per_section:
        shown_videos = videos[:max_per_section]
        capped["Video"] = (max_per_section, len(videos))
    if shown_videos:
        vnote = ("" if shown_videos is videos else
                 f' <span class="capnote">of {len(videos):,} &mdash; capped at '
                 f'{max_per_section:,}</span>')
        nav.append(f'<a href="#video">Video <b>{len(shown_videos)}</b></a>')
        rows = "".join(
            f'<li><a href="{_rel(fp, out_dir)}" target="_blank">{html.escape(fp.name)}</a>'
            f' <span class="n">{_human(size)}</span></li>' for fp, size in shown_videos
        )
        parts.append(
            f'<section id="video"><h2>Video <span class="n">{len(shown_videos)}</span>'
            f'{vnote}</h2><ul class="vids">{rows}</ul></section>'
        )

    # caller-supplied sections go first (they are the curated headline)
    extras: list[ExtraSection] = []
    if extra_sections is not None:
        ctx = GalleryContext(root, out_dir, thumbs)
        got = extra_sections(ctx) if callable(extra_sections) else extra_sections
        extras = [e for e in (got or []) if e.count or e.html]
        for e in reversed(extras):
            badge = e.badge if e.badge is not None else str(e.count)
            nav.insert(0, f'<a href="#{e.id}">{html.escape(e.label)} <b>{e.count}</b></a>')
            parts.insert(0, f'<section id="{e.id}"><h2>{html.escape(e.label)} '
                            f'<span class="n">{html.escape(badge)}</span></h2>{e.html}</section>')

    heading = html.escape(title or root.name or str(root))
    stamp = datetime.date.today().isoformat()
    extra_meta = "".join(f"{e.count} {html.escape(e.label.lower())} &middot; " for e in extras)
    tail = footer or (
        "Generated by <code>navig explore gallery</code> &middot; paths relative to "
        f"<code>{html.escape(out_dir.name)}/</code>."
    )
    doc = (
        '<!doctype html>\n<html lang="en"><head><meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width,initial-scale=1">\n'
        f"<title>{heading} — Gallery</title>\n<style>{CSS}{extra_css}</style></head>\n"
        f'<body data-generated="{stamp}" data-root="{html.escape(str(root))}">\n'
        f"<header>\n <h1>{heading}</h1>\n"
        f' <div class="meta">{extra_meta}{total:,} images &middot; {len(videos):,} videos '
        f"&middot; generated {stamp}</div>\n"
        ' <div class="tools">\n'
        '  <input id="q" type="search" placeholder="Filter by name&hellip;   (press / )" '
        'autocomplete="off" spellcheck="false" aria-label="Filter items">\n'
        '  <span id="qinfo"></span>\n </div>\n'
        f' <nav>{"".join(nav)}</nav>\n</header>\n'
        f'{"".join(parts)}\n'
        f"<footer>{tail}</footer>\n"
        f"{UI}\n</body></html>\n"
    )
    out_path.write_text(doc, encoding="utf-8")

    pruned = thumbs.prune(Path(stale_dir) if stale_dir else thumbs.dir / "_stale") if prune else 0
    return {
        "out": str(out_path),
        "images": total,
        "videos": len(shown_videos),
        "videos_found": len(videos),
        "sections": len(parts),
        "extra_sections": len(extras),
        "bytes": out_path.stat().st_size,
        "pruned": pruned,
        "pillow": HAVE_PIL,
        "capped": capped,
        "dropped": sum(found - shown for shown, found in capped.values()),
        **{f"thumb_{k}": v for k, v in thumbs.stats.items()},
    }


INDEX_CSS = """
:root{color-scheme:dark}body{margin:0;background:#0d0f12;color:#e6e8ec;
 font:14px/1.5 system-ui,Segoe UI,Roboto,sans-serif;padding:26px}
h1{font-size:19px;margin:0 0 4px}.sub{color:#8a919b;font-size:12px;margin-bottom:20px}
ul{list-style:none;padding:0;margin:0;display:grid;
 grid-template-columns:repeat(auto-fill,minmax(280px,1fr));gap:8px}
li{background:#14171b;border:1px solid #21262d;border-radius:8px;padding:10px 12px}
li:hover{border-color:#3a7bd5}a{color:#8ec5ff;text-decoration:none;font-weight:600}
li span{display:block;color:#6b7280;font-size:11px;margin-top:2px}
"""


def _count_images(base: Path) -> int:
    n = 0
    for dirpath, dirnames, filenames in os.walk(base):
        dirnames[:] = [d for d in dirnames if not _skip(d)]
        n += sum(1 for f in filenames if _is_media(f, IMG_EXT))
    return n


def _tree_targets(root: Path, split_over: int) -> list[tuple[str, Path]]:
    """Which folders get their own gallery.

    One per immediate subfolder — except a subfolder holding more than *split_over*
    images, which is expanded into ITS children instead. That is what makes a mixed
    library work in one pass: a 15k-image folder stays one browsable page, while a
    97k-image folder (grouped by year underneath) becomes one page per year rather
    than a single document no browser can open.
    """
    targets: list[tuple[str, Path]] = []
    for child in sorted(root.iterdir(), key=lambda p: p.name.lower()):
        if not child.is_dir() or _skip(child.name):
            continue
        count = _count_images(child)
        if not count:
            continue
        subs = [d for d in sorted(child.iterdir(), key=lambda p: p.name.lower())
                if d.is_dir() and not _skip(d.name)]
        if count > split_over and subs:
            for sub in subs:
                if _count_images(sub):
                    targets.append((f"{child.name}/{sub.name}", sub))
        else:
            targets.append((child.name, child))
    return targets


def build_gallery_tree(
    root: Path,
    *,
    split_over: int = 20_000,
    index_name: str = "galleries-index.html",
    title: str | None = None,
    on_progress: Callable[[str, dict], None] | None = None,
    **kwargs,
) -> dict:
    """Build one gallery per subfolder and an index page linking them.

    Point it at a library whose top level is already meaningful (categories, years,
    clients) and every part becomes its own browsable page — instead of one document
    holding 100k images. Returns a summary; ``on_progress`` is called per gallery.
    """
    root = Path(root).expanduser().resolve()
    if not root.is_dir():
        raise NotADirectoryError(f"not a folder: {root}")
    targets = _tree_targets(root, split_over)

    built: list[tuple[str, Path, dict]] = []
    for label, path in targets:
        stats = build_gallery(path, title=label, **kwargs)
        built.append((label, path, stats))
        if on_progress:
            on_progress(label, stats)

    rows = []
    for label, path, stats in sorted(built, key=lambda b: b[2]["images"]):
        href = quote(os.path.relpath(path / "gallery.html", root).replace("\\", "/"), safe="/()")
        rows.append(
            f'<li><a href="{href}">{html.escape(label)}</a>'
            f'<span>{stats["images"]:,} images'
            + (f' · {stats["videos"]:,} videos' if stats["videos"] else "")
            + f' · {stats["sections"]} sections</span></li>'
        )
    total = sum(s["images"] for _, _, s in built)
    heading = html.escape(title or root.name or str(root))
    stamp = datetime.date.today().isoformat()
    index = root / index_name
    index.write_text(
        '<!doctype html>\n<html lang="en"><head><meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width,initial-scale=1">\n'
        f"<title>{heading} — galleries</title>\n<style>{INDEX_CSS}</style></head><body>\n"
        f"<h1>{heading} — galleries</h1>\n"
        f'<div class="sub">{len(built)} galleries · {total:,} images · generated {stamp}</div>\n'
        f'<ul>\n{chr(10).join(rows)}\n</ul>\n</body></html>\n',
        encoding="utf-8",
    )
    return {
        "index": str(index),
        "galleries": len(built),
        "images": total,
        "videos": sum(s["videos"] for _, _, s in built),
        "thumb_fail": sum(s["thumb_fail"] for _, _, s in built),
        "dropped": sum(s["dropped"] for _, _, s in built),
        "built": [(label, s["images"]) for label, _, s in built],
    }


def _serve_root(out_path: Path, root: Path) -> Path:
    """Directory to serve so BOTH the page and the media it links resolve.

    The page uses paths relative to itself (often ``../images/x.jpg``), and the HTTP
    handler refuses to serve outside its root — so serve the common ancestor.
    """
    out_path, root = out_path.resolve(), root.resolve()
    try:
        return Path(os.path.commonpath([out_path.parent, root]))
    except ValueError:  # different drives — fall back to the page's own folder
        return out_path.parent


def _lan_ip() -> str | None:
    import socket  # noqa: PLC0415
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))   # no packet is sent; just picks the outbound NIC
        return s.getsockname()[0]
    except Exception:  # noqa: BLE001
        return None
    finally:
        s.close()


DEFAULT_PORT = 8099   # outside the ranges Windows/Hyper-V commonly reserves


def serve_gallery(
    out_path: Path,
    root: Path,
    *,
    port: int | None = None,
    lan: bool = False,
    open_browser: bool = True,
) -> None:
    """Serve a built gallery over HTTP so phones/tablets on the LAN can open it.

    ``port=None`` prefers :data:`DEFAULT_PORT` and, if that is taken or reserved, lets
    the OS pick a free one — Windows/Hyper-V black-hole whole port ranges, so a fixed
    default cannot be relied on. An explicit port is honoured exactly, and a clear error
    is raised if it cannot be bound.

    Binds localhost by default; ``lan=True`` binds all interfaces. The server is
    read-only (GET/HEAD) but **unauthenticated**, so LAN mode exposes everything under
    the served folder to anyone on the network — the CLI says so before starting.
    """
    import functools  # noqa: PLC0415
    from http.server import SimpleHTTPRequestHandler  # noqa: PLC0415

    from navig.http_bind import PortBindError, bind_http_server  # noqa: PLC0415

    out_path, root = Path(out_path).resolve(), Path(root).resolve()
    base = _serve_root(out_path, root)
    url_path = "/" + os.path.relpath(out_path, base).replace("\\", "/")
    handler = functools.partial(SimpleHTTPRequestHandler, directory=str(base))
    host = "0.0.0.0" if lan else "127.0.0.1"  # noqa: S104 - LAN mode is explicit opt-in
    try:
        srv, bound = bind_http_server(handler, port, preferred=DEFAULT_PORT, host=host)
    except PortBindError as exc:
        raise GalleryServeError(str(exc)) from exc

    print(f"\n  Gallery → http://localhost:{bound}{url_path}", flush=True)
    if lan:
        ip = _lan_ip()
        if ip:
            print(f"  On your phone → http://{ip}:{bound}{url_path}", flush=True)
        print("  ⚠  LAN mode: anyone on this network can read "
              f"{base} — no authentication.", flush=True)
    print("  Ctrl+C to stop.\n", flush=True)

    if open_browser:
        try:
            import webbrowser  # noqa: PLC0415
            webbrowser.open(f"http://localhost:{bound}{url_path}")
        except Exception:  # noqa: BLE001
            pass
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()
