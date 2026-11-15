import { test } from "node:test";
import assert from "node:assert/strict";
import { layout } from "../../src/r7s/ui/static/layout.js";

const P = (id) => ({ id: `p:${id}`, kind: "plugin", label: id, inputs: [{ symbol: "in" }, { symbol: "in2" }], outputs: [{ symbol: "out" }] });
const D = (id, side, ports) => ({ id: `d:${id}:${side}`, kind: "device", side, label: id,
  inputs: side === "out" ? ports.map((s) => ({ symbol: s })) : [], outputs: side === "in" ? ports.map((s) => ({ symbol: s })) : [] });
const E = (from, fromPort, to, toPort, state = "Connected", section = "audio") => ({ id: `${from}${fromPort}${to}${toPort}`, from, fromPort, to, toPort, state, section });

const chain = {
  nodes: [P("verb"), D("sc", "in", ["capture_1"]), P("amp"), D("sc", "out", ["playback_l", "playback_r"]), P("lonely")],
  edges: [E("d:sc:in", "capture_1", "p:amp", "in"), E("p:amp", "out", "p:verb", "in"),
          E("p:verb", "out", "d:sc:out", "playback_l"), E("p:verb", "out", "d:sc:out", "playback_r", "Pending")],
};

test("columns run inputs -> plugins in signal order -> outputs", () => {
  const l = layout(chain, "plugin");
  assert.deepEqual(l.columns, [["d:sc:in"], ["p:amp", "p:lonely"], ["p:verb"], ["d:sc:out"]]);
  const x = Object.fromEntries(l.nodes.map((n) => [n.id, n.x]));
  assert.ok(x["d:sc:in"] < x["p:amp"] && x["p:amp"] < x["p:verb"] && x["p:verb"] < x["d:sc:out"]);
});

test("plugin view bundles parallel cables and reports the worst state", () => {
  const l = layout(chain, "plugin");
  const b = l.edges.find((e) => e.from === "p:verb" && e.to === "d:sc:out");
  assert.equal(b.members.length, 2);
  assert.equal(b.state, "Pending");
  assert.equal(b.label, "2 cables");
  assert.equal(l.edges.length, 3);
  assert.equal(l.edges.find((e) => e.from === "p:amp").label, null, "adjacent single cable: no label");
});

test("port view attaches edges to port rows", () => {
  const l = layout(chain, "port");
  assert.equal(l.edges.length, 4);
  const amp = l.nodes.find((n) => n.id === "p:amp");
  const e = l.edges.find((x) => x.to === "p:amp");
  const y = Number(e.path.split(" ").at(-1));
  assert.ok(Math.abs(y - (amp.y + 46 + 10)) < 1e-9, `edge lands on the first input row (${y})`);
  assert.ok(amp.h > 64);
});

test("cycles and dangling edges don't hang or crash", () => {
  const g = { nodes: [P("a"), P("b")], edges: [E("p:a", "out", "p:b", "in"), E("p:b", "out", "p:a", "in"), E("p:a", "out", "p:zz", "in")] };
  const l = layout(g, "plugin");
  assert.equal(l.nodes.length, 2);
  assert.ok(l.width > 0 && l.height > 0);
});

test("empty graph", () => {
  assert.deepEqual(layout({ nodes: [], edges: [] }), { nodes: [], edges: [], width: 0, height: 0, columns: [] });
});

test("barycenter ordering untangles crossed parallel chains", () => {
  const g = {
    nodes: [D("x", "in", ["capture_1", "capture_2"]), P("b2"), P("a2"), P("b1"), P("a1")],
    edges: [E("d:x:in", "capture_1", "p:a1", "in"), E("d:x:in", "capture_2", "p:b1", "in"),
            E("p:a1", "out", "p:a2", "in"), E("p:b1", "out", "p:b2", "in")],
  };
  const l = layout(g, "plugin");
  const y = Object.fromEntries(l.nodes.map((n) => [n.id, n.y]));
  assert.equal(Math.sign(y["p:a1"] - y["p:b1"]), Math.sign(y["p:a2"] - y["p:b2"]), "chains stay parallel");
});

test("a source plugin sits left of what it feeds (oscillator into a reverb)", () => {
  const g = {
    nodes: [D("mic", "in", ["capture_1"]), P("tone"), P("verb"), D("sc", "out", ["playback_1"])],
    edges: [E("d:mic:in", "capture_1", "p:verb", "in"), E("p:tone", "out", "p:verb", "in2"),
            E("p:verb", "out", "d:sc:out", "playback_1")],
  };
  const l = layout(g, "plugin");
  assert.deepEqual(l.columns, [["d:mic:in"], ["p:tone"], ["p:verb"], ["d:sc:out"]]);
  for (const e of l.edges) {
    const a = l.nodes.find((n) => n.id === e.from), b = l.nodes.find((n) => n.id === e.to);
    assert.ok(a.x < b.x, `${e.from} -> ${e.to} flows left to right`);
  }
});

test("an edge skipping a column gets its own lane instead of crossing a node", () => {
  const g = {
    nodes: [D("mic", "in", ["capture_1"]), P("tone"), P("verb")],
    edges: [E("d:mic:in", "capture_1", "p:verb", "in"), E("p:tone", "out", "p:verb", "in2")],
  };
  const l = layout(g, "plugin");
  const tone = l.nodes.find((n) => n.id === "p:tone");
  const long = l.edges.find((e) => e.from === "d:mic:in");
  // The lane's straight run across tone's column must not overlap tone's box.
  const m = long.path.match(/L (\S+) (\S+)/);
  assert.ok(m, "long edge is routed through a lane");
  const laneY = Number(m[2]);
  assert.ok(laneY < tone.y || laneY > tone.y + tone.h, `lane y ${laneY} clears tone [${tone.y}, ${tone.y + tone.h}]`);
});
