"""The code extractor's two passes, over a store kept in memory.

`codegraph` reaches the database only through the storage functions it
imports, so they are replaced here by a dictionary that behaves the same way.
"""

from __future__ import annotations

from typing import Any

import pytest

from enggraph.core.config import GRAPHIFYY_EXTENSIONS, OWN_CODE_EXTENSIONS
from enggraph.indexer import codegraph
from enggraph.indexer.codegraph import is_own_code, link_files, to_relink, write_file
from enggraph.indexer.parsers.languages import (
    JavaScriptParser,
    PythonParser,
    TSXParser,
    TypeScriptParser,
)

PROJECT = "alpha"
HELPER = '''def helper():
    """Help."""
    return 1
'''
CALLER = """from .a import helper


def main():
    return helper()
"""


class Store:
    """The rows `codegraph` writes and reads, without a database."""

    def __init__(self) -> None:
        """Start empty."""
        self.nodes: dict[str, dict[str, Any]] = {}
        self.edges: set[tuple[str, str, str, str]] = set()
        self.references: dict[str, list[tuple[str, str, str, str, int]]] = {}

    def upsert_file_node(
        self,
        _: Any,  # noqa: ANN401
        project: str,
        rel_path: str,
        summary: str,
        source: str = "",
    ) -> None:
        """Keep a file node and the producer that wrote it last."""
        node = self.nodes.setdefault(rel_path, {"type": "file", "file_path": rel_path})
        node["source"] = source

    def file_source(
        self,
        _: Any,  # noqa: ANN401
        project: str,
        rel_path: str,
    ) -> str | None:
        """Return the producer that wrote a file node last."""
        node = self.nodes.get(rel_path)
        return node.get("source") if node else None

    def clear_file_artifacts(
        self,
        _: Any,  # noqa: ANN401
        project: str,
        rel_path: str,
    ) -> None:
        """Drop every entity of a file and every edge leaving the file."""
        self.drop_file_entities(None, project, rel_path, [])
        self.edges = {edge for edge in self.edges if edge[0] != rel_path}
        self.references.pop(rel_path, None)

    def drop_file_entities(
        self,
        _: Any,  # noqa: ANN401
        project: str,
        rel_path: str,
        keep: list[str],
    ) -> int:
        """Drop what the file no longer declares, with its edges."""
        gone = {
            node_id
            for node_id, node in self.nodes.items()
            if node["file_path"] == rel_path
            and node["type"] != "file"
            and node_id not in keep
        }
        for node_id in gone:
            del self.nodes[node_id]
        self.edges = {
            edge for edge in self.edges if edge[0] not in gone and edge[1] not in gone
        }
        return len(gone)

    def upsert_extracted_node(
        self,
        _: Any,  # noqa: ANN401
        project: str,
        node_id: str,
        name: str,
        node_type: str,
        file_path: str,
        summary: str,
        metadata: dict[str, object],
    ) -> None:
        """Insert a node or refresh it, keeping what else it carries."""
        node = self.nodes.setdefault(node_id, {})
        node.update(name=name, type=node_type, file_path=file_path, **metadata)

    def ensure_external_node(
        self,
        _: Any,  # noqa: ANN401
        project: str,
        node_id: str,
        node_type: str,
    ) -> None:
        """Keep a placeholder."""
        self.nodes.setdefault(node_id, {"type": node_type, "file_path": None})

    def replace_edges_from(
        self,
        _: Any,  # noqa: ANN401
        project: str,
        source: str,
        relations: list[str],
        source_ids: list[str],
        edges: list[tuple[str, str, str, dict[str, object]]],
    ) -> None:
        """Rewrite the edges of these relations leaving these nodes."""
        self.edges = {
            edge
            for edge in self.edges
            if not (
                edge[3] == source and edge[2] in relations and edge[0] in source_ids
            )
        }
        self.edges |= {(one, two, relation, source) for one, two, relation, _ in edges}

    def replace_code_references(
        self,
        _: Any,  # noqa: ANN401
        project: str,
        rel_path: str,
        rows: list[tuple[str, str, str, str, int]],
    ) -> None:
        """Keep a file's references."""
        self.references[rel_path] = rows

    def save_entity_summaries(self, *_: Any) -> int:  # noqa: ANN401
        """Summaries are not under test here."""
        return 0

    def code_references_of(
        self,
        _: Any,  # noqa: ANN401
        project: str,
        paths: list[str],
    ) -> list[tuple[str, str, str, str, str]]:
        """Read the references of these files."""
        return [
            (path, source_id, kind, name, receiver)
            for path in paths
            for source_id, kind, name, receiver, _ in self.references.get(path, [])
        ]

    def code_symbols(
        self,
        _: Any,  # noqa: ANN401
        project: str,
        source: str,
        types: list[str],
    ) -> list[tuple[str, str, str, str]]:
        """Read the symbols one producer wrote."""
        return [
            (node_id, node["name"], node["type"], node["file_path"])
            for node_id, node in self.nodes.items()
            if node.get("source") == source and node["type"] in types
        ]

    def sourced_edges(
        self,
        _: Any,  # noqa: ANN401
        project: str,
        source: str,
        relation: str,
    ) -> list[tuple[str, str]]:
        """Read one relation one producer wrote."""
        return [
            (one, two)
            for one, two, kind, owner in self.edges
            if kind == relation and owner == source
        ]

    def relations(self, relation: str) -> set[tuple[str, str]]:
        """Return the edges of one relation."""
        return {(one, two) for one, two, kind, _ in self.edges if kind == relation}


@pytest.fixture
def store(monkeypatch: pytest.MonkeyPatch) -> Store:
    """Swap the storage functions `codegraph` imported for the store's."""
    kept = Store()
    for name in (
        "upsert_file_node",
        "file_source",
        "clear_file_artifacts",
        "drop_file_entities",
        "upsert_extracted_node",
        "ensure_external_node",
        "replace_edges_from",
        "replace_code_references",
        "save_entity_summaries",
        "code_references_of",
        "code_symbols",
        "sourced_edges",
    ):
        monkeypatch.setattr(codegraph, name, getattr(kept, name))
    return kept


def index(
    store: Store,
    files: dict[str, str],
    dirty: set[str] | None = None,
    others: tuple[str, ...] = (),
) -> None:
    """Run both passes over these files, as a run that read them would."""
    for rel_path, content in files.items():
        write_file(None, PROJECT, rel_path, content)
    known = sorted({*files, *(path for path in store.references), *others})
    link_files(None, PROJECT, known, set(files) if dirty is None else dirty)


def test_python_is_read_by_the_own_extractor_alone() -> None:
    """A language is read by one extractor, never by both."""
    assert is_own_code("pkg/a.py")
    assert not OWN_CODE_EXTENSIONS & GRAPHIFYY_EXTENSIONS


def test_python_symbols_carry_their_labels_lines_and_owners() -> None:
    """Labels and lines are the ones node ids were built from before."""
    symbols, _ = PythonParser().extract_code(
        "class Thing(Base):\n"
        "    @property\n"
        "    def run(self):\n"
        "        def inner():\n"
        "            pass\n"
        "\n"
        "def top():\n"
        "    pass\n"
    )
    assert [(one.key, one.kind, one.parent) for one in symbols] == [
        ("Thing@L1", "class", ""),
        (".run()@L3", "method", "Thing@L1"),
        ("top()@L7", "function", ""),
    ]


def test_python_references_keep_their_scope_and_receiver() -> None:
    """A nested function's calls belong to the function around it."""
    _, references = PythonParser().extract_code(
        "from . import sibling\n"
        "import os.path as p\n"
        "\n"
        "def run():\n"
        "    def inner():\n"
        "        helper()\n"
        "    self.go()\n"
    )
    found = {(one.scope, one.kind, one.name, one.receiver) for one in references}
    assert found == {
        ("", "imports", ".", ""),
        ("", "imports_name", ".sibling", ""),
        ("", "imports", "os.path", ""),
        ("run()@L4", "calls", "helper", ""),
        ("run()@L4", "calls", "go", "self"),
    }


def test_a_call_resolves_into_the_imported_file(store: Store) -> None:
    """The edge the upstream extractor could not draw across files."""
    index(store, {"pkg/a.py": HELPER, "pkg/b.py": CALLER})
    assert ("pkg/b.py::main()@L4", "pkg/a.py::helper()@L1") in store.relations("calls")
    assert ("pkg/b.py", "pkg/a.py") in store.relations("imports_from")
    assert ("pkg/a.py", "pkg/a.py::helper()@L1") in store.relations("contains")


def test_a_module_taken_by_name_resolves_its_calls(store: Store) -> None:
    """`from pkg import a` reaches a file, and `a.helper()` the symbol in it."""
    caller = "from pkg import a\n\n\ndef main():\n    return a.helper()\n"
    index(store, {"pkg/__init__.py": "", "pkg/a.py": HELPER, "b.py": caller})
    assert ("b.py::main()@L4", "pkg/a.py::helper()@L1") in store.relations("calls")


def test_a_symbol_above_an_edit_keeps_its_node(store: Store) -> None:
    """What was written onto a node survives an edit that did not move it."""
    index(store, {"pkg/a.py": HELPER})
    store.nodes["pkg/a.py::helper()@L1"]["summary_source"] = "llm"
    index(store, {"pkg/a.py": HELPER + "\n\ndef later():\n    pass\n"})
    assert store.nodes["pkg/a.py::helper()@L1"]["summary_source"] == "llm"
    assert "pkg/a.py::later()@L6" in store.nodes


def test_an_importer_follows_a_moved_symbol(store: Store) -> None:
    """The importing file is relinked although its own content did not move."""
    index(store, {"pkg/a.py": HELPER, "pkg/b.py": CALLER})
    write_file(None, PROJECT, "pkg/a.py", "import os\n\n" + HELPER)
    known = ["pkg/a.py", "pkg/b.py"]
    dirty = to_relink(
        known,
        set(known),
        {"pkg/a.py"},
        lambda paths: codegraph.importers(None, PROJECT, paths),
    )
    assert dirty == {"pkg/a.py", "pkg/b.py"}
    link_files(None, PROJECT, known, dirty)
    assert store.relations("calls") == {
        ("pkg/b.py::main()@L4", "pkg/a.py::helper()@L3")
    }


def test_a_self_call_picks_the_method_of_the_callers_class(store: Store) -> None:
    """Two classes of one file may each declare the name called."""
    source = (
        "class One:\n"
        "    def go(self):\n"
        "        pass\n"
        "    def run(self):\n"
        "        self.go()\n"
        "\n"
        "class Two:\n"
        "    def go(self):\n"
        "        pass\n"
    )
    index(store, {"a.py": source})
    assert store.relations("calls") == {("a.py::.run()@L4", "a.py::.go()@L2")}
    assert ("a.py::One@L1", "a.py::.go()@L2") in store.relations("method")


def test_an_unknown_base_becomes_a_placeholder(store: Store) -> None:
    """A base the tree does not declare still gets an edge to stand on."""
    index(store, {"a.py": "class Thing(Exception):\n    pass\n"})
    assert ("a.py::Thing@L1", "Exception") in store.relations("inherits")
    assert store.nodes["Exception"]["type"] == "external_symbol"


def test_what_is_relinked() -> None:
    """Nothing when nothing moved, everything when the set of files did."""
    files = ["a.py", "b.py", "c.py"]
    importing = {"a.py": {"b.py"}}

    def importers(paths: set[str]) -> set[str]:
        return {one for path in paths for one in importing.get(path, set())}

    assert to_relink(files, set(files), set(), importers) == set()
    assert to_relink(files, set(files), {"a.py"}, importers) == {"a.py", "b.py"}
    assert to_relink(files, {"a.py", "b.py"}, set(), importers) == set(files)
    assert to_relink(files, set(files), set(), importers, fresh=True) == set(files)


TS_SERVICE = """export interface Store {
  save(): void;
}

export class Service implements Store {
  save(): void {
    this.flush();
  }
  flush = () => {
    log();
  };
}

export const log = () => {};
"""
TS_CALLER = """import { Service } from "./service";

export function main(): void {
  new Service().save();
}
"""


def test_typescript_is_read_by_the_own_extractor_alone() -> None:
    """Both TypeScript grammars leave the upstream extractor."""
    assert is_own_code("src/a.ts")
    assert is_own_code("src/b.tsx")


def test_typescript_symbols_carry_their_labels_lines_and_owners() -> None:
    """Labels and lines are the ones the upstream extractor built ids from."""
    symbols, _ = TypeScriptParser().extract_code(TS_SERVICE)
    assert [(one.key, one.kind, one.parent) for one in symbols] == [
        ("Store@L1", "interface", ""),
        ("Service@L5", "class", ""),
        (".save()@L6", "method", "Service@L5"),
        (".flush()@L9", "method", "Service@L5"),
        ("log()@L14", "function", ""),
    ]


def test_typescript_references_keep_their_scope_and_receiver() -> None:
    """A nested arrow's calls belong to the function around it."""
    _, references = TypeScriptParser().extract_code(
        'import a from "./a";\n'
        'export * from "./b";\n'
        'import c = require("./c");\n'
        'const d = require("./d");\n'
        "abstract class Base extends ns.Root<T> implements Shape {}\n"
        "function run() {\n"
        "  const inner = () => helper();\n"
        "  this.go();\n"
        '  import("./e");\n'
        "}\n"
    )
    found = {(one.scope, one.kind, one.name, one.receiver) for one in references}
    assert found == {
        ("", "imports", "./a", ""),
        ("", "imports", "./b", ""),
        ("", "imports", "./c", ""),
        ("", "imports", "./d", ""),
        ("", "imports", "./e", ""),
        ("Base@L5", "inherits", "Root", "ns"),
        ("Base@L5", "inherits", "Shape", ""),
        ("run()@L6", "calls", "helper", ""),
        ("run()@L6", "calls", "go", "this"),
    }


def test_a_tsx_component_is_called() -> None:
    """A capitalised element is a component; a lowercase one is markup."""
    _, references = TSXParser().extract_code(
        "export const Page = () => <div><Card /><ui.Button /></div>;\n"
    )
    found = {(one.scope, one.kind, one.name, one.receiver) for one in references}
    assert found == {
        ("Page()@L1", "calls", "Card", ""),
        ("Page()@L1", "calls", "Button", "ui"),
    }


def test_a_typescript_call_resolves_across_files(store: Store) -> None:
    """Calls, a constructor and an implemented interface reach their nodes."""
    index(store, {"src/service.ts": TS_SERVICE, "src/main.ts": TS_CALLER})
    calls = store.relations("calls")
    assert ("src/main.ts::main()@L3", "src/service.ts::Service@L5") in calls
    assert ("src/main.ts::main()@L3", "src/service.ts::.save()@L6") in calls
    assert ("src/service.ts::.save()@L6", "src/service.ts::.flush()@L9") in calls
    assert ("src/service.ts::.flush()@L9", "src/service.ts::log()@L14") in calls
    assert ("src/main.ts", "src/service.ts") in store.relations("imports_from")
    assert (
        "src/service.ts::Service@L5",
        "src/service.ts::Store@L1",
    ) in store.relations("inherits")


def test_an_import_reaches_a_file_another_extractor_reads(store: Store) -> None:
    """A `.js` or `.css` file is a node of its own, not a placeholder."""
    source = 'import { a } from "./util.js";\nimport "./page.css";\n'
    index(store, {"src/app.ts": source}, others=("src/util.js", "src/page.css"))
    assert store.relations("imports_from") == {
        ("src/app.ts", "src/util.js"),
        ("src/app.ts", "src/page.css"),
    }


def test_javascript_is_read_by_the_own_extractor_alone() -> None:
    """Every ECMAScript extension leaves the upstream and the native parsers."""
    for path in ("src/a.js", "src/b.jsx", "src/c.mjs", "src/d.cjs"):
        assert is_own_code(path)


def test_javascript_symbols_and_references() -> None:
    """A base given as an expression, a private field holding an arrow."""
    symbols, references = JavaScriptParser().extract_code(
        'const lib = require("./lib");\n'
        "class Job extends ns.Base {\n"
        "  #run = () => this.go();\n"
        "}\n"
        "class Mixed extends mixin(Job) {}\n"
        "export const App = () => <Card />;\n"
    )
    assert [(one.key, one.kind, one.parent) for one in symbols] == [
        ("Job@L2", "class", ""),
        (".#run()@L3", "method", "Job@L2"),
        ("Mixed@L5", "class", ""),
        ("App()@L6", "function", ""),
    ]
    found = {(one.scope, one.kind, one.name, one.receiver) for one in references}
    assert found == {
        ("", "imports", "./lib", ""),
        ("Job@L2", "inherits", "Base", "ns"),
        (".#run()@L3", "calls", "go", "this"),
        ("App()@L6", "calls", "Card", ""),
    }


def test_a_file_the_native_parsers_wrote_is_cleared_once(store: Store) -> None:
    """Their ids have another shape, and nothing of theirs is left behind."""
    store.upsert_file_node(None, PROJECT, "src/app.jsx", "", source="native")
    store.upsert_extracted_node(
        None, PROJECT, "src/app.jsx::App", "App", "function", "src/app.jsx", "", {}
    )
    store.edges.add(("src/app.jsx", "react", "imports", "native"))
    index(store, {"src/app.jsx": "export const App = () => <div />;\n"})
    assert "src/app.jsx::App" not in store.nodes
    assert "src/app.jsx::App()@L1" in store.nodes
    assert store.relations("imports") == set()
    assert store.nodes["src/app.jsx"]["source"] == "code"
