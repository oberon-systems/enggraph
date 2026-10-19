"""The graph page: the drawing data of one graph, inlined into one document.

The styles and the script are files of this package, read once and inlined
too, so the page is a single response: the dashboard proxies it without a
route of its own for either.
"""

from __future__ import annotations

import html
import json
from pathlib import Path

import networkx as nx

HERE = Path(__file__).parent
STYLES = (HERE / "graph.css").read_text(encoding="utf-8")
SCRIPT = (HERE / "graph.js").read_text(encoding="utf-8")

# Past this the browser spends minutes laying the graph out.
MAX_NODES = 5000
COMMUNITY_COLORS = (
    "#4E79A7",
    "#F28E2B",
    "#E15759",
    "#76B7B2",
    "#59A14F",
    "#EDC948",
    "#B07AA1",
    "#FF9DA7",
    "#9C755F",
    "#BAB0AC",
)
# A label is drawn for a node at least this connected, relative to the most
# connected one; the rest show theirs on hover.
LABELED_DEGREE = 0.15
EXTRACTED = "EXTRACTED"

PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Code graph</title>
<script src="{library}"></script>
<style>
{styles}
</style>
</head>
<body>
<div id="graph"></div>
<div id="sidebar">
  <div id="search-wrap">
    <input id="search" type="text" placeholder="Search nodes..." autocomplete="off">
    <div id="search-results"></div>
  </div>
  <div id="info-panel">
    <h3>Node</h3>
    <div id="info-content"><span class="empty">Click a node to inspect it</span></div>
  </div>
  <div id="legend-wrap">
    <h3>Communities</h3>
    <div id="legend"></div>
  </div>
  <div id="stats">{stats}</div>
</div>
<script type="application/json" id="graph-data">{data}</script>
<script>
{script}
</script>
</body>
</html>
"""


def color_of(community_id: int) -> str:
    """Return the colour a community is drawn in."""
    return COMMUNITY_COLORS[community_id % len(COMMUNITY_COLORS)]


def drawing(
    graph: nx.Graph, grouped: dict[int, list[str]], labels: dict[int, str]
) -> dict[str, list[dict[str, object]]]:
    """Return the nodes, edges and legend the script draws."""
    community = {node: cid for cid, members in grouped.items() for node in members}
    degree = dict(graph.degree())
    top = max(degree.values(), default=1) or 1
    nodes = []
    for node_key, data in graph.nodes(data=True):
        cid = community.get(node_key, 0)
        reach = degree.get(node_key, 0)
        nodes.append(
            {
                "id": node_key,
                "label": str(data.get("label", node_key)),
                "color": color_of(cid),
                "size": round(10 + 30 * reach / top, 1),
                "labeled": reach >= top * LABELED_DEGREE,
                "community": cid,
                "community_name": labels.get(cid, f"Community {cid}"),
                "kind": str(data.get("kind", "")),
                "source_file": str(data.get("source_file", "")),
                "summary": str(data.get("summary", "")),
                "degree": reach,
            }
        )
    edges = [
        {
            "from": source,
            "to": target,
            "title": f"{data.get('relation', '')} [{data.get('confidence', '')}]",
            "extracted": data.get("confidence", EXTRACTED) == EXTRACTED,
        }
        for source, target, data in graph.edges(data=True)
    ]
    legend = [
        {
            "community": cid,
            "color": color_of(cid),
            "label": labels[cid],
            "count": len(grouped.get(cid, [])),
        }
        for cid in sorted(labels)
    ]
    return {"nodes": nodes, "edges": edges, "legend": legend}


def graph_page(
    graph: nx.Graph,
    grouped: dict[int, list[str]],
    labels: dict[int, str],
    library: str,
) -> str:
    """Return the page drawing a graph with the library at this address.

    Raises ValueError for a graph too large to lay out in a browser.
    """
    if graph.number_of_nodes() > MAX_NODES:
        raise ValueError(
            f"the graph has {graph.number_of_nodes()} nodes, more than the "
            f"{MAX_NODES} a page can lay out"
        )
    data = json.dumps(drawing(graph, grouped, labels))
    stats = (
        f"{graph.number_of_nodes()} nodes &middot; {graph.number_of_edges()} "
        f"edges &middot; {len(grouped)} communities"
    )
    return PAGE.format(
        library=html.escape(library, quote=True),
        styles=STYLES,
        stats=stats,
        data=data.replace("<", "\\u003c"),
        script=SCRIPT,
    )
