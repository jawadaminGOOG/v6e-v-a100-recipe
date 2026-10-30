/* G4 colo demo frontend. No build step, no CDN: Chart.js is bundled in /static and optional. */
"use strict";

const POLL_MS = 2000;
const STALE_MS = 6000;
const HISTORY_MS = 15 * 60 * 1000;
const SVCS = ["sandbox", "toolcall", "analytics"];
const CSS = getComputedStyle(document.documentElement);
const C = (n) => CSS.getPropertyValue(n).trim();
const COL = { v: C("--v"), i: C("--i"), m: C("--m"), bad: C("--bad"), muted: C("--muted"), faint: C("--faint"), line: C("--line"), n4d: C("--n4d"), ink: C("--ink") };
const CAT = { vllm: "v", integer: "i", memory: "m", buffer: "b", system: "s", shared: "idle" };
const CAT_ORDER = { v: 0, i: 1, m: 2, b: 3, s: 4, idle: 5 };
const LETTER = { v: "V", i: "I", m: "M", b: "B", s: "", idle: "" };

const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const num = (v, d = 0) => (v === null || v === undefined || Number.isNaN(+v) ? "–" : Number(v).toFixed(d));
const money = (v) => (v === null || v === undefined ? "–" : `$${Number(v).toFixed(2)}`);
const hhmmss = (t) => new Date(t).toLocaleTimeString([], { hour12: false });

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
  else if (age !== null) { cls = "ok"; text = last && last.mode === "mock" ? "Live (mock)" : "Live"; }
  $("conn-pill").className = `pill status ${cls}`;
  if (connKey !== text) { $("conn").textContent = text; connKey = text; } // announce transitions only
  $("conn-age").textContent = age !== null ? `${Math.max(0, Math.round(age / 1000))} s ago` : "";
}
setInterval(() => { renderConn(); renderAlerts(); renderClock(); }, 1000);

/* ------------------------------------------------------------------ controls */
function debounce(fn, ms) { let h; return (...a) => { clearTimeout(h); h = setTimeout(() => fn(...a), ms); }; }
const sendRate = debounce((v) => act("/api/gpu-rate", { rate: v }, `GPU rate set to ${v} rps`), 400);
$("rate").addEventListener("input", (e) => { touched.rate = Date.now(); $("rate-out").textContent = e.target.value; sendRate(+e.target.value); });
const demandValues = () => Object.fromEntries(SVCS.map((s) => [s, +$(`d-${s}`).value]));
const sendDemand = debounce(() => { const d = demandValues(); act("/api/demand", d, `Demand: ${SVCS.map((s) => `${s} ${d[s]}`).join(", ")}`); }, 400);
SVCS.forEach((s) => $(`d-${s}`).addEventListener("input", (e) => { touched.demand = Date.now(); $(`d-${s}-out`).textContent = e.target.value; sendDemand(); }));
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
  try { localStorage.setItem("colo-presenter", on ? "1" : "0"); } catch (_) { /* private mode */ }
  Object.values(charts).forEach((c) => c && c.resize && c.resize());
}
$("presenter").addEventListener("click", () => setPresenter(!document.documentElement.classList.contains("presenter")));
document.addEventListener("keydown", (e) => {
  if (e.ctrlKey || e.metaKey || e.altKey || /INPUT|TEXTAREA|SELECT/.test(e.target.tagName)) return;
  const map = { 1: "gpu-scale-out", 2: "gpu-scale-in", 3: "cpu-scale-out", 4: "guard-integer-vs-memory" };
  if (map[e.key]) startScenario(map[e.key]);
  else if (e.key === "p" || e.key === "P") setPresenter(!document.documentElement.classList.contains("presenter"));
});

const fresh = (k) => touched[k] && Date.now() - touched[k] < 8000;
function syncControls(s) {
  const c = s.controller || {};
  if (!fresh("rate") && s.loadgen && s.loadgen.target_rps !== undefined) { $("rate").value = Math.round(s.loadgen.target_rps); $("rate-out").textContent = Math.round(s.loadgen.target_rps); }
  if (!fresh("demand") && c.demand) SVCS.forEach((sv) => { if (c.demand[sv] !== undefined) { $(`d-${sv}`).value = c.demand[sv]; $(`d-${sv}-out`).textContent = c.demand[sv]; } });
  if (!fresh("guard") && c.guard_enabled !== undefined && c.guard_enabled !== null) $("guard").checked = !!c.guard_enabled;
  $("guard-label").textContent = `Latency guard ${c.guard_enabled ? "on" : "off"}`;
  $("baseline-info").textContent = c.baseline_ttft_p99_ms ? `baseline ${num(c.baseline_ttft_p99_ms)} ms · threshold ${num(c.threshold_ms)} ms` : "no baseline yet";
  const running = s.scenario && s.scenario.status === "running";
  $("manual-fields").disabled = running;
  $("manual-lock").hidden = !running;
}

/* ------------------------------------------------------------------ story strip */
function renderStory(s) {
  const st = s.story || {};
  const cf = (s.cost || {}).counterfactual || {};
  $("h-slice").textContent = num(st.gpu_slice_vcpu);
  const g4n = (s.counts || {}).g4_ready || 0;
  $("h-slice-note").textContent = `reserved for vLLM on each G4 node (${g4n} ${g4n === 1 ? "node" : "nodes"})`;
  $("h-reclaim").textContent = num(st.reclaimed_vcpu);
  $("h-reclaim-note").textContent = st.reclaimed_pct_of_spare !== null && st.reclaimed_pct_of_spare !== undefined
    ? `${num(st.reclaimed_pct_of_spare)}% of spare G4 cores · ${st.reclaimed_pods} CPU pods` : "no spare G4 cores yet";
  $("h-overflow").textContent = num(st.overflow_pods);
  const n4d = st.n4d_nodes || 0;
  $("h-overflow-note").textContent = `${num(st.overflow_vcpu)} vCPU on ${n4d} N4D ${n4d === 1 ? "node" : "nodes"}${n4d && !st.overflow_pods ? " (warm buffer only)" : ""}`;
  $("h-saved").textContent = money(st.saved_per_hour);
  $("h-saved-note").textContent = cf.n4d_nodes !== undefined
    ? `${cf.n4d_nodes} fewer n4d-standard-16 · ${num(st.saved_pct)}% of the all-N4D bill` : "";

  const total = st.g4_alloc_vcpu || 0;
  const segs = [
    ["v", "GPU slice", st.gpu_slice_total_vcpu],
    ["i", "Reclaimed", st.reclaimed_vcpu],
    ["b", "Warm buffer", st.g4_buffer_vcpu],
    ["s", "System", st.g4_system_vcpu],
    ["idle", "Idle", st.g4_idle_vcpu],
  ].filter(([, , v]) => v > 0.05);
  $("budget-total").textContent = total ? `${num(total)} allocatable vCPU on ${g4n} G4 ${g4n === 1 ? "node" : "nodes"}` : "no Ready G4 node";
  $("budget-bar").innerHTML = segs.map(([k, label, v]) => `<span class="seg-${k}" style="flex-grow:${v}" title="${esc(label)} ${num(v, 1)} vCPU">${v / (total || 1) > 0.07 ? `${esc(label)} ${num(v)}` : ""}</span>`).join("");
  $("budget-bar").setAttribute("aria-label", `G4 vCPU budget: ${segs.map(([, l, v]) => `${l} ${num(v)}`).join(", ")}`);
  $("budget-key").innerHTML = segs.map(([k, label, v]) => `<li><i class="seg-${k}" aria-hidden="true"></i>${esc(label)} <b>${num(v)}</b></li>`).join("");
}

/* ------------------------------------------------------------------ nodes */
function nodeCells(n) {
  if (n.cpus && n.cpus.length) {
    return n.cpus.map((c) => ({ k: CAT[c.cat] || "idle", pending: false })).sort((a, b) => CAT_ORDER[a.k] - CAT_ORDER[b.k]);
  }
  const cells = [];
  for (const p of n.pods || []) {
    const k = CAT[p.cat] || "s";
    for (let i = 0; i < Math.max(1, Math.round(p.cpu || 0)); i++) cells.push({ k, pending: !p.ready });
  }
  const cap = n.ncpu || Math.round(n.alloc_cpu || 0);
  while (cells.length < cap) cells.push({ k: "idle", pending: false });
  return cells.sort((a, b) => CAT_ORDER[a.k] - CAT_ORDER[b.k]);
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

function renderNodes(s) {
  const thr = (s.controller || {}).threshold_ms;
  const order = { g4: 0, n4d: 1 };
  const nodes = s.nodes.filter((n) => n.role in order).sort((a, b) => order[a.role] - order[b.role] || (b.age_s ?? 0) - (a.age_s ?? 0));
  const idx = { g4: 0, n4d: 0 };
  const html = nodes.map((n) => {
    idx[n.role] += 1;
    const cells = nodeCells(n);
    const cnt = { v: 0, i: 0, m: 0, b: 0, s: 0, idle: 0 };
    cells.forEach((c) => (cnt[c.k] += 1));
    const pods = { i: 0, m: 0 };
    (n.pods || []).forEach((p) => { if (p.cat === "integer") pods.i++; else if (p.cat === "memory") pods.m++; });
    const breach = n.role === "g4" && thr && n.ttft_p99_ms && n.ttft_p99_ms > thr;
    const chips = [];
    if (!n.ready) chips.push(`<span class="chip warn">starting up</span>`);
    if (n.role === "g4" && n.ttft_p99_ms !== null && n.ttft_p99_ms !== undefined) chips.push(`<span class="chip ${breach ? "bad" : ""}">TTFT p99 ${num(n.ttft_p99_ms)} ms${breach ? " · over threshold" : ""}</span>`);
    (n.blocks || []).forEach((b) => chips.push(`<span class="chip bad">guard: no ${esc(b)}</span>`));
    const label = n.role === "g4" ? `G4 #${idx.g4}` : `N4D #${idx.n4d}`;
    const type = n.instance_type || (n.role === "g4" ? "g4-standard-48" : "n4d-standard-16");
    const aria = `${label}, ${type}: ${cnt.v} vCPU vLLM, ${cnt.i} integer, ${cnt.m} memory-heavy, ${cnt.b} buffer, ${cnt.s + cnt.idle} system or idle`;
    const slots = cells.map((c) => `<i class="cell ${c.k}${c.pending ? " pending" : ""}">${LETTER[c.k]}</i>`).join("");
    const sum = n.role === "g4"
      ? `<span><b>${cnt.v}</b> vCPU vLLM</span><span><b>${pods.i}</b> integer pods</span><span><b>${pods.m}</b> memory-heavy pods</span>${n.achieved_rps ? `<span><b>${num(n.achieved_rps)}</b> rps</span>` : ""}`
      : `<span><b>${pods.i + pods.m}</b> overflow pods</span><span><b>${cnt.b}</b> vCPU warm buffer</span><span><b>${cnt.idle}</b> vCPU free</span>`;
    const ev = evictedUntil[n.name] && evictedUntil[n.name] > Date.now();
    return `<div class="node ${n.role}${breach ? " breach" : ""}${ev ? " evicted" : ""}">` +
      `<div class="node-id"><span class="role ${n.role}">${label}</span><span class="meta" title="${esc(n.name)}">${esc(type)} · ${esc(n.zone || "")}</span><span class="chips">${chips.join("")}</span></div>` +
      `<div class="slots" role="img" aria-label="${esc(aria)}" style="--cols:24">${slots}</div>` +
      `<div class="node-sum">${sum}</div></div>`;
  }).join("");
  $("nodes").innerHTML = html || `<p class="empty">No G4 or N4D nodes reported yet.</p>`;
}

function renderTopology(s) {
  if (!document.querySelector("#topology").closest("details").open) return;
  const out = s.nodes.filter((n) => n.role === "g4" || n.role === "n4d").map((n) => {
    if (!n.cpus.length) return `<div class="topo-node"><h3>${esc(n.role.toUpperCase())} ${esc(n.name)}</h3><p class="empty">Node agent not reporting yet.</p></div>`;
    const half = n.ncpu / 2, nllc = Math.ceil(half / 8);
    let html = `<div class="llcs" style="grid-template-columns:repeat(${nllc},1fr)">`;
    for (let l = 0; l < nllc; l++) {
      const cores = [...Array(8).keys()].map((i) => l * 8 + i).filter((c) => c < half);
      const inL = cores.flatMap((c) => [n.cpus[c], n.cpus[c + half]]).filter(Boolean);
      const hot = inL.some((c) => c.cat === "vllm");
      let tiles = "";
      for (const t of [0, 1]) for (const c of cores) {
        const cpu = n.cpus[c + t * half];
        const k = cpu ? CAT[cpu.cat] || "idle" : "idle";
        const tip = cpu ? `CPU ${cpu.id}: ${cpu.owner ? `${cpu.owner.ns || ""}/${cpu.owner.pod}` : "shared pool"} · busy ${cpu.busy ?? "?"}%` : "";
        tiles += `<i class="cell ${k}" title="${esc(tip)}">${LETTER[k]}</i>`;
      }
      html += `<div class="llc${hot ? " hot" : ""}"><div class="llc-title">L3 domain ${l} <span>cores ${cores[0]}–${cores[cores.length - 1]}${hot ? " · vLLM" : ""}</span></div><div class="tiles">${tiles}</div></div>`;
    }
    return `<div class="topo-node"><h3>${esc(n.role.toUpperCase())} <span class="muted small">${esc(n.name)}</span></h3>${html}</div></div>`;
  }).join("");
  $("topology").innerHTML = out || `<p class="empty">No nodes.</p>`;
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
    `<b>Saved vs all-N4D</b>: the CPU pods now running on G4 spare cores (${cf.pods ?? "–"} pods, ${num(cf.vcpu, 1)} vCPU, ${num(cf.mem_gib, 1)} GiB requested) are packed first-fit-decreasing onto n4d-standard-16 nodes (${num(cf.node_cpu, 2)} vCPU, ${num(cf.node_mem_gib, 1)} GiB usable each). That needs ${cf.n4d_nodes ?? "–"} nodes at ${money(p.n4d16_hr)}/hr = ${money(cf.per_hour)}/hr. Colo avoids those nodes because the G4 node is already paid for.`,
    `<b>Fleet now</b>: ${(s.counts || {}).g4 || 0} G4 × ${money(p.g4_hr)} + ${(s.counts || {}).n4d || 0} N4D × ${money(p.n4d16_hr)} = ${money(c.per_hour)}/hr; all-N4D counterfactual ${money(cf.fleet_per_hour)}/hr.`,
    `<b>Reclaimed</b>: CPU requests of Running backfill pods on Ready G4 nodes. <b>Spare</b>: G4 allocatable minus vLLM and system-pod requests.`,
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
      scales: { x: { ticks: { maxTicksLimit: 6, maxRotation: 0 }, grid: { display: false } },
        y: { beginAtZero: true, title: { display: !!y, text: y }, grid: { color: COL.line }, ...extra } },
    });
    const ds = (label, color, o = {}) => ({ label, borderColor: color, backgroundColor: color, data: [], ...o });
    const mk = (id, y, sets, extra) => { const el = $(id); if (el) charts[id] = new Chart(el, { type: "line", options: opts(y, extra), data: { labels: [], datasets: sets } }); };
    mk("c-ttft", "ms", [ds("TTFT p99", COL.v), ds("guard threshold", COL.bad, { borderDash: [6, 4], borderWidth: 1.5 }), ds("baseline", COL.faint, { borderDash: [2, 3], borderWidth: 1.5 })]);
    mk("c-rps", "req/s", [ds("achieved", COL.v), ds("target", COL.faint, { borderDash: [5, 4], stepped: true, borderWidth: 1.5 })]);
    mk("c-scale", "count", [ds("vLLM ready", COL.v, { stepped: true }), ds("G4 nodes", COL.ink, { stepped: true, borderDash: [5, 4] }), ds("N4D nodes", COL.n4d, { stepped: true })], { ticks: { stepSize: 1 } });
    mk("c-svc", "pods", [ds("on G4 (reclaimed)", COL.i, { stepped: true }), ds("on N4D (overflow)", COL.n4d, { stepped: true, borderDash: [5, 4] })]);
    mk("c-cost", "$/hr", [ds("fleet now", COL.v, { stepped: true }), ds("all-N4D", COL.faint, { stepped: true, borderDash: [5, 4] })]);
  } catch (e) {
    console.error("chart init failed", e);
  }
}
function pushHistory(s) {
  const sv = Object.values(s.services || {});
  history.push({
    t: s.ts * 1000, p99: s.loadgen.ttft_p99_ms, base: s.controller.baseline_ttft_p99_ms, thr: s.controller.threshold_ms,
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
    set("c-ttft", [col("p99"), col("thr"), col("base")]);
    const moreOpen = document.querySelector("#c-rps").closest("details").open;
    if (moreOpen) {
      set("c-rps", [col("rps"), col("target")]);
      set("c-scale", [col("vr"), col("g4"), col("n4d")]);
      set("c-svc", [col("bf"), col("of")]);
      set("c-cost", [col("cost"), col("cf")]);
    }
  } catch (e) { console.error("chart update failed", e); }
  const lg = last && last.loadgen, thr = last && last.controller.threshold_ms;
  $("ttft-now").textContent = lg ? `p99 ${num(lg.ttft_p99_ms)} ms${thr ? ` · threshold ${num(thr)} ms` : ""}` : "";
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
    $("mode").hidden = s.mode !== "mock";
    $("accel").hidden = !(s.demo && s.demo.accelerator_active);
    const parts = [
      ["story", renderStory], ["controls", syncControls], ["evictions", trackEvictions], ["nodes", renderNodes],
      ["topology", renderTopology], ["scenario", () => renderScenario(s.scenario)], ["events", renderEvents], ["method", renderMethod],
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

try {
  const q = new URLSearchParams(location.search).get("presenter");
  if (q === "1" || (q === null && localStorage.getItem("colo-presenter") === "1")) setPresenter(true);
} catch (_) { /* ignore */ }
initCharts();
document.querySelectorAll("details").forEach((d) => d.addEventListener("toggle", () => { if (last) { renderTopology(last); renderCharts(); } }));
poll();
setInterval(poll, POLL_MS);
