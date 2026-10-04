"""What a layered deployment workspace contributes to the graph."""

from __future__ import annotations

from enggraph.parsers.ansible import AnsibleParser
from enggraph.parsers.workspace import (
    workspace_candidates,
    workspace_relations,
    workspace_root,
)
from enggraph.resolution import has_placeholder, placeholder_id, resolve_file_target

NODE = """---
role: web
modules:
  - backup
docker:
  stacks: {}
"""
ROLE = """---
modules:
  - base
  - nginx
"""
REQUIRES = """---
requires:
  - repos
  - nftables
"""


def _relations(rel_path: str, content: str) -> list[tuple[str, str]]:
    return [
        (one["target"], one["type"])
        for one in AnsibleParser().get_relations(content, rel_path)
    ]


def test_a_node_takes_its_role_and_its_own_modules() -> None:
    """A node file names its role and may add modules of its own."""
    assert _relations("deploy/data/nodes/web-01.example.com.yaml", NODE) == [
        ("web", "has_role"),
        ("backup", "includes_module"),
    ]


def test_a_role_lists_its_modules_and_a_module_its_requirements() -> None:
    """A role runs modules, and a module needs other modules."""
    assert _relations("data/roles/web.yaml", ROLE) == [
        ("base", "includes_module"),
        ("nginx", "includes_module"),
    ]
    assert _relations("modules/docker/requires.yaml", REQUIRES) == [
        ("repos", "requires_module"),
        ("nftables", "requires_module"),
    ]


def test_other_yaml_is_left_to_the_parsers_before() -> None:
    """A file with the path but not the shape is not a workspace file."""
    assert workspace_relations("data/roles/web.yaml", [{"name": "web"}]) is None
    assert workspace_relations("data/nodes/a.yaml", [["role"]]) is None
    assert workspace_relations("config/web.yaml", [{"role": "web"}]) is None
    assert workspace_root("modules/docker/data/defaults.yaml") is None


def test_names_resolve_inside_the_workspace_or_become_placeholders() -> None:
    """A name the workspace holds is a file; any other is a placeholder."""
    known = {"deploy/data/roles/web.yaml", "deploy/modules/base/requires.yaml"}
    node = "deploy/data/nodes/web-01.example.com.yaml"
    assert workspace_candidates("has_role", "web", node)[0] == (
        "deploy/data/roles/web.yaml"
    )
    assert resolve_file_target("has_role", "web", node, known) == (
        "deploy/data/roles/web.yaml"
    )
    assert resolve_file_target("includes_module", "base", node, known) == (
        "deploy/modules/base/requires.yaml"
    )
    assert resolve_file_target("includes_module", "nginx", node, known) is None
    assert has_placeholder("nginx", node)
    assert placeholder_id("includes_module", "nginx") == "deploy-module:nginx"
    assert placeholder_id("has_role", "db") == "deploy-role:db"
