"""The stored graph as a networkx graph: clustered, exported, drawn.

Read by the indexer, which clusters what it wrote, and by the viewer, which
draws it. It needs the extractor's own cluster and export code, so only
those two install what this module imports.
"""

from __future__ import annotations

import logging
from collections import Counter

import networkx as nx
from graphify.cluster import cluster
from graphify.export import to_html, to_json
from psycopg2.extensions import cursor as Cursor

from enggraph.core.storage import iter_edges, iter_nodes, store_communities

LOG = logging.getLogger(__name__)


def db_to_graph(cursor: Cursor, project: str) -> nx.Graph:
    """Rebuild the whole graph in memory, in the shape graphifyy expects.

    Node attributes follow its own export: `label`, `source_file`,
    `community`. `summary` is ours and has no counterpart there, which is the
    point - it rides along into its HTML view and its MCP tools.
    """
    graph = nx.Graph()
    for node_key, name, kind, file_path, summary, community in iter_nodes(
        cursor, project
    ):
        attrs: dict[str, object] = {
            "label": name,
            "kind": kind,
            "source_file": file_path or "",
            "summary": summary or "",
        }
        if community:
            attrs["community"] = int(community)
        graph.add_node(node_key, **attrs)

    for source_id, target_id, relation, confidence in iter_edges(cursor, project):
        if source_id is None or target_id is None:
            continue
        graph.add_edge(
            source_id,
            target_id,
            relation=relation,
            confidence=confidence,
        )
    return graph


def communities_of(graph: nx.Graph) -> dict[int, list[str]]:
    """Group the nodes of a graph by the community stored on them."""
    grouped: dict[int, list[str]] = {}
    for node_key, data in graph.nodes(data=True):
        community_id = data.get("community")
        if community_id is not None:
            grouped.setdefault(int(community_id), []).append(node_key)
    return grouped


def recluster(cursor: Cursor, project: str) -> int:
    """Cluster the merged graph and store the result on the nodes."""
    graph = db_to_graph(cursor, project)
    if graph.number_of_nodes() == 0:
        return 0
    grouped = cluster(graph)
    sizes = Counter(len(members) for members in grouped.values())
    LOG.info("Clustered into %d communities (sizes %s)", len(grouped), dict(sizes))
    return store_communities(cursor, project, grouped)


def annotate_for_graphifyy(graph: nx.Graph) -> nx.Graph:
    """Fold our own attributes into the two fields graphifyy renders.

    Neither its page nor its `get_node` tool shows an attribute it was not
    written to expect: both build a fixed block of label, type, source and
    degree. A summary therefore has nowhere of its own to arrive in, so it is
    appended to the source line, and our node type is copied into the
    `file_type` field its `Type:` line reads. The searchable side effect is
    deliberate - its `query_graph` scores on the source string, so summary
    words become searchable there too.
    """
    annotated = graph.copy()
    for _, data in annotated.nodes(data=True):
        data["file_type"] = data.get("kind", "")
        summary = data.get("summary")
        if summary:
            source_file = data.get("source_file") or ""
            separator = " - " if source_file else ""
            data["source_file"] = f"{source_file}{separator}{summary}"
    return annotated


def community_labels(graph: nx.Graph, grouped: dict[int, list[str]]) -> dict[int, str]:
    """Name each community after its most connected member.

    The legend on the rendered page is built from the labels it is given and
    is empty without them. Upstream fills them in by asking a model; naming a
    community after the node everything in it hangs off is free and says
    roughly the same thing. Communities of one are left out: there are more of
    them than of everything else, and a legend listing each would bury the
    groups worth seeing.
    """
    labels: dict[int, str] = {}
    for community_id, members in grouped.items():
        if len(members) < 2:
            continue
        hub = max(members, key=lambda node_key: graph.degree(node_key))
        labels[community_id] = str(graph.nodes[hub].get("label", hub))
    return labels


def materialize_graph_json(cursor: Cursor, project: str, output_path: str) -> None:
    """Write the database out as the graph.json graphifyy tools read."""
    graph = annotate_for_graphifyy(db_to_graph(cursor, project))
    to_json(graph, communities_of(graph), output_path)


def render_html(cursor: Cursor, project: str, output_path: str) -> None:
    """Render the database as the interactive graph page."""
    graph = annotate_for_graphifyy(db_to_graph(cursor, project))
    grouped = communities_of(graph)
    to_html(
        graph,
        grouped,
        output_path,
        community_labels=community_labels(graph, grouped),
    )
