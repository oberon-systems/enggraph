"""Link graphifyy's code across files, which the extractor itself never does.

graphifyy resolves a call only against the file it sits in and drops the rest,
and it names an import after a mangled module name rather than the file it
reaches. What it drops cannot be read back from its output, so the files are
parsed again here with our own grammars and resolved against its node table:
an import to the file it names, a call to the one symbol of that name the
imported files declare. Nothing here touches the database.
"""

from __future__ import annotations

from typing import NamedTuple

from enggraph.config import MAX_NODE_ID_LENGTH
from enggraph.identifiers import truncate
from enggraph.interop import node_id, node_type
from enggraph.parsers import get_parser
from enggraph.resolution import placeholder_id, resolve_import, suffix_index

IMPORT_RELATIONS = frozenset({"imports", "imports_from"})
CALLABLE_TYPES = frozenset({"function", "method", "class"})
# A call through one of these, or a bare one, to a name the file declares is
# the extractor's own local edge; any other receiver may be an imported type.
SELF_RECEIVERS = frozenset({"", "this", "self", "cls"})
CALL_CONFIDENCE = "INFERRED"
CALL_WEIGHT = 0.8
# Tagged beside the extractor's producer tag, which is what lets the next run
# clear these edges together with everything else it extracted.
RESOLVER = "cross_file"


class CrossLink(NamedTuple):
    """One edge the extractor could not write itself."""

    source_id: str
    target_id: str
    relation: str
    external: bool
    confidence: str
    weight: float


def is_linkable(rel_path: str) -> bool:
    """Say whether our own grammar can re-read a file for its call sites."""
    parser = get_parser(rel_path)
    return parser is not None and bool(parser.SCOPE_TYPES)


def strip_handled_imports(
    extraction: dict[str, list[dict[str, str]]], handled: set[str]
) -> int:
    """Drop the extractor's import edges from files linked here. Returns count."""
    edges = extraction.get("edges", [])
    kept = [
        edge
        for edge in edges
        if not (
            edge.get("relation") in IMPORT_RELATIONS
            and edge.get("source_file") in handled
        )
    ]
    extraction["edges"] = kept
    return len(edges) - len(kept)


def _callable_name(label: str) -> str:
    return label.strip("()").lstrip(".")


def _line(location: str) -> int | None:
    digits = location[1:] if location.startswith("L") else ""
    return int(digits) if digits.isdigit() else None


def link_cross_file(
    nodes: list[dict[str, str]],
    contents: dict[str, str],
    known_files: set[str],
) -> tuple[list[CrossLink], int]:
    """Resolve the imports and cross-file calls of every linkable file.

    Returns the edges to write and how many calls were dropped because the
    files a caller imports declare more than one symbol of that name.
    """
    by_file: dict[str, dict[str, set[str]]] = {}
    scopes: dict[tuple[str, int], str] = {}
    for node in nodes:
        source_file = node.get("source_file") or ""
        label = node.get("label", "")
        if not source_file or node_type(label, source_file) not in CALLABLE_TYPES:
            continue
        our_id = node_id(node)
        names = by_file.setdefault(source_file, {})
        names.setdefault(_callable_name(label), set()).add(our_id)
        line = _line(node.get("source_location", ""))
        if line is not None:
            scopes.setdefault((source_file, line), our_id)

    suffixes = suffix_index(known_files)
    links: list[CrossLink] = []
    dropped = 0
    for rel_path in sorted(contents):
        parser = get_parser(rel_path)
        if parser is None or not parser.SCOPE_TYPES:
            continue
        content = contents[rel_path]
        file_id = truncate(rel_path, MAX_NODE_ID_LENGTH)

        imported: set[str] = set()
        seen_imports: set[str] = set()
        for relation in parser.get_relations(content, rel_path):
            if relation["type"] != "imports":
                continue
            target = relation["target"]
            resolved = resolve_import(target, rel_path, known_files, suffixes)
            if resolved == file_id:
                continue
            target_id = resolved or placeholder_id("imports", target)
            if target_id in seen_imports:
                continue
            seen_imports.add(target_id)
            if resolved:
                imported.add(resolved)
            links.append(
                CrossLink(
                    file_id,
                    target_id,
                    "imports_from",
                    resolved is None,
                    "EXTRACTED",
                    1.0,
                )
            )

        own = by_file.get(rel_path, {})
        seen_calls: set[tuple[str, str]] = set()
        for name, receiver, lines in parser.get_call_sites(content):
            if name in own and receiver in SELF_RECEIVERS:
                continue
            owners = [scopes.get((rel_path, line)) for line in lines]
            caller = next((owner for owner in owners if owner), file_id)
            if (caller, name) in seen_calls:
                continue
            seen_calls.add((caller, name))
            candidates = {
                target
                for path in imported
                for target in by_file.get(path, {}).get(name, ())
            }
            if len(candidates) > 1:
                dropped += 1
                continue
            if not candidates:
                continue
            target_id = next(iter(candidates))
            if target_id == caller:
                continue
            links.append(
                CrossLink(
                    caller, target_id, "calls", False, CALL_CONFIDENCE, CALL_WEIGHT
                )
            )
    return links, dropped


def handled_paths(contents: dict[str, str]) -> set[str]:
    """Return the files whose imports and calls are linked here."""
    return {rel_path for rel_path in contents if is_linkable(rel_path)}
