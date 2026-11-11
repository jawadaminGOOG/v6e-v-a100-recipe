/* Replay player: plays recorded /api/state frames (trimmed by deck_charts.py) through a light copy of the demo's
   core-map renderer (g4_colo/demo/static/app.js buildCoreMap / updateCoreMap). No network: frames come from
   js/deck_data.js. Tile classes a0..a8 = free, system, vllm, sandbox, toolcall, analytics, buffer, agent, other. */
(function () {
  "use strict";
  const NS = { g4: { tag: "G4", short: "G" }, v6e: { tag: "v6e", short: "T" } };
  const SCEN = [["cpu-scale-out", "CPU spike"], ["gpu-scale-out", "Scale-out"], ["gpu-scale-in", "Scale-in"]];
  const num = (x, d = 0) => (x === null || x === undefined || !isFinite(x) ? "–" : Number(x).toFixed(d));
  const el = (tag, cls, html) => { const e = document.createElement(tag); if (cls) e.className = cls; if (html !== undefined) e.innerHTML = html; return e; };

  // Core map: one column per physical core of each L3 domain, one row per hyperthread (same layout as the demo).
  function buildCoreMap(cpus) {
    const root = el("div", "cmap");
    root._tiles = [];
    root._llcs = [];
    const byL = new Map();
    for (const [id, core, thread, llc] of cpus) {
      if (!byL.has(llc)) byL.set(llc, new Map());
      const m = byL.get(llc);
      if (!m.has(core)) m.set(core, []);
      m.get(core)[thread] = id;
    }
    for (const l of [...byL.keys()].sort((a, b) => a - b)) {
      const cores = [...byL.get(l).keys()].sort((a, b) => a - b);
      const nth = Math.max(...cores.map((k) => byL.get(l).get(k).length));
      const g = el("div", "llc", `<div class="llc-h">L3 ${l}</div>`);
      const grid = el("div", "tl");
      grid.style.setProperty("--cols", cores.length);
      for (let t = 0; t < nth; t++) for (const k of cores) {
        const id = byL.get(l).get(k)[t];
        const tile = el("i", "cpu a0", '<i class="f"></i>');
        grid.appendChild(tile);
        if (id !== undefined) root._tiles[id] = { el: tile, f: tile.firstChild, app: 0, pend: 0, busy: -1, llc: root._llcs.length };
      }
      g.appendChild(grid);
      root.appendChild(g);
      root._llcs.push(g);
    }
    root._key = cpus.map((c) => `${c[0]}:${c[1]}:${c[2]}:${c[3]}`).join(",");
    return root;
  }
  // Writes only what changed (class on app/pending change, transform when busy moves by ≥ 1 %), so a 60 fps
  // playback touches a handful of style properties per frame instead of every tile.
  function updateCoreMap(root, cpus, next, a) {
    const per = root._llcs.map(() => false);
    for (let j = 0; j < cpus.length; j++) {
      const [id, , , , app, busy0, pend] = cpus[j];
      const t = root._tiles[id];
      if (!t) continue;
      if (t.app !== app || t.pend !== pend) { t.el.className = `cpu a${app}${pend ? " pend" : ""}`; t.app = app; t.pend = pend; }
      const b1 = next && next[j] && next[j][0] === id ? next[j][5] : busy0;
      const busy = Math.round(busy0 + (b1 - busy0) * (a || 0));
      if (t.busy !== busy) { t.f.style.transform = `scaleY(${Math.max(0, Math.min(100, busy)) / 100})`; t.busy = busy; }
      if (app === 2) per[t.llc] = true;
    }
    root._llcs.forEach((g, i) => { if (g._v !== per[i]) { g.classList.toggle("has-vllm", per[i]); g._v = per[i]; } });
  }
  function flat(n) {
    const cells = [];
    for (const [app, cpu, pend] of n.p || []) for (let i = 0; i < cpu; i++) cells.push([app, pend]);
    while (cells.length < (n.n || 16)) cells.push([0, 0]);
    cells.length = n.n || 16;
    const cols = Math.ceil(cells.length / 2);
    return `<div class="flat" style="--cols:${cols}">${cells.map(([a, p]) => `<i class="cpu a${a}${p ? " pend" : ""}"><i class="f" style="transform:scaleY(${a ? 0.75 : 0})"></i></i>`).join("")}</div>`;
  }

  class Player {
    constructor(host, acc, data) {
      this.host = host; this.acc = acc; this.data = data || {}; this.ns = NS[acc];
      this.scen = SCEN.find(([k]) => this.data[k])?.[0] || SCEN[0][0];
      this.i = -1; this.pos = 0; this.playing = false; this.raf = 0; this.maps = new Map(); this.speed = "fit";
      this.build();
      this.load(this.scen);
    }
    build() {
      const h = this.host;
      h.innerHTML = "";
      const head = el("h3", "", `<span>Replay · ${this.acc === "v6e" ? "TPU v6e" : "G4"}</span>`);
      h.appendChild(head);
      const seg = el("div", "seg");
      seg.setAttribute("role", "group");
      seg.setAttribute("aria-label", "Replay scenario");
      for (const [k, label] of SCEN) {
        const b = el("button", "", label);
        b.type = "button";
        b.dataset.k = k;
        b.disabled = !this.data[k];
        b.addEventListener("click", () => { this.load(k); this.play(true); });
        seg.appendChild(b);
      }
      this.seg = seg;
      h.appendChild(seg);
      const ctl = el("div", "rp-ctl");
      this.btn = el("button", "", "");
      this.btn.type = "button";
      this.btn.addEventListener("click", () => (this.playing ? this.pause() : this.play()));
      this.range = el("input");
      this.range.type = "range"; this.range.min = 0; this.range.value = 0; this.range.step = "any";
      this.range.setAttribute("aria-label", "Replay position");
      this.range.addEventListener("input", () => { this.pause(); this.seek(+this.range.value); });
      this.tl = el("span", "t", "0 s");
      ctl.append(this.btn, this.range, this.tl);
      h.appendChild(ctl);
      const sp = el("div", "rp-speed");
      sp.setAttribute("role", "group");
      sp.setAttribute("aria-label", "Playback speed");
      for (const [k, label] of [["fit", "Fit ≈ 22 s"], ["1", "1×"], ["4", "4×"], ["8", "8×"]]) {
        const b = el("button", "", label);
        b.type = "button"; b.dataset.k = k;
        b.addEventListener("click", () => { this.speed = k; this.markSpeed(); });
        sp.appendChild(b);
      }
      this.sp = sp;
      h.appendChild(sp);
      this.markSpeed();
      this.k = el("div", "rp-k");
      h.appendChild(this.k);
      this.nodes = el("div", "rp-nodes");
      h.appendChild(this.nodes);
      this.note = el("div", "rp-note");
      h.appendChild(this.note);
      h.appendChild(el("div", "legend rp-leg", '<span><i style="background:var(--blue)"></i>vLLM</span><span><i style="background:var(--green)"></i>backfill</span><span><i style="background:var(--red)"></i>N4D overflow</span><span><i style="background:var(--yellow)"></i>buffer</span><span><i style="background:var(--warm)"></i>reserved</span>'));
      this.icon();
    }
    markSpeed() { for (const b of this.sp.children) b.setAttribute("aria-pressed", b.dataset.k === this.speed ? "true" : "false"); }
    rate() {
      // recorded (idle-trimmed) seconds per wall-clock second
      return this.speed === "fit" ? Math.max(1, this.dur / 22) : +this.speed;
    }
    keyFrame() {
      // static frame for screenshots / Slides: the busiest moment (most N4D pods and nodes)
      let best = 0, bv = -1;
      this.frames.forEach((f, i) => { const v = (f.k.of || 0) + 4 * (f.nodes || []).length + (f.k.vr || 0); if (v > bv) { bv = v; best = i; } });
      return best;
    }
    icon() {
      this.btn.innerHTML = this.playing
        ? '<svg width="16" height="16" viewBox="0 0 16 16" aria-hidden="true"><rect x="3" y="2" width="3.5" height="12" rx="1.2" fill="currentColor"/><rect x="9.5" y="2" width="3.5" height="12" rx="1.2" fill="currentColor"/></svg>'
        : '<svg width="16" height="16" viewBox="0 0 16 16" aria-hidden="true"><path d="M4 2.5v11a1 1 0 0 0 1.5.86l9-5.5a1 1 0 0 0 0-1.72l-9-5.5A1 1 0 0 0 4 2.5z" fill="currentColor"/></svg>';
      this.btn.setAttribute("aria-label", this.playing ? "Pause replay" : "Play replay");
    }
    load(k) {
      this.pause();
      this.scen = k;
      const r = this.data[k];
      for (const b of this.seg.children) b.setAttribute("aria-pressed", b.dataset.k === k ? "true" : "false");
      this.frames = (r && r.frames) || [];
      this.tcs = this.frames.map((f) => f.tc ?? f.t);
      this.dur = this.tcs.length ? this.tcs[this.tcs.length - 1] : 0;
      this.range.max = this.dur || 1;
      this.nodes.innerHTML = "";
      this.maps.clear();
      this._sig = this._k = this._note = this._tl = null;
      this.i = -1;
      if (document.documentElement.classList.contains("shot")) this.seek(this.tcs[this.keyFrame()] || 0);
      else this.seek(0);
    }
    // Playback position is continuous (recorded seconds). Discrete state (nodes, KPIs, note) changes only when the
    // frame index changes; busy % is interpolated between frames so motion stays smooth at any speed.
    seek(pos) {
      if (!this.frames.length) { this.note.textContent = "No recording yet."; return; }
      this.pos = Math.max(0, Math.min(this.dur, pos));
      let i = this.i < 0 ? 0 : this.i;
      while (i < this.tcs.length - 1 && this.tcs[i + 1] <= this.pos) i++;
      while (i > 0 && this.tcs[i] > this.pos) i--;
      const f = this.frames[i], g = this.frames[i + 1];
      const a = g ? (this.pos - this.tcs[i]) / Math.max(1e-6, this.tcs[i + 1] - this.tcs[i]) : 0;
      if (i !== this.i) { this.i = i; this.showDiscrete(f); }
      this.showKpis(f, g, a);
      this.renderNodes(f.nodes || [], g && g.nodes, a);
      this.range.value = this.pos;
      const tl = `${num(f.t + (g ? (g.t - f.t) * a : 0))} s`;
      if (tl !== this._tl) { this.tl.textContent = tl; this._tl = tl; }
    }
    showKpis(f, g, a) {
      // numbers glide between recorded samples; the DOM is written only when the text changes
      const k = f.k || {}, k2 = (g && g.k) || k;
      const lerp = (x, y) => (x == null ? x : y == null ? x : x + (y - x) * a);
      const kh = `<div><b>${num(lerp(k.p99, k2.p99))} ms</b><span>TTFT p99</span></div>` +
        `<div><b>${num(lerp(k.bf, k2.bf))} · ${num(lerp(k.of, k2.of))}</b><span>CPU pods ${this.ns.tag} · N4D</span></div>` +
        `<div><b>${num(k.vr)}/${num(k.vd)}</b><span>vLLM ready</span></div>`;
      if (kh !== this._k) { this.k.innerHTML = kh; this._k = kh; }
    }
    showDiscrete(f) {
      const note = f.note || "";
      if (note !== this._note) { this.note.textContent = note; this._note = note; }
    }
    renderNodes(nodes, nextNodes, a) {
      const acc = nodes.filter((n) => n.r === "g4"), n4d = nodes.filter((n) => n.r === "n4d");
      // rebuild when the node list changes shape
      // rebuild when the node list changes shape, including a node whose CPU topology starts reporting
      const sig = nodes.map((n) => n.r + (n.c ? `c${n.c.length}` : `n${n.n}`)).join("|");
      if (sig !== this._sig) {
        this._sig = sig;
        this.nodes.innerHTML = "";
        this.maps.clear();
        acc.forEach((n, i) => {
          const row = el("div", "rp-node", `<b>${this.ns.tag} #${i + 1}</b>`);
          const m = n.c ? buildCoreMap(n.c) : el("div");
          row.appendChild(m);
          this.nodes.appendChild(row);
          this.maps.set(`a${i}`, { row, m });
        });
        if (n4d.length) {
          const wrap = el("div", "rp-n4d n4d");
          n4d.forEach((n, i) => {
            const one = el("div", "one", `<b>N4D #${i + 1}</b><div></div>`);
            wrap.appendChild(one);
            this.maps.set(`n${i}`, { row: one, m: one.lastChild });
          });
          this.nodes.appendChild(wrap);
        }
      }
      acc.forEach((n, i) => {
        const x = this.maps.get(`a${i}`);
        if (!x) return;
        x.row.classList.toggle("off", !n.ok);
        const nx = nextNodes && nextNodes.filter((m) => m.r === "g4")[i];
        if (n.c && x.m._llcs) updateCoreMap(x.m, n.c, nx && nx.c, a);
        else { const h = flat(n); if (x.m._h !== h) { x.m.innerHTML = h; x.m._h = h; } }
      });
      n4d.forEach((n, i) => {
        const x = this.maps.get(`n${i}`);
        if (!x) return;
        x.row.classList.toggle("off", !n.ok);
        const nx = nextNodes && nextNodes.filter((m) => m.r === "n4d")[i];
        if (n.c) { if (!x.m._key) { x.m.innerHTML = ""; const cm = buildCoreMap(n.c); x.m.appendChild(cm); x.m._key = 1; x.m._cm = cm; } updateCoreMap(x.m._cm, n.c, nx && nx.c, a); }
        else { const h = flat(n); if (x.m._h !== h) { x.m.innerHTML = h; x.m._h = h; } }
      });
    }
    play(fromStart = false) {
      if (!this.frames.length || document.documentElement.classList.contains("shot")) return;
      if (fromStart || this.pos >= this.dur) this.seek(0);
      this.playing = true; this.icon();
      cancelAnimationFrame(this.raf);
      let last = performance.now();
      const step = (now) => {
        if (!this.playing) return;
        const dt = Math.min(0.1, (now - last) / 1000); // clamp: a background tab or a long task never jumps far
        last = now;
        try { this.seek(this.pos + dt * this.rate()); } catch (e) { console.error(e); this._sig = null; this.pos += dt * this.rate(); }
        if (this.pos >= this.dur) { this.pause(); return; }
        this.raf = requestAnimationFrame(step);
      };
      this.raf = requestAnimationFrame(step);
    }
    restart() { this.load(this.scen); this.play(true); }
    pause() { this.playing = false; cancelAnimationFrame(this.raf); this.icon(); }
  }
  window.DeckReplay = { Player };
})();
