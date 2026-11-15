// Layered left-to-right layout for small signal graphs (no dependencies).
//
// Input-side devices sit in the first layer, output-side devices in the
// last, plugins in between by longest path from the inputs. Within a layer
// nodes are ordered by barycenter sweeps to reduce crossings. In "port"
// mode each node lists its ports and edges attach to them; in "plugin" mode
// parallel edges between two nodes collapse into one labelled bundle.

export const SIZES = {
  plugin: { w: 188, h: 64, header: 64 },
  port: { w: 216, header: 46, row: 20, pad: 10 },
  gapX: 100, gapY: 28, margin: 32,
};

function layers(nodes, edges) {
  const ids = nodes.map((n) => n.id);
  const out = new Map(ids.map((id) => [id, []]));
  const indeg = new Map(ids.map((id) => [id, 0]));
  const byId = new Map(nodes.map((n) => [n.id, n]));
  // Drop edges that would close a cycle (validation forbids them, but the
  // UI must never hang on bad data): DFS back-edge removal.
  const state = new Map();
  const kept = [];
  const adj = new Map(ids.map((id) => [id, []]));
  for (const e of edges) if (adj.has(e.from) && adj.has(e.to) && e.from !== e.to) adj.get(e.from).push(e);
  const visit = (id) => {
    state.set(id, 1);
    for (const e of adj.get(id)) {
      const s = state.get(e.to);
      if (s === 1) continue; // back edge
      kept.push(e);
      if (!s) visit(e.to);
    }
    state.set(id, 2);
  };
  for (const id of ids) if (!state.get(id)) visit(id);
  for (const e of kept) { out.get(e.from).push(e.to); indeg.set(e.to, indeg.get(e.to) + 1); }

  // Seed roots before propagating: hardware inputs in column 0, plugins with
  // no inputs (oscillators, synths) in the first plugin column, so whatever
  // they feed lands strictly to their right.
  const hasInputs = nodes.some((n) => n.kind === "device" && n.side === "in");
  const layer = new Map();
  const queue = ids.filter((id) => indeg.get(id) === 0);
  for (const id of queue) {
    const n = byId.get(id);
    layer.set(id, n.kind === "device" ? (n.side === "in" ? 0 : 1) : (hasInputs ? 1 : 0));
  }
  const deg = new Map(indeg);
  while (queue.length) {
    const id = queue.shift();
    for (const to of out.get(id)) {
      layer.set(to, Math.max(layer.get(to) ?? 0, layer.get(id) + 1));
      deg.set(to, deg.get(to) - 1);
      if (deg.get(to) === 0) queue.push(to);
    }
  }
  // Inputs first, outputs in one column after the last plugin.
  const plugins = nodes.filter((n) => n.kind !== "device");
  const lastPlugin = plugins.length ? Math.max(...plugins.map((n) => layer.get(n.id) ?? 0)) : 0;
  for (const n of nodes) {
    if (n.kind === "device") layer.set(n.id, n.side === "in" ? 0 : lastPlugin + 1);
  }
  return { layer, kept };
}

function order(nodes, edges, layer) {
  const cols = [];
  for (const n of nodes) {
    const l = layer.get(n.id);
    (cols[l] ||= []).push(n.id);
  }
  for (let i = 0; i < cols.length; i++) cols[i] ||= [];
  const pos = () => new Map(cols.flatMap((c) => c.map((id, i) => [id, i])));
  const preds = new Map(nodes.map((n) => [n.id, []]));
  const succs = new Map(nodes.map((n) => [n.id, []]));
  for (const e of edges) { preds.get(e.to)?.push(e.from); succs.get(e.from)?.push(e.to); }
  const sweep = (range, nbrs) => {
    for (const l of range) {
      const p = pos();
      const bary = (id) => {
        const ns = nbrs.get(id).filter((x) => p.has(x));
        return ns.length ? ns.reduce((a, x) => a + p.get(x), 0) / ns.length : p.get(id);
      };
      const keyed = cols[l].map((id, i) => [bary(id), i, id]);
      keyed.sort((a, b) => a[0] - b[0] || a[1] - b[1]);
      cols[l] = keyed.map((k) => k[2]);
    }
  };
  const down = [...Array(cols.length).keys()].slice(1);
  const up = [...Array(cols.length).keys()].slice(0, -1).reverse();
  for (let i = 0; i < 4; i++) { sweep(down, preds); sweep(up, succs); }
  return cols;
}

function nodeBox(n, mode) {
  if (n.kind === "dummy") return { w: 0, h: 16 };
  if (mode === "plugin") return { w: SIZES.plugin.w, h: SIZES.plugin.h };
  const rows = Math.max(n.inputs.length, n.outputs.length, 1);
  return { w: SIZES.port.w, h: SIZES.port.header + rows * SIZES.port.row + SIZES.port.pad };
}

export function bezier(x1, y1, x2, y2) {
  const dx = Math.max(40, Math.abs(x2 - x1) / 2);
  return `M ${x1} ${y1} C ${x1 + dx} ${y1}, ${x2 - dx} ${y2}, ${x2} ${y2}`;
}

// Curve from a to b through each lane point; inside a column the wire runs
// straight across the column, between columns it bends.
function through(start, lanes, end) {
  let d = `M ${start[0]} ${start[1]}`;
  let [px, py] = start;
  for (const [lx, rx, y] of lanes) {
    const dx = Math.max(30, (lx - px) / 2);
    d += ` C ${px + dx} ${py}, ${lx - dx} ${y}, ${lx} ${y} L ${rx} ${y}`;
    [px, py] = [rx, y];
  }
  const dx = Math.max(30, (end[0] - px) / 2);
  return d + ` C ${px + dx} ${py}, ${end[0] - dx} ${end[1]}, ${end[0]} ${end[1]}`;
}

export function layout(graph, mode = "plugin") {
  const nodes = graph.nodes || [];
  const edges = graph.edges || [];
  if (!nodes.length) return { nodes: [], edges: [], width: 0, height: 0, columns: [] };
  const { layer } = layers(nodes, edges);
  const byId = new Map(nodes.map((n) => [n.id, n]));

  // Routes: one per cable in port view, one per node pair in plugin view.
  const routes = [];
  if (mode === "port") {
    for (const e of edges) if (byId.has(e.from) && byId.has(e.to)) routes.push({ key: e.id, members: [e] });
  } else {
    const bundles = new Map();
    for (const e of edges) {
      if (!byId.has(e.from) || !byId.has(e.to)) continue;
      const key = `${e.from}|${e.to}|${e.section}`;
      if (!bundles.has(key)) bundles.set(key, { key, members: [] });
      bundles.get(key).members.push(e);
    }
    routes.push(...bundles.values());
  }

  // Long routes get a dummy node in every column they cross, so ordering
  // gives them a lane instead of drawing them through other nodes.
  const all = [...nodes];
  const orderEdges = [];
  for (const r of routes) {
    const e = r.members[0];
    const a = layer.get(e.from), b = layer.get(e.to);
    r.dummies = [];
    let prev = e.from;
    for (let l = a + 1; l < b; l++) {
      const id = `~${r.key}~${l}`;
      all.push({ id, kind: "dummy", inputs: [], outputs: [] });
      layer.set(id, l);
      orderEdges.push({ from: prev, to: id });
      r.dummies.push(id);
      prev = id;
    }
    orderEdges.push({ from: prev, to: e.to });
  }
  const cols = order(all, orderEdges, layer);
  const boxes = new Map(all.map((n) => [n.id, nodeBox(n, mode)]));

  const colW = mode === "plugin" ? SIZES.plugin.w : SIZES.port.w;
  const heights = cols.map((c) => c.reduce((s, id) => s + boxes.get(id).h, 0) + Math.max(0, c.length - 1) * SIZES.gapY);
  const tallest = Math.max(...heights);
  const placed = new Map();
  cols.forEach((c, l) => {
    let y = SIZES.margin + (tallest - heights[l]) / 2;
    const x = SIZES.margin + l * (colW + SIZES.gapX);
    for (const id of c) {
      const b = boxes.get(id);
      const n = byId.get(id) || all.find((d) => d.id === id);
      placed.set(id, { ...n, x, y, w: b.w || colW, h: b.h });
      y += b.h + SIZES.gapY;
    }
  });

  const portY = (n, list, sym) => {
    const i = Math.max(0, list.findIndex((p) => p.symbol === sym));
    return n.y + SIZES.port.header + i * SIZES.port.row + SIZES.port.row / 2;
  };
  // Spread several plugin-view routes leaving/entering the same node.
  const slots = (key) => {
    const total = new Map(), idx = new Map();
    for (const r of routes) total.set(r.members[0][key], (total.get(r.members[0][key]) || 0) + 1);
    return (id, h) => { const i = idx.get(id) || 0; idx.set(id, i + 1); return (h * (i + 1)) / (total.get(id) + 1); };
  };
  const outSlot = slots("from"), inSlot = slots("to");

  const outEdges = [];
  for (const r of routes) {
    const e = r.members[0];
    const a = placed.get(e.from), b = placed.get(e.to);
    const start = mode === "port" ? [a.x + a.w, portY(a, a.outputs, e.fromPort)] : [a.x + a.w, a.y + outSlot(e.from, a.h)];
    const end = mode === "port" ? [b.x, portY(b, b.inputs, e.toPort)] : [b.x, b.y + inSlot(e.to, b.h)];
    const lanes = r.dummies.map((id) => { const d = placed.get(id); return [d.x, d.x + colW, d.y + d.h / 2]; });
    const worst = ["Unresolved", "Pending", "Inactive", "Connected"].find((s) => r.members.some((m) => m.state === s)) || e.state;
    // Label where the wire is straight and clear: the first lane, else the middle.
    const [lx, ly] = lanes.length ? [lanes[0][0] + colW / 2, lanes[0][2]] : [(start[0] + end[0]) / 2, (start[1] + end[1]) / 2];
    outEdges.push({
      ...e, id: r.key, state: worst, members: r.members,
      // Only label where there's room: bundles, and long edges whose label
      // sits on a straight lane. Single cables between neighbours would
      // collide with the nodes; their ports are in the tooltip and port view.
      label: r.members.length > 1 ? `${r.members.length} cables`
        : lanes.length ? `${e.fromPort} → ${e.toPort}` : null,
      path: lanes.length ? through(start, lanes, end) : bezier(start[0], start[1], end[0], end[1]),
      lx, ly,
    });
  }
  const real = [...placed.values()].filter((n) => n.kind !== "dummy");
  const width = Math.max(...[...placed.values()].map((n) => n.x + n.w)) + SIZES.margin;
  const height = Math.max(...[...placed.values()].map((n) => n.y + n.h)) + SIZES.margin;
  return { nodes: real, edges: outEdges, width, height, columns: cols.map((c) => c.filter((id) => !id.startsWith("~"))) };
}
