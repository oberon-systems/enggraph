"""The graph page drawn from a networkx graph, with no database."""

from __future__ import annotations

import json
import re

import networkx as nx
import pytest

from enggraph.viewer import page, viewer
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


class Counted:
    """A cursor answering the two counts `weigh` asks for."""

    def __init__(self, nodes: int, edges: int) -> None:
        """Take what the project holds."""
        self.counts = (nodes, edges)

    def execute(self, *_: object) -> None:
        """Take the one statement there is."""

    def fetchone(self) -> tuple[int, int]:
        """Answer the counts."""
        return self.counts


@pytest.mark.parametrize("counts", [(MAX_NODES + 1, 0), (1, viewer.MAX_EDGES + 1)])
def test_a_graph_too_large_is_refused_before_it_is_read(
    counts: tuple[int, int],
) -> None:
    """Counting is cheap; loading every node and summary is not."""
    with pytest.raises(viewer.TooLarge, match="more than"):
        viewer.weigh(Counted(*counts), "alpha")  # type: ignore[arg-type]
    viewer.weigh(Counted(MAX_NODES, viewer.MAX_EDGES), "alpha")  # type: ignore[arg-type]


def test_a_missing_library_is_an_error_and_no_cdn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A page reaching for a CDN would run what it sent, inside the dashboard."""
    monkeypatch.setattr(viewer, "VIS_NETWORK_PATH", "/nonexistent/vis-network.js")
    with pytest.raises(FileNotFoundError):
        viewer.library()


def test_a_field_of_any_size_is_cut_to_what_a_page_shows() -> None:
    """One megabyte of summary is one paragraph on the page."""
    graph, grouped, labels = sample()
    graph.nodes["one"]["summary"] = "s" * 1_000_000
    graph.nodes["one"]["label"] = "l" * 100_000
    graph.nodes["hub"]["source_file"] = "p" * 100_000
    data = drawing(graph, grouped, {0: "c" * 100_000})
    one = next(node for node in data["nodes"] if node["id"] == "one")
    hub = next(node for node in data["nodes"] if node["id"] == "hub")
    assert len(str(one["summary"])) == page.MAX_SUMMARY_CHARS
    assert str(one["summary"]).endswith("...")
    assert len(str(one["label"])) == page.MAX_LABEL_CHARS
    assert len(str(hub["source_file"])) == page.MAX_PATH_CHARS
    assert len(str(data["legend"][0]["label"])) == page.MAX_LABEL_CHARS
    assert len(json.dumps(data)) < 10_000


def test_a_graph_that_grew_while_it_was_read_is_refused_all_the_same(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """What was counted and what was read are held to the same limit."""

    class Connection:
        """A connection that notes how its one transaction is to read."""

        session: dict[str, object] = {}

        def set_session(self, **kwargs: object) -> None:
            self.session.update(kwargs)

        def cursor(self) -> Connection:
            return self

        def __enter__(self) -> Counted:
            return Counted(1, 1)

        def __exit__(self, *exc: object) -> None:
            return None

        def close(self) -> None:
            return None

    grown = nx.empty_graph(MAX_NODES + 1)
    monkeypatch.setattr(viewer, "get_db_connection", Connection)
    monkeypatch.setattr(viewer, "db_to_graph", lambda cursor, project: grown)
    with pytest.raises(viewer.TooLarge, match="more than"):
        viewer.render("alpha")
    assert Connection.session == {
        "isolation_level": "REPEATABLE READ",
        "readonly": True,
    }
