/* G4 colo at scale demo - frontend. No build step; Chart.js from CDN. */
"use strict";

const POLL_MS = 2000;
const HISTORY_MS = 15 * 60 * 1000;
const COLORS = {
  accent: "#d4572a", integer: "#9a9a96", memory: "#4a78b8", ink: "#222222",
  muted: "#6b6a66", ok: "#3b8a5a", dark: "#555451", line: "#e4e2dd",
};
const PRESETS = {
  integer: { sandbox: 10, toolcall: 9, analytics: 0 },
  memory: { sandbox: 0, toolcall: 0, analytics: 16 },
  mixed: { sandbox: 10, toolcall: 9, analytics: 16 },
};
const SVCS = ["sandbox", "toolcall", "analytics"];

const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const fmt = (v, d = 0) => (v === null || v === undefined || Number.isNaN(v) ? "–" : Number(v).toFixed(d));
const hhmmss = (t) => new Date(t).toLocaleTimeString([], { hour12: false });
const shortNode = (n) => { const p = String(n).split("-"); return p.slice(-2).join("-"); };

const history = [];
let last = null;
let costAcc = 0;
let lastCostT = null;
const touched = {}; // control id -> last user interaction ms

/* ------------------------------------------------------------------ API */
async function post(path, body) {
  const r = await fetch(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body ?? {}) });
  const j = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(j.detail || r.statusText);
  return j;
}
function toast(msg, err = false) {
  const t = $("toast");
  t.textContent = msg; t.className = "toast" + (err ? " err" : ""); t.hidden = false;
  clearTimeout(toast._h); toast._h = setTimeout(() => (t.hidden = true), 3500);
}
async function act(path, body, label) {
  try { await post(path, body); toast(label); poll(); } catch (e) { toast(`${label}: ${e.message}`, true); }
}

/* ------------------------------------------------------------------ controls */
function debounce(fn, ms) { let h; return (...a) => { clearTimeout(h); h = setTimeout(() => fn(...a), ms); }; }

const sendRate = debounce((v) => act("/api/gpu-rate", { rate: v }, `GPU rate → ${v} rps`), 400);
$("rate").addEventListener("input", (e) => { touched.rate = Date.now(); $("rate-out").textContent = e.target.value; sendRate(+e.target.value); });

function demandValues() { return Object.fromEntries(SVCS.map((s) => [s, +$(`d-${s}`).value])); }
const sendDemand = debounce(() => { const d = demandValues(); act("/api/demand", d, `demand → ${SVCS.map((s) => `${s} ${d[s]}`).join(", ")}`); }, 400);
SVCS.forEach((s) => $(`d-${s}`).addEventListener("input", (e) => {
  touched.demand = Date.now(); $(`d-${s}-out`).textContent = e.target.value; sendDemand();
}));
document.querySelectorAll("[data-preset]").forEach((b) => b.addEventListener("click", () => {
  const p = PRESETS[b.dataset.preset];
  touched.demand = Date.now();
  SVCS.forEach((s) => { $(`d-${s}`).value = p[s]; $(`d-${s}-out`).textContent = p[s]; });
  act("/api/demand", p, `preset ${b.textContent}`);
}));
$("guard").addEventListener("change", (e) => { touched.guard = Date.now(); act("/api/guard", { enabled: e.target.checked }, `guard ${e.target.checked ? "on" : "off"}`); });
$("btn-baseline").addEventListener("click", () => act("/api/baseline", {}, "baseline measured"));
document.querySelectorAll("[data-scenario]").forEach((b) => b.addEventListener("click", () => act(`/api/scenario/${b.dataset.scenario}`, {}, `scenario ${b.dataset.scenario} started`)));
$("scen-cancel").addEventListener("click", () => act("/api/scenario/cancel", {}, "scenario cancelled"));
if ($("sb-cancel")) $("sb-cancel").addEventListener("click", () => act("/api/scenario/cancel", {}, "scenario cancelled"));

const fresh = (k) => touched[k] && Date.now() - touched[k] < 8000;

function syncControls(s) {
  const c = s.controller || {};
  if (!fresh("rate") && s.loadgen && s.loadgen.target_rps !== undefined) {
    $("rate").value = Math.round(s.loadgen.target_rps); $("rate-out").textContent = Math.round(s.loadgen.target_rps);
  }
  if (!fresh("demand") && c.demand) {
    SVCS.forEach((sv) => { if (c.demand[sv] !== undefined) { $(`d-${sv}`).value = c.demand[sv]; $(`d-${sv}-out`).textContent = c.demand[sv]; } });
  }
  if (!fresh("guard") && c.guard_enabled !== undefined && c.guard_enabled !== null) $("guard").checked = !!c.guard_enabled;
  $("guard-label").textContent = c.guard_enabled ? "Guard on: evicts backfill when p99 breaches" : "Guard off";
  $("baseline").textContent = c.baseline_ttft_p99_ms ? `${fmt(c.baseline_ttft_p99_ms)} ms` : "–";
  $("threshold").textContent = c.threshold_ms ? `${fmt(c.threshold_ms)} ms (${c.factor}×)` : "–";
}

/* ------------------------------------------------------------------ header + KPIs */
function renderHeader(s) {
  $("cluster-name").textContent = s.cluster.name;
  $("cluster-region").textContent = s.cluster.region;
  $("cluster-project").textContent = s.cluster.project;
  $("mode-badge").hidden = s.mode !== "mock";
  const c = s.cost;
  $("cost-hr").textContent = `$${c.per_hour.toFixed(2)}`;
  $("cost-split").textContent = `G4 ${s.counts.g4 || 0} × $${c.prices.g4_hr} · N4D ${s.counts.n4d || 0} × $${c.prices.n4d16_hr}`;
  $("reclaim-vcpu").textContent = fmt(c.reclaimed_vcpu);
  $("reclaim-val").textContent = `${fmt(c.reclaimed_mem_gib)} GiB · worth $${c.reclaimed_per_hour.toFixed(3)}/hr`;
  const now = Date.now();
  if (lastCostT) costAcc += c.per_hour * (now - lastCostT) / 3.6e6;
  lastCostT = now;
}
setInterval(() => { // smooth ticker between polls
  if (!last || !lastCostT) return;
  const v = costAcc + last.cost.per_hour * (Date.now() - lastCostT) / 3.6e6;
  $("cost-acc").textContent = `$${v.toFixed(4)}`;
}, 200);

function renderKpis(s) {
  const lg = s.loadgen || {}, c = s.controller || {};
  $("k-rps").textContent = fmt(lg.achieved_rps);
  $("k-target").textContent = fmt(lg.target_rps);
  $("k-p50").textContent = fmt(lg.ttft_p50_ms);
  $("k-p99").textContent = fmt(lg.ttft_p99_ms);
  $("k-p99").parentElement.classList.toggle("bad", !!(c.threshold_ms && lg.ttft_p99_ms > c.threshold_ms));
  $("k-vllm").textContent = `${s.vllm.ready ?? "–"}/${s.vllm.desired ?? "–"}`;
  $("k-hpa").textContent = s.vllm.hpa ? ` HPA ${s.vllm.hpa.min}-${s.vllm.hpa.max}, metric ${s.vllm.hpa.metric ?? "–"}` : "";
  $("k-g4").textContent = s.counts.g4 || 0;
  $("k-n4d").textContent = s.counts.n4d || 0;
  const sv = Object.values(s.services || {});
  $("k-bf").textContent = sv.reduce((a, x) => a + (x.backfill.ready || 0), 0);
  $("k-of").textContent = sv.reduce((a, x) => a + (x.overflow.ready || 0), 0);
}

/* ------------------------------------------------------------------ node map */
function tileHtml(cpu) {
  if (!cpu) return `<div class="tile na"></div>`;
  const o = cpu.owner;
  const busy = cpu.busy;
  const tip = `CPU ${cpu.id} (core ${cpu.core}, thread ${cpu.thread}, LLC ${cpu.llc})\n` +
    (o ? `${o.ns ?? ""}/${o.pod}${o.tier ? ` [${o.tier}]` : ""}` : "shared pool (system + burstable pods)") +
    `\nbusy ${busy ?? "?"}%`;
  if (cpu.cat === "shared") {
    return `<div class="tile shared" title="${esc(tip)}"><div class="fill" style="height:${busy ?? 0}%"></div></div>`;
  }
  if (cpu.cat === "buffer") return `<div class="tile buffer" title="${esc(tip)}"></div>`;
  const op = busy === null || busy === undefined ? 1 : 0.5 + 0.5 * Math.min(1, busy / 100);
  return `<div class="tile ${cpu.cat}" style="opacity:${op.toFixed(2)}" title="${esc(tip)}"></div>`;
}

function g4Body(n) {
  if (!n.cpus.length) return `<div class="empty">node agent not reporting yet${n.ready ? "" : " (node not Ready)"}</div>`;
  const half = n.ncpu / 2;
  const nllc = Math.ceil(half / 8);
  let html = `<div class="llcs" style="grid-template-columns:repeat(${nllc},1fr)">`;
  for (let l = 0; l < nllc; l++) {
    const cores = [...Array(8).keys()].map((i) => l * 8 + i).filter((c) => c < half);
    const cpusInL = cores.flatMap((c) => [n.cpus[c], n.cpus[c + half]]).filter(Boolean);
    const hot = cpusInL.some((c) => c.cat === "vllm");
    const mem = cpusInL.filter((c) => c.cat === "memory").length;
    let tiles = "";
    for (const t of [0, 1]) for (const c of cores) tiles += tileHtml(n.cpus[c + t * half]);
    html += `<div class="llc${hot ? " hot" : ""}"><div class="llc-title">LLC ${l}</div>` +
      `<div class="llc-sub">cores ${cores[0]}–${cores[cores.length - 1]}${hot ? " · vLLM" : ""}${mem ? ` · ${mem} mem-heavy CPUs` : ""}</div>` +
      `<div class="tiles">${tiles}</div></div>`;
  }
  return html + "</div>";
}

function podBlocks(n) {
  const unit = 24; // px per CPU
  let used = 0;
  let html = `<div class="pblocks">`;
  for (const p of n.pods) {
    const w = Math.max(n.role === "loadgen" ? 3 : 1, p.cpu || 1) * unit + 8;
    used += p.cpu || 0;
    const tip = `${p.ns}/${p.name}\n${p.cpu} CPU · ${p.mem_gib} GiB · ${p.phase}${p.ready ? " · Ready" : ""}`;
    const label = p.app === "colo-buffer" ? "buffer" : p.app;
    html += `<div class="pblock ${p.cat}${p.ready ? "" : " pending"}" style="width:${w}px" title="${esc(tip)}"><span class="pn">${esc(label)}</span>${p.tier ? esc(p.tier) : ""}</div>`;
  }
  if (n.role === "n4d") {
    const free = Math.floor(((n.alloc_cpu || 0) - used) / 2);
    for (let i = 0; i < free; i++) html += `<div class="pblock free" style="width:${2 * unit + 8}px"><span class="pn">free</span>2 CPU</div>`;
  }
  html += "</div>";
  if (n.cpus.length) {
    html += `<div class="cpubar" style="grid-template-columns:repeat(${n.cpus.length},1fr)" title="per-CPU busy %">` +
      n.cpus.map((c) => `<i style="background:linear-gradient(to top, ${c.cat === "shared" ? "#bdbbb6" : c.cat === "memory" ? COLORS.memory : c.cat === "integer" ? COLORS.integer : c.cat === "vllm" ? COLORS.accent : "#cfcdc8"} ${c.busy ?? 0}%, #efede9 ${c.busy ?? 0}%)"></i>`).join("") + "</div>";
  }
  return html;
}

function age(s) {
  if (s === null || s === undefined) return "";
  if (s < 120) return `${Math.round(s)}s`;
  if (s < 7200) return `${Math.round(s / 60)}m`;
  return `${Math.round(s / 3600)}h`;
}

function renderNodes(s) {
  const thr = s.controller.threshold_ms;
  const order = { g4: 0, n4d: 1, loadgen: 2, other: 3 };
  const nodes = s.nodes.filter((n) => n.role !== "other").sort((a, b) => (order[a.role] ?? 9) - (order[b.role] ?? 9) || String(a.zone).localeCompare(String(b.zone)) || (b.age_s ?? 0) - (a.age_s ?? 0));
  const html = nodes.map((n) => {
    const breach = thr && n.ttft_p99_ms && n.ttft_p99_ms > thr;
    const cnt = {};
    n.pods.forEach((p) => { const k = p.app === "colo-buffer" ? "buffer" : `${p.app}${p.tier ? "/" + p.tier : ""}`; cnt[k] = (cnt[k] || 0) + 1; });
    const head = `<div class="node-head"><span class="role">${esc(n.role === "g4" ? "G4 · " + (n.instance_type || "g4-standard-48") : n.role === "n4d" ? "N4D · " + (n.instance_type || "n4d-standard-16") : n.role)}</span>` +
      `<span class="chip zone">${esc(n.zone || "?")}</span>` +
      (n.ready ? "" : `<span class="chip state">provisioning</span>`) +
      n.blocks.map((b) => `<span class="chip block">blocked: ${esc(b)}</span>`).join("") +
      `<span class="name" title="${esc(n.name)}">${esc(shortNode(n.name))} · ${age(n.age_s)}</span>` +
      (n.role === "g4" ? `<span class="chip p99${breach ? " bad" : ""}">p99 ${fmt(n.ttft_p99_ms)} ms</span>` : "") + `</div>`;
    const body = n.role === "g4" ? g4Body(n) : podBlocks(n);
    const foot = n.role === "loadgen" ? "" : `<div class="node-foot">` +
      Object.entries(cnt).map(([k, v]) => `<span><b>${v}</b> ${esc(k)}</span>`).join("") +
      (n.mem_used_gib !== null && n.mem_used_gib !== undefined ? `<span>mem <b>${fmt(n.mem_used_gib)}</b>/${fmt(n.alloc_mem_gib)} GiB</span>` : "") +
      (n.role === "g4" && n.achieved_rps !== null && n.achieved_rps !== undefined ? `<span><b>${fmt(n.achieved_rps)}</b> rps</span>` : "") + `</div>`;
    return `<div class="node ${n.role}${n.ready ? "" : " notready"}${breach ? " breach" : ""}">${head}${body}${foot}</div>`;
  }).join("");
  $("nodes").innerHTML = html || `<div class="empty">no nodes</div>`;
}

/* ------------------------------------------------------------------ events + scenario */
function reasonClass(r) {
  return { TriggeredScaleUp: "up", ScaledUpGroup: "up", ScaleDown: "down", ScaleDownEmpty: "down", ColoGuardEvict: "guard", ColoGuardRelease: "release", Preempted: "pre", NotTriggerScaleUp: "no" }[r] || "";
}
function renderEvents(s) {
  const ev = s.events || [];
  $("events").innerHTML = ev.length ? ev.map((e) => `<li><span class="t">${e.time ? hhmmss(e.time) : ""}</span>` +
    `<span class="r ${reasonClass(e.reason)}">${esc(e.reason)}${e.count > 1 ? ` ×${e.count}` : ""}</span>` +
    `<span class="m">${esc(e.message)}<small>${esc(e.kind)} ${esc(e.namespace ? e.namespace + "/" : "")}${esc(e.object)} · ${esc(e.source || "")}</small></span></li>`).join("")
    : `<li class="empty">no matching events yet</li>`;
  $("errors").textContent = (s.errors || []).join("\n");
}
function renderScenario(sc) {
  if (!sc) return;
  document.querySelectorAll("[data-scenario]").forEach((b) => {
    b.classList.toggle("active", sc.name === b.dataset.scenario && sc.status === "running");
    b.disabled = sc.status === "running";
  });
  $("scen-status").textContent = sc.name ? `${sc.name}: ${sc.status}${sc.status === "running" ? ` (step ${sc.step_index + 1}/${sc.n_steps})` : ""}` : "idle";
  $("scen-cancel").hidden = sc.status !== "running";
  const log = $("scen-log");
  const atBottom = log.scrollTop + log.clientHeight >= log.scrollHeight - 4;
  log.innerHTML = (sc.log || []).map((e) => `<li class="${e.level}"><span class="t">+${Math.round(e.elapsed_s)}s</span>${esc(e.msg)}</li>`).join("");
  if (atBottom) log.scrollTop = log.scrollHeight;
  const sb = $("scen-banner");
  if (sb) {
    sb.hidden = !sc.name || sc.status === "idle";
    if (!sb.hidden) {
      $("sb-badge").textContent = sc.status.toUpperCase();
      $("sb-badge").className = `badge ${sc.status}`;
      $("sb-title").textContent = sc.title || sc.name;
      const idx = Math.max(0, sc.step_index) + 1;
      const tot = Math.max(1, sc.n_steps || 1);
      $("sb-step").textContent = `· Step ${idx} of ${tot}`;
      $("sb-elapsed").textContent = `${Math.round(sc.elapsed_s || 0)}s elapsed`;
      $("sb-cancel").hidden = sc.status !== "running";
      const pct = sc.status === "done" ? 100 : Math.min(98, Math.round((idx / tot) * 100));
      $("sb-fill").style.width = `${pct}%`;
      const lastLog = (sc.log || [])[sc.log.length - 1];
      const curNote = sc.current_step && sc.current_step.note ? sc.current_step.note : "";
      $("sb-now").textContent = lastLog ? `${lastLog.msg}${curNote && !lastLog.msg.includes(curNote) ? " — " + curNote : ""}` : curNote;
    }
  }
}

/* ------------------------------------------------------------------ charts */
Chart.defaults.font.family = getComputedStyle(document.documentElement).fontFamily;
Chart.defaults.font.size = 11;
Chart.defaults.color = COLORS.muted;
Chart.defaults.animation = false;
const baseOpts = (yTitle, extra = {}) => ({
  responsive: true, maintainAspectRatio: false, spanGaps: true,
  interaction: { mode: "index", intersect: false },
  elements: { point: { radius: 0 }, line: { borderWidth: 2, tension: 0.2 } },
  plugins: { legend: { position: "bottom", labels: { boxWidth: 12, boxHeight: 2 } } },
  scales: {
    x: { ticks: { maxTicksLimit: 8, maxRotation: 0 }, grid: { color: "#f0eee9" } },
    y: { beginAtZero: true, title: { display: true, text: yTitle }, grid: { color: "#f0eee9" }, ...extra },
  },
});
const ds = (label, color, o = {}) => ({ label, borderColor: color, backgroundColor: color, data: [], ...o });
const charts = {
  ttft: new Chart($("c-ttft"), { type: "line", options: baseOpts("ms"), data: { labels: [], datasets: [
    ds("TTFT p99", COLORS.accent), ds("TTFT p50", "#e9a184", { borderWidth: 1.5 }),
    ds("baseline", COLORS.dark, { borderDash: [5, 4], borderWidth: 1.5 }),
    ds("1.5× threshold", "#b0321b", { borderDash: [2, 3], borderWidth: 1.5 }),
  ] } }),
  rps: new Chart($("c-rps"), { type: "line", options: baseOpts("req/s"), data: { labels: [], datasets: [
    ds("achieved", COLORS.accent), ds("target", COLORS.dark, { borderDash: [5, 4], stepped: true, borderWidth: 1.5 }),
  ] } }),
  scale: new Chart($("c-scale"), { type: "line", options: baseOpts("count", { ticks: { stepSize: 1 } }), data: { labels: [], datasets: [
    ds("vLLM ready", COLORS.accent, { stepped: true }), ds("vLLM desired", "#e9a184", { stepped: true, borderDash: [4, 3] }),
    ds("G4 nodes", COLORS.ink, { stepped: true }), ds("N4D nodes", COLORS.memory, { stepped: true }),
  ] } }),
  svc: new Chart($("c-svc"), { type: "line", options: baseOpts("ready pods", { ticks: { stepSize: 2 } }), data: { labels: [], datasets: [
    ds("sandbox backfill", "#6f6e6a", { stepped: true }), ds("sandbox overflow", "#6f6e6a", { stepped: true, borderDash: [5, 4] }),
    ds("toolcall backfill", "#b3b1ac", { stepped: true }), ds("toolcall overflow", "#b3b1ac", { stepped: true, borderDash: [5, 4] }),
    ds("analytics backfill", COLORS.memory, { stepped: true }), ds("analytics overflow", COLORS.memory, { stepped: true, borderDash: [5, 4] }),
  ] } }),
};

function pushHistory(s) {
  const sv = s.services || {};
  history.push({
    t: s.ts * 1000,
    p99: s.loadgen.ttft_p99_ms, p50: s.loadgen.ttft_p50_ms,
    base: s.controller.baseline_ttft_p99_ms, thr: s.controller.threshold_ms,
    rps: s.loadgen.achieved_rps, target: s.loadgen.target_rps,
    vr: s.vllm.ready, vd: s.vllm.desired, g4: s.counts.g4 || 0, n4d: s.counts.n4d || 0,
    svc: SVCS.flatMap((k) => [sv[k]?.backfill.ready ?? null, sv[k]?.overflow.ready ?? null]),
  });
  const cut = Date.now() - HISTORY_MS;
  while (history.length && history[0].t < cut) history.shift();
}
function renderCharts() {
  const labels = history.map((h) => hhmmss(h.t));
  const set = (c, arrs) => { c.data.labels = labels; arrs.forEach((a, i) => (c.data.datasets[i].data = a)); c.update("none"); };
  set(charts.ttft, [history.map((h) => h.p99), history.map((h) => h.p50), history.map((h) => h.base), history.map((h) => h.thr)]);
  set(charts.rps, [history.map((h) => h.rps), history.map((h) => h.target)]);
  set(charts.scale, [history.map((h) => h.vr), history.map((h) => h.vd), history.map((h) => h.g4), history.map((h) => h.n4d)]);
  set(charts.svc, [0, 1, 2, 3, 4, 5].map((i) => history.map((h) => h.svc[i])));
}

/* ------------------------------------------------------------------ poll loop */
let inflight = false;
async function poll() {
  if (inflight) return;
  inflight = true;
  try {
    const r = await fetch("/api/state");
    const s = await r.json();
    if (!r.ok || s.error) throw new Error(s.error || r.statusText);
    last = s;
    $("conn").textContent = `updated ${hhmmss(s.ts * 1000)}`; $("conn").className = "conn ok";
    renderHeader(s); syncControls(s); renderKpis(s); renderNodes(s); renderEvents(s); renderScenario(s.scenario);
    pushHistory(s); renderCharts();
  } catch (e) {
    $("conn").textContent = `error: ${e.message}`; $("conn").className = "conn bad";
  } finally { inflight = false; }
}
poll();
setInterval(poll, POLL_MS);
