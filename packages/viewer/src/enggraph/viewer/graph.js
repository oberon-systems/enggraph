// Draws the graph the page carries as JSON, with vis-network. Every text
// taken from the graph goes in through textContent, never as markup.
(function () {
  "use strict";

  const DATA = JSON.parse(document.getElementById("graph-data").textContent);
  const SEARCH_LIMIT = 20;

  const nodes = new vis.DataSet(
    DATA.nodes.map((node) => ({
      id: node.id,
      label: node.label,
      title: node.label,
      size: node.size,
      color: {
        background: node.color,
        border: node.color,
        highlight: { background: "#ffffff", border: node.color },
      },
      font: { size: node.labeled ? 12 : 0, color: "#ffffff" },
    })),
  );
  const edges = new vis.DataSet(
    DATA.edges.map((edge, index) => ({
      id: index,
      from: edge.from,
      to: edge.to,
      title: edge.title,
      dashes: !edge.extracted,
      width: edge.extracted ? 2 : 1,
      color: { opacity: edge.extracted ? 0.7 : 0.35 },
      arrows: { to: { enabled: true, scaleFactor: 0.5 } },
    })),
  );
  const byId = new Map(DATA.nodes.map((node) => [node.id, node]));

  const network = new vis.Network(
    document.getElementById("graph"),
    { nodes, edges },
    {
      physics: {
        enabled: true,
        solver: "forceAtlas2Based",
        forceAtlas2Based: {
          gravitationalConstant: -60,
          centralGravity: 0.005,
          springLength: 120,
          springConstant: 0.08,
          damping: 0.4,
          avoidOverlap: 0.8,
        },
        stabilization: { iterations: 200, fit: true },
      },
      interaction: { hover: true, tooltipDelay: 100, hideEdgesOnDrag: true },
      nodes: { shape: "dot", borderWidth: 1.5 },
      edges: {
        smooth: { type: "continuous", roundness: 0.2 },
        selectionWidth: 3,
      },
    },
  );
  // Laid out once, then left still, so a click does not set it moving again.
  network.once("stabilizationIterationsDone", () => {
    network.setOptions({ physics: { enabled: false } });
  });

  function element(tag, className, text) {
    const made = document.createElement(tag);
    if (className) made.className = className;
    if (text !== undefined) made.textContent = text;
    return made;
  }

  function field(name, value) {
    const line = element("div", "field");
    line.append(element("b", "", name + ": "), value || "-");
    return line;
  }

  function nodeLink(node, className, onPick) {
    const link = element("span", className, node.label);
    link.style.borderLeftColor = node.color;
    link.addEventListener("click", onPick);
    return link;
  }

  const info = document.getElementById("info-content");

  function clearInfo() {
    info.replaceChildren(
      element("span", "empty", "Click a node to inspect it"),
    );
  }

  function showInfo(nodeId) {
    const node = byId.get(nodeId);
    if (!node) return;
    const title = element("div", "field");
    title.append(element("b", "", node.label));
    const parts = [
      title,
      field("Type", node.kind),
      field("Community", node.community_name),
      field("Source", node.source_file),
    ];
    if (node.summary) parts.push(field("Summary", node.summary));
    parts.push(field("Degree", String(node.degree)));
    const neighbours = network.getConnectedNodes(nodeId);
    if (neighbours.length) {
      parts.push(element("div", "muted", `Neighbours (${neighbours.length})`));
      for (const otherId of neighbours) {
        const other = byId.get(otherId);
        if (other) {
          parts.push(
            nodeLink(other, "neighbor-link", () => focusNode(otherId)),
          );
        }
      }
    }
    info.replaceChildren(...parts);
  }

  function focusNode(nodeId) {
    network.focus(nodeId, { scale: 1.4, animation: true });
    network.selectNodes([nodeId]);
    showInfo(nodeId);
  }

  network.on("click", (params) => {
    if (params.nodes.length) showInfo(params.nodes[0]);
    else clearInfo();
  });

  const search = document.getElementById("search");
  const results = document.getElementById("search-results");
  search.addEventListener("input", () => {
    const query = search.value.toLowerCase().trim();
    const matches = query
      ? DATA.nodes
          .filter((node) => node.label.toLowerCase().includes(query))
          .slice(0, SEARCH_LIMIT)
      : [];
    results.replaceChildren(
      ...matches.map((node) =>
        nodeLink(node, "search-item", () => {
          focusNode(node.id);
          results.style.display = "none";
          search.value = "";
        }),
      ),
    );
    results.style.display = matches.length ? "block" : "none";
  });
  document.addEventListener("click", (event) => {
    if (!results.contains(event.target) && event.target !== search) {
      results.style.display = "none";
    }
  });

  const hidden = new Set();
  const legend = document.getElementById("legend");
  for (const group of DATA.legend) {
    const item = element("div", "legend-item");
    const dot = element("div", "legend-dot");
    dot.style.background = group.color;
    item.append(
      dot,
      element("span", "legend-label", group.label),
      element("span", "legend-count", String(group.count)),
    );
    item.addEventListener("click", () => {
      const hide = !hidden.has(group.community);
      if (hide) hidden.add(group.community);
      else hidden.delete(group.community);
      item.classList.toggle("dimmed", hide);
      nodes.update(
        DATA.nodes
          .filter((node) => node.community === group.community)
          .map((node) => ({ id: node.id, hidden: hide })),
      );
    });
    legend.append(item);
  }
})();
