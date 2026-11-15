// r7s web UI: read-only view of the active rig. No dependencies, no build.
// All data is inserted with textContent / setAttribute, never innerHTML.
import { layout, SIZES } from "./layout.js";

const SVGNS = "http://www.w3.org/2000/svg";
const $ = (id) => document.getElementById(id);
const store = {
  get(k, d) { try { return localStorage.getItem(k) ?? d; } catch { return d; } },
  set(k, v) { try { localStorage.setItem(k, v); } catch { /* private mode */ } },
};

const state = {
  graph: null,
  mode: new URLSearchParams(location.search).get("view") === "port" ? "port"
    : new URLSearchParams(location.search).get("view") === "plugin" ? "plugin" : store.get("r7s.mode", "plugin"),
  rig: new URLSearchParams(location.search).get("rig"),
  selected: null,
  view: null, // {x, y, w, h} viewBox, null = fit
  laid: null,
  events: [],
  showDebug: store.get("r7s.debug", "0") === "1",
};

function el(tag, attrs = {}, text) {
  const e = tag.startsWith("svg:") ? document.createElementNS(SVGNS, tag.slice(4)) : document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) if (v !== undefined && v !== null && v !== false) e.setAttribute(k, v);
  if (text !== undefined && text !== null) e.textContent = String(text);
  return e;
}

function fmtNum(v) {
  if (v === null || v === undefined) return "–";
  if (typeof v === "boolean") return v ? "on" : "off";
  if (typeof v === "number") return Math.abs(v) >= 1000 || Number.isInteger(v) ? String(v) : String(+v.toFixed(3));
  return String(v);
}
function ago(sec) {
  if (sec === null || sec === undefined) return "";
  if (sec < 60) return `${Math.round(sec)}s`;
  if (sec < 3600) return `${Math.round(sec / 60)}m`;
  return `${(sec / 3600).toFixed(1)}h`;
}
const clock = (t) => new Date(t * 1000).toLocaleTimeString([], { hour12: false });

// --- data -------------------------------------------------------------------

let inflight = false, again = false, lastFetch = 0, timer = null;
async function refresh() {
  if (inflight) { again = true; return; }
  inflight = true;
  try {
    const q = state.rig ? `?rig=${encodeURIComponent(state.rig)}` : "";
    const r = await fetch(`/api/graph${q}`, { cache: "no-store" });
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    state.graph = await r.json();
    render();
  } catch (e) {
    showProblems([`can't reach r7sd: ${e.message}`]);
  } finally {
    inflight = false;
    lastFetch = performance.now();
    if (again) { again = false; schedule(); }
  }
}
function schedule() {
  const wait = Math.max(0, 700 - (performance.now() - lastFetch));
  clearTimeout(timer);
  timer = setTimeout(refresh, wait);
}

async function loadEvents() {
  try {
    const since = Date.now() / 1000 - 6 * 3600;
    const r = await fetch(`/api/events?since=${since}`, { cache: "no-store" });
    const d = await r.json();
    state.events = d.events.slice(-200).reverse();
    renderEvents();
  } catch { /* the stream will catch up */ }
}

function connect() {
  const live = $("live");
  const es = new EventSource("/api/stream");
  es.addEventListener("open", () => { live.className = "live on"; live.title = "Live"; });
  es.addEventListener("error", () => { live.className = "live off"; live.title = "Disconnected; retrying"; });
  es.addEventListener("status", schedule);
  es.addEventListener("event", (m) => {
    const ev = JSON.parse(m.data);
    const head = state.events[0];
    if (head && head.type === ev.type && head.reason === ev.reason && head.object === ev.object && head.message === ev.message) {
      head.count = (head.count || 1) + 1; head.time = ev.time;
    } else {
      ev.fresh = true;
      state.events.unshift(ev);
      state.events.length = Math.min(state.events.length, 300);
    }
    renderEvents();
    schedule();
  });
}

// --- header -------------------------------------------------------------------

function renderHeader(g) {
  const sel = $("rig");
  sel.replaceChildren();
  if (!g.rigs.length) sel.append(el("option", { value: "" }, "no rigs"));
  for (const name of g.rigs) {
    const label = name === g.activeRig ? `● ${name}` : name;
    sel.append(el("option", { value: name, selected: name === g.rig ? "" : null }, label));
  }
  sel.disabled = !g.rigs.length;

  const phase = $("phase");
  phase.className = `badge ${g.phase || ""}`;
  phase.textContent = g.rig ? (g.active ? g.phase : "Inactive (not running)") : "no active rig";

  const stats = $("stats");
  stats.replaceChildren();
  const stat = (label, value, hot, title) => {
    const s = el("span", { class: hot ? "hot" : null, title });
    s.append(`${label} `, el("b", {}, value));
    stats.append(s);
  };
  if (g.rig && g.active) {
    stat("xruns", g.rigXruns ?? 0, (g.rigXruns ?? 0) > 0, "xruns while this rig was active");
    stat("DSP", g.cpuLoad === null || g.cpuLoad === undefined ? "–" : `${g.cpuLoad}%`, (g.cpuLoad ?? 0) > 80);
    if (g.drift) stat("drift", g.drift, false, "parameters whose live value differs from the spec (MIDI CC or a plugin UI)");
  }
  const j = g.jack || {};
  stat("JACK", j.connected ? `${j.sampleRate ?? "?"} Hz / ${j.bufferSize ?? "?"}` : "down", !j.connected,
       j.connected ? "sample rate / buffer size" : j.error || "not connected");
}

function showProblems(list) {
  const box = $("problems");
  if (!list.length) { box.hidden = true; return; }
  box.hidden = false;
  const ul = el("ul");
  for (const p of list) ul.append(el("li", {}, p));
  box.replaceChildren(ul);
}

// --- graph --------------------------------------------------------------------

function nodeState(n) {
  if (n.kind === "device") return n.present === true ? "present" : n.present === false ? "absent" : "unknown";
  return n.state || "Inactive";
}

function drawNode(n, mode) {
  const st = nodeState(n);
  const g = el("svg:g", {
    class: `node ${n.kind} state-${st}${st === "absent" ? " absent" : ""}${state.selected === n.id ? " selected" : ""}`,
    transform: `translate(${n.x},${n.y})`, tabindex: "0", role: "button",
    "aria-label": `${n.kind} ${n.label}, ${st}`, "data-id": n.id,
  });
  g.append(el("svg:rect", { class: "box", width: n.w, height: n.h, rx: n.kind === "device" ? 14 : 8 }));
  g.append(el("svg:rect", { class: "stripe", x: 0, y: 8, width: 4, height: n.h - 16, rx: 2 }));
  const side = n.kind === "device" ? (n.side === "in" ? " ▸" : " ◂") : "";
  g.append(el("svg:text", { class: "label", x: 14, y: 22 }, n.label + side));
  const sub = n.kind === "device"
    ? (n.present === false ? "not connected" : (n.endpoint || n.title || ""))
    : (n.title || "");
  g.append(el("svg:text", { class: "title", x: 14, y: 38 }, clip(sub, mode === "plugin" ? 28 : 32)));

  // chips: restarts, drift, CC, problems
  const chips = [];
  if (n.restarts) chips.push(["bad", `↻${n.restarts}`]);
  if (n.drift) chips.push(["warn", `≈${n.drift}`]);
  if (n.params && n.params.some((p) => p.cc)) chips.push(["info", "CC"]);
  if (n.kind === "plugin" && n.installed === false) chips.push(["bad", "missing"]);
  if (n.kind === "plugin" && n.state && n.state !== "Running" && n.state !== "Inactive") chips.push([n.state === "CrashLoop" ? "bad" : "warn", n.state]);
  if (n.kind === "device" && n.present === false) chips.push(["bad", "absent"]);
  const chipW = (text) => text.length * 6.6 + 10;
  if (mode === "plugin") {
    // Own row under the title, left to right.
    let x = 14;
    for (const [cls, text] of chips) {
      g.append(el("svg:text", { class: `chip ${cls}`, x, y: 55 }, text));
      x += chipW(text);
    }
  } else {
    // Port view: the header is busy, so right-align chips on the label row.
    let x = n.w - 10;
    for (const [cls, text] of [...chips].reverse()) {
      g.append(el("svg:text", { class: `chip ${cls}`, x, y: 22, "text-anchor": "end" }, text));
      x -= chipW(text);
    }
  }

  if (mode === "port") {
    const row = SIZES.port.row, top = SIZES.port.header;
    n.inputs.forEach((p, i) => {
      const y = top + i * row + row / 2;
      g.append(el("svg:circle", { class: "pin", cx: 0, cy: y, r: 4.5, fill: p.type === "midi" ? "var(--midi)" : "var(--audio)" }));
      g.append(el("svg:text", { class: "port", x: 10, y: y + 4 }, clip(p.symbol, 14)));
    });
    n.outputs.forEach((p, i) => {
      const y = top + i * row + row / 2;
      g.append(el("svg:circle", { class: "pin", cx: n.w, cy: y, r: 4.5, fill: p.type === "midi" ? "var(--midi)" : "var(--audio)" }));
      g.append(el("svg:text", { class: "port", x: n.w - 10, y: y + 4, "text-anchor": "end" }, clip(p.symbol, 14)));
    });
  }
  const tip = [n.title, n.uri, n.kind === "plugin" ? `state: ${n.state}` : `present: ${n.present}`,
               ...Object.values(n.errors || {})].filter(Boolean).join("\n");
  g.prepend(el("svg:title", {}, tip));
  g.addEventListener("click", (e) => { e.stopPropagation(); select(n.id); });
  g.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); select(n.id); } });
  return g;
}

function clip(s, n) { s = String(s ?? ""); return s.length > n ? s.slice(0, n - 1) + "…" : s; }

function drawEdge(e, mode) {
  const g = el("svg:g", { class: `edge ${e.section} ${e.state}` });
  const lines = e.members.map((m) => `${m.raw[0]} → ${m.raw[1]}: ${m.state}${m.reason ? ` (${m.reason})` : ""}`);
  g.append(el("svg:title", {}, lines.join("\n")));
  g.append(el("svg:path", { class: "hit", d: e.path }));
  g.append(el("svg:path", { class: "wire", d: e.path }));
  if (mode === "plugin" && e.label) {
    g.append(el("svg:text", { class: "elabel", x: e.lx, y: e.ly - 6, "text-anchor": "middle" }, clip(e.label, 30)));
  }
  return g;
}

function renderGraph() {
  const g = state.graph;
  const svg = $("graph");
  const empty = $("empty");
  if (!g.rig || !g.nodes.length) {
    svg.replaceChildren();
    empty.hidden = false;
    empty.replaceChildren(
      el("h2", {}, g.rig ? `Rig ${g.rig} has no plugins` : "No active rig"),
      el("p", {}, g.rigs.length ? "Pick a rig above, or activate one with an ActiveRig resource." :
        "Apply a Rig and an ActiveRig with r7sctl apply -f rig.yaml"),
    );
    state.laid = null;
    return;
  }
  empty.hidden = true;
  const laid = layout(g, state.mode);
  state.laid = laid;
  const root = el("svg:g", { id: "root" });
  const edges = el("svg:g", { class: "edges" });
  for (const e of laid.edges) edges.append(drawEdge(e, state.mode));
  const nodes = el("svg:g", { class: "nodes" });
  for (const n of laid.nodes) nodes.append(drawNode(n, state.mode));
  root.append(edges, nodes);
  svg.replaceChildren(root);
  applyView();
}

// --- pan / zoom ------------------------------------------------------------------

// Fit the graph, but never below MIN_SCALE: past that it's unreadable, so a
// wide rig keeps a readable size and you pan sideways instead.
const MAX_SCALE = 1.25;
const minScale = () => (window.innerWidth < 860 ? 0.45 : 0.7);
function fitBox() {
  const l = state.laid;
  if (!l) return { x: 0, y: 0, w: 800, h: 400 };
  const r = $("graph").getBoundingClientRect();
  const rw = r.width || 800, rh = r.height || 400;
  const scale = Math.min(MAX_SCALE, Math.max(minScale(), Math.min(rw / l.width, rh / l.height)));
  const w = rw / scale, h = rh / scale;
  return { x: l.width <= w ? (l.width - w) / 2 : 0, y: (l.height - h) / 2, w, h };
}
// When the canvas changes size (panel opens, window resizes), keep the
// user's zoom level instead of letting the stale viewBox rescale everything.
let lastRect = null;
function onResize() {
  const r = $("graph").getBoundingClientRect();
  if (state.view && lastRect && lastRect.width && lastRect.height) {
    const k = state.view.w / lastRect.width;
    state.view = { ...state.view, w: r.width * k, h: r.height * k };
  }
  lastRect = r;
  applyView();
}
function applyView() {
  const v = state.view || fitBox();
  $("graph").setAttribute("viewBox", `${v.x} ${v.y} ${v.w} ${v.h}`);
  $("graph").setAttribute("preserveAspectRatio", "xMidYMid meet");
}
function setupPanZoom() {
  const svg = $("graph");
  let drag = null;
  const toUser = (cx, cy) => {
    const v = state.view || fitBox();
    const r = svg.getBoundingClientRect();
    const s = Math.max(v.w / r.width, v.h / r.height);
    const ox = v.x + (v.w - r.width * s) / 2, oy = v.y + (v.h - r.height * s) / 2;
    return { x: ox + (cx - r.left) * s, y: oy + (cy - r.top) * s, s };
  };

  // One pointer pans; two pointers (touch) pinch-zoom around their midpoint.
  const touches = new Map();
  let pinch = null;
  const zoomAt = (cx, cy, k) => {
    const v = state.view || fitBox();
    const p = toUser(cx, cy);
    const w = Math.min(Math.max(v.w * k, 120), 20000), h = v.h * (w / v.w);
    state.view = { x: p.x - (p.x - v.x) * (w / v.w), y: p.y - (p.y - v.y) * (h / v.h), w, h };
    applyView();
  };
  svg.addEventListener("wheel", (e) => {
    if (!state.laid) return;
    e.preventDefault();
    zoomAt(e.clientX, e.clientY, Math.exp(e.deltaY * 0.0015));
  }, { passive: false });
  svg.addEventListener("pointerdown", (e) => {
    touches.set(e.pointerId, { x: e.clientX, y: e.clientY });
    if (touches.size === 2) {
      const [a, b] = [...touches.values()];
      pinch = { d: Math.hypot(a.x - b.x, a.y - b.y) };
      drag = null;
      return;
    }
    if (e.target.closest(".node")) return;
    drag = { x: e.clientX, y: e.clientY, v: { ...(state.view || fitBox()) }, s: toUser(0, 0).s };
    svg.classList.add("dragging");
    svg.setPointerCapture(e.pointerId);
  });
  svg.addEventListener("pointermove", (e) => {
    if (touches.has(e.pointerId)) touches.set(e.pointerId, { x: e.clientX, y: e.clientY });
    if (pinch && touches.size === 2) {
      const [a, b] = [...touches.values()];
      const d = Math.hypot(a.x - b.x, a.y - b.y);
      if (d > 0 && pinch.d > 0) zoomAt((a.x + b.x) / 2, (a.y + b.y) / 2, pinch.d / d);
      pinch.d = d;
      return;
    }
    if (!drag) return;
    state.view = { ...drag.v, x: drag.v.x - (e.clientX - drag.x) * drag.s, y: drag.v.y - (e.clientY - drag.y) * drag.s };
    applyView();
  });
  const end = (e) => {
    touches.delete(e.pointerId);
    if (touches.size < 2) pinch = null;
    drag = null;
    svg.classList.remove("dragging");
  };
  svg.addEventListener("pointerup", end);
  svg.addEventListener("pointercancel", end);
  svg.addEventListener("click", () => { if (state.selected) select(null); });
  $("fit").addEventListener("click", () => { state.view = null; applyView(); });
}

// --- details panel ----------------------------------------------------------------

function select(id) {
  state.selected = id;
  document.querySelector("main").classList.toggle("no-panel", !id);
  // Deep link: #p:drive selects a node (shareable, and handy for screenshots).
  history.replaceState(null, "", id ? `#${encodeURIComponent(id)}` : location.pathname + location.search);
  render();
}

function kv(pairs) {
  const dl = el("dl", { class: "kv" });
  for (const [k, v, cls] of pairs) {
    if (v === undefined || v === null || v === "") continue;
    dl.append(el("dt", {}, k), el("dd", { class: cls || null }, v));
  }
  return dl;
}

function renderPanel() {
  const g = state.graph;
  const n = g && g.nodes.find((x) => x.id === state.selected);
  const body = $("panel-body");
  if (!n) {
    $("panel-title").textContent = "Details";
    body.replaceChildren(el("p", { class: "muted" }, "Select a plugin or device."));
    return;
  }
  $("panel-title").textContent = n.label;
  const parts = [];
  if (n.kind === "plugin") {
    parts.push(kv([
      ["Plugin", n.title], ["URI", n.uri], ["Category", n.category],
      ["State", n.state, n.state === "CrashLoop" ? "err" : null], ["PID", n.pid],
      ["Uptime", ago(n.uptime)], ["Restarts", n.restarts || null],
      ["Last exit", n.lastExit], ["Error", n.lastError, "err"],
      ["Installed", n.installed === false ? "no: not found in plugin paths" : null, "err"],
    ]));
    const ins = n.inputs.map((p) => p.symbol).join(", "), outs = n.outputs.map((p) => p.symbol).join(", ");
    parts.push(el("h3", {}, "Ports"), kv([["In", ins || "–"], ["Out", outs || "–"]]));
    if (n.params.length) {
      parts.push(el("h3", {}, `Parameters (${n.params.length})`));
      const t = el("table", { class: "params" });
      const head = el("tr");
      for (const h of ["Parameter", "Live", "Spec", "Range"]) head.append(el("th", {}, h));
      t.append(el("thead", {}, null), el("tbody"));
      t.tHead.append(head);
      for (const p of n.params) {
        const tr = el("tr", { class: [p.drift ? "drift" : "", p.inSpec ? "" : "unset"].join(" ").trim() || null,
                              title: p.drift ? `live ${p.liveRaw} differs from the applied spec value ${p.applied}` : null });
        const name = el("td", {}, p.name || p.symbol);
        if (p.cc) name.append(el("span", { class: "cc", title: `CC ${p.cc.cc} ch ${p.cc.channel ?? "omni"} from ${p.cc.device}` }, `CC${p.cc.cc}`));
        const unit = p.unit ? ` ${p.unit}` : "";
        const range = p.labels ? p.labels.join(" | ") : p.toggle ? "toggle" :
          (p.min !== null || p.max !== null) ? `${fmtNum(p.min)} … ${fmtNum(p.max)}${unit}` : "";
        tr.append(name, el("td", { class: "num" }, fmtNum(p.live)), el("td", { class: "num" }, p.inSpec ? fmtNum(p.spec) : "default"),
                  el("td", { class: "muted" }, range));
        t.tBodies[0].append(tr);
      }
      parts.push(t);
    }
    if (n.log && n.log.length) parts.push(el("h3", {}, "Last output"), el("pre", { class: "log" }, n.log.join("\n")));
  } else {
    parts.push(kv([
      ["Device", n.label], ["Hardware", n.title], ["Side", n.side === "in" ? "input (capture)" : "output (playback)"],
      ["Endpoint", n.endpoint],
      ["Present", n.present === true ? "yes" : n.present === false ? "no" : "unknown", n.present === false ? "err" : null],
    ]));
    const errs = Object.entries(n.errors || {});
    if (errs.length) parts.push(el("h3", {}, "Problems"), kv(errs.map(([k, v]) => [k, v, "err"])));
    const ports = (n.side === "in" ? n.outputs : n.inputs).map((p) => p.symbol).join(", ");
    parts.push(el("h3", {}, "Ports used"), el("p", {}, ports || "–"));
  }
  const edges = g.edges.filter((e) => e.from === n.id || e.to === n.id);
  if (edges.length) {
    parts.push(el("h3", {}, "Connections"));
    const ul = el("ul");
    for (const e of edges) {
      const li = el("li", { class: e.state === "Unresolved" ? "err" : null }, `${e.raw[0]} → ${e.raw[1]} · ${e.state}`);
      if (e.reason) li.append(el("div", { class: "muted" }, e.reason));
      ul.append(li);
    }
    parts.push(ul);
  }
  body.replaceChildren(...parts);
}

// --- events ------------------------------------------------------------------------

function renderEvents() {
  const ol = $("events");
  const rows = [];
  for (const ev of state.events) {
    if (!state.showDebug && ev.priority === "debug") continue;
    const li = el("li", { class: ev.fresh ? "fresh" : null });
    ev.fresh = false;
    li.append(
      el("span", { class: "t" }, clock(ev.time)),
      el("span", { class: `p p-${ev.priority}` }, ev.priority),
      el("span", { class: "o", title: ev.object }, ev.object),
      el("span", { class: "m" }, `${ev.reason}: ${ev.message}${ev.count > 1 ? ` (×${ev.count})` : ""}`),
    );
    rows.push(li);
    if (rows.length >= 150) break;
  }
  ol.replaceChildren(...rows);
}

// --- top level ---------------------------------------------------------------------

function render() {
  const g = state.graph;
  if (!g) return;
  renderHeader(g);
  // Rig problems (CC routing, errors) belong to the running rig; when viewing
  // a stored, inactive rig only system-wide problems apply.
  const problems = g.active
    ? [...(g.errors || []), ...(g.problems || []), ...((g.midi && g.midi.problems) || [])]
    : [...(g.problems || [])];
  if (g.jack && !g.jack.connected) problems.unshift(`JACK: ${g.jack.error || "not connected"}`);
  showProblems(problems);
  renderGraph();
  renderPanel();
  $("view-plugin").setAttribute("aria-pressed", String(state.mode === "plugin"));
  $("view-port").setAttribute("aria-pressed", String(state.mode === "port"));
}

function init() {
  const fromHash = decodeURIComponent(location.hash.slice(1));
  state.selected = fromHash || null;
  document.querySelector("main").classList.toggle("no-panel", !state.selected);
  $("rig").addEventListener("change", (e) => {
    state.rig = e.target.value || null;
    const u = new URL(location.href);
    if (state.rig && state.rig !== state.graph?.activeRig) u.searchParams.set("rig", state.rig);
    else u.searchParams.delete("rig");
    history.replaceState(null, "", u);
    state.view = null;
    select(null);
    refresh();
  });
  for (const m of ["plugin", "port"]) {
    $(`view-${m}`).addEventListener("click", () => { state.mode = m; store.set("r7s.mode", m); state.view = null; render(); });
  }
  $("panel-close").addEventListener("click", () => select(null));
  $("ev-debug").checked = state.showDebug;
  $("ev-debug").addEventListener("change", (e) => { state.showDebug = e.target.checked; store.set("r7s.debug", e.target.checked ? "1" : "0"); renderEvents(); });
  document.addEventListener("keydown", (e) => { if (e.key === "Escape" && state.selected) select(null); });
  setupPanZoom();
  if ("ResizeObserver" in window) new ResizeObserver(onResize).observe($("graph"));
  refresh();
  loadEvents();
  connect();
}

init();
