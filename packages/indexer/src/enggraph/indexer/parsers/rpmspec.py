"""Read an RPM spec: the packages it builds and what they require.

No grammar for spec files is published, so the preamble is read line by line.
The main package and each `%package` become nodes; `Requires` and
`BuildRequires` become edges to `package:` placeholders, the same names the
project links of a spec are matched on, and a local source or patch becomes
an edge to the file.
"""

from __future__ import annotations

import posixpath
import re

from enggraph.indexer.parsers.base import TextParser, qualified, unique_pairs

SPEC_TAG = re.compile(
    r"^(?P<tag>[A-Za-z][A-Za-z0-9]*)(?:\([^)]*\))?\s*:\s*(?P<value>.*?)\s*$"
)
SPEC_DEFINE = re.compile(r"^%(?:define|global)\s+(\w+)\s+(.*?)\s*$")
SPEC_PACKAGE = re.compile(r"^%package\s+(?:(-n)\s+)?(\S+)")
SPEC_SECTION = re.compile(r"^%(\w+)")
SPEC_MACRO = re.compile(
    r"%\{(?P<flags>[!?]*)(?P<name>\w+)(?::(?P<body>[^{}]*))?\}"
    r"|%(?P<bare>[A-Za-z_]\w*)"
)
SPEC_SOURCE_TAG = re.compile(r"^(?:source|patch)\d*$", re.I)
# Sections holding scripts or text rather than tags: a `Requires:` line in a
# %description is prose.
SPEC_BODY_SECTIONS = frozenset(
    {
        "build",
        "changelog",
        "check",
        "clean",
        "conf",
        "description",
        "files",
        "generate_buildrequires",
        "install",
        "post",
        "posttrans",
        "postun",
        "pre",
        "prep",
        "pretrans",
        "preun",
        "triggerin",
        "triggerpostun",
        "triggerun",
        "verifyscript",
    }
)
SPEC_MACRO_TAGS = frozenset({"name", "version", "release", "epoch"})
SPEC_OPERATORS = frozenset({"<", ">", "=", "<=", ">=", "=="})
REQUIRES = {"requires": "depends_on", "buildrequires": "build_depends_on"}
PACKAGE_PREFIX = "package:"
MAX_EXPANSION_DEPTH = 5


def expand_macros(value: str, macros: dict[str, str]) -> str:
    """Expand the macros a spec defines, leaving the unknown ones in place."""

    def replace(match: re.Match[str]) -> str:
        bare = match.group("bare")
        if bare is not None:
            return macros.get(bare, match.group(0))
        name, flags, body = match.group("name", "flags", "body")
        if "?" not in flags:
            return macros.get(name, match.group(0))
        if (name in macros) == ("!" in flags):
            return ""
        return body if body is not None else macros.get(name, "")

    for _ in range(MAX_EXPANSION_DEPTH):
        expanded = SPEC_MACRO.sub(replace, value)
        if expanded == value:
            break
        value = expanded
    return value


def required_names(value: str) -> list[str]:
    """Return the package names a `Requires` value lists, without versions."""
    names: list[str] = []
    previous = ""
    for token in value.replace(",", " ").split():
        versioned = previous in SPEC_OPERATORS
        previous = token
        if versioned or token in SPEC_OPERATORS:
            continue
        # File dependencies and rich or virtual ones name no package.
        if token.startswith("/") or "(" in token or ")" in token:
            continue
        names.append(token)
    return names


def read_spec(content: str) -> tuple[list[str], list[tuple[str, str]]]:
    """Return the packages a spec builds and its (target, relation) pairs."""
    macros: dict[str, str] = {}
    packages: list[str] = []
    relations: list[tuple[str, str]] = []
    in_body = False
    for raw in content.splitlines():
        line = raw.strip()
        define = SPEC_DEFINE.match(line)
        if define:
            macros[define.group(1)] = expand_macros(define.group(2), macros)
            continue
        package = SPEC_PACKAGE.match(line)
        if package:
            in_body = False
            own = expand_macros(package.group(2), macros)
            main = macros.get("name")
            packages.append(own if package.group(1) or not main else f"{main}-{own}")
            continue
        section = SPEC_SECTION.match(line)
        if section and section.group(1) in SPEC_BODY_SECTIONS:
            in_body = True
            continue
        tag = SPEC_TAG.match(line)
        if in_body or not tag:
            continue
        name = tag.group("tag").lower()
        value = expand_macros(tag.group("value"), macros)
        if name in SPEC_MACRO_TAGS:
            macros[name] = value
            if name == "name":
                packages.insert(0, value)
        elif name in REQUIRES:
            relations.extend(
                (f"{PACKAGE_PREFIX}{required}", REQUIRES[name])
                for required in required_names(value)
            )
        elif SPEC_SOURCE_TAG.match(name) and "://" not in value:
            relations.append((posixpath.basename(value), "uses_file"))
    return packages, relations


class RpmSpecParser(TextParser):
    """RPM spec parser: packages as nodes, requirements and sources as edges."""

    FAMILY = "rpmspec"

    def get_entities(self, content: str, rel_path: str) -> list[dict[str, str]]:
        """Return the main package and the subpackages a spec builds."""
        packages, _ = read_spec(content)
        return unique_pairs(
            (qualified("package", name), "package")
            for name in packages
            if "%" not in name
        )

    def get_relations(self, content: str, rel_path: str) -> list[dict[str, str]]:
        """Return the packages a spec requires and the files it builds from."""
        _, relations = read_spec(content)
        return [
            {
                "target": entry["name"],
                "type": entry["type"],
                "scope": "file" if entry["type"] == "uses_file" else "symbol",
            }
            for entry in unique_pairs(
                (target, kind) for target, kind in relations if "%" not in target
            )
        ]
