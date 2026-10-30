/* G4 colo demo frontend. No build step, no CDN: Chart.js is bundled in /static and optional. */
"use strict";

const POLL_MS = 2000;
const STALE_MS = 6000;
const HISTORY_MS = 15 * 60 * 1000;
const SVCS = ["sandbox", "toolcall", "analytics"];
const SVC_CLASS = { sandbox: "integer", toolcall: "integer", analytics: "memory-heavy" };
const PRESETS = {
  integer: { sandbox: 10, toolcall: 9, analytics: 0 },
  memory: { sandbox: 0, toolcall: 0, analytics: 16 },
  mixed: { sandbox: 10, toolcall: 9, analytics: 16 },
  light: { sandbox: 6, toolcall: 6, analytics: 2 },
};
const CSS = getComputedStyle(document.documentElement);
const C = (n) => CSS.getPropertyValue(n).trim();
const COL = { v: C("--v"), vSoft: C("--f-vllm"), i: C("--i"), m: C("--m"), bad: C("--bad"), muted: C("--muted"), faint: C("--faint"), line: C("--line"), n4d: C("--n4d"), ink: C("--ink") };
const TAG = { vllm: "V", sandbox: "S", toolcall: "T", analytics: "A", buffer: "B" };

const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const num = (v, d = 0) => (v === null || v === undefined || Number.isNaN(+v) ? "–" : Number(v).toFixed(d));
// Round half-up in cents first: toFixed(2) alone renders 0.305 as "0.30" (binary float).
const money = (v) => (v === null || v === undefined ? "–" : `$${(Math.round(Number(v) * 100 + 1e-6) / 100).toFixed(2)}`);
const hhmmss = (t) => new Date(t).toLocaleTimeString([], { hour12: false });
const plural = (n, one, many) => `${n} ${n === 1 ? one : many}`;

const history = [];
let last = null;
let lastGoodAt = 0;
let session = { auth: false, net: null, server: null };
const touched = {};
const evictedUntil = {};
const seenEvents = new Set();
let firstEvents = true;
let dismissedScenario = null;

/* ------------------------------------------------------------------ fetch with session detection */
class AuthError extends Error {}
async function fetchJSON(path, opts = {}) {
  let r;
  try {
    r = await fetch(path, { redirect: "manual", credentials: "same-origin", cache: "no-store", ...opts });
  } catch (e) {
    // IAP redirects to accounts.google.com are cross-origin; with redirect:"manual" they surface as
    // opaqueredirect below, so a TypeError here means the demo server itself is unreachable.
    throw Object.assign(new Error("Cannot reach the demo server"), { net: true });
  }
  if (r.type === "opaqueredirect" || r.redirected || r.status === 401 || r.status === 403) throw new AuthError("session");
  const ct = r.headers.get("content-type") || "";
  if (!ct.includes("application/json")) {
    if (ct.includes("text/html")) throw new AuthError("session"); // a login page instead of JSON
    throw new Error(`Unexpected response (${r.status})`);
  }
  const j = await r.json();
  if (!r.ok) throw Object.assign(new Error(j.detail || j.error || r.statusText), { status: r.status, body: j });
  return j;
}

function toast(msg, err = false) {
  const t = $("toast");
  t.textContent = msg; t.className = "toast" + (err ? " err" : ""); t.hidden = false;
  clearTimeout(toast._h); toast._h = setTimeout(() => (t.hidden = true), err ? 7000 : 3500);
}
async function act(path, body, label) {
  try {
    await fetchJSON(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body ?? {}) });
    toast(label); poll();
  } catch (e) {
    if (e instanceof AuthError) { session.auth = true; renderAlerts(); return; }
    toast(`${label} failed: ${e.message}`, true);
  }
}

/* ------------------------------------------------------------------ alerts + connection */
let alertsKey = "";
function renderAlerts() {
  const items = [];
  const age = lastGoodAt ? Date.now() - lastGoodAt : null;
  if (session.auth) {
    items.push({ sev: "error", title: "Session expired", detail: "Your sign-in to the demo ended, so the numbers stopped updating.", action: "reload" });
  } else if (session.net) {
    items.push({ sev: "error", title: "Demo server unreachable", detail: "Check the network; the page keeps retrying every 2 s.", action: "reload" });
  } else if (session.server) {
    items.push({ sev: "error", title: "The demo backend failed to read the cluster", detail: session.server });
  }
  if (!session.auth && age !== null && age > STALE_MS) {
    items.push({ sev: "warn", title: `Data is ${Math.round(age / 1000)} s old`, detail: `Last good update ${hhmmss(lastGoodAt)}.` });
  }
  for (const a of (last && !session.auth ? last.alerts || [] : [])) {
    items.push({ sev: a.severity === "error" ? "error" : "warn", title: a.title, detail: a.detail, raw: (a.raw || [])[0] });
  }
  // Re-render only on change so the assertive live region does not repeat itself every second.
  const key = JSON.stringify(items.map((i) => [i.sev, i.title, i.detail && i.detail.replace(/\d+ s old/, "")]));
  if (key === alertsKey) {
    const st = document.querySelector("[data-stale]");
    if (st && age !== null) st.textContent = `Data is ${Math.round(age / 1000)} s old`;
    return;
  }
  alertsKey = key;
  $("alerts").innerHTML = items.map((i) => `<div class="alert ${i.sev === "warn" ? "warn" : ""}">` +
    `<div class="grow"><b ${i.title.startsWith("Data is") ? "data-stale" : ""}>${esc(i.title)}</b><span class="detail">${esc(i.detail || "")}</span>` +
    (i.raw ? `<span class="raw">${esc(i.raw)}</span>` : "") + `</div>` +
    (i.action === "reload" ? `<button class="btn danger" type="button" onclick="location.reload()">Reload</button>` : "") + `</div>`).join("");
}

let connKey = "";
function renderConn() {
  const age = lastGoodAt ? Date.now() - lastGoodAt : null;
  let cls = "", text = "Connecting";
  if (session.auth) { cls = "bad"; text = "Session expired"; }
  else if (session.net) { cls = "bad"; text = "Offline"; }
  else if (session.server) { cls = "bad"; text = "Backend error"; }
  else if (age !== null && age > STALE_MS) { cls = "bad"; text = "Stale"; }
  else if (age !== null) { cls = "ok"; text = last && last.mode === "mock" ? "Mock data" : "Live"; }
  $("conn-pill").className = `pill status ${cls}`;
  if (connKey !== text) { $("conn").textContent = text; connKey = text; } // announce transitions only
  $("conn-age").textContent = age !== null ? `${Math.max(0, Math.round(age / 1000))} s ago` : "";
}
setInterval(() => { renderConn(); renderAlerts(); renderClock(); }, 1000);

/* ------------------------------------------------------------------ controls (left rail) */
function debounce(fn, ms) { let h; return (...a) => { clearTimeout(h); h = setTimeout(() => fn(...a), ms); }; }
const sendRate = debounce((v) => act("/api/gpu-rate", { rate: v }, `GPU rate set to ${v} rps`), 400);
$("rate").addEventListener("input", (e) => { touched.rate = Date.now(); $("rate-out").textContent = e.target.value; sendRate(+e.target.value); });
const demandValues = () => Object.fromEntries(SVCS.map((s) => [s, +$(`d-${s}`).value]));
const sendDemand = debounce(() => { const d = demandValues(); act("/api/demand", d, `Demand: ${SVCS.map((s) => `${s} ${d[s]}`).join(", ")}`); }, 400);
SVCS.forEach((s) => $(`d-${s}`).addEventListener("input", (e) => { touched.demand = Date.now(); $(`d-${s}-out`).textContent = e.target.value; markPresets(demandValues()); sendDemand(); }));
document.querySelectorAll("[data-preset]").forEach((b) => b.addEventListener("click", () => {
  const p = PRESETS[b.dataset.preset];
  touched.demand = Date.now();
  SVCS.forEach((s) => { $(`d-${s}`).value = p[s]; $(`d-${s}-out`).textContent = p[s]; });
  markPresets(p);
  act("/api/demand", p, `Preset ${b.textContent}: ${SVCS.map((s) => `${s} ${p[s]}`).join(", ")}`);
}));
function markPresets(d) {
  document.querySelectorAll("[data-preset]").forEach((b) => { const p = PRESETS[b.dataset.preset]; b.setAttribute("aria-pressed", String(SVCS.every((s) => p[s] === d[s]))); });
}
$("guard").addEventListener("change", (e) => { touched.guard = Date.now(); act("/api/guard", { enabled: e.target.checked }, `Latency guard ${e.target.checked ? "on" : "off"}`); });
$("btn-baseline").addEventListener("click", () => act("/api/baseline", {}, "Baseline measured"));
document.querySelectorAll("[data-scenario]").forEach((b) => b.addEventListener("click", () => startScenario(b.dataset.scenario)));
$("sb-cancel").addEventListener("click", () => {
  if (last && last.scenario && last.scenario.status !== "running") { dismissedScenario = last.scenario.started; $("scen-banner").hidden = true; return; }
  act("/api/scenario/cancel", {}, "Scenario stopped");
});
function startScenario(name) {
  if (last && last.scenario && last.scenario.status === "running") return;
  dismissedScenario = null;
  act(`/api/scenario/${name}`, {}, "Scenario started");
}

function setPresenter(on) {
  document.documentElement.classList.toggle("presenter", on);
  $("presenter").setAttribute("aria-pressed", String(on));
  $("presenter").textContent = on ? "Exit presenter" : "Presenter view";
  try { localStorage.setItem("colo-presenter", on ? "1" : "0"); } catch (_) { /* private mode */ }
  applyChartFonts(on);
}
// Chart.js draws text on canvas, so CSS rem scaling does not reach it; set sizes explicitly.
function applyChartFonts(on) {
  if (typeof Chart === "undefined") return;
  const size = on ? 16 : 12;
  try {
    Chart.defaults.font.size = size;
    for (const c of Object.values(charts)) {
      const o = c.options;
      o.plugins.legend.labels.font = { size };
      o.plugins.legend.labels.boxWidth = on ? 20 : 14;
      for (const ax of Object.values(o.scales || {})) {
        ax.ticks = { ...(ax.ticks || {}), font: { size } };
        if (ax.title) ax.title.font = { size };
      }
      c.resize();
      c.update("none");
    }
  } catch (e) { console.error("chart font update failed", e); }
}
$("presenter").addEventListener("click", () => setPresenter(!document.documentElement.classList.contains("presenter")));
document.addEventListener("keydown", (e) => {
  if (e.ctrlKey || e.metaKey || e.altKey || /INPUT|TEXTAREA|SELECT/.test(e.target.tagName)) return;
  const map = { 1: "gpu-scale-out", 2: "gpu-scale-in", 3: "cpu-scale-out", 4: "guard-integer-vs-memory" };
  if (map[e.key]) {
    const btn = document.querySelector(`[data-scenario="${map[e.key]}"]`);
    if (!btn || btn.disabled) return;
    const where = last && last.mode === "mock" ? "the simulator" : "the live cluster (it can add or remove nodes)";
    if (window.confirm(`Start scenario ${e.key}: ${btn.querySelector("b").textContent}?\nThis changes ${where}.`)) startScenario(map[e.key]);
  }
  else if (e.key === "p" || e.key === "P") setPresenter(!document.documentElement.classList.contains("presenter"));
});

const fresh = (k) => touched[k] && Date.now() - touched[k] < 8000;
function syncControls(s) {
  const c = s.controller || {};
  if (!fresh("rate") && s.loadgen && s.loadgen.target_rps !== undefined) { $("rate").value = Math.round(s.loadgen.target_rps); $("rate-out").textContent = Math.round(s.loadgen.target_rps); }
  if (!fresh("demand") && c.demand) SVCS.forEach((sv) => { if (c.demand[sv] !== undefined) { $(`d-${sv}`).value = c.demand[sv]; $(`d-${sv}-out`).textContent = c.demand[sv]; } });
  if (!fresh("demand")) markPresets(demandValues());
  if (!fresh("guard") && c.guard_enabled !== undefined && c.guard_enabled !== null) $("guard").checked = !!c.guard_enabled;
  $("guard-label").textContent = `Latency guard ${c.guard_enabled ? "on" : "off"}`;
  $("baseline-info").textContent = c.baseline_ttft_p99_ms ? `baseline ${num(c.baseline_ttft_p99_ms)} ms · threshold ${num(c.threshold_ms)} ms` : "no baseline yet";
  const running = s.scenario && s.scenario.status === "running";
  $("manual-fields").disabled = running;
  $("manual-lock").hidden = !running;
}

function svcTotals(s) {
  const out = {};
  for (const sv of SVCS) {
    const x = (s.services || {})[sv] || { backfill: {}, overflow: {} };
    out[sv] = { bf: x.backfill.ready || 0, of: x.overflow.ready || 0, bfWant: x.backfill.desired || 0, ofWant: x.overflow.desired || 0, demand: x.demand ?? ((s.controller || {}).demand || {})[sv] ?? 0 };
  }
  return out;
}
function renderRail(s) {
  const t = svcTotals(s);
  for (const sv of SVCS) {
    const sp = $(`sp-${sv}`);
    sp.children[0].style.flexGrow = t[sv].bf; sp.children[1].style.flexGrow = t[sv].of;
    $(`w-${sv}`).innerHTML = `<span><b>${t[sv].bf}</b> on G4</span><span><b>${t[sv].of}</b> on N4D</span>`;
  }
  const slots = ((s.story || {}).g4_slots_per_node || 0) * ((s.counts || {}).g4_ready || 0);
  const bf = SVCS.reduce((a, sv) => a + t[sv].bf, 0);
  const demand = SVCS.reduce((a, sv) => a + (+t[sv].demand || 0), 0);
  $("cap-text").innerHTML = slots ? `<b>${bf}</b> of <b>${slots}</b> G4 slots` : "no G4 slots yet";
  const bar = $("cap-bar");
  bar.style.transform = `scaleX(${slots ? Math.min(1, demand / slots) : 0})`;
  bar.classList.toggle("over", demand > slots);
  bar.parentElement.title = `demand ${demand} pods vs ${slots} G4 slots`;
}

/* ------------------------------------------------------------------ story strip */
function renderStory(s) {
  const st = s.story || {};
  const cf = (s.cost || {}).counterfactual || {};
  $("h-slice").textContent = num(st.gpu_slice_vcpu);
  const g4n = (s.counts || {}).g4_ready || 0, g4all = (s.counts || {}).g4 || 0;
  const starting = g4all - g4n;
  $("h-slice-note").textContent = `reserved for vLLM on each G4 node (${g4n} ready${starting > 0 ? `, ${starting} starting` : ""})`;
  $("h-reclaim").textContent = num(st.reclaimed_vcpu);
  $("h-reclaim-note").textContent = st.reclaimed_pct_of_spare !== null && st.reclaimed_pct_of_spare !== undefined
    ? `${num(st.reclaimed_pct_of_spare)}% of spare G4 cores · ${st.reclaimed_pods} CPU pods` : "no spare G4 cores yet";
  $("h-overflow").textContent = num(st.overflow_vcpu);
  const n4d = st.n4d_nodes || 0;
  $("h-overflow-note").textContent = `${num(st.overflow_pods)} CPU pods on ${n4d} N4D ${n4d === 1 ? "node" : "nodes"}${n4d && !st.overflow_pods ? " (warm buffer only)" : ""}`;
  $("h-saved").textContent = money(st.saved_per_hour);
  $("h-saved-note").textContent = cf.saved_nodes !== undefined
    ? `${cf.saved_nodes} fewer n4d-standard-16 · ${num(st.saved_pct)}% of the bill without colo` : "";

  const total = st.g4_alloc_vcpu || 0;
  const segs = [
    ["v", "GPU slice", st.gpu_slice_total_vcpu],
    ["i", "Reclaimed", st.reclaimed_vcpu],
    ["b", "Warm buffer", st.g4_buffer_vcpu],
    ["s", "System", st.g4_system_vcpu],
    ["idle", "Free", st.g4_idle_vcpu],
  ].filter(([, , v]) => v > 0.05);
  $("budget-total").textContent = total ? `${num(total)} allocatable vCPU on ${g4n} G4 ${g4n === 1 ? "node" : "nodes"}` : "no Ready G4 node";
  $("budget-bar").innerHTML = segs.map(([k, label, v]) => `<span class="seg-${k}" style="flex-grow:${v}" title="${esc(label)} ${num(v, 1)} vCPU">${v / (total || 1) > 0.07 ? `${esc(label)} ${num(v)}` : ""}</span>`).join("");
  $("budget-bar").setAttribute("aria-label", `G4 vCPU budget: ${segs.map(([, l, v]) => `${l} ${num(v)}`).join(", ")}`);
  $("budget-key").innerHTML = segs.map(([k, label, v]) => `<li><i class="seg-${k}" aria-hidden="true"></i>${esc(label)} <b>${num(v)}</b></li>`).join("");
}

/* ------------------------------------------------------------------ live metrics row */
function renderMetrics(s) {
  const lg = s.loadgen || {}, c = s.controller || {}, v = s.vllm || {}, hpa = v.hpa || {}, cn = s.counts || {};
  const ach = lg.achieved_rps, tgt = lg.target_rps;
  $("m-ach").textContent = num(ach);
  $("m-tgt").textContent = `${num(tgt)} rps`;
  const top = Math.max(tgt || 0, ach || 0, 1) * 1.1;
  $("m-gauge").style.transform = `scaleX(${(ach || 0) / top})`;
  $("m-gauge-t").style.left = `${((tgt || 0) / top) * 100}%`;
  $("m-p50").textContent = num(lg.ttft_p50_ms);
  $("m-p50s").textContent = lg.e2e_p50_ms ? `end-to-end p50 ${num(lg.e2e_p50_ms)} ms` : "median first token";
  const thr = c.threshold_ms, p99 = lg.ttft_p99_ms;
  const over = thr && p99 && p99 > thr;
  $("m-p99").textContent = num(p99);
  $("m-p99").classList.toggle("bad", !!over);
  $("m-thr").textContent = thr ? ` / ${num(thr)} ms` : " ms";
  $("m-p99s").textContent = c.baseline_ttft_p99_ms ? `baseline ${num(c.baseline_ttft_p99_ms)} ms · ${over ? "over threshold" : "within threshold"}` : "no baseline yet";
  $("m-rep").textContent = num(v.ready);
  $("m-rep-d").textContent = ` ready of ${num(v.desired)} desired`;
  $("m-rep-s").textContent = hpa.min !== undefined ? `HPA ${hpa.min}–${hpa.max} · 1 per G4 node` : "1 per G4 node";
  $("m-g4").textContent = num(cn.g4 || 0);
  $("m-n4d").textContent = num(cn.n4d || 0);
  const booting = (cn.g4 || 0) - (cn.g4_ready || 0) + (cn.n4d || 0) - (cn.n4d_ready || 0);
  $("m-nodes-s").textContent = `${money((s.cost || {}).per_hour)}/hr fleet${booting > 0 ? ` · ${booting} starting` : ""}`;
  const t = svcTotals(s);
  const bf = SVCS.reduce((a, sv) => a + t[sv].bf, 0), of = SVCS.reduce((a, sv) => a + t[sv].of, 0);
  const want = SVCS.reduce((a, sv) => a + t[sv].bfWant + t[sv].ofWant, 0);
  $("m-bf").textContent = bf; $("m-of").textContent = of;
  $("m-pods-s").textContent = `${Math.max(0, want - bf - of)} pending · demand ${SVCS.reduce((a, sv) => a + (+t[sv].demand || 0), 0)}`;
}

/* ------------------------------------------------------------------ nodes: L3-grouped core maps */
// Nodes are numbered oldest first so a new G4 node always appears as the next number.
function orderedNodes(s) {
  const order = { g4: 0, n4d: 1 };
  const nodes = (s.nodes || []).filter((n) => n.role in order).sort((a, b) => order[a.role] - order[b.role] || (b.age_s ?? 0) - (a.age_s ?? 0));
  const idx = { g4: 0, n4d: 0 };
  return nodes.map((n) => { idx[n.role] += 1; return { n, label: `${n.role === "g4" ? "G4" : "N4D"} #${idx[n.role]}`, short: `${n.role === "g4" ? "G" : "N"}${idx[n.role]}` }; });
}

// Shared-pool CPUs covering the kubelet reserve (ncpu - allocatable) plus daemonset requests are drawn
// grey as reserved, lowest core first, so the map agrees with the budget bar and "% of spare".
function cpuKinds(n) {
  const podReady = Object.fromEntries((n.pods || []).map((p) => [p.name, p.ready !== false]));
  const kinds = n.cpus.map((c) => {
    const o = c.owner;
    let app = "free";
    if (c.cat === "vllm") app = "vllm";
    else if (c.cat === "buffer") app = "buffer";
    else if (c.cat === "system") app = "system";
    else if (c.cat === "integer" || c.cat === "memory") app = o && SVCS.includes(o.app) ? o.app : (c.cat === "memory" ? "analytics" : "other");
    else if (o) app = "other";
    return { app, pending: !!(o && o.pod && podReady[o.pod] === false), label: o ? `${o.ns ? o.ns + "/" : ""}${o.pod}` : "" };
  });
  let reserved = Math.max(0, Math.round((n.ncpu || 0) - (n.alloc_cpu || n.ncpu || 0))) + Math.round(n.sys_cpu || 0);
  const shared = n.cpus.filter((c, i) => kinds[i].app === "free").sort((a, b) => a.core - b.core || a.thread - b.thread);
  for (const c of shared) { if (reserved <= 0) break; const i = n.cpus.indexOf(c); kinds[i].app = "system"; kinds[i].label = "reserved for kubelet / system"; reserved--; }
  return kinds;
}

function buildCoreMap(n) {
  const root = document.createElement("div");
  root.className = "cmap";
  const half = n.ncpu / 2, nllc = Math.ceil(half / 8);
  root._tiles = [];
  root._llcs = [];
  for (let l = 0; l < nllc; l++) {
    const cores = [...Array(8).keys()].map((i) => l * 8 + i).filter((c) => c < half);
    const g = document.createElement("div");
    g.className = "llc";
    g.innerHTML = `<div class="llc-h"><span class="n">L3 ${l}</span><span class="c">cores ${cores[0]}–${cores[cores.length - 1]}</span><span class="s"></span></div><div class="tiles"></div>`;
    const grid = g.lastChild;
    for (const t of [0, 1]) for (const c of cores) {
      const id = c + t * half;
      const el = document.createElement("i");
      el.className = "cpu a-free";
      el.innerHTML = `<i class="fill"></i><b class="lt"></b>`;
      grid.appendChild(el);
      root._tiles[id] = { el, fill: el.firstChild, tag: el.lastChild, app: "free", llc: l };
    }
    root.appendChild(g); root._llcs.push(g);
  }
  root.setAttribute("role", "img");
  return root;
}
function updateCoreMap(root, n, label) {
  const kinds = cpuKinds(n);
  const per = root._llcs.map(() => new Set());
  const cnt = {};
  n.cpus.forEach((c, i) => {
    const t = root._tiles[c.id]; if (!t) return;
    const k = kinds[i];
    if (t.app !== k.app) { t.el.className = `cpu a-${k.app}`; t.tag.textContent = TAG[k.app] || ""; t.app = k.app; }
    t.el.classList.toggle("pending", k.pending);
    const busy = Math.max(0, Math.min(100, Number(c.busy) || 0));
    t.fill.style.transform = `scaleY(${busy / 100})`;
    t.el.title = `CPU ${c.id} (core ${c.core}, thread ${c.thread}, L3 ${c.llc}): ${k.app === "free" ? "free (shared pool)" : k.app === "system" ? "reserved" : k.label || k.app}${k.pending ? " · starting" : ""} · busy ${c.busy ?? "?"}%`;
    if (!["free", "system"].includes(k.app)) per[t.llc].add(k.app);
    cnt[k.app] = (cnt[k.app] || 0) + 1;
  });
  root._llcs.forEach((g, i) => {
    const names = [...per[i]].map((a) => (a === "vllm" ? "vLLM" : a));
    g.querySelector(".s").textContent = names.length ? names.join(" · ") : "free";
    g.classList.toggle("has-vllm", per[i].has("vllm"));
    g.classList.toggle("has-mem", per[i].has("analytics"));
  });
  root.setAttribute("aria-label", `${label} per-vCPU map: ${Object.entries(cnt).map(([k, v]) => `${v} ${k === "system" ? "reserved" : k}`).join(", ")}`);
}

// Node agent not reporting yet: fall back to one flat cell per requested vCPU from the pod list.
function flatCells(n) {
  const cells = [];
  for (const p of n.pods || []) {
    const app = p.cat === "vllm" ? "vllm" : p.cat === "buffer" ? "buffer" : SVCS.includes(p.app) ? p.app : "system";
    for (let i = 0; i < Math.max(1, Math.round(p.cpu || 0)); i++) cells.push({ app, pending: !p.ready });
  }
  const cap = n.ncpu || Math.round(n.alloc_cpu || 0);
  let reserved = Math.max(0, Math.round((n.ncpu || 0) - (n.alloc_cpu || n.ncpu || 0))) + Math.round(n.sys_cpu || 0);
  while (reserved-- > 0 && cells.length < cap) cells.push({ app: "system", pending: false });
  while (cells.length < cap) cells.push({ app: "free", pending: false });
  const cols = Math.max(1, Math.ceil(cap / 2)); // two rows, like the hyperthread pairs of the core map
  return `<div class="flat" style="--cols:${cols}" role="img" aria-label="vCPU requests (node agent not reporting${n.ready ? "" : "; node starting"})">${cells.map((c) => `<i class="cpu a-${c.app}${c.pending ? " pending" : ""}"><i class="fill"></i><b class="lt">${TAG[c.app] || ""}</b></i>`).join("")}</div>`;
}

function trackEvictions(s) {
  for (const e of s.events || []) {
    const key = `${e.time}|${e.reason}|${e.object}`;
    if (seenEvents.has(key)) continue;
    seenEvents.add(key);
    if (firstEvents || e.reason !== "ColoGuardEvict") continue;
    for (const n of s.nodes) if (e.object === n.name || (e.message || "").includes(n.name)) evictedUntil[n.name] = Date.now() + 4000;
  }
  firstEvents = false;
}

const nodeEls = new Map();
function nodeRow(n) {
  const el = document.createElement("div");
  el.className = "node-row";
  el.innerHTML = `<div class="node-h"><span class="role ${n.role}"></span><span class="meta"></span><span class="chips"></span><span class="stats"></span></div><div class="map"></div>`;
  return el;
}
function renderNodes(s) {
  const thr = (s.controller || {}).threshold_ms;
  const slots = (s.story || {}).g4_slots_per_node;
  const list = orderedNodes(s);
  const seen = new Set();
  const g4Rows = [], n4dRows = [];
  for (const { n, label } of list) {
    seen.add(n.name);
    let el = nodeEls.get(n.name);
    if (!el) { el = nodeRow(n); nodeEls.set(n.name, el); }
    const breach = n.role === "g4" && thr && n.ttft_p99_ms && n.ttft_p99_ms > thr;
    el.className = `node-row ${n.role}${breach ? " breach" : ""}${evictedUntil[n.name] > Date.now() ? " evicted" : ""}`;
    el.querySelector(".role").textContent = label;
    const type = n.instance_type || (n.role === "g4" ? "g4-standard-48" : "n4d-standard-16");
    const meta = el.querySelector(".meta");
    meta.textContent = `${type} · ${n.zone || ""}`; meta.title = n.name;
    const chips = [];
    if (!n.ready) chips.push(`<span class="chip warn">starting up</span>`);
    else if (n.role === "g4" && !(n.pods || []).some((p) => p.cat === "vllm" && p.ready)) chips.push(`<span class="chip warn">vLLM starting</span>`);
    else chips.push(`<span class="chip">Ready</span>`);
    (n.blocks || []).forEach((b) => chips.push(`<span class="chip bad">guard: no ${esc(b)}</span>`));
    el.querySelector(".chips").innerHTML = chips.join("");
    const cpuPods = (n.pods || []).filter((p) => SVCS.includes(p.app));
    el.querySelector(".stats").innerHTML = n.role === "g4"
      ? `<span class="${breach ? "bad" : ""}">TTFT p99 <b>${n.ttft_p99_ms != null ? `${num(n.ttft_p99_ms)} ms` : "–"}</b>${breach ? " · over threshold" : ""}</span><span>p50 <b>${n.ttft_p50_ms != null ? `${num(n.ttft_p50_ms)} ms` : "–"}</b></span><span><b>${n.achieved_rps ? num(n.achieved_rps) : "–"}</b> rps</span><span><b>${cpuPods.length}</b>${slots ? ` / ${slots}` : ""} CPU pods</span>`
      : `<span><b>${cpuPods.length}</b> overflow pods</span>`;
    const host = el.querySelector(".map");
    if (n.cpus && n.cpus.length) {
      if (!host._map || host._ncpu !== n.cpus.length) { host.innerHTML = ""; host._map = buildCoreMap(n); host._ncpu = n.cpus.length; host.appendChild(host._map); }
      updateCoreMap(host._map, n, label);
    } else {
      host._map = null; host._ncpu = 0;
      host.innerHTML = flatCells(n);
    }
    (n.role === "g4" ? g4Rows : n4dRows).push(el);
  }
  for (const k of [...nodeEls.keys()]) if (!seen.has(k)) nodeEls.delete(k);
  const root = $("nodes");
  const want = [...g4Rows];
  const hpa = (s.vllm || {}).hpa || {};
  if (g4Rows.length && hpa.max && g4Rows.length < hpa.max) {
    let gh = root.querySelector(".ghost") || Object.assign(document.createElement("div"), { className: "ghost" });
    gh.innerHTML = `<b>G4 #${g4Rows.length + 1}</b>joins when vLLM scales out (HPA max ${hpa.max}). vLLM takes its GPU slice; CPU pods fill the spare cores.`;
    want.push(gh);
  }
  if (n4dRows.length) {
    let grp = root.querySelector(".n4d-group") || Object.assign(document.createElement("div"), { className: "n4d-group" });
    let wrap = grp.querySelector(".n4d-wrap");
    if (!wrap) { wrap = document.createElement("div"); wrap.className = "n4d-wrap"; grp.appendChild(wrap); }
    n4dRows.forEach((el, i) => { if (wrap.children[i] !== el) wrap.insertBefore(el, wrap.children[i] || null); });
    [...wrap.children].slice(n4dRows.length).forEach((x) => x.remove());
    want.push(grp);
  }
  if (!want.length) { root.innerHTML = `<p class="empty">No G4 or N4D nodes reported yet.</p>`; return; }
  root.querySelector(".empty")?.remove();
  want.forEach((el, i) => { if (root.children[i] !== el) root.insertBefore(el, root.children[i] || null); });
  [...root.children].slice(want.length).forEach((x) => x.remove());
}

/* ------------------------------------------------------------------ service lanes */
// One lane per service. Chips are keyed by position, so when demand spills or the guard evicts,
// a chip visibly slides from the G4 zone to the N4D zone (FLIP animation).
const lanes = {};
(function initLanes() {
  const host = $("lanes");
  for (const sv of SVCS) {
    host.insertAdjacentHTML("beforeend", `<span class="lane-n a-${sv}"><i class="sw" aria-hidden="true"><b>${TAG[sv]}</b></i>${sv}</span><div class="zone g" id="zg-${sv}" aria-label="${sv} on G4"></div><div class="zone n" id="zn-${sv}" aria-label="${sv} on N4D"></div>`);
    lanes[sv] = [];
  }
})();
function renderLanes(s) {
  const list = orderedNodes(s);
  const reduce = matchMedia("(prefers-reduced-motion: reduce)").matches;
  const first = new Map();
  if (!reduce) for (const sv of SVCS) for (const c of lanes[sv]) if (c.isConnected) first.set(c, c.getBoundingClientRect());
  let gTot = 0, nTot = 0, pend = 0;
  for (const sv of SVCS) {
    const svc = (s.services || {})[sv] || { backfill: {}, overflow: {} };
    const g = [], nn = [];
    for (const { n, short } of list) for (const p of n.pods || []) {
      if (p.app !== sv) continue;
      const item = { short, ready: p.ready !== false, name: p.name, tier: p.tier };
      if (p.tier === "overflow" || n.role === "n4d") nn.push(item); else g.push(item);
    }
    const pg = Math.max(0, (svc.backfill.desired || 0) - g.length), pn = Math.max(0, (svc.overflow.desired || 0) - nn.length);
    const slotsList = [...g.map((x) => ["g", x]), ...Array(pg).fill(["g", null]), ...nn.map((x) => ["n", x]), ...Array(pn).fill(["n", null])];
    gTot += g.length; nTot += nn.length; pend += pg + pn;
    const L = lanes[sv];
    while (L.length < slotsList.length) { const c = document.createElement("span"); L.push(c); }
    while (L.length > slotsList.length) L.pop().remove();
    const zg = $(`zg-${sv}`), zn = $(`zn-${sv}`);
    zg.querySelector(".none")?.remove(); zn.querySelector(".none")?.remove();
    L.forEach((c, i) => {
      const [z, p] = slotsList[i];
      c.className = `pod a-${sv}${p ? "" : " pending"}`;
      c.textContent = p ? p.short : "…";
      c.title = p ? `${p.name} · ${p.tier || "pod"} on ${p.short}${p.ready ? "" : " · starting"}` : "pending: waiting for a node";
      if (p && !p.ready) c.classList.add("pending");
      (z === "g" ? zg : zn).appendChild(c);
    });
    if (!zg.querySelector(".pod")) zg.insertAdjacentHTML("beforeend", `<span class="none">none</span>`);
    if (!zn.querySelector(".pod")) zn.insertAdjacentHTML("beforeend", `<span class="none">none</span>`);
  }
  const slots = ((s.story || {}).g4_slots_per_node || 0) * ((s.counts || {}).g4_ready || 0);
  $("zh-g").textContent = slots ? `${gTot} of ${slots} slots` : `${gTot} pods`;
  $("zh-n").textContent = `${plural(nTot, "pod", "pods")}${pend ? ` · ${pend} pending` : ""}`;
  first.forEach((r, c) => {
    if (!c.isConnected) return;
    const b = c.getBoundingClientRect(), dx = r.left - b.left, dy = r.top - b.top;
    if (Math.abs(dx) + Math.abs(dy) > 2) c.animate([{ transform: `translate(${dx}px, ${dy}px)` }, { transform: "none" }], { duration: 750, easing: "cubic-bezier(.16,1,.3,1)" });
  });
}

/* ------------------------------------------------------------------ scenario banner */
function renderScenario(sc) {
  if (!sc) return;
  const running = sc.status === "running";
  document.querySelectorAll("[data-scenario]").forEach((b) => {
    b.classList.toggle("active", sc.name === b.dataset.scenario && running);
    b.disabled = running;
  });
  const log = $("scen-log");
  log.innerHTML = (sc.log || []).map((e) => `<li class="${esc(e.level)}"><span class="t">+${Math.round(e.elapsed_s)}s</span><span>${esc(e.msg)}</span></li>`).join("") || `<li class="empty">No scenario run yet.</li>`;
  const sb = $("scen-banner");
  sb.hidden = !sc.name || sc.status === "idle" || dismissedScenario === sc.started;
  if (sb.hidden) return;
  sb.className = `banner ${sc.status}`;
  $("sb-badge").textContent = { running: "Running", done: "Done", failed: "Failed", cancelled: "Stopped" }[sc.status] || sc.status;
  $("sb-badge").className = `tag ${sc.status}`;
  $("sb-title").textContent = sc.title || sc.name;
  const idx = Math.max(0, sc.step_index) + 1, tot = Math.max(1, sc.n_steps || 1);
  $("sb-step").textContent = `Step ${Math.min(idx, tot)} of ${tot}`;
  $("sb-cancel").textContent = running ? "Stop scenario" : "Close";
  $("sb-fill").style.transform = `scaleX(${sc.status === "done" ? 1 : Math.min(0.98, (idx - (running ? 1 : 0)) / tot)})`;
  if (sc.status === "failed") $("sb-now").textContent = sc.error || "Scenario failed.";
  else if (sc.status === "done") $("sb-now").textContent = "Scenario finished.";
  else if (sc.status === "cancelled") $("sb-now").textContent = "Scenario stopped. Settings stay where the scenario left them.";
  else $("sb-now").textContent = sc.current_note || (sc.current_step && sc.current_step.note) || "";
  const w = running ? sc.wait : null;
  const OPW = { ">=": "at least", "<=": "at most", "==": "exactly", ">": "more than", "<": "fewer than", "!=": "not" };
  $("sb-conds").innerHTML = w && w.conds ? w.conds.map((c) => `<li class="${c.ok ? "ok" : ""}">${esc(c.label)} <b>${esc(fmtVal(c.value))}</b> · target ${OPW[c.op] || c.op} ${esc(fmtVal(c.target))}${c.ok ? " · met" : ""}</li>`).join("") : "";
  renderClock();
}
function fmtVal(v) { return v === true ? "yes" : v === false ? "no" : v === null || v === undefined ? "–" : typeof v === "number" ? (Number.isInteger(v) ? v : v.toFixed(1)) : v; }
function renderClock() {
  const sc = last && last.scenario;
  if (!sc || $("scen-banner").hidden) return;
  const elapsed = sc.status === "running" ? (Date.now() / 1000 - sc.started) : sc.elapsed_s;
  let txt = `${Math.round(elapsed)} s elapsed`;
  const w = sc.status === "running" ? sc.wait : null;
  if (w) {
    const left = Math.max(0, Math.round(w.deadline - Date.now() / 1000));
    txt = w.kind === "wait" ? `${left} s left in this hold · ${txt}` : `times out in ${Math.floor(left / 60)}:${String(left % 60).padStart(2, "0")} · ${txt}`;
  }
  $("sb-clock").textContent = txt;
}

/* ------------------------------------------------------------------ events + method */
function renderEvents(s) {
  const ev = s.events || [];
  $("events").innerHTML = ev.length ? ev.map((e) => `<li><span class="t">${e.time ? hhmmss(e.time) : ""}</span><span><span class="r ${e.reason === "ColoGuardEvict" ? "guard" : ""}">${esc(e.reason)}${e.count > 1 ? ` ×${e.count}` : ""}</span> ${esc(e.message)}<small>${esc(e.kind)} ${esc(e.namespace ? e.namespace + "/" : "")}${esc(e.object)}</small></span></li>`).join("")
    : `<li class="empty">No autoscaler, guard or preemption events yet.</li>`;
}
function renderMethod(s) {
  const c = s.cost || {}, cf = c.counterfactual || {}, p = c.prices || {};
  $("method").innerHTML = [
    `<b>Saved vs CPU on N4D</b>: without colo, every CPU pod running now (${cf.pods ?? "–"} pods: G4 backfill plus N4D overflow; ${num(cf.vcpu, 1)} vCPU, ${num(cf.mem_gib, 1)} GiB requested) would run on N4D. Packed first-fit-decreasing onto n4d-standard-16 nodes (${num(cf.node_cpu, 2)} vCPU, ${num(cf.node_mem_gib, 1)} GiB usable each after daemonsets) they need ${cf.n4d_nodes ?? "–"} nodes. ${cf.n4d_nodes_now ?? "–"} N4D nodes run today, so colo saves ${cf.saved_nodes ?? "–"} × ${money(p.n4d16_hr)}/hr = ${money(cf.saved_per_hour)}/hr. Free room on running N4D nodes is counted, and the warm buffer is treated as free.`,
    `<b>Fleet now</b>: ${(s.counts || {}).g4 || 0} G4 × ${money(p.g4_hr)} + ${(s.counts || {}).n4d || 0} N4D × ${money(p.n4d16_hr)} = ${money(c.per_hour)}/hr. Without colo: same G4 + ${cf.n4d_nodes ?? "–"} N4D = ${money(cf.fleet_per_hour)}/hr.`,
    `<b>Reclaimed</b>: CPU requests of Running backfill pods on Ready G4 nodes. <b>Spare</b>: G4 allocatable minus vLLM and system-pod requests.`,
    `<b>Core map</b>: one tile per vCPU from the node agent; columns are the 8 physical cores of an L3 domain, rows the two hyperthreads. Fill height is the CPU's busy %. Grey tiles are the kubelet reserve plus daemonset requests.`,
    `<b>Prices</b> are list unit prices set in the demo config (DEMO_PRICE_*), not a quote.`,
  ].map((x) => `<li>${x}</li>`).join("");
  $("cluster").textContent = `${s.cluster.name} · ${s.cluster.region} · ${s.cluster.project}`;
}

/* ------------------------------------------------------------------ charts (optional) */
const charts = {};
function initCharts() {
  if (typeof Chart === "undefined") {
    document.querySelectorAll(".chart-box").forEach((b) => (b.innerHTML = `<p class="chart-fallback">Charts unavailable (chart library did not load). Live numbers above are unaffected.</p>`));
    return;
  }
  try {
    Chart.defaults.font.family = CSS.fontFamily;
    Chart.defaults.font.size = 12;
    Chart.defaults.color = COL.muted;
    Chart.defaults.animation = false;
    const opts = (y, extra = {}) => ({
      responsive: true, maintainAspectRatio: false, spanGaps: true,
      interaction: { mode: "index", intersect: false },
      elements: { point: { radius: 0 }, line: { borderWidth: 2, tension: 0.2 } },
      plugins: { legend: { position: "bottom", labels: { boxWidth: 14, boxHeight: 2 } } },
      scales: { x: { ticks: { maxTicksLimit: 5, maxRotation: 0 }, grid: { display: false } },
        y: { beginAtZero: true, title: { display: !!y, text: y }, grid: { color: COL.line }, ...extra } },
    });
    const ds = (label, color, o = {}) => ({ label, borderColor: color, backgroundColor: color, data: [], ...o });
    const mk = (id, y, sets, extra) => { const el = $(id); if (el) charts[id] = new Chart(el, { type: "line", options: opts(y, extra), data: { labels: [], datasets: sets } }); };
    mk("c-ttft", "ms", [ds("TTFT p99", COL.v), ds("TTFT p50", COL.vSoft), ds("guard threshold", COL.bad, { borderDash: [6, 4], borderWidth: 1.5 }), ds("baseline", COL.faint, { borderDash: [2, 3], borderWidth: 1.5 })]);
    mk("c-rps", "req/s", [ds("achieved", COL.i, { fill: { target: "origin" }, backgroundColor: "rgba(15,107,99,.08)" }), ds("target", COL.faint, { borderDash: [5, 4], stepped: true, borderWidth: 1.5 })]);
    mk("c-scale", "count", [ds("vLLM ready", COL.v, { stepped: true }), ds("G4 nodes", COL.ink, { stepped: true, borderDash: [5, 4] }), ds("N4D nodes", COL.n4d, { stepped: true })], { ticks: { stepSize: 1 } });
    mk("c-svc", "pods", [ds("on G4 (reclaimed)", COL.i, { stepped: true }), ds("on N4D (overflow)", COL.n4d, { stepped: true, borderDash: [5, 4] })]);
    mk("c-cost", "$/hr", [ds("with colo", COL.v, { stepped: true }), ds("without colo", COL.faint, { stepped: true, borderDash: [5, 4] })]);
  } catch (e) {
    console.error("chart init failed", e);
  }
}
function pushHistory(s) {
  const sv = Object.values(s.services || {});
  history.push({
    t: s.ts * 1000, p99: s.loadgen.ttft_p99_ms, p50: s.loadgen.ttft_p50_ms, base: s.controller.baseline_ttft_p99_ms, thr: s.controller.threshold_ms,
    rps: s.loadgen.achieved_rps, target: s.loadgen.target_rps, vr: s.vllm.ready, g4: s.counts.g4 || 0, n4d: s.counts.n4d || 0,
    bf: sv.reduce((a, x) => a + (x.backfill.ready || 0), 0), of: sv.reduce((a, x) => a + (x.overflow.ready || 0), 0),
    cost: s.cost.per_hour, cf: (s.cost.counterfactual || {}).fleet_per_hour,
  });
  const cut = Date.now() - HISTORY_MS;
  while (history.length && history[0].t < cut) history.shift();
}
function renderCharts() {
  if (!Object.keys(charts).length) return;
  const now = history.length ? history[history.length - 1].t : Date.now();
  let prev = null;
  const labels = history.map((h) => {
    const s = Math.round((now - h.t) / 1000);
    const l = s < 5 ? "now" : `-${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
    const out = l === prev ? "" : l; prev = l; return out;
  });
  const set = (id, arrs) => { const c = charts[id]; if (!c) return; c.data.labels = labels; arrs.forEach((a, i) => (c.data.datasets[i].data = a)); c.update("none"); };
  const col = (k) => history.map((h) => h[k]);
  try {
    set("c-ttft", [col("p99"), col("p50"), col("thr"), col("base")]);
    set("c-rps", [col("rps"), col("target")]);
    const moreOpen = document.querySelector("#c-scale").closest("details").open;
    if (moreOpen) {
      set("c-scale", [col("vr"), col("g4"), col("n4d")]);
      set("c-svc", [col("bf"), col("of")]);
      set("c-cost", [col("cost"), col("cf")]);
    }
  } catch (e) { console.error("chart update failed", e); }
  const lg = last && last.loadgen, thr = last && last.controller.threshold_ms;
  $("ttft-now").textContent = lg ? `p99 ${num(lg.ttft_p99_ms)} ms${thr ? ` · threshold ${num(thr)} ms` : ""}` : "";
  $("rps-now").textContent = lg ? `${num(lg.achieved_rps)} of ${num(lg.target_rps)} rps` : "";
}

/* ------------------------------------------------------------------ poll loop */
let inflight = false;
async function poll() {
  if (inflight) return;
  inflight = true;
  try {
    const s = await fetchJSON("/api/state");
    last = s; lastGoodAt = Date.now();
    session = { auth: false, net: null, server: null };
    $("accel").hidden = !(s.demo && s.demo.accelerator_active);
    const parts = [
      ["story", renderStory], ["controls", syncControls], ["rail", renderRail], ["metrics", renderMetrics], ["evictions", trackEvictions],
      ["nodes", renderNodes], ["lanes", renderLanes], ["scenario", () => renderScenario(s.scenario)], ["events", renderEvents], ["method", renderMethod],
      ["history", (x) => { pushHistory(x); renderCharts(); }],
    ];
    for (const [name, fn] of parts) {
      try { fn(s); } catch (e) { console.error(`render ${name} failed`, e); } // one bad panel never blanks the page
    }
  } catch (e) {
    if (e instanceof AuthError) session.auth = true;
    else if (e.net) session.net = e.message;
    else session.server = e.message;
  } finally {
    inflight = false;
    renderConn(); renderAlerts();
  }
}

initCharts();
try {
  const q = new URLSearchParams(location.search).get("presenter");
  if (q === "1" || (q === null && localStorage.getItem("colo-presenter") === "1")) setPresenter(true);
} catch (_) { /* ignore */ }
document.querySelectorAll("details").forEach((d) => d.addEventListener("toggle", () => { if (last) renderCharts(); }));
poll();
setInterval(poll, POLL_MS);
