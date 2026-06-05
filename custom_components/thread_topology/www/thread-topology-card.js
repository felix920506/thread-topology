/*
 * Thread Topology Card
 *
 * A custom Lovelace card that renders the Thread mesh as a force-directed graph:
 * routers as coloured hexagons (leader / connected OTBR / router), the
 * router-to-router mesh links coloured + labelled by link quality, and each
 * router's children as small nodes hanging off it.
 *
 * It reads everything from the `nodes` attribute of the Thread Topology Map
 * sensor (default: sensor.thread_topology_map), so it needs no extra data.
 *
 * Config:
 *   type: custom:thread-topology-card
 *   entity: sensor.thread_topology_map   # optional
 *   title: Thread Network               # optional
 *   height: 420                         # optional, px
 */

const LQ_COLORS = {
  3: "#2ecc71", // excellent - green
  2: "#f39c12", // good - amber
  1: "#e67e22", // fair - orange
  0: "#e74c3c", // poor - red
};
const LQ_UNKNOWN = "#9aa0a6";

function lqColor(lq) {
  return lq === null || lq === undefined ? LQ_UNKNOWN : LQ_COLORS[Math.max(0, Math.min(3, lq))];
}

function rlocHex(v) {
  return "0x" + Number(v || 0).toString(16).padStart(4, "0");
}

class ThreadTopologyCard extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._lastSignature = null;
  }

  setConfig(config) {
    this._config = {
      entity: config.entity || "sensor.thread_topology_map",
      title: config.title ?? "Thread Network",
      height: config.height || 420,
      ...config,
    };
  }

  set hass(hass) {
    this._hass = hass;
    this._render();
  }

  getCardSize() {
    return Math.ceil((this._config?.height || 420) / 50);
  }

  static getStubConfig() {
    return { entity: "sensor.thread_topology_map" };
  }

  /* ----- data ----- */

  _buildGraph(nodes) {
    // routers keyed by extAddress; also index by rloc16 so mesh connections
    // (which reference a neighbour routerId) can be resolved to a router node.
    const routers = [];
    const byRloc = new Map();
    for (const [ext, n] of Object.entries(nodes)) {
      const node = {
        id: "r:" + ext,
        kind: "router",
        ext,
        rloc16: n.rloc16 || 0,
        role: n.role || "router",
        isOtbr: !!n.is_otbr,
        name: n.name || rlocHex(n.rloc16),
        lq: n.link_quality === undefined ? null : n.link_quality,
        x: 0, y: 0, vx: 0, vy: 0,
      };
      routers.push(node);
      byRloc.set(node.rloc16 & 0xfc00, node);
    }

    const graphNodes = [...routers];
    const meshLinks = new Map(); // key: unordered rloc pair
    const childLinks = [];

    for (const [ext, n] of Object.entries(nodes)) {
      const router = routers.find((r) => r.ext === ext);
      // mesh links (router <-> router), de-duplicated
      for (const c of n.connections || []) {
        const bRloc = ((c.router_id || 0) << 10) & 0xffff;
        const other = byRloc.get(bRloc);
        if (!other || other === router) continue;
        const key = [router.rloc16, bRloc].sort((a, b) => a - b).join("-");
        if (meshLinks.has(key)) continue;
        const lqOut = c.lq_out ?? 0;
        const lqIn = c.lq_in ?? 0;
        meshLinks.set(key, {
          a: router,
          b: other,
          lq: Math.min(lqOut, lqIn),
          lqOut,
          lqIn,
          cost: c.cost ?? 0,
        });
      }
      // children hang off their router
      (n.children || []).forEach((child, i) => {
        const cnode = {
          id: "c:" + ext + ":" + (child.rloc16 || i),
          kind: "child",
          rloc16: child.rloc16 || 0,
          ctype: child.type || "sleepy",
          name: child.name || "Device",
          x: 0, y: 0, vx: 0, vy: 0,
        };
        graphNodes.push(cnode);
        childLinks.push({ a: router, b: cnode });
      });
    }

    return { graphNodes, routers, meshLinks: [...meshLinks.values()], childLinks };
  }

  /* ----- layout (lightweight force-directed simulation) ----- */

  _layout(graph, width, height) {
    const { graphNodes, meshLinks, childLinks } = graph;
    const n = graphNodes.length;
    if (n === 0) return;

    const cx = width / 2;
    const cy = height / 2;
    // Deterministic initial placement on a circle (stable between renders).
    graphNodes.forEach((nd, i) => {
      const a = (2 * Math.PI * i) / n;
      const radius = Math.min(width, height) * 0.32;
      nd.x = cx + radius * Math.cos(a);
      nd.y = cy + radius * Math.sin(a);
      nd.vx = 0;
      nd.vy = 0;
    });

    const springs = [
      ...meshLinks.map((l) => ({ a: l.a, b: l.b, len: 170, k: 0.04 })),
      ...childLinks.map((l) => ({ a: l.a, b: l.b, len: 64, k: 0.12 })),
    ];

    const REPULSION = 4200;
    const DAMPING = 0.85;
    const GRAVITY = 0.015;
    const iterations = 420;

    for (let it = 0; it < iterations; it++) {
      // pairwise repulsion
      for (let i = 0; i < n; i++) {
        const a = graphNodes[i];
        for (let j = i + 1; j < n; j++) {
          const b = graphNodes[j];
          let dx = a.x - b.x;
          let dy = a.y - b.y;
          let d2 = dx * dx + dy * dy;
          if (d2 < 0.01) {
            dx = Math.random() - 0.5;
            dy = Math.random() - 0.5;
            d2 = 0.01;
          }
          const d = Math.sqrt(d2);
          const f = REPULSION / d2;
          const fx = (dx / d) * f;
          const fy = (dy / d) * f;
          a.vx += fx; a.vy += fy;
          b.vx -= fx; b.vy -= fy;
        }
      }
      // spring attraction along links
      for (const s of springs) {
        let dx = s.b.x - s.a.x;
        let dy = s.b.y - s.a.y;
        const d = Math.sqrt(dx * dx + dy * dy) || 0.01;
        const f = (d - s.len) * s.k;
        const fx = (dx / d) * f;
        const fy = (dy / d) * f;
        s.a.vx += fx; s.a.vy += fy;
        s.b.vx -= fx; s.b.vy -= fy;
      }
      // gravity toward centre + integrate
      for (const nd of graphNodes) {
        nd.vx += (cx - nd.x) * GRAVITY;
        nd.vy += (cy - nd.y) * GRAVITY;
        nd.vx *= DAMPING;
        nd.vy *= DAMPING;
        nd.x += nd.vx;
        nd.y += nd.vy;
      }
    }
  }

  /* ----- rendering ----- */

  _hexPath(cx, cy, r) {
    let p = "";
    for (let i = 0; i < 6; i++) {
      const a = (Math.PI / 3) * i - Math.PI / 2;
      p += (i === 0 ? "M" : "L") + (cx + r * Math.cos(a)).toFixed(1) + " " + (cy + r * Math.sin(a)).toFixed(1) + " ";
    }
    return p + "Z";
  }

  _routerColor(r) {
    if (r.role === "leader") return "#e74c3c";
    if (r.isOtbr) return "#4a90e2";
    return "#f5a623";
  }

  _svg(graph, width, height) {
    const { routers, meshLinks, childLinks, graphNodes } = graph;

    // fit viewBox to node bounds
    let minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity;
    for (const nd of graphNodes) {
      minX = Math.min(minX, nd.x); maxX = Math.max(maxX, nd.x);
      minY = Math.min(minY, nd.y); maxY = Math.max(maxY, nd.y);
    }
    const pad = 60;
    const vbX = minX - pad, vbY = minY - pad;
    const vbW = Math.max(maxX - minX + 2 * pad, 10);
    const vbH = Math.max(maxY - minY + 2 * pad, 10);

    const parts = [];
    parts.push(
      `<svg viewBox="${vbX.toFixed(1)} ${vbY.toFixed(1)} ${vbW.toFixed(1)} ${vbH.toFixed(1)}" ` +
        `width="100%" height="${height}" preserveAspectRatio="xMidYMid meet" font-family="var(--paper-font-body1_-_font-family, sans-serif)">`
    );

    // child links (dashed, neutral)
    for (const l of childLinks) {
      parts.push(
        `<line x1="${l.a.x.toFixed(1)}" y1="${l.a.y.toFixed(1)}" x2="${l.b.x.toFixed(1)}" y2="${l.b.y.toFixed(1)}" ` +
          `stroke="var(--divider-color, #9aa0a6)" stroke-width="1" stroke-dasharray="4 3" opacity="0.7"/>`
      );
    }

    // mesh links (coloured by link quality) + LQ label
    for (const l of meshLinks) {
      const color = lqColor(l.lq);
      const mx = (l.a.x + l.b.x) / 2;
      const my = (l.a.y + l.b.y) / 2;
      const w = l.lq >= 3 ? 3 : l.lq >= 2 ? 2.2 : 1.4;
      parts.push(
        `<line x1="${l.a.x.toFixed(1)}" y1="${l.a.y.toFixed(1)}" x2="${l.b.x.toFixed(1)}" y2="${l.b.y.toFixed(1)}" ` +
          `stroke="${color}" stroke-width="${w}" opacity="0.9"/>`
      );
      parts.push(
        `<text x="${mx.toFixed(1)}" y="${my.toFixed(1)}" font-size="11" fill="${color}" ` +
          `text-anchor="middle" dy="-3" font-weight="600">LQ ${l.lqOut}/${l.lqIn}</text>`
      );
    }

    // child nodes
    for (const nd of graphNodes) {
      if (nd.kind !== "child") continue;
      const fill = nd.ctype === "sleepy" ? "#6b7280" : "#10b981";
      parts.push(
        `<circle cx="${nd.x.toFixed(1)}" cy="${nd.y.toFixed(1)}" r="7" fill="${fill}" stroke="#fff" stroke-width="1"/>`
      );
      parts.push(
        `<text x="${nd.x.toFixed(1)}" y="${(nd.y + 20).toFixed(1)}" font-size="10" ` +
          `fill="var(--secondary-text-color, #888)" text-anchor="middle">${this._esc(nd.name)}</text>`
      );
      parts.push(
        `<text x="${nd.x.toFixed(1)}" y="${(nd.y + 31).toFixed(1)}" font-size="9" ` +
          `fill="var(--secondary-text-color, #888)" text-anchor="middle" opacity="0.7">${rlocHex(nd.rloc16)}</text>`
      );
    }

    // router nodes (hexagons)
    for (const r of routers) {
      const color = this._routerColor(r);
      parts.push(`<path d="${this._hexPath(r.x, r.y, 16)}" fill="${color}" stroke="#fff" stroke-width="2"/>`);
      if (r.role === "leader") {
        parts.push(
          `<text x="${r.x.toFixed(1)}" y="${(r.y + 4).toFixed(1)}" font-size="13" text-anchor="middle">👑</text>`
        );
      }
      const label = r.name + (r.isOtbr ? " 🌐" : "");
      parts.push(
        `<text x="${r.x.toFixed(1)}" y="${(r.y + 32).toFixed(1)}" font-size="12" font-weight="600" ` +
          `fill="var(--primary-text-color, #111)" text-anchor="middle">${this._esc(label)}</text>`
      );
      parts.push(
        `<text x="${r.x.toFixed(1)}" y="${(r.y + 45).toFixed(1)}" font-size="10" ` +
          `fill="var(--secondary-text-color, #888)" text-anchor="middle">${rlocHex(r.rloc16)}</text>`
      );
    }

    parts.push("</svg>");
    return parts.join("");
  }

  _esc(s) {
    return String(s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
  }

  _legend() {
    return (
      `<div class="legend">` +
      `<span><span class="dot" style="background:#e74c3c"></span>Leader</span>` +
      `<span><span class="dot" style="background:#4a90e2"></span>Connected OTBR</span>` +
      `<span><span class="dot" style="background:#f5a623"></span>Router</span>` +
      `<span><span class="dot" style="background:#6b7280"></span>Sleepy child</span>` +
      `<span><span class="bar" style="background:#2ecc71"></span>LQ 3 → 0<span class="bar" style="background:#e74c3c"></span></span>` +
      `</div>`
    );
  }

  _render() {
    if (!this._hass || !this._config) return;
    const stateObj = this._hass.states[this._config.entity];

    if (!stateObj) {
      this._renderError(`Entity not found: ${this._config.entity}`);
      return;
    }
    const nodes = stateObj.attributes.nodes;
    if (!nodes || Object.keys(nodes).length === 0) {
      this._renderError("No routers in topology yet.");
      return;
    }

    // Only re-layout when the data actually changed (avoids re-running the
    // simulation on every unrelated hass update).
    const signature = JSON.stringify(
      Object.entries(nodes).map(([e, n]) => [e, n.rloc16, n.role, (n.children || []).length, (n.connections || []).length])
    );
    if (signature === this._lastSignature && this.shadowRoot.querySelector("svg")) return;
    this._lastSignature = signature;

    const width = this.clientWidth || 600;
    const height = this._config.height;
    const graph = this._buildGraph(nodes);
    this._layout(graph, width, height - 40);
    const svg = this._svg(graph, width, height - 40);

    this.shadowRoot.innerHTML = `
      <style>
        ha-card { padding: 12px 8px 8px; overflow: hidden; }
        .title { font-size: 1.1rem; font-weight: 500; padding: 4px 8px 8px; color: var(--primary-text-color); }
        .graph { width: 100%; }
        .legend { display: flex; flex-wrap: wrap; gap: 10px 16px; padding: 8px 12px 4px; font-size: 12px; color: var(--secondary-text-color); }
        .legend span { display: inline-flex; align-items: center; gap: 4px; }
        .dot { width: 10px; height: 10px; border-radius: 50%; display: inline-block; }
        .bar { width: 16px; height: 4px; border-radius: 2px; display: inline-block; }
        .err { padding: 24px; text-align: center; color: var(--secondary-text-color); }
      </style>
      <ha-card>
        ${this._config.title ? `<div class="title">${this._esc(this._config.title)}</div>` : ""}
        <div class="graph">${svg}</div>
        ${this._legend()}
      </ha-card>
    `;
  }

  _renderError(msg) {
    this.shadowRoot.innerHTML = `
      <style>
        ha-card { padding: 12px; }
        .title { font-size: 1.1rem; font-weight: 500; padding: 4px 8px; color: var(--primary-text-color); }
        .err { padding: 24px; text-align: center; color: var(--secondary-text-color); }
      </style>
      <ha-card>
        ${this._config?.title ? `<div class="title">${this._esc(this._config.title)}</div>` : ""}
        <div class="err">${this._esc(msg)}</div>
      </ha-card>
    `;
    this._lastSignature = null;
  }
}

customElements.define("thread-topology-card", ThreadTopologyCard);

window.customCards = window.customCards || [];
window.customCards.push({
  type: "thread-topology-card",
  name: "Thread Topology Card",
  description: "Force-directed graph of the Thread mesh (routers, mesh links, children).",
  preview: false,
  documentation: "https://github.com/felix920506/thread-topology",
});

console.info("%c THREAD-TOPOLOGY-CARD ", "background:#4a90e2;color:#fff;border-radius:3px");
