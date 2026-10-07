"""Read what a Terraform, OpenTofu or Terragrunt file refers to.

Only what is static resolves: a quoted path, optionally rooted at
`${path.module}`, `${get_terragrunt_dir()}` or `${get_repo_root()}`. A path
built from anything else is skipped rather than guessed.
"""

from __future__ import annotations

import posixpath

from tree_sitter import Node

from enggraph.indexer.parsers.base import node_text

USES_MODULE = "uses_module"
READS_FILE = "reads_file"
READS_VARS = "reads_vars"
INCLUDES = "includes"
DEPENDS_ON = "depends_on"
# A module source that is no local path names a module somewhere else, and is
# matched to the project providing it.
MODULE_PREFIX = "tfmodule:"
# A file found by walking up from the file naming it, as find_in_parent_folders
# does; the resolver does the walk.
PARENT_PREFIX = "parents:"
LOCAL_SOURCE_PREFIXES = ("./", "../")
TERRAGRUNT_CONFIG = "terragrunt.hcl"
FILE_FUNCTIONS = frozenset({"file", "templatefile", "filebase64", "filesha256"})
CONFIG_FUNCTIONS = frozenset({"read_terragrunt_config"})
PARENT_FUNCTION = "find_in_parent_folders"
# Interpolations that stand for a directory, by what they stand for.
MODULE_DIR_CALLS = frozenset({"get_terragrunt_dir"})
ROOT_DIR_CALLS = frozenset({"get_repo_root"})
# Marks a path rooted at the tree rather than at the file naming it.
ROOTED = "\0"


def _unwrap(node: Node) -> Node:
    while node.type == "expression" and node.named_child_count == 1:
        node = node.named_children[0]
    return node


def _call_name(node: Node) -> str:
    if node.type != "function_call":
        return ""
    head = node.named_children[0] if node.named_child_count else None
    return node_text(head) if head and head.type == "identifier" else ""


def _call_arguments(node: Node) -> list[Node]:
    for child in node.named_children:
        if child.type == "function_arguments":
            return [_unwrap(argument) for argument in child.named_children]
    return []


def _interpolated(node: Node, base_dir: str) -> str | None:
    """Return the directory an interpolation stands for, or None."""
    inner = next(
        (_unwrap(child) for child in node.named_children if child.type == "expression"),
        None,
    )
    if inner is None:
        return None
    name = _call_name(inner)
    if name in MODULE_DIR_CALLS or "".join(node_text(inner).split()) == "path.module":
        return f"{ROOTED}{base_dir}"
    if name in ROOT_DIR_CALLS:
        return ROOTED
    return None


def static_string(node: Node, base_dir: str) -> str | None:
    """Return the text of a string expression, or None when it is not static."""
    node = _unwrap(node)
    if node.type == "literal_value":
        node = node.named_children[0] if node.named_child_count else node
    if node.type == "template_expr":
        node = node.named_children[0] if node.named_child_count else node
    if node.type not in ("string_lit", "quoted_template"):
        return None
    parts: list[str] = []
    for child in node.named_children:
        if child.type == "template_literal":
            parts.append(node_text(child))
        elif child.type == "template_interpolation":
            directory = _interpolated(child, base_dir)
            if directory is None:
                return None
            parts.append(directory)
        elif child.type not in ("quoted_template_start", "quoted_template_end"):
            return None
    return "".join(parts)


def tree_path(raw: str, base_dir: str) -> str | None:
    """Return a path relative to the tree, or None when it climbs out of it."""
    if raw.startswith(ROOTED):
        joined = raw[len(ROOTED) :].lstrip("/")
    else:
        joined = posixpath.join(base_dir, raw)
    # Terragrunt marks the subdirectory of a source with a double slash.
    joined = posixpath.normpath(joined.replace("//", "/")) if joined else "."
    if joined.startswith(".."):
        return None
    return "" if joined == "." else joined


def _path_target(argument: Node, base_dir: str) -> str | None:
    """Return the target of a path argument: a tree path or a parent lookup."""
    if _call_name(argument) == PARENT_FUNCTION:
        names = _call_arguments(argument)
        name = static_string(names[0], base_dir) if names else TERRAGRUNT_CONFIG
        return f"{PARENT_PREFIX}{name}" if name else None
    raw = static_string(argument, base_dir)
    return None if raw is None else tree_path(raw, base_dir)


def _source_target(raw: str, base_dir: str) -> str | None:
    if raw.startswith(ROOTED) or raw.startswith(LOCAL_SOURCE_PREFIXES):
        return tree_path(raw.split("?", 1)[0], base_dir)
    return f"{MODULE_PREFIX}{raw}" if raw else None


def _attributes(block: Node) -> dict[str, Node]:
    body = next((child for child in block.named_children if child.type == "body"), None)
    attributes: dict[str, Node] = {}
    for child in body.named_children if body is not None else []:
        if child.type != "attribute" or child.named_child_count < 2:
            continue
        key, value = child.named_children[0], child.named_children[1]
        attributes[node_text(key)] = value
    return attributes


def _block_relations(block: Node, base_dir: str) -> list[tuple[str, str]]:
    head = block.named_children[0] if block.named_child_count else None
    kind = node_text(head) if head else ""
    attributes = _attributes(block)
    found: list[tuple[str, str]] = []
    if kind in ("module", "terraform") and "source" in attributes:
        raw = static_string(attributes["source"], base_dir)
        target = None if raw is None else _source_target(raw, base_dir)
        if target is not None:
            found.append((target, USES_MODULE))
    elif kind == "include" and "path" in attributes:
        target = _path_target(_unwrap(attributes["path"]), base_dir)
        if target is not None:
            found.append((target, INCLUDES))
    elif kind == "dependency" and "config_path" in attributes:
        target = _path_target(_unwrap(attributes["config_path"]), base_dir)
        if target is not None:
            found.append((target, DEPENDS_ON))
    return found


def _call_relations(call: Node, base_dir: str) -> list[tuple[str, str]]:
    name = _call_name(call)
    if name in FILE_FUNCTIONS:
        relation = READS_FILE
    elif name in CONFIG_FUNCTIONS:
        relation = READS_VARS
    else:
        return []
    arguments = _call_arguments(call)
    target = _path_target(arguments[0], base_dir) if arguments else None
    return [(target, relation)] if target else []


def _walk(node: Node) -> list[Node]:
    found: list[Node] = []
    stack = [node]
    while stack:
        current = stack.pop()
        if current.type in ("block", "function_call"):
            found.append(current)
        stack.extend(current.named_children)
    return found


def terraform_relations(root: Node, rel_path: str) -> list[dict[str, str]]:
    """Return the modules, files and configs a file refers to."""
    base_dir = posixpath.dirname(rel_path)
    found: list[tuple[str, str]] = []
    for node in _walk(root):
        if node.type == "block":
            found.extend(_block_relations(node, base_dir))
        else:
            found.extend(_call_relations(node, base_dir))
    return [
        {"target": target, "type": relation_type, "scope": "file"}
        for target, relation_type in dict.fromkeys(found)
        if target != rel_path
    ]
