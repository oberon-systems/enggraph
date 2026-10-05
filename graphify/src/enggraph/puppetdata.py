"""Follow a Puppet class out into the data that applies and configures it.

The class is the root. Every YAML file of the tree is searched for it: a
`classes:` list naming it applies it, a `class::param` key configures it, and a
file whose value names one of those files by its stem selects it, the way a
node file names its role. Where the data lives is never assumed: hiera layouts
differ from tree to tree, the class names do not.
"""

from __future__ import annotations

import posixpath
import re
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

from enggraph.parsers.puppetuses import class_parameters, lookup_keys, puppet_uses
from enggraph.parsers.yamldocs import load_yaml_documents

PUPPET_EXTENSION = ".pp"
DATA_EXTENSIONS = (".yaml", ".yml", ".eyaml")
CLASSES_KEY = "classes"
INCLUDES_CLASS = "includes_class"
CONFIGURES = "configures"
SELECTS = "selects"
EDGE_SOURCE = "puppetdata"
NAME_SEPARATORS = re.compile(r"[:/_.\-\s]+")

Name = tuple[str, ...]


@dataclass(frozen=True)
class DataEdge:
    """An edge from a data file to a class manifest or to another data file."""

    source_id: str
    target_id: str
    relation: str


@dataclass(frozen=True)
class ParameterValue:
    """An artifact a class takes, its name resolved through the data."""

    kind: str
    name: str
    manifest: str
    relation: str


def _stem(rel_path: str) -> str:
    base = posixpath.basename(rel_path)
    for extension in DATA_EXTENSIONS:
        base = base.removesuffix(extension)
    return base


def _canonical(name: str) -> Name:
    """Return a name as its words, whatever separates them in a given tree.

    `alpha::web`, `alpha_web`, `alpha-web` and `alpha/web` are one name: how a
    role value becomes a data path is each tree's own convention.
    """
    return tuple(part for part in NAME_SEPARATORS.split(name.lower()) if part)


def _key(name: str) -> Name:
    words = _canonical(name)
    return (*words[:-1], words[-1].removesuffix("s")) if words else words


def _places(rel_path: str) -> Iterator[tuple[Name, Name]]:
    """Yield (directory, the rest of the path) for every directory above a file."""
    parts = rel_path.split("/")
    parts[-1] = _stem(rel_path)
    for index in range(len(parts) - 1):
        yield _key(parts[index]), _canonical("/".join(parts[index + 1 :]))


def _top(content: str) -> dict[str, Any]:
    documents = load_yaml_documents(content) or []
    document = documents[0] if documents else None
    return document if isinstance(document, dict) else {}


def _strings(value: Any) -> list[str]:  # noqa: ANN401
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [entry for entry in value if isinstance(entry, str)]
    if isinstance(value, dict):
        # A hash of resources is keyed by their titles.
        return [str(key) for key in value]
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
    readers: dict[str, list[str]] = {}
    for path in contents:
        if path.endswith(PUPPET_EXTENSION):
            for key in lookup_keys(contents[path]):
                readers.setdefault(key, []).append(path)
    for path, top in data.items():
        for name in _strings(top.get(CLASSES_KEY)):
            manifest = classes.get(name.lstrip(":"))
            if manifest:
                edges.append(DataEdge(path, manifest, INCLUDES_CLASS))
                applying[path] = manifest
        for key, value in top.items():
            for manifest in readers.get(str(key).lstrip(":"), []):
                edges.append(DataEdge(path, manifest, CONFIGURES))
                applying.setdefault(path, manifest)
                values.setdefault(str(key).lstrip(":"), []).extend(_strings(value))
            owner, separator, _ = str(key).lstrip(":").rpartition("::")
            manifest = classes.get(owner) if separator else None
            if manifest:
                edges.append(DataEdge(path, manifest, CONFIGURES))
                applying.setdefault(path, manifest)
                values.setdefault(str(key).lstrip(":"), []).extend(_strings(value))
    # A file names another under the key its directory is named after:
    # `role: web` in a node file and `role/web.yaml` holding classes.
    by_place: dict[tuple[Name, Name], set[str]] = {}
    for path in applying:
        for place in _places(path):
            by_place.setdefault(place, set()).add(path)
    for path, top in data.items():
        for key, value in top.items():
            if not isinstance(value, str):
                continue
            targets = by_place.get((_key(str(key)), _canonical(value)), set())
            # A name per layer root (web.yaml and web.eyaml are one); two names
            # in one directory spelling the same words are a guess.
            names = {(posixpath.dirname(one), _stem(one)) for one in targets}
            folders = [folder for folder, _ in names]
            for target in sorted(targets - {path}):
                if folders.count(posixpath.dirname(target)) == 1:
                    edges.append(DataEdge(path, target, SELECTS))
    defaults: dict[str, list[str]] = {}
    for path in contents:
        if path.endswith(PUPPET_EXTENSION):
            owner, given = class_parameters(contents[path])
            for name, value in given.items():
                defaults[f"{owner}::{name}"] = [value]

    def lookup(key: str) -> list[str]:
        key = key.lstrip(":")
        set_by_data = [value for value in values.get(key, []) if "%{" not in value]
        return set_by_data or defaults.get(key, [])

    found: list[ParameterValue] = []
    for path in sorted(contents):
        if not path.endswith(PUPPET_EXTENSION):
            continue
        for target, relation in puppet_uses(contents[path], lookup):
            kind, _, name = target.partition(":")
            found.append(ParameterValue(kind, name, path, relation))
    return list(dict.fromkeys(edges)), list(dict.fromkeys(found))


def host_files(edges: list[DataEdge]) -> list[str]:
    """Return the data files named like a host that apply, configure or select."""
    return sorted({edge.source_id for edge in edges if "." in _stem(edge.source_id)})


def host_name(rel_path: str) -> str:
    """Return the host a data file is named after."""
    return _stem(rel_path)
