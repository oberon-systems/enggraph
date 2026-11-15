"""How the steps of a trace are joined into the chains an agent reads."""

from __future__ import annotations

from typing import Any

import pytest

from enggraph.mcp import trace
from enggraph.mcp.trace import trace_chains, trace_node

CODE = {"project": "alpha", "id": "tools/keeper/src/main.py"}
BUILT = {"project": "alpha", "id": "tools/keeper/"}
KLASS = {"project": "beta", "id": "modules/keeper/manifests/init.pp"}
ROLE = {"project": "beta", "id": "data/role/keeper.yaml"}
NODE = {"project": "beta", "id": "data/node/web-01.example.com.yaml"}
HOST = {"project": "gamma", "id": "live/web/config.yaml"}


def step(
    start: dict[str, str],
    end: dict[str, str],
    way: str,
    relation: str,
    kind: str | None = None,
    name: str | None = None,
) -> dict[str, Any]:
    """One step of a walk."""
    return {
        "from": start,
        "to": end,
        "way": way,
        "relation": relation,
        "kind": kind,
        "name": name,
        "origin": "matched",
    }


STEPS = [
    step(CODE, BUILT, "contains", "provides", "image", "example.com/keeper"),
    step(BUILT, KLASS, "taken_by", "uses_image", "image", "example.com/keeper"),
    step(KLASS, ROLE, "applied_by", "includes_class"),
    step(ROLE, NODE, "applied_by", "selects"),
    step(NODE, HOST, "uses", "deploys_to", "host", "web-01.example.com"),
]


def test_the_steps_join_into_one_chain_from_the_code_to_the_host() -> None:
    """Each arrow says which edge or matched name made the step."""
    assert trace_chains(CODE, STEPS) == [
        "alpha:tools/keeper/src/main.py"
        " -[contains provides image example.com/keeper]-> alpha:tools/keeper/"
        " -[taken_by uses_image image example.com/keeper]->"
        " beta:modules/keeper/manifests/init.pp"
        " -[applied_by includes_class]-> beta:data/role/keeper.yaml"
        " -[applied_by selects]-> beta:data/node/web-01.example.com.yaml"
        " -[uses deploys_to host web-01.example.com]-> gamma:live/web/config.yaml"
    ]


def test_a_chain_ends_at_every_point_the_walk_went_no_further_from() -> None:
    """Two nodes selecting one role are two chains."""
    other = {"project": "beta", "id": "data/node/web-02.example.com.yaml"}
    chains = trace_chains(CODE, [*STEPS, step(ROLE, other, "applied_by", "selects")])
    assert len(chains) == 2
    assert chains[1].endswith("beta:data/node/web-02.example.com.yaml")


def test_nothing_found_is_no_chain() -> None:
    """An empty walk is an empty answer."""
    assert trace_chains(CODE, []) == []


class Graph:
    """Answer the walk's queries from lists, counting them."""

    def __init__(
        self,
        exports: list[tuple[str, str, str, str]],
        links: list[tuple[str, str, str, str, str | None, str | None]],
        edges: list[tuple[str, str, str]],
    ) -> None:
        """Hold the rows the three tables answer with."""
        self.exports = exports
        self.links = links
        self.edges = edges
        self.asked: list[str] = []

    def link(self, row: tuple[Any, ...], far: int) -> dict[str, Any]:
        """Spell one link as the view does, from the end the walk stands on."""
        return {
            "project": row[far],
            "id": row[far + 1],
            "target_id": row[3],
            "relation_type": "uses_image",
            "kind": row[4],
            "name": row[5],
            "origin": "matched",
        }

    def query(self, sql: str, params: list[Any]) -> list[dict[str, Any]]:
        """Answer one statement of trace_node."""
        self.asked.append(sql)
        assert "LIMIT" in sql
        project, limit = params[0], params[-1]
        if "provided_names" in sql:
            rows = [
                {"kind": kind, "name": name, "node_id": node}
                for owner, node, kind, name in self.exports
                if owner == project
                and (node in params[1] or (params[2] and node.startswith(params[2])))
            ]
        elif "FROM edges" in sql:
            rows = [
                {"id": source, "relation_type": "includes"}
                for owner, source, target in self.edges
                if owner == project and target in params[1]
            ]
        elif "target_project = $1" in sql:
            rows = [
                self.link(row, 0)
                for row in self.links
                if row[2] == project and row[3] in params[1] and "declared" not in sql
            ]
        else:
            rows = [
                self.link(row, 2)
                for row in self.links
                if row[0] == project and row[1] in params[1]
            ]
        return rows[:limit]


@pytest.fixture
def graph(monkeypatch: pytest.MonkeyPatch) -> Graph:
    """Return a built directory of alpha that beta takes and applies in turn."""
    held = Graph(
        exports=[
            ("alpha", "tools/keeper/", "image", "example.com/keeper"),
            ("beta", "modules/keeper/", "module", "keeper"),
        ],
        links=[
            (
                "beta",
                KLASS["id"],
                "alpha",
                "tools/keeper/",
                "image",
                "example.com/keeper",
            ),
            ("beta", ROLE["id"], "gamma", "live/web/config.yaml", "host", "web-01"),
            ("delta", "site.pp", "beta", "modules/keeper/", "module", "keeper"),
        ],
        edges=[("beta", ROLE["id"], KLASS["id"])],
    )
    monkeypatch.setattr(trace.db, "query", held.query)
    return held


def test_a_walk_stops_at_a_project_the_caller_may_not_read(graph: Graph) -> None:
    """The far end of a link is named by its project, and nothing past it is read."""
    whole = trace_node(CODE, 60)
    reached = {(one["to"]["project"], one["to"]["id"]) for one in whole["steps"]}
    assert ("beta", ROLE["id"]) in reached
    assert ("delta", "site.pp") in reached
    asked = len(graph.asked)
    held = trace_node(CODE, 60, {"alpha"})
    ends = [one["to"] for one in held["steps"]]
    assert {"project": "beta", "id": ""} in ends
    assert all(end["project"] in ("alpha", "beta") for end in ends)
    assert all(end["id"] == "" for end in ends if end["project"] == "beta")
    said = str(held)
    assert KLASS["id"] not in said and ROLE["id"] not in said
    assert "gamma" not in said and "delta" not in said
    # Only alpha's points were expanded: the start and nothing of beta.
    assert len(graph.asked) - asked == 5


def test_many_takers_in_a_withheld_project_are_one_step(graph: Graph) -> None:
    """How many nodes of it take the name is not told either."""
    graph.links.append(
        ("beta", "other.pp", "alpha", "tools/keeper/", "image", "example.com/keeper")
    )
    held = trace_node(BUILT, 60, {"alpha"})
    assert [one["to"] for one in held["steps"]] == [{"project": "beta", "id": ""}]


def test_a_walk_asks_a_fixed_number_of_queries_however_much_is_provided(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Five thousand names under one directory are one bounded read, not one each."""
    names = [("alpha", f"tools/t{n:04}/", "image", f"n{n}") for n in range(5000)]
    held = Graph(exports=names, links=[], edges=[])
    monkeypatch.setattr(trace.db, "query", held.query)
    answer = trace_node({"project": "alpha", "id": "tools/"}, 1)
    assert answer["truncated"] is True
    assert len(answer["steps"]) == 1
    assert len(held.asked) == 5
