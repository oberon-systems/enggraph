"""Reading a symbol, the patterns it is searched by, and sorting what it reaches."""

from __future__ import annotations

from typing import Any

from enggraph.mcp.symbols import (
    bare_name,
    bucket_impact,
    call_pattern,
    cross_project_hits,
    implementation_pattern,
    import_pattern,
    link_targets,
    merge_hits,
    parse_symbol,
    word_pattern,
)


def hit(node_id: str, **fields: Any) -> dict[str, Any]:  # noqa: ANN401
    """One graph hit."""
    return {
        "project": "alpha",
        "id": node_id,
        "name": node_id,
        "type": "function",
        "file_path": None,
        "line": None,
        "relation": "calls",
        "evidence": "graph",
        "confidence": "INFERRED",
        "hop": 1,
        **fields,
    }


def text(file: str, lines: list[int], hop: int = 1) -> dict[str, Any]:
    """One text hit: a file and the lines that matched in it."""
    return hit(
        file,
        type="file",
        file_path=file,
        line=lines[0] if lines else None,
        lines=lines,
        relation="mentions",
        evidence="text",
        confidence="NAME_MATCH",
        hop=hop,
    )


def test_an_owner_is_split_from_a_member() -> None:
    """`Alpha.beta` is beta, of Alpha."""
    parsed = parse_symbol("Alpha.beta")
    assert parsed == {"owner": "Alpha", "name": "beta", "path": None}


def test_only_the_nearest_owner_of_a_chain_is_kept() -> None:
    """Whatever separates the parts."""
    assert parse_symbol("pkg.Alpha.beta")["owner"] == "Alpha"
    assert parse_symbol("Alpha::beta")["owner"] == "Alpha"
    assert parse_symbol("Alpha#beta")["owner"] == "Alpha"


def test_the_extractor_s_decoration_is_dropped() -> None:
    """A leading dot and the trailing parentheses are not part of a name."""
    assert parse_symbol("beta()") == {"owner": None, "name": "beta", "path": None}
    assert parse_symbol(".beta()")["name"] == "beta"
    assert bare_name(".beta()") == "beta"


def test_a_path_is_a_file_with_its_stem_as_the_name() -> None:
    """And loses a leading `./`."""
    assert parse_symbol("src/alpha/beta.ts") == {
        "owner": None,
        "name": "beta",
        "path": "src/alpha/beta.ts",
    }
    assert parse_symbol("./beta.py")["path"] == "beta.py"


def test_a_word_pattern_matches_whole_words_only() -> None:
    """One pattern, spelled for the database and for the reader of the trees."""
    pattern = word_pattern(["beta"])
    assert pattern["py"].search("return beta + 1")
    assert not pattern["py"].search("return alphabeta")
    assert pattern["pg"] == "(?n)\\mbeta\\M"


def test_several_names_join_into_one_alternation() -> None:
    """A repeated name is written once."""
    assert word_pattern(["alpha", "beta", "alpha"])["grep"] == "\\b(alpha|beta)\\b"


def test_a_call_wants_a_call_shape() -> None:
    """A name followed by an opening parenthesis."""
    pattern = call_pattern(["beta"])["py"]
    assert pattern.search("this.alpha.beta (1)")
    assert not pattern.search("const beta = 1")


def test_what_a_regex_would_read_is_escaped() -> None:
    """A dollar in a name is a dollar."""
    assert word_pattern(["a$b"])["py"].search("x a$b y")


def test_an_implementation_is_found_in_either_language() -> None:
    """`extends`, `implements`, and a base class in parentheses."""
    pattern = implementation_pattern("Beta")["py"]
    assert pattern.search("class Alpha extends Beta {")
    assert pattern.search("class Alpha implements Gamma, Beta {")
    assert pattern.search("class Alpha(Base, Beta):")
    assert not pattern.search("const x: Beta = y")


def test_an_import_of_a_stem_is_found_on_one_line() -> None:
    """Either spelling of an import."""
    pattern = import_pattern("beta")["py"]
    assert pattern.search('import { Beta } from "./beta";')
    assert pattern.search("from alpha.beta import Beta")
    assert not pattern.search("const beta = 1")


def test_one_row_is_kept_per_graph_node() -> None:
    """The nearest sighting of a node is the one that counts."""
    assert len(merge_hits([hit("a"), hit("a", hop=2)], [])) == 1


def test_text_lines_a_graph_hit_stands_on_are_dropped() -> None:
    """The edge already says what the line would."""
    merged = merge_hits(
        [hit("x.ts::f()@L4", file_path="x.ts", line=4)], [text("x.ts", [4, 9])]
    )
    assert merged[1]["lines"] == [9]
    assert merged[1]["line"] == 9


def test_a_text_row_left_without_lines_is_dropped() -> None:
    """Nothing is left for it to say."""
    merged = merge_hits(
        [hit("x.ts::f()@L4", file_path="x.ts", line=4)], [text("x.ts", [4])]
    )
    assert len(merged) == 1


def test_two_text_rows_of_one_file_fold_into_one() -> None:
    """With their lines merged and sorted."""
    merged = merge_hits([], [text("y.ts", [7, 3]), text("y.ts", [3, 1])])
    assert len(merged) == 1
    assert merged[0]["lines"] == [1, 3, 7]


HITS = [
    hit("src/alpha/service.ts", file_path="src/alpha/service.ts"),
    hit("src/routes.ts", file_path="src/routes.ts", hop=2),
    hit("test/alpha.test.ts", file_path="test/alpha.test.ts"),
    hit(
        "deploy/compose.yml",
        file_path="deploy/compose.yml",
        relation="uses_file",
        hop=3,
    ),
]


def test_direct_is_split_from_indirect_by_hop() -> None:
    """One hop away is direct."""
    impact = bucket_impact(HITS)
    assert impact["counts"]["direct"] == 2
    assert impact["counts"]["indirect"] == 2


def test_tests_the_public_api_and_configuration_are_filed_apart() -> None:
    """Each by its path or by the relation that reached it."""
    impact = bucket_impact(HITS)
    assert [one["id"] for one in impact["tests"]] == ["test/alpha.test.ts"]
    assert [one["id"] for one in impact["public_api"]] == ["src/routes.ts"]
    assert [one["id"] for one in impact["configuration"]] == ["deploy/compose.yml"]
    assert impact["counts"]["cross_project"] == 0


def test_what_other_projects_take_goes_into_cross_project() -> None:
    """A link is evidence of its own kind."""
    linked = cross_project_hits(
        [
            {
                "source_project": "beta",
                "source_id": "deploy/compose.yml::service.job",
                "target_project": "alpha",
                "target_id": "worker/",
                "relation_type": "uses_image",
                "kind": "image",
                "name": "alpha-worker",
                "origin": "matched",
                "note": None,
                "node_name": "service.job",
                "node_type": "service",
                "file_path": "deploy/compose.yml",
            }
        ]
    )
    crossed = bucket_impact(HITS, linked)
    assert crossed["counts"]["cross_project"] == 1
    assert crossed["cross_project"][0]["evidence"] == "link"
    assert crossed["cross_project"][0]["link"]["to"] == "alpha"


def test_the_files_are_listed_nearest_first() -> None:
    """The farthest hop comes last."""
    impact = bucket_impact(HITS)
    assert impact["files"][-1] == "deploy/compose.yml"
    assert impact["counts"]["files"] == 4


def test_link_targets_name_the_node_its_file_and_every_directory_once() -> None:
    """A symbol and its file share the directories above them."""
    targets = link_targets(
        [
            {
                "project": "alpha",
                "id": "worker/main.py::run@L3",
                "file_path": "worker/main.py",
            },
            {"project": "alpha", "id": "worker/main.py", "file_path": "worker/main.py"},
        ]
    )
    assert [one["id"] for one in targets] == [
        "worker/main.py::run@L3",
        "worker/main.py",
        "worker/",
        "./",
    ]
