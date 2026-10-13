"""Read a systemd unit: what it needs, what it orders against, what starts it.

A unit file is one node, `unit.<file name>`, and every unit it names becomes
an edge leaving that node to `unit.<name>`, so a unit the tree holds resolves
to its file and a stock one such as `network-online.target` stays external.
No grammar for unit files is published, so they are read line by line.
"""

from __future__ import annotations

import posixpath
import re

from enggraph.indexer.parsers.base import TextParser, qualified

UNIT_EXTENSIONS = (
    ".automount",
    ".mount",
    ".path",
    ".service",
    ".slice",
    ".socket",
    ".swap",
    ".target",
    ".timer",
)
UNIT_DEPENDENCIES = {
    "Unit": {
        "Requires": "requires",
        "BindsTo": "requires",
        "Requisite": "requires",
        "PartOf": "requires",
        "Wants": "wants",
        "Upholds": "wants",
        "After": "after",
        "Before": "before",
        "Conflicts": "conflicts",
    },
    "Install": {"WantedBy": "wanted_by", "RequiredBy": "required_by"},
}
# The section of a unit that starts another one, and the service it starts when
# it names none.
ACTIVATING_SECTIONS = {".timer": "Timer", ".socket": "Socket", ".path": "Path"}
UNIT_INSTANCE = re.compile(r"@[^.@]*\.")
COMMENT_PREFIXES = ("#", ";")
SECTION_HEADER = re.compile(r"^\[(?P<name>[^\]]+)\]$")
ACCEPT_TRUE = frozenset({"yes", "true", "1", "on"})


def is_unit_name(file_path: str) -> bool:
    """Report whether a path names a systemd unit file."""
    return file_path.lower().endswith(UNIT_EXTENSIONS)


def read_unit(content: str) -> dict[str, list[tuple[str, str]]]:
    """Return the (key, value) assignments of a unit, by section."""
    sections: dict[str, list[tuple[str, str]]] = {}
    current: list[tuple[str, str]] | None = None
    pending = ""
    for raw in content.splitlines():
        line = pending + raw.strip()
        pending = ""
        if line.endswith("\\"):
            pending = line[:-1] + " "
            continue
        if not line or line.startswith(COMMENT_PREFIXES):
            continue
        header = SECTION_HEADER.match(line)
        if header:
            current = sections.setdefault(header.group("name"), [])
        elif current is not None and "=" in line:
            key, value = line.split("=", 1)
            current.append((key.strip(), value.strip()))
    return sections


def unit_target(name: str) -> str:
    """Return the node an instance or a template names, or "" for none.

    `alpha@one.service` is read from the file `alpha@.service`; a name still
    holding a specifier after that cannot be resolved here.
    """
    template = UNIT_INSTANCE.sub("@.", name)
    return "" if "%" in template else qualified("unit", template)


def values(assignments: list[tuple[str, str]], key: str) -> list[str]:
    """Return every space separated value given to a key."""
    return [
        part for name, value in assignments if name == key for part in value.split()
    ]


class SystemdParser(TextParser):
    """systemd unit parser: one node per unit, its dependencies as edges."""

    FAMILY = "systemd"

    def get_entities(self, content: str, rel_path: str) -> list[dict[str, str]]:
        """Return the unit a file declares."""
        name = qualified("unit", posixpath.basename(rel_path))
        return [{"name": name, "type": "unit"}]

    def get_relations(self, content: str, rel_path: str) -> list[dict[str, str]]:
        """Return the units a unit depends on, orders against and starts."""
        file_name = posixpath.basename(rel_path)
        source = qualified("unit", file_name)
        sections = read_unit(content)
        pairs: list[tuple[str, str]] = []
        for section, keys in UNIT_DEPENDENCIES.items():
            assignments = sections.get(section, [])
            for key, relation in keys.items():
                pairs.extend((name, relation) for name in values(assignments, key))
        activated = self._activated(file_name, sections)
        pairs.extend((name, "activates") for name in activated)
        relations = [
            {"source": source, "target": target, "type": relation, "scope": "symbol"}
            for target, relation in (
                (unit_target(name), relation) for name, relation in pairs
            )
            if target and target != source
        ]
        for name in values(sections.get("Service", []), "EnvironmentFile"):
            path = name.removeprefix("-")
            # An absolute path is on the host the unit runs on, not in the tree.
            if path and not path.startswith("/") and "%" not in path:
                relations.append(
                    {
                        "source": source,
                        "target": path,
                        "type": "reads_vars",
                        "scope": "file",
                    }
                )
        return self._deduplicate(relations)

    @staticmethod
    def _activated(
        file_name: str, sections: dict[str, list[tuple[str, str]]]
    ) -> list[str]:
        """Return the unit a timer, socket or path unit starts."""
        stem, extension = posixpath.splitext(file_name)
        section = ACTIVATING_SECTIONS.get(extension)
        if section is None:
            return []
        assignments = sections.get(section, [])
        named = values(assignments, "Unit")
        if named:
            return named
        accept = values(assignments, "Accept")
        accepting = extension == ".socket" and any(
            value.lower() in ACCEPT_TRUE for value in accept
        )
        return [f"{stem}@.service" if accepting else f"{stem}.service"]

    @staticmethod
    def _deduplicate(relations: list[dict[str, str]]) -> list[dict[str, str]]:
        """Drop the relations given twice, keeping the first of each."""
        seen: set[tuple[str, str, str]] = set()
        unique: list[dict[str, str]] = []
        for relation in relations:
            key = (relation["source"], relation["target"], relation["type"])
            if key not in seen:
                seen.add(key)
                unique.append(relation)
        return unique
