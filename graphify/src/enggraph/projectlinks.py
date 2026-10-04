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

from enggraph import puppetdata
from enggraph.config import MAX_NAME_LENGTH
from enggraph.hierarchy import depth_of, parent_of
from enggraph.identifiers import owner_path, truncate
from enggraph.parsers.terraform import MODULE_PREFIX
from enggraph.parsers.workspace import (
    MODULE_DIR,
    NODE_FILE,
    ROLE_FILE,
    WORKSPACE_DATA,
)
from enggraph.parsers.yamldocs import load_yaml_documents
from enggraph.resolution import ANSIBLE_ROLE_ENTRY_POINTS, HCL_SOURCE_EXTENSIONS

DEPENDS_ON = "depends_on"
DEPLOYS_TO = "deploys_to"
INSTALLS = "installs"
USES_BUCKET = "uses_bucket"
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
    ".package.yaml",
    "nfpm.yaml",
    "nfpm.yml",
)
# The id prefixes of the placeholder nodes the parsers write, by kind.
PLACEHOLDER_KINDS = {
    "image:": "image",
    "role:": "role",
    "npm:": "npm",
    "composer:": "composer",
    "deploy-role:": "deploy-role",
    "deploy-module:": "deploy-module",
    MODULE_PREFIX: "tfmodule",
    "package:": "package",
    "pypi:": "pypi",
}
# The configuration a Terraform circuit reads its instances from, beside it.
CIRCUIT_CONFIGS = ("config.yaml", "config.yml")
YAML_EXTENSIONS = (".yaml", ".yml")
# What a circuit config creates, by the key listing it.
CIRCUIT_KINDS = {"instances": "host", "buckets": "bucket"}
MAKEFILE_NAMES = ("makefile", "gnumakefile")
SPEC_EXTENSION = ".spec"
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
SPEC_NAME = re.compile(r"^Name:\s*(\S+)\s*$", re.M | re.I)
SPEC_SUBPACKAGE = re.compile(r"^%package\s+(-n\s+)?(\S+)\s*$", re.M)
SPEC_REQUIRES = re.compile(r"^Requires(?:\([^)]*\))?:\s*(.+)$", re.M | re.I)
SPEC_OPERATORS = frozenset({"<", ">", "=", "<=", ">=", "=="})
MAKE_VARIABLE = re.compile(
    r"^\s*(?:export\s+|override\s+)?([A-Za-z_][\w.]*)\s*(?:::=|:=|\?=|\+=|=)\s*(.*?)\s*$",
    re.M,
)
MAKE_REFERENCE = re.compile(r"\$\(([^()]*)\)|\$\{([^{}]*)\}")
IMAGE_BUILD = re.compile(r"\b(?:docker|podman)\s+(?:buildx\s+)?build\b[^\n]*")
# Build options that take a value, so the value is not the build context.
BUILD_VALUE_OPTIONS = frozenset(
    {
        "-t",
        "--tag",
        "-f",
        "--file",
        "--build-arg",
        "--target",
        "--platform",
        "--label",
        "--secret",
        "--ssh",
        "--cache-from",
        "--cache-to",
        "--network",
        "--progress",
        "-o",
        "--output",
        "--iidfile",
    }
)
IMAGE_TAG = re.compile(r"(?:\s-t|\s--tag)[\s=]+['\"]?([^\s'\"]+)")
# Stands for what a Makefile computes at run time, a version or a commit.
UNKNOWN = "\0"
MAKE_BUCKET = re.compile(
    r"^\s*(?:export\s+)?\w*BUCKET\s*[?:+]?=\s*(\S+)\s*$", re.M | re.I
)
SOURCE_GETTER = re.compile(r"^[a-z0-9]+::")
SOURCE_SCHEME = re.compile(r"^[a-z0-9+]+://")
SOURCE_USER = re.compile(r"^[^@/]+@")


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
    elif kind == "host":
        name = name.rstrip(".").lower()
    elif kind == "bucket":
        name = name.lower()
    elif kind == "package" and "%" in name:
        return ""
    elif kind == "tfmodule":
        name = _module_source(name)
    return truncate(name, MAX_NAME_LENGTH)


def _module_source(raw: str) -> str:
    """Return a remote module source without getter, scheme, user or ref."""
    name = SOURCE_GETTER.sub("", raw.lower()).split("?", 1)[0]
    name = SOURCE_USER.sub("", SOURCE_SCHEME.sub("", name))
    host, colon, rest = name.partition(":")
    # git@example.com:alpha/infra.git is example.com/alpha/infra.
    if colon and "/" not in host and not rest[:1].isdigit():
        name = f"{host}/{rest}"
    return name.replace(".git//", "//").removesuffix(".git").rstrip("/")


def manifest_candidates(rel_paths: list[str]) -> list[str]:
    """Return every path a manifest could have beside the indexed files."""
    directories = {posixpath.dirname(rel_path) for rel_path in rel_paths} | {""}
    return (
        sorted(
            posixpath.join(directory, name)
            for directory in directories
            for name in MANIFEST_NAMES
        )
        + circuit_configs(rel_paths)
        + host_definitions(rel_paths)
        + sorted(
            rel_path
            for rel_path in rel_paths
            if rel_path.endswith(SPEC_EXTENSION) or is_makefile(rel_path)
        )
        + workspace_data(rel_paths)
    )


def is_makefile(rel_path: str) -> bool:
    """Report whether a path is a Makefile, by name or by extension."""
    base = posixpath.basename(rel_path).lower()
    return base in MAKEFILE_NAMES or base.endswith(".mk")


def circuit_configs(rel_paths: list[str]) -> list[str]:
    """Return the configs that sit beside a Terraform or Terragrunt file."""
    circuits = {
        posixpath.dirname(rel_path)
        for rel_path in rel_paths
        if rel_path.endswith(HCL_SOURCE_EXTENSIONS)
    }
    return sorted(
        rel_path
        for rel_path in rel_paths
        if rel_path.endswith(YAML_EXTENSIONS)
        and posixpath.dirname(rel_path) in circuits
    )


def _yaml_stem(rel_path: str) -> str:
    base = posixpath.basename(rel_path)
    for extension in YAML_EXTENSIONS:
        base = base.removesuffix(extension)
    return base


def host_definitions(rel_paths: list[str]) -> list[str]:
    """Return the YAML files named like a host, which may define that host."""
    return sorted(
        rel_path
        for rel_path in rel_paths
        if rel_path.endswith(YAML_EXTENSIONS) and "." in _yaml_stem(rel_path)
    )


def host_exports(rel_path: str, content: str) -> list[Export]:
    """Return the host a file defines: one named like it, keyed by that name.

    `vm/web-01.example.com.yaml` holding `web-01.example.com: {cpu: 2}` is
    the definition a provisioning tree keeps per machine.
    """
    stem = _yaml_stem(rel_path)
    documents = load_yaml_documents(content) or []
    document = documents[0] if documents else None
    if not isinstance(document, dict) or not isinstance(document.get(stem), dict):
        return []
    host = normalize("host", stem)
    return [Export("host", host, rel_path)] if host else []


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


def _yaml_name(content: str) -> tuple[list[str], list[tuple[str, str]]]:
    documents = load_yaml_documents(content) or []
    document = documents[0] if documents else None
    name = document.get("name") if isinstance(document, dict) else None
    return ([name] if isinstance(name, str) else []), []


READERS = {
    "pyproject.toml": ("pypi", _pyproject_names),
    "setup.cfg": ("pypi", _setup_cfg_names),
    "requirements.txt": ("pypi", _requirements_names),
    "Cargo.toml": ("cargo", _cargo_names),
    "go.mod": ("go", _go_names),
    "CMakeLists.txt": ("cmake", _cmake_names),
    ".package.yaml": ("package", _yaml_name),
    "nfpm.yaml": ("package", _yaml_name),
    "nfpm.yml": ("package", _yaml_name),
}


def circuit_exports(rel_path: str, content: str) -> list[Export]:
    """Return the hosts and buckets a circuit config creates, by their keys."""
    documents = load_yaml_documents(content) or []
    document = documents[0] if documents else None
    if not isinstance(document, dict):
        return []
    exports: list[Export] = []
    for key, kind in CIRCUIT_KINDS.items():
        created = document.get(key)
        if isinstance(created, dict):
            names = (normalize(kind, str(name)) for name in created)
            exports.extend(Export(kind, name, rel_path) for name in names if name)
    return exports


def _spec_names(content: str) -> tuple[list[str], list[tuple[str, str]]]:
    main = SPEC_NAME.search(content)
    provided = [main.group(1)] if main else []
    for match in SPEC_SUBPACKAGE.finditer(content):
        own = match.group(2)
        provided.append(own if match.group(1) or not main else f"{main.group(1)}-{own}")
    taken: list[tuple[str, str]] = []
    for line in SPEC_REQUIRES.findall(content):
        previous = ""
        for token in line.replace(",", " ").split():
            # The token after an operator is the version it constrains.
            versioned = previous in SPEC_OPERATORS
            previous = token
            if versioned or token in SPEC_OPERATORS:
                continue
            if not token.startswith("/") and "(" not in token:
                taken.append(("package", token))
    return provided, taken


def _make_buckets(content: str) -> list[str]:
    return MAKE_BUCKET.findall(content)


def _expand(value: str, variables: dict[str, str], depth: int = 5) -> str:
    def replace(match: re.Match[str]) -> str:
        name = (match.group(1) or match.group(2) or "").strip()
        known = variables.get(name)
        if known is None or depth == 0:
            return UNKNOWN
        return _expand(known, variables, depth - 1)

    return MAKE_REFERENCE.sub(replace, value)


def _build_context(line: str) -> str | None:
    """Return the context argument of a build command, or None."""
    tokens = line.split()
    start = tokens.index("build") + 1 if "build" in tokens else len(tokens)
    skip = False
    for token in tokens[start:]:
        if skip:
            skip = False
        elif token in BUILD_VALUE_OPTIONS:
            skip = True
        elif not token.startswith("-") and token not in ("&&", ";", "|"):
            return token
    return None


def _make_images(rel_path: str, content: str) -> list[tuple[str, str]]:
    """Return (image, build context) for what a Makefile builds.

    Variables of the Makefile itself are expanded; the context is the
    directory the image is built from, so the code under it is what it holds.
    """
    joined = content.replace("\\\n", " ")
    variables = dict(MAKE_VARIABLE.findall(joined))
    directory = posixpath.dirname(rel_path)
    images: list[tuple[str, str]] = []
    for line in IMAGE_BUILD.findall(joined):
        context = _build_context(line)
        built_from = parent_of(rel_path)
        if context is not None:
            expanded = _expand(context.strip("'\""), variables)
            joined_path = posixpath.normpath(posixpath.join(directory, expanded))
            if UNKNOWN not in expanded and not joined_path.startswith(".."):
                built_from = "./" if joined_path == "." else f"{joined_path}/"
        for raw in IMAGE_TAG.findall(line):
            # A tag computed at run time is dropped with the tag itself.
            name = normalize("image", _expand(raw, variables))
            if name and UNKNOWN not in name:
                images.append((name, built_from))
    return images


def read_manifest(
    rel_path: str, content: str, indexed: set[str]
) -> tuple[list[Export], list[Import]]:
    """Return what one manifest declares and what it depends on."""
    base = posixpath.basename(rel_path)
    if base in CIRCUIT_CONFIGS:
        return circuit_exports(rel_path, content), []
    if is_makefile(rel_path):
        imports = [
            Import("bucket", name, rel_path, USES_BUCKET)
            for name in (normalize("bucket", raw) for raw in _make_buckets(content))
            if name
        ]
        exports = [
            Export("image", name, built_from)
            for name, built_from in _make_images(rel_path, content)
        ]
        return exports, imports
    if base.endswith(SPEC_EXTENSION):
        kind = "package"
        provided, taken = _spec_names(content)
    elif base in NAME_KEY:
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


def workspace_roots(rel_paths: list[str]) -> set[str]:
    """Return the roots holding both workspace data and modules."""
    data = {
        match.group("root")
        for match in (
            NODE_FILE.match(rel_path) or ROLE_FILE.match(rel_path)
            for rel_path in rel_paths
        )
        if match
    }
    modules = {
        match.group("root")
        for match in (MODULE_DIR.match(rel_path) for rel_path in rel_paths)
        if match
    }
    return data & modules


def puppet_links(
    contents: dict[str, str],
) -> tuple[list[puppetdata.DataEdge], list[Import]]:
    """Return a Puppet tree's data edges, and what its classes take through them.

    A parameter value is an artifact the class runs or installs; a data file
    named like a host that applies, configures or selects is a host it deploys to.
    """
    edges, values = puppetdata.follow(contents)
    imports = [
        Import(value.kind, name, value.manifest, value.relation)
        for value in values
        if (name := normalize(value.kind, value.name))
    ]
    for path in puppetdata.host_files(edges):
        host = normalize("host", puppetdata.host_name(path))
        if host:
            imports.append(Import("host", host, path, DEPLOYS_TO))
    return edges, imports


def workspace_data(rel_paths: list[str]) -> list[str]:
    """Return the YAML data files of every workspace in a tree."""
    roots = workspace_roots(rel_paths)
    return sorted(
        rel_path
        for rel_path in rel_paths
        if rel_path.endswith((".yaml", ".yml"))
        and _workspace_data_root(rel_path) in roots
    )


def _workspace_data_root(rel_path: str) -> str | None:
    match = WORKSPACE_DATA.match(rel_path)
    return match.group("root") if match else None


def _named(value: Any) -> list[str]:  # noqa: ANN401
    if isinstance(value, dict):
        return [str(key) for key in value]
    return _strings(value)


def workspace_data_imports(rel_path: str, content: str) -> list[Import]:
    """Return the packages a workspace data file installs and buckets it reads."""
    found: list[tuple[str, str, str]] = []
    stack: list[Any] = list(load_yaml_documents(content) or [])
    while stack:
        node = stack.pop()
        if isinstance(node, list):
            stack.extend(node)
            continue
        if not isinstance(node, dict):
            continue
        for key, value in node.items():
            if key == "packages":
                listed = value.get("list") if isinstance(value, dict) else None
                names = _named(listed if isinstance(listed, dict) else value)
                found.extend(("package", name, INSTALLS) for name in names)
            elif key == "package" and isinstance(value, str):
                found.append(("package", value, INSTALLS))
            elif key == "bucket" and isinstance(value, str):
                found.append(("bucket", value, USES_BUCKET))
            else:
                stack.append(value)
    return [
        Import(kind, name, rel_path, relation)
        for kind, name, relation in (
            (kind, normalize(kind, raw), relation) for kind, raw, relation in found
        )
        if name
    ]


def workspace_links(rel_paths: list[str]) -> tuple[list[Export], list[Import]]:
    """Return a workspace's roles and modules, and the hosts it deploys to."""
    roots = workspace_roots(rel_paths)
    exports: list[Export] = []
    imports: list[Import] = []
    for rel_path in rel_paths:
        role = ROLE_FILE.match(rel_path)
        if role and role.group("root") in roots:
            exports.append(Export("deploy-role", role.group("name"), rel_path))
        node = NODE_FILE.match(rel_path)
        if node and node.group("root") in roots:
            host = normalize("host", node.group("name"))
            if host:
                imports.append(Import("host", host, rel_path, DEPLOYS_TO))
        module = MODULE_DIR.match(rel_path)
        if module and module.group("root") in roots:
            directory = f"{module.group('root')}modules/{module.group('name')}/"
            exports.append(Export("deploy-module", module.group("name"), directory))
    return exports, imports


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
    exports, imports = workspace_links(rel_paths)
    exports += role_exports(rel_paths) + image_exports(image_rows, indexed)
    imports += placeholder_imports(placeholder_rows)
    data = set(workspace_data(rel_paths))
    circuits = set(circuit_configs(rel_paths))
    for rel_path in sorted(manifests):
        if rel_path in data:
            imports.extend(workspace_data_imports(rel_path, manifests[rel_path]))
            continue
        if rel_path.endswith(YAML_EXTENSIONS):
            exports.extend(host_exports(rel_path, manifests[rel_path]))
        if rel_path in circuits:
            exports.extend(circuit_exports(rel_path, manifests[rel_path]))
            continue
        provided, taken = read_manifest(rel_path, manifests[rel_path], indexed)
        exports.extend(provided)
        imports.extend(taken)
    # The shallowest directory wins a name declared twice in one tree.
    exports.sort(key=lambda export: (depth_of(export.node_id), export.node_id))
    unique: dict[tuple[str, str], Export] = {}
    for export in exports:
        unique.setdefault((export.kind, export.name), export)
    return list(unique.values()), sorted(set(imports), key=lambda i: (i.kind, i.name))
