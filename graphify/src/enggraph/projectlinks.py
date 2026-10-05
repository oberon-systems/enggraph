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
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from enggraph import puppetdata, workspacedata
from enggraph.config import MAX_NAME_LENGTH
from enggraph.hierarchy import depth_of, parent_of
from enggraph.identifiers import is_fqdn, owner_path, truncate
from enggraph.parsers.terraform import MODULE_PREFIX
from enggraph.parsers.yamldocs import load_yaml_documents
from enggraph.resolution import ANSIBLE_ROLE_ENTRY_POINTS, HCL_SOURCE_EXTENSIONS

DEPENDS_ON = "depends_on"
DEPLOYS_TO = "deploys_to"
INSTALLS = "installs"
USES_BUCKET = "uses_bucket"
USES_IMAGE = "uses_image"
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
IMAGE_RETAG = re.compile(r"\b(?:docker|podman)\s+(?:image\s+)?tag\b[^\n]*")
IMAGE_PUSH = re.compile(r"\b(?:docker|podman)\s+(?:image\s+)?push\b[^\n]*")
COMMAND_SEPARATORS = frozenset({"&&", "||", ";", "|"})
# Stands for what a Makefile computes at run time, a version or a commit.
UNKNOWN = "\0"
SHELL_EXTENSIONS = (".sh", ".bash")
SHELL_LEFTOVER = re.compile(r"\$[(`{]?[^/:@\s'\"]*|`[^`]*")
IMAGE_TOOLS = frozenset({"docker", "podman"})
SHELL_SHEBANG = re.compile(r"^#!\s*\S*?(?:/env\s+)?\b(?:ba|z|k|da)?sh\b")
SHELL_VARIABLE = re.compile(
    r"^\s*(?:export\s+|local\s+|readonly\s+|declare\s+(?:-\w+\s+)*)?"
    r"([A-Za-z_]\w*)=(\"[^\"\n]*\"|'[^'\n]*'|[^\s;&|]*)",
    re.M,
)
SHELL_REFERENCE = re.compile(
    r"\$\{([A-Za-z_]\w*)(?::?[-=]([^}]*))?\}|\$([A-Za-z_]\w*)|\$\([^)]*\)|`[^`]*`"
)
MAKE_BUCKET = re.compile(
    r"^\s*(?:export\s+)?\w*BUCKET\s*[?:+]?=\s*(\S+)\s*$", re.M | re.I
)
TERRAFORM_EXTENSIONS = (".tf",)
TF_RESOURCE = re.compile(r'^\s*resource\s+"([\w-]+)"\s+"[^"]*"\s*\{', re.M)
TF_ATTRIBUTE = re.compile(r"^\s*([A-Za-z_][\w-]*)\s*=")
HCL_STRING = re.compile(r'"((?:[^"\\\n]|\\.)*)"')
HCL_COMMENT = re.compile(r"(?:#|//).*$")
TAG_NAME = re.compile(r'\bName\s*=\s*"([^"$\n]+)"')
# Resources that create a machine, and where they name it.
MACHINE_RESOURCES = frozenset(
    {
        "aws_instance",
        "azurerm_linux_virtual_machine",
        "azurerm_virtual_machine",
        "azurerm_windows_virtual_machine",
        "digitalocean_droplet",
        "exoscale_compute_instance",
        "google_compute_instance",
        "hcloud_server",
        "libvirt_domain",
        "linode_instance",
        "openstack_compute_instance_v2",
        "ovirt_vm",
        "proxmox_virtual_environment_vm",
        "proxmox_vm_qemu",
        "scaleway_instance_server",
        "vsphere_virtual_machine",
        "vultr_instance",
        "yandex_compute_instance",
    }
)
MACHINE_NAME_ATTRIBUTES = ("name", "hostname", "computer_name")
DNS_RESOURCES = frozenset(
    {
        "aws_route53_record",
        "cloudflare_dns_record",
        "cloudflare_record",
        "digitalocean_record",
        "dns_cname_record",
        "dnsimple_zone_record",
        "google_dns_record_set",
        "hetznerdns_record",
        "ovh_domain_zone_record",
        "powerdns_record",
    }
)
DNS_ATTRIBUTES = ("name", "records", "record", "value", "content", "rrdatas", "cname")
USES_HOST = "uses_host"
INVENTORY_NAMES = (
    "hosts",
    "hosts.ini",
    "hosts.yml",
    "hosts.yaml",
    "inventory",
    "inventory.ini",
    "inventory.yml",
    "inventory.yaml",
)
INVENTORY_DIRS = ("inventory", "inventories")
# Directories of an inventory that hold variables rather than hosts.
INVENTORY_VARS_DIRS = ("group_vars", "host_vars")
ANSIBLE_CFG = "ansible.cfg"
INI_SECTION = re.compile(r"^\[([^\]]+)\]$")
HOST_RANGE = re.compile(r"\[([0-9]+|[A-Za-z]):([0-9]+|[A-Za-z])\]")
MAX_RANGE = 256
COMPOSE_SERVICES = re.compile(r"^services:\s*$", re.M)
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
            if rel_path.endswith(
                (SPEC_EXTENSION, *SHELL_EXTENSIONS, *TERRAFORM_EXTENSIONS)
            )
            or is_makefile(rel_path)
            or "." not in posixpath.basename(rel_path)
        )
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
    return [Export("host", host, rel_path)] if host and is_fqdn(host) else []


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


def _hosts(raws: list[str]) -> list[str]:
    return [host for host in (normalize("host", raw) for raw in raws) if is_fqdn(host)]


def _resource_bodies(content: str) -> list[tuple[str, str]]:
    """Return (type, body) for every Terraform resource block of a file."""
    found: list[tuple[str, str]] = []
    for match in TF_RESOURCE.finditer(content):
        depth = 1
        rest = content[match.end() :].splitlines(keepends=True)
        for index, line in enumerate(rest):
            depth += _depth_change(line)
            if depth <= 0:
                found.append((match.group(1), "".join(rest[:index])))
                break
    return found


def _depth_change(line: str) -> int:
    bare = HCL_COMMENT.sub("", HCL_STRING.sub('""', line))
    return sum(bare.count(one) for one in "{[(") - sum(bare.count(one) for one in "}])")


def _top_attributes(body: str) -> dict[str, str]:
    """Return the text of every attribute a block sets at its own level."""
    attributes: dict[str, str] = {}
    depth = 0
    current: str | None = None
    for line in body.splitlines():
        if depth == 0:
            match = TF_ATTRIBUTE.match(line)
            current = match.group(1) if match else None
            if current is not None:
                attributes[current] = ""
        if current is not None:
            attributes[current] += line + "\n"
        depth = max(depth + _depth_change(line), 0)
    return attributes


def _literals(text: str) -> list[str]:
    return [value for value in HCL_STRING.findall(text) if "${" not in value]


def terraform_hosts(rel_path: str, content: str) -> tuple[list[Export], list[Import]]:
    """Return the machines a Terraform file creates and the hosts its DNS names."""
    exports: list[Export] = []
    imports: list[Import] = []
    for kind, body in _resource_bodies(content):
        attributes = _top_attributes(body)
        if kind in MACHINE_RESOURCES:
            raws = [
                value
                for key in MACHINE_NAME_ATTRIBUTES
                for value in _literals(attributes.get(key, ""))[:1]
            ]
            raws += TAG_NAME.findall(attributes.get("tags", ""))
            exports.extend(Export("host", host, rel_path) for host in _hosts(raws))
        elif kind in DNS_RESOURCES or (
            kind.startswith("azurerm_dns_") and kind.endswith("_record")
        ):
            raws = [
                value
                for key in DNS_ATTRIBUTES
                for value in _literals(attributes.get(key, ""))
            ]
            imports.extend(
                Import("host", host, rel_path, USES_HOST) for host in _hosts(raws)
            )
    return list(dict.fromkeys(exports)), list(dict.fromkeys(imports))


def inventory_probes(rel_paths: list[str]) -> tuple[list[str], list[str]]:
    """Return the files and directories an Ansible inventory is kept in.

    INI and extensionless inventories are never indexed, so these are looked
    up on disk beside every indexed directory.
    """
    directories = sorted({posixpath.dirname(rel_path) for rel_path in rel_paths})
    directories = directories if "" in directories else ["", *directories]
    files = [
        posixpath.join(one, name) for one in directories for name in INVENTORY_NAMES
    ]
    folders = [
        posixpath.join(one, name) for one in directories for name in INVENTORY_DIRS
    ]
    return files, folders


def configured_inventories(rel_path: str, content: str) -> list[str]:
    """Return the tree paths the `inventory` of an ansible.cfg points at."""
    parser = configparser.ConfigParser(interpolation=None)
    try:
        parser.read_string(content)
    except configparser.Error:
        return []
    raw = parser.get("defaults", "inventory", fallback="")
    directory = posixpath.dirname(rel_path)
    found: list[str] = []
    for entry in (one.strip() for one in raw.split(",")):
        if entry and not entry.startswith(("/", "~", "$")):
            joined = posixpath.normpath(posixpath.join(directory, entry))
            if not joined.startswith(".."):
                found.append(joined)
    return found


def _expand_ranges(host: str) -> list[str]:
    match = HOST_RANGE.search(host)
    if match is None:
        return [host]
    start, end = match.group(1), match.group(2)
    if start.isdigit() and end.isdigit():
        width = len(start) if start.startswith("0") else 0
        steps = [str(one).zfill(width) for one in range(int(start), int(end) + 1)]
    elif start.isalpha() and end.isalpha():
        steps = [chr(one) for one in range(ord(start), ord(end) + 1)]
    else:
        return []
    head, tail = host[: match.start()], host[match.end() :]
    expanded = [
        one for step in steps[:MAX_RANGE] for one in _expand_ranges(head + step + tail)
    ]
    return expanded[:MAX_RANGE]


def _ini_inventory(content: str) -> list[str]:
    hosts: list[str] = []
    listing = True
    for raw in content.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line or line.startswith(";"):
            continue
        section = INI_SECTION.match(line)
        if section:
            listing = not section.group(1).endswith((":vars", ":children"))
            continue
        token = line.split()[0]
        if listing and "=" not in token:
            hosts.extend(_expand_ranges(token))
    return hosts


def _yaml_inventory(document: Any) -> list[str]:  # noqa: ANN401
    hosts: list[str] = []
    stack = [document]
    while stack:
        node = stack.pop()
        if not isinstance(node, dict):
            continue
        for key, value in node.items():
            if key == "hosts" and isinstance(value, dict):
                for host in value:
                    hosts.extend(_expand_ranges(str(host)))
            elif key != "vars" and isinstance(value, dict):
                stack.append(value)
    return hosts


def inventory_hosts(rel_path: str, content: str) -> list[Import]:
    """Return the hosts an Ansible inventory, INI or YAML, works on."""
    if any(INI_SECTION.match(line.strip()) for line in content.splitlines()):
        raws = _ini_inventory(content)
    else:
        documents = load_yaml_documents(content) or []
        document = documents[0] if documents else None
        raws = (
            _yaml_inventory(document)
            if isinstance(document, dict)
            else _ini_inventory(content)
        )
    return list(
        dict.fromkeys(
            Import("host", host, rel_path, USES_HOST) for host in _hosts(raws)
        )
    )


def embedded_compose_images(content: str) -> list[str]:
    """Return the images of every compose document held in a data string."""
    stack: list[Any] = list(load_yaml_documents(content) or [])
    images: list[str] = []
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            stack.extend(node.values())
        elif isinstance(node, list):
            stack.extend(node)
        elif isinstance(node, str) and COMPOSE_SERVICES.search(node):
            for document in load_yaml_documents(node) or []:
                services = (
                    document.get("services") if isinstance(document, dict) else None
                )
                for service in services.values() if isinstance(services, dict) else []:
                    image = service.get("image") if isinstance(service, dict) else None
                    if isinstance(image, str):
                        images.append(image)
    return images


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


def _shell_expand(value: str, variables: dict[str, str], depth: int = 5) -> str:
    def replace(match: re.Match[str]) -> str:
        name = match.group(1) or match.group(3)
        if name is None:
            return UNKNOWN
        known = variables.get(name)
        if known is None:
            known = match.group(2)
        if known is None or depth == 0:
            return UNKNOWN
        return _shell_expand(known, variables, depth - 1)

    # Whatever is still a reference is a command or a split substitution.
    return SHELL_LEFTOVER.sub(UNKNOWN, SHELL_REFERENCE.sub(replace, value))


def _named_tools(content: str, variables: dict[str, str], shell: bool) -> str:
    """Write `docker` for a variable holding it, `$(DOCKER) build` and the like."""
    for name, value in variables.items():
        if posixpath.basename(value.strip("'\" ")) in IMAGE_TOOLS:
            bare = rf"|\${name}\b" if shell else ""
            pattern = rf"\$(?:\({name}\)|\{{{name}\}}{bare})"
            content = re.sub(pattern, "docker", content)
    return content


def _shell_variables(content: str) -> dict[str, str]:
    variables: dict[str, str] = {}
    for name, raw in SHELL_VARIABLE.findall(content):
        variables[name] = raw[1:-1] if raw[:1] in "'\"" and len(raw) > 1 else raw
    return variables


def is_shell_script(rel_path: str, content: str) -> bool:
    """Report whether a file is a shell script, by extension or by shebang."""
    if rel_path.endswith(SHELL_EXTENSIONS):
        return True
    base = posixpath.basename(rel_path)
    return "." not in base and bool(SHELL_SHEBANG.match(content))


def _positionals(line: str, verb: str) -> list[str]:
    tokens = line.split()
    found: list[str] = []
    for token in tokens[tokens.index(verb) + 1 :] if verb in tokens else []:
        if token in COMMAND_SEPARATORS:
            break
        if not token.startswith("-"):
            found.append(token.strip("'\""))
    return found


def _image_builds(
    rel_path: str, content: str, expand: Callable[[str], str]
) -> list[tuple[str, str]]:
    """Return (image, build context) for every image a script or Makefile makes.

    Built, retagged or pushed, an image is provided by the directory it is built
    from; one that is only the local name a retag starts from is not.
    """
    joined = content.replace("\\\n", " ")
    directory = posixpath.dirname(rel_path)
    named: list[tuple[str, str]] = []
    contexts: list[str] = []
    for line in IMAGE_BUILD.findall(joined):
        context = _build_context(line)
        built_from = parent_of(rel_path)
        if context is not None:
            expanded = expand(context.strip("'\""))
            joined_path = posixpath.normpath(posixpath.join(directory, expanded))
            if (
                UNKNOWN not in expanded
                and "$" not in expanded
                and not joined_path.startswith("..")
            ):
                built_from = "./" if joined_path == "." else f"{joined_path}/"
        contexts.append(built_from)
        named.extend((raw, built_from) for raw in IMAGE_TAG.findall(line))
    built_from = contexts[0] if len(set(contexts)) == 1 else parent_of(rel_path)
    sources: set[str] = set()
    targets: set[str] = set()
    for line in IMAGE_RETAG.findall(joined):
        names = _positionals(line, "tag")
        if len(names) >= 2:
            sources.add(normalize("image", expand(names[0])))
            targets.add(normalize("image", expand(names[1])))
            named.append((names[1], built_from))
    for line in IMAGE_PUSH.findall(joined):
        named.extend((raw, built_from) for raw in _positionals(line, "push")[:1])
    images: list[tuple[str, str]] = []
    for raw, context in named:
        # A tag computed at run time is dropped with the tag itself.
        name = normalize("image", expand(raw))
        # A bare name is a local one or an official image, never this tree's.
        if "/" in name and UNKNOWN not in name and name not in sources - targets:
            images.append((name, context))
    return list(dict.fromkeys(images))


def _make_images(rel_path: str, content: str) -> list[tuple[str, str]]:
    """Return (image, build context) for what a Makefile builds."""
    variables = dict(MAKE_VARIABLE.findall(content.replace("\\\n", " ")))
    content = _named_tools(content, variables, shell=False)
    return _image_builds(rel_path, content, lambda raw: _expand(raw, variables))


def _shell_images(rel_path: str, content: str) -> list[tuple[str, str]]:
    """Return (image, build context) for what a shell script builds."""
    variables = _shell_variables(content.replace("\\\n", " "))
    content = _named_tools(content, variables, shell=True)
    return _image_builds(rel_path, content, lambda raw: _shell_expand(raw, variables))


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
    if is_shell_script(rel_path, content):
        images = _shell_images(rel_path, content)
        return [Export("image", name, built_from) for name, built_from in images], []
    if base.endswith(TERRAFORM_EXTENSIONS):
        return terraform_hosts(rel_path, content)
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
    if edges:
        for path in sorted(contents):
            if path.endswith(puppetdata.DATA_EXTENSIONS):
                imports.extend(compose_imports(path, contents[path]))
    return edges, imports


def _bucket_imports(rel_path: str, content: str) -> list[Import]:
    """Return the buckets a data file names under a `bucket` key, at any depth."""
    found: list[Import] = []
    stack: list[Any] = list(load_yaml_documents(content) or [])
    while stack:
        node = stack.pop()
        if isinstance(node, list):
            stack.extend(node)
        elif isinstance(node, dict):
            for key, value in node.items():
                if key == "bucket" and isinstance(value, str):
                    name = normalize("bucket", value)
                    if name:
                        found.append(Import("bucket", name, rel_path, USES_BUCKET))
                else:
                    stack.append(value)
    return found


def compose_imports(rel_path: str, content: str) -> list[Import]:
    """Return the images of the compose documents a data file holds."""
    return [
        Import("image", name, rel_path, USES_IMAGE)
        for name in (
            normalize("image", raw) for raw in embedded_compose_images(content)
        )
        if name
    ]


def workspace_links(
    rel_paths: list[str], contents: dict[str, str]
) -> tuple[list[puppetdata.DataEdge], list[Export], list[Import]]:
    """Return the data edges of every workspace, and what they provide and take."""
    edges: list[puppetdata.DataEdge] = []
    exports: list[Export] = []
    imports: list[Import] = []
    for space in workspacedata.workspaces(contents):
        followed = workspacedata.follow(space, rel_paths, contents)
        edges.extend(followed.edges)
        exports.extend(
            Export(kind, name, node_id)
            for kind, raw, node_id in followed.provided
            if (name := normalize(kind, raw))
        )
        imports.extend(
            Import(kind, name, source_id, relation)
            for kind, raw, source_id, relation in followed.taken
            if (name := normalize(kind, raw))
        )
        for path in followed.data_files:
            imports.extend(compose_imports(path, contents[path]))
            imports.extend(_bucket_imports(path, contents[path]))
    return edges, exports, imports


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
    circuits = set(circuit_configs(rel_paths))
    for rel_path in sorted(manifests):
        if rel_path.endswith(YAML_EXTENSIONS):
            exports.extend(host_exports(rel_path, manifests[rel_path]))
        if rel_path in circuits:
            exports.extend(circuit_exports(rel_path, manifests[rel_path]))
            continue
        provided, taken = read_manifest(rel_path, manifests[rel_path], indexed)
        exports.extend(provided)
        imports.extend(taken)
    return unique_exports(exports), sorted(set(imports), key=lambda i: (i.kind, i.name))


def unique_exports(exports: list[Export]) -> list[Export]:
    """Return each name once: the shallowest directory wins a name given twice."""
    ordered = sorted(
        exports, key=lambda export: (depth_of(export.node_id), export.node_id)
    )
    unique: dict[tuple[str, str], Export] = {}
    for export in ordered:
        unique.setdefault((export.kind, export.name), export)
    return list(unique.values())
