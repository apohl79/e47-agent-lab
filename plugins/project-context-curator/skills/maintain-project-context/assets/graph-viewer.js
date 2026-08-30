(function () {
  "use strict";
  const DATA = JSON.parse(document.getElementById("graph-data").textContent);
  const STORE_KINDS = new Set(["project", "domain", "universal"]);
  const STRUCTURAL = new Set(["member_of", "stored_in"]);
  const DASHED = new Set(["uninitialized", "missing", "remote-only"]);
  const KIND_COLORS = {
    project: "#4c9be8", domain: "#f2a83b", universal: "#9b6bd6",
    term: "#5cc47a", component: "#e8735a", pattern: "#d9c55b", question: "#8fa3b8",
  };
  const RELATION_COLORS = {
    member_of: "#5a6478", stored_in: "#3d4556", depends_on: "#e85d75", integrates_with: "#4c9be8",
    owns: "#f2a83b", produces: "#5cc47a", consumes: "#9b6bd6", references: "#7f8a9e",
    mentions: "#6fb3c9", shadows: "#f2c14e", diverges: "#e8735a",
  };
  const canvas = document.getElementById("graph-canvas");
  const ctx = canvas.getContext("2d");
  const tooltip = document.getElementById("tooltip");
  const sidebar = (id) => document.getElementById(id);
  const FOCAL = 900;
  const MAX_SPEED = 30;
  const HINTS = {
    flat: "drag: pan · wheel: zoom · click: select · double-click: expand or collapse a store",
    deep: "drag: orbit · shift+drag: pan · wheel: zoom · click: select · double-click: expand or collapse a store",
  };
  const clamp = (value, low, high) => Math.max(low, Math.min(high, value));

  const nodes = DATA.nodes.map((raw, index) => {
    const angle = index * 2.399963;
    const radius = 26 * Math.sqrt(index + 1);
    const total = Object.values(raw.counts).reduce((sum, value) => sum + value, 0);
    return Object.assign({}, raw, {
      x: Math.cos(angle) * radius, y: Math.sin(angle) * radius, z: ((index * 37) % 200) - 100,
      vx: 0, vy: 0, vz: 0, seed: index, total: total, visible: false, neighbours: new Set(),
      r: STORE_KINDS.has(raw.kind) ? 7 + Math.min(20, Math.sqrt(total) * 1.6) : 3.5,
    });
  });
  const byId = new Map(nodes.map((node) => [node.id, node]));
  const edges = DATA.edges.map((raw) => Object.assign({}, raw, {s: byId.get(raw.source), t: byId.get(raw.target)}));
  edges.forEach((edge) => { edge.s.neighbours.add(edge.t.id); edge.t.neighbours.add(edge.s.id); });
  const placed = new Map();
  nodes.forEach((node) => {
    if (STORE_KINDS.has(node.kind)) return;
    const store = byId.get(node.store);
    if (!store) return;
    const slot = placed.get(store.id) || 0;
    placed.set(store.id, slot + 1);
    const angle = slot * 2.399963, radius = store.r + 12 + 6 * Math.sqrt(slot);
    node.x = store.x + Math.cos(angle) * radius; node.y = store.y + Math.sin(angle) * radius; node.z = store.z + ((slot * 37) % 60) - 30;
  });
  const stores = nodes.filter((node) => STORE_KINDS.has(node.kind));
  const relationNames = Array.from(new Set(edges.map((edge) => edge.relation))).sort();
  const kindNames = Array.from(new Set(nodes.map((node) => node.kind)));

  const state = {
    expanded: new Set(DATA.view.level === "records" ? stores.map((node) => node.id) : []),
    relations: new Set(relationNames), kinds: new Set(kindNames), minConfidence: 0, query: "",
    selected: null, hover: null, visibleNodes: [], visibleEdges: [], visibleStores: [], visibleRecords: [], clusters: [], order: [], alpha: 1,
    mode3d: false, spin: false, cam: {x: 0, y: 0, z: 0, k: 1, rotX: -0.35, rotY: 0.6}, drag: null, width: 0, height: 0,
  };

  function matches(node) {
    return !state.query || node.label.toLowerCase().includes(state.query) || node.id.toLowerCase().includes(state.query);
  }
  function refresh(reheat) {
    nodes.forEach((node) => {
      node.visible = state.kinds.has(node.kind) && (STORE_KINDS.has(node.kind) || state.expanded.has(node.store));
    });
    state.visibleNodes = nodes.filter((node) => node.visible);
    state.visibleEdges = edges.filter((edge) => edge.s.visible && edge.t.visible && state.relations.has(edge.relation)
      && (STRUCTURAL.has(edge.relation) || edge.confidence >= state.minConfidence));
    state.visibleStores = state.visibleNodes.filter((node) => STORE_KINDS.has(node.kind));
    state.visibleRecords = state.visibleNodes.filter((node) => !STORE_KINDS.has(node.kind));
    const clusters = new Map();
    state.visibleRecords.forEach((node) => {
      if (!clusters.has(node.store)) clusters.set(node.store, []);
      clusters.get(node.store).push(node);
    });
    state.clusters = Array.from(clusters.values());
    state.visibleNodes.forEach((node) => { node.degree = 0; });
    state.visibleEdges.forEach((edge) => { edge.s.degree += 1; edge.t.degree += 1; });
    if (reheat) state.alpha = Math.max(state.alpha, 0.6);
    if (state.selected && !state.selected.visible) select(null);
    draw();
  }

  function repel(a, b, charge, alpha) {
    let dx = b.x - a.x, dy = b.y - a.y, dz = b.z - a.z;
    let l = dx * dx + dy * dy + dz * dz;
    if (l < 1) { dx = (a.seed % 3) - 1 || 0.5; dy = (b.seed % 3) - 1 || -0.5; l = 1; }
    if (l > 250000) return;
    const w = charge * alpha / l;
    a.vx -= dx * w; a.vy -= dy * w; a.vz -= dz * w;
    b.vx += dx * w; b.vy += dy * w; b.vz += dz * w;
  }
  function tick() {
    if (state.alpha < 0.002) return;
    const alpha = state.alpha;
    state.alpha += (0 - alpha) * 0.0228;
    // Records only repel siblings of their own store; store-store repulsion keeps clusters apart.
    const storesVisible = state.visibleStores, records = state.visibleRecords;
    for (let i = 0; i < storesVisible.length; i++) {
      for (let j = i + 1; j < storesVisible.length; j++) repel(storesVisible[i], storesVisible[j], 900, alpha);
      for (let j = 0; j < records.length; j++) repel(storesVisible[i], records[j], 220, alpha);
    }
    state.clusters.forEach((cluster) => {
      for (let i = 0; i < cluster.length; i++) {
        for (let j = i + 1; j < cluster.length; j++) repel(cluster[i], cluster[j], 60, alpha);
      }
    });
    state.visibleEdges.forEach((edge) => {
      const a = edge.s, b = edge.t;
      const dx = b.x - a.x, dy = b.y - a.y, dz = b.z - a.z;
      const d = Math.sqrt(dx * dx + dy * dy + dz * dz) || 1;
      const target = edge.relation === "stored_in" ? 40 + a.r + b.r : (STORE_KINDS.has(a.kind) && STORE_KINDS.has(b.kind)) ? 150 : 70;
      const strength = edge.relation === "stored_in" ? 0.2 : 0.06;
      const f = (d - target) / d * strength * alpha;
      // High-degree nodes (stores) absorb less of each spring, as in d3-force's link bias.
      const bias = b.degree / (a.degree + b.degree);
      a.vx += dx * f * bias; a.vy += dy * f * bias; a.vz += dz * f * bias;
      b.vx -= dx * f * (1 - bias); b.vy -= dy * f * (1 - bias); b.vz -= dz * f * (1 - bias);
    });
    state.visibleNodes.forEach((node) => {
      if (node === (state.drag && state.drag.node)) { node.vx = node.vy = node.vz = 0; return; }
      node.vx -= node.x * 0.02 * alpha; node.vy -= node.y * 0.02 * alpha; node.vz -= node.z * (state.mode3d ? 0.02 : 0.05) * alpha;
      node.vx *= 0.6; node.vy *= 0.6; node.vz *= 0.6;
      const speed = Math.hypot(node.vx, node.vy, node.vz);
      if (speed > MAX_SPEED) { const s = MAX_SPEED / speed; node.vx *= s; node.vy *= s; node.vz *= s; }
      node.x += node.vx; node.y += node.vy; node.z += node.vz;
    });
    const count = state.visibleNodes.length;
    if (!count) return;
    let cx = 0, cy = 0, cz = 0;
    state.visibleNodes.forEach((node) => { cx += node.x; cy += node.y; cz += node.z; });
    cx /= count; cy /= count; cz /= count;
    const held = state.drag && state.drag.node;
    state.visibleNodes.forEach((node) => { if (node !== held) { node.x -= cx; node.y -= cy; node.z -= cz; } });
  }

  function rotate(x, y, z) {
    const cam = state.cam, cy = Math.cos(cam.rotY), sy = Math.sin(cam.rotY), cx = Math.cos(cam.rotX), sx = Math.sin(cam.rotX);
    const x1 = x * cy + z * sy, z1 = z * cy - x * sy;
    return {x: x1, y: y * cx - z1 * sx, z: y * sx + z1 * cx};
  }
  function unrotate(x, y, z) {
    const cam = state.cam, cy = Math.cos(cam.rotY), sy = Math.sin(cam.rotY), cx = Math.cos(cam.rotX), sx = Math.sin(cam.rotX);
    const y1 = y * cx + z * sx, z1 = z * cx - y * sx;
    return {x: x * cy - z1 * sy, y: y1, z: x * sy + z1 * cy};
  }
  function project(node) {
    const cam = state.cam;
    if (!state.mode3d) return {x: (node.x - cam.x) * cam.k + state.width / 2, y: (node.y - cam.y) * cam.k + state.height / 2, s: 1, depth: 0};
    const p = rotate(node.x - cam.x, node.y - cam.y, node.z - cam.z);
    const s = FOCAL / Math.max(FOCAL * 0.2, FOCAL + p.z);
    return {x: p.x * s * cam.k + state.width / 2, y: p.y * s * cam.k + state.height / 2, s: s, depth: p.z};
  }
  function fade(scale) { return state.mode3d ? clamp(0.4 + scale * 0.6, 0.3, 1) : 1; }
  function emphasis(node) {
    if (state.selected) {
      const focus = state.selected.s ? [state.selected.s, state.selected.t] : [state.selected];
      if (focus.includes(node) || focus.some((item) => item.neighbours.has(node.id))) return 1;
      return 0.15;
    }
    return matches(node) ? 1 : 0.15;
  }
  function draw() {
    ctx.clearRect(0, 0, state.width, state.height);
    const k = state.cam.k;
    state.visibleNodes.forEach((node) => { node.p = project(node); });
    state.order = state.mode3d ? state.visibleNodes.slice().sort((a, b) => b.p.depth - a.p.depth) : state.visibleNodes;
    const edgeOrder = state.mode3d
      ? state.visibleEdges.slice().sort((a, b) => (b.s.p.depth + b.t.p.depth) - (a.s.p.depth + a.t.p.depth))
      : state.visibleEdges;
    edgeOrder.forEach((edge) => {
      const a = edge.s.p, b = edge.t.p;
      const strong = state.selected && (state.selected === edge || state.selected === edge.s || state.selected === edge.t);
      ctx.globalAlpha = Math.min(emphasis(edge.s), emphasis(edge.t)) * (strong ? 1 : 0.3 + edge.confidence * 0.6) * fade(Math.min(a.s, b.s));
      ctx.strokeStyle = RELATION_COLORS[edge.relation] || "#7f8a9e";
      ctx.lineWidth = (strong || state.hover === edge ? 2.5 : 1) * Math.min(a.s, b.s);
      ctx.setLineDash(STRUCTURAL.has(edge.relation) ? [3, 4] : []);
      ctx.beginPath(); ctx.moveTo(a.x, a.y); ctx.lineTo(b.x, b.y); ctx.stroke();
      ctx.setLineDash([]);
      if (!STRUCTURAL.has(edge.relation)) {
        const dx = b.x - a.x, dy = b.y - a.y, d = Math.sqrt(dx * dx + dy * dy) || 1;
        const ux = dx / d, uy = dy / d, r = edge.t.r * k * b.s + 2;
        const tipX = b.x - ux * r, tipY = b.y - uy * r, size = 5 * b.s;
        ctx.fillStyle = ctx.strokeStyle;
        ctx.beginPath(); ctx.moveTo(tipX, tipY);
        ctx.lineTo(tipX - ux * size - uy * size * 0.6, tipY - uy * size + ux * size * 0.6);
        ctx.lineTo(tipX - ux * size + uy * size * 0.6, tipY - uy * size - ux * size * 0.6);
        ctx.closePath(); ctx.fill();
      }
    });
    ctx.font = "12px -apple-system, Helvetica, Arial, sans-serif";
    state.order.forEach((node) => {
      const p = node.p, r = node.r * k * p.s;
      const active = node === state.hover || node === state.selected;
      ctx.globalAlpha = emphasis(node) * fade(p.s);
      ctx.fillStyle = KIND_COLORS[node.kind] || "#8fa3b8";
      ctx.beginPath(); ctx.arc(p.x, p.y, r, 0, Math.PI * 2); ctx.fill();
      ctx.lineWidth = active ? 3 : 1.2;
      ctx.strokeStyle = active ? "#ffffff" : "rgba(255,255,255,0.35)";
      ctx.setLineDash(DASHED.has(node.status) ? [3, 3] : []);
      ctx.stroke(); ctx.setLineDash([]);
      if (STORE_KINDS.has(node.kind) || active || k * p.s > 1.4 || (state.query && matches(node))) {
        ctx.fillStyle = active ? "#ffffff" : "#c8cfda";
        ctx.fillText(node.label.length > 48 ? node.label.slice(0, 47) + "…" : node.label, p.x + r + 4, p.y + 4);
      }
    });
    ctx.globalAlpha = 1;
  }

  function hitTest(x, y) {
    for (let i = state.order.length - 1; i >= 0; i--) {
      const node = state.order[i], p = project(node), r = node.r * state.cam.k * p.s + 3;
      if ((p.x - x) ** 2 + (p.y - y) ** 2 <= r * r) return node;
    }
    let best = null, bestDistance = 5;
    state.visibleEdges.forEach((edge) => {
      const a = project(edge.s), b = project(edge.t);
      const dx = b.x - a.x, dy = b.y - a.y, l = dx * dx + dy * dy || 1;
      const t = Math.max(0, Math.min(1, ((x - a.x) * dx + (y - a.y) * dy) / l));
      const distance = Math.hypot(a.x + dx * t - x, a.y + dy * t - y);
      if (distance < bestDistance) { best = edge; bestDistance = distance; }
    });
    return best;
  }
  function describe(item) {
    if (!item) return "";
    if (item.s) {
      const evidence = item.evidence.map((entry) => `<li>${escape(entry.kind)}: ${escape(entry.label)}</li>`).join("");
      return `<div class="kind">${escape(item.relation)} · confidence ${item.confidence.toFixed(2)}</div>` +
        `<div>${escape(item.s.label)} → ${escape(item.t.label)}</div>` + (evidence ? `<ul class="evidence">${evidence}</ul>` : "");
    }
    const counts = Object.entries(item.counts).filter(([, value]) => value).map(([key, value]) => `${value} ${key}`).join(", ");
    return `<div class="kind">${escape(item.kind)}${item.status ? " · " + escape(item.status) : ""}</div><div>${escape(item.label)}</div>` +
      (item.summary ? `<div class="summary">${escape(item.summary)}</div>` : "") +
      (counts ? `<div class="summary">${counts}</div>` : "") + (item.path ? `<div class="path">${escape(item.path)}</div>` : "");
  }
  function escape(value) {
    return String(value).replace(/[&<>"]/g, (char) => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;"}[char]));
  }
  function select(item) {
    state.selected = item;
    const details = sidebar("details");
    if (!item) { details.innerHTML = '<p class="muted">Click a node or edge.</p>'; return; }
    let html = describe(item);
    if (!item.s) {
      const incident = state.visibleEdges.filter((edge) => edge.s === item || edge.t === item)
        .sort((a, b) => b.confidence - a.confidence).slice(0, 40);
      html += "<ul>" + incident.map((edge) => {
        const other = edge.s === item ? edge.t : edge.s;
        return `<li><a href="#" data-node="${escape(other.id)}">${edge.s === item ? "→" : "←"} ${escape(edge.relation)} ${escape(other.label)}</a> <span class="muted">${edge.confidence.toFixed(2)}</span></li>`;
      }).join("") + "</ul>";
      if (STORE_KINDS.has(item.kind) && item.total) {
        html += `<button data-toggle="${escape(item.id)}">${state.expanded.has(item.id) ? "Collapse" : "Expand"} ${item.total} records</button>`;
      }
    }
    details.innerHTML = html;
    details.querySelectorAll("a[data-node]").forEach((link) => link.addEventListener("click", (event) => {
      event.preventDefault(); select(byId.get(link.dataset.node)); draw();
    }));
    details.querySelectorAll("button[data-toggle]").forEach((button) => button.addEventListener("click", () => toggle(byId.get(button.dataset.toggle))));
  }
  function toggle(store) {
    if (!store || !STORE_KINDS.has(store.kind)) return;
    if (state.expanded.has(store.id)) state.expanded.delete(store.id); else state.expanded.add(store.id);
    refresh(true); select(store);
  }
  function fit() {
    if (!state.visibleNodes.length) return;
    const span = (axis) => {
      const values = state.visibleNodes.map((node) => node[axis]), low = Math.min(...values), high = Math.max(...values);
      state.cam[axis] = (low + high) / 2; return high - low;
    };
    const width = span("x"), height = span("y"), depth = span("z");
    const extent = state.mode3d ? Math.max(width, height, depth) : 0;
    state.cam.k = clamp(0.9 * Math.min(state.width / (Math.max(width, extent) + 120), state.height / (Math.max(height, extent) + 120)), 0.05, 4);
    draw();
  }
  function moveNode(node, dx, dy) {
    const scale = state.cam.k * (node.p ? node.p.s : 1);
    const d = state.mode3d ? unrotate(dx / scale, dy / scale, 0) : {x: dx / scale, y: dy / scale, z: 0};
    node.x += d.x; node.y += d.y; node.z += d.z; state.alpha = Math.max(state.alpha, 0.3);
  }
  function pan(dx, dy) {
    const d = state.mode3d ? unrotate(dx / state.cam.k, dy / state.cam.k, 0) : {x: dx / state.cam.k, y: dy / state.cam.k, z: 0};
    state.cam.x -= d.x; state.cam.y -= d.y; state.cam.z -= d.z;
  }
  function setMode(mode3d) {
    state.mode3d = mode3d;
    sidebar("mode-3d").classList.toggle("active", mode3d);
    sidebar("spin").disabled = !mode3d;
    sidebar("hint").textContent = mode3d ? HINTS.deep : HINTS.flat;
    state.alpha = Math.max(state.alpha, 0.5); fit();
  }

  canvas.addEventListener("mousedown", (event) => {
    const hit = hitTest(event.offsetX, event.offsetY);
    state.drag = {node: hit && !hit.s ? hit : null, x: event.offsetX, y: event.offsetY, moved: false, pan: event.shiftKey};
    canvas.classList.add("dragging");
  });
  window.addEventListener("mousemove", (event) => {
    const rect = canvas.getBoundingClientRect();
    const x = event.clientX - rect.left, y = event.clientY - rect.top;
    if (state.drag) {
      const dx = x - state.drag.x, dy = y - state.drag.y;
      if (Math.abs(dx) + Math.abs(dy) > 2) state.drag.moved = true;
      if (state.drag.node) moveNode(state.drag.node, dx, dy);
      else if (state.mode3d && !state.drag.pan) { state.cam.rotY += dx * 0.006; state.cam.rotX = clamp(state.cam.rotX + dy * 0.006, -1.5, 1.5); }
      else pan(dx, dy);
      state.drag.x = x; state.drag.y = y; draw(); return;
    }
    const hit = hitTest(x, y);
    if (hit !== state.hover) { state.hover = hit; draw(); }
    tooltip.style.display = hit ? "block" : "none";
    if (hit) { tooltip.innerHTML = describe(hit); tooltip.style.left = `${Math.min(x + 14, state.width - 380)}px`; tooltip.style.top = `${y + 14}px`; }
  });
  window.addEventListener("mouseup", () => {
    if (!state.drag) return;
    if (!state.drag.moved) { select(state.hover); draw(); }
    state.drag = null; canvas.classList.remove("dragging");
  });
  canvas.addEventListener("dblclick", (event) => { const hit = hitTest(event.offsetX, event.offsetY); if (hit && !hit.s) toggle(hit); });
  canvas.addEventListener("wheel", (event) => {
    event.preventDefault();
    const factor = Math.exp(-event.deltaY * 0.0015);
    if (state.mode3d) { state.cam.k = clamp(state.cam.k * factor, 0.05, 8); draw(); return; }
    const before = {x: (event.offsetX - state.width / 2) / state.cam.k + state.cam.x, y: (event.offsetY - state.height / 2) / state.cam.k + state.cam.y};
    state.cam.k = clamp(state.cam.k * factor, 0.05, 8);
    state.cam.x = before.x - (event.offsetX - state.width / 2) / state.cam.k;
    state.cam.y = before.y - (event.offsetY - state.height / 2) / state.cam.k;
    draw();
  }, {passive: false});
  function resize() {
    const stage = canvas.parentElement;
    state.width = stage.clientWidth; state.height = stage.clientHeight;
    canvas.width = state.width * devicePixelRatio; canvas.height = state.height * devicePixelRatio;
    ctx.setTransform(devicePixelRatio, 0, 0, devicePixelRatio, 0, 0);
    draw();
  }
  window.addEventListener("resize", resize);

  function checkbox(container, name, color, set, isLine) {
    const label = document.createElement("label");
    label.innerHTML = `<input type="checkbox" checked> <span class="swatch${isLine ? " line" : ""}" style="background:${color}"></span>${escape(name)}`;
    label.querySelector("input").addEventListener("change", (event) => {
      if (event.target.checked) set.add(name); else set.delete(name);
      refresh(true);
    });
    container.appendChild(label);
  }
  function buildControls() {
    const view = DATA.view, focus = view.focus ? byId.get(view.focus) : null;
    sidebar("summary").textContent = `${view.kind}${focus ? " " + focus.label : ""} · depth ${view.depth} · ${nodes.length} nodes, ${edges.length} edges`;
    relationNames.forEach((name) => checkbox(sidebar("relations"), name, RELATION_COLORS[name] || "#7f8a9e", state.relations, true));
    kindNames.forEach((name) => checkbox(sidebar("kinds"), name, KIND_COLORS[name] || "#8fa3b8", state.kinds, false));
    sidebar("search").addEventListener("input", (event) => { state.query = event.target.value.trim().toLowerCase(); draw(); });
    sidebar("confidence").addEventListener("input", (event) => {
      state.minConfidence = Number(event.target.value); sidebar("confidence-value").textContent = state.minConfidence.toFixed(2); refresh(true);
    });
    sidebar("fit").addEventListener("click", fit);
    sidebar("relayout").addEventListener("click", () => { state.alpha = 1; });
    sidebar("expand-all").addEventListener("click", () => { stores.forEach((store) => state.expanded.add(store.id)); refresh(true); });
    sidebar("collapse-all").addEventListener("click", () => { state.expanded.clear(); refresh(true); });
    sidebar("mode-3d").addEventListener("click", () => setMode(!state.mode3d));
    sidebar("spin").addEventListener("click", () => { state.spin = !state.spin; sidebar("spin").classList.toggle("active", state.spin); });
  }
  function buildInsights() {
    const insights = DATA.insights;
    const list = (items, field) => items.map((item) => `${escape(item.label)} ${item[field]}`).join(", ") || "none";
    const counts = (values) => Object.entries(values).map(([key, value]) => `${value} ${key}`).join(", ") || "none";
    const lines = [
      `<p>${insights.projects} projects (${counts(insights.project_statuses)}), ${insights.domains} domains, ${insights.records} records, ${insights.edges} edges.</p>`,
      `<p><b>Hubs:</b> ${list(insights.hubs, "degree")}</p>`,
      `<p><b>Most referenced:</b> ${list(insights.most_referenced, "in_degree")}</p>`,
      `<p><b>Orphans:</b> ${insights.orphans.length ? insights.orphans.map(escape).join(", ") : "none"}</p>`,
      `<p><b>Weak edges:</b> ${insights.weak_edges} of ${insights.relationship_edges}</p>`,
    ];
    insights.domain_coverage.forEach((domain) => lines.push(
      `<p><b>Domain ${escape(domain.id)}:</b> ${domain.members} members (${counts(domain.member_statuses)}), ${domain.records} records, ${domain.internal_edges} internal edges` +
      (domain.isolated_members.length ? `; isolated: ${domain.isolated_members.map(escape).join(", ")}` : "") + "</p>"));
    const level = insights.record_level;
    if (level && level.records) {
      lines.push(`<p><b>Records:</b> ${level.records} nodes, ${counts(level.relations)}; ${level.unconnected} unconnected; most mentioned: ${list(level.most_mentioned, "mentions")}</p>`);
    }
    sidebar("insights").innerHTML = lines.join("");
  }

  function loop() {
    tick();
    const spinning = state.mode3d && state.spin && !state.drag;
    if (spinning) state.cam.rotY += 0.003;
    if (state.alpha >= 0.002 || spinning) draw();
    requestAnimationFrame(loop);
  }
  buildControls(); buildInsights(); select(null); resize(); refresh(false);
  setTimeout(fit, 900);
  loop();
})();
