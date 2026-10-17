/* Reel Stage — renders one scene (hook / terminal / telegram / outro segments) on a
   1080x1920 canvas for `navig pipeline reel|clip` to film.

   Every frame is a PURE FUNCTION OF TIME: render(t) rebuilds whatever it needs from the
   scene and t alone. That is what makes a capture repeatable, `?t=` screenshots exact, and
   `window.__stage.seek(t)` usable for frame-stepped rendering.

   URL:  ?seg=<n>     render only segment n (a reel shot); omit to play the whole scene
         &dur=<secs>  the slot the reel gave this shot; a segment longer than it speeds up
                      (never slows down) and every segment holds its last frame
         &t=<secs>    freeze at t (screenshots)     &manual=1  no clock; drive via seek()
         &guides=1    draw TikTok's UI-covered strips

   Terminal output is REPLAYED from a real capture (`navig pipeline cast`), never typed by
   hand: the scene carries the cast's events verbatim and this file only lays them out. */
(() => {
  "use strict";
  const S = window.__SCENE;
  const P = new URLSearchParams(location.search);
  const stage = document.getElementById("stage");
  // A filmed capture starts a beat after load, so finish this early; a stepped render
  // (manual=1, driven by seek) starts at exactly t=0 and needs none.
  const HEADROOM = P.get("manual") === "1" ? 0 : 0.35;
  const XFADE = 0.25;

  // ── helpers ──────────────────────────────────────────────────────────────
  const clamp = (v, a = 0, b = 1) => Math.min(b, Math.max(a, v));
  const outCubic = (x) => 1 - Math.pow(1 - clamp(x), 3);
  const outBack = (x) => { x = clamp(x); const c = 1.70158; return 1 + (c + 1) * Math.pow(x - 1, 3) + c * Math.pow(x - 1, 2); };
  const el = (tag, cls, html) => { const n = document.createElement(tag); if (cls) n.className = cls; if (html != null) n.innerHTML = html; return n; };
  const esc = (s) => String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  const asset = (name) => (S.assets && S.assets[name]) || name;

  /** `*one or more words*` → gradient emphasis, `\n` → line break; spans for kinetic words. */
  function kinetic(text) {
    return String(text || "").split("\n").map((line) => {
      // Split on the emphasis spans first (odd parts are emphasised), then into words.
      const words = line.split(/\*([^*]+)\*/).flatMap((part, i) =>
        part.split(/(\s+)/).filter((w) => w.length).map((w) => [w, i % 2 === 1]));
      return `<span class="line">${words.map(([w, em]) => {
        if (/^\s+$/.test(w)) return `<span class="w"> </span>`;
        return `<span class="w">${em ? `<em>${esc(w)}</em>` : esc(w)}</span>`;
      }).join("")}</span>`;
    }).join("");
  }
  function popWords(root, lt, start, stagger) {
    root.querySelectorAll(".w").forEach((w, i) => {
      const k = (lt - start - i * stagger) / 0.32;
      w.style.opacity = clamp(k * 1.6);
      w.style.transform = `translateY(${(1 - outCubic(k)) * 40}px) scale(${0.7 + 0.3 * outBack(k)})`;
    });
  }
  function bug() {
    return el("div", "bug", `<img src="${asset("kraken")}" alt=""><span>NAVIG</span>`);
  }
  /** Right-hand transmission readout: live square + episode, site under it (schema's HUD). */
  function hudR() {
    if (!S.episode && !S.site) return null;
    return el("div", "hud-r",
      `<div><span class="sq"></span>${esc(S.episode || "")}</div>` +
      `<div class="site">${esc(S.site || "navig.run")}</div>`);
  }

  /** The slam size ladder — fewer words, bigger type — then shrink until it fits its band.
     Measured from the first render, i.e. after fonts are loaded, so it measures Bebas. */
  function fitSlam(title, avail) {
    const words = (title.textContent || "").trim().split(/\s+/).length;
    let size = words <= 1 ? 300 : words <= 2 ? 270 : words <= 4 ? 230 : words <= 7 ? 195 : 170;
    for (; size > 96; size -= 12) {
      title.style.fontSize = size + "px";
      if (title.scrollHeight <= avail) break;
    }
  }

  /** RGB-split + jitter for `dur` seconds — a few frames, never held (schema's rule).
     Purely a function of `t`: the jitter is seeded by the frame index, so a stepped
     render and a live one produce the same frames. */
  function glitch(node, lt, from, dur, base) {
    const on = lt >= from && lt < from + dur;
    if (!on) {
      if (node.dataset.fx === "1") { node.style.textShadow = base; node.style.transform = ""; }
      node.dataset.fx = "0";
      return;
    }
    const r = Math.sin(Math.floor(lt * 30) * 12.9898) * 43758.5453;
    const f = r - Math.floor(r);
    const off = (5 + f * 7).toFixed(1);
    node.style.textShadow =
      `${off}px 0 rgba(255,74,61,.95), -${off}px 0 rgba(34,211,238,.95), ${base}`;
    node.style.transform = `translateX(${((f - 0.5) * 14).toFixed(1)}px)`;
    node.dataset.fx = "1";
  }
  const TITLE_SHADOW = "0 8px 40px rgba(0,0,0,.75), 0 2px 8px rgba(0,0,0,.55)";
  const CTA_SHADOW = "";
  /** The caption band. With `seg.speech` it becomes a KARAOKE transcript: the words the
      narrator actually says, landing as they are spoken (accent flash on the newest word),
      held through silences, swapping at each cue. Without it: the static beat caption.
      Returns {el, tick(lt, natural)} — tick drives everything, wall-clock-free, so a
      stepped render reproduces it exactly.
      `speech` = {cues: [[text, t0, t1]…], words: [[word, s, e]…]} in SECTION-LOCAL video
      seconds; `tick` converts to the segment's natural clock via natural/slot. */
  function caption(seg) {
    if (!seg.caption && !seg.step) return null;
    const c = el("div", "cap");
    if (seg.step) c.appendChild(el("span", "step", esc(seg.step)));
    if (!seg.speech) {
      c.insertAdjacentHTML("beforeend", kinetic(seg.caption || ""));
      return { el: c, tick(lt) { popWords(c, lt, 0.05, 0.06); } };
    }
    const sp = seg.speech;
    const line = el("div", "sline");
    c.appendChild(line);
    // Pre-render the LONGEST cue: fit() measures this band before the first tick, and an
    // empty line reports one line high — the panel would then centre into where a two-line
    // cue is about to land. The first tick replaces this with the actual cue anyway.
    const longest = sp.cues.reduce((a, b) => (String(b[0]).length > String(a[0]).length ? b : a), sp.cues[0]);
    if (longest) {
      line.innerHTML = String(longest[0]).split(/\s+/).filter(Boolean)
        .map((w) => `<span class="sw">${esc(w)}</span>`).join(" ");
    }
    const durParam = parseFloat(P.get("dur"));
    let lastCue = -2, spans = [];
    return { el: c, tick(lt, natural) {
      if (!natural) return;
      // natural time → section (video) seconds: the segment is time-fitted to its slot,
      // so word times recorded against the slot must ride the same transform.
      const slot = isFinite(durParam) && durParam > 0 ? durParam : natural;
      const t = lt * (slot / natural);
      let i = -1;
      for (let k = 0; k < sp.cues.length; k++) if (sp.cues[k][1] <= t) i = k;
      if (i !== lastCue) {
        lastCue = i;
        if (i < 0) { line.innerHTML = ""; spans = []; }
        else {
          const cue = sp.cues[i];
          const words = String(cue[0]).split(/\s+/).filter(Boolean);
          line.innerHTML = words.map((w) => `<span class="sw">${esc(w)}</span>`).join(" ");
          spans = Array.from(line.querySelectorAll(".sw"));
          const next0 = sp.cues[i + 1] ? sp.cues[i + 1][1] : Infinity;
          const timed = sp.words.filter((w) => w[1] >= cue[1] - 1e-6 && w[1] < next0 - 1e-6);
          spans.forEach((s, k) => {
            // alignment words and displayed words should match; if they ever don't,
            // spread the cue evenly rather than desynchronising every following word
            s._t0 = timed[k] ? timed[k][1]
                 : (timed.length === spans.length ? timed[0][1] : cue[1]);
          });
          if (timed.length !== spans.length) {
            const span = (cue[2] - cue[1]) / Math.max(1, spans.length);
            spans.forEach((s, k) => { s._t0 = cue[1] + k * span; });
          }
        }
      }
      for (const s of spans) {
        const age = t - s._t0;
        const k = clamp(age / 0.18);
        s.style.opacity = age >= 0 ? String(clamp(k * 1.6)) : "0";
        s.style.transform = `translateY(${(1 - outCubic(k)) * 16}px) scale(${0.82 + 0.18 * outBack(k)})`;
        s.classList.toggle("live", age >= 0 && age < 0.75);
      }
    } };
  }

  // ── ANSI → cells (a small terminal: SGR colours, CR/LF, erase, cursor moves) ─
  const PAL = ["#1b1f27", "#ff5c57", "#5af78e", "#f3f99d", "#57c7ff", "#ff6ac1", "#22d3ee", "#e6edf3",
               "#6b7280", "#ff7b72", "#7ee787", "#ffe66d", "#79c0ff", "#ff9de2", "#67e8f9", "#ffffff"];
  function c256(n) {
    if (n < 16) return PAL[n];
    if (n >= 232) { const v = 8 + (n - 232) * 10; return `rgb(${v},${v},${v})`; }
    n -= 16; const s = [0, 95, 135, 175, 215, 255];
    return `rgb(${s[Math.floor(n / 36)]},${s[Math.floor(n / 6) % 6]},${s[n % 6]})`;
  }
  // Two cells, as a terminal counts them: East Asian Wide/Fullwidth, emoji whose DEFAULT
  // presentation is emoji (🎧), or any glyph forced to emoji by VS16. NOT every pictograph —
  // ⚠ ℹ ✓ are one cell, and counting them as two wrapped real lines a column early.
  const WIDE = /[ᄀ-ᅟ⺀-꓏가-힣豈-﫿︰-﹏＀-｠￠-￦️]|\p{Emoji_Presentation}/u;

  class Screen {
    constructor(cols) { this.cols = cols; this.reset(); }
    reset() { this.lines = [[]]; this.r = 0; this.c = 0; this.st = {}; this.key = ""; this.pending = ""; }
    styleKey() {
      const s = this.st; let css = "";
      let fg = s.fg, bg = s.bg;
      if (s.inv) { [fg, bg] = [bg || "#0b0d11", fg || "#e6edf3"]; }
      if (fg) css += `color:${fg};`;
      if (bg) css += `background:${bg};`;
      const cls = (s.b ? "b " : "") + (s.d ? "d " : "") + (s.i ? "i " : "") + (s.u ? "u" : "");
      return cls.trim() + "|" + css;
    }
    sgr(ps) {
      if (!ps.length) ps = [0];
      for (let i = 0; i < ps.length; i++) {
        const p = ps[i];
        if (p === 0) this.st = {};
        else if (p === 1) this.st.b = 1; else if (p === 2) this.st.d = 1; else if (p === 3) this.st.i = 1;
        else if (p === 4) this.st.u = 1; else if (p === 7) this.st.inv = 1;
        else if (p === 22) { delete this.st.b; delete this.st.d; } else if (p === 23) delete this.st.i;
        else if (p === 24) delete this.st.u; else if (p === 27) delete this.st.inv;
        else if (p >= 30 && p <= 37) this.st.fg = PAL[p - 30]; else if (p >= 90 && p <= 97) this.st.fg = PAL[p - 82];
        else if (p >= 40 && p <= 47) this.st.bg = PAL[p - 40]; else if (p >= 100 && p <= 107) this.st.bg = PAL[p - 92];
        else if (p === 39) delete this.st.fg; else if (p === 49) delete this.st.bg;
        else if (p === 38 || p === 48) {
          const k = p === 38 ? "fg" : "bg";
          if (ps[i + 1] === 5) { this.st[k] = c256(ps[i + 2] || 0); i += 2; }
          else if (ps[i + 1] === 2) { this.st[k] = `rgb(${ps[i + 2] || 0},${ps[i + 3] || 0},${ps[i + 4] || 0})`; i += 4; }
        }
      }
      this.key = this.styleKey();
    }
    line(r) { while (this.lines.length <= r) this.lines.push([]); return this.lines[r]; }
    put(ch) {
      const w = WIDE.test(ch) ? 2 : 1;
      if (this.c + w > this.cols) { this.r++; this.c = 0; }
      const L = this.line(this.r);
      while (L.length < this.c) L.push({ ch: " ", k: "" });
      L[this.c] = { ch, k: this.key };
      if (w === 2) L[this.c + 1] = { ch: "", k: this.key };
      this.c += w;
    }
    write(data) {
      const s = this.pending + data; this.pending = "";
      for (let i = 0; i < s.length; i++) {
        const ch = s[i];
        if (ch === "\x1b") {
          const rest = s.slice(i);
          let m = rest.match(/^\x1b\[([0-9;?]*)([@-~])/);
          if (m) { this.csi(m[1], m[2]); i += m[0].length - 1; continue; }
          m = rest.match(/^\x1b\][^\x07\x1b]*(\x07|\x1b\\)/);
          if (m) { i += m[0].length - 1; continue; }
          if (/^\x1b(\[[0-9;?]*|\][^\x07]*|\x1b?)$/.test(rest)) { this.pending = rest; return; } // split mid-sequence
          i += 1; continue; // ESC + one char (charset selects etc.)
        }
        if (ch === "\r") { this.c = 0; continue; }
        if (ch === "\n") { this.r++; this.c = 0; this.line(this.r); continue; }
        if (ch === "\b") { this.c = Math.max(0, this.c - 1); continue; }
        if (ch === "\t") { this.c = Math.min(this.cols - 1, (Math.floor(this.c / 8) + 1) * 8); continue; }
        if (ch < " ") continue;
        // surrogate pairs + variation selectors / ZWJ sequences stay one glyph
        let g = ch;
        const cp = s.codePointAt(i);
        if (cp > 0xffff) { g = String.fromCodePoint(cp); i++; }
        while (i + 1 < s.length && /[️‍⃣]/.test(s[i + 1])) { g += s[++i]; }
        this.put(g);
      }
    }
    csi(params, f) {
      const ps = params.replace("?", "").split(";").filter((x) => x !== "").map(Number);
      const n = ps[0] || 1;
      if (f === "m") this.sgr(ps);
      else if (f === "K") {
        const L = this.line(this.r);
        if ((ps[0] || 0) === 0) L.length = Math.min(L.length, this.c);
        else if (ps[0] === 2) L.length = 0;
      } else if (f === "J" && (ps[0] === 2 || ps[0] === 3)) { this.lines = [[]]; this.r = 0; this.c = 0; }
      else if (f === "G") this.c = clamp(n - 1, 0, this.cols - 1);
      else if (f === "A") this.r = Math.max(0, this.r - n);
      else if (f === "B") { this.r += n; this.line(this.r); }
      else if (f === "C") this.c = Math.min(this.cols - 1, this.c + n);
      else if (f === "D") this.c = Math.max(0, this.c - n);
      else if (f === "H" || f === "f") { this.r = Math.max(0, (ps[0] || 1) - 1); this.c = Math.max(0, (ps[1] || 1) - 1); this.line(this.r); }
    }
    text(r) { return (this.lines[r] || []).map((x) => x.ch).join(""); }
    html(r) {
      const L = this.lines[r] || []; let out = "", cur = null, buf = "";
      const flush = () => {
        if (!buf) return;
        if (!cur) { out += buf; }
        else { const [cls, css] = cur.split("|"); out += `<span${cls ? ` class="${cls}"` : ""}${css ? ` style="${css}"` : ""}>${buf}</span>`; }
        buf = "";
      };
      for (let x = 0; x < L.length; x++) {
        const cell = L[x];
        if (cell.k !== cur) { flush(); cur = cell.k; }
        if (cell.ch === "") continue; // the right half of a wide glyph
        // A glyph Geist Mono lacks (…, ✓, emoji) falls back to a font with other metrics and
        // would shift every column after it. Pin anything non-ASCII to its cell width.
        if (cell.ch.charCodeAt(0) > 0x7e) {
          const wide = L[x + 1] && L[x + 1].ch === "";
          const box = /[\u2500-\u257f]/.test(cell.ch);
          buf += `<span class="g${wide ? " w2" : ""}${box ? " bx" : ""}">${esc(cell.ch)}</span>`;
        } else buf += esc(cell.ch);
      }
      flush();
      return out;
    }
  }

  // ── segment builders: each returns {el, natural, render(lt)} ──────────────
  const builders = {
    hook(seg) {
      const n = el("div", "seg hook");
      n.innerHTML = `<div class="ring"></div><img class="art" src="${asset(seg.art || "kraken")}" alt="">
        <div class="title">${kinetic(seg.title)}</div>
        ${seg.sub ? `<div class="sub">${esc(seg.sub).replace(/\*\*(.+?)\*\*/g, "<b>$1</b>")}</div>` : ""}`;
      const art = n.querySelector(".art"), ring = n.querySelector(".ring"), title = n.querySelector(".title"), sub = n.querySelector(".sub");
      n.prepend(bug());
      const hr = hudR(); if (hr) n.appendChild(hr);
      const nw = title.querySelectorAll(".w").length;
      let fitted = false;
      return {
        el: n, natural: seg.secs || 2.4,
        render(lt) {
          if (!fitted) { fitSlam(title, 1400 - 240 - 60); fitted = true; }
          const k = outBack(lt / 0.6);
          art.style.transform = `translate(-50%,-50%) scale(${0.78 + 0.22 * k + Math.sin(lt * 2.2) * 0.012})`;
          art.style.opacity = clamp(lt / 0.25);
          ring.style.transform = `translate(-50%,-50%) scale(${0.6 + 0.4 * outCubic(lt / 0.8)}) rotate(${lt * 12}deg)`;
          ring.style.opacity = clamp(lt / 0.4) * (0.7 + 0.3 * Math.sin(lt * 4));
          popWords(title, lt, 0.12, 0.08);
          glitch(title, lt, 0.12 + nw * 0.08 + 0.3, 0.16, TITLE_SHADOW);
          if (sub) { sub.style.opacity = clamp((lt - 0.9) / 0.3); sub.style.transform = `translateY(${(1 - outCubic((lt - 0.9) / 0.4)) * 30}px)`; }
        },
      };
    },

    terminal(seg) {
      const cast = seg.cast || { header: { width: 44 }, events: [] };
      const cols = seg.cols || cast.header.width || 44;
      const command = seg.command != null ? seg.command : (cast.header.command || "");
      const speed = seg.speed || 1, idle = seg.idle != null ? seg.idle : 0.6;
      const typeDt = Math.min(0.045, 1.6 / Math.max(1, command.length));
      // timeline: think → type → enter → output (gaps compressed) → hold
      let t = 0.3;
      const typeStart = t; t += command.length * typeDt; const typeEnd = t; t += 0.22;
      const outStart = t; let prev = 0; const evs = [];
      // `pace` (lines/s): a table printed in one burst would jump straight to its last row,
      // so its lines are revealed at a readable rate instead. Same bytes, slower reveal —
      // the same kind of edit as compressing idle time.
      const pace = seg.pace || 0;
      let lastLine = -Infinity; // consecutive lines land at least 1/pace apart, across events
      for (const [et, data] of cast.events) {
        t += Math.min(Math.max(0, et - prev), idle) / speed; prev = et;
        if (!pace) { evs.push([t, data]); continue; }
        for (const ln of data.split(/(?<=\n)/)) {
          if (!/[^\s]/.test(ln.replace(/\x1b\[[0-9;?]*[@-~]/g, ""))) { evs.push([t, ln]); continue; } // control-only / blank: no beat
          t = Math.max(t, lastLine + 1 / pace); lastLine = t;
          evs.push([t, ln]);
          // `dwell`: a highlighted line holds the scroll a moment so it can be read
          if (seg.dwell && (seg.highlight || []).some((h) => ln.includes(h))) lastLine += seg.dwell;
        }
      }
      const outEnd = t;
      const natural = (seg.secs || 0) || (outEnd + (seg.hold != null ? seg.hold : 1.4));
      const hl = (seg.highlight || []).map(String);

      const n = el("div", "seg" + (seg.phone ? " phone" : ""));
      n.appendChild(bug());
      const hr = hudR(); if (hr) n.appendChild(hr);
      const cap = caption(seg); if (cap) n.appendChild(cap.el);
      const term = el("div", "term", `<div class="bar"><i></i><i></i><i></i><span>${esc(seg.title || "~ navig")}</span></div><div class="screen"><div class="rows"></div></div>`);
      n.appendChild(term);
      const rows = term.querySelector(".rows");
      const scr = new Screen(cols);
      let applied = 0, lastSig = "", appear = [];

      const screenEl = term.querySelector(".screen");
      function fit() { // largest mono size that fits `cols` across the panel (Geist Mono advance = 0.6em)
        const avail = rows.clientWidth || 870;
        const probe = el("span", null, "M".repeat(20)); probe.style.font = `400 100px ${getComputedStyle(rows).fontFamily}`; probe.style.position = "absolute"; probe.style.visibility = "hidden";
        document.body.appendChild(probe); const adv = probe.getBoundingClientRect().width / 20 / 100; probe.remove();
        const size = Math.min(seg.fontSize || 40, Math.floor((avail / (cols * (adv || 0.6))) * 10) / 10);
        rows.style.fontSize = size + "px";
        // Size the window to what the command will END UP printing, so a short output is not
        // a small block at the bottom of a tall empty panel.
        const probeScr = new Screen(cols); for (const [, d] of evs) probeScr.write(d);
        const lines = probeScr.lines.length + 2; // + the command line and the final prompt
        const lh = size * 1.25, maxH = term.offsetHeight;
        term.style.bottom = "auto";
        const h = Math.max(420, Math.min(maxH, Math.ceil(lines * lh + 64 + 52)));
        term.style.height = h + "px";
        // Centre in the band, but NEVER ride up under the caption: a 3-line transcript at
        // the old 66px reached y≈460 and the panel's own top edge (440) cut through it
        // (seen on E05). The caption's bottom wins; the 1310 floor still clears the
        // burned-subtitle band (bottom-anchored at 1519, up to two lines → top ≈1341 at
        // Fontsize 11) and TikTok's bottom UI — the old 1560 band let tall panels reach
        // into both — and past the stage's own 1520 safe limit.
        const cap = n.querySelector(".cap");
        const capBottom = cap ? Math.ceil(cap.getBoundingClientRect().bottom) : 0;
        const centre = Math.max(440, Math.round((440 + 1310 - h) / 2));
        const top = Math.max(440, capBottom + 24, centre);
        term.style.top = top + "px";
        // Pushed down by a tall caption, a tall panel's bottom edge slid under TikTok's caption
        // bar (E05 measured y=1530 > 1520 by `qa --layout`). Shrink it instead: the rows
        // already scroll to keep the newest line in view, so a shorter box loses nothing.
        if (top + h > 1500) term.style.height = Math.max(420, 1500 - top) + "px";
      }
      return {
        el: n, natural, fit,
        render(lt) {
          if (cap) cap.tick(lt, natural);
          const typed = command.slice(0, clamp(Math.floor((lt - typeStart) / typeDt), 0, command.length));
          let want = 0; while (want < evs.length && evs[want][0] <= lt) want++;
          if (want < applied) { scr.reset(); applied = 0; appear = []; }
          for (; applied < want; applied++) scr.write(evs[applied][1]);
          const done = lt >= outEnd;
          const cursorOn = Math.floor(lt * 2.4) % 2 === 0;
          const sig = typed.length + "|" + applied + "|" + (lt >= typeEnd) + "|" + cursorOn;
          if (sig !== lastSig) {
            lastSig = sig;
            const parts = [`<div class="row cmdrow"><span class="prompt">❯ </span><span class="cmd">${esc(typed)}</span>${lt < outStart && cursorOn ? '<span class="cursor"></span>' : ""}</div>`];
            const used = scr.lines.length - (scr.lines[scr.lines.length - 1].length ? 0 : 1);
            for (let r = 0; r < used; r++) {
              if (appear[r] == null) appear[r] = lt;
              const isHl = hl.length && hl.some((h) => scr.text(r).includes(h));
              parts.push(`<div class="row${isHl ? " hl" : ""}" data-r="${r}">${scr.html(r) || " "}</div>`);
            }
            if (done) parts.push(`<div class="row"><span class="prompt">❯ </span>${cursorOn ? '<span class="cursor"></span>' : ""}</div>`);
            rows.innerHTML = parts.join("");
            const over = rows.offsetHeight - (screenEl.clientHeight - 48);
            rows.style.transform = over > 0 ? `translateY(${-over}px)` : "";
          }
          // highlight flash: bright when the row lands, settling to a steady band
          rows.querySelectorAll(".row.hl").forEach((row) => {
            const age = lt - (appear[+row.dataset.r] || lt);
            row.style.background = `rgba(34,211,238,${0.13 + 0.32 * Math.exp(-age * 3)})`;
          });
          const z = seg.zoom;
          if (z) {
            const zk = outCubic((lt - (z.at != null ? z.at : outEnd)) / 0.45);
            term.style.transform = `scale(${1 + ((z.scale || 1.15) - 1) * zk})`;
          }
          term.style.opacity = clamp(lt / 0.2);
          term.style.translate = `0 ${(1 - outCubic(lt / 0.35)) * 60}px`;
        },
      };
    },

    telegram(seg) {
      const bot = seg.bot || {};
      const n = el("div", "seg" + (seg.phone !== false ? " phone" : ""));
      n.appendChild(bug());
      const hr = hudR(); if (hr) n.appendChild(hr);
      const cap = caption(seg); if (cap) n.appendChild(cap.el);
      const tg = el("div", "tg", `<div class="head"><span class="back">‹</span><img src="${asset(bot.avatar || "avatar")}" alt="">
        <div><div class="who">${esc(bot.name || "NAVIG")}</div><div class="st">${esc(bot.status || "bot")}</div></div></div>
        <div class="wall"><div class="msgs"></div></div>
        <div class="input"><span class="field">Message</span><span class="send">➤</span></div>`);
      n.appendChild(tg);
      const msgs = tg.querySelector(".msgs"), field = tg.querySelector(".field"), st = tg.querySelector(".st");
      // Authoring markdown (**x**, `x`) AND the bot's own Telegram HTML (<b>, <i>, <code>)
      // — real captured replies are pasted verbatim, never re-typed.
      const fmt = (s) => esc(s).replace(/\*\*(.+?)\*\*/g, "<b>$1</b>").replace(/`([^`]+)`/g, "<code>$1</code>")
        .replace(/&lt;(\/?)(b|i|code|s|u|a [^&]*?)&gt;/g, "<$1$2>")
        .replace(/(https?:\/\/[^\s<]+)/g, "<a>$1</a>");
      // compile the script into timed beats
      const beats = []; let t = 0.35;
      for (const step of seg.script || []) {
        if (step.me != null) {
          const typeS = Math.min(1.1, String(step.me).length * 0.03);
          beats.push({ kind: "typing-me", text: step.me, t0: t, t1: t + typeS }); t += typeS + 0.15;
          beats.push({ kind: "msg", side: "out", step, t0: t }); t += 0.45;
        } else if (step.bot != null) {
          const wait = step.typing != null ? step.typing : 0.8;
          beats.push({ kind: "typing-bot", t0: t, t1: t + wait }); t += wait;
          beats.push({ kind: "msg", side: "in", step, t0: t }); t += step.read != null ? step.read : Math.min(2.6, 0.6 + String(step.bot).length * 0.012);
        } else if (step.tap != null) {
          beats.push({ kind: "tap", label: step.tap, t0: t, t1: t + 0.55 }); t += 0.7;
        } else if (step.wait != null) { t += step.wait; }
      }
      const natural = (seg.secs || 0) || t + (seg.hold != null ? seg.hold : 1.2);
      let lastSig = "";
      return {
        el: n, natural,
        render(lt) {
          if (cap) cap.tick(lt, natural);
          const shown = beats.filter((b) => b.kind === "msg" && b.t0 <= lt);
          const typingBot = beats.find((b) => b.kind === "typing-bot" && b.t0 <= lt && lt < b.t1);
          const typingMe = beats.find((b) => b.kind === "typing-me" && b.t0 <= lt && lt < b.t1 + 0.15);
          const tap = beats.filter((b) => b.kind === "tap" && b.t0 <= lt && lt < b.t1).pop();
          const sig = shown.length + "|" + !!typingBot + "|" + (tap ? tap.label : "");
          if (sig !== lastSig) {
            lastSig = sig;
            msgs.innerHTML = shown.map((b, i) => {
              const s = b.step, txt = b.side === "out" ? s.me : s.bot;
              const img = s.image ? `<img class="media" src="${asset(s.image)}" alt="">` : "";
              let h = `<div class="m ${b.side}" data-i="${i}">${img}${fmt(txt)}<span class="meta">${esc(s.time || S.clock || "12:41")}</span></div>`;
              if (s.buttons) {
                h += `<div class="kb">${s.buttons.map((row) => `<div class="r">${row.map((k) => `<div class="k${tap && tap.label === k ? " tap" : ""}">${esc(k)}</div>`).join("")}</div>`).join("")}</div>`;
              }
              return h;
            }).join("") + (typingBot ? `<div class="typing"><i></i><i></i><i></i></div>` : "");
          }
          msgs.querySelectorAll(".m").forEach((m) => {
            const b = shown[+m.dataset.i]; const k = outBack((lt - b.t0) / 0.3);
            m.style.transform = `scale(${0.6 + 0.4 * k})`; m.style.opacity = clamp((lt - b.t0) / 0.12);
          });
          msgs.querySelectorAll(".typing i").forEach((d, i) => { d.style.opacity = 0.35 + 0.65 * (0.5 + 0.5 * Math.sin(lt * 9 - i * 0.9)); });
          st.textContent = typingBot ? "typing…" : (bot.status || "bot");
          if (typingMe) {
            const k = clamp((lt - typingMe.t0) / (typingMe.t1 - typingMe.t0));
            field.textContent = String(typingMe.text).slice(0, Math.ceil(k * String(typingMe.text).length)) || "Message";
            field.classList.toggle("typed", k > 0);
          } else { field.textContent = "Message"; field.classList.remove("typed"); }
          tg.style.opacity = clamp(lt / 0.2);
          tg.style.translate = `0 ${(1 - outCubic(lt / 0.35)) * 60}px`;
        },
      };
    },

    image(seg) {
      // A file the commands produced (a sealed SVG, a screenshot, an extracted icon), framed.
      const n = el("div", "seg imageseg");
      n.appendChild(bug());
      const hr = hudR(); if (hr) n.appendChild(hr);
      const cap = caption(seg); if (cap) n.appendChild(cap.el);
      const frame = el("div", "frame", `<img src="${asset(seg.image)}" alt="">${seg.label ? `<div class="label">${esc(seg.label)}</div>` : ""}<div class="sweep"></div>`);
      n.appendChild(frame);
      const sweep = frame.querySelector(".sweep");
      return {
        el: n, natural: seg.secs || 2.6,
        render(lt) {
          if (cap) cap.tick(lt, seg.secs || 2.6);
          const k = outBack(lt / 0.5);
          frame.style.transform = `translate(-50%,-50%) scale(${0.85 + 0.15 * k}) rotate(${(1 - outCubic(lt / 0.6)) * -4}deg)`;
          frame.style.opacity = clamp(lt / 0.2);
          sweep.style.transform = `translateY(${-110 + ((lt * 70) % 220)}%)`;
        },
      };
    },

    outro(seg) {
      const n = el("div", "seg outro");
      const cta = seg.cta || "pipx install navig";
      n.innerHTML = `<img class="mark" src="${asset(seg.art || "mark")}" alt="">
        <div class="word">NAVIG</div><div class="tag">${esc(seg.tag || "Inspect. Operate. Automate.")}</div>
        <div class="cta"><span class="p">$ </span><span class="c"></span></div>
        <div class="follow">${esc(seg.follow || "Follow for one command a day")}</div>`;
      const mark = n.querySelector(".mark"), word = n.querySelector(".word"), tag = n.querySelector(".tag"),
        c = n.querySelector(".c"), box = n.querySelector(".cta"), follow = n.querySelector(".follow");
      n.prepend(bug());
      const hr = hudR(); if (hr) n.appendChild(hr);
      return {
        el: n, natural: seg.secs || 3.2,
        render(lt) {
          const k = outBack(lt / 0.7);
          mark.style.transform = `translate(-50%,-50%) scale(${0.4 + 0.6 * k}) rotate(${(1 - outCubic(lt / 0.9)) * -180}deg)`;
          mark.style.opacity = clamp(lt / 0.2);
          word.style.opacity = clamp((lt - 0.35) / 0.3);
          word.style.letterSpacing = `${0.22 + (1 - outCubic((lt - 0.35) / 0.6)) * 0.3}em`;
          tag.style.opacity = clamp((lt - 0.7) / 0.3);
          box.style.opacity = clamp((lt - 0.9) / 0.2);
          c.textContent = cta.slice(0, clamp(Math.floor((lt - 1.0) / 0.035), 0, cta.length));
          glitch(box, lt, 0.92, 0.15, CTA_SHADOW);
          follow.style.opacity = clamp((lt - 1.9) / 0.3);
        },
      };
    },
  };

  // ── assemble ─────────────────────────────────────────────────────────────
  const only = P.has("seg") ? parseInt(P.get("seg"), 10) : null;
  const segDefs = (S.segments || []).map((s, i) => [s, i]).filter(([, i]) => only == null || i === only);
  if (!segDefs.length) { stage.textContent = `no segment ${only}`; return; }
  const segs = segDefs.map(([s]) => {
    const b = builders[s.type];
    if (!b) throw new Error(`unknown segment type ${s.type}`);
    const built = b(s); stage.appendChild(built.el);
    // `from`: start this far into the segment (a smash cut shows the RESULT, not a 10x
    // fast-forward of getting there). Clamped so at least a beat of it remains.
    const skip = Math.max(0, Math.min(s.from || 0, built.natural - 0.6));
    if (skip) {
      const inner = built.render;
      built.render = (lt) => inner(lt + skip);
      built.natural -= skip;
    }
    return built;
  });
  let at = 0; for (const s of segs) { s.start = at; at += s.natural; }
  const total = at;
  const slot = parseFloat(P.get("dur")) || null;
  // Never slow a segment down (that reads as lag); speed it up only when the slot is short.
  const rate = slot ? Math.max(1, total / Math.max(0.5, slot - HEADROOM)) : 1;
  if (P.get("guides") === "1") stage.appendChild(el("div", "guides", `<div class="t"></div><div class="r"></div><div class="bt"></div>`));

  function render(t) {
    const ct = t * rate;
    segs.forEach((s, i) => {
      const last = i === segs.length - 1;
      const lt = ct - s.start;
      const live = lt >= -XFADE && (last || lt < s.natural + XFADE);
      s.el.classList.toggle("on", live);
      if (!live) return;
      // the HUD square blinks on the GLOBAL clock, so it does not restart at each cut
      const sq = s.el.querySelector(".hud-r .sq");
      if (sq) sq.style.opacity = (t % 1) < 0.5 ? "1" : "0.3";
      const fadeIn = segs.length > 1 && i > 0 ? clamp((lt + XFADE) / XFADE) : 1;
      const fadeOut = !last ? clamp((s.natural + XFADE - lt) / XFADE) : 1;
      s.el.style.opacity = Math.min(fadeIn, fadeOut);
      s.render(clamp(lt, 0, last ? Infinity : s.natural));
    });
  }

  const fonts = document.fonts ? Promise.race([
    Promise.all([document.fonts.load('400 40px "Geist Mono"'), document.fonts.load('800 80px "Geist"')]).then(() => document.fonts.ready),
    new Promise((r) => setTimeout(r, 2500)), // offline: fall back, never hang the capture
  ]) : Promise.resolve();
  const ready = fonts.then(() => { segs.forEach((s) => s.fit && s.fit()); });
  // Where every load-bearing box ended up after fit(), in STAGE pixels (1080×1920) whatever
  // the viewport scale — `navig pipeline qa --layout` judges collisions and safe zones from
  // this before a single frame is recorded. Measured with the longest cue in the band.
  function layout() {
    const sr = stage.getBoundingClientRect();
    const k = sr.width ? 1080 / sr.width : 1;
    const box = (node) => {
      if (!node) return null;
      const r = node.getBoundingClientRect();
      if (!r.width && !r.height) return null;
      return { top: Math.round((r.top - sr.top) * k), bottom: Math.round((r.bottom - sr.top) * k),
               left: Math.round((r.left - sr.left) * k), right: Math.round((r.right - sr.left) * k) };
    };
    return segs.map((s, i) => ({
      index: segDefs[i][1], type: segDefs[i][0].type,
      caption: box(s.el.querySelector(".cap")),
      panel: box(s.el.querySelector(".term, .tg, .img, .frame")),
      title: box(s.el.querySelector(".hook .title, .title")),
    }));
  }
  window.__stage = { duration: total / rate, natural: total, rate, ready, layout: () => ready.then(layout),
                     seek: (t) => ready.then(() => render(t)) };
  ready.then(() => {
    document.body.dataset.ready = "1";
    if (P.has("t")) { render(parseFloat(P.get("t"))); return; }
    if (P.get("manual") === "1") { render(0); return; }
    const t0 = performance.now();
    const tick = () => { render((performance.now() - t0) / 1000); requestAnimationFrame(tick); };
    tick();
  });
})();
