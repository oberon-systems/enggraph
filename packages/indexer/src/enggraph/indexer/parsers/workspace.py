"""Read what a module of a deployment workspace requires.

A module names the modules it needs in `modules/<name>/requires.yaml`. The
data of the workspace - its nodes, the files they select and the modules
those run - is laid out by the hierarchy the data declares, so it is followed
across the whole tree in `enggraph.indexer.workspacedata` rather than file by file.
"""

from __future__ import annotations

import re
from typing import Any

REQUIRES_FILE = re.compile(
    r"^(?P<root>(?:.*/)?)modules/(?P<name>[^/]+)/requires\.ya?ml$"
)

REQUIRES_MODULE = "requires_module"
# The placeholder a name takes when the workspace does not hold the module.
WORKSPACE_PLACEHOLDERS = {REQUIRES_MODULE: "deploy-module:"}
MODULE_ENTRY_POINTS = (
    "{root}modules/{name}/requires.yaml",
    "{root}modules/{name}/requires.yml",
    "{root}modules/{name}/code/main.py",
    "{root}modules/{name}/README.md",
)


def workspace_root(rel_path: str) -> str | None:
    """Return the workspace root of a requires file, or None."""
    match = REQUIRES_FILE.match(rel_path)
    return match.group("root") if match else None


def _names(value: Any) -> list[str]:  # noqa: ANN401
    if not isinstance(value, list):
        return []
    return [entry.strip() for entry in value if isinstance(entry, str) and entry]


def workspace_relations(
    rel_path: str, documents: list[Any]
) -> list[dict[str, str]] | None:
    """Return the relations of a requires file, or None for any other file."""
    document = documents[0] if documents else None
    if not REQUIRES_FILE.match(rel_path) or not isinstance(document, dict):
        return None
    if "requires" not in document:
        return None
    return [
        {"target": target, "type": REQUIRES_MODULE, "scope": "file"}
        for target in dict.fromkeys(_names(document["requires"]))
    ]


def workspace_candidates(relation_type: str, target: str, rel_path: str) -> list[str]:
    """Return the files a required module resolves to inside the workspace."""
    root = workspace_root(rel_path)
    if root is None or relation_type != REQUIRES_MODULE:
        return []
    return [pattern.format(root=root, name=target) for pattern in MODULE_ENTRY_POINTS]
