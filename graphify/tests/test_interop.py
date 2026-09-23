"""How graphifyy's stem-based ids are resolved onto ids of our own."""

from __future__ import annotations

from enggraph.interop import link_extraction


def _node(their_id: str, label: str, source_file: str, line: str) -> dict[str, str]:
    return {
        "id": their_id,
        "label": label,
        "source_file": source_file,
        "source_location": line,
    }


def _edge(source: str, target: str, source_file: str) -> dict[str, str]:
    return {
        "source": source,
        "target": target,
        "relation": "calls",
        "source_file": source_file,
    }


NODES = [
    _node("utils", "utils.py", "alpha/utils.py", "L1"),
    _node("utils_load", "load()", "alpha/utils.py", "L3"),
    _node("utils_save", "save()", "alpha/utils.py", "L9"),
    _node("utils", "utils.py", "beta/utils.py", "L1"),
    _node("utils_load", "load()", "beta/utils.py", "L4"),
    _node("utils_save", "save()", "beta/utils.py", "L12"),
    _node("worker", "worker.py", "beta/worker.py", "L1"),
    _node("worker_run", "run()", "beta/worker.py", "L2"),
    _node("store", "store.py", "alpha/store.py", "L1"),
    _node("store_commit", "commit()", "alpha/store.py", "L5"),
]


def _pairs(edges: list[dict[str, str]]) -> list[tuple[str, str, bool]]:
    links, _, _ = link_extraction(NODES, edges)
    return [(source, target, external) for source, target, external, _ in links]


def test_each_file_keeps_its_own_edges() -> None:
    """The second `utils.py` no longer lands on the first one's nodes."""
    pairs = _pairs(
        [
            _edge("utils_load", "utils_save", "alpha/utils.py"),
            _edge("utils_load", "utils_save", "beta/utils.py"),
        ]
    )
    assert pairs == [
        ("alpha/utils.py::load()@L3", "alpha/utils.py::save()@L9", False),
        ("beta/utils.py::load()@L4", "beta/utils.py::save()@L12", False),
    ]


def test_a_cross_file_target_named_once_resolves() -> None:
    """An id only one file declares needs no file to be found."""
    pairs = _pairs([_edge("worker_run", "store_commit", "beta/worker.py")])
    assert pairs == [("beta/worker.py::run()@L2", "alpha/store.py::commit()@L5", False)]


def test_a_cross_file_target_named_twice_is_dropped() -> None:
    """Guessing one of the two `utils_save` would state a relation nobody has."""
    links, reused, dropped = link_extraction(
        NODES, [_edge("worker_run", "utils_save", "beta/worker.py")]
    )
    assert links == []
    assert reused == 3
    assert dropped == 1


def test_a_target_without_a_node_is_external() -> None:
    """Something outside the tree still gets its placeholder."""
    pairs = _pairs([_edge("worker_run", "requests", "beta/worker.py")])
    assert pairs == [("beta/worker.py::run()@L2", "requests", True)]
