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
    code_references_by_kind,
    code_references_of,
    code_symbols,
    drop_edges_into,
    drop_file_entities,
    ensure_external_node,
    file_source,
    files_referring,
    replace_code_references,
    replace_edges_from,
    save_entity_summaries,
    sourced_edges,
    upsert_extracted_node,
    upsert_file_node,
)
from enggraph.indexer.crossfile import CALL_CONFIDENCE, CALL_WEIGHT, SELF_RECEIVERS
from enggraph.indexer.parsers import get_parser
from enggraph.indexer.parsers.base import (
    CALLS,
    DECLARES,
    IMPORTS,
    IMPORTS_NAME,
    INHERITS,
    RECEIVER,
)
from enggraph.indexer.parsers.languages import (
    PHP_CLASS,
    PHP_EXACT,
    PHP_FUNCTION,
    PHP_NEW,
)
from enggraph.indexer.resolution import (
    basename_index,
    go_packages,
    go_visible,
    is_go,
    is_php,
    nearest,
    placeholder_id,
    resolve_go_import,
    resolve_import,
    resolve_php_include,
    suffix_index,
)
from enggraph.indexer.summaries import (
    SUMMARY_HEAD_LINES,
    entity_summary,
    extract_summary,
)

STRUCTURE = ["contains", "method"]
LINKS = ["imports_from", "calls", "inherits"]
SYMBOL_TYPES = ["function", "method", "class", "interface"]
TYPE_KINDS = frozenset({"class", "interface"})
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
        relation = "method" if symbol.kind == "method" and symbol.parent else "contains"
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


def package_mates(rel_paths: set[str], files: list[str]) -> set[str]:
    """Return the Go files sharing a directory with one of these Go files."""
    directories = {posixpath.dirname(one) for one in rel_paths if is_go(one)}
    return {
        one for one in files if is_go(one) and posixpath.dirname(one) in directories
    }


def php_callers(cursor: Cursor, project: str, rel_paths: set[str]) -> set[str]:
    """Return the files whose references name what these PHP files declare.

    Procedural PHP calls a function without including its file, so a moved
    declaration reaches callers no import edge leads to.
    """
    php = sorted(one for one in rel_paths if is_php(one))
    if not php:
        return set()
    names: set[str] = set()
    receivers: set[str] = set()
    for _, _, kind, name, receiver in code_references_of(cursor, project, php):
        if kind != DECLARES:
            continue
        names.add(name.lower())
        if receiver == PHP_CLASS:
            receivers.add(f"{PHP_EXACT}{name.lower()}")
    found = files_referring(
        cursor,
        project,
        [CALLS, INHERITS, IMPORTS],
        sorted(names),
        sorted(receivers),
    )
    return {one for one in found if is_php(one)}


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
        self.kind: dict[str, str] = {}
        self.file_of: dict[str, str] = {}
        for node_id, name, kind, file_path in code_symbols(
            cursor, project, SOURCE_CODE, SYMBOL_TYPES
        ):
            self.by_file[file_path][callable_name(name)].append(node_id)
            self.ids_by_file[file_path].append(node_id)
            self.kind[node_id] = kind
            self.file_of[node_id] = file_path
        self.owner = {
            method: owner
            for owner, method in sourced_edges(cursor, project, SOURCE_CODE, "method")
        }

    def named(self, rel_path: str, name: str) -> list[str]:
        """Return the symbols of one file a name may mean."""
        return self.by_file.get(rel_path, {}).get(name, [])

    def resolve_in(self, files: set[str], name: str) -> tuple[str | None, bool]:
        """Resolve a name among these files; the flag says it was ambiguous."""
        candidates = {
            target for path in sorted(files) for target in self.named(path, name)
        }
        if len(candidates) > 1:
            return None, True
        return (next(iter(candidates)) if candidates else None), False

    def resolve_call(
        self,
        rel_path: str,
        name: str,
        receiver: str,
        caller: str,
        imported: set[str],
        home: set[str] | None = None,
    ) -> tuple[str | None, bool]:
        """Resolve a call to one symbol; the flag says it was ambiguous.

        A bare or self call means a symbol of the file, or of the `home` files
        it sees without an import, the one in the caller's class when several
        share the name. Otherwise the files this one imports are asked, and
        its own symbols last.
        """
        local = [
            target
            for path in sorted(home or {rel_path})
            for target in self.named(path, name)
        ]
        if receiver in SELF_RECEIVERS and local:
            if len(local) == 1:
                return local[0], False
            owner = self.owner.get(caller, caller)
            same = [one for one in local if self.owner.get(one) == owner]
            return (same[0], False) if len(same) == 1 else (None, True)
        target, ambiguous = self.resolve_in(imported, name)
        if target is not None or ambiguous:
            return target, ambiguous
        return (local[0], False) if len(local) == 1 else (None, False)


class PhpDeclarations:
    """What the PHP files of a project declare, by kind and qualified name."""

    def __init__(self, cursor: Cursor, project: str) -> None:
        """Read every declaration, keyed without case as PHP looks names up."""
        self.found: dict[tuple[str, str], set[tuple[str, str]]] = defaultdict(set)
        for file_path, source_id, name, receiver in code_references_by_kind(
            cursor, project, DECLARES
        ):
            self.found[(receiver, name.lower())].add((source_id, file_path))

    def choose(
        self, kind: str, name: str, rel_path: str, imported: set[str]
    ) -> tuple[tuple[str, str] | None, bool]:
        """Pick the (id, file) a name means; the flag says it was ambiguous.

        Of several declarations the file's own wins, then the one in a file it
        includes or imports, then the one nearest by path.
        """
        found = sorted(self.found.get((kind, name.lower()), set()))
        if len(found) <= 1:
            return (found[0] if found else None), False
        for preferred in (
            [one for one in found if one[1] == rel_path],
            [one for one in found if truncate(one[1], MAX_NODE_ID_LENGTH) in imported],
        ):
            if len(preferred) == 1:
                return preferred[0], False
        closest = nearest(rel_path, [path for _, path in found])
        same = [one for one in found if one[1] == closest]
        return (same[0], False) if len(same) == 1 else (None, True)

    def resolve(
        self,
        table: SymbolTable,
        rel_path: str,
        kind: str,
        name: str,
        receiver: str,
        caller: str,
        imported: set[str],
    ) -> tuple[str | None, bool]:
        """Resolve a PHP call or base to one symbol; the flag says ambiguous.

        An unqualified function falls back from its namespace to the global
        one; `Class::name()` is looked up among the methods of that class.
        """
        if kind == INHERITS or receiver == PHP_NEW:
            found, ambiguous = self.choose(PHP_CLASS, name, rel_path, imported)
            return (found[0] if found else None), ambiguous
        if receiver in ("", PHP_EXACT):
            found, ambiguous = self.choose(PHP_FUNCTION, name, rel_path, imported)
            short = name.rpartition("\\")[2]
            if found is None and not ambiguous and not receiver and short != name:
                found, ambiguous = self.choose(PHP_FUNCTION, short, rel_path, imported)
            return (found[0] if found else None), ambiguous
        if receiver.startswith(PHP_EXACT):
            found, ambiguous = self.choose(
                PHP_CLASS, receiver[len(PHP_EXACT) :], rel_path, imported
            )
            if found is None:
                return None, ambiguous
            members = [
                one
                for one in table.named(found[1], name)
                if table.owner.get(one) == found[0]
            ]
            return (members[0], False) if len(members) == 1 else (None, bool(members))
        return table.resolve_call(rel_path, name, receiver, caller, imported)


def link_receivers(
    cursor: Cursor,
    project: str,
    table: SymbolTable,
    rel_path: str,
    references: list[tuple[str, str, str, str]],
    home: set[str],
) -> int:
    """Draw `method` from a type another file of the package declares.

    Edges into this file's methods from another file are redrawn here, since
    the pass writing this file cannot see the type.
    """
    edges: list[Edge] = []
    for source_id, kind, name, _ in references:
        if kind != RECEIVER:
            continue
        table.owner.pop(source_id, None)
        types = [
            target
            for path in sorted(home - {rel_path})
            for target in table.named(path, name)
            if table.kind.get(target) in TYPE_KINDS
        ]
        if len(types) == 1:
            table.owner[source_id] = types[0]
            edges.append((types[0], source_id, "method", EXTRACTED))
    own = table.ids_by_file.get(rel_path, [])
    drop_edges_into(cursor, project, SOURCE_CODE, "method", own, own)
    if edges:
        replace_edges_from(cursor, project, SOURCE_CODE, ["method"], [], edges)
    return len(edges)


def php_import(
    declared: PhpDeclarations,
    name: str,
    receiver: str,
    rel_path: str,
    known_files: set[str],
    basenames: dict[str, list[str]],
) -> str | None:
    """Resolve a PHP include, or a `use` of a class, to the file it reaches."""
    if not receiver:
        return resolve_php_include(name, rel_path, known_files, basenames)
    found, _ = declared.choose(PHP_CLASS, name, rel_path, set())
    return truncate(found[1], MAX_NODE_ID_LENGTH) if found else None


def link_files(
    cursor: Cursor,
    project: str,
    known: list[str],
    dirty: set[str],
    modules: dict[str, str] | None = None,
) -> tuple[int, int]:
    """Resolve the stored references of the dirty files into edges.

    `known` is every file of the project, which is what an import resolves
    against; `modules` maps a Go module path to its directory. A Go import
    names a package, the directory, and a call qualified by its alias is
    looked up in that package alone. A PHP name resolves through what every
    PHP file declares. Returns the edges written and how many calls were
    dropped as ambiguous.
    """
    if not dirty:
        return 0, 0
    table = SymbolTable(cursor, project)
    known_files = set(known)
    suffixes = suffix_index(known_files)
    packages = go_packages(known_files)
    php_files = any(map(is_php, dirty))
    declared = PhpDeclarations(cursor, project) if php_files else None
    basenames = basename_index(known_files) if php_files else {}
    by_file: dict[str, list[tuple[str, str, str, str]]] = defaultdict(list)
    for file_path, source_id, kind, name, receiver in code_references_of(
        cursor, project, sorted(dirty)
    ):
        by_file[file_path].append((source_id, kind, name, receiver))

    written = 0
    for rel_path in sorted(dirty):
        if is_go(rel_path):
            written += link_receivers(
                cursor,
                project,
                table,
                rel_path,
                by_file.get(rel_path, []),
                go_visible(rel_path, packages),
            )

    dropped = 0
    for rel_path in sorted(dirty):
        go = is_go(rel_path)
        php = declared if is_php(rel_path) else None
        home = go_visible(rel_path, packages) if go else {rel_path}
        file_id = truncate(rel_path, MAX_NODE_ID_LENGTH)
        edges: dict[tuple[str, str, str], dict[str, object]] = {}
        imported: set[str] = set()
        dotted: set[str] = set()
        aliases: dict[str, set[str]] = {}
        references = by_file.get(rel_path, [])
        for _, kind, name, receiver in references:
            if kind not in (IMPORTS, IMPORTS_NAME):
                continue
            if go:
                found = resolve_go_import(name, modules or {}, packages)
                targets = [one for one in found if one != file_id]
            elif php is not None:
                resolved = php_import(
                    php, name, receiver, rel_path, known_files, basenames
                )
                if resolved == file_id:
                    continue
                targets = [resolved] if resolved is not None else []
            else:
                resolved = resolve_import(name, rel_path, known_files, suffixes)
                if resolved == file_id or (resolved is None and kind == IMPORTS_NAME):
                    continue
                targets = [resolved] if resolved is not None else []
            if receiver == ".":
                dotted.update(targets)
            elif receiver and receiver != "_":
                aliases[receiver] = set(targets)
            if targets:
                imported.update(targets)
            else:
                targets = [placeholder_id(IMPORTS, name)]
                ensure_external_node(cursor, project, targets[0], "external_import")
            for target in targets:
                edges[(file_id, target, "imports_from")] = EXTRACTED
        for source_id, kind, name, receiver in references:
            if kind not in (CALLS, INHERITS):
                continue
            if php is not None:
                target, ambiguous = php.resolve(
                    table, rel_path, kind, name, receiver, source_id, imported
                )
            elif receiver in aliases:
                target, ambiguous = table.resolve_in(aliases[receiver], name)
            else:
                target, ambiguous = table.resolve_call(
                    rel_path,
                    name,
                    receiver if kind == CALLS else "",
                    source_id,
                    dotted if go and not receiver else imported,
                    home,
                )
            if kind == CALLS:
                dropped += ambiguous
                if target is not None and target != source_id:
                    edges.setdefault((source_id, target, "calls"), INFERRED)
                continue
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
