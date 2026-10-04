"""Follow a Puppet class out into the data that applies and configures it.

The class is the root. Every YAML file of the tree is searched for it: a
`classes:` list naming it applies it, a `class::param` key configures it, and a
file whose value names one of those files by its stem selects it, the way a
node file names its role. Where the data lives is never assumed: hiera layouts
differ from tree to tree, the class names do not.
"""

from __future__ import annotations

import posixpath
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

from enggraph.parsers.puppetuses import class_parameters, puppet_parameter_uses
from enggraph.parsers.yamldocs import load_yaml_documents

PUPPET_EXTENSION = ".pp"
DATA_EXTENSIONS = (".yaml", ".yml", ".eyaml")
CLASSES_KEY = "classes"
INCLUDES_CLASS = "includes_class"
CONFIGURES = "configures"
SELECTS = "selects"
EDGE_SOURCE = "puppetdata"


@dataclass(frozen=True)
class DataEdge:
    """An edge from a data file to a class manifest or to another data file."""

    source_id: str
    target_id: str
    relation: str


@dataclass(frozen=True)
class ParameterValue:
    """A value the data gives a class parameter that names an image or package."""

    kind: str
    name: str
    manifest: str
    relation: str


def _stem(rel_path: str) -> str:
    base = posixpath.basename(rel_path)
    for extension in DATA_EXTENSIONS:
        base = base.removesuffix(extension)
    return base


def _top(content: str) -> dict[str, Any]:
    documents = load_yaml_documents(content) or []
    document = documents[0] if documents else None
    return document if isinstance(document, dict) else {}


def _strings(value: Any) -> list[str]:  # noqa: ANN401
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [entry for entry in value if isinstance(entry, str)]
    return []


def class_index(contents: dict[str, str]) -> dict[str, str]:
    """Return the manifest declaring each class, the shallowest when repeated."""
    index: dict[str, str] = {}
    manifests = sorted(
        (path for path in contents if path.endswith(PUPPET_EXTENSION)),
        key=lambda path: (path.count("/"), path),
    )
    for path in manifests:
        owner, _ = class_parameters(contents[path])
        if owner:
            index.setdefault(owner, path)
    return index


def _data(contents: dict[str, str]) -> Iterator[tuple[str, dict[str, Any]]]:
    for path in sorted(contents):
        if path.endswith(DATA_EXTENSIONS):
            top = _top(contents[path])
            if top:
                yield path, top


def follow(contents: dict[str, str]) -> tuple[list[DataEdge], list[ParameterValue]]:
    """Return the data edges of a tree and the values its parameters take."""
    classes = class_index(contents)
    if not classes:
        return [], []
    data = dict(_data(contents))
    edges: list[DataEdge] = []
    values: dict[str, list[str]] = {}
    applying: dict[str, str] = {}
    for path, top in data.items():
        for name in _strings(top.get(CLASSES_KEY)):
            manifest = classes.get(name.lstrip(":"))
            if manifest:
                edges.append(DataEdge(path, manifest, INCLUDES_CLASS))
                applying[path] = manifest
        for key, value in top.items():
            owner, separator, _ = str(key).lstrip(":").rpartition("::")
            manifest = classes.get(owner) if separator else None
            if manifest:
                edges.append(DataEdge(path, manifest, CONFIGURES))
                applying.setdefault(path, manifest)
                values.setdefault(str(key).lstrip(":"), []).extend(_strings(value))
    # A file names another by its stem under the key its directory is named
    # after: `role: web` in a node file and `role/web.yaml` holding classes.
    by_place = {
        (posixpath.basename(posixpath.dirname(path)), _stem(path)): path
        for path in applying
    }
    for path, top in data.items():
        for key, value in top.items():
            if not isinstance(value, str):
                continue
            target = by_place.get((str(key), value))
            if target and target != path:
                edges.append(DataEdge(path, target, SELECTS))
    found: list[ParameterValue] = []
    for manifest in sorted(set(classes.values())):
        for kind, relation, key in puppet_parameter_uses(contents[manifest]):
            for name in values.get(key, []):
                if "%{" not in name:
                    found.append(ParameterValue(kind, name, manifest, relation))
    return list(dict.fromkeys(edges)), list(dict.fromkeys(found))


def selecting_hosts(edges: list[DataEdge]) -> list[str]:
    """Return the data files that select others and are named like a host."""
    return sorted(
        {
            edge.source_id
            for edge in edges
            if edge.relation == SELECTS and "." in _stem(edge.source_id)
        }
    )


def host_name(rel_path: str) -> str:
    """Return the host a selecting data file is named after."""
    return _stem(rel_path)
