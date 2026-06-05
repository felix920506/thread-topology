/*
 * Thread Topology Card
 *
 * A custom Lovelace card that renders the Thread mesh as an interactive graph
 * using vis-network (bundled with the integration, served from the same path):
 *   - routers as coloured hexagons (leader / connected OTBR / router)
 *   - router-to-router mesh links coloured + labelled by link quality
 *   - each router's children as small nodes hanging off it
 * The graph supports drag, zoom, pan and hover tooltips. A router with no mesh
 * links shows as detached, making re-parenting / dropped routers obvious.
 *
 * It reads everything from the `nodes` attribute of the Thread Topology Map
 * sensor (default: sensor.thread_topology_map), so it needs no extra data.
 *
 * Config:
 *   type: custom:thread-topology-card
 *   entity: sensor.thread_topology_map   # optional
 *   title: Thread Network               # optional
 *   height: 460                         # optional, px
 *   physics: true                       # optional, keep nodes settling/draggable
 */

const VIS_URL = "/thread_topology/vis-network.min.js";

const ROUTER_COLORS = {
  leader: "#e74c3c", // red
  otbr: "#4a90e2", // blue
  router: "#f5a623", // amber
};
const CHILD_COLORS = {
  sleepy: "#6b7280", // grey
  active: "#10b981", // green
};
// Brightened so low-quality links (1 / 0) stay visible on a dark dashboard
// theme — dark red/orange hairlines were effectively invisible before.
const LQ_COLORS = { 3: "#2ecc71", 2: "#ffd21a", 1: "#ff8c1a", 0: "#ff4d4d" };
const LQ_UNKNOWN = "#b0b6be";
const CHILD_LINK_COLOR = "#9aa0a6";

function lqColor(lq) {
  return lq === null || lq === undefined ? LQ_UNKNOWN : LQ_COLORS[Math.max(0, Math.min(3, lq))];
}
function rlocHex(v) {
  return "0x" + Number(v || 0).toString(16).padStart(4, "0");
}

// Load the bundled vis-network UMD once and resolve to the global `vis`.
function loadVis() {
  if (window.vis && window.vis.Network) return Promise.resolve(window.vis);
  if (!window.__threadTopologyVisPromise) {
    window.__threadTopologyVisPromise = new Promise((resolve, reject) => {
      const script = document.createElement("script");
      script.src = VIS_URL;
      script.onload = () => resolve(window.vis);
      script.onerror = () => reject(new Error("Failed to load vis-network from " + VIS_URL));
      document.head.appendChild(script);
    });
  }
  return window.__threadTopologyVisPromise;
}

class ThreadTopologyCard extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._network = null;
    this._datasets = null;
    this._lastSignature = null;
    this._built = false;
  }

  setConfig(config) {
    this._config = {
      entity: config.entity || "sensor.thread_topology_map",
      title: config.title ?? "Thread Network",
      height: config.height || 460,
      physics: config.physics !== false,
      ...config,
    };
    this._built = false; // force a rebuild of the shell on reconfig
  }

  set hass(hass) {
    this._hass = hass;
    this._update();
  }

  getCardSize() {
    return Math.ceil((this._config?.height || 460) / 50);
  }

  static getStubConfig() {
    return { entity: "sensor.thread_topology_map" };
  }

  /* ----- build the static shell (card + container) once ----- */
  _buildShell() {
    const themeColor = (name, fallback) =>
      getComputedStyle(this).getPropertyValue(name).trim() || fallback;
    this._textColor = themeColor("--primary-text-color", "#e1e1e1");
    this._secondaryColor = themeColor("--secondary-text-color", "#9aa0a6");

    this.shadowRoot.innerHTML = `
      <style>
        ha-card { padding: 12px 8px 8px; overflow: hidden; }
        .title { font-size: 1.1rem; font-weight: 500; padding: 4px 8px 8px; color: var(--primary-text-color); }
        #graph { width: 100%; height: ${this._config.height}px; }
        .legend { display: flex; flex-wrap: wrap; gap: 10px 16px; padding: 8px 12px 4px; font-size: 12px; color: var(--secondary-text-color); }
        .legend span { display: inline-flex; align-items: center; gap: 4px; }
        .dot { width: 10px; height: 10px; border-radius: 50%; display: inline-block; }
        .bar { width: 16px; height: 4px; border-radius: 2px; display: inline-block; }
        .msg { padding: 24px; text-align: center; color: var(--secondary-text-color); }
      </style>
      <ha-card>
        ${this._config.title ? `<div class="title">${this._esc(this._config.title)}</div>` : ""}
        <div id="graph"></div>
        <div class="legend">
          <span><span class="dot" style="background:${ROUTER_COLORS.leader}"></span>Leader</span>
          <span><span class="dot" style="background:${ROUTER_COLORS.otbr}"></span>Connected OTBR</span>
          <span><span class="dot" style="background:${ROUTER_COLORS.router}"></span>Router</span>
          <span><span class="dot" style="background:${CHILD_COLORS.sleepy}"></span>Sleepy child</span>
          <span><span class="bar" style="background:${LQ_COLORS[3]}"></span>LQ 3 → 0<span class="bar" style="background:${LQ_COLORS[0]}"></span></span>
        </div>
      </ha-card>
    `;
    this._container = this.shadowRoot.getElementById("graph");
    this._built = true;
    this._network = null;
    this._datasets = null;
  }

  _message(msg) {
    if (!this._built) this._buildShell();
    if (this._container) {
      this._container.innerHTML = `<div class="msg">${this._esc(msg)}</div>`;
    }
    if (this._network) {
      this._network.destroy();
      this._network = null;
    }
    this._lastSignature = null;
  }

  /* ----- translate sensor nodes -> vis nodes/edges ----- */
  _buildGraphData(nodes) {
    const visNodes = [];
    const visEdges = [];
    const byRloc = new Map();

    for (const [ext, n] of Object.entries(nodes)) {
      const rloc = n.rloc16 || 0;
      const role = n.role === "leader" ? "leader" : n.is_otbr ? "otbr" : "router";
      const color = ROUTER_COLORS[role];
      const crown = n.role === "leader" ? "👑 " : "";
      const globe = n.is_otbr ? " 🌐" : "";
      const lqTxt = ["Poor", "Fair", "Good", "Excellent"];
      const lq = n.link_quality;
      visNodes.push({
        id: "r:" + ext,
        label: `${crown}${n.name || rlocHex(rloc)}${globe}\n${rlocHex(rloc)}`,
        shape: "hexagon",
        size: 22,
        color: { background: color, border: "#ffffff", highlight: { background: color, border: "#ffffff" } },
        borderWidth: 2,
        font: { color: this._textColor, size: 14, multi: false, vadjust: 0 },
        title: `${n.name || rlocHex(rloc)} (${rlocHex(rloc)})\nRole: ${
          n.role === "leader" ? "Leader" : "Router"
        }${n.is_otbr ? " · connected OTBR" : ""}\nLink quality: ${
          typeof lq === "number" ? lqTxt[Math.min(lq, 3)] : "Unknown"
        }`,
        _kind: "router",
      });
      byRloc.set(rloc & 0xfc00, "r:" + ext);
    }

    for (const [ext, n] of Object.entries(nodes)) {
      // mesh links (router <-> router), de-duplicated via undirected edge ids
      for (const c of n.connections || []) {
        const bRloc = ((c.router_id || 0) << 10) & 0xffff;
        const target = byRloc.get(bRloc);
        const source = "r:" + ext;
        if (!target || target === source) continue;
        const pair = [source, target].sort();
        const edgeId = "m:" + pair[0] + "|" + pair[1];
        if (visEdges.some((e) => e.id === edgeId)) continue;
        const lqOut = c.lq_out ?? 0;
        const lqIn = c.lq_in ?? 0;
        const lq = Math.min(lqOut, lqIn);
        visEdges.push({
          id: edgeId,
          from: source,
          to: target,
          label: `LQ ${lqOut}/${lqIn}`,
          color: { color: lqColor(lq), highlight: lqColor(lq) },
          // Keep a generous minimum width so poor links are still easy to see.
          width: lq >= 3 ? 3.5 : lq >= 2 ? 3 : 2.5,
          font: { color: lqColor(lq), size: 12, strokeWidth: 3, strokeColor: "rgba(0,0,0,0.45)", align: "top" },
          smooth: false,
          title: `cost ${c.cost ?? 0}`,
        });
      }
      // children hang off their router with a dashed link
      (n.children || []).forEach((child, i) => {
        const cid = "c:" + ext + ":" + (child.rloc16 || i);
        const ctype = child.type === "active" ? "active" : "sleepy";
        visNodes.push({
          id: cid,
          label: `${child.name || "Device"}\n${rlocHex(child.rloc16)}`,
          shape: "dot",
          size: 8,
          color: { background: CHILD_COLORS[ctype], border: "#ffffff" },
          borderWidth: 1,
          font: { color: this._secondaryColor, size: 11 },
          title: `${child.name || "Device"} (${rlocHex(child.rloc16)})\n${
            ctype === "sleepy" ? "Sleepy end device" : "Active end device"
          }`,
          _kind: "child",
        });
        visEdges.push({
          id: "ce:" + cid,
          from: "r:" + ext,
          to: cid,
          dashes: [4, 4],
          color: { color: CHILD_LINK_COLOR, opacity: 0.85 },
          width: 1.4,
          smooth: false,
        });
      });
    }

    return { visNodes, visEdges };
  }

  async _update() {
    if (!this._hass || !this._config) return;
    if (!this._built) this._buildShell();

    const stateObj = this._hass.states[this._config.entity];
    if (!stateObj) {
      this._message(`Entity not found: ${this._config.entity}`);
      return;
    }
    const nodes = stateObj.attributes.nodes;
    if (!nodes || Object.keys(nodes).length === 0) {
      this._message("No routers in topology yet.");
      return;
    }

    // Only rebuild the graph data when the topology actually changed.
    const signature = JSON.stringify(
      Object.entries(nodes).map(([e, n]) => [
        e, n.rloc16, n.role, n.is_otbr, n.link_quality,
        (n.children || []).map((c) => [c.rloc16, c.name, c.type]),
        (n.connections || []).map((c) => [c.router_id, c.lq_out, c.lq_in, c.cost]),
      ])
    );
    if (signature === this._lastSignature && this._network) return;
    this._lastSignature = signature;

    let vis;
    try {
      vis = await loadVis();
    } catch (err) {
      this._message("Could not load the graph library (vis-network).");
      return;
    }
    // hass may have changed while awaiting; re-read guard
    if (!this._container) return;

    const { visNodes, visEdges } = this._buildGraphData(nodes);

    if (!this._network) {
      this._datasets = {
        nodes: new vis.DataSet(visNodes),
        edges: new vis.DataSet(visEdges),
      };
      this._network = new vis.Network(
        this._container,
        this._datasets,
        this._networkOptions()
      );
    } else {
      // Update in place so the view (zoom/pan) and node positions are kept.
      this._syncDataset(this._datasets.nodes, visNodes);
      this._syncDataset(this._datasets.edges, visEdges);
    }
  }

  _syncDataset(dataset, items) {
    const incoming = new Map(items.map((it) => [it.id, it]));
    const existing = new Set(dataset.getIds());
    dataset.update(items);
    for (const id of existing) {
      if (!incoming.has(id)) dataset.remove(id);
    }
  }

  _networkOptions() {
    return {
      autoResize: true,
      layout: { improvedLayout: true },
      physics: {
        enabled: this._config.physics,
        solver: "forceAtlas2Based",
        forceAtlas2Based: { gravitationalConstant: -60, springLength: 130, springConstant: 0.08, avoidOverlap: 1 },
        stabilization: { iterations: 250, fit: true },
      },
      nodes: { shadow: false, labelHighlightBold: false },
      edges: { selectionWidth: 1.5 },
      interaction: {
        hover: true,
        dragNodes: true,
        dragView: true,
        zoomView: true,
        tooltipDelay: 150,
        navigationButtons: false,
      },
    };
  }

  _esc(s) {
    return String(s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
  }

  disconnectedCallback() {
    if (this._network) {
      this._network.destroy();
      this._network = null;
    }
  }
}

customElements.define("thread-topology-card", ThreadTopologyCard);

window.customCards = window.customCards || [];
window.customCards.push({
  type: "thread-topology-card",
  name: "Thread Topology Card",
  description: "Interactive force-directed graph of the Thread mesh (routers, mesh links, children).",
  preview: false,
  documentation: "https://github.com/felix920506/thread-topology",
});

console.info("%c THREAD-TOPOLOGY-CARD ", "background:#4a90e2;color:#fff;border-radius:3px");
