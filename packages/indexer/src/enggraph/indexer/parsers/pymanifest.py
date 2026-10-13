"""Read the Python manifests that are not pyproject.toml.

A requirements file is read through its grammar and a `setup.cfg` through
configparser. Every package either one asks for becomes a `depends_on` edge
from the file to a `pypi:` placeholder - the same name the project links of
the manifest are matched on - and a requirements file it pulls in becomes an
`includes` edge.
"""

from __future__ import annotations

import configparser
import posixpath
import re

import tree_sitter_requirements
from tree_sitter import Node

from enggraph.indexer.parsers.base import (
    CodeParser,
    TextParser,
    node_text,
    qualified,
    unique_pairs,
)

REQUIREMENTS_NAME = re.compile(
    r"^(?:requirements|constraints)(?:[-_.][\w.-]*)?\.(?:txt|in)$"
)
REQUIREMENTS_DIR = "requirements"
REQUIREMENTS_EXTENSIONS = (".txt", ".in")
INCLUDE_OPTIONS = frozenset({"-r", "--requirement", "-c", "--constraint"})
SETUP_CFG = "setup.cfg"
SETUP_REQUIRES_KEYS = ("install_requires", "setup_requires", "tests_require")
ENTRY_POINT_GROUPS = ("console_scripts", "gui_scripts")
FILE_DIRECTIVE = "file:"
PEP508_NAME = re.compile(r"^\s*([A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?)")
PEP503_RUN = re.compile(r"[-_.]+")
PYPI_PREFIX = "pypi:"


def is_requirements_name(file_path: str) -> bool:
    """Report whether a path names a pip requirements or constraints file."""
    name = posixpath.basename(file_path).lower()
    if REQUIREMENTS_NAME.match(name):
        return True
    parent = posixpath.basename(posixpath.dirname(file_path)).lower()
    return parent == REQUIREMENTS_DIR and name.endswith(REQUIREMENTS_EXTENSIONS)


def pypi_target(requirement: str) -> str:
    """Return the placeholder of a PEP 508 requirement, or "" for no name."""
    match = PEP508_NAME.match(requirement)
    if not match:
        return ""
    return PYPI_PREFIX + PEP503_RUN.sub("-", match.group(1)).lower()


def as_relations(pairs: list[tuple[str, str]]) -> list[dict[str, str]]:
    """Turn (target, type) pairs into relations, an include being a file."""
    return [
        {
            "target": entry["name"],
            "type": entry["type"],
            "scope": "file" if entry["type"] == "includes" else "symbol",
        }
        for entry in unique_pairs(iter(pairs))
    ]


def child_of_type(node: Node, node_type: str) -> Node | None:
    """Return the first named child of a node with the given type."""
    for child in node.named_children:
        if child.type == node_type:
            return child
    return None


class RequirementsParser(CodeParser):
    """pip requirements file parser: dependencies and included files."""

    FAMILY = "pymanifest"

    def __init__(self) -> None:
        """Initialize the requirements parser."""
        super().__init__(tree_sitter_requirements.language())

    def get_relations(self, content: str, rel_path: str) -> list[dict[str, str]]:
        """Return the packages a requirements file names and the files it reads."""
        pairs: list[tuple[str, str]] = []
        for node in self.parse(content).named_children:
            if node.type == "requirement":
                package = child_of_type(node, "package")
                target = pypi_target(node_text(package)) if package else ""
                if target:
                    pairs.append((target, "depends_on"))
            elif node.type == "global_opt":
                option = child_of_type(node, "option")
                path = child_of_type(node, "path")
                if option and path and node_text(option) in INCLUDE_OPTIONS:
                    pairs.append((node_text(path).strip(), "includes"))
        return as_relations(pairs)


def load_setup_cfg(content: str) -> configparser.ConfigParser | None:
    """Return a parsed setup.cfg, or None when it does not parse."""
    parser = configparser.ConfigParser(interpolation=None, strict=False)
    try:
        parser.read_string(content)
    except configparser.Error:
        return None
    return parser


def listed(value: str) -> list[str]:
    """Return the entries of a dangling list value, comments dropped."""
    entries = (line.split("#", 1)[0].strip() for line in value.splitlines())
    return [entry for entry in entries if entry]


def requirement_pairs(value: str) -> list[tuple[str, str]]:
    """Return the pairs one requirements value holds, inline or by file."""
    stripped = value.strip()
    if stripped.startswith(FILE_DIRECTIVE):
        files = stripped[len(FILE_DIRECTIVE) :].replace(",", " ").split()
        return [(path, "includes") for path in files]
    targets = (pypi_target(entry) for entry in listed(value))
    return [(target, "depends_on") for target in targets if target]


class SetupCfgParser(TextParser):
    """setup.cfg parser: sections, scripts and requirements."""

    FAMILY = "pymanifest"

    def get_entities(self, content: str, rel_path: str) -> list[dict[str, str]]:
        """Return the sections of a setup.cfg and the scripts it installs."""
        parser = load_setup_cfg(content)
        if parser is None:
            return []
        pairs = [(qualified("section", name), "section") for name in parser.sections()]
        for group in ENTRY_POINT_GROUPS:
            value = parser.get("options.entry_points", group, fallback="")
            for entry in listed(value):
                script = entry.split("=", 1)[0].strip()
                pairs.append((qualified("entry_point", script), "entry_point"))
        return unique_pairs(iter(pairs))

    def get_relations(self, content: str, rel_path: str) -> list[dict[str, str]]:
        """Return the packages a setup.cfg requires and the files it reads."""
        parser = load_setup_cfg(content)
        if parser is None:
            return []
        pairs: list[tuple[str, str]] = []
        for key in SETUP_REQUIRES_KEYS:
            pairs.extend(requirement_pairs(parser.get("options", key, fallback="")))
        if parser.has_section("options.extras_require"):
            for _, value in parser.items("options.extras_require"):
                pairs.extend(requirement_pairs(value))
        return as_relations(pairs)
