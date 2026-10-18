"""Extract code with the parsers of this package, in two passes.

The first pass reads one changed file and writes its symbols and the edges
that hold them, and stores what the file refers to without resolving it. The
second resolves those references against the symbols stored for the whole
project, so a file that did not change is never read again.
"""

from __future__ import annotations

import posixpath
import re
from collections import defaultdict
from collections.abc import Callable
from functools import partial

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
    symbol_names_of,
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
    JAVA_GLOB,
    JAVA_NEW,
    JAVA_PACKAGE,
    JAVA_SELF,
    JAVA_STATIC,
    JAVA_STATIC_GLOB,
    JAVA_SUPER,
    JAVA_TYPE,
    JAVA_VALUE,
    KOTLIN_EXTENSION,
    KOTLIN_FUNCTION,
    PHP_CLASS,
    PHP_EXACT,
    PHP_FUNCTION,
    PHP_NEW,
    RUBY_INSTANCE,
    RUBY_NESTING,
    RUBY_RELATIVE,
    RUBY_SINGLETON,
    RUBY_SUPERCLASS,
    RUST_GLOB,
    RUST_SELF,
    RUST_VALUE,
)
from enggraph.indexer.resolution import (
    RUBY_EXTENSION,
    RUST_ANCHORS,
    RustCrate,
    RustLayout,
    basename_index,
    go_packages,
    go_visible,
    is_csharp,
    is_go,
    is_jvm,
    is_kotlin,
    is_php,
    is_ruby,
    is_rust,
    nearest,
    placeholder_id,
    resolve_go_import,
    resolve_import,
    resolve_php_include,
    ruby_file_name,
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
# How many `pub use` steps a Rust name is followed through to its declaration.
RUST_REEXPORT_HOPS = 4
# How many levels of Java superclasses and interfaces a member is looked up in.
JAVA_HIERARCHY_HOPS = 8
SYMBOL_LINE = re.compile(r"@L(\d+)$")

Edge = tuple[str, str, str, dict[str, object]]
RustAliases = dict[str, tuple[str | None, str]]


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


class Declarations:
    """What the files of one language declare, by kind and qualified name."""

    def __init__(
        self,
        cursor: Cursor,
        project: str,
        accept: Callable[[str], bool],
        fold: bool,
    ) -> None:
        """Read the declarations of those files; `fold` keys them without case."""
        self.fold = fold
        self.found: dict[tuple[str, str], set[tuple[str, str]]] = defaultdict(set)
        for file_path, source_id, name, receiver in code_references_by_kind(
            cursor, project, DECLARES
        ):
            if accept(file_path):
                self.found[(receiver, self.key(name))].add((source_id, file_path))

    def key(self, name: str) -> str:
        """Return the form a name is looked up by."""
        return name.lower() if self.fold else name

    def choose(
        self, kind: str, name: str, rel_path: str, imported: set[str]
    ) -> tuple[tuple[str, str] | None, bool]:
        """Pick the (id, file) a name means; the flag says it was ambiguous.

        Of several declarations the file's own wins, then the one in a file it
        includes or imports, then the one nearest by path.
        """
        found = sorted(self.found.get((kind, self.key(name)), set()))
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


class PhpDeclarations(Declarations):
    """What the PHP files of a project declare, keyed without case as PHP does."""

    def __init__(self, cursor: Cursor, project: str) -> None:
        """Read every PHP declaration."""
        super().__init__(cursor, project, is_php, fold=True)

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


class RustResolver:
    """What the Rust files of a project see through their `use` and `mod`."""

    def __init__(
        self,
        cursor: Cursor,
        project: str,
        table: SymbolTable,
        layout: RustLayout,
    ) -> None:
        """Read the imports of every Rust file."""
        self.table = table
        self.layout = layout
        self.uses: dict[str, list[tuple[str, str]]] = defaultdict(list)
        for file_path, _, name, receiver in code_references_by_kind(
            cursor, project, IMPORTS
        ):
            if is_rust(file_path):
                self.uses[file_path].append((name, receiver))
        self.scopes: dict[str, tuple[RustAliases, list[str]]] = {}
        self.found: dict[tuple[str, str, int], set[str]] = {}
        self.members: dict[tuple[str, str], list[str]] = {}

    def index_members(self) -> None:
        """Index the methods of each type by name, once receivers are drawn."""
        self.members = defaultdict(list)
        for names in self.table.by_file.values():
            for name, ids in names.items():
                for one in ids:
                    if one in self.table.owner:
                        self.members[(self.table.owner[one], name)].append(one)

    def scope(self, rel_path: str) -> tuple[RustAliases, list[str]]:
        """Return a file's imported names, each a (file, item), and its globs.

        The item is empty when the name is a module; the file is None when
        the path leaves the tree.
        """
        if rel_path not in self.scopes:
            aliases: RustAliases = {}
            globs: list[str] = []
            for path, alias in self.uses.get(rel_path, []):
                target, rest = self.layout.resolve(rel_path, path.split("::"))
                if alias == RUST_GLOB:
                    if target is not None and not rest:
                        globs.append(target)
                elif len(rest) <= 1:
                    aliases[alias] = (target, rest[0] if rest else "")
                else:
                    aliases[alias] = (None, "")
            self.scopes[rel_path] = (aliases, globs)
        return self.scopes[rel_path]

    def items(
        self, rel_path: str, name: str, hops: int = RUST_REEXPORT_HOPS
    ) -> set[str]:
        """Return what a name means in a module, re-exports followed."""
        key = (rel_path, name, hops)
        if key not in self.found:
            self.found[key] = set()
            found = {
                one
                for one in self.table.named(rel_path, name)
                if self.table.kind.get(one) != "method"
            }
            aliases, globs = self.scope(rel_path)
            if not found and hops and name in aliases:
                target, item = aliases[name]
                if target is not None and item:
                    found = self.items(target, item, hops - 1)
            elif not found and hops:
                for glob in globs:
                    found |= self.items(glob, name, hops - 1)
            self.found[key] = found
        return self.found[key]

    def locate(self, rel_path: str, path: str) -> tuple[str | None, list[str]]:
        """Resolve a path to the module file it reaches and the items after it.

        A head no module answers to is an item of the file itself.
        """
        segments = path.split("::")
        head = segments[0]
        aliases, _ = self.scope(rel_path)
        if head not in RUST_ANCHORS and head in aliases:
            target, item = aliases[head]
            if target is None:
                return None, []
            if item:
                return target, [item, *segments[1:]]
            return self.layout.descend(target, segments[1:])
        target, rest = self.layout.resolve(rel_path, segments)
        if target is None and head not in RUST_ANCHORS:
            return rel_path, segments
        return target, rest

    def types(self, rel_path: str, name: str, path: str) -> list[str]:
        """Return the types a name under a path may mean."""
        if path:
            target, rest = self.locate(rel_path, path)
            if target is None or rest:
                return []
        else:
            target = rel_path
        return sorted(
            one
            for one in self.items(target, name)
            if self.table.kind.get(one) in TYPE_KINDS
        )

    def resolve(
        self,
        rel_path: str,
        kind: str,
        name: str,
        receiver: str,
        caller: str,
        imported: set[str],
    ) -> tuple[str | None, bool]:
        """Resolve a Rust call or base to one symbol; the flag says ambiguous.

        A path call reaches a type's method wherever its impl is, or a
        symbol of a module; a call on a value of unknown type is matched by
        name among the methods of the file and of what it imports.
        """
        if kind == INHERITS:
            return unique(self.types(rel_path, name, receiver))
        if receiver == RUST_SELF:
            owner = self.table.owner.get(caller)
            found = self.members.get((owner, name), []) if owner else []
            if found:
                return unique(found)
            return unique(sorted(self.items(rel_path, name)))
        if receiver == RUST_VALUE:
            return unique(
                [
                    one
                    for path in sorted(imported | {rel_path})
                    for one in self.table.named(path, name)
                    if self.table.kind.get(one) == "method"
                ]
            )
        if not receiver:
            return unique(sorted(self.items(rel_path, name)))
        target, rest = self.locate(rel_path, receiver)
        if target is None or len(rest) > 1:
            return None, False
        if not rest:
            return unique(sorted(self.items(target, name)))
        return unique(
            [
                method
                for owner in self.types(target, rest[0], "")
                for method in self.members.get((owner, name), [])
            ]
        )

    def imports(self, rel_path: str, path: str) -> tuple[list[str], str]:
        """Return the file an import reaches, else the external crate it names.

        Both are empty for a path into the file itself or a module missing.
        """
        target, _ = self.layout.resolve(rel_path, path.split("::"))
        if target is not None:
            found = [truncate(target, MAX_NODE_ID_LENGTH)] if target != rel_path else []
            return found, ""
        head = path.split("::")[0]
        return [], "" if head in RUST_ANCHORS else head


class JvmResolver:
    """What the Java and Kotlin files of a project see through package and imports.

    The two languages share one package space, so either resolves the other.
    """

    accept = staticmethod(is_jvm)

    def __init__(self, cursor: Cursor, project: str, table: SymbolTable) -> None:
        """Read the declarations, imports and bases of every file it accepts."""
        self.table = table
        self.declared = Declarations(cursor, project, self.accept, fold=False)
        self.package_of: dict[str, str] = {}
        self.packages: dict[str, list[str]] = defaultdict(list)
        self.local: dict[tuple[str, str], list[str]] = defaultdict(list)
        self.qualified: dict[str, str] = {}
        for (kind, name), found in self.declared.found.items():
            for source_id, file_path in found:
                if kind == JAVA_PACKAGE:
                    self.package_of[file_path] = name
                    self.packages[name].append(file_path)
                elif kind == JAVA_TYPE:
                    self.qualified[source_id] = name
                    self.local[(file_path, name.rpartition(".")[2])].append(name)
        self.extensions: dict[str, set[str]] = defaultdict(set)
        for (kind, name), found in self.declared.found.items():
            if kind == KOTLIN_EXTENSION:
                self.extensions[name].update(source_id for source_id, _ in found)
        self.uses: dict[str, list[tuple[str, str]]] = defaultdict(list)
        for file_path, _, name, receiver in code_references_by_kind(
            cursor, project, IMPORTS
        ):
            if self.accept(file_path):
                self.uses[file_path].append((name, receiver))
        self.written_bases: dict[str, list[tuple[str, str, str]]] = defaultdict(list)
        for file_path, source_id, name, receiver in code_references_by_kind(
            cursor, project, INHERITS
        ):
            if self.accept(file_path):
                self.written_bases[source_id].append((file_path, name, receiver))
        self.members: dict[tuple[str, str], list[str]] = defaultdict(list)
        for names in table.by_file.values():
            for name, ids in names.items():
                for one in ids:
                    if one in table.owner:
                        self.members[(table.owner[one], name)].append(one)
        self.found_bases: dict[str, list[str]] = {}

    def namespaces(self, rel_path: str) -> list[str]:
        """Return the packages whose types a file sees unqualified, nearest first."""
        return [self.package_of.get(rel_path, "")]

    def mates(self, rel_path: str) -> set[str]:
        """Return the files of the package a file is in."""
        package = self.package_of.get(rel_path)
        return set(self.packages.get(package, [])) if package else set()

    def extended(self, rel_path: str) -> bool:
        """Say whether a file's calls may reach extension functions."""
        return is_kotlin(rel_path)

    def parts(self, type_id: str) -> list[str]:
        """Return every declaration making up one type."""
        return [type_id]

    def qualify(
        self, rel_path: str, written: str, kind: str = JAVA_TYPE
    ) -> tuple[list[str], str]:
        """Return the declared names a written type may mean, and its own name.

        The first part is looked up in the file, its single-type imports, its
        packages and its on-demand imports, else the name is taken as written.
        A type imported by name and not declared here is outside the tree.
        `kind` KOTLIN_FUNCTION qualifies a top-level function the same way.
        """
        head, _, rest = written.partition(".")
        tail = f".{rest}" if rest else ""
        imports = self.uses.get(rel_path, [])
        single = [name for name, alias in imports if alias == head]
        tiers = [
            self.local.get((rel_path, head), []) if kind == JAVA_TYPE else [],
            single,
            *(
                [f"{space}.{head}" if space else head]
                for space in self.namespaces(rel_path)
            ),
            [f"{name}.{head}" for name, alias in imports if alias == JAVA_GLOB],
            [head] if rest else [],
        ]
        for index, names in enumerate(tiers):
            found = sorted(
                {
                    f"{one}{tail}"
                    for one in names
                    if (kind, f"{one}{tail}") in self.declared.found
                }
            )
            if found or (index == 1 and single):
                return found, f"{single[0]}{tail}" if single else written
        return [], written

    def type_of(
        self, rel_path: str, written: str, imported: set[str]
    ) -> tuple[str | None, bool]:
        """Resolve a written type to one type; the flag says it was ambiguous."""
        names, _ = self.qualify(rel_path, written)
        if len(names) != 1:
            return None, len(names) > 1
        found, ambiguous = self.declared.choose(JAVA_TYPE, names[0], rel_path, imported)
        return (found[0] if found else None), ambiguous

    def bases(self, type_id: str) -> list[str]:
        """Return the types a type extends or implements, as far as resolved."""
        if type_id not in self.found_bases:
            self.found_bases[type_id] = []
            found = [
                self.type_of(file_path, name, set())[0]
                for part in self.parts(type_id)
                for file_path, name, _ in self.written_bases.get(part, [])
            ]
            self.found_bases[type_id] = [one for one in found if one is not None]
        return self.found_bases[type_id]

    def outer(self, type_id: str) -> str | None:
        """Return the type a nested type is declared in."""
        outer = self.qualified.get(type_id, "").rpartition(".")[0]
        file_path = self.table.file_of.get(type_id)
        found = [
            one
            for one, path in self.declared.found.get((JAVA_TYPE, outer), set())
            if path == file_path
        ]
        return found[0] if len(found) == 1 else None

    def member(self, start: list[str], name: str) -> tuple[str | None, bool]:
        """Find a method in these types, then up their bases level by level."""
        level, seen = start, set()
        for _ in range(JAVA_HIERARCHY_HOPS):
            level = [part for one in level for part in self.parts(one)]
            level = [one for one in dict.fromkeys(level) if one not in seen]
            seen.update(level)
            if not level:
                break
            found = [
                self.members[(one, name)]
                for one in level
                if self.members.get((one, name))
            ]
            if len(found) > 1:
                return None, True
            if found:
                return first_declared(found[0]), False
            level = [base for one in level for base in self.bases(one)]
        return None, False

    def static_import(
        self, rel_path: str, name: str, imported: set[str]
    ) -> tuple[str | None, bool]:
        """Resolve a bare call to a member a static import brings in."""
        for path, alias in self.uses.get(rel_path, []):
            if alias == JAVA_STATIC and path.rpartition(".")[2] == name:
                written = path.rpartition(".")[0]
            elif alias == JAVA_STATIC_GLOB:
                written = path
            else:
                continue
            owner, _ = self.type_of(rel_path, written, imported)
            target, ambiguous = self.member([owner], name) if owner else (None, False)
            if target is not None or ambiguous:
                return target, ambiguous
        return None, False

    def function_of(
        self, rel_path: str, name: str, imported: set[str]
    ) -> tuple[str | None, bool]:
        """Resolve a bare name to one Kotlin top-level function, overloads as one."""
        names, _ = self.qualify(rel_path, name, KOTLIN_FUNCTION)
        if len(names) != 1:
            return None, len(names) > 1
        found = self.declared.found.get((KOTLIN_FUNCTION, names[0]), set())
        if len({path for _, path in found}) == 1:
            return first_declared([one for one, _ in found]), False
        chosen, ambiguous = self.declared.choose(
            KOTLIN_FUNCTION, names[0], rel_path, imported
        )
        return (chosen[0] if chosen else None), ambiguous

    def kotlin_bare(
        self, rel_path: str, name: str, imported: set[str]
    ) -> tuple[str | None, bool]:
        """Resolve a bare Kotlin call no member answers.

        It creates a class, else calls a top-level function, else a member a
        single import names.
        """
        for lookup in (self.type_of, self.function_of):
            target, ambiguous = lookup(rel_path, name, imported)
            if target is not None or ambiguous:
                return target, ambiguous
        for path, alias in self.uses.get(rel_path, []):
            if alias != name:
                continue
            written, _, member = path.rpartition(".")
            owner, _ = self.type_of(rel_path, written, imported)
            if owner is not None:
                return self.member([owner], member)
        return None, False

    def bare(
        self, rel_path: str, name: str, imported: set[str]
    ) -> tuple[str | None, bool]:
        """Resolve a bare call no class around the caller answers."""
        if is_kotlin(rel_path):
            return self.kotlin_bare(rel_path, name, imported)
        return self.static_import(rel_path, name, imported)

    def extension(
        self, rel_path: str, name: str, imported: set[str], methods: bool
    ) -> tuple[str | None, bool]:
        """Resolve a call on a value by name among the extension functions seen.

        `methods` lets the methods of those files answer too. A file with
        extensions sees its package without an import.
        """
        extended = self.extended(rel_path)
        visible = imported | {rel_path} | (self.mates(rel_path) if extended else set())
        extensions = self.extensions.get(name, set()) if extended else set()
        found = [
            one
            for path in sorted(visible)
            for one in self.table.named(path, name)
            if one in extensions or (methods and self.table.kind.get(one) == "method")
        ]
        owner, file_of = self.table.owner, self.table.file_of
        if len({owner.get(one, file_of.get(one)) for one in found}) > 1:
            return None, True
        return (first_declared(found) if found else None), False

    def resolve(
        self,
        rel_path: str,
        kind: str,
        name: str,
        receiver: str,
        caller: str,
        imported: set[str],
    ) -> tuple[str | None, bool]:
        """Resolve a JVM call or base to one symbol; the flag says ambiguous.

        A bare call is looked up in the caller's class, its bases and the
        classes around it, then in what static imports bring in (Kotlin: a
        class, a top-level function, a member imported by name); a call on a
        type in that type and its bases; a call on a value of unknown type by
        name among the methods of the file and of what it imports. A Kotlin
        call no type answers may reach an extension function.
        """
        if kind == INHERITS or receiver == JAVA_NEW:
            return self.type_of(rel_path, name, imported)
        extended = self.extended(rel_path)
        owner = self.table.owner.get(caller)
        if owner is None and self.table.kind.get(caller) in TYPE_KINDS:
            owner = caller
        if receiver == JAVA_SUPER:
            return self.member(self.bases(owner), name) if owner else (None, False)
        if receiver in ("", JAVA_SELF):
            current = owner
            while current is not None:
                target, ambiguous = self.member([current], name)
                if target is not None or ambiguous or receiver:
                    return target, ambiguous
                current = self.outer(current)
            return self.bare(rel_path, name, imported)
        if receiver == JAVA_VALUE:
            return self.extension(rel_path, name, imported, methods=True)
        owner, ambiguous = self.type_of(rel_path, receiver, imported)
        if owner is not None:
            target, ambiguous = self.member([owner], name)
        else:
            target = None
        if extended and target is None and not ambiguous:
            return self.extension(rel_path, name, imported, methods=False)
        return target, ambiguous

    def placeholder(self, rel_path: str, name: str) -> str:
        """Return the name a type outside the tree is kept under."""
        return self.qualify(rel_path, name)[1]

    def declaring_file(self, kind: str, name: str, rel_path: str) -> str | None:
        """Return the one file declaring a name, overloads in one file as one."""
        files = {path for _, path in self.declared.found.get((kind, name), set())}
        if len(files) == 1:
            return next(iter(files))
        found, _ = self.declared.choose(kind, name, rel_path, set())
        return found[1] if found else None

    def imports(self, rel_path: str, name: str, receiver: str) -> tuple[list[str], str]:
        """Return the files an import reaches, else the name it is kept under.

        `pkg.*` reaches every file of the package; both are empty for an
        import of the file itself. A Kotlin import may name a top-level
        function or a member, which reaches the file of its type.
        """
        own = truncate(rel_path, MAX_NODE_ID_LENGTH)
        if receiver == JAVA_GLOB and name in self.packages:
            files = {truncate(one, MAX_NODE_ID_LENGTH) for one in self.packages[name]}
            return sorted(files - {own}), ""
        written = name.rpartition(".")[0] if receiver == JAVA_STATIC else name
        found = self.declaring_file(JAVA_TYPE, written, rel_path)
        if found is None and is_kotlin(rel_path):
            found = self.declaring_file(KOTLIN_FUNCTION, name, rel_path)
            if found is None and receiver != JAVA_GLOB:
                outer = name.rpartition(".")[0]
                found = self.declaring_file(JAVA_TYPE, outer, rel_path)
        if found is None:
            return [], written
        target = truncate(found, MAX_NODE_ID_LENGTH)
        return ([target] if target != own else []), ""


class CSharpResolver(JvmResolver):
    """What the C# files of a project see through their namespaces and usings.

    A type sees its own namespace and every namespace enclosing it. The parts
    of a partial type are its declarations of one name in one directory.
    """

    accept = staticmethod(is_csharp)

    def __init__(self, cursor: Cursor, project: str, table: SymbolTable) -> None:
        """Read the declarations, usings and bases of every C# file."""
        super().__init__(cursor, project, table)
        self.spaces: dict[str, set[str]] = defaultdict(set)
        for name, files in self.packages.items():
            for file_path in files:
                self.spaces[file_path].add(name)

    def namespaces(self, rel_path: str) -> list[str]:
        """Return the file's namespaces and those enclosing them, deepest first."""
        found = {
            ".".join(parts[:depth])
            for space in self.spaces.get(rel_path, set())
            for parts in [space.split(".")]
            for depth in range(1, len(parts) + 1)
        }
        return [*sorted(found, key=lambda one: (-one.count("."), one)), ""]

    def mates(self, rel_path: str) -> set[str]:
        """Return the files of every namespace a file sees unqualified."""
        return {
            one
            for space in self.namespaces(rel_path)
            for one in self.packages.get(space, [])
        }

    def extended(self, rel_path: str) -> bool:
        """Say that C# calls may reach extension methods."""
        return True

    def parts(self, type_id: str) -> list[str]:
        """Return the declarations of a partial type, this one among them."""
        directory = posixpath.dirname(self.table.file_of.get(type_id, ""))
        qualified = self.qualified.get(type_id, "")
        found = self.declared.found.get((JAVA_TYPE, qualified), set())
        same = [one for one, path in found if posixpath.dirname(path) == directory]
        return sorted({type_id, *same})

    def bare(
        self, rel_path: str, name: str, imported: set[str]
    ) -> tuple[str | None, bool]:
        """Resolve a bare call no class around the caller answers."""
        return self.static_import(rel_path, name, imported)

    def type_of(
        self, rel_path: str, written: str, imported: set[str]
    ) -> tuple[str | None, bool]:
        """Resolve a written type to one type; the flag says it was ambiguous.

        Partial parts in one directory are one type, its shortest path first.
        """
        names, _ = self.qualify(rel_path, written)
        if len(names) != 1:
            return None, len(names) > 1
        found = self.declared.found.get((JAVA_TYPE, names[0]), set())
        own = sorted(one for one, path in found if path == rel_path)
        if own:
            return own[0], False
        heads: dict[str, tuple[str, str]] = {}
        for one in sorted(found, key=lambda one: (len(one[1]), one[1], one[0])):
            heads.setdefault(posixpath.dirname(one[1]), one)
        reached = {
            posixpath.dirname(path)
            for _, path in found
            if truncate(path, MAX_NODE_ID_LENGTH) in imported
        }
        if len(heads) <= 1:
            return next((one for one, _ in heads.values()), None), False
        if len(reached) == 1:
            return heads[reached.pop()][0], False
        closest = nearest(rel_path, [path for _, path in heads.values()])
        if closest is None:
            return None, True
        return heads[posixpath.dirname(closest)][0], False

    def imports(self, rel_path: str, name: str, receiver: str) -> tuple[list[str], str]:
        """Return the files a using reaches, else the name it is kept under.

        A namespace, by itself or under an alias, reaches every file of it.
        """
        own = truncate(rel_path, MAX_NODE_ID_LENGTH)
        if receiver != JAVA_STATIC_GLOB and name in self.packages:
            files = {truncate(one, MAX_NODE_ID_LENGTH) for one in self.packages[name]}
            return sorted(files - {own}), ""
        found = self.declaring_file(JAVA_TYPE, name, rel_path)
        if found is None:
            return [], name
        target = truncate(found, MAX_NODE_ID_LENGTH)
        return ([target] if target != own else []), ""


class RubyResolver(JvmResolver):
    """What the Ruby files of a project see through nesting and requires.

    An autoloaded constant needs no require, so it resolves wherever it is
    declared; every declaration of one qualified name is one open class.
    """

    accept = staticmethod(is_ruby)

    def __init__(
        self, cursor: Cursor, project: str, table: SymbolTable, known_files: set[str]
    ) -> None:
        """Read the declarations, requires and bases of every Ruby file."""
        super().__init__(cursor, project, table)
        self.nesting: dict[str, list[str]] = {}
        self.singletons: set[str] = set()
        self.types: dict[str, list[tuple[str, str]]] = {}
        for (kind, name), found in self.declared.found.items():
            if kind == JAVA_TYPE:
                self.types[name] = sorted(
                    found, key=lambda one: (len(one[1]), one[1], one[0])
                )
            for source_id, _ in found:
                if kind == RUBY_NESTING:
                    self.nesting[source_id] = name.split()
                elif kind == RUBY_SINGLETON:
                    self.singletons.add(source_id)
        self.known = {one for one in known_files if is_ruby(one)}
        self.suffixes = suffix_index(self.known, RUBY_EXTENSION)
        self.methods: dict[str, list[str]] = defaultdict(list)
        for file_path, names in table.by_file.items():
            if not is_ruby(file_path):
                continue
            for name, ids in names.items():
                self.methods[name].extend(
                    one for one in ids if table.kind.get(one) == "method"
                )
        self.found_ancestors: dict[str, list[str]] = {}

    def declarations(self, qualified: str) -> list[tuple[str, str]]:
        """Return the (id, file) of every declaration of a name, shortest path first."""
        return self.types.get(qualified, [])

    def qualify_constant(self, written: str, caller: str) -> str | None:
        """Return the declared name a constant means where the caller writes it.

        Its head is looked up in each module of the caller's nesting, then
        at the top level; the rest of the path is taken under the head.
        """
        holder = caller if caller in self.nesting else self.table.owner.get(caller, "")
        spaces = [*self.nesting.get(holder, []), ""]
        if written.startswith("::"):
            spaces, written = [""], written[2:]
        head, _, rest = written.partition("::")
        for space in spaces:
            start = f"{space}::{head}" if space else head
            if (JAVA_TYPE, start) in self.declared.found:
                full = f"{start}::{rest}" if rest else start
                return full if (JAVA_TYPE, full) in self.declared.found else None
        return None

    def constant_of(
        self, rel_path: str, written: str, caller: str, imported: set[str]
    ) -> str | None:
        """Pick the declaration of a constant an edge points to.

        The file's own wins, then one in a file it requires, then the one in
        the file an autoloader expects, then the shortest path.
        """
        qualified = self.qualify_constant(written, caller)
        found = self.declarations(qualified) if qualified else []
        expected = ruby_file_name(qualified or "")
        for preferred in (
            [one for one, path in found if path == rel_path],
            [
                one
                for one, path in found
                if truncate(path, MAX_NODE_ID_LENGTH) in imported
            ],
            [
                one
                for one, path in found
                if path == expected or path.endswith(f"/{expected}")
            ],
            [one for one, _ in found],
        ):
            if preferred:
                return preferred[0]
        return None

    def ancestors(
        self,
        qualified: str,
        hops: int = JAVA_HIERARCHY_HOPS,
        seen: set[str] | None = None,
    ) -> list[str]:
        """Return where a method of a type is looked up, in Ruby's order.

        Prepended modules, the type, included modules last first, extended
        ones, then the same for the superclass; each by its qualified name.
        """
        if seen is None and qualified in self.found_ancestors:
            return self.found_ancestors[qualified]
        visited = set() if seen is None else seen
        if qualified in visited or hops <= 0:
            return []
        visited.add(qualified)
        bases: dict[str, list[str]] = defaultdict(list)
        for part, _ in self.declarations(qualified):
            for file_path, name, kind in self.written_bases.get(part, []):
                found = self.constant_of(file_path, name, part, set())
                if found is not None:
                    bases[kind].append(self.qualified[found])
        order: list[str] = []
        for one in reversed(bases["prepend"]):
            order.extend(self.ancestors(one, hops - 1, visited))
        order.append(qualified)
        for one in [
            *reversed(bases["include"]),
            *reversed(bases["extend"]),
            *bases[RUBY_SUPERCLASS][:1],
        ]:
            order.extend(self.ancestors(one, hops - 1, visited))
        if seen is None:
            self.found_ancestors[qualified] = order
        return order

    def settle(self, found: list[str]) -> tuple[str | None, bool]:
        """Return the def that wins of several of one name: the later in one file."""
        if len({self.table.file_of.get(one) for one in found}) > 1:
            return None, True
        return (last_declared(found) if found else None), False

    def member_of(
        self, qualified: str, name: str, singleton: bool, after: bool = False
    ) -> tuple[str | None, bool]:
        """Find a method along a type's ancestors; `after` starts past the type.

        A def of the wanted kind, singleton or not, wins over the other.
        """
        order = self.ancestors(qualified)
        if after and qualified in order:
            order = order[order.index(qualified) + 1 :]
        for owner in order:
            found = [
                one
                for part, _ in self.declarations(owner)
                for one in self.members.get((part, name), [])
            ]
            if found:
                wanted = [one for one in found if (one in self.singletons) == singleton]
                return self.settle(wanted or found)
        return None, False

    def function(
        self, rel_path: str, name: str, imported: set[str]
    ) -> tuple[str | None, bool]:
        """Resolve a bare call to a top-level def of the file or one it requires."""
        found = [
            one
            for path in sorted({rel_path} | imported)
            for one in self.table.named(path, name)
            if self.table.kind.get(one) == "function"
        ]
        own = [one for one in found if self.table.file_of.get(one) == rel_path]
        return self.settle(own or found)

    def by_name(
        self, rel_path: str, name: str, imported: set[str]
    ) -> tuple[str | None, bool]:
        """Resolve a call on a value of unknown class by the method's name alone.

        The methods of the file and the files it requires answer first, then
        those of every Ruby file; methods of several types are ambiguous.
        """
        visible = {rel_path} | imported
        every = self.methods.get(name, [])
        for found in (
            [one for one in every if self.table.file_of.get(one) in visible],
            every,
        ):
            if not found:
                continue
            owners = {
                self.qualified.get(self.table.owner.get(one, ""), one) for one in found
            }
            return self.settle(found) if len(owners) == 1 else (None, True)
        return None, False

    def resolve(
        self,
        rel_path: str,
        kind: str,
        name: str,
        receiver: str,
        caller: str,
        imported: set[str],
    ) -> tuple[str | None, bool]:
        """Resolve a Ruby call or base to one symbol; the flag says ambiguous.

        A bare or self call is looked up along the caller's class, a bare one
        then among top-level defs; a call on a constant prefers its singleton
        methods, one on an instance its instance methods; one on a value of
        unknown class goes by name alone.
        """
        if kind == INHERITS or receiver == JAVA_NEW:
            return self.constant_of(rel_path, name, caller, imported), False
        owner = self.table.owner.get(caller)
        if owner is None and self.table.kind.get(caller) in TYPE_KINDS:
            owner = caller
        qualified = self.qualified.get(owner, "") if owner else ""
        singleton = caller in self.singletons or caller == owner
        if receiver == JAVA_SUPER:
            if not qualified:
                return None, False
            return self.member_of(qualified, name, singleton, after=True)
        if receiver in ("", JAVA_SELF):
            if qualified:
                target, ambiguous = self.member_of(qualified, name, singleton)
                if target is not None or ambiguous or receiver:
                    return target, ambiguous
            return self.function(rel_path, name, imported)
        if receiver == JAVA_VALUE:
            return self.by_name(rel_path, name, imported)
        written = receiver.removeprefix(RUBY_INSTANCE)
        found = self.constant_of(rel_path, written, caller, imported)
        if found is None:
            return None, False
        return self.member_of(self.qualified[found], name, written == receiver)

    def placeholder(self, rel_path: str, name: str) -> str:
        """Return the name a constant outside the tree is kept under."""
        return name.removeprefix("::")

    def imports(self, rel_path: str, name: str, receiver: str) -> tuple[list[str], str]:
        """Return the file a require loads, else the name it is kept under.

        A relative path is taken from the file's directory and draws nothing
        when missing; any other goes through the load path, `lib/` first.
        """
        own = truncate(rel_path, MAX_NODE_ID_LENGTH)
        file_name = name if name.endswith(RUBY_EXTENSION) else f"{name}{RUBY_EXTENSION}"
        if receiver == RUBY_RELATIVE:
            path = posixpath.normpath(
                posixpath.join(posixpath.dirname(rel_path), file_name)
            )
            target = truncate(path, MAX_NODE_ID_LENGTH)
            return ([target] if path in self.known and target != own else []), ""
        found = sorted({*self.suffixes.get(file_name, []), *({file_name} & self.known)})
        if not found:
            return [], name
        lib = [
            one
            for one in found
            if one == f"lib/{file_name}" or one.endswith(f"/lib/{file_name}")
        ]
        chosen = nearest(rel_path, lib or found) or min(lib or found, key=len)
        target = truncate(chosen, MAX_NODE_ID_LENGTH)
        return ([target] if target != own else []), ""


def declared_line(node_id: str) -> int:
    """Return the line a symbol id ends with."""
    match = SYMBOL_LINE.search(node_id)
    return int(match[1]) if match else 0


def first_declared(found: list[str]) -> str:
    """Return the overload declared first, by the line its id ends with."""
    return min(found, key=declared_line)


def last_declared(found: list[str]) -> str:
    """Return the def declared last, which is the one Ruby keeps."""
    return max(found, key=declared_line)


def go_types(table: SymbolTable, home: set[str], name: str, _: str) -> list[str]:
    """Return the types of a name the other files of a Go package declare."""
    return [
        target
        for path in sorted(home)
        for target in table.named(path, name)
        if table.kind.get(target) in TYPE_KINDS
    ]


def unique(found: list[str]) -> tuple[str | None, bool]:
    """Return the one symbol found; the flag says there were several."""
    if len(set(found)) > 1:
        return None, True
    return (found[0] if found else None), False


def rust_callers(cursor: Cursor, project: str, rel_paths: set[str]) -> set[str]:
    """Return the Rust files whose references name what these Rust files declare.

    A path call reaches a method through its type, whose impl may sit in a
    file the caller never imports.
    """
    return naming_callers(cursor, project, rel_paths, is_rust)


def jvm_callers(cursor: Cursor, project: str, rel_paths: set[str]) -> set[str]:
    """Return the Java and Kotlin files naming what these files of either declare.

    A type or function of the same package is used without an import.
    """
    return naming_callers(cursor, project, rel_paths, is_jvm)


def csharp_callers(cursor: Cursor, project: str, rel_paths: set[str]) -> set[str]:
    """Return the C# files naming what these C# files declare.

    A type of the same or an enclosing namespace is used without a using.
    """
    return naming_callers(cursor, project, rel_paths, is_csharp)


def ruby_callers(cursor: Cursor, project: str, rel_paths: set[str]) -> set[str]:
    """Return the Ruby files naming what these Ruby files declare.

    An autoloaded constant is used without a require, and a call on a value
    resolves by the method's name alone.
    """
    chosen = sorted(one for one in rel_paths if is_ruby(one))
    if not chosen:
        return set()
    names = {
        callable_name(name).lower()
        for name in symbol_names_of(cursor, project, SOURCE_CODE, chosen)
    }
    receivers: set[str] = set()
    for _, _, kind, name, receiver in code_references_of(cursor, project, chosen):
        if kind != DECLARES or receiver != JAVA_TYPE:
            continue
        parts = name.lower().split("::")
        for start in range(len(parts)):
            written = "::".join(parts[start:])
            names.add(written)
            receivers.update({written, f"::{written}", f"{RUBY_INSTANCE}{written}"})
    found = files_referring(
        cursor, project, [CALLS, INHERITS], sorted(names), sorted(receivers)
    )
    return {one for one in found if is_ruby(one)}


def naming_callers(
    cursor: Cursor,
    project: str,
    rel_paths: set[str],
    accept: Callable[[str], bool],
) -> set[str]:
    """Return the files of one language naming what these files of it declare."""
    chosen = sorted(one for one in rel_paths if accept(one))
    if not chosen:
        return set()
    names = {
        callable_name(name).lower()
        for name in symbol_names_of(cursor, project, SOURCE_CODE, chosen)
    }
    found = files_referring(
        cursor, project, [CALLS, INHERITS, RECEIVER], sorted(names), []
    )
    return {one for one in found if accept(one)}


def link_receivers(
    cursor: Cursor,
    project: str,
    table: SymbolTable,
    rel_path: str,
    references: list[tuple[str, str, str, str]],
    find: Callable[[str, str], list[str]],
) -> int:
    """Draw `method` from a type another file declares.

    Edges into this file's methods from another file are redrawn here, since
    the pass writing this file cannot see the type.
    """
    edges: list[Edge] = []
    for source_id, kind, name, receiver in references:
        if kind != RECEIVER:
            continue
        table.owner.pop(source_id, None)
        types = find(name, receiver)
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
    crates: list[RustCrate] | None = None,
) -> tuple[int, int]:
    """Resolve the stored references of the dirty files into edges.

    `known` is every file of the project, which is what an import resolves
    against; `modules` maps a Go module path to its directory and `crates`
    lists the Rust packages. A Go import names a package, the directory, and
    a call qualified by its alias is looked up in that package alone. A PHP
    name resolves through what every PHP file declares, a Rust path through
    the module tree, a Java or Kotlin name through its package and imports,
    a C# name through its namespaces and usings, a Ruby constant through its
    nesting and a require through the load path.
    Returns the edges written and how many calls were dropped as ambiguous.
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
    rust = (
        RustResolver(cursor, project, table, RustLayout(known_files, crates or []))
        if any(map(is_rust, dirty))
        else None
    )
    jvm = JvmResolver(cursor, project, table) if any(map(is_jvm, dirty)) else None
    csharp = (
        CSharpResolver(cursor, project, table) if any(map(is_csharp, dirty)) else None
    )
    ruby = (
        RubyResolver(cursor, project, table, known_files)
        if any(map(is_ruby, dirty))
        else None
    )
    by_file: dict[str, list[tuple[str, str, str, str]]] = defaultdict(list)
    for file_path, source_id, kind, name, receiver in code_references_of(
        cursor, project, sorted(dirty)
    ):
        by_file[file_path].append((source_id, kind, name, receiver))

    written = 0
    for rel_path in sorted(dirty):
        if is_go(rel_path):
            home = go_visible(rel_path, packages) - {rel_path}
            find = partial(go_types, table, home)
        elif rust is not None and is_rust(rel_path):
            find = partial(rust.types, rel_path)
        else:
            continue
        written += link_receivers(
            cursor, project, table, rel_path, by_file.get(rel_path, []), find
        )
    if rust is not None:
        rust.index_members()

    dropped = 0
    for rel_path in sorted(dirty):
        go = is_go(rel_path)
        php = declared if is_php(rel_path) else None
        typed = (
            jvm
            if is_jvm(rel_path)
            else csharp
            if is_csharp(rel_path)
            else ruby
            if is_ruby(rel_path)
            else None
        )
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
            if rust is not None and is_rust(rel_path):
                targets, name = rust.imports(rel_path, name)
                if not targets and not name:
                    continue
                receiver = ""
            elif typed is not None:
                targets, name = typed.imports(rel_path, name, receiver)
                if not targets and not name:
                    continue
                receiver = ""
            elif go:
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
            elif rust is not None and is_rust(rel_path):
                target, ambiguous = rust.resolve(
                    rel_path, kind, name, receiver, source_id, imported
                )
            elif typed is not None:
                target, ambiguous = typed.resolve(
                    rel_path, kind, name, receiver, source_id, imported
                )
                if kind == INHERITS:
                    name = typed.placeholder(rel_path, name)
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
