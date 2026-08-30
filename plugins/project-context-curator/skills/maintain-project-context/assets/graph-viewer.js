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

  const nodes = DATA.nodes.map((raw, index) => {
    const angle = index * 2.399963;
    const radius = 26 * Math.sqrt(index + 1);
    const total = Object.values(raw.counts).reduce((sum, value) => sum + value, 0);
    return Object.assign({}, raw, {
      x: Math.cos(angle) * radius, y: Math.sin(angle) * radius, z: ((index * 37) % 200) - 100,
      vx: 0, vy: 0, vz: 0, total: total, visible: false, neighbours: new Set(),
      r: STORE_KINDS.has(raw.kind) ? 7 + Math.min(20, Math.sqrt(total) * 1.6) : 3.5,
    });
  });
  const byId = new Map(nodes.map((node) => [node.id, node]));
  const edges = DATA.edges.map((raw) => Object.assign({}, raw, {s: byId.get(raw.source), t: byId.get(raw.target)}));
  edges.forEach((edge) => { edge.s.neighbours.add(edge.t.id); edge.t.neighbours.add(edge.s.id); });
  nodes.forEach((node) => {
    if (!STORE_KINDS.has(node.kind)) {
      const store = byId.get(node.store);
      if (store) { node.x = store.x + ((node.z % 23) - 11); node.y = store.y + ((node.z % 17) - 8); }
    }
  });
  const stores = nodes.filter((node) => STORE_KINDS.has(node.kind));
  const relationNames = Array.from(new Set(edges.map((edge) => edge.relation))).sort();
  const kindNames = Array.from(new Set(nodes.map((node) => node.kind)));

  const state = {
    expanded: new Set(DATA.view.level === "records" ? stores.map((node) => node.id) : []),
    relations: new Set(relationNames), kinds: new Set(kindNames), minConfidence: 0, query: "",
    selected: null, hover: null, visibleNodes: [], visibleEdges: [], alpha: 1,
    cam: {x: 0, y: 0, k: 1}, drag: null, width: 0, height: 0,
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
    if (reheat) state.alpha = Math.max(state.alpha, 0.6);
    if (state.selected && !state.selected.visible) select(null);
    draw();
  }

  function tick() {
    if (state.alpha < 0.002) return;
    const alpha = state.alpha;
    state.alpha += (0 - alpha) * 0.0228;
    const visible = state.visibleNodes;
    for (let i = 0; i < visible.length; i++) {
      const a = visible[i];
      for (let j = i + 1; j < visible.length; j++) {
        const b = visible[j];
        let dx = b.x - a.x, dy = b.y - a.y, dz = b.z - a.z;
        let l = dx * dx + dy * dy + dz * dz;
        if (l < 1) { dx = (i % 3) - 1 || 0.5; dy = (j % 3) - 1 || -0.5; l = 1; }
        if (l > 250000) continue;
        const charge = (STORE_KINDS.has(a.kind) && STORE_KINDS.has(b.kind)) ? 900 : (STORE_KINDS.has(a.kind) || STORE_KINDS.has(b.kind)) ? 220 : 60;
        const w = charge * alpha / l;
        a.vx -= dx * w; a.vy -= dy * w; a.vz -= dz * w;
        b.vx += dx * w; b.vy += dy * w; b.vz += dz * w;
      }
    }
    state.visibleEdges.forEach((edge) => {
      const a = edge.s, b = edge.t;
      const dx = b.x - a.x, dy = b.y - a.y, dz = b.z - a.z;
      const d = Math.sqrt(dx * dx + dy * dy + dz * dz) || 1;
      const target = edge.relation === "stored_in" ? 40 + a.r + b.r : (STORE_KINDS.has(a.kind) && STORE_KINDS.has(b.kind)) ? 150 : 70;
      const strength = edge.relation === "stored_in" ? 0.2 : 0.06;
      const f = (d - target) / d * strength * alpha;
      a.vx += dx * f; a.vy += dy * f; a.vz += dz * f;
      b.vx -= dx * f; b.vy -= dy * f; b.vz -= dz * f;
    });
    visible.forEach((node) => {
      if (node === (state.drag && state.drag.node)) { node.vx = node.vy = node.vz = 0; return; }
      node.vx -= node.x * 0.02 * alpha; node.vy -= node.y * 0.02 * alpha; node.vz -= node.z * 0.05 * alpha;
      node.vx *= 0.6; node.vy *= 0.6; node.vz *= 0.6;
      node.x += node.vx; node.y += node.vy; node.z += node.vz;
    });
  }

  function project(node) {
    return {x: (node.x - state.cam.x) * state.cam.k + state.width / 2, y: (node.y - state.cam.y) * state.cam.k + state.height / 2, s: 1};
  }
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
    state.visibleEdges.forEach((edge) => {
      const a = project(edge.s), b = project(edge.t);
      const strong = state.selected && (state.selected === edge || state.selected === edge.s || state.selected === edge.t);
      ctx.globalAlpha = Math.min(emphasis(edge.s), emphasis(edge.t)) * (strong ? 1 : 0.3 + edge.confidence * 0.6);
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
    state.visibleNodes.forEach((node) => {
      const p = project(node), r = node.r * k * p.s;
      const active = node === state.hover || node === state.selected;
      ctx.globalAlpha = emphasis(node);
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
    for (let i = state.visibleNodes.length - 1; i >= 0; i--) {
      const node = state.visibleNodes[i], p = project(node), r = node.r * state.cam.k * p.s + 3;
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
    const xs = state.visibleNodes.map((node) => node.x), ys = state.visibleNodes.map((node) => node.y);
    const minX = Math.min(...xs), maxX = Math.max(...xs), minY = Math.min(...ys), maxY = Math.max(...ys);
    state.cam.x = (minX + maxX) / 2; state.cam.y = (minY + maxY) / 2;
    state.cam.k = Math.max(0.05, Math.min(4, 0.9 * Math.min(state.width / (maxX - minX + 120), state.height / (maxY - minY + 120))));
    draw();
  }

  canvas.addEventListener("mousedown", (event) => {
    const hit = hitTest(event.offsetX, event.offsetY);
    state.drag = {node: hit && !hit.s ? hit : null, x: event.offsetX, y: event.offsetY, moved: false};
    canvas.classList.add("dragging");
  });
  window.addEventListener("mousemove", (event) => {
    const rect = canvas.getBoundingClientRect();
    const x = event.clientX - rect.left, y = event.clientY - rect.top;
    if (state.drag) {
      const dx = x - state.drag.x, dy = y - state.drag.y;
      if (Math.abs(dx) + Math.abs(dy) > 2) state.drag.moved = true;
      if (state.drag.node) { state.drag.node.x += dx / state.cam.k; state.drag.node.y += dy / state.cam.k; state.alpha = Math.max(state.alpha, 0.3); }
      else { state.cam.x -= dx / state.cam.k; state.cam.y -= dy / state.cam.k; }
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
    const before = {x: (event.offsetX - state.width / 2) / state.cam.k + state.cam.x, y: (event.offsetY - state.height / 2) / state.cam.k + state.cam.y};
    state.cam.k = Math.max(0.05, Math.min(8, state.cam.k * factor));
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
  }

  function loop() { tick(); if (state.alpha >= 0.002) draw(); requestAnimationFrame(loop); }
  buildControls(); select(null); resize(); refresh(false);
  setTimeout(fit, 900);
  loop();
})();
