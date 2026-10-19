"""The stored graph as a networkx graph: rebuilt, clustered, grouped.

Read by the indexer, which clusters what it wrote, and by the viewer, which
draws it. Only those two install networkx.
"""

from __future__ import annotations

import logging
from collections import Counter

import networkx as nx
from psycopg2.extensions import cursor as Cursor

from enggraph.core.storage import iter_edges, iter_nodes, store_communities

LOG = logging.getLogger(__name__)

# A community holding more than this share of the graph, and at least
# CLUSTER_MIN_SPLIT nodes, is clustered once more on its own.
CLUSTER_MAX_FRACTION = 0.25
CLUSTER_MIN_SPLIT = 10
# Louvain is randomised; a fixed seed keeps a node's community from moving
# between two runs over the same graph.
CLUSTER_SEED = 42


def db_to_graph(cursor: Cursor, project: str) -> nx.Graph:
    """Rebuild the whole graph of a project in memory.

    Node attributes are `label`, `kind`, `source_file`, `summary` and, once
    clustered, `community`; edge attributes `relation` and `confidence`.
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


def louvain(graph: nx.Graph) -> list[list[str]]:
    """Partition a graph with edges into communities, each sorted."""
    found = nx.community.louvain_communities(graph, weight=None, seed=CLUSTER_SEED)
    return [sorted(one) for one in found]


def split(graph: nx.Graph, members: list[str]) -> list[list[str]]:
    """Cluster one oversized community again, on its own subgraph."""
    subgraph = graph.subgraph(members)
    if subgraph.number_of_edges() == 0:
        return [[one] for one in sorted(members)]
    parts = louvain(subgraph)
    return parts if len(parts) > 1 else [sorted(members)]


def cluster(graph: nx.Graph) -> dict[int, list[str]]:
    """Cluster a graph into communities, numbered from the largest.

    A node with no edge is a community of its own. A community holding more
    than a quarter of the graph is split by a second pass over its subgraph.
    """
    if graph.number_of_nodes() == 0:
        return {}
    isolates = sorted(one for one in graph.nodes if graph.degree(one) == 0)
    connected = graph.subgraph(one for one in graph.nodes if graph.degree(one) > 0)
    found = louvain(connected) if connected.number_of_nodes() else []
    largest = max(
        CLUSTER_MIN_SPLIT, int(graph.number_of_nodes() * CLUSTER_MAX_FRACTION)
    )
    communities: list[list[str]] = []
    for members in found:
        if len(members) > largest:
            communities.extend(split(graph, members))
        else:
            communities.append(members)
    communities.extend([one] for one in isolates)
    communities.sort(key=lambda members: (-len(members), members[0]))
    return dict(enumerate(communities))


def recluster(cursor: Cursor, project: str) -> int:
    """Cluster the merged graph and store the result on the nodes."""
    graph = db_to_graph(cursor, project)
    if graph.number_of_nodes() == 0:
        return 0
    grouped = cluster(graph)
    sizes = Counter(len(members) for members in grouped.values())
    LOG.info("Clustered into %d communities (sizes %s)", len(grouped), dict(sizes))
    return store_communities(cursor, project, grouped)


def community_labels(graph: nx.Graph, grouped: dict[int, list[str]]) -> dict[int, str]:
    """Name each community after its most connected member.

    Naming a community after the node everything in it hangs off is free and
    says roughly what a model would. Communities of one are left out: there
    are more of them than of everything else, and a legend listing each would
    bury the groups worth seeing.
    """
    labels: dict[int, str] = {}
    for community_id, members in grouped.items():
        if len(members) < 2:
            continue
        hub = max(members, key=lambda node_key: graph.degree(node_key))
        labels[community_id] = str(graph.nodes[hub].get("label", hub))
    return labels
