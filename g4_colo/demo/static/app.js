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
const TAG = { vllm: "V", sandbox: "S", toolcall: "T", analytics: "A", buffer: "B", agent: "D", worker: "W" };

const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const num = (v, d = 0) => (v === null || v === undefined || Number.isNaN(+v) ? "–" : Number(v).toFixed(d));
// Round half-up in cents first: toFixed(2) alone renders 0.305 as "0.30" (binary float).
const money = (v) => (v === null || v === undefined ? "–" : `$${(Math.round(Number(v) * 100 + 1e-6) / 100).toFixed(2)}`);
const hhmmss = (t) => new Date(t).toLocaleTimeString([], { hour12: false });
const plural = (n, one, many) => `${n} ${n === 1 ? one : many}`;

/* ------------------------------------------------------------------ accelerator wording */
// The accelerator host of the selected cluster (/api/state .accelerator, /api/clusters .accelerator_profile). Every
// G4/GPU word the page shows goes through L(), which mirrors state.localize(): identity on G4 clusters, so their text
// is exactly as written; on a TPU v6e cluster "G4 #1" -> "v6e #1", "G4" -> "TPU v6e", "GPU" -> "TPU".
const ACC_G4 = { id: "g4", name: "G4 · RTX PRO 6000", host: "G4", tag: "G4", short: "G", unit: "GPU", chip: "RTX PRO 6000", machine: "g4-standard-48" };
let ACC = ACC_G4;
function L(s) {
  if (s === null || s === undefined || ACC.id === "g4") return s;
  return String(s).replace(/RTX PRO 6000/g, ACC.chip).replace(/\bG4 #/g, `${ACC.tag} #`).replace(/\bG4\b/g, ACC.host).replace(/\bGPU(?=s?\b)/g, ACC.unit);
}
// Static markup marked data-acc: originals are captured once (page load, G4 wording) and re-localized on each switch.
const DOC_TITLE = document.title;
const accOrig = new Map();
function localizeStatic() {
  document.title = L(DOC_TITLE);
  document.querySelectorAll("[data-acc]").forEach((el) => {
    let o = accOrig.get(el);
    if (!o) {
      o = { texts: [], attrs: {} };
      const w = document.createTreeWalker(el, NodeFilter.SHOW_TEXT);
      for (let t = w.nextNode(); t; t = w.nextNode()) o.texts.push([t, t.nodeValue]);
      for (const a of ["title", "aria-label"]) if (el.hasAttribute(a)) o.attrs[a] = el.getAttribute(a);
      accOrig.set(el, o);
    }
    for (const [t, v] of o.texts) t.nodeValue = L(v);
    for (const [a, v] of Object.entries(o.attrs)) el.setAttribute(a, L(v));
  });
  for (const c of Object.values(charts)) { c.data.datasets.forEach((d) => { d._orig = d._orig ?? d.label; d.label = L(d._orig); }); c.update("none"); }
}
function setAccelerator(prof) {
  const next = prof && prof.id ? prof : ACC_G4;
  const changed = next.id !== ACC.id;
  ACC = next;
  if (changed) localizeStatic();
}

const history = [];
let last = null;
let lastGoodAt = 0;
let session = { auth: false, net: null, server: null };
const touched = {};
const evictedUntil = {};
const seenEvents = new Set();
let firstEvents = true;
let dismissedScenario = null;

/* ------------------------------------------------------------------ clusters */
// One UI, several clusters (GET /api/clusters). The choice comes from ?cluster=, then localStorage, then the
// server default, and every API call carries ?cluster=<id>.
const CLUSTER_KEY = "colo-demo.cluster";
let CLUSTERS = [];
let CLUSTER = new URLSearchParams(location.search).get("cluster") || null;
const api = (path) => (CLUSTER ? `${path}${path.includes("?") ? "&" : "?"}cluster=${encodeURIComponent(CLUSTER)}` : path);
const clusterById = (id) => CLUSTERS.find((c) => c.id === id);
// Native semantics: the native recipe, or the substrate tab on a native base (DEMO_CLUSTERS base_recipe "native").
const isNative = (s) => !!s && (s.recipe === "native" || s.base_recipe === "native");
const isSubstrate = (s) => !!s && s.recipe === "substrate";

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
    await fetchJSON(api(path), { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body ?? {}) });
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
const sendRate = debounce((v) => act("/api/gpu-rate", { rate: v }, L(`GPU rate set to ${v} rps`)), 400);
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

document.addEventListener("keydown", (e) => {
  if (e.ctrlKey || e.metaKey || e.altKey || /INPUT|TEXTAREA|SELECT/.test(e.target.tagName)) return;
  const map = { 1: "gpu-scale-out", 2: "gpu-scale-in", 3: "cpu-scale-out", 4: "guard-integer-vs-memory" };
  if (map[e.key]) {
    const btn = document.querySelector(`[data-scenario="${map[e.key]}"]`);
    if (!btn || btn.disabled) return;
    const cl = clusterById(CLUSTER);
    const where = (last && last.mode === "mock" ? "the simulator" : "the live cluster (it can add or remove nodes)") + (cl ? ` · ${cl.label}` : "");
    if (window.confirm(`Start scenario ${e.key}: ${btn.querySelector("b").textContent}?\nThis changes ${where}.`)) startScenario(map[e.key]);
  }
});

const fresh = (k) => touched[k] && Date.now() - touched[k] < 8000;
function syncControls(s) {
  const c = s.controller || {};
  if (!fresh("rate") && s.loadgen && s.loadgen.target_rps !== undefined) { $("rate").value = Math.round(s.loadgen.target_rps); $("rate-out").textContent = Math.round(s.loadgen.target_rps); }
  if (!fresh("demand") && c.demand) SVCS.forEach((sv) => { if (c.demand[sv] !== undefined) { $(`d-${sv}`).value = c.demand[sv]; $(`d-${sv}-out`).textContent = c.demand[sv]; } });
  if (!fresh("demand")) markPresets(demandValues());
  if (isNative(s)) {
    $("baseline-info").textContent = "no guard on this cluster";
  } else {
    if (!fresh("guard") && c.guard_enabled !== undefined && c.guard_enabled !== null) $("guard").checked = !!c.guard_enabled;
    $("guard-label").textContent = c.guard_enabled ? "On" : "Off";
    $("guard-sub").textContent = L(c.guard_enabled
      ? (c.threshold_ms ? `moves analytics off G4 above ${num(c.threshold_ms)} ms` : "moves analytics off G4 when TTFT rises")
      : "off: nothing is moved");
    $("guard").title = $("guard-sub").textContent;
    $("baseline-info").textContent = c.baseline_ttft_p99_ms ? `baseline ${num(c.baseline_ttft_p99_ms)} ms · threshold ${num(c.threshold_ms)} ms` : "no baseline yet";
  }
  const running = s.scenario && s.scenario.status === "running";
  $("manual-fields").disabled = running;
  $("manual-lock").hidden = !running;
}

// Which control plane runs this cluster, and what it does not have (native: no guard, no scenario 4).
function renderControl(s) {
  const ctl = s.control || {}, native = isNative(s);
  document.body.classList.toggle("recipe-native", native);
  document.body.classList.toggle("recipe-substrate", isSubstrate(s));
  $("ctl-chip").hidden = !ctl.label;
  $("ctl-label").textContent = ctl.label || "";
  const hp = ctl.hpas || {};
  $("ctl-chip").title = [ctl.detail, ...Object.entries(hp).map(([sv, h]) => `${h.name}: colo_overflow_needed ${num(h.metric)} → ${num(h.desired)} replicas (min ${h.min})`),
    native ? L(`${ctl.eligible_g4 ?? 0} G4 node(s) eligible (running a Ready vLLM pod) · ${ctl.backfill_waiting ?? 0} backfill pod(s) waiting`) : ""].filter(Boolean).join("\n");
  $("guard-row").hidden = native;
  $("guard-na").hidden = !native;
  if (ctl.guard_reason) $("guard-na").textContent = `Not used here: ${ctl.guard_reason}.`;
  $("btn-baseline").disabled = native;
  $("btn-baseline").title = native ? `No baseline on this cluster: ${ctl.guard_reason || "no latency guard"}.` : "Records the current TTFT p99 as the guard's reference. Do this at steady load with no memory-heavy pods.";
  $("zh-n").textContent = native ? "on N4D · overflow HPA" : "on N4D";
}
const unavailable = (name) => ((last && last.demo && last.demo.scenarios_unavailable) || {})[name];

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
  for (const sv of SVCS) $(`spill-${sv}`).innerHTML = t[sv].of ? (isNative(s) ? `<b>${t[sv].of}</b> on N4D` : `<b>${t[sv].of}</b> spill to N4D`) : "";
}

// Secondary numbers live in the "Presets, scenarios, numbers" expander, not on the main view.
function renderNumbers(s) {
  const lg = s.loadgen || {}, c = s.controller || {}, v = s.vllm || {}, hpa = v.hpa || {}, cn = s.counts || {};
  const t = svcTotals(s);
  const bf = SVCS.reduce((a, sv) => a + t[sv].bf, 0), of = SVCS.reduce((a, sv) => a + t[sv].of, 0);
  const want = SVCS.reduce((a, sv) => a + t[sv].bfWant + t[sv].ofWant, 0);
  const slots = ((s.story || {}).g4_slots_per_node || 0) * (cn.g4_ready || 0);
  const rows = [
    ["TTFT p50", lg.ttft_p50_ms != null ? `${num(lg.ttft_p50_ms)} ms` : "–"],
    ["End-to-end p50", lg.e2e_p50_ms != null ? `${num(lg.e2e_p50_ms)} ms` : "–"],
    isNative(s)
      ? ["Overflow HPA metric → replicas", Object.entries((s.control || {}).hpas || {}).map(([sv, h]) => `${sv[0].toUpperCase()} ${num(h.metric)}→${num(h.desired)}`).join(" · ") || "–"]
      : ["Guard baseline · threshold", c.baseline_ttft_p99_ms ? `${num(c.baseline_ttft_p99_ms)} · ${num(c.threshold_ms)} ms` : "no baseline"],
    ["vLLM replicas ready / desired", `${num(v.ready)} / ${num(v.desired)}${hpa.min !== undefined ? ` (HPA ${hpa.min}–${hpa.max})` : ""}`],
    ["Nodes G4 · N4D", `${cn.g4 || 0} · ${cn.n4d || 0}`],
    isNative(s)
      ? ["CPU pods on G4 · N4D", `${bf} · ${of}${(s.control || {}).backfill_waiting ? ` (${s.control.backfill_waiting} waiting → N4D)` : ""}`]
      : ["CPU pods on G4 · N4D", `${bf} · ${of}${want > bf + of ? ` (${want - bf - of} pending)` : ""}`],
    ["G4 slots used", slots ? `${bf} of ${slots}` : "–"],
    ["Fleet cost", `${money((s.cost || {}).per_hour)}/hr`],
  ];
  $("kvs").innerHTML = rows.map(([k, x]) => `<dt>${esc(L(k))}</dt><dd>${esc(L(x))}</dd>`).join("");
}

/* ------------------------------------------------------------------ story strip */
function renderStory(s) {
  const st = s.story || {};
  const cf = (s.cost || {}).counterfactual || {};
  const lg = s.loadgen || {}, c = s.controller || {};
  const g4n = (s.counts || {}).g4_ready || 0;
  $("h-reclaim").textContent = num(st.reclaimed_vcpu);
  $("s-reclaim").title = L(st.reclaimed_pct_of_spare != null
    ? `${num(st.reclaimed_pct_of_spare)}% of spare G4 cores · ${st.reclaimed_pods} CPU pods · GPU slice ${num(st.gpu_slice_vcpu)} vCPU per G4 node`
    : "no spare G4 cores yet");
  const segs = [["v", L("GPU slice"), st.gpu_slice_total_vcpu], ["r", "Reclaimed", st.reclaimed_vcpu], ["b", "Warm buffer", st.g4_buffer_vcpu], ["f", "Free", (st.g4_idle_vcpu || 0) + (st.g4_system_vcpu || 0)]];
  const bar = $("vis-budget");
  segs.forEach(([k, , x], i) => { bar.children[i].style.flexGrow = Math.max(0, x || 0); });
  const total = st.g4_alloc_vcpu || 0;
  bar.setAttribute("aria-label", L(`G4 vCPU budget`) + `${total ? ` (${num(total)} allocatable on ${g4n} node${g4n === 1 ? "" : "s"})` : ""}: ${segs.filter(([, , x]) => x > 0.05).map(([, l, x]) => `${l} ${num(x)}`).join(", ")}`);
  bar.title = bar.getAttribute("aria-label");
  $("budget-key").innerHTML = segs.map(([, l, x]) => `<li>${esc(l)} ${num(x)}</li>`).join("");

  $("h-saved").textContent = money(st.saved_per_hour);
  $("s-saved").title = cf.saved_nodes !== undefined
    ? `${cf.saved_nodes} fewer n4d-standard-16 than running every CPU pod on N4D · ${num(st.saved_pct)}% of the bill without colo` : "";
  const running = st.n4d_nodes || 0, avoided = Math.max(0, cf.saved_nodes || 0);
  $("vis-nodes").innerHTML = '<i class="run"></i>'.repeat(running) + '<i class="avoid"></i>'.repeat(avoided);
  $("vis-nodes").setAttribute("aria-label", `${running} N4D node${running === 1 ? "" : "s"} running, ${avoided} avoided by colo`);

  const thr = c.threshold_ms, p99 = lg.ttft_p99_ms, over = !!(thr && p99 && p99 > thr);
  $("h-p99").firstChild.nodeValue = num(p99);
  $("h-p99").classList.toggle("bad", over);
  $("h-lim").textContent = thr ? `TTFT p99 · ${over ? "over" : "limit"} ${num(thr)}` : "TTFT p99";
  $("h-lim").classList.toggle("warn", over);
  $("s-ttft").title = `p50 ${num(lg.ttft_p50_ms)} ms${c.baseline_ttft_p99_ms ? ` · baseline ${num(c.baseline_ttft_p99_ms)} ms` : ""}`;
  const ach = lg.achieved_rps, tgt = lg.target_rps, short = !!(tgt && ach != null && ach < tgt * 0.95);
  $("h-rps").firstChild.nodeValue = num(ach);
  $("h-tgt").textContent = short ? `of ${num(tgt)} target` : "requests/s · on target";
  $("h-tgt").classList.toggle("warn", short);
}

/* Tiny inline-SVG trend line: no axes; the big number beside it carries the value. */
function spark(svg, vals, ref, cls) {
  const W = svg.clientWidth || 200, H = svg.clientHeight || 58, pad = 4;
  const pts = vals.map((v, i) => [i, v]).filter(([, v]) => v !== null && v !== undefined && Number.isFinite(+v));
  if (!pts.length) { svg.innerHTML = ""; return; }
  const max = Math.max(1, ref || 0, ...pts.map(([, v]) => v)) * 1.12;
  const n = Math.max(2, vals.length);
  const X = (i) => (i / (n - 1)) * (W - 6);
  const Y = (v) => pad + (1 - v / max) * (H - 2 * pad);
  const line = pts.map(([i, v]) => `${X(i).toFixed(1)},${Y(v).toFixed(1)}`).join(" ");
  const [li, lv] = pts[pts.length - 1];
  svg.setAttribute("viewBox", `0 0 ${W} ${H}`);
  svg.innerHTML = (ref ? `<line class="thr ${cls === "ach" ? "target" : "limit"}" x1="0" x2="${W - 6}" y1="${Y(ref)}" y2="${Y(ref)}"/>` : "") +
    `<polygon class="area ${cls}" points="${X(pts[0][0])},${H - pad} ${line} ${X(li)},${H - pad}"/>` +
    `<polyline class="ln ${cls}" points="${line}"/><circle class="dot ${cls}" cx="${X(li)}" cy="${Y(lv)}" r="2.8"/>`;
}
function renderSparks() {
  const recent = history.slice(-90); // ~3 min at a 2 s poll
  const lastH = recent[recent.length - 1] || {};
  const over = lastH.thr && lastH.p99 > lastH.thr;
  spark($("sp-ttft"), recent.map((h) => h.p99), lastH.thr, over ? "p99 bad" : "p99");
  spark($("sp-rps"), recent.map((h) => h.rps), lastH.target, "ach");
}

/* ------------------------------------------------------------------ nodes: L3-grouped core maps */
// Nodes are numbered oldest first so a new G4 node always appears as the next number.
function orderedNodes(s) {
  const order = { g4: 0, n4d: 1 };
  const nodes = (s.nodes || []).filter((n) => n.role in order).sort((a, b) => order[a.role] - order[b.role] || (b.age_s ?? 0) - (a.age_s ?? 0));
  const idx = { g4: 0, n4d: 0 };
  return nodes.map((n) => { idx[n.role] += 1; return { n, label: `${n.role === "g4" ? ACC.tag : "N4D"} #${idx[n.role]}`, short: `${n.role === "g4" ? ACC.short : "N"}${idx[n.role]}` }; });
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
    else if (c.cat === "worker") app = "worker";
    else if (c.cat === "system") app = "system";
    else if (c.cat === "integer" || c.cat === "memory") app = o && SVCS.includes(o.app) ? o.app : (c.cat === "memory" ? "analytics" : "other");
    else if (o) app = "other";
    // occ: Substrate Worker hosting a RUNNING actor (true), warm idle Worker (false), or not reported (undefined).
    return { app, occ: app === "worker" && o ? o.actor : undefined, pending: !!(o && o.pod && podReady[o.pod] === false), label: o ? `${o.ns ? o.ns + "/" : ""}${o.pod}` : "" };
  });
  // Customer DaemonSets get their own colour: ceil(their CPU requests) shared-pool CPUs, after the kubelet/system reserve.
  const agentCpu = n.agent_cpu || 0;
  let reserved = Math.max(0, Math.round((n.ncpu || 0) - (n.alloc_cpu || n.ncpu || 0))) + Math.round(Math.max(0, (n.sys_cpu || 0) - agentCpu));
  let agentTiles = agentCpu > 0 ? Math.ceil(agentCpu) : 0;
  const agentLabel = `DaemonSet (shared cores): ${(n.agents || []).map((a) => `${a.ns}/${a.name} ${a.cpu} vCPU, ${a.ready ? "Running" : "Starting"}`).join("; ")}`;
  const agentPending = (n.agents || []).some((a) => !a.ready);
  const shared = n.cpus.filter((c, i) => kinds[i].app === "free").sort((a, b) => a.core - b.core || a.thread - b.thread);
  for (const c of shared) {
    const i = n.cpus.indexOf(c);
    if (reserved > 0) { kinds[i].app = "system"; kinds[i].label = "reserved for kubelet / system"; reserved--; }
    else if (agentTiles > 0) { kinds[i].app = "agent"; kinds[i].label = agentLabel; kinds[i].pending = agentPending; agentTiles--; }
    else break;
  }
  return kinds;
}

// L3 domains, columns and rows come from the node's real CPU topology (node agent, sysfs): e.g. 8/8/8 physical cores
// on g4-standard-48 (EPYC 9B45) and 8/7/7 on ct6e-standard-1t (EPYC 9B14). One column per physical core, one row per
// hyperthread. Rebuilt when the topology signature changes (see coreMapKey).
function coreMapKey(n) { return (n.cpus || []).map((c) => `${c.id}:${c.core}:${c.thread}:${c.llc}`).join(","); }
function buildCoreMap(n) {
  const root = document.createElement("div");
  root.className = "cmap";
  root._key = coreMapKey(n);
  root._tiles = [];
  root._llcs = [];
  const byLlc = new Map();
  for (const c of n.cpus || []) {
    if (!byLlc.has(c.llc)) byLlc.set(c.llc, new Map());
    const cores = byLlc.get(c.llc);
    if (!cores.has(c.core)) cores.set(c.core, []);
    cores.get(c.core)[c.thread] = c.id;
  }
  for (const l of [...byLlc.keys()].sort((a, b) => a - b)) {
    const cores = [...byLlc.get(l).keys()].sort((a, b) => a - b);
    const nthreads = Math.max(...cores.map((k) => byLlc.get(l).get(k).length));
    const g = document.createElement("div");
    g.className = "llc";
    g.innerHTML = `<div class="llc-h">L3 ${l}</div><div class="tiles" style="--llc-cols:${cores.length}"></div>`;
    g._cores = `${cores.length} cores (${cores.length * nthreads} vCPU), cores ${cores[0]}–${cores[cores.length - 1]}`;
    const grid = g.lastChild;
    for (let t = 0; t < nthreads; t++) for (const k of cores) {
      const id = byLlc.get(l).get(k)[t];
      const el = document.createElement("i");
      el.className = "cpu a-free";
      el.innerHTML = `<i class="fill"></i><b class="lt"></b>`;
      grid.appendChild(el);
      if (id !== undefined) root._tiles[id] = { el, fill: el.firstChild, tag: el.lastChild, app: "free", llc: root._llcs.length };
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
    // Worker occupancy is its own encoding (solid tile + "W●" vs outlined "W"), separate from CPU busy %.
    t.el.classList.toggle("occ", k.occ === true);
    t.el.classList.toggle("idle", k.occ === false);
    if (k.app === "worker") t.tag.textContent = k.occ === true ? "W●" : "W";
    const busy = Math.max(0, Math.min(100, Number(c.busy) || 0));
    t.fill.style.transform = `scaleY(${busy / 100})`;
    const occTxt = k.occ === true ? " · actor running" : k.occ === false ? " · idle warm Worker" : "";
    t.el.title = `CPU ${c.id} (core ${c.core}, thread ${c.thread}, L3 ${c.llc}): ${k.app === "free" ? "free (shared pool)" : k.app === "system" ? "reserved" : k.label || k.app}${occTxt}${k.pending ? " · starting" : ""} · busy ${c.busy ?? "?"}%`;
    if (!["free", "system"].includes(k.app)) per[t.llc].add(k.app);
    cnt[k.app] = (cnt[k.app] || 0) + 1;
  });
  root._llcs.forEach((g, i) => {
    const names = [...per[i]].map((a) => (a === "vllm" ? "vLLM" : a));
    g.title = `L3 cache domain ${i}, ${g._cores}: ${names.length ? names.join(", ") : "free"}`;
    g.classList.toggle("has-vllm", per[i].has("vllm"));
    g.classList.toggle("has-mem", per[i].has("analytics"));
  });
  root.setAttribute("aria-label", `${label} per-vCPU map: ${Object.entries(cnt).map(([k, v]) => `${v} ${k === "system" ? "reserved" : k === "agent" ? "DaemonSet" : k === "worker" ? "Substrate Worker" : k}`).join(", ")}`);
}

// Node agent not reporting yet: fall back to one flat cell per requested vCPU from the pod list.
function flatCells(n) {
  const cells = [];
  for (const p of n.pods || []) {
    const app = p.cat === "vllm" ? "vllm" : p.cat === "buffer" ? "buffer" : p.cat === "worker" ? "worker" : SVCS.includes(p.app) ? p.app : "system";
    for (let i = 0; i < Math.max(1, Math.round(p.cpu || 0)); i++) cells.push({ app, pending: !p.ready, occ: app === "worker" ? p.actor : undefined });
  }
  const cap = n.ncpu || Math.round(n.alloc_cpu || 0);
  const agentCpu = n.agent_cpu || 0;
  let reserved = Math.max(0, Math.round((n.ncpu || 0) - (n.alloc_cpu || n.ncpu || 0))) + Math.round(Math.max(0, (n.sys_cpu || 0) - agentCpu));
  while (reserved-- > 0 && cells.length < cap) cells.push({ app: "system", pending: false });
  let agentTiles = agentCpu > 0 ? Math.ceil(agentCpu) : 0;
  const agentPending = (n.agents || []).some((a) => !a.ready);
  while (agentTiles-- > 0 && cells.length < cap) cells.push({ app: "agent", pending: agentPending });
  while (cells.length < cap) cells.push({ app: "free", pending: false });
  cells.length = Math.min(cells.length, cap); // never draw more cells than the node has vCPUs
  const cols = Math.max(1, Math.ceil(cap / 2)); // two rows, like the hyperthread pairs of the core map
  return `<div class="flat" style="--cols:${cols}" role="img" aria-label="vCPU requests (node agent not reporting${n.ready ? "" : "; node starting"})">${cells.map((c) => `<i class="cpu a-${c.app}${c.pending ? " pending" : ""}${c.occ === true ? " occ" : c.occ === false ? " idle" : ""}"><i class="fill"></i><b class="lt">${c.occ === true ? "W●" : TAG[c.app] || ""}</b></i>`).join("")}</div>`;
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
function nodeEl(n) {
  const el = document.createElement("div");
  el.className = n.role === "g4" ? "nrow" : "n4d-one";
  el.innerHTML = n.role === "g4"
    ? `<div class="nname"><b class="g4"></b><span class="flag" hidden></span><span class="atl" hidden></span></div><div class="map"></div>`
    : `<span class="t"><span class="nm"></span><span class="flag" hidden></span><span class="atl" hidden></span></span><div class="map"></div>`;
  return el;
}
function nodeFlag(n, breach) {
  if (!n.ready) return ["warn", "starting"];
  if (n.role === "g4" && !(n.pods || []).some((p) => p.cat === "vllm" && p.ready)) return ["warn", "vLLM starting"];
  if ((n.blocks || []).length) return ["bad", "guard"];
  if (breach) return ["bad", "p99 high"];
  return null;
}
function renderNodes(s) {
  const thr = (s.controller || {}).threshold_ms;
  const slots = (s.story || {}).g4_slots_per_node;
  const seen = new Set();
  const g4 = [], n4d = [];
  for (const { n, label } of orderedNodes(s)) {
    seen.add(n.name);
    let el = nodeEls.get(n.name);
    if (!el) { el = nodeEl(n); nodeEls.set(n.name, el); }
    const breach = !!(n.role === "g4" && thr && n.ttft_p99_ms && n.ttft_p99_ms > thr);
    el.classList.toggle("breach", breach);
    el.classList.toggle("evicted", evictedUntil[n.name] > Date.now());
    el.querySelector(n.role === "g4" ? "b" : ".nm").textContent = label;
    const f = nodeFlag(n, breach), fl = el.querySelector(".flag");
    fl.hidden = !f;
    if (f) { fl.className = `flag ${f[0]}`; fl.textContent = f[1]; }
    // Per-node detail on hover, so the map stays quiet.
    const type = n.instance_type || (n.role === "g4" ? ACC.machine : "n4d-standard-16");
    const cpuPods = (n.pods || []).filter((p) => SVCS.includes(p.app)).length;
    const bits = [`${label} · ${type} · ${n.zone || ""}`, n.name];
    if (n.cpus && n.cpus.length) {
      const doms = [...new Set(n.cpus.map((c) => c.llc))].map((l) => new Set(n.cpus.filter((c) => c.llc === l).map((c) => c.core)).size);
      bits.push(`${n.cpus.length} vCPU · ${n.cpu_model || "CPU"} · ${doms.length} L3 domains of ${doms.join("/")} cores${n.l3_kib ? ` · ${Math.round(n.l3_kib / 1024)} MiB L3 each` : ""}${n.topology_source === "assumed" ? " (assumed)" : ""}`);
    }
    if (n.role === "g4") bits.push(`TTFT p99 ${n.ttft_p99_ms != null ? num(n.ttft_p99_ms) + " ms" : "–"} · p50 ${n.ttft_p50_ms != null ? num(n.ttft_p50_ms) + " ms" : "–"} · ${n.achieved_rps ? num(n.achieved_rps) : "–"} rps`, `${cpuPods}${slots ? ` of ${slots}` : ""} CPU pods`);
    else bits.push(`${cpuPods} overflow pods`);
    const wk = (n.pods || []).filter((p) => p.cat === "worker");
    if (wk.length) bits.push(`${plural(wk.length, "Substrate Worker", "Substrate Workers")} (${num(wk.reduce((a, p) => a + (p.cpu || 0), 0))} vCPU)${wk.some((p) => p.actor !== undefined) ? ` · ${wk.filter((p) => p.actor).length} with a running actor` : ""}${wk.some((p) => !p.ready) ? " · some starting" : ""}`);
    (n.blocks || []).forEach((b) => bits.push(`guard: no ${b} pods`));
    // Substrate tab: the node's atelet (DaemonSet) as a small marker with an icon and a word, not colour alone.
    const atl = el.querySelector(".atl"), cpOn = isSubstrate(s) && ((s.substrate || {}).control_plane || {}).present;
    atl.hidden = !cpOn;
    if (cpOn) {
      const a = n.atelet;
      atl.className = `atl ${!a ? "warn" : a.ready ? "ok" : "bad"}`;
      atl.textContent = !a ? "– no atelet" : a.ready ? "✓ atelet" : "✕ atelet";
      atl.title = !a ? "No atelet pod on this node (ate-system DaemonSet)" : `${a.pod}: ${a.ready ? "Ready" : `NotReady (${a.phase || "?"})`} · restarts ${a.restarts ?? "?"}`;
      bits.push(atl.title);
    }
    el.title = bits.join("\n");
    const host = el.querySelector(".map");
    if (n.cpus && n.cpus.length) {
      if (!host._map || host._map._key !== coreMapKey(n)) { host.innerHTML = ""; host._map = buildCoreMap(n); host.appendChild(host._map); }
      updateCoreMap(host._map, n, label);
    } else {
      host._map = null; host._ncpu = 0;
      host.innerHTML = flatCells(n);
    }
    (n.role === "g4" ? g4 : n4d).push(el);
  }
  for (const k of [...nodeEls.keys()]) if (!seen.has(k)) nodeEls.delete(k);

  const root = $("nodes");
  const want = [...g4];
  const hpa = (s.vllm || {}).hpa || {};
  if (g4.length && hpa.max && g4.length < hpa.max) {
    const gh = root.querySelector(".ghost") || Object.assign(document.createElement("div"), { className: "ghost" });
    gh.textContent = L(`G4 #${g4.length + 1} joins when vLLM scales out`);
    want.push(gh);
  }
  if (n4d.length) {
    let line = root.querySelector(".n4d-line");
    if (!line) {
      line = document.createElement("div"); line.className = "n4d-line";
      line.innerHTML = `<div class="nname"><b class="n4d">N4D</b></div><div class="n4ds"></div>`;
    }
    const wrap = line.querySelector(".n4ds");
    n4d.forEach((el, i) => { if (wrap.children[i] !== el) wrap.insertBefore(el, wrap.children[i] || null); });
    [...wrap.children].slice(n4d.length).forEach((x) => x.remove());
    want.push(line);
  }
  const cpNodes = isSubstrate(s) ? (((s.substrate || {}).control_plane || {}).nodes || []) : [];
  if (cpNodes.length) {
    let line = root.querySelector(".cp-line");
    if (!line) {
      line = document.createElement("div"); line.className = "cp-line";
      line.innerHTML = `<div class="nname"><b class="cp" title="Agent Substrate control plane (namespace ate-system)">Control plane</b></div><div class="cps"></div>`;
    }
    line.querySelector(".cps").innerHTML = cpNodes.map(cpNodeHTML).join("");
    want.push(line);
  }
  // The CSS sizes G4 tile height from the space left in #nodes, divided by the rows it must hold.
  root.style.setProperty("--cpn", cpNodes.length ? "1" : "0");
  root.style.setProperty("--g4n", String(Math.max(1, g4.length)));
  root.style.setProperty("--ghost", want.some((el) => el.classList.contains("ghost")) ? "1" : "0");
  root.style.setProperty("--n4dn", n4d.length ? "1" : "0");
  root.style.setProperty("--n4dc", String(Math.max(1, n4d.length)));
  if (!want.length) { root.innerHTML = `<p class="empty">${esc(L("No G4 or N4D nodes reported yet."))}</p>`; return; }
  root.querySelector(".empty")?.remove();
  want.forEach((el, i) => { if (root.children[i] !== el) root.insertBefore(el, root.children[i] || null); });
  [...root.children].slice(want.length).forEach((x) => x.remove());
}

/* ------------------------------------------------------------------ Agent Substrate control plane */
// One tile per ate-system pod on the control-plane nodes: C = control plane, P = postgres, D = a DaemonSet pod.
// NotReady pods get a dashed outline and a "!" after the letter (not colour alone); detail on hover.
const cpPodTitle = (p) => `${p.name} · ${p.component || "other"} · ${p.ready ? "Ready" : `NotReady (${p.phase || "?"})`} · restarts ${p.restarts ?? "?"} · ${num(p.cpu, 2)} vCPU requested`;
function cpNodeHTML(n) {
  const cls = (p) => (p.letter === "P" ? "a-pg" : p.letter === "D" ? "a-agent" : "a-cp");
  const tiles = (n.pods || []).map((p) => `<i class="cpt ${cls(p)}${p.ready ? "" : " nr"}" title="${esc(cpPodTitle(p))}"><b>${p.letter}${p.ready ? "" : "!"}</b></i>`).join("")
    || '<span class="nd">no ate-system pods</span>';
  const head = `${n.kind === "postgres" ? "postgres" : "infra"} · ${n.instance_type || "?"}`;
  const tip = `${n.name}\n${n.pool ? `pool ${n.pool} · ` : ""}${n.zone || ""}\nate.dev/workloadType=${n.kind === "postgres" ? "ate-postgres" : "ate-system-infra"}`;
  return `<div class="cp-one" title="${esc(tip)}"><span class="t">${esc(head)}${n.ready ? "" : ' <span class="flag bad">✕ NotReady</span>'}</span><div class="cpts" role="img" aria-label="${esc(`${head}: ${(n.pods || []).map((p) => `${p.component || p.name} ${p.ready ? "ready" : "not ready"}`).join(", ")}`)}">${tiles}</div></div>`;
}
const HEALTH = { ok: ["✓", "h-ok"], degraded: ["▲", "h-warn"], "scaled-to-zero": ["▲", "h-warn"], down: ["✕", "h-bad"], unknown: ["?", "h-unk"], missing: ["–", "h-unk"] };
function cpStripHTML(cp) {
  if (!cp || !cp.present) return '<p class="cp-strip"><b class="cp-k">Control plane</b> <span class="nd">no data yet · ate-system not found (Substrate control plane not installed)</span></p>';
  const chip = (label, x, extraTitle) => {
    const [icon, cls] = HEALTH[x.health] || HEALTH.unknown;
    const val = x.health === "missing" ? "not found" : `${num(x.ready)}/${num(x.desired)}`;
    const word = { degraded: " degraded", down: " down", "scaled-to-zero": " scaled to 0" }[x.health] || "";
    const t = `${label}: ${x.health}${x.source ? ` · ${x.source}${x.workloads && x.workloads.length ? ` ${x.workloads.join(", ")}` : ""}` : ""}${x.restarts ? ` · ${x.restarts} restarts` : ""}${extraTitle || ""}`;
    return `<li class="${cls}" title="${esc(t)}"><span aria-hidden="true">${icon}</span> ${esc(label)} <b>${val}</b>${word ? `<span class="hw">${word}</span>` : ""}</li>`;
  };
  const at = cp.atelet || {};
  return `<div class="cp-strip"><b class="cp-k">Control plane</b><ul>${(cp.components || []).map((c) => chip(c.label, c)).join("")}` +
    chip("atelet", at, at.found ? ` · ${num(at.available)} available across nodes` : "") + `</ul>` +
    ((cp.unscheduled || []).length ? `<span class="hw h-warn" title="${esc(cp.unscheduled.map(cpPodTitle).join("\n"))}">▲ ${cp.unscheduled.length} unscheduled</span>` : "") + `</div>`;
}

/* ------------------------------------------------------------------ Agent Substrate actor load */
// Two sliders patch ConfigMap substrate-exp/actor-load through POST /api/substrate/actors (debounced 1 s; awake <= total).
// Target vs actual: target_awake against actors RUNNING per pool, so G4-first then N4D overflow is visible.
const alVals = () => ({ total: +$("al-total").value, awake: +$("al-awake").value });
function alSet(total, awake) {
  $("al-total").value = total; $("al-total-out").textContent = total;
  $("al-awake").max = String(Math.max(0, total));
  awake = Math.min(awake, total);
  $("al-awake").value = awake; $("al-awake-out").textContent = awake;
  document.querySelectorAll("[data-al-total]").forEach((b) => { b.setAttribute("aria-pressed", String(+b.dataset.alTotal === total)); b.disabled = false; });
  document.querySelectorAll("[data-al-awake]").forEach((b) => { b.setAttribute("aria-pressed", String(+b.dataset.alAwake === awake)); b.disabled = +b.dataset.alAwake > total; });
}
const sendActors = debounce(() => { const v = alVals(); act("/api/substrate/actors", v, `Actors: ${v.total} total, ${v.awake} awake`); }, 1000);
function alInput(total, awake) { touched.actors = Date.now(); alSet(total, awake); sendActors(); }
$("al-total").addEventListener("input", (e) => alInput(+e.target.value, alVals().awake));
$("al-awake").addEventListener("input", (e) => alInput(alVals().total, +e.target.value));
document.querySelectorAll("[data-al-total]").forEach((b) => b.addEventListener("click", () => alInput(+b.dataset.alTotal, alVals().awake)));
document.querySelectorAll("[data-al-awake]").forEach((b) => b.addEventListener("click", () => alInput(alVals().total, +b.dataset.alAwake)));
// Total Workers: HPA substrate-exp/g4-workers maxReplicas (option 2; the HPA sets the G4 share, the n4d-workers HPA the
// rest), or WorkerPool g4-workers replicas when that HPA is absent. POST /api/substrate/workers, debounced 1 s.
const sendWorkers = debounce(() => { const n = +$("wp-n").value; act("/api/substrate/workers", { total: n }, `Total Workers: ${n}`); }, 1000);
$("wp-n").addEventListener("input", (e) => { touched.workers = Date.now(); $("wp-n-out").textContent = e.target.value; sendWorkers(); });
function renderWorkerPool(sub) {
  const pools = sub.pools || {}, g4 = pools["g4-workers"], n4 = pools["n4d-workers"], hpa = sub.g4_hpa;
  const on = sub.total_workers_source != null;
  $("wp-n").disabled = !on;
  $("wp-n").min = String(hpa ? Math.max(1, hpa.min ?? 1) : 0);  // an HPA cannot have maxReplicas below max(1, min)
  if (on && !fresh("workers")) { $("wp-n").value = sub.total_workers ?? 0; $("wp-n-out").textContent = sub.total_workers ?? 0; }
  if (!on) { $("wp-v").innerHTML = '<span class="nd" title="Neither HPA nor WorkerPool substrate-exp/g4-workers found">Not connected: no g4-workers HPA or WorkerPool.</span>'; return; }
  const gs = g4 ? g4.desired ?? 0 : 0, ns = n4 ? n4.desired ?? 0 : 0, k = (g4 ? g4.pending ?? 0 : 0) + (n4 ? n4.pending ?? 0 : 0);
  const src = hpa ? `HPA ${hpa.name} maxReplicas (G4 share = Workers that fit next to Ready vLLM)` : "WorkerPool g4-workers replicas (no g4-workers HPA)";
  // Two short lines that never ellipsize at 1366 px: the shares, then pending + the headroom note (detail in tooltips).
  $("wp-v").innerHTML = `<span class="wp-line" title="${esc(`Total Workers = ${src}. G4 share = WorkerPool g4-workers spec.replicas; N4D share = n4d-workers spec.replicas.`)}">Share: G4 <b>${num(gs)}</b> · N4D <b>${num(ns)}</b></span>` +
    `<span class="al-sub" title="Pending = Workers with no node yet. The n4d-workers HPA adds N4D Workers above Total Workers when awake actors need them (busy headroom)."><b class="${k ? "warn" : ""}">${num(k)}</b> pending · N4D adds more if busy</span>`;
}

function renderActorLoad(sub) {
  renderWorkerPool(sub);
  const al = sub.actor_load || {}, a = sub.actors || {};
  const on = !!al.exists;
  $("al").classList.toggle("off", false);
  $("al").querySelectorAll(".field:not(:has(#wp-n))").forEach((f) => f.classList.toggle("off", !on));
  $("al").querySelectorAll("input:not(#wp-n), button").forEach((x) => { if (!on) x.disabled = true; else if (x.tagName === "INPUT") x.disabled = false; });
  if (on && !fresh("actors")) alSet(al.total ?? 0, al.awake ?? 0);
  if (!on) { $("al-v").innerHTML = '<span class="nd" title="ConfigMap substrate-exp/actor-load not found. Deploy the actor-load generator (Deployment substrate-exp/actor-load) to drive actors from here.">Not connected: deploy actor-load to use these controls.</span>'; return; }
  // One awake actor occupies one Worker: running = min(target, Ready Workers), G4 first; the rest wait.
  const tgt = a.target_awake ?? al.awake ?? 0, g = a.running_g4 ?? 0, n = a.running_n4d ?? 0;
  const run = a.actors_running ?? g + n, wait = Math.max(0, tgt - run);
  const W = Math.max(1, tgt, run);
  const seg = (k, x) => (x > 0 ? `<i class="${k}" style="flex-grow:${x / W}"></i>` : "");
  const lag = a.target_awake != null && a.target_awake !== al.awake ? `<span class="al-lag">generator still on ${a.target_awake} awake</span>` : "";
  $("al-v").innerHTML =
    `<span class="al-line" title="${esc(`${num(al.total)} actors exist · 1 awake actor per Worker (suspended actors take none) · each awake actor holds its Worker ${num(al.hold_s ?? a.hold_s ?? 5)} s per cycle`)}">Running <b>${num(run)}</b> / <b>${num(tgt)}</b> awake${wait ? ` <b class="warn" title="${wait} awake actors waiting for a free Worker">▲ ${wait}</b>` : tgt ? ' <span title="every awake actor has a Worker">✓</span>' : ""}</span>` +
    `<div class="al-bar" role="img" aria-label="${esc(`${g} awake actors on G4 Workers, ${n} on N4D Workers, ${wait} waiting for a Worker`)}">${seg("g", g)}${seg("n", n)}${seg("w", wait)}${tgt || run ? "" : '<i class="e" style="flex-grow:1"></i>'}</div>` +
    `<span class="al-key"><span><i class="k g"></i>G4 <b>${num(g)}</b></span><span><i class="k n"></i>N4D <b>${num(n)}</b></span><span><i class="k w"></i>waiting <b>${num(wait)}</b></span></span>` +
    lag;
}

/* ------------------------------------------------------------------ CPU-backfill clamp (substrate tab) */
// Workers outrank CPU backfill (option 2), so the backfill sliders max out at floor(vCPUs left after Workers / per-replica
// CPU). A UI clamp only: preemption enforces it in-cluster. Other tabs keep the static max of 20.
const CLAMP_SVCS = ["sandbox", "toolcall"];
function renderCpuClamp(s) {
  const b = isSubstrate(s) ? (s.substrate || {}).cpu_budget : null;
  $("cpu-left").hidden = !b;
  for (const sv of CLAMP_SVCS) {
    const el = $(`d-${sv}`);
    const mx = b && b.max_replicas && b.max_replicas[sv] != null ? Math.max(0, Math.min(200, b.max_replicas[sv])) : 20;
    if (el.max !== String(mx)) el.max = String(mx);
    el.title = b ? `max ${mx} = floor(${num(b.vcpu_left, 1)} vCPU left / ${num((b.per_replica_vcpu || {})[sv], 1)} vCPU per ${sv} pod)` : "";
  }
  if (b) {
    $("cpu-left").textContent = `${num(b.vcpu_left)} vCPU left after Workers`;
    $("cpu-left").title = `${num(b.alloc_vcpu, 1)} vCPU allocatable on Ready G4 + N4D nodes − ${num(b.non_backfill_vcpu, 1)} requested by non-backfill pods (vLLM, daemonsets, Workers) − ${b.pending_workers} Pending Workers × 2 vCPU. UI clamp only; preemption enforces it in the cluster.`;
  }
}

/* ------------------------------------------------------------------ Agent Substrate panel (recipe "substrate") */
// One quiet row of numbers above the node map. Every number tolerates absence: Substrate may not be installed yet, the
// probe may not have published actor-stats, so a missing value reads "no data yet", never an error.
function renderSubstrate(s) {
  const host = $("sub");
  if (!isSubstrate(s)) { host.hidden = true; $("sub-body").innerHTML = ""; $("sub-cp").innerHTML = ""; return; }
  host.hidden = false;
  const sub = s.substrate || {}, pools = sub.pools || {}, a = sub.actors || null, hpa = sub.n4d_hpa || null;
  const notes = (sub.notes || []).join("\n");
  const g4 = pools["g4-workers"], n4 = pools["n4d-workers"];
  const nd = '<span class="nd" title="no data yet">–</span>';
  // G4 / N4D pairs: each number carries its word (and the pool colour), never colour alone.
  const pair = (x, y, unit = "") => (x == null && y == null ? nd
    : `${x == null ? "–" : esc(x)}<small class="pg"> G4</small> <span class="sep">·</span> ${y == null ? "–" : esc(y)}<small class="pn"> N4D${unit}</small>`);
  const poolVal = (p) => (!p ? nd : p.exists ? `${num(p.ready ?? p.pods_ready)}<small> / ${num(p.desired)} ready</small>` : `${num(p.pods_ready)}<small> ready · no WorkerPool</small>`);
  const runG = a ? (a.running_g4 ?? a.actors_g4) : null, runN = a ? (a.running_n4d ?? a.actors_n4d) : null;
  const cells = [
    ["Actors running", a ? pair(runG, runN) : nd, a ? `${num(a.actors_running)} awake of ${num(a.actors_total)}` : "from actor-stats",
      a && a.actors_other ? `${a.actors_other} on other nodes` : "", "hero"],
    ["g4-workers", poolVal(g4) + (g4 && g4.pending ? ` <span class="flag warn">▲ ${g4.pending} Pending</span>` : ""),
      g4 ? (g4.pending ? "no room left on G4" : `${num(g4.on_g4)} on G4 spare cores`) : "WorkerPool on G4",
      g4 ? `WorkerPool g4-workers: spec ${num(g4.desired)}, status ${num(g4.replicas)} (${num(g4.ready)} ready); ${g4.pods} pods, ${g4.pending} Pending, ${num(g4.vcpu)} vCPU running` : ""],
    ["n4d-workers", poolVal(n4), hpa ? `HPA wants <b>${num(hpa.desired)}</b> (${num(hpa.min)}–${num(hpa.max)})` : "HPA: –",
      hpa ? `${hpa.name} → ${hpa.target}: metric ${num(hpa.metric, 1)}, current ${num(hpa.current)}, desired ${num(hpa.desired)}` : ""],
    ["Resumes/s", a && a.resumes_per_s != null ? num(a.resumes_per_s, 1) : nd,
      [a && a.resume_p50_ms != null ? `p50 ${num(a.resume_p50_ms)} ms` : "",
       // Neutral: under oversubscription Substrate refuses a resume when the pool is full and the actor waits.
       a && a.resumes_waited_no_room != null ? `<span title="resumes Substrate refused because every Worker was busy; the actor waited (expected when awake actors exceed Workers). Cumulative since actor-load started.">waited for a Worker ${num(a.resumes_waited_no_room)}</span>` : ""].filter(Boolean).join("<br>"),
      a && a.failed_requests ? `${a.failed_requests} other failed requests (cumulative)` : ""],
    ["Resume p99", a ? pair(a.resume_p99_g4_ms != null ? num(a.resume_p99_g4_ms) : null, a.resume_p99_n4d_ms != null ? num(a.resume_p99_n4d_ms) : null, " ms") : nd,
      a && a.resume_p99_ms != null ? `all pools ${num(a.resume_p99_ms)} ms` : "", ""],
    ["Lost actors", a && a.lost_actors != null ? (a.lost_actors > 0 ? `<span class="bad">✕ ${num(a.lost_actors)}</span>` : "0") : nd,
      [a && a.memory_resets != null ? `<span class="${a.memory_resets > 0 ? "warn" : ""}" title="resumed on a Worker with a different CPU feature set; files kept, process memory restarted">${a.memory_resets > 0 ? "▲ " : ""}memory resets ${num(a.memory_resets)}</span>` : "",
       a && a.age_s != null ? `updated ${Math.round(a.age_s)} s ago` : ""].filter(Boolean).join("<br>"),
      `Lost = actors Substrate reports CRASHED (cumulative)${a && a.updated ? ` · updated ${a.updated}` : ""}`],
  ];
  const head = `<h3 class="sub-h" id="sub-h">Agent Substrate</h3>`;
  const empty = sub.installed ? "WorkerPools, the n4d-workers HPA and actor-stats are not published yet."
    : (sub.control_plane || {}).present ? "No WorkerPools yet: workerpools.ate.dev is not readable in substrate-exp."
    : "Substrate is not installed on this cluster yet. Install it and the WorkerPools in substrate-exp to fill this panel.";
  const body = !sub.has_data
    ? `<p class="sub-empty" title="${esc(notes)}">${esc(empty)}${notes ? ` <span class="nd">${esc(notes.split("\n")[0])}</span>` : ""}</p>`
    : `<dl class="sub-cells">${cells.map(([k, v, sm, t, cls]) => `<div class="sub-c${cls ? ` ${cls}` : ""}"${t ? ` title="${esc(t)}"` : ""}><dt>${esc(k)}</dt><dd>${v}</dd><dd class="sm">${sm}</dd></div>`).join("")}</dl>` +
      (notes ? `<p class="sub-notes" title="${esc(notes)}">${esc(notes.split("\n")[0])}${sub.notes.length > 1 ? ` (+${sub.notes.length - 1} more, hover)` : ""}</p>` : "");
  $("sub-body").innerHTML = `<div class="sub-row">${head}<div class="sub-main">${body}</div></div>`;
  $("sub-cp").innerHTML = cpStripHTML(sub.control_plane);
  renderActorLoad(sub);
}

/* ------------------------------------------------------------------ service lanes */
// One lane per service. Chips are keyed by position, so when demand spills or the guard evicts,
// a chip visibly slides from the G4 zone to the N4D zone (FLIP animation).
const lanes = {};
(function initLanes() {
  const host = $("lanes");
  for (const sv of SVCS) {
    host.insertAdjacentHTML("beforeend", `<span class="lane-n a-${sv}"><i class="dotc" aria-hidden="true"></i>${sv}</span><div class="zone g" id="zg-${sv}" aria-label="${sv} on G4"><span class="wait" id="zw-${sv}" hidden></span></div><div class="zone n" id="zn-${sv}" aria-label="${sv} on N4D"><span class="hpa-tag" id="zt-${sv}" hidden></span></div>`);
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
    const native = isNative(s);
    // Native: backfill pods with no G4 slot wait Pending by design (the overflow HPA covers them), so they are a
    // calm note, not amber "pending" chips.
    const pg = native ? 0 : Math.max(0, (svc.backfill.desired || 0) - g.length), pn = Math.max(0, (svc.overflow.desired || 0) - nn.length);
    const slotsList = [...g.map((x) => ["g", x]), ...Array(pg).fill(["g", null]), ...nn.map((x) => ["n", x]), ...Array(pn).fill(["n", null])];
    gTot += g.length; nTot += nn.length; pend += pg + pn;
    const chips = lanes[sv];
    while (chips.length < slotsList.length) { const c = document.createElement("span"); chips.push(c); }
    while (chips.length > slotsList.length) chips.pop().remove();
    const zg = $(`zg-${sv}`), zn = $(`zn-${sv}`);
    zg.setAttribute("aria-label", L(`${sv} on G4`));
    chips.forEach((c, i) => {
      const [z, p] = slotsList[i];
      c.className = `pod a-${sv}${p ? (p.ready ? "" : " starting") : " pending"}`;
      c.title = p ? `${p.name} · ${p.tier || "pod"} on ${p.short}${p.ready ? "" : " · starting"}` : "pending: waiting for a node";
      (z === "g" ? zg : zn).appendChild(c);
    });
    const w = $(`zw-${sv}`), tg = $(`zt-${sv}`);
    const waiting = native ? (svc.backfill.waiting || 0) : 0, hpa = native ? svc.overflow.hpa : null;
    if (native && SVC_CLASS[sv] === "memory-heavy") {
      w.hidden = false; w.textContent = L("kept off G4 · admission policy");
      w.title = L("Memory-heavy services never backfill on this cluster: a ValidatingAdmissionPolicy bans them from G4, so analytics-overflow holds the whole demand.");
    } else {
      w.hidden = !waiting;
      w.textContent = `+${waiting} waiting → N4D`;
      w.title = L(`${waiting} ${sv}-backfill pod${waiting === 1 ? "" : "s"} found no free G4 slot next to vLLM and wait Pending at priority -100 (by design: the autoscaler ignores them). The ${sv}-overflow HPA runs the same number on N4D.`);
    }
    tg.hidden = !hpa;
    if (hpa) { tg.textContent = `HPA ${num(hpa.metric)}→${num(hpa.desired)}`; tg.title = `${hpa.name}: colo_overflow_needed = ${num(hpa.metric)} → ${num(hpa.desired)} replicas (min ${hpa.min}, max ${hpa.max})`; }
    zg.appendChild(w); zn.appendChild(tg);
  }
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
    const na = unavailable(b.dataset.scenario);
    b.classList.toggle("active", sc.name === b.dataset.scenario && running);
    b.classList.toggle("na", !!na);
    b.disabled = running || !!na;
    if (!b.dataset.title) b.dataset.title = b.title;
    b.title = na || L(b.dataset.title);
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
    `<b>Saved vs CPU on N4D</b>: without colo, every CPU pod running now (${cf.pods ?? "–"} pods: G4 backfill plus N4D overflow${cf.workers ? `, incl. ${cf.workers} Agent Substrate Workers` : ""}; ${num(cf.vcpu, 1)} vCPU, ${num(cf.mem_gib, 1)} GiB requested) would run on N4D. Packed first-fit-decreasing onto n4d-standard-16 nodes (${num(cf.node_cpu, 2)} vCPU, ${num(cf.node_mem_gib, 1)} GiB usable each after daemonsets) they need ${cf.n4d_nodes ?? "–"} nodes. ${cf.n4d_nodes_now ?? "–"} N4D nodes run today, so colo saves ${cf.saved_nodes ?? "–"} × ${money(p.n4d16_hr)}/hr = ${money(cf.saved_per_hour)}/hr. Free room on running N4D nodes is counted, and the warm buffer is treated as free.`,
    `<b>Fleet now</b>: ${(s.counts || {}).g4 || 0} G4 × ${money(p.g4_hr)} + ${(s.counts || {}).n4d || 0} N4D × ${money(p.n4d16_hr)} = ${money(c.per_hour)}/hr. Without colo: same G4 + ${cf.n4d_nodes ?? "–"} N4D = ${money(cf.fleet_per_hour)}/hr.`,
    `<b>Reclaimed</b>: CPU requests of Running backfill pods on Ready G4 nodes. <b>Spare</b>: G4 allocatable minus vLLM and system-pod requests.`,
    `<b>Core map</b>: one tile per vCPU from the node agent; columns are the physical cores of each L3 cache domain as the node reports them (8/8/8 on a G4 host, 8/7/7 on a TPU v6e host), rows the two hyperthreads. Fill height is the CPU's busy %. Grey tiles are the kubelet reserve plus daemonset requests.`,
    `<b>Prices</b> are list unit prices set in the demo config (DEMO_PRICE_*), not a quote.`,
    isNative(s)
      ? `<b>Control on this cluster</b>: no controller. <i>svc</i>-backfill replicas = demand; a backfill pod binds only next to a vLLM pod (podAffinity), so a G4 node is eligible iff it runs a Ready vLLM pod. Pods with no slot wait Pending at priority -100, and the <i>svc</i>-overflow HPA (metric colo_overflow_needed, min 1) runs them on N4D. Analytics never backfills (admission policy).`
      : `<b>Control on this cluster</b>: colo-controller splits demand into G4 backfill and N4D overflow every 2 s and runs the TTFT guard.`,
    ...(isSubstrate(s) ? [`<b>Agent Substrate</b>: Worker pods (label ate.dev/worker-pool, namespace substrate-exp) are drawn as W tiles. g4-workers run on G4 spare cores next to vLLM at a low priority (colo-backfill on the controller base; substrate-worker, bound by podAffinity, on the native base) and count as reclaimed; n4d-workers run on N4D, scaled by an HPA. Pool numbers are the WorkerPool (ate.dev/v1) spec and status; actor numbers come from the probe's ConfigMap substrate-exp/actor-stats (stats.json): actors per node are summed by node type, resume p99 is per pool. Missing objects show "no data yet".`, `<b>Substrate control plane</b>: nodes labelled ate.dev/workloadType=ate-system-infra or ate-postgres, one tile per ate-system pod (C control plane, P postgres, D DaemonSet; "!" = NotReady). Health = ready/desired of the ate-system Deployments/StatefulSet matched by name (podcert: Deployment podcertificate-controller in podcertificate-controller-system), and the atelet DaemonSet across nodes.`] : []),
  ].map((x) => `<li>${L(x)}</li>`).join("");
  const ver = (s.demo || {}).version;
  $("cluster").textContent = `${s.cluster.label ? s.cluster.label + " · " : ""}${s.cluster.name} · ${s.cluster.region} · ${s.cluster.project}${ver ? ` · demo v${ver}` : ""}`;
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
    const open = document.querySelector("#c-scale").closest("details").open;
    if (open) {
      set("c-ttft", [col("p99"), col("p50"), col("thr"), col("base")]);
      set("c-rps", [col("rps"), col("target")]);
      set("c-scale", [col("vr"), col("g4"), col("n4d")]);
      set("c-svc", [col("bf"), col("of")]);
      set("c-cost", [col("cost"), col("cf")]);
    }
  } catch (e) { console.error("chart update failed", e); }
}

/* ------------------------------------------------------------------ poll loop */
let inflight = false;
let repoll = false;
async function poll() {
  if (inflight) { repoll = true; return; }
  inflight = true;
  const cid = CLUSTER;
  try {
    const s = await fetchJSON(api("/api/state"));
    if (cid !== CLUSTER) return; // the presenter switched clusters while this request was in flight
    last = s; lastGoodAt = Date.now();
    setAccelerator(s.accelerator || (clusterById(cid) || {}).accelerator_profile);
    session = { auth: false, net: null, server: null };
    $("accel").hidden = !(s.demo && s.demo.accelerator_active);
    const parts = [
      ["control", renderControl], ["story", renderStory], ["controls", syncControls], ["rail", renderRail], ["numbers", renderNumbers], ["evictions", trackEvictions],
      ["substrate", renderSubstrate], ["cpuclamp", renderCpuClamp], ["nodes", renderNodes], ["lanes", renderLanes], ["scenario", () => renderScenario(s.scenario)], ["events", renderEvents], ["method", renderMethod],
      ["history", (x) => { pushHistory(x); renderSparks(); renderCharts(); }],
    ];
    for (const [name, fn] of parts) {
      try { fn(s); } catch (e) { console.error(`render ${name} failed`, e); } // one bad panel never blanks the page
    }
  } catch (e) {
    if (cid === CLUSTER) {
      if (e instanceof AuthError) session.auth = true;
      else if (e.net) session.net = e.message;
      else session.server = e.message;
    }
  } finally {
    inflight = false;
    renderConn(); renderAlerts();
    if (repoll || cid !== CLUSTER) { repoll = false; poll(); }
  }
}

/* ------------------------------------------------------------------ cluster selector */
function renderClusterSeg() {
  const seg = $("cluster-seg");
  seg.hidden = CLUSTERS.length < 2;
  seg.innerHTML = CLUSTERS.map((c) => `<button type="button" role="radio" data-cluster="${esc(c.id)}" aria-checked="${c.id === CLUSTER}" ` +
    `title="${esc(`${c.name} · ${(c.accelerator_profile || ACC_G4).name} · control: ${c.control}`)}">${esc(c.label)}</button>`).join("");
  seg.querySelectorAll("[data-cluster]").forEach((b) => b.addEventListener("click", () => switchCluster(b.dataset.cluster)));
  seg.onkeydown = (e) => { // arrow keys move between the radios
    if (!["ArrowLeft", "ArrowRight"].includes(e.key) || CLUSTERS.length < 2) return;
    const i = CLUSTERS.findIndex((c) => c.id === CLUSTER), n = CLUSTERS.length;
    const next = CLUSTERS[(i + (e.key === "ArrowRight" ? 1 : n - 1)) % n];
    switchCluster(next.id); seg.querySelector(`[data-cluster="${next.id}"]`)?.focus(); e.preventDefault();
  };
}
function resetView() {
  // Everything drawn so far belongs to the other cluster.
  history.length = 0; last = null; lastGoodAt = 0; session = { auth: false, net: null, server: null };
  for (const k of Object.keys(touched)) delete touched[k];
  for (const k of Object.keys(evictedUntil)) delete evictedUntil[k];
  seenEvents.clear(); firstEvents = true; dismissedScenario = null; alertsKey = ""; connKey = "";
  nodeEls.clear(); $("nodes").innerHTML = ""; $("sub-body").innerHTML = ""; $("sub-cp").innerHTML = ""; $("sub").hidden = true;
  for (const sv of SVCS) { lanes[sv].forEach((c) => c.remove()); lanes[sv] = []; }
  $("scen-banner").hidden = true;
  renderSparks(); renderCharts();
}
function switchCluster(id, push = true) {
  if (!clusterById(id) || id === CLUSTER) return;
  CLUSTER = id;
  setAccelerator(clusterById(id).accelerator_profile);
  try { localStorage.setItem(CLUSTER_KEY, id); } catch (e) { /* private mode */ }
  if (push) { const u = new URL(location.href); u.searchParams.set("cluster", id); window.history.replaceState(null, "", u); }
  renderClusterSeg(); resetView(); renderConn(); poll();
}
async function initClusters() {
  try {
    const j = await fetchJSON("/api/clusters");
    CLUSTERS = j.clusters || [];
    let saved = null;
    try { saved = localStorage.getItem(CLUSTER_KEY); } catch (e) { /* private mode */ }
    if (!clusterById(CLUSTER)) CLUSTER = clusterById(saved) ? saved : j.default;
    setAccelerator((clusterById(CLUSTER) || {}).accelerator_profile);
    try { localStorage.setItem(CLUSTER_KEY, CLUSTER); } catch (e) { /* private mode */ }
    const u = new URL(location.href);
    if (CLUSTERS.length > 1 && u.searchParams.get("cluster") !== CLUSTER) { u.searchParams.set("cluster", CLUSTER); window.history.replaceState(null, "", u); }
  } catch (e) {
    CLUSTER = null; // old server or auth trouble: poll() reports it; the API default cluster is used
  }
  renderClusterSeg();
}

// Capture the G4 originals of the data-acc markup and the scenario button tooltips before anything rewrites them.
document.querySelectorAll("[data-scenario]").forEach((b) => { b.dataset.title = b.title; });
initCharts();
localizeStatic();
document.querySelectorAll("details").forEach((d) => d.addEventListener("toggle", () => { if (last) renderCharts(); }));
// Remember the one-screen height of the live body row while no bottom drawer is open; the CSS keeps it when one opens.
const PANE = document.querySelector(".pane"), BODY = document.querySelector(".pane > .body");
function lockBodyHeight() {
  if (!document.querySelector(".details > details[open]")) PANE.style.setProperty("--body-h", `${Math.round(BODY.getBoundingClientRect().height)}px`);
}
new ResizeObserver(lockBodyHeight).observe(BODY);
addEventListener("resize", () => requestAnimationFrame(lockBodyHeight));
lockBodyHeight();
initClusters().then(() => { poll(); setInterval(poll, POLL_MS); });
