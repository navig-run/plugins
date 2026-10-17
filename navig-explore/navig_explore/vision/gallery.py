"""A self-contained HTML page for a set of search results.

Deliberately small and separate from ``navig_explore.gallery``, which renders a
whole folder tree. This one renders *an answer* — the photos a query returned,
each carrying the evidence behind it, so a claimed date can be judged rather
than taken on trust.

Thumbnails are inlined as data URIs, so the page is one file you can move, keep
or send without breaking it.
"""
from __future__ import annotations

import base64
import html
import io
from pathlib import Path

THUMB = 320


def _thumb_uri(path: str, size: int = THUMB) -> str:
    try:
        from PIL import Image, ImageOps  # noqa: PLC0415

        im = Image.open(path)
        im = ImageOps.exif_transpose(im)
        im.thumbnail((size, size))
        buf = io.BytesIO()
        im.convert("RGB").save(buf, format="JPEG", quality=78)
        return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()
    except Exception:  # noqa: BLE001 - a broken thumb must not break the page
        return ""


_CSS = """
:root { color-scheme: light dark; --bg:#fff; --fg:#111; --mut:#666; --card:#f6f6f7; --line:#e3e3e6; }
@media (prefers-color-scheme: dark) {
  :root { --bg:#111316; --fg:#e9e9ec; --mut:#9a9aa2; --card:#1b1e22; --line:#2a2e34; }
}
* { box-sizing:border-box; }
body { margin:0; padding:24px; background:var(--bg); color:var(--fg);
       font:14px/1.5 ui-sans-serif,system-ui,-apple-system,Segoe UI,Roboto,sans-serif; }
h1 { font-size:18px; margin:0 0 4px; }
.sub { color:var(--mut); margin-bottom:20px; }
.grid { display:grid; gap:14px; grid-template-columns:repeat(auto-fill,minmax(220px,1fr)); }
.card { background:var(--card); border:1px solid var(--line); border-radius:10px;
        overflow:hidden; display:flex; flex-direction:column; }
.card img { width:100%; aspect-ratio:1; object-fit:cover; display:block; background:#0002; }
.meta { padding:9px 11px; font-size:12px; }
.name { font-weight:600; overflow-wrap:anywhere; }
.row { color:var(--mut); margin-top:3px; overflow-wrap:anywhere; }
.tag { display:inline-block; padding:1px 7px; border-radius:99px; font-size:11px;
       border:1px solid var(--line); margin-right:4px; }
.low { opacity:.62; }
"""


def _facet_thumb(path: str, dest: Path, size: int = 240) -> bool:
    """Write one thumbnail, reusing it if already present."""
    if dest.exists() and dest.stat().st_size:
        return True
    try:
        from PIL import Image, ImageOps  # noqa: PLC0415

        im = Image.open(path)
        im = ImageOps.exif_transpose(im)
        im.thumbnail((size, size))
        im.convert("RGB").save(dest, format="JPEG", quality=72)
        return True
    except Exception:  # noqa: BLE001
        return False


_FACET_JS = """
const D = window.__DATA__, F = {person:null, year:null, place:null, class:null};
const grid = document.getElementById('grid'), count = document.getElementById('count');
function matches(d){
  return (!F.person || d.pe.includes(F.person)) && (!F.year || d.y === F.year)
      && (!F.place  || d.pl === F.place)       && (!F.class || d.c === F.class);
}
function render(){
  const hits = D.filter(matches);
  count.textContent = hits.length.toLocaleString() + ' of ' + D.length.toLocaleString();
  grid.innerHTML = hits.slice(0, 1200).map(d => {
    const meta = [];
    if (d.d) meta.push('<div class="row'+(d.dc<0.8?' low':'')+'">'+d.d+' · '+d.ds+'</div>');
    if (d.pe.length) meta.push('<div class="row">'+d.pe.map(p=>'<span class="tag">'+p+'</span>').join('')+'</div>');
    if (d.pl) meta.push('<div class="row">'+d.pl+'</div>');
    return '<div class="card"><a href="'+d.f+'" target="_blank">'
      + '<img loading="lazy" src="thumbs/'+d.t+'" alt=""></a><div class="meta">'
      + '<div class="name">'+d.n+'</div>'+meta.join('')+'</div></div>';
  }).join('');
  if (hits.length > 1200)
    grid.insertAdjacentHTML('beforeend',
      '<div class="note">showing the first 1,200 of '+hits.length.toLocaleString()+
      ' matches — narrow the filters to see the rest</div>');
}
document.querySelectorAll('.facet button').forEach(b => b.onclick = () => {
  const k = b.dataset.k, v = b.dataset.v === '' ? null : b.dataset.v;
  F[k] = (F[k] === v) ? null : v;
  document.querySelectorAll('.facet button[data-k="'+k+'"]').forEach(x =>
    x.classList.toggle('on', x.dataset.v === (F[k] ?? '\\u0000')));
  render();
});
render();
"""

_FACET_CSS = _CSS + """
.wrap { display:grid; grid-template-columns:230px 1fr; gap:22px; align-items:start; }
@media (max-width:760px){ .wrap { grid-template-columns:1fr; } }
.side { position:sticky; top:16px; max-height:92vh; overflow:auto; }
.facet { margin-bottom:18px; }
.facet h3 { font-size:11px; text-transform:uppercase; letter-spacing:.07em;
            color:var(--mut); margin:0 0 6px; }
.facet button { display:block; width:100%; text-align:left; background:none;
  border:0; color:var(--fg); padding:3px 7px; border-radius:6px; cursor:pointer;
  font:inherit; font-size:12.5px; }
.facet button:hover { background:var(--card); }
.facet button.on { background:var(--fg); color:var(--bg); }
.facet .n { float:right; color:var(--mut); font-variant-numeric:tabular-nums; }
.facet button.on .n { color:var(--bg); opacity:.75; }
.note { grid-column:1/-1; color:var(--mut); padding:14px 2px; font-size:12.5px; }
"""


def _facet_block(key: str, label: str, pairs: list[tuple[str, int]]) -> str:
    btns = "".join(
        f'<button data-k="{key}" data-v="{html.escape(str(v))}">{html.escape(str(v))}'
        f'<span class="n">{n:,}</span></button>' for v, n in pairs)
    return f'<div class="facet"><h3>{html.escape(label)}</h3>{btns}</div>'


def write_facets(root: Path, out: Path, *, limit: int = 4000,
                 workers: int = 12, quiet: bool = False) -> tuple[Path, dict]:
    """A persistent browse page over the catalog, filterable by person/year/place/kind.

    Everything shown comes from ``vision.db``; the originals are only read to make
    thumbnails. A derived date is displayed with its source and dimmed when it is
    inferred, so the page can never present a guess as a measured fact.
    """
    import json  # noqa: PLC0415
    from collections import Counter  # noqa: PLC0415
    from concurrent.futures import ThreadPoolExecutor  # noqa: PLC0415

    from . import catalog  # noqa: PLC0415

    root = Path(root).resolve()
    conn = catalog.connect(root)
    out = Path(out).expanduser().resolve()
    thumbs = out.parent / "thumbs"
    thumbs.mkdir(parents=True, exist_ok=True)

    total = conn.execute(
        "SELECT COUNT(*) FROM files WHERE present=1 AND root=? AND sha256 IS NOT NULL",
        (str(root),)).fetchone()[0]

    # A capped page must be REPRESENTATIVE, not extreme. Ordering by `quality`
    # (variance of Laplacian) seemed sensible and was badly wrong: heavily
    # stylised line-art filter photos have enormous edge variance, so they
    # crowded out every ordinary photograph. Prefer photographs, then ones with
    # a face and a real date — then order by sha256, which is a stable spread
    # rather than a ranking of any single attribute.
    rows = conn.execute("""
        SELECT f.path, f.rel, f.name, f.sha256,
               d.value AS dv, d.source AS ds, d.confidence AS dc,
               g.place AS place, g.country AS country,
               (SELECT class FROM classes c WHERE c.sha256=f.sha256
                ORDER BY score DESC LIMIT 1) AS cls,
               EXISTS(SELECT 1 FROM faces x WHERE x.sha256 = f.sha256) AS has_face
        FROM files f
        JOIN assets a ON a.sha256 = f.sha256 AND a.decoded = 1
        LEFT JOIN dates d ON d.sha256 = f.sha256
        LEFT JOIN geo   g ON g.sha256 = f.sha256
        WHERE f.present = 1 AND f.root = ?
        GROUP BY f.sha256
        ORDER BY (CASE WHEN cls='photo' THEN 0 ELSE 1 END),
                 has_face DESC,
                 (CASE WHEN d.confidence >= 0.8 THEN 0 ELSE 1 END),
                 f.sha256
        LIMIT ?""", (str(root), int(limit))).fetchall()

    people: dict[str, list[str]] = {}
    for r in conn.execute("""
            SELECT fa.sha256, p.name FROM faces fa
            JOIN people p ON p.person_id = fa.person_id
            WHERE p.name IS NOT NULL"""):
        people.setdefault(r["sha256"], []).append(r["name"])

    items, jobs = [], []
    for r in rows:
        tname = f"{r['sha256'][:20]}.jpg"
        jobs.append((r["path"], thumbs / tname))
        place = r["place"] or ""
        if place and r["country"]:
            place = f"{place}, {r['country']}"
        items.append({
            "t": tname, "n": r["name"], "f": Path(r["path"]).as_uri(),
            "d": (r["dv"] or "")[:10], "ds": r["ds"] or "", "dc": r["dc"] or 0,
            "y": (r["dv"] or "")[:4], "pl": place, "c": r["cls"] or "",
            "pe": sorted(set(people.get(r["sha256"], []))),
        })

    ok = 0
    with ThreadPoolExecutor(max_workers=workers) as ex:
        for good in ex.map(lambda a: _facet_thumb(*a), jobs):
            ok += bool(good)
    items = [it for it, (_p, dest) in zip(items, jobs) if dest.exists()]

    yr = Counter(i["y"] for i in items if i["y"])
    pl = Counter(i["pl"] for i in items if i["pl"])
    cl = Counter(i["c"] for i in items if i["c"])
    pe: Counter = Counter()
    for i in items:
        pe.update(i["pe"])

    side = (
        _facet_block("person", "People", pe.most_common(30))
        + _facet_block("year", "Year", sorted(yr.items(), reverse=True))
        + _facet_block("place", "Place", pl.most_common(25))
        + _facet_block("class", "Kind", cl.most_common())
    )
    capped = ("" if total <= limit else
              f" · <strong>capped at {len(items):,}</strong> of {total:,} indexed "
              f"(raise with --limit)")
    doc = (
        "<!doctype html><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        f"<title>{html.escape(root.name)} — photo facets</title>"
        f"<style>{_FACET_CSS}</style>"
        f"<h1>{html.escape(str(root))}</h1>"
        f"<div class='sub'><span id='count'></span> shown{capped} · dates carry their "
        f"source; dimmed means inferred, not measured</div>"
        f"<div class='wrap'><div class='side'>{side}</div>"
        f"<div class='grid' id='grid'></div></div>"
        f"<script>window.__DATA__={json.dumps(items, ensure_ascii=False)};</script>"
        f"<script>{_FACET_JS}</script>")
    out.write_text(doc, encoding="utf-8")

    stats = {"shown": len(items), "indexed": total, "thumbs_written": ok,
             "people": len(pe), "years": len(yr), "places": len(pl)}
    if not quiet:
        print(f"[gallery] {stats}", flush=True)
    return out, stats


def write_results(hits: list[dict], out: Path, *, title: str = "results") -> Path:
    """Render search hits, each annotated with the evidence behind its date."""
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    cards = []
    for h in hits:
        uri = _thumb_uri(h["path"])
        rows = []
        if h.get("date"):
            # A derived date is shown WITH its source; a weak one is visibly dimmed,
            # so nothing inferred can be mistaken for something measured.
            conf = h.get("date_confidence") or 0
            cls = "row" if conf >= 0.8 else "row low"
            rows.append(f'<div class="{cls}">{html.escape(h["date"][:10])} '
                        f'· {html.escape(str(h.get("date_source")))} '
                        f'({conf:.2f})</div>')
        if h.get("people"):
            rows.append('<div class="row">' +
                        " ".join(f'<span class="tag">{html.escape(p)}</span>'
                                 for p in h["people"]) + "</div>")
        if h.get("class"):
            rows.append(f'<div class="row"><span class="tag">'
                        f'{html.escape(h["class"])}</span>'
                        f'{h.get("score", 0):.3f}</div>')
        cards.append(
            f'<div class="card"><a href="file:///{html.escape(h["path"])}">'
            f'<img loading="lazy" src="{uri}" alt=""></a>'
            f'<div class="meta"><div class="name">{html.escape(h["name"])}</div>'
            + "".join(rows) + "</div></div>")

    doc = (f"<!doctype html><meta charset='utf-8'>"
           f"<meta name='viewport' content='width=device-width,initial-scale=1'>"
           f"<title>{html.escape(title)}</title><style>{_CSS}</style>"
           f"<h1>{html.escape(title)}</h1>"
           f"<div class='sub'>{len(hits)} results · dates shown with the evidence "
           f"they came from; dimmed means inferred, not measured</div>"
           f"<div class='grid'>{''.join(cards)}</div>")
    out.write_text(doc, encoding="utf-8")
    return out
