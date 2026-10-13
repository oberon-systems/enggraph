"""Extract code with the parsers of this package, in two passes.

The first pass reads one changed file and writes its symbols and the edges
that hold them, and stores what the file refers to without resolving it. The
second resolves those references against the symbols stored for the whole
project, so a file that did not change is never read again.
"""

from __future__ import annotations

import posixpath
from collections import defaultdict
from collections.abc import Callable

from psycopg2.extensions import cursor as Cursor

from enggraph.core.config import (
    MAX_NODE_ID_LENGTH,
    OWN_CODE_EXTENSIONS,
    SOURCE_CODE,
    SOURCE_NATIVE,
)
from enggraph.core.identifiers import entity_node_id, truncate
from enggraph.core.storage import (
    clear_file_artifacts,
    code_references_of,
    code_symbols,
    drop_file_entities,
    ensure_external_node,
    file_source,
    replace_code_references,
    replace_edges_from,
    save_entity_summaries,
    sourced_edges,
    upsert_extracted_node,
    upsert_file_node,
)
from enggraph.indexer.crossfile import CALL_CONFIDENCE, CALL_WEIGHT, SELF_RECEIVERS
from enggraph.indexer.parsers import get_parser
from enggraph.indexer.parsers.base import CALLS, IMPORTS, IMPORTS_NAME, INHERITS
from enggraph.indexer.resolution import placeholder_id, resolve_import, suffix_index
from enggraph.indexer.summaries import (
    SUMMARY_HEAD_LINES,
    entity_summary,
    extract_summary,
)

STRUCTURE = ["contains", "method"]
LINKS = ["imports_from", "calls", "inherits"]
SYMBOL_TYPES = ["function", "method", "class", "interface"]
EXTRACTED = {"confidence": "EXTRACTED", "weight": 1.0}
INFERRED = {"confidence": CALL_CONFIDENCE, "weight": CALL_WEIGHT}

Edge = tuple[str, str, str, dict[str, object]]


def is_own_code(rel_path: str) -> bool:
    """Say whether a file is extracted here rather than by the upstream one."""
    return posixpath.splitext(rel_path)[1] in OWN_CODE_EXTENSIONS


def callable_name(label: str) -> str:
    """Return the name a call spells for a symbol label: `.run()` is `run`."""
    return label.removesuffix("()").lstrip(".")


def write_file(cursor: Cursor, project: str, rel_path: str, content: str) -> int:
    """Store one file's symbols, their edges and references. Returns symbols.

    A symbol that kept its name and line keeps its node, with the summary and
    the vectors written for it; only the ones gone from the file are dropped.
    """
    parser = get_parser(rel_path)
    if parser is None:
        raise ValueError(f"{rel_path}: no parser extracts this language")
    symbols, references = parser.extract_code(content)
    file_id = truncate(rel_path, MAX_NODE_ID_LENGTH)
    ids = {symbol.key: entity_node_id(rel_path, symbol.key) for symbol in symbols}
    lines = content.splitlines()

    if file_source(cursor, project, rel_path) == SOURCE_NATIVE:
        # A file the native parsers read before: their ids have another shape
        # and no pass of theirs visits the file again to clear what they drew.
        clear_file_artifacts(cursor, project, rel_path)
    upsert_file_node(
        cursor,
        project,
        rel_path,
        extract_summary(
            rel_path,
            "\n".join(lines[:SUMMARY_HEAD_LINES]),
            [{"name": symbol.label, "type": symbol.kind} for symbol in symbols],
        ),
        source=SOURCE_CODE,
    )
    drop_file_entities(cursor, project, rel_path, list(ids.values()))
    edges: list[Edge] = []
    for symbol in symbols:
        upsert_extracted_node(
            cursor,
            project,
            ids[symbol.key],
            symbol.label,
            symbol.kind,
            rel_path,
            "",
            {"source": SOURCE_CODE, "source_location": f"L{symbol.line}"},
        )
        relation = "method" if symbol.kind == "method" else "contains"
        edges.append(
            (ids.get(symbol.parent, file_id), ids[symbol.key], relation, EXTRACTED)
        )
    replace_edges_from(
        cursor, project, SOURCE_CODE, STRUCTURE, [file_id, *ids.values()], edges
    )
    replace_code_references(
        cursor,
        project,
        rel_path,
        [
            (ids.get(one.scope, file_id), one.kind, one.name, one.receiver, one.line)
            for one in references
        ],
    )
    save_entity_summaries(
        cursor,
        project,
        [(ids[symbol.key], entity_summary(lines, symbol.line)) for symbol in symbols],
    )
    return len(symbols)


def importers(cursor: Cursor, project: str, rel_paths: set[str]) -> set[str]:
    """Return the files whose stored imports reach one of these files."""
    targets = {truncate(rel_path, MAX_NODE_ID_LENGTH) for rel_path in rel_paths}
    return {
        source_id
        for source_id, target_id in sourced_edges(
            cursor, project, SOURCE_CODE, "imports_from"
        )
        if target_id in targets
    }


def to_relink(
    readable: list[str],
    previous: set[str],
    changed: set[str],
    importing: Callable[[set[str]], set[str]],
    fresh: bool = False,
) -> set[str]:
    """Return the files whose references are resolved again this run.

    A file added or gone can change what any import resolves to, so the set
    of files moving relinks them all; otherwise the changed files and the
    files importing them.
    """
    if fresh or set(readable) != previous:
        return set(readable)
    return changed | importing(changed) if changed else set()


class SymbolTable:
    """The stored symbols of a project, by file and by the name a call uses."""

    def __init__(self, cursor: Cursor, project: str) -> None:
        """Read the symbols and which class each method belongs to."""
        self.by_file: dict[str, dict[str, list[str]]] = defaultdict(
            lambda: defaultdict(list)
        )
        self.ids_by_file: dict[str, list[str]] = defaultdict(list)
        for node_id, name, _, file_path in code_symbols(
            cursor, project, SOURCE_CODE, SYMBOL_TYPES
        ):
            self.by_file[file_path][callable_name(name)].append(node_id)
            self.ids_by_file[file_path].append(node_id)
        self.owner = {
            method: owner
            for owner, method in sourced_edges(cursor, project, SOURCE_CODE, "method")
        }

    def named(self, rel_path: str, name: str) -> list[str]:
        """Return the symbols of one file a name may mean."""
        return self.by_file.get(rel_path, {}).get(name, [])

    def resolve_call(
        self, rel_path: str, name: str, receiver: str, caller: str, imported: set[str]
    ) -> tuple[str | None, bool]:
        """Resolve a call to one symbol; the flag says it was ambiguous.

        A bare or self call means the file's own symbol, the one in the
        caller's class when several share the name. Otherwise the files this
        one imports are asked, and the file's own symbols last.
        """
        local = self.named(rel_path, name)
        if receiver in SELF_RECEIVERS and local:
            if len(local) == 1:
                return local[0], False
            home = self.owner.get(caller, caller)
            same = [one for one in local if self.owner.get(one) == home]
            return (same[0], False) if len(same) == 1 else (None, True)
        candidates = {
            target for path in sorted(imported) for target in self.named(path, name)
        }
        if len(candidates) > 1:
            return None, True
        if candidates:
            return next(iter(candidates)), False
        return (local[0], False) if len(local) == 1 else (None, False)


def link_files(
    cursor: Cursor, project: str, known: list[str], dirty: set[str]
) -> tuple[int, int]:
    """Resolve the stored references of the dirty files into edges.

    `known` is every file of the project, which is what an import resolves
    against. Returns the edges written and how many calls were dropped as
    ambiguous.
    """
    if not dirty:
        return 0, 0
    table = SymbolTable(cursor, project)
    known_files = set(known)
    suffixes = suffix_index(known_files)
    by_file: dict[str, list[tuple[str, str, str, str]]] = defaultdict(list)
    for file_path, source_id, kind, name, receiver in code_references_of(
        cursor, project, sorted(dirty)
    ):
        by_file[file_path].append((source_id, kind, name, receiver))

    written = 0
    dropped = 0
    for rel_path in sorted(dirty):
        file_id = truncate(rel_path, MAX_NODE_ID_LENGTH)
        edges: dict[tuple[str, str, str], dict[str, object]] = {}
        imported: set[str] = set()
        references = by_file.get(rel_path, [])
        for _, kind, name, _ in references:
            if kind not in (IMPORTS, IMPORTS_NAME):
                continue
            resolved = resolve_import(name, rel_path, known_files, suffixes)
            if resolved == file_id or (resolved is None and kind == IMPORTS_NAME):
                continue
            if resolved is None:
                resolved = placeholder_id(IMPORTS, name)
                ensure_external_node(cursor, project, resolved, "external_import")
            else:
                imported.add(resolved)
            edges[(file_id, resolved, "imports_from")] = EXTRACTED
        for source_id, kind, name, receiver in references:
            if kind == CALLS:
                target, ambiguous = table.resolve_call(
                    rel_path, name, receiver, source_id, imported
                )
                dropped += ambiguous
                if target is not None and target != source_id:
                    edges.setdefault((source_id, target, "calls"), INFERRED)
            elif kind == INHERITS:
                target, _ = table.resolve_call(rel_path, name, "", source_id, imported)
                if target is None:
                    target = truncate(name, MAX_NODE_ID_LENGTH)
                    ensure_external_node(cursor, project, target, "external_symbol")
                if target != source_id:
                    edges[(source_id, target, "inherits")] = EXTRACTED
        replace_edges_from(
            cursor,
            project,
            SOURCE_CODE,
            LINKS,
            [file_id, *table.ids_by_file.get(rel_path, [])],
            [(*key, meta) for key, meta in edges.items()],
        )
        written += len(edges)
    return written, dropped
