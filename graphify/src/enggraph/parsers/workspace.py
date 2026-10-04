"""Read a layered deployment workspace: nodes, the roles they take, modules.

A workspace keeps its data in `<root>/data/` and its code in
`<root>/modules/<name>/`. A node file `data/nodes/<host>.yaml` names its role,
a role file `data/roles/<name>.yaml` lists the modules it runs, and a module
names the modules it needs in `modules/<name>/requires.yaml`. Every one of them
is plain YAML, so the files are told apart by their path and their shape.
"""

from __future__ import annotations

import re
from typing import Any

NODE_FILE = re.compile(r"^(?P<root>(?:.*/)?)data/nodes/(?P<name>[^/]+)\.ya?ml$")
ROLE_FILE = re.compile(r"^(?P<root>(?:.*/)?)data/roles/(?P<name>[^/]+)\.ya?ml$")
REQUIRES_FILE = re.compile(
    r"^(?P<root>(?:.*/)?)modules/(?P<name>[^/]+)/requires\.ya?ml$"
)
MODULE_DIR = re.compile(r"^(?P<root>(?:.*/)?)modules/(?P<name>[^/]+)/")
# The data a workspace keeps: the shared tree, and each module's own defaults.
WORKSPACE_DATA = re.compile(r"^(?P<root>(?:.*?/)?)(?:data|modules/[^/]+/data)/")

HAS_ROLE = "has_role"
INCLUDES_MODULE = "includes_module"
REQUIRES_MODULE = "requires_module"
# The placeholder a name takes when the workspace holding the reference does
# not hold the role or the module itself.
WORKSPACE_PLACEHOLDERS = {
    HAS_ROLE: "deploy-role:",
    INCLUDES_MODULE: "deploy-module:",
    REQUIRES_MODULE: "deploy-module:",
}
ROLE_FILES = ("{root}data/roles/{name}.yaml", "{root}data/roles/{name}.yml")
MODULE_ENTRY_POINTS = (
    "{root}modules/{name}/requires.yaml",
    "{root}modules/{name}/requires.yml",
    "{root}modules/{name}/code/main.py",
    "{root}modules/{name}/README.md",
)


def workspace_root(rel_path: str) -> str | None:
    """Return the workspace root of a node, role or requires file, or None."""
    for pattern in (NODE_FILE, ROLE_FILE, REQUIRES_FILE):
        match = pattern.match(rel_path)
        if match:
            return match.group("root")
    return None


def _names(value: Any) -> list[str]:  # noqa: ANN401
    if not isinstance(value, list):
        return []
    return [entry.strip() for entry in value if isinstance(entry, str) and entry]


def workspace_relations(
    rel_path: str, documents: list[Any]
) -> list[dict[str, str]] | None:
    """Return the relations of a workspace file, or None for any other file."""
    document = documents[0] if documents else None
    if not isinstance(document, dict):
        return None
    found: list[tuple[str, str]] = []
    if NODE_FILE.match(rel_path) or ROLE_FILE.match(rel_path):
        role = document.get("role")
        modules = _names(document.get("modules"))
        if isinstance(role, str) and role.strip():
            found.append((role.strip(), HAS_ROLE))
        elif not modules:
            return None
        found.extend((name, INCLUDES_MODULE) for name in modules)
    elif REQUIRES_FILE.match(rel_path):
        if "requires" not in document:
            return None
        found.extend((name, REQUIRES_MODULE) for name in _names(document["requires"]))
    else:
        return None
    return [
        {"target": target, "type": relation_type, "scope": "file"}
        for target, relation_type in dict.fromkeys(found)
    ]


def workspace_candidates(relation_type: str, target: str, rel_path: str) -> list[str]:
    """Return the files a role or module name resolves to inside the workspace."""
    root = workspace_root(rel_path)
    if root is None:
        return []
    patterns = ROLE_FILES if relation_type == HAS_ROLE else MODULE_ENTRY_POINTS
    return [pattern.format(root=root, name=target) for pattern in patterns]
