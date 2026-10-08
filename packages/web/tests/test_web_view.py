"""What the pages say about a value."""

from __future__ import annotations

from enggraph.web import queues, view
from enggraph.web.routes.records import around_node
from enggraph.web.routes.skills import frontmatter_name


def test_an_age_is_said_in_its_coarsest_unit() -> None:
    """Hold that an age is said in its coarsest unit."""
    assert view.age(5) == "just now"
    assert view.age(120) == "2 min ago"
    assert view.age(3600) == "1 hour ago"
    assert view.age(3 * 86400) == "3 days ago"


def test_a_share_is_never_whole_while_one_is_missing() -> None:
    """Hold that a share is never whole while one is missing."""
    assert view.percent_of(0, 0) is None
    assert view.percent_of(999, 1000) == 99
    assert view.percent_of(1000, 1000) == 100


def test_the_tone_of_a_queue() -> None:
    """Hold that the tone of a queue."""
    assert view.coverage_tone(4, 4, 0, 0) == "coverage-done"
    assert view.coverage_tone(1, 4, 2, 1) == "coverage-busy"
    assert view.coverage_tone(1, 4, 0, 1) == "coverage-failed"
    assert view.coverage_tone(1, 4, 0, 0) is None


def test_an_address_leaves_out_what_says_nothing() -> None:
    """Hold that an address leaves out what says nothing."""
    assert view.address("/plans", project=None, q="") == "/plans"
    assert view.address("/plans", q="a b", offset=50) == "/plans?q=a%20b&offset=50"
    assert view.go("/plans", "status", q="a", status="x") == "/plans?q=a&status={value}"
    assert view.go("/", "project") == "/?project={value}"


def test_entries_list_organizations_then_the_projects_in_none() -> None:
    """Hold that entries list organizations then the projects in none."""
    targets = [
        {"name": "alpha", "type": "codebase", "organizations": ["omega"]},
        {"name": "beta", "type": "docs", "organizations": []},
        {"name": "omega", "type": "organization", "organizations": []},
    ]
    entries = view.project_entries(targets, ["gone", "beta"])
    assert [(one["value"], one["group"]) for one in entries] == [
        ("omega", "omega"),
        ("alpha", "omega"),
        ("beta", "Projects"),
        ("gone", "No longer a project"),
    ]


def test_a_lamp_is_as_bad_as_its_worst_server() -> None:
    """Hold that a lamp is as bad as its worst server."""

    def row(project: str, state: str) -> dict[str, object]:
        server = {"url": f"http://{state}", "state": state, "reason": "refused"}
        return {"project": project, "enabled": True, "server": server}

    assert view.lamp([], True) is None
    lit = view.lamp([row("alpha", "ok"), row("beta", "down")], True)
    assert lit is not None
    assert lit["state"] == "down"
    assert lit["title"].splitlines()[0].startswith("http://down does not answer")


def test_markdown_is_rendered_and_cleaned() -> None:
    """Hold that markdown is rendered and cleaned."""
    drawn = str(view.markdown("# Title\n\n<script>alert(1)</script>"))
    assert "<h1>Title</h1>" in drawn
    assert "<script>" not in drawn


def test_a_node_is_known_by_every_directory_above_it() -> None:
    """Hold that a node is known by every directory above it."""
    assert around_node("src/a/b.py::f") == ["src/a/b.py::f", "src/a/", "src/", "./"]
    assert around_node("./") == ["./"]


def test_a_skill_is_called_what_its_frontmatter_says() -> None:
    """Hold that a skill is called what its frontmatter says."""
    assert frontmatter_name("---\nname: alpha\n---\ntext") == "alpha"
    assert frontmatter_name("name: alpha") is None


def test_the_queues_fold_over_every_project() -> None:
    """Hold that the queues fold over every project."""
    summaries = {
        "loop": False,
        "stats_at": None,
        "summaries": [
            {
                "project": "alpha",
                "enabled": True,
                "gated": False,
                "files": 10,
                "manual": 2,
                "described": 4,
                "queue": {"pending": 3},
                "skipped": 1,
            }
        ],
    }
    embeddings = {"model": "m", "chunk_chars": 1500, "embeddings": []}
    folded = queues.fold(summaries, embeddings)
    first = folded["tiles"][0]
    assert (first["done"], first["total"], first["waiting"]) == (4, 8, 3)
    assert first["percent"] == "50%"
    assert first["note"] == "the push loop is off in this process"
    assert folded["rows"][0]["embedded"]["enabled"] is False
    assert folded["counted"] is None
