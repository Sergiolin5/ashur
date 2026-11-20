// 0.75.0: ForceAtlas2 off the main thread, as a real file (the panel's CSP allows workers from 'self', not blobs).
// In: {x, y, sizes, edges} as typed arrays; out: positions every few iterations, until stopped or settled.
import Graph from "graphology";
import forceAtlas2 from "graphology-layout-forceatlas2";

let g = null, settings = null, running = false;
self.onmessage = (e) => {
  const m = e.data;
  if (m.tipo === "parar") { running = false; return; }
  if (m.tipo !== "empezar") return;
  g = new Graph({ type: "undirected", multi: false, allowSelfLoops: false });
  const n = m.x.length;
  for (let i = 0; i < n; i++) g.addNode(i, { x: m.x[i], y: m.y[i], size: m.sizes[i] });
  for (let k = 0; k < m.edges.length; k += 2) {
    const a = m.edges[k], b = m.edges[k + 1];
    if (a !== b && !g.hasEdge(a, b)) g.addEdge(a, b);
  }
  settings = { ...forceAtlas2.inferSettings(g), barnesHutOptimize: n > 1500, adjustSizes: false, slowDown: 1 + Math.log(Math.max(n, 2)) };
  running = true;
  let left = m.iteraciones || 400;
  const tick = () => {
    if (!running || left <= 0) { self.postMessage({ tipo: "fin" }); return; }
    const step = n > 20000 ? 1 : n > 3000 ? 3 : 10;
    forceAtlas2.assign(g, { iterations: step, settings });
    left -= step;
    const x = new Float32Array(n), y = new Float32Array(n);
    g.forEachNode((k, a) => { x[k] = a.x; y[k] = a.y; });
    self.postMessage({ tipo: "pos", x, y }, [x.buffer, y.buffer]);
    setTimeout(tick, 0);
  };
  tick();
};
