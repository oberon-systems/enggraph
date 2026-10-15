"""Turn what a file refers to into the id of a node in the graph.

Nothing here touches the database: a reference resolves against the set of
indexed paths and the table of declared symbols, both handed in by the caller.
"""

from __future__ import annotations

import posixpath
import re
import tomllib
from collections.abc import Callable
from typing import NamedTuple

from enggraph.core.config import (
    MAX_NODE_ID_LENGTH,
    MODULE_EXTENSIONS,
    REWRITABLE_IMPORT_EXTENSIONS,
)
from enggraph.core.identifiers import entity_node_id, owner_path, truncate
from enggraph.indexer.parsers import language_family, strip_literal
from enggraph.indexer.parsers.ansible import role_root
from enggraph.indexer.parsers.terraform import (
    DEPENDS_ON,
    MODULE_PREFIX,
    PARENT_PREFIX,
    TERRAGRUNT_CONFIG,
    USES_MODULE,
)
from enggraph.indexer.parsers.workspace import (
    WORKSPACE_PLACEHOLDERS,
    workspace_candidates,
)

# Where each edge looks inside the owning role when the target is a bare name.
ANSIBLE_RELATION_DIRS = {
    "includes": ("tasks",),
    "reads_vars": ("vars", "defaults"),
    "uses_template": ("templates",),
    "uses_file": ("files",),
}
# Edges whose target is a role rather than a file below the role.
ANSIBLE_ROLE_RELATIONS = frozenset({"uses_role", "depends_on"})
# The files a role name resolves to, in the order they are tried.
ANSIBLE_ROLE_ENTRY_POINTS = (
    "tasks/main.yml",
    "tasks/main.yaml",
    "meta/main.yml",
    "meta/main.yaml",
)
# The two fixed directories of a Puppet module, which is what makes a template
# reference resolvable from the path of the manifest naming it.
PUPPET_MANIFEST_DIR = "/manifests/"
PUPPET_TEMPLATE_DIR = "templates"
# Puppet is the only other producer of `uses_template`, and Ansible already
# claims that name in ANSIBLE_RELATION_DIRS, so the two are told apart by the
# file the edge leaves rather than by the relation alone.
PUPPET_SOURCE_EXTENSIONS = (".pp",)
# Pages, whose references are paths rather than module names, and whose
# site root is the project root.
HTML_SOURCE_EXTENSIONS = (".html", ".htm", ".phtml")
HCL_SOURCE_EXTENSIONS = (".tf", ".hcl", ".tfvars")
# The files a module or a Terragrunt unit directory resolves to, in order. Any
# other configuration file of the directory follows them.
HCL_DIRECTORY_ENTRY_POINTS = {
    USES_MODULE: ("main.tf", TERRAGRUNT_CONFIG, "versions.tf", "variables.tf"),
    DEPENDS_ON: (TERRAGRUNT_CONFIG, "main.tf"),
}


def python_import_candidates(target: str, base_dir: str) -> list[str]:
    """Return the file paths a Python import may refer to."""
    stripped = target.lstrip(".")
    dots = len(target) - len(stripped)
    if dots:
        parts = [part for part in base_dir.split("/") if part]
        # One dot is the current package, each further dot climbs one level.
        climb = dots - 1
        if climb > len(parts):
            return []
        parts = parts[: len(parts) - climb] if climb else parts
        tail = stripped.split(".") if stripped else []
        prefix = "/".join([*parts, *tail])
    else:
        prefix = target.replace(".", "/")
    if not prefix:
        return []
    return [f"{prefix}.py", f"{prefix}/__init__.py"]


def path_import_candidates(target: str, base_dir: str) -> list[str]:
    """Return the file paths a relative path-like import may refer to."""
    if not target.startswith("."):
        # Bare specifiers name a package, not a file in this project.
        return []
    joined = posixpath.normpath(posixpath.join(base_dir, target))
    if joined.startswith(".."):
        return []
    joined = joined.lstrip("./")
    if not joined:
        return []
    bases = [joined]
    for extension in REWRITABLE_IMPORT_EXTENSIONS:
        if joined.endswith(extension):
            bases.append(joined[: -len(extension)])
    candidates = list(bases)
    for base in bases:
        candidates.extend(f"{base}{extension}" for extension in MODULE_EXTENSIONS)
        candidates.extend(f"{base}/index{extension}" for extension in MODULE_EXTENSIONS)
    return candidates


def suffix_index(known_files: set[str]) -> dict[str, list[str]]:
    """Index the Python files by every trailing run of their path segments."""
    index: dict[str, list[str]] = {}
    for path in known_files:
        if not path.endswith(".py"):
            continue
        parts = path.split("/")
        for start in range(1, len(parts)):
            index.setdefault("/".join(parts[start:]), []).append(path)
    return index


GO_EXTENSION = ".go"
GO_TEST_SUFFIX = "_test.go"
GO_MOD = "go.mod"
GO_MODULE = re.compile(r"^module\s+(\S+)", re.M)


def is_go(rel_path: str) -> bool:
    """Say whether a file is Go source."""
    return rel_path.endswith(GO_EXTENSION)


def go_module_path(content: str) -> str:
    """Return the module path a go.mod declares, empty when it declares none."""
    match = GO_MODULE.search(content)
    return strip_literal(match.group(1)) if match else ""


def manifest_candidates(
    rel_paths: list[str], accept: Callable[[str], bool], manifest: str
) -> list[str]:
    """Return every manifest path at or above a directory holding such files."""
    directories: set[str] = set()
    for rel_path in rel_paths:
        if not accept(rel_path):
            continue
        directory = posixpath.dirname(rel_path)
        while directory not in directories:
            directories.add(directory)
            if not directory:
                break
            directory = posixpath.dirname(directory)
    return sorted(posixpath.join(directory, manifest) for directory in directories)


def go_mod_candidates(rel_paths: list[str]) -> list[str]:
    """Return every go.mod path at or above a directory holding Go files."""
    return manifest_candidates(rel_paths, is_go, GO_MOD)


def go_packages(known_files: set[str]) -> dict[str, list[str]]:
    """Return the Go files of each directory, test files apart."""
    packages: dict[str, list[str]] = {}
    for rel_path in sorted(known_files):
        if is_go(rel_path):
            packages.setdefault(posixpath.dirname(rel_path), []).append(rel_path)
    return packages


def go_visible(rel_path: str, packages: dict[str, list[str]]) -> set[str]:
    """Return the files whose symbols a Go file sees without an import.

    A test file sees its whole package; the package does not see its tests.
    """
    test = rel_path.endswith(GO_TEST_SUFFIX)
    return {
        one
        for one in packages.get(posixpath.dirname(rel_path), [])
        if test or not one.endswith(GO_TEST_SUFFIX)
    } | {rel_path}


def go_package_dir(target: str, modules: dict[str, str]) -> str | None:
    """Resolve an import path to a directory by the longest module holding it.

    `modules` maps a module path to the directory of its go.mod.
    """
    held = [one for one in modules if target == one or target.startswith(f"{one}/")]
    if not held:
        return None
    best = max(held, key=len)
    return posixpath.join(modules[best], target[len(best) :].lstrip("/")).rstrip("/")


def resolve_go_import(
    target: str, modules: dict[str, str], packages: dict[str, list[str]]
) -> list[str]:
    """Return the non-test files of the package an import names, if indexed."""
    directory = go_package_dir(target, modules)
    if directory is None:
        return []
    return [
        truncate(one, MAX_NODE_ID_LENGTH)
        for one in packages.get(directory, [])
        if not one.endswith(GO_TEST_SUFFIX)
    ]


RUST_EXTENSION = ".rs"
CARGO_TOML = "Cargo.toml"
RUST_ROOT_FILES = frozenset({"lib.rs", "main.rs"})
RUST_MOD_FILE = "mod.rs"
# Directories of a crate whose every file is the root of a crate of its own.
RUST_TARGET_DIRS = ("src/bin", "tests", "examples", "benches")
RUST_ANCHORS = frozenset({"crate", "self", "super"})


class RustCrate(NamedTuple):
    """A package a Cargo.toml declares: its directory, name and library root."""

    directory: str
    name: str
    library: str


def is_rust(rel_path: str) -> bool:
    """Say whether a file is Rust source."""
    return rel_path.endswith(RUST_EXTENSION)


def cargo_crate(rel_path: str, content: str) -> RustCrate | None:
    """Read the package a Cargo.toml declares, None for a workspace alone.

    The name is the one `use` spells: the library's own, or the package's
    with dashes as underscores.
    """
    try:
        data = tomllib.loads(content)
    except tomllib.TOMLDecodeError:
        return None
    package = data.get("package")
    library = data.get("lib")
    library = library if isinstance(library, dict) else {}
    if not isinstance(package, dict) or not isinstance(package.get("name"), str):
        return None
    directory = posixpath.dirname(rel_path)
    name = library.get("name")
    name = name if isinstance(name, str) else package["name"]
    path = library.get("path")
    path = path if isinstance(path, str) else "src/lib.rs"
    return RustCrate(
        directory,
        name.replace("-", "_"),
        posixpath.normpath(posixpath.join(directory, path)),
    )


class RustLayout:
    """The module tree the Rust files of a project form, by their paths.

    A crate root and a `mod.rs` hold their child modules beside them; any
    other `a.rs` holds them under `a/`.
    """

    def __init__(self, known_files: set[str], crates: list[RustCrate]) -> None:
        """Index the Rust files, the crate roots and the library names."""
        self.files = {one for one in known_files if is_rust(one)}
        self.target_dirs = {
            posixpath.join(crate.directory, one)
            for crate in crates
            for one in RUST_TARGET_DIRS
        }
        self.build_files = {
            posixpath.join(crate.directory, "build.rs") for crate in crates
        }
        self.libraries = {
            crate.name: crate.library for crate in crates if crate.library in self.files
        }
        self.library_roots = set(self.libraries.values())

    def is_root(self, rel_path: str) -> bool:
        """Say whether a file is the root of a crate."""
        return (
            posixpath.basename(rel_path) in RUST_ROOT_FILES
            or posixpath.dirname(rel_path) in self.target_dirs
            or rel_path in self.build_files
            or rel_path in self.library_roots
        )

    def module_dir(self, rel_path: str) -> str:
        """Return the directory a file's child modules live in."""
        if self.is_root(rel_path) or posixpath.basename(rel_path) == RUST_MOD_FILE:
            return posixpath.dirname(rel_path)
        return posixpath.splitext(rel_path)[0]

    def child(self, rel_path: str, name: str) -> str | None:
        """Return the file of a child module, if indexed."""
        directory = self.module_dir(rel_path)
        for candidate in (
            posixpath.join(directory, f"{name}{RUST_EXTENSION}"),
            posixpath.join(directory, name, RUST_MOD_FILE),
        ):
            if candidate in self.files:
                return candidate
        return None

    def parent(self, rel_path: str) -> str | None:
        """Return the file of the parent module, None for a crate root."""
        if self.is_root(rel_path):
            return None
        directory = posixpath.dirname(self.module_dir(rel_path))
        candidates = [
            posixpath.join(directory, one)
            for one in (RUST_MOD_FILE, *sorted(RUST_ROOT_FILES))
        ]
        if directory:
            candidates.insert(0, f"{directory}{RUST_EXTENSION}")
        return next((one for one in candidates if one in self.files), None)

    def root(self, rel_path: str) -> str:
        """Return the root of the crate a file belongs to."""
        current, seen = rel_path, {rel_path}
        while (above := self.parent(current)) is not None and above not in seen:
            seen.add(above)
            current = above
        return current

    def descend(self, rel_path: str, segments: list[str]) -> tuple[str, list[str]]:
        """Walk child modules down a path; what no file holds is items."""
        current = rel_path
        for index, segment in enumerate(segments):
            below = self.child(current, segment)
            if below is None:
                return current, segments[index:]
            current = below
        return current, []

    def resolve(
        self, rel_path: str, segments: list[str]
    ) -> tuple[str | None, list[str]]:
        """Resolve a path to the deepest module file it names and the rest.

        A bare first segment is a child module, then a crate of the tree,
        then a module of the crate root as Rust 2015 reads it. None is a path
        outside the tree.
        """
        if not segments:
            return None, []
        head, rest = segments[0], segments[1:]
        if head == "crate":
            return self.descend(self.root(rel_path), rest)
        if head == "self":
            return self.descend(rel_path, rest)
        if head == "super":
            current: str | None = rel_path
            while rest[:1] == ["super"] and current is not None:
                current, rest = self.parent(current), rest[1:]
            current = self.parent(current) if current is not None else None
            return self.descend(current, rest) if current else (None, [])
        start = self.child(rel_path, head) or self.libraries.get(head)
        if start is None:
            start = self.child(self.root(rel_path), head)
        return self.descend(start, rest) if start else (None, [])


PHP_EXTENSION = ".php"


def is_php(rel_path: str) -> bool:
    """Say whether a file is PHP source the code extractor reads."""
    return rel_path.endswith(PHP_EXTENSION)


def shared_depth(one: str, two: str) -> int:
    """Count the leading directories two file paths share."""
    depth = 0
    for left, right in zip(
        posixpath.dirname(one).split("/"),
        posixpath.dirname(two).split("/"),
        strict=False,
    ):
        if not left or left != right:
            break
        depth += 1
    return depth


def nearest(rel_path: str, paths: list[str]) -> str | None:
    """Return the one path sharing the most directories with a file, if one does."""
    depths = {path: shared_depth(rel_path, path) for path in set(paths)}
    if not depths:
        return None
    best = max(depths.values())
    closest = [path for path, depth in depths.items() if depth == best]
    return closest[0] if len(closest) == 1 else None


def basename_index(known_files: set[str]) -> dict[str, list[str]]:
    """Index the files by their base name."""
    index: dict[str, list[str]] = {}
    for path in sorted(known_files):
        index.setdefault(posixpath.basename(path), []).append(path)
    return index


def resolve_php_include(
    target: str,
    rel_path: str,
    known_files: set[str],
    basenames: dict[str, list[str]],
) -> str | None:
    """Resolve what a PHP file includes to the id of an indexed file.

    The file's own directory is tried first. PHP also searches the include
    path and the working directory, which no tree records, so otherwise the
    file ending with the path that lies nearest to the includer is taken; an
    absolute path is shortened from the left until some file ends with it.
    """
    target = strip_literal(target)
    if not target:
        return None
    joined = posixpath.normpath(posixpath.join(posixpath.dirname(rel_path), target))
    if joined in known_files:
        return truncate(joined, MAX_NODE_ID_LENGTH)
    segments = [
        one
        for one in posixpath.normpath(target.lstrip("/")).split("/")
        if one not in ("", ".", "..")
    ]
    while segments:
        tail = "/".join(segments)
        matches = [
            path
            for path in basenames.get(segments[-1], [])
            if path == tail or path.endswith(f"/{tail}")
        ]
        if matches:
            found = nearest(rel_path, matches)
            return truncate(found, MAX_NODE_ID_LENGTH) if found else None
        if not target.startswith("/") or len(segments) == 1:
            return None
        segments.pop(0)
    return None


def resolve_import(
    target: str,
    rel_path: str,
    known_files: set[str],
    suffixes: dict[str, list[str]] | None = None,
) -> str | None:
    """Resolve an import target to the id of an indexed file node.

    With `suffixes`, an absolute Python import that names no file from the
    project root still resolves when exactly one file ends with it: the
    package sits under a source root such as `src/`.
    """
    target = strip_literal(target)
    if not target:
        return None
    base_dir = posixpath.dirname(rel_path)
    if rel_path.endswith(".py"):
        candidates = python_import_candidates(target, base_dir)
    else:
        candidates = path_import_candidates(target, base_dir)
    for candidate in candidates:
        if candidate in known_files:
            return truncate(candidate, MAX_NODE_ID_LENGTH)
    if suffixes is None or not rel_path.endswith(".py") or target.startswith("."):
        return None
    for candidate in candidates:
        matches = suffixes.get(candidate, [])
        if len(matches) == 1:
            return truncate(matches[0], MAX_NODE_ID_LENGTH)
    return None


def ansible_candidates(relation_type: str, target: str, rel_path: str) -> list[str]:
    """Return the file paths an Ansible reference may point at."""
    root = role_root(rel_path)
    base_dir = posixpath.dirname(rel_path)
    if relation_type in ANSIBLE_ROLE_RELATIONS:
        # A role sits next to the role that names it, or under a top level
        # roles directory.
        prefixes = ["roles", posixpath.join(base_dir, "roles")]
        if root:
            prefixes.insert(0, posixpath.dirname(root))
        return [
            posixpath.normpath(posixpath.join(prefix, target, entry_point))
            for prefix in prefixes
            for entry_point in ANSIBLE_ROLE_ENTRY_POINTS
        ]

    directories = [base_dir]
    if root:
        directories.extend(
            posixpath.join(root, name)
            for name in ANSIBLE_RELATION_DIRS.get(relation_type, ())
        )
    # The target may already be written from the project root.
    directories.append("")
    return [
        posixpath.normpath(posixpath.join(directory, target))
        for directory in directories
    ]


def puppet_candidates(target: str, rel_path: str) -> list[str]:
    """Return the file paths a Puppet template reference may point at.

    A manifest lives at `<modules>/<module>/manifests/<name>.pp` and names a
    template by the module it belongs to rather than by its path, so
    `template('profile/nginx.conf.erb')` means
    `<modules>/profile/templates/nginx.conf.erb`. The module in the reference
    is not necessarily the one holding the manifest, which is why only the
    directory the modules sit in is taken from the manifest path.
    """
    candidates: list[str] = []
    root, separator, _ = rel_path.partition(PUPPET_MANIFEST_DIR)
    module, _, tail = target.partition("/")
    if separator and tail:
        modules_dir = posixpath.dirname(root)
        candidates.append(
            posixpath.normpath(
                posixpath.join(modules_dir, module, PUPPET_TEMPLATE_DIR, tail)
            )
        )
    # The target may already be written from the project root.
    candidates.append(posixpath.normpath(target))
    return candidates


def html_candidates(target: str, rel_path: str) -> list[str]:
    """Return the file paths an asset reference of a page may point at.

    A page names an asset either relative to itself or from the site root, and
    the site root is the project root here - so a leading slash does not put
    the target outside the tree, it picks the second of the two candidates.
    """
    cleaned = target.split("?", 1)[0].split("#", 1)[0].lstrip("/")
    if not cleaned:
        return []
    base_dir = posixpath.dirname(rel_path)
    candidates = [
        posixpath.normpath(posixpath.join(base_dir, cleaned)),
        posixpath.normpath(cleaned),
    ]
    # A candidate still climbing out of the tree names no indexed file, and
    # keeping it would only spend a lookup on it.
    return [
        candidate
        for index, candidate in enumerate(candidates)
        if not candidate.startswith("..") and candidate not in candidates[:index]
    ]


def hcl_candidates(
    relation_type: str, target: str, rel_path: str, known_files: set[str]
) -> list[str]:
    """Return the files a Terraform or Terragrunt reference may point at."""
    if target.startswith(MODULE_PREFIX):
        return []
    if target.startswith(PARENT_PREFIX):
        name = target[len(PARENT_PREFIX) :]
        directory = posixpath.dirname(posixpath.dirname(rel_path))
        candidates = []
        while True:
            candidates.append(posixpath.join(directory, name))
            if not directory:
                return candidates
            directory = posixpath.dirname(directory)
    entry_points = HCL_DIRECTORY_ENTRY_POINTS.get(relation_type)
    if entry_points is None:
        return [target]
    prefix = f"{target}/" if target else ""
    others = sorted(
        path
        for path in known_files
        if path.startswith(prefix)
        and "/" not in path[len(prefix) :]
        and path.endswith(HCL_SOURCE_EXTENSIONS)
    )
    return [f"{prefix}{name}" for name in entry_points] + others


def resolve_file_target(
    relation_type: str, target: str, rel_path: str, known_files: set[str]
) -> str | None:
    """Resolve a file scoped relation to the id of an indexed file node."""
    if relation_type == "imports":
        return resolve_import(target, rel_path, known_files)
    if rel_path.endswith(HCL_SOURCE_EXTENSIONS):
        candidates = hcl_candidates(relation_type, target, rel_path, known_files)
    elif relation_type in WORKSPACE_PLACEHOLDERS:
        candidates = workspace_candidates(relation_type, target, rel_path)
    elif rel_path.endswith(PUPPET_SOURCE_EXTENSIONS):
        candidates = puppet_candidates(target, rel_path)
    elif rel_path.endswith(HTML_SOURCE_EXTENSIONS):
        candidates = html_candidates(target, rel_path)
    else:
        candidates = ansible_candidates(relation_type, target, rel_path)
    for candidate in candidates:
        if candidate in known_files:
            return truncate(candidate, MAX_NODE_ID_LENGTH)
    return None


def has_placeholder(target: str, rel_path: str) -> bool:
    """Report whether a target that resolved to nothing still gets a node.

    A Terraform path that resolves to nothing is a file the tree does not
    have, so it gets none; a remote module source does.
    """
    if rel_path.endswith(HCL_SOURCE_EXTENSIONS):
        return target.startswith(MODULE_PREFIX)
    return True


def placeholder_id(relation_type: str, target: str) -> str:
    """Return the node id standing for a target outside the tree."""
    if relation_type in WORKSPACE_PLACEHOLDERS:
        prefix = WORKSPACE_PLACEHOLDERS[relation_type]
    else:
        prefix = "role:" if relation_type in ANSIBLE_ROLE_RELATIONS else ""
    return truncate(f"{prefix}{target}", MAX_NODE_ID_LENGTH)


def resolve_symbol(
    name: str,
    rel_path: str,
    symbols: dict[str, list[str]],
    imported: set[str],
) -> str | None:
    """Resolve a called or inherited name to an entity node id.

    Preference order: the same file, then a file this one imports, then any
    other file of the same language. Names are matched within a language
    family only, otherwise a `helper()` call in TypeScript happily binds to a
    Rust `fn helper` that shares nothing but the spelling.
    """
    node_ids = symbols.get(name)
    if not node_ids:
        return None
    local = entity_node_id(rel_path, name)
    if local in node_ids:
        return local
    family = language_family(rel_path)
    candidates = [
        node_id
        for node_id in node_ids
        if language_family(owner_path(node_id)) == family
    ]
    if not candidates:
        return None
    for node_id in candidates:
        if owner_path(node_id) in imported:
            return node_id
    return candidates[0]
