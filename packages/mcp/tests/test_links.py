"""The name two projects are matched on, and the walk between projects."""

from __future__ import annotations

from typing import Any

import pytest

from enggraph.mcp.links import ancestor_ids, edges_along_walk, name_key, normalize_name


@pytest.mark.parametrize(
    ("kind", "raw", "expected"),
    [
        ("image", "alpha-worker:latest", "alpha-worker"),
        ("image", "example.com:5000/alpha/worker:1.2", "example.com:5000/alpha/worker"),
        ("image", "docker.io/library/postgres:16@sha256:abc", "postgres"),
        ("image", "$WORKER_IMAGE", ""),
        ("pypi", "Alpha_Worker>=1.0", "alpha-worker"),
        ("pypi", "beta.client[extra] ; python_version > '3'", "beta-client"),
        ("role", "../roles/alpha", "alpha"),
        ("npm", "@alpha/api", "@alpha/api"),
        ("go", "  ", ""),
        ("host", "Web-01.Example.com.", "web-01.example.com"),
        (
            "tfmodule",
            "git::https://example.com/alpha/infra.git//modules/vpc?ref=v1",
            "example.com/alpha/infra//modules/vpc",
        ),
        ("tfmodule", "git@example.com:alpha/infra.git", "example.com/alpha/infra"),
        ("tfmodule", "alpha/network/openstack", "alpha/network/openstack"),
        ("bucket", "Repo", "repo"),
        ("package", "%{name}-devel", ""),
    ],
)
def test_a_name_is_normalized_by_its_kind(kind: str, raw: str, expected: str) -> None:
    """The same rule the indexer applies, or the two sides never meet."""
    assert normalize_name(kind, raw) == expected


def test_case_and_separators_do_not_tell_two_spellings_apart() -> None:
    """And a name of separators alone is no name."""
    assert name_key("Alpha_Web_01_example_com") == name_key("alpha-web-01.example.com")
    assert name_key(" -._/ ") == ""


def test_a_symbol_climbs_to_the_root_nearest_first() -> None:
    """Its file is not a directory above it."""
    assert ancestor_ids("src/auth/jwt.ts::sign@L4") == ["src/auth/", "src/", "./"]


def test_a_directory_climbs_and_stops_at_the_root() -> None:
    """The root has nothing above it."""
    assert ancestor_ids("roles/alpha/") == ["roles/", "./"]
    assert ancestor_ids("./") == []
    assert ancestor_ids("README.md") == ["./"]


def edge(start: str, end: str) -> dict[str, Any]:
    """One rolled-up link between two projects."""
    return {
        "from": start,
        "to": end,
        "relation": "uses_image",
        "kind": "image",
        "origin": "matched",
        "count": 1,
        "samples": [],
    }


EDGES = [
    edge("alpha", "beta"),
    edge("beta", "gamma"),
    edge("beta", "delta"),
    edge("gamma", "delta"),
]


def pairs(edges: list[dict[str, Any]]) -> list[str]:
    """Spell edges as `from>to`."""
    return [f"{one['from']}>{one['to']}" for one in edges]


def test_the_edges_an_outgoing_walk_took_are_kept() -> None:
    """An edge between two projects equally far is not on the way."""
    hops = {"alpha": 0, "beta": 1, "gamma": 2, "delta": 2}
    assert pairs(edges_along_walk(EDGES, hops, "outgoing")) == [
        "alpha>beta",
        "beta>gamma",
        "beta>delta",
    ]


def test_an_incoming_walk_is_read_the_other_way_round() -> None:
    """The same edges, climbed from their targets."""
    hops = {"delta": 0, "beta": 1, "gamma": 1, "alpha": 2}
    assert pairs(edges_along_walk(EDGES, hops, "incoming")) == [
        "alpha>beta",
        "beta>delta",
        "gamma>delta",
    ]


def test_an_edge_between_two_members_of_an_organization_is_kept() -> None:
    """Both are starts, and their link is inside what was asked about."""
    hops = {"alpha": 0, "beta": 0, "gamma": 1}
    assert pairs(edges_along_walk(EDGES, hops, "outgoing")) == [
        "alpha>beta",
        "beta>gamma",
    ]


def test_an_edge_to_a_project_the_walk_never_reached_is_dropped() -> None:
    """It would name a project the answer does not list."""
    assert len(edges_along_walk(EDGES, {"alpha": 0, "beta": 1}, "both")) == 1
