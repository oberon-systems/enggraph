"""What a project provides to other projects, and what it takes from them.

Nothing here touches the database or a model: manifests and the placeholder
edges an index run already wrote are turned into names, and a view joins the
names of one project to those of another.
"""

from __future__ import annotations

import configparser
import json
import posixpath
import re
import tomllib
from dataclasses import dataclass
from typing import Any

from enggraph.config import MAX_NAME_LENGTH
from enggraph.hierarchy import depth_of, parent_of
from enggraph.identifiers import owner_path, truncate
from enggraph.resolution import ANSIBLE_ROLE_ENTRY_POINTS

DEPENDS_ON = "depends_on"
MANIFEST_NAMES = (
    "package.json",
    "composer.json",
    "pyproject.toml",
    "setup.cfg",
    "requirements.txt",
    "Cargo.toml",
    "go.mod",
    "CMakeLists.txt",
    "vcpkg.json",
)
# The id prefixes of the placeholder nodes the parsers write, by kind.
PLACEHOLDER_KINDS = {
    "image:": "image",
    "role:": "role",
    "npm:": "npm",
    "composer:": "composer",
}
NAME_KEY = {"package.json": "npm", "composer.json": "composer", "vcpkg.json": "vcpkg"}
CARGO_TABLES = ("dependencies", "dev-dependencies", "build-dependencies")
IMAGE_REGISTRY_DEFAULTS = ("docker.io/", "library/")
ROLES_DIR = "roles"

PEP508_NAME = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)")
PEP503_RUN = re.compile(r"[-_.]+")
CMAKE_PROJECT = re.compile(r"^\s*project\s*\(\s*([A-Za-z0-9_.+-]+)", re.I | re.M)
CMAKE_PACKAGE = re.compile(r"^\s*find_package\s*\(\s*([A-Za-z0-9_.+-]+)", re.I | re.M)
GO_MODULE = re.compile(r"^module\s+(\S+)", re.M)
GO_REQUIRE = re.compile(r"^(?:require\s+)?([^\s()]+)\s+v\d\S*")


@dataclass(frozen=True)
class Export:
    """A name a project provides, and the directory that is the thing."""

    kind: str
    name: str
    node_id: str


@dataclass(frozen=True)
class Import:
    """A name a project takes from outside, and the node that takes it."""

    kind: str
    name: str
    source_id: str
    relation: str


def normalize(kind: str, raw: str) -> str:
    """Return the name two projects are matched on, or "" for no name."""
    name = raw.strip()
    if not name or "$" in name:
        return ""
    if kind == "image":
        name = name.split("@", 1)[0]
        head, _, last = name.rpartition("/")
        last = last.split(":", 1)[0]
        name = f"{head}/{last}" if head else last
        for prefix in IMAGE_REGISTRY_DEFAULTS:
            name = name.removeprefix(prefix)
    elif kind == "pypi":
        match = PEP508_NAME.match(name)
        name = PEP503_RUN.sub("-", match.group(1)).lower() if match else ""
    elif kind == "role":
        name = posixpath.basename(name.rstrip("/"))
    return truncate(name, MAX_NAME_LENGTH)


def manifest_candidates(rel_paths: list[str]) -> list[str]:
    """Return every path a manifest could have beside the indexed files."""
    directories = {posixpath.dirname(rel_path) for rel_path in rel_paths} | {""}
    return sorted(
        posixpath.join(directory, name)
        for directory in directories
        for name in MANIFEST_NAMES
    )


def _loaded_toml(content: str) -> dict[str, Any]:
    try:
        return tomllib.loads(content)
    except tomllib.TOMLDecodeError:
        return {}


def _loaded_json(content: str) -> dict[str, Any]:
    try:
        loaded = json.loads(content)
    except ValueError:
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _table(value: Any, *keys: str) -> dict[str, Any]:  # noqa: ANN401
    for key in keys:
        value = value.get(key) if isinstance(value, dict) else None
    return value if isinstance(value, dict) else {}


def _strings(value: Any) -> list[str]:  # noqa: ANN401
    if not isinstance(value, list):
        return []
    return [entry for entry in value if isinstance(entry, str)]


def _json_names(content: str, kind: str) -> tuple[list[str], list[tuple[str, str]]]:
    document = _loaded_json(content)
    name = document.get("name")
    provided = [name] if isinstance(name, str) else []
    taken: list[tuple[str, str]] = []
    # npm and composer dependencies are placeholder edges already.
    if kind == "vcpkg":
        for entry in document.get("dependencies") or []:
            wanted = entry.get("name") if isinstance(entry, dict) else entry
            if isinstance(wanted, str):
                taken.append((kind, wanted))
    return provided, taken


def _pyproject_names(content: str) -> tuple[list[str], list[tuple[str, str]]]:
    document = _loaded_toml(content)
    project = _table(document, "project")
    poetry = _table(document, "tool", "poetry")
    names = (project.get("name"), poetry.get("name"))
    provided = [name for name in names if isinstance(name, str)]
    wanted = _strings(project.get("dependencies"))
    for group in _table(project, "optional-dependencies").values():
        wanted.extend(_strings(group))
    wanted.extend(key for key in _table(poetry, "dependencies") if key != "python")
    return provided[:1], [("pypi", name) for name in wanted]


def _setup_cfg_names(content: str) -> tuple[list[str], list[tuple[str, str]]]:
    parser = configparser.ConfigParser(interpolation=None)
    try:
        parser.read_string(content)
    except configparser.Error:
        return [], []
    name = parser.get("metadata", "name", fallback="")
    wanted = parser.get("options", "install_requires", fallback="").splitlines()
    return ([name] if name else []), [("pypi", line) for line in wanted]


def _requirements_names(content: str) -> tuple[list[str], list[tuple[str, str]]]:
    wanted = [
        line
        for line in (raw.split("#", 1)[0].strip() for raw in content.splitlines())
        if line and not line.startswith("-") and "://" not in line
    ]
    return [], [("pypi", line) for line in wanted]


def _cargo_names(content: str) -> tuple[list[str], list[tuple[str, str]]]:
    document = _loaded_toml(content)
    name = _table(document, "package").get("name")
    tables = [_table(document, key) for key in CARGO_TABLES]
    tables.append(_table(document, "workspace", "dependencies"))
    taken: list[tuple[str, str]] = []
    for table in tables:
        for key, value in table.items():
            renamed = value.get("package") if isinstance(value, dict) else None
            taken.append(("cargo", renamed if isinstance(renamed, str) else key))
    return ([name] if isinstance(name, str) else []), taken


def _go_names(content: str) -> tuple[list[str], list[tuple[str, str]]]:
    module = GO_MODULE.search(content)
    taken: list[tuple[str, str]] = []
    for raw in content.splitlines():
        if "// indirect" in raw:
            continue
        match = GO_REQUIRE.match(raw.strip())
        if match:
            taken.append(("go", match.group(1)))
    return ([module.group(1)] if module else []), taken


def _cmake_names(content: str) -> tuple[list[str], list[tuple[str, str]]]:
    return (
        CMAKE_PROJECT.findall(content),
        [("cmake", name) for name in CMAKE_PACKAGE.findall(content)],
    )


READERS = {
    "pyproject.toml": ("pypi", _pyproject_names),
    "setup.cfg": ("pypi", _setup_cfg_names),
    "requirements.txt": ("pypi", _requirements_names),
    "Cargo.toml": ("cargo", _cargo_names),
    "go.mod": ("go", _go_names),
    "CMakeLists.txt": ("cmake", _cmake_names),
}


def read_manifest(
    rel_path: str, content: str, indexed: set[str]
) -> tuple[list[Export], list[Import]]:
    """Return what one manifest declares and what it depends on."""
    base = posixpath.basename(rel_path)
    if base in NAME_KEY:
        kind = NAME_KEY[base]
        provided, taken = _json_names(content, kind)
    elif base in READERS:
        kind, reader = READERS[base]
        provided, taken = reader(content)
    else:
        return [], []
    directory = parent_of(rel_path)
    source_id = rel_path if rel_path in indexed else directory
    exports = [
        Export(kind, name, directory)
        for name in (normalize(kind, raw) for raw in provided)
        if name
    ]
    imports = [
        Import(wanted_kind, name, source_id, DEPENDS_ON)
        for wanted_kind, name in (
            (wanted_kind, normalize(wanted_kind, raw)) for wanted_kind, raw in taken
        )
        if name
    ]
    return exports, imports


def role_exports(rel_paths: list[str]) -> list[Export]:
    """Return the Ansible roles a tree holds under a `roles` directory."""
    exports: list[Export] = []
    for rel_path in rel_paths:
        for entry_point in ANSIBLE_ROLE_ENTRY_POINTS:
            if not rel_path.endswith(f"/{entry_point}"):
                continue
            role_dir = rel_path[: -len(entry_point) - 1]
            holder, name = posixpath.split(role_dir)
            if posixpath.basename(holder) == ROLES_DIR and name:
                exports.append(Export("role", name, f"{role_dir}/"))
    return exports


def image_exports(rows: list[tuple[str, str, str]], indexed: set[str]) -> list[Export]:
    """Return the images a tree builds: (image id, build target, service id)."""
    exports: list[Export] = []
    for image_id, dockerfile, service_id in rows:
        name = normalize("image", image_id.removeprefix("image:"))
        built_from = dockerfile if dockerfile in indexed else owner_path(service_id)
        if name:
            exports.append(Export("image", name, parent_of(built_from)))
    return exports


def placeholder_imports(rows: list[tuple[str, str, str]]) -> list[Import]:
    """Return what the placeholder edges take: (source id, target id, relation)."""
    imports: list[Import] = []
    for source_id, target_id, relation in rows:
        for prefix, kind in PLACEHOLDER_KINDS.items():
            if not target_id.startswith(prefix):
                continue
            name = normalize(kind, target_id[len(prefix) :])
            if name:
                imports.append(Import(kind, name, source_id, relation))
    return imports


def collect(
    rel_paths: list[str],
    manifests: dict[str, str],
    placeholder_rows: list[tuple[str, str, str]],
    image_rows: list[tuple[str, str, str]],
) -> tuple[list[Export], list[Import]]:
    """Return every export and import of one tree, each name once."""
    indexed = set(rel_paths)
    exports = role_exports(rel_paths) + image_exports(image_rows, indexed)
    imports = placeholder_imports(placeholder_rows)
    for rel_path in sorted(manifests):
        provided, taken = read_manifest(rel_path, manifests[rel_path], indexed)
        exports.extend(provided)
        imports.extend(taken)
    # The shallowest directory wins a name declared twice in one tree.
    exports.sort(key=lambda export: (depth_of(export.node_id), export.node_id))
    unique: dict[tuple[str, str], Export] = {}
    for export in exports:
        unique.setdefault((export.kind, export.name), export)
    return list(unique.values()), sorted(set(imports), key=lambda i: (i.kind, i.name))
