"""How the steps of a trace are joined into the chains an agent reads."""

from __future__ import annotations

from typing import Any

from enggraph.mcp.trace import trace_chains

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
