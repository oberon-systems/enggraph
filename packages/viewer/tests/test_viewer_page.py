"""The graph page drawn from a networkx graph, with no database."""

from __future__ import annotations

import json
import re

import networkx as nx
import pytest

from enggraph.viewer.page import MAX_NODES, drawing, graph_page

LIBRARY = "/vis-network.min.js"


def sample() -> tuple[nx.Graph, dict[int, list[str]], dict[int, str]]:
    """Build a hub with two leaves in one community, and a node alone."""
    graph = nx.Graph()
    graph.add_node("hub", label="hub", kind="file", source_file="src/a.py")
    graph.add_node("one", label="one", summary="Reads one thing.")
    graph.add_node("two", label="two")
    graph.add_node("alone", label="alone")
    graph.add_edge("hub", "one", relation="contains", confidence="EXTRACTED")
    graph.add_edge("hub", "two", relation="calls", confidence="INFERRED")
    return graph, {0: ["hub", "one", "two"], 1: ["alone"]}, {0: "hub"}


def page_data(page: str) -> dict[str, list[dict[str, object]]]:
    """Read back the JSON a page carries."""
    found = re.search(r'id="graph-data">(.*?)</script>', page, re.S)
    assert found is not None
    return json.loads(found[1])


def test_the_drawing_sizes_colours_and_labels_by_degree_and_community() -> None:
    """The legend lists named communities only; unnamed ones get a number."""
    data = drawing(*sample())
    nodes = {node["id"]: node for node in data["nodes"]}
    assert nodes["hub"]["size"] == 40.0
    assert nodes["one"]["size"] == 25.0
    assert nodes["alone"]["size"] == 10.0
    assert nodes["hub"]["labeled"] and not nodes["alone"]["labeled"]
    assert nodes["hub"]["color"] != nodes["alone"]["color"]
    assert nodes["alone"]["community_name"] == "Community 1"
    assert nodes["one"]["summary"] == "Reads one thing."
    assert {(one["from"], one["to"], one["extracted"]) for one in data["edges"]} == {
        ("hub", "one", True),
        ("hub", "two", False),
    }
    assert data["legend"] == [
        {"community": 0, "color": nodes["hub"]["color"], "label": "hub", "count": 3}
    ]


def test_the_page_inlines_its_data_without_closing_a_tag() -> None:
    """A label holding `</script>` stays text inside the data block."""
    graph, grouped, labels = sample()
    graph.nodes["one"]["label"] = "</script><b>x</b>"
    page = graph_page(graph, grouped, labels, LIBRARY)
    assert "</script><b>" not in page
    labels_found = {node["label"] for node in page_data(page)["nodes"]}
    assert "</script><b>x</b>" in labels_found
    assert '<script src="/vis-network.min.js"></script>' in page
    assert "<title>Code graph</title>" in page
    assert "4 nodes &middot; 2 edges &middot; 2 communities" in page


def test_the_library_address_is_escaped() -> None:
    """An address is an attribute value, quotes and all."""
    page = graph_page(*sample(), 'https://cdn.example.com/a.js?x="y"')
    assert 'src="https://cdn.example.com/a.js?x=&quot;y&quot;"' in page


def test_a_graph_too_large_to_lay_out_is_refused() -> None:
    """Past the limit the page is not built at all."""
    graph = nx.Graph()
    graph.add_nodes_from(range(MAX_NODES + 1))
    with pytest.raises(ValueError, match="more than"):
        graph_page(graph, {}, {}, LIBRARY)
