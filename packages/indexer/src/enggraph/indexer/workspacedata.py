"""Follow a deployment workspace the way hiera is followed: from the module.

The layout is what the data declares. A `hierarchy:` list of path patterns,
`nodes/{node}.yaml` and the like, names the layers below the directory holding
it, and the modules live beside that directory in `modules/<name>/`. A key
named like a layer variable selects the file it fills, a top-level key naming
a module configures it, and `modules:` lists the modules a file runs. What a
module installs is read from its code, its config resolved through the data.
"""

from __future__ import annotations

import posixpath
import re
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

import yaml

from enggraph.core.identifiers import is_fqdn
from enggraph.indexer.parsers.puppetuses import MAX_VALUES
from enggraph.indexer.parsers.pyinfrauses import pyinfra_uses
from enggraph.indexer.parsers.yamldocs import TolerantYamlLoader
from enggraph.indexer.puppetdata import DataEdge

HIERARCHY_KEY = "hierarchy"
HIERARCHY_LINE = re.compile(r"^hierarchy\s*:", re.M)
LAYER_VARIABLE = re.compile(r"\{(\w+)\}")
DATA_EXTENSIONS = (".yaml", ".yml")
CODE_EXTENSION = ".py"
MODULES_DIR = "modules"
MODULES_KEY = "modules"
MODULE_DATA_DIR = "data"
MODULE_ENTRY_POINTS = ("requires.yaml", "requires.yml", "code/main.py", "README.md")
TEST_PARTS = frozenset({"tests", "test"})
SELECTS = "selects"
CONFIGURES = "configures"
INCLUDES_MODULE = "includes_module"
DEPLOYS_TO = "deploys_to"
EDGE_SOURCE = "workspacedata"


class _DataLoader(TolerantYamlLoader):
    """Keeps the value under a merge tag (`!replace {...}`), drops a secret."""


def _tagged(loader: yaml.SafeLoader, suffix: str, node: yaml.Node) -> Any:  # noqa: ANN401
    if isinstance(node, yaml.MappingNode):
        return loader.construct_mapping(node, deep=True)
    if isinstance(node, yaml.SequenceNode):
        return loader.construct_sequence(node, deep=True)
    return None


_DataLoader.add_multi_constructor("", _tagged)


def load_data(content: str) -> dict[Any, Any]:
    """Return the first document of a data file when it is a mapping."""
    try:
        document = next(yaml.load_all(content, Loader=_DataLoader), None)
    except (yaml.YAMLError, RecursionError, TypeError):
        return {}
    return document if isinstance(document, dict) else {}


@dataclass(frozen=True)
class Workspace:
    """One workspace: where its data and modules live and its layer patterns."""

    root: str
    data_dir: str
    layers: tuple[re.Pattern[str], ...]
    variables: frozenset[str]


def _layer(pattern: str) -> re.Pattern[str]:
    parts: list[str] = []
    seen: set[str] = set()
    for index, piece in enumerate(LAYER_VARIABLE.split(pattern)):
        if index % 2 == 0:
            parts.append(re.escape(piece))
        elif piece in seen:
            parts.append(f"(?P={piece})")
        else:
            seen.add(piece)
            parts.append(f"(?P<{piece}>[^/]+)")
    return re.compile("".join(parts))


def workspaces(contents: dict[str, str]) -> list[Workspace]:
    """Return every workspace whose data declares a hierarchy of layers."""
    found: list[Workspace] = []
    for path in sorted(contents):
        if not path.endswith(DATA_EXTENSIONS) or not HIERARCHY_LINE.search(
            contents[path]
        ):
            continue
        listed = load_data(contents[path]).get(HIERARCHY_KEY)
        if not isinstance(listed, list):
            continue
        patterns = [
            one for one in listed if isinstance(one, str) and LAYER_VARIABLE.search(one)
        ]
        if not patterns:
            continue
        data_dir = posixpath.dirname(path)
        parent = posixpath.dirname(data_dir)
        found.append(
            Workspace(
                root=f"{parent}/" if parent else "",
                data_dir=f"{data_dir}/" if data_dir else "",
                layers=tuple(_layer(one) for one in patterns),
                variables=frozenset(
                    name for one in patterns for name in LAYER_VARIABLE.findall(one)
                ),
            )
        )
    return found


def _module_of(space: Workspace, rel_path: str) -> str | None:
    prefix = f"{space.root}{MODULES_DIR}/"
    if not rel_path.startswith(prefix):
        return None
    name, separator, _ = rel_path[len(prefix) :].partition("/")
    return name if separator and name else None


def _is_test(rel_path: str) -> bool:
    parts = rel_path.split("/")
    return bool(TEST_PARTS & set(parts[:-1])) or parts[-1].startswith("test_")


def workspace_files(space: Workspace, rel_paths: list[str]) -> list[str]:
    """Return the data and the module code of a workspace."""
    found: list[str] = []
    prefix = f"{space.root}{MODULES_DIR}/"
    for rel_path in rel_paths:
        module = _module_of(space, rel_path)
        if module is None:
            if rel_path.endswith(DATA_EXTENSIONS) and rel_path.startswith(
                space.data_dir
            ):
                found.append(rel_path)
            continue
        inside = rel_path[len(prefix) + len(module) + 1 :]
        if rel_path.endswith(DATA_EXTENSIONS) and inside.startswith(
            f"{MODULE_DATA_DIR}/"
        ):
            found.append(rel_path)
        elif rel_path.endswith(CODE_EXTENSION) and not _is_test(rel_path):
            found.append(rel_path)
    return sorted(found)


def _scalars(value: Any) -> list[str]:  # noqa: ANN401
    if isinstance(value, dict):
        return [str(key) for key in value]
    if isinstance(value, list):
        return [
            one
            for entry in value
            for one in _scalars(entry)
            if not isinstance(entry, list | dict)
        ]
    if isinstance(value, bool) or value is None:
        return []
    return [str(value)] if isinstance(value, str | int | float) else []


@dataclass
class Followed:
    """What one workspace yields: data edges, names it provides and takes."""

    edges: list[DataEdge]
    provided: list[tuple[str, str, str]]
    taken: list[tuple[str, str, str, str]]
    data_files: list[str]


def _entries(space: Workspace, modules: set[str], known: set[str]) -> dict[str, str]:
    entries: dict[str, str] = {}
    for name in modules:
        directory = f"{space.root}{MODULES_DIR}/{name}/"
        candidates = [f"{directory}{entry}" for entry in MODULE_ENTRY_POINTS]
        entries[name] = next((one for one in candidates if one in known), directory)
    return entries


def _layered(
    space: Workspace, data: dict[str, dict[Any, Any]]
) -> Iterator[tuple[str, str, str]]:
    """Yield (variable, value, path) for every data file a layer pattern names."""
    for path in sorted(data):
        if not path.startswith(space.data_dir):
            continue
        relative = path[len(space.data_dir) :]
        for layer in space.layers:
            match = layer.fullmatch(relative)
            if match:
                for variable, value in match.groupdict().items():
                    yield variable, value, path
                break


def follow(
    space: Workspace, rel_paths: list[str], contents: dict[str, str]
) -> Followed:
    """Return the data edges of a workspace and what it provides and takes."""
    known = set(rel_paths)
    files = [one for one in workspace_files(space, rel_paths) if one in contents]
    data = {
        path: load_data(contents[path])
        for path in files
        if path.endswith(DATA_EXTENSIONS)
    }
    modules = {name for name in (_module_of(space, one) for one in rel_paths) if name}
    entries = _entries(space, modules, known)
    filled: dict[tuple[str, str], str] = {}
    for variable, value, path in _layered(space, data):
        filled.setdefault((variable, value), path)

    edges: list[DataEdge] = []
    taken: list[tuple[str, str, str, str]] = []
    values: dict[str, list[Any]] = {}
    selecting: set[str] = set()
    for path, top in data.items():
        own = _module_of(space, path)
        for raw_key, value in top.items():
            key = str(raw_key)
            if key in modules:
                values.setdefault(key, []).append(value)
                if own is None:
                    edges.append(DataEdge(path, entries[key], CONFIGURES))
            if own is not None:
                continue
            if key in space.variables and isinstance(value, str):
                target = filled.get((key, value))
                selecting.add(key)
                if target and target != path:
                    edges.append(DataEdge(path, target, SELECTS))
            elif key == MODULES_KEY and isinstance(value, list):
                for name in (one for one in value if isinstance(one, str)):
                    if name in entries:
                        edges.append(DataEdge(path, entries[name], INCLUDES_MODULE))
                    else:
                        taken.append(("deploy-module", name, path, INCLUDES_MODULE))

    applying = {edge.source_id for edge in edges}
    for path in sorted(applying):
        stem = posixpath.basename(path).rsplit(".", 1)[0]
        if is_fqdn(stem):
            taken.append(("host", stem, path, DEPLOYS_TO))

    for name in sorted(modules):

        def lookup(attribute: str, module: str = name) -> list[str]:
            found: list[str] = []
            for value in values.get(module, []):
                if isinstance(value, dict) and attribute in value:
                    found.extend(_scalars(value[attribute]))
            return [one for one in found if one][:MAX_VALUES]

        prefix = f"{space.root}{MODULES_DIR}/{name}/"
        sources = {
            path: contents[path]
            for path in files
            if path.startswith(prefix) and path.endswith(CODE_EXTENSION)
        }
        for target, relation in pyinfra_uses(sources, lookup) if sources else []:
            kind, _, value = target.partition(":")
            taken.append((kind, value, entries[name], relation))

    provided = [
        ("deploy-module", name, f"{space.root}{MODULES_DIR}/{name}/")
        for name in sorted(modules)
    ]
    provided.extend(
        ("deploy-role", value, path)
        for (variable, value), path in sorted(filled.items())
        if variable in selecting
    )
    return Followed(
        edges=list(dict.fromkeys(edges)),
        provided=provided,
        taken=list(dict.fromkeys(taken)),
        data_files=sorted(data),
    )
