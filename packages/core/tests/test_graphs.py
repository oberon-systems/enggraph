"""Clustering of a graph rebuilt from the database, with no database."""

from __future__ import annotations

import networkx as nx

from enggraph.core.graphs import cluster, communities_of, community_labels, split


def clique(graph: nx.Graph, names: list[str]) -> None:
    """Join every pair of these nodes."""
    for index, one in enumerate(names):
        for other in names[index + 1 :]:
            graph.add_edge(one, other)


def bridged() -> nx.Graph:
    """Two cliques of four joined by one edge, and a node with no edge."""
    graph = nx.Graph()
    clique(graph, ["a1", "a2", "a3", "a4"])
    clique(graph, ["b1", "b2", "b3", "b4"])
    graph.add_edge("a1", "b1")
    graph.add_node("z")
    return graph


def test_an_empty_graph_has_no_community() -> None:
    """Nothing to cluster is no community at all."""
    assert cluster(nx.Graph()) == {}


def test_nodes_with_no_edge_are_communities_of_one() -> None:
    """Isolates are numbered in name order."""
    graph = nx.Graph()
    graph.add_nodes_from(["b", "a"])
    assert cluster(graph) == {0: ["a"], 1: ["b"]}


def test_cliques_are_communities_numbered_from_the_largest() -> None:
    """Equal sizes fall back to the first member; isolates come last."""
    assert cluster(bridged()) == {
        0: ["a1", "a2", "a3", "a4"],
        1: ["b1", "b2", "b3", "b4"],
        2: ["z"],
    }


def test_the_same_graph_clusters_the_same_way() -> None:
    """The fixed seed keeps a community from moving between runs."""
    assert cluster(bridged()) == cluster(bridged())


def test_an_oversized_community_is_split_on_its_subgraph() -> None:
    """Cliques part on the second pass, members with no edge one each."""
    graph = bridged()
    members = ["a1", "a2", "a3", "a4", "b1", "b2", "b3", "b4"]
    assert sorted(split(graph, members)) == [
        ["a1", "a2", "a3", "a4"],
        ["b1", "b2", "b3", "b4"],
    ]
    assert split(graph, ["z", "a2"]) == [["a2"], ["z"]]


def test_communities_are_read_back_and_named_after_their_hub() -> None:
    """A community of one gets no label."""
    graph = bridged()
    for community_id, members in cluster(graph).items():
        for one in members:
            graph.nodes[one]["community"] = community_id
            graph.nodes[one]["label"] = one.upper()
    grouped = communities_of(graph)
    assert sorted(grouped) == [0, 1, 2]
    assert community_labels(graph, grouped) == {0: "A1", 1: "B1"}
