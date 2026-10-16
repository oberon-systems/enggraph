"""The code extractor's two passes, over a store kept in memory.

`codegraph` reaches the database only through the storage functions it
imports, so they are replaced here by a dictionary that behaves the same way.
"""

from __future__ import annotations

from typing import Any

import pytest

from enggraph.core.config import GRAPHIFYY_EXTENSIONS, OWN_CODE_EXTENSIONS
from enggraph.indexer import codegraph
from enggraph.indexer.codegraph import (
    is_own_code,
    jvm_callers,
    link_files,
    package_mates,
    php_callers,
    rust_callers,
    to_relink,
    write_file,
)
from enggraph.indexer.parsers.languages import (
    GoParser,
    JavaParser,
    JavaScriptParser,
    KotlinParser,
    PhpParser,
    PythonParser,
    RustParser,
    TSXParser,
    TypeScriptParser,
)
from enggraph.indexer.resolution import (
    RustCrate,
    RustLayout,
    basename_index,
    cargo_crate,
    go_mod_candidates,
    go_package_dir,
    resolve_php_include,
)
from enggraph.indexer.summaries import entity_summary

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

    def drop_edges_into(
        self,
        _: Any,  # noqa: ANN401
        project: str,
        source: str,
        relation: str,
        target_ids: list[str],
        keep_sources: list[str],
    ) -> None:
        """Drop one relation drawn into these nodes from elsewhere."""
        self.edges = {
            edge
            for edge in self.edges
            if not (
                edge[3] == source
                and edge[2] == relation
                and edge[1] in target_ids
                and edge[0] not in keep_sources
            )
        }

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

    def code_references_by_kind(
        self,
        _: Any,  # noqa: ANN401
        project: str,
        kind: str,
    ) -> list[tuple[str, str, str, str]]:
        """Read one kind of reference of every file."""
        return [
            (path, source_id, name, receiver)
            for path, rows in self.references.items()
            for source_id, one, name, receiver, _ in rows
            if one == kind
        ]

    def files_referring(
        self,
        _: Any,  # noqa: ANN401
        project: str,
        kinds: list[str],
        names: list[str],
        receivers: list[str],
    ) -> set[str]:
        """Return the files whose references name one of these, lowercase."""
        return {
            path
            for path, rows in self.references.items()
            for _, kind, name, receiver, _ in rows
            if kind in kinds
            and (name.lower() in names or receiver.lower() in receivers)
        }

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

    def symbol_names_of(
        self,
        _: Any,  # noqa: ANN401
        project: str,
        source: str,
        file_paths: list[str],
    ) -> set[str]:
        """Return the names of the entities one producer wrote for these files."""
        return {
            node["name"]
            for node in self.nodes.values()
            if node.get("source") == source
            and node["file_path"] in file_paths
            and node["type"] != "file"
        }

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
        "drop_edges_into",
        "replace_code_references",
        "save_entity_summaries",
        "code_references_of",
        "code_references_by_kind",
        "files_referring",
        "code_symbols",
        "symbol_names_of",
        "sourced_edges",
    ):
        monkeypatch.setattr(codegraph, name, getattr(kept, name))
    return kept


def index(
    store: Store,
    files: dict[str, str],
    dirty: set[str] | None = None,
    others: tuple[str, ...] = (),
    modules: dict[str, str] | None = None,
    crates: list[RustCrate] | None = None,
) -> None:
    """Run both passes over these files, as a run that read them would."""
    for rel_path, content in files.items():
        write_file(None, PROJECT, rel_path, content)
    known = sorted({*files, *(path for path in store.references), *others})
    link_files(
        None,
        PROJECT,
        known,
        set(files) if dirty is None else dirty,
        modules,
        crates,
    )


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


GO_TYPES = """package alpha

type Server struct {
\tBase
\tsync.Mutex
}

func (s *Server) stop() {}

type Handler interface {
\tServe() error
}
"""
GO_SERVE = """package alpha

func (s *Server) Run() {
\ts.stop()
\thelper()
}
"""
GO_HELPER = """package alpha

type Base struct{}

func helper() {}
"""


def test_go_is_read_by_the_own_extractor_alone() -> None:
    """Go leaves the upstream extractor."""
    assert is_own_code("cmd/alpha/main.go")


def test_go_symbols_carry_their_labels_lines_and_owners() -> None:
    """A method hangs off its type when the file declares it, else names it."""
    symbols, _ = GoParser().extract_code(GO_TYPES + GO_SERVE.split("\n", 1)[1])
    assert [(one.key, one.kind, one.parent) for one in symbols] == [
        ("Server@L3", "class", ""),
        (".stop()@L8", "method", "Server@L3"),
        ("Handler@L10", "interface", ""),
        (".Run()@L14", "method", "Server@L3"),
    ]
    symbols, references = GoParser().extract_code(GO_SERVE)
    assert [(one.key, one.parent) for one in symbols] == [(".Run()@L3", "")]
    found = {(one.scope, one.kind, one.name, one.receiver) for one in references}
    assert found == {
        (".Run()@L3", "receiver", "Server", ""),
        (".Run()@L3", "calls", "stop", "self"),
        (".Run()@L3", "calls", "helper", ""),
    }


def test_go_references_keep_their_alias_and_receiver() -> None:
    """Imports carry the alias, literals construct, embedded types are bases."""
    _, references = GoParser().extract_code(
        "package alpha\n"
        "\n"
        "import (\n"
        '\t"net/http"\n'
        '\tb "example.com/alpha/beta"\n'
        '\t"example.com/alpha/go-gamma/v2"\n'
        '\t. "example.com/alpha/delta"\n'
        ")\n"
        "\n"
        "type Reader interface {\n"
        "\tio.Closer\n"
        "}\n"
        "\n"
        "func Run() {\n"
        "\tgo func() { b.Do() }()\n"
        "\t_ = &Thing{}\n"
        "}\n"
    )
    found = {(one.scope, one.kind, one.name, one.receiver) for one in references}
    assert found == {
        ("", "imports", "net/http", "http"),
        ("", "imports", "example.com/alpha/beta", "b"),
        ("", "imports", "example.com/alpha/go-gamma/v2", "gamma"),
        ("", "imports", "example.com/alpha/delta", "."),
        ("Reader@L10", "inherits", "Closer", "io"),
        ("Run()@L14", "calls", "Do", "b"),
        ("Run()@L14", "calls", "Thing", ""),
    }


def test_a_go_method_joins_its_type_across_files(store: Store) -> None:
    """The type's methods are whole, and a call on the receiver finds them."""
    index(
        store,
        {"pkg/types.go": GO_TYPES, "pkg/serve.go": GO_SERVE, "pkg/util.go": GO_HELPER},
    )
    methods = store.relations("method")
    assert ("pkg/types.go::Server@L3", "pkg/types.go::.stop()@L8") in methods
    assert ("pkg/types.go::Server@L3", "pkg/serve.go::.Run()@L3") in methods
    assert ("pkg/serve.go", "pkg/serve.go::.Run()@L3") in store.relations("contains")
    assert store.relations("calls") == {
        ("pkg/serve.go::.Run()@L3", "pkg/types.go::.stop()@L8"),
        ("pkg/serve.go::.Run()@L3", "pkg/util.go::helper()@L5"),
    }
    inherits = store.relations("inherits")
    assert ("pkg/types.go::Server@L3", "pkg/util.go::Base@L3") in inherits
    assert ("pkg/types.go::Server@L3", "Mutex") in inherits


def test_a_go_method_follows_its_type_moving(store: Store) -> None:
    """An edit of the type's file relinks the package, the edge moves with it."""
    files = {"pkg/types.go": GO_TYPES, "pkg/serve.go": GO_SERVE}
    index(store, files)
    write_file(None, PROJECT, "pkg/types.go", "package alpha\n\n" + GO_TYPES)
    known = sorted(files)
    dirty = to_relink(
        known,
        set(known),
        {"pkg/types.go"},
        lambda paths: codegraph.importers(None, PROJECT, paths)
        | package_mates(paths, known),
    )
    assert dirty == set(known)
    link_files(None, PROJECT, known, dirty)
    assert ("pkg/types.go::Server@L5", "pkg/serve.go::.Run()@L3") in store.relations(
        "method"
    )
    assert not {edge for edge in store.relations("method") if edge[0].endswith("@L3")}


def test_a_go_test_file_sees_its_package_not_the_other_way(store: Store) -> None:
    """Tests call the package; the package never binds to a test helper."""
    index(
        store,
        {
            "pkg/util.go": GO_HELPER,
            "pkg/util_test.go": "package alpha\n\nfunc fixture() {}\n",
            "pkg/run_test.go": "package alpha\n\nfunc TestRun() { helper() }\n",
            "pkg/run.go": "package alpha\n\nfunc Run() { fixture() }\n",
        },
    )
    assert store.relations("calls") == {
        ("pkg/run_test.go::TestRun()@L3", "pkg/util.go::helper()@L5")
    }


def test_a_go_call_is_looked_up_in_the_package_its_alias_names(store: Store) -> None:
    """Two imported packages declare `Do`; the alias picks one, stdlib none."""
    index(
        store,
        {
            "beta/b.go": "package beta\n\nfunc Do() {}\n",
            "beta/b_test.go": "package beta\n\nfunc TestDo() {}\n",
            "gamma/g.go": "package gamma\n\nfunc Do() {}\n\nfunc Get() {}\n",
            "main.go": "package main\n\n"
            'import (\n\t"net/http"\n\t"example.com/alpha/beta"\n'
            '\tg "example.com/alpha/gamma"\n)\n\n'
            "func main() { beta.Do(); http.Get() }\n",
        },
        modules={"example.com/alpha": ""},
    )
    assert store.relations("calls") == {("main.go::main()@L9", "beta/b.go::Do()@L3")}
    assert store.relations("imports_from") == {
        ("main.go", "net/http"),
        ("main.go", "beta/b.go"),
        ("main.go", "gamma/g.go"),
    }
    assert store.nodes["net/http"]["type"] == "external_import"


def test_a_go_import_resolves_by_the_longest_module() -> None:
    """A nested module owns its own directories."""
    modules = {"example.com/alpha": "", "example.com/alpha/tools": "tools"}
    assert go_package_dir("example.com/alpha/beta", modules) == "beta"
    assert go_package_dir("example.com/alpha", modules) == ""
    assert go_package_dir("example.com/alpha/tools/lint", modules) == "tools/lint"
    assert go_package_dir("example.com/beta", modules) is None
    assert go_mod_candidates(["svc/cmd/main.go", "README.md"]) == [
        "go.mod",
        "svc/cmd/go.mod",
        "svc/go.mod",
    ]


PHP_LIB = """<?php
function helper() {
    return 1;
}
"""
PHP_ENTRY = """<?php
include_once('lib/config.php');
helper();
"""
PHP_STORE = """<?php
namespace Alpha\\Model;

class Store {
    public static function make() {}
    public function go() {}
}
"""
PHP_SERVICE = """<?php
namespace Alpha\\Service;

use Alpha\\Model\\Store as Box;

class Service extends Box implements \\Countable {
    public function run() {
        Box::make();
        parent::go();
        $this->count();
        new Box();
    }
    public function count() {}
}
"""


def test_php_is_read_by_the_own_extractor_alone() -> None:
    """PHP leaves the upstream extractor."""
    assert is_own_code("site/index.php")
    assert ".php" not in GRAPHIFYY_EXTENSIONS


def test_php_symbols_carry_their_labels_lines_and_owners() -> None:
    """Ids keep the upstream shape; a nested function is no symbol."""
    symbols, _ = PhpParser().extract_code(
        "<html><?php\n"
        "#[Attribute]\n"
        "class Thing {\n"
        "    public function run() {}\n"
        "}\n"
        "interface Shape {}\n"
        "trait Helps { function help() {} }\n"
        "enum Kind: string { case A = 'a'; }\n"
        "if (!function_exists('top')) {\n"
        "    function top() { function inner() {} }\n"
        "}\n"
        "$alpha = new class { function hidden() {} };\n"
    )
    assert [(one.key, one.kind, one.parent) for one in symbols] == [
        ("Thing@L2", "class", ""),
        (".run()@L4", "method", "Thing@L2"),
        ("Shape@L6", "interface", ""),
        ("Helps@L7", "class", ""),
        (".help()@L7", "method", "Helps@L7"),
        ("Kind@L8", "class", ""),
        ("top()@L10", "function", ""),
    ]


def test_php_references_are_qualified_by_namespace_and_use() -> None:
    """Every class and function name carries the namespace it resolves in."""
    _, references = PhpParser().extract_code(
        "<?php\n"
        "namespace Alpha;\n"
        "use Beta\\{Gamma, Delta as D};\n"
        "use function Beta\\assist;\n"
        "class Thing extends D {\n"
        "    use Helps;\n"
        "    function run() {\n"
        "        assist(); local(); \\Beta\\exact();\n"
        "        Gamma::make(); parent::go(); static::own(); $alpha?->call();\n"
        "        new Gamma(); new static();\n"
        "    }\n"
        "}\n"
        "local();\n"
    )
    found = {(one.scope, one.kind, one.name, one.receiver) for one in references}
    assert found == {
        ("", "imports", "Beta\\Gamma", "Gamma"),
        ("", "imports", "Beta\\Delta", "D"),
        ("Thing@L5", "declares", "Alpha\\Thing", "class"),
        ("Thing@L5", "inherits", "Beta\\Delta", ""),
        ("Thing@L5", "inherits", "Alpha\\Helps", ""),
        (".run()@L7", "calls", "Beta\\assist", "\\"),
        (".run()@L7", "calls", "Alpha\\local", ""),
        (".run()@L7", "calls", "Beta\\exact", "\\"),
        (".run()@L7", "calls", "make", "\\Beta\\Gamma"),
        (".run()@L7", "calls", "go", "\\Beta\\Delta"),
        (".run()@L7", "calls", "own", "self"),
        (".run()@L7", "calls", "call", "$alpha"),
        (".run()@L7", "calls", "Beta\\Gamma", "new"),
        ("", "calls", "Alpha\\local", ""),
    }


def test_php_includes_keep_what_the_source_spells() -> None:
    """A directory constant anchors the path; any other prefix leaves the tail."""
    _, references = PhpParser().extract_code(
        "<?php\n"
        "require_once __DIR__ . '/lib/a.php';\n"
        'include_once(dirname(__FILE__) . "/b.php");\n'
        "require dirname(__DIR__, 2) . '/c.php';\n"
        "include $root . '/libs/d.php';\n"
        'include "$root/e.php";\n'
        "include 'lib/f.php';\n"
        "include $file;\n"
    )
    assert {one.name for one in references if one.kind == "imports"} == {
        "./lib/a.php",
        "./b.php",
        "../../c.php",
        "libs/d.php",
        "e.php",
        "lib/f.php",
    }


def test_a_php_include_resolves_relative_then_nearest() -> None:
    """The include path is not in the tree, so the nearest file ending so wins."""
    known = {
        "alpha/lib/config.php",
        "alpha/lib/item.php",
        "beta/lib/config.php",
        "alpha/index.php",
    }
    names = basename_index(known)
    resolved = resolve_php_include("lib/config.php", "alpha/index.php", known, names)
    assert resolved == "alpha/lib/config.php"
    resolved = resolve_php_include("lib/config.php", "alpha/lib/item.php", known, names)
    assert resolved == "alpha/lib/config.php"
    assert resolve_php_include("lib/config.php", "top.php", known, names) is None
    resolved = resolve_php_include(
        "/var/www/alpha/lib/item.php", "beta/x.php", known, names
    )
    assert resolved == "alpha/lib/item.php"


def test_a_php_call_reaches_a_function_its_file_never_includes(store: Store) -> None:
    """Procedural PHP sees every function its entry script loaded."""
    index(
        store,
        {
            "alpha/lib/config.php": PHP_LIB,
            "alpha/lib/item.php": "<?php\nfunction item() { return helper(); }\n",
            "alpha/index.php": PHP_ENTRY,
        },
    )
    assert store.relations("calls") == {
        ("alpha/index.php", "alpha/lib/config.php::helper()@L2"),
        ("alpha/lib/item.php::item()@L2", "alpha/lib/config.php::helper()@L2"),
    }
    assert ("alpha/index.php", "alpha/lib/config.php") in store.relations(
        "imports_from"
    )


def test_a_php_name_declared_twice_picks_the_nearest(store: Store) -> None:
    """Two sites declare one function; a file between them resolves neither."""
    index(
        store,
        {
            "alpha/lib/config.php": PHP_LIB,
            "beta/lib/config.php": PHP_LIB,
            "beta/run.php": "<?php\nhelper();\n",
            "top.php": "<?php\nhelper();\n",
        },
    )
    assert store.relations("calls") == {
        ("beta/run.php", "beta/lib/config.php::helper()@L2")
    }


def test_php_classes_resolve_through_namespaces(store: Store) -> None:
    """An alias, a static call, parent and new reach the class declaring them."""
    index(store, {"src/Model/Store.php": PHP_STORE, "src/Service.php": PHP_SERVICE})
    store_id = "src/Model/Store.php::Store@L4"
    run = "src/Service.php::.run()@L7"
    assert store.relations("inherits") == {
        ("src/Service.php::Service@L6", store_id),
        ("src/Service.php::Service@L6", "Countable"),
    }
    assert store.nodes["Countable"]["type"] == "external_symbol"
    assert store.relations("calls") == {
        (run, "src/Model/Store.php::.make()@L5"),
        (run, "src/Model/Store.php::.go()@L6"),
        (run, "src/Service.php::.count()@L13"),
        (run, store_id),
    }
    assert store.relations("imports_from") == {
        ("src/Service.php", "src/Model/Store.php")
    }


def test_a_php_caller_follows_a_moved_function(store: Store) -> None:
    """The caller includes nothing, yet an edit of the callee relinks it."""
    files = {"alpha/lib/item.php": PHP_LIB, "alpha/run.php": "<?php\nhelper();\n"}
    index(store, files)
    write_file(None, PROJECT, "alpha/lib/item.php", "<?php\n\n" + PHP_LIB[6:])
    known = sorted(files)
    dirty = to_relink(
        known,
        set(known),
        {"alpha/lib/item.php"},
        lambda paths: php_callers(None, PROJECT, paths),
    )
    assert dirty == set(known)
    link_files(None, PROJECT, known, dirty)
    assert store.relations("calls") == {
        ("alpha/run.php", "alpha/lib/item.php::helper()@L3")
    }


RUST_TYPES = """use std::fmt;

/// A server.
#[derive(Debug)]
pub struct Server {
    port: u16,
}

impl Server {
    pub fn new() -> Self {
        Self::bind();
        Self { port: 0 }
    }

    fn bind() {}
}

pub trait Handler: fmt::Display {
    fn serve(&self);
    fn run(&self) {
        self.serve();
    }
}

impl fmt::Display for Server {
    fn fmt(&self, f: &mut fmt::Formatter) -> fmt::Result {
        write!(f, "{}", self.port)
    }
}
"""
RUST_OPS = """use crate::types::Server;
use crate::util::*;

impl Server {
    pub fn stop(&self) {
        self.close();
        helper();
    }

    fn close(&self) {}
}

#[cfg(test)]
mod tests {
    use super::*;

    fn check() {
        super::helper();
    }
}
"""
RUST_LIB = "pub mod types;\nmod ops;\nmod util;\n\npub use types::Server;\n"
RUST_MAIN = """use alpha::Server;

fn main() {
    let server = Server::new();
    server.stop();
    alpha::types::Server::bind();
}
"""
RUST_CRATE = {
    "src/lib.rs": RUST_LIB,
    "src/types.rs": RUST_TYPES,
    "src/ops.rs": RUST_OPS,
    "src/util.rs": "pub fn helper() {}\n",
    "src/main.rs": RUST_MAIN,
}
RUST_CRATES = [RustCrate("", "alpha", "src/lib.rs")]


def test_rust_is_read_by_the_own_extractor_alone() -> None:
    """Rust leaves the upstream extractor."""
    assert is_own_code("src/lib.rs")
    assert ".rs" not in GRAPHIFYY_EXTENSIONS
    assert ".rs" in OWN_CODE_EXTENSIONS


def test_rust_symbols_carry_their_labels_lines_and_owners() -> None:
    """Methods hang off the type an impl names; trait methods off the trait."""
    symbols, references = RustParser().extract_code(RUST_TYPES)
    assert [(one.key, one.kind, one.parent) for one in symbols] == [
        ("Server@L5", "class", ""),
        (".new()@L10", "method", "Server@L5"),
        (".bind()@L15", "method", "Server@L5"),
        ("Handler@L18", "interface", ""),
        (".serve()@L19", "method", "Handler@L18"),
        (".run()@L20", "method", "Handler@L18"),
        (".fmt()@L26", "method", "Server@L5"),
    ]
    assert entity_summary(RUST_TYPES.splitlines(), 5) == "A server."
    found = {(one.scope, one.kind, one.name, one.receiver) for one in references}
    assert found == {
        ("", "imports", "std::fmt", "fmt"),
        (".new()@L10", "calls", "bind", "self"),
        (".run()@L20", "calls", "serve", "self"),
        ("Handler@L18", "inherits", "Display", "fmt"),
        ("Server@L5", "inherits", "Display", "fmt"),
    }


def test_rust_references_keep_their_alias_and_receiver() -> None:
    """A foreign impl names its type; inline modules read from the file."""
    symbols, references = RustParser().extract_code(RUST_OPS)
    assert [(one.key, one.parent) for one in symbols] == [
        (".stop()@L5", ""),
        (".close()@L10", ""),
        ("check()@L17", ""),
    ]
    found = {(one.scope, one.kind, one.name, one.receiver) for one in references}
    assert found == {
        ("", "imports", "crate::types::Server", "Server"),
        ("", "imports", "crate::util", "*"),
        ("", "imports", "self", "*"),
        (".stop()@L5", "receiver", "Server", ""),
        (".close()@L10", "receiver", "Server", ""),
        (".stop()@L5", "calls", "close", "self"),
        (".stop()@L5", "calls", "helper", ""),
        ("check()@L17", "calls", "helper", ""),
    }


def test_rust_use_trees_and_calls_are_split_into_paths() -> None:
    """Every leaf of a use tree is bound to its name; calls keep their path."""
    _, references = RustParser().extract_code(
        "use crate::a::{b, c::{D, e as f}, self};\n"
        "extern crate beta as gamma;\n"
        "mod delta;\n"
        "\n"
        "fn run() {\n"
        "    a::b::go();\n"
        "    Vec::<u8>::new();\n"
        "    item.call();\n"
        "    b::Thing { x: 1 };\n"
        '    println!("{}", hidden());\n'
        "}\n"
    )
    found = {(one.scope, one.kind, one.name, one.receiver) for one in references}
    assert found == {
        ("", "imports", "crate::a::b", "b"),
        ("", "imports", "crate::a::c::D", "D"),
        ("", "imports", "crate::a::c::e", "f"),
        ("", "imports", "crate::a", "a"),
        ("", "imports", "beta", "gamma"),
        ("", "imports", "self::delta", "delta"),
        ("run()@L5", "calls", "go", "a::b"),
        ("run()@L5", "calls", "new", "Vec"),
        ("run()@L5", "calls", "call", "."),
        ("run()@L5", "calls", "Thing", "b"),
    }


def test_a_rust_path_resolves_through_the_module_tree() -> None:
    """Child modules sit beside a root or mod.rs, under `a/` for a.rs."""
    layout = RustLayout(
        {
            "src/lib.rs",
            "src/main.rs",
            "src/a.rs",
            "src/a/b.rs",
            "src/c/mod.rs",
            "src/c/d.rs",
            "src/bin/tool.rs",
            "tests/it.rs",
            "tests/common/mod.rs",
        },
        RUST_CRATES,
    )
    assert layout.child("src/lib.rs", "a") == "src/a.rs"
    assert layout.child("src/a.rs", "b") == "src/a/b.rs"
    assert layout.child("src/c/mod.rs", "d") == "src/c/d.rs"
    assert layout.child("tests/it.rs", "common") == "tests/common/mod.rs"
    assert layout.parent("src/a/b.rs") == "src/a.rs"
    assert layout.parent("src/c/mod.rs") == "src/lib.rs"
    assert layout.parent("src/bin/tool.rs") is None
    assert layout.resolve("src/a/b.rs", ["crate", "c", "d", "Thing"]) == (
        "src/c/d.rs",
        ["Thing"],
    )
    assert layout.resolve("src/a/b.rs", ["super", "super", "c"]) == (
        "src/c/mod.rs",
        [],
    )
    assert layout.resolve("src/bin/tool.rs", ["alpha", "a", "Item"]) == (
        "src/a.rs",
        ["Item"],
    )
    assert layout.resolve("src/a.rs", ["serde", "Deserialize"]) == (None, [])


def test_a_cargo_manifest_names_its_crate() -> None:
    """The name `use` spells, and no crate for a workspace or a broken file."""
    assert cargo_crate(
        "crates/beta-core/Cargo.toml", '[package]\nname = "beta-core"\n'
    ) == RustCrate("crates/beta-core", "beta_core", "crates/beta-core/src/lib.rs")
    assert cargo_crate(
        "Cargo.toml",
        '[package]\nname = "alpha"\n[lib]\nname = "gamma"\npath = "lib.rs"\n',
    ) == RustCrate("", "gamma", "lib.rs")
    assert cargo_crate("Cargo.toml", '[workspace]\nmembers = ["crates/*"]\n') is None
    assert cargo_crate("Cargo.toml", "[package\n") is None


def test_rust_calls_resolve_across_modules_and_crates(store: Store) -> None:
    """Paths, globs, re-exports and impls in other files all reach the method."""
    index(store, RUST_CRATE, crates=RUST_CRATES)
    methods = store.relations("method")
    assert ("src/types.rs::Server@L5", "src/types.rs::.fmt()@L26") in methods
    assert ("src/types.rs::Server@L5", "src/ops.rs::.stop()@L5") in methods
    assert ("src/types.rs::Server@L5", "src/ops.rs::.close()@L10") in methods
    assert ("src/ops.rs", "src/ops.rs::.stop()@L5") in store.relations("contains")
    assert store.relations("calls") == {
        ("src/types.rs::.new()@L10", "src/types.rs::.bind()@L15"),
        ("src/types.rs::.run()@L20", "src/types.rs::.serve()@L19"),
        ("src/ops.rs::.stop()@L5", "src/ops.rs::.close()@L10"),
        ("src/ops.rs::.stop()@L5", "src/util.rs::helper()@L1"),
        ("src/ops.rs::check()@L17", "src/util.rs::helper()@L1"),
        ("src/main.rs::main()@L3", "src/types.rs::.new()@L10"),
        ("src/main.rs::main()@L3", "src/types.rs::.bind()@L15"),
    }
    assert store.relations("imports_from") == {
        ("src/lib.rs", "src/types.rs"),
        ("src/lib.rs", "src/ops.rs"),
        ("src/lib.rs", "src/util.rs"),
        ("src/types.rs", "std"),
        ("src/ops.rs", "src/types.rs"),
        ("src/ops.rs", "src/util.rs"),
        ("src/main.rs", "src/lib.rs"),
    }
    assert store.nodes["std"]["type"] == "external_import"
    assert store.relations("inherits") == {
        ("src/types.rs::Server@L5", "Display"),
        ("src/types.rs::Handler@L18", "Display"),
    }


def test_a_rust_caller_follows_a_moved_method(store: Store) -> None:
    """main.rs imports only the crate root, yet an edit of the impl relinks it."""
    index(store, RUST_CRATE, crates=RUST_CRATES)
    write_file(None, PROJECT, "src/types.rs", "\n" + RUST_TYPES)
    known = sorted(RUST_CRATE)
    dirty = to_relink(
        known,
        set(known),
        {"src/types.rs"},
        lambda paths: codegraph.importers(None, PROJECT, paths)
        | rust_callers(None, PROJECT, paths),
    )
    assert "src/main.rs" in dirty
    link_files(None, PROJECT, known, dirty, crates=RUST_CRATES)
    assert ("src/main.rs::main()@L3", "src/types.rs::.new()@L11") in store.relations(
        "calls"
    )
    assert ("src/types.rs::Server@L6", "src/ops.rs::.stop()@L5") in store.relations(
        "method"
    )


JAVA_BASE = """package alpha.model;

public abstract class Base {
    protected void log() {}

    public static Base of() {
        return null;
    }
}
"""
JAVA_STORE = """package alpha.model;

import java.util.List;
import static alpha.util.Tools.helper;

/** A store. */
@Entity
public class Store extends Base implements Runnable {
    private final List<String> items = List.of();
    private Index index = new Index();

    public Store() {
        this(0);
    }

    Store(int size) {
        log();
    }

    @Override
    public void run() {
        index.rebuild();
        this.save(items);
        super.log();
        helper();
    }

    void save(List<String> xs) {
        xs.forEach(this::save);
    }

    void save(String x) {}

    static class Index {
        void rebuild() {
            flush();
            save("a");
        }

        void flush() {}
    }

    enum Kind { A, B; boolean on() { return true; } }

    record Pair(String key, Index value) {
        Pair {
            value.flush();
        }
    }
}
"""
JAVA_TOOLS = """package alpha.util;

public final class Tools {
    public static void helper() {}

    public static int count() {
        return 0;
    }
}
"""
JAVA_APP = """package alpha.app;

import alpha.model.*;
import alpha.util.Tools;

public class App {
    public static void main(String[] args) {
        Store store = new Store();
        store.run();
        var pair = new Store.Pair("a", null);
        Tools.count();
        Base.of();
        Store.Index index = new Store.Index();
        index.flush();
        System.out.println(pair);
    }
}
"""
JAVA_TREE = {
    "src/alpha/model/Base.java": JAVA_BASE,
    "src/alpha/model/Store.java": JAVA_STORE,
    "src/alpha/util/Tools.java": JAVA_TOOLS,
    "src/alpha/app/App.java": JAVA_APP,
}
JAVA_BASE_PATH = "src/alpha/model/Base.java"
JAVA_STORE_PATH = "src/alpha/model/Store.java"
JAVA_TOOLS_PATH = "src/alpha/util/Tools.java"
JAVA_APP_PATH = "src/alpha/app/App.java"


def test_java_is_read_by_the_own_extractor_alone() -> None:
    """Java leaves the upstream extractor."""
    assert is_own_code("src/alpha/App.java")
    assert ".java" not in GRAPHIFYY_EXTENSIONS
    assert ".java" in OWN_CODE_EXTENSIONS


def test_java_symbols_carry_their_labels_lines_and_owners() -> None:
    """Ids keep the upstream shape; nested types, enums and records join in."""
    symbols, _ = JavaParser().extract_code(JAVA_STORE)
    assert [(one.key, one.kind, one.parent) for one in symbols] == [
        ("Store@L7", "class", ""),
        (".Store()@L12", "method", "Store@L7"),
        (".Store()@L16", "method", "Store@L7"),
        (".run()@L20", "method", "Store@L7"),
        (".save()@L28", "method", "Store@L7"),
        (".save()@L32", "method", "Store@L7"),
        ("Index@L34", "class", "Store@L7"),
        (".rebuild()@L35", "method", "Index@L34"),
        (".flush()@L40", "method", "Index@L34"),
        ("Kind@L43", "class", "Store@L7"),
        (".on()@L43", "method", "Kind@L43"),
        ("Pair@L45", "class", "Store@L7"),
        (".Pair()@L46", "method", "Pair@L45"),
    ]
    assert entity_summary(JAVA_STORE.splitlines(), 7) == "A store."


def test_java_symbols_leave_anonymous_classes_out() -> None:
    """An interface is one; a method of an anonymous class calls for its own."""
    symbols, references = JavaParser().extract_code(
        "interface Shape extends Comparable<Shape> {\n"
        "    double area();\n"
        "    default Runnable task() {\n"
        "        return new Runnable() { public void run() { area(); } };\n"
        "    }\n"
        "}\n"
        "@interface Marker {}\n"
    )
    assert [(one.key, one.kind, one.parent) for one in symbols] == [
        ("Shape@L1", "interface", ""),
        (".area()@L2", "method", "Shape@L1"),
        (".task()@L3", "method", "Shape@L1"),
        ("Marker@L7", "interface", ""),
    ]
    found = {(one.scope, one.kind, one.name, one.receiver) for one in references}
    assert found == {
        ("Shape@L1", "declares", "Shape", "type"),
        ("Shape@L1", "inherits", "Comparable", ""),
        (".task()@L3", "calls", "Runnable", "new"),
        (".task()@L3", "calls", "area", ""),
        ("Marker@L7", "declares", "Marker", "type"),
    }


def test_java_references_carry_the_receivers_type() -> None:
    """A variable, a field or a component stands for the type it was declared."""
    _, references = JavaParser().extract_code(JAVA_STORE)
    found = {(one.scope, one.kind, one.name, one.receiver) for one in references}
    assert found == {
        ("", "declares", "alpha.model", "package"),
        ("", "imports", "java.util.List", "List"),
        ("", "imports", "alpha.util.Tools.helper", "static"),
        ("Store@L7", "declares", "alpha.model.Store", "type"),
        ("Store@L7", "inherits", "Base", ""),
        ("Store@L7", "inherits", "Runnable", ""),
        ("Store@L7", "calls", "of", "List"),
        ("Store@L7", "calls", "Index", "new"),
        (".Store()@L16", "calls", "log", ""),
        (".run()@L20", "calls", "rebuild", "Index"),
        (".run()@L20", "calls", "save", "self"),
        (".run()@L20", "calls", "log", "super"),
        (".run()@L20", "calls", "helper", ""),
        (".save()@L28", "calls", "forEach", "List"),
        (".save()@L28", "calls", "save", "self"),
        ("Index@L34", "declares", "alpha.model.Store.Index", "type"),
        (".rebuild()@L35", "calls", "flush", ""),
        (".rebuild()@L35", "calls", "save", ""),
        ("Kind@L43", "declares", "alpha.model.Store.Kind", "type"),
        ("Pair@L45", "declares", "alpha.model.Store.Pair", "type"),
        (".Pair()@L46", "calls", "flush", "Index"),
    }
    _, references = JavaParser().extract_code(JAVA_APP)
    found = {(one.kind, one.name, one.receiver) for one in references}
    assert found == {
        ("declares", "alpha.app", "package"),
        ("imports", "alpha.model", "*"),
        ("imports", "alpha.util.Tools", "Tools"),
        ("declares", "alpha.app.App", "type"),
        ("calls", "Store", "new"),
        ("calls", "run", "Store"),
        ("calls", "Store.Pair", "new"),
        ("calls", "count", "Tools"),
        ("calls", "of", "Base"),
        ("calls", "Store.Index", "new"),
        ("calls", "flush", "Store.Index"),
        ("calls", "println", "."),
    }


def test_java_calls_resolve_through_packages_imports_and_bases(
    store: Store,
) -> None:
    """Package mates, on-demand and static imports, nesting and bases all resolve."""
    index(store, JAVA_TREE)
    base, model, tools, app = (
        JAVA_BASE_PATH,
        JAVA_STORE_PATH,
        JAVA_TOOLS_PATH,
        JAVA_APP_PATH,
    )
    assert store.relations("calls") == {
        (f"{model}::Store@L7", f"{model}::Index@L34"),
        (f"{model}::.Store()@L16", f"{base}::.log()@L4"),
        (f"{model}::.run()@L20", f"{model}::.rebuild()@L35"),
        (f"{model}::.run()@L20", f"{model}::.save()@L28"),
        (f"{model}::.run()@L20", f"{base}::.log()@L4"),
        (f"{model}::.run()@L20", f"{tools}::.helper()@L4"),
        (f"{model}::.rebuild()@L35", f"{model}::.flush()@L40"),
        (f"{model}::.rebuild()@L35", f"{model}::.save()@L28"),
        (f"{model}::.Pair()@L46", f"{model}::.flush()@L40"),
        (f"{app}::.main()@L7", f"{model}::Store@L7"),
        (f"{app}::.main()@L7", f"{model}::.run()@L20"),
        (f"{app}::.main()@L7", f"{model}::Pair@L45"),
        (f"{app}::.main()@L7", f"{tools}::.count()@L6"),
        (f"{app}::.main()@L7", f"{base}::.of()@L6"),
        (f"{app}::.main()@L7", f"{model}::Index@L34"),
        (f"{app}::.main()@L7", f"{model}::.flush()@L40"),
    }
    assert store.relations("imports_from") == {
        (model, "java.util.List"),
        (model, tools),
        (app, base),
        (app, model),
        (app, tools),
    }
    assert store.nodes["java.util.List"]["type"] == "external_import"
    assert store.relations("inherits") == {
        (f"{model}::Store@L7", f"{base}::Base@L3"),
        (f"{model}::Store@L7", "Runnable"),
    }
    assert store.nodes["Runnable"]["type"] == "external_symbol"
    assert (f"{model}::Store@L7", f"{model}::Index@L34") in store.relations("contains")


def test_a_java_package_mate_follows_a_moved_base(store: Store) -> None:
    """Store uses Base without an import, yet an edit of Base relinks it."""
    index(store, JAVA_TREE)
    write_file(None, PROJECT, JAVA_BASE_PATH, "\n" + JAVA_BASE)
    known = sorted(JAVA_TREE)
    dirty = to_relink(
        known,
        set(known),
        {JAVA_BASE_PATH},
        lambda paths: codegraph.importers(None, PROJECT, paths)
        | jvm_callers(None, PROJECT, paths),
    )
    assert dirty == set(known) - {JAVA_TOOLS_PATH}
    link_files(None, PROJECT, known, dirty)
    calls = store.relations("calls")
    assert (
        f"{JAVA_STORE_PATH}::.Store()@L16",
        f"{JAVA_BASE_PATH}::.log()@L5",
    ) in calls
    assert (f"{JAVA_APP_PATH}::.main()@L7", f"{JAVA_BASE_PATH}::.of()@L7") in calls
    assert (
        f"{JAVA_STORE_PATH}::Store@L7",
        f"{JAVA_BASE_PATH}::Base@L4",
    ) in store.relations("inherits")


KOTLIN_BASE = """package alpha.model

open class Base(val name: String) {
    open fun log() {}

    companion object {
        fun of(): Base = Base("a")
    }
}
"""
KOTLIN_STORE = """package alpha.model

import alpha.util.helper
import alpha.util.Tools as T

@Suppress("unused")
class Store(private val index: Index) : Base("store"), Runnable {
    private val items = mutableListOf<String>()

    override fun run() {
        index.rebuild()
        this.save("a")
        super.log()
        helper()
        T.count()
    }

    fun save(x: String) {}

    fun save(x: Int) {}

    class Index {
        fun rebuild() {
            flush()
        }

        fun flush() {}
    }

    enum class Kind {
        A,
        B;

        fun on() = true
    }
}
"""
KOTLIN_TOOLS = """package alpha.util

fun helper() {}

fun helper(x: Int) {}

fun String.shout(): String = this

object Tools {
    fun count(): Int = 0
}
"""
KOTLIN_APP = """package alpha.app

import alpha.model.*
import alpha.util.shout
import alpha.legacy.Legacy

fun main() {
    val store = Store(Store.Index())
    store.run()
    Base.of()
    "a".shout()
    alpha.model.Store.Index().flush()
    val legacy = Legacy()
    legacy.ping()
    println(store)
}
"""
KOTLIN_LEGACY = """package alpha.legacy;

import alpha.model.Base;

public class Legacy {
    public void ping() {
        new Base("x").log();
    }
}
"""
KOTLIN_BASE_PATH = "src/alpha/model/Base.kt"
KOTLIN_STORE_PATH = "src/alpha/model/Store.kt"
KOTLIN_TOOLS_PATH = "src/alpha/util/Tools.kt"
KOTLIN_APP_PATH = "src/alpha/app/App.kt"
KOTLIN_LEGACY_PATH = "src/alpha/legacy/Legacy.java"
KOTLIN_TREE = {
    KOTLIN_BASE_PATH: KOTLIN_BASE,
    KOTLIN_STORE_PATH: KOTLIN_STORE,
    KOTLIN_TOOLS_PATH: KOTLIN_TOOLS,
    KOTLIN_APP_PATH: KOTLIN_APP,
    KOTLIN_LEGACY_PATH: KOTLIN_LEGACY,
}


def test_kotlin_is_read_by_the_own_extractor_alone() -> None:
    """Kotlin leaves the upstream extractor."""
    assert is_own_code("src/alpha/App.kt")
    assert ".kt" not in GRAPHIFYY_EXTENSIONS
    assert ".kt" in OWN_CODE_EXTENSIONS


def test_kotlin_symbols_carry_their_labels_lines_and_owners() -> None:
    """Ids keep the upstream shape; companion members belong to the class."""
    symbols, _ = KotlinParser().extract_code(KOTLIN_STORE)
    assert [(one.key, one.kind, one.parent) for one in symbols] == [
        ("Store@L6", "class", ""),
        (".run()@L10", "method", "Store@L6"),
        (".save()@L18", "method", "Store@L6"),
        (".save()@L20", "method", "Store@L6"),
        ("Index@L22", "class", "Store@L6"),
        (".rebuild()@L23", "method", "Index@L22"),
        (".flush()@L27", "method", "Index@L22"),
        ("Kind@L30", "class", "Store@L6"),
        (".on()@L34", "method", "Kind@L30"),
    ]
    symbols, _ = KotlinParser().extract_code(KOTLIN_BASE)
    assert [(one.key, one.kind, one.parent) for one in symbols] == [
        ("Base@L3", "class", ""),
        (".log()@L4", "method", "Base@L3"),
        (".of()@L7", "method", "Base@L3"),
    ]


def test_kotlin_symbols_leave_object_literals_and_locals_out() -> None:
    """An object literal's members and a local function call for their holder."""
    symbols, references = KotlinParser().extract_code(
        "interface Shape : Comparable<Shape> {\n"
        "    fun area(): Double\n"
        "\n"
        "    fun task(): Runnable = object : Runnable {\n"
        "        override fun run() {\n"
        "            area()\n"
        "        }\n"
        "    }\n"
        "}\n"
        "\n"
        "sealed interface Node\n"
        "\n"
        "data class Point(val x: Int)\n"
        "\n"
        "fun Point.norm(): Int = x\n"
        "\n"
        "fun outer() {\n"
        "    fun inner() = norm()\n"
        "    inner()\n"
        "}\n"
    )
    assert [(one.key, one.kind, one.parent) for one in symbols] == [
        ("Shape@L1", "interface", ""),
        (".area()@L2", "method", "Shape@L1"),
        (".task()@L4", "method", "Shape@L1"),
        ("Node@L11", "interface", ""),
        ("Point@L13", "class", ""),
        ("norm()@L15", "function", ""),
        ("outer()@L17", "function", ""),
    ]
    found = {(one.scope, one.kind, one.name, one.receiver) for one in references}
    assert found == {
        ("Shape@L1", "declares", "Shape", "type"),
        ("Shape@L1", "inherits", "Comparable", ""),
        (".task()@L4", "calls", "area", ""),
        ("Node@L11", "declares", "Node", "type"),
        ("Point@L13", "declares", "Point", "type"),
        ("norm()@L15", "declares", "norm", "function"),
        ("norm()@L15", "declares", "norm", "extension"),
        ("outer()@L17", "declares", "outer", "function"),
        ("outer()@L17", "calls", "norm", ""),
        ("outer()@L17", "calls", "inner", ""),
    }


def test_kotlin_references_carry_the_receivers_type() -> None:
    """A parameter, a property or a constructed value stands for its type."""
    _, references = KotlinParser().extract_code(KOTLIN_STORE)
    found = {(one.scope, one.kind, one.name, one.receiver) for one in references}
    assert found == {
        ("", "declares", "alpha.model", "package"),
        ("", "imports", "alpha.util.helper", "helper"),
        ("", "imports", "alpha.util.Tools", "T"),
        ("Store@L6", "declares", "alpha.model.Store", "type"),
        ("Store@L6", "inherits", "Base", ""),
        ("Store@L6", "inherits", "Runnable", ""),
        ("Store@L6", "calls", "mutableListOf", ""),
        (".run()@L10", "calls", "rebuild", "Index"),
        (".run()@L10", "calls", "save", "self"),
        (".run()@L10", "calls", "log", "super"),
        (".run()@L10", "calls", "helper", ""),
        (".run()@L10", "calls", "count", "T"),
        ("Index@L22", "declares", "alpha.model.Store.Index", "type"),
        (".rebuild()@L23", "calls", "flush", ""),
        ("Kind@L30", "declares", "alpha.model.Store.Kind", "type"),
    }
    _, references = KotlinParser().extract_code(KOTLIN_APP)
    found = {(one.kind, one.name, one.receiver) for one in references}
    assert found == {
        ("declares", "alpha.app", "package"),
        ("imports", "alpha.model", "*"),
        ("imports", "alpha.util.shout", "shout"),
        ("imports", "alpha.legacy.Legacy", "Legacy"),
        ("declares", "alpha.app.main", "function"),
        ("calls", "Store", ""),
        ("calls", "Store.Index", "new"),
        ("calls", "run", "Store"),
        ("calls", "of", "Base"),
        ("calls", "shout", "."),
        ("calls", "alpha.model.Store.Index", "new"),
        ("calls", "flush", "alpha.model.Store.Index"),
        ("calls", "Legacy", ""),
        ("calls", "ping", "Legacy"),
        ("calls", "println", ""),
    }


def test_kotlin_calls_resolve_through_packages_imports_and_java(
    store: Store,
) -> None:
    """Package mates, aliases, top-level and extension functions, Java both ways."""
    index(store, KOTLIN_TREE)
    base, model, tools, app, legacy = (
        KOTLIN_BASE_PATH,
        KOTLIN_STORE_PATH,
        KOTLIN_TOOLS_PATH,
        KOTLIN_APP_PATH,
        KOTLIN_LEGACY_PATH,
    )
    assert store.relations("calls") == {
        (f"{base}::.of()@L7", f"{base}::Base@L3"),
        (f"{model}::.run()@L10", f"{model}::.rebuild()@L23"),
        (f"{model}::.run()@L10", f"{model}::.save()@L18"),
        (f"{model}::.run()@L10", f"{base}::.log()@L4"),
        (f"{model}::.run()@L10", f"{tools}::helper()@L3"),
        (f"{model}::.run()@L10", f"{tools}::.count()@L10"),
        (f"{model}::.rebuild()@L23", f"{model}::.flush()@L27"),
        (f"{app}::main()@L7", f"{model}::Store@L6"),
        (f"{app}::main()@L7", f"{model}::Index@L22"),
        (f"{app}::main()@L7", f"{model}::.run()@L10"),
        (f"{app}::main()@L7", f"{base}::.of()@L7"),
        (f"{app}::main()@L7", f"{tools}::shout()@L7"),
        (f"{app}::main()@L7", f"{model}::.flush()@L27"),
        (f"{app}::main()@L7", f"{legacy}::Legacy@L5"),
        (f"{app}::main()@L7", f"{legacy}::.ping()@L6"),
        (f"{legacy}::.ping()@L6", f"{base}::Base@L3"),
        (f"{legacy}::.ping()@L6", f"{base}::.log()@L4"),
    }
    assert store.relations("imports_from") == {
        (model, tools),
        (app, base),
        (app, model),
        (app, tools),
        (app, legacy),
        (legacy, base),
    }
    assert store.relations("inherits") == {
        (f"{model}::Store@L6", f"{base}::Base@L3"),
        (f"{model}::Store@L6", "Runnable"),
    }
    assert store.nodes["Runnable"]["type"] == "external_symbol"
    assert (f"{base}::Base@L3", f"{base}::.of()@L7") in store.relations("method")


def test_a_kotlin_package_mate_follows_a_moved_base(store: Store) -> None:
    """Store uses Base without an import, yet an edit of Base relinks it."""
    index(store, KOTLIN_TREE)
    write_file(None, PROJECT, KOTLIN_BASE_PATH, "\n" + KOTLIN_BASE)
    known = sorted(KOTLIN_TREE)
    dirty = to_relink(
        known,
        set(known),
        {KOTLIN_BASE_PATH},
        lambda paths: codegraph.importers(None, PROJECT, paths)
        | jvm_callers(None, PROJECT, paths),
    )
    assert dirty == set(known) - {KOTLIN_TOOLS_PATH}
    link_files(None, PROJECT, known, dirty)
    calls = store.relations("calls")
    assert (
        f"{KOTLIN_STORE_PATH}::.run()@L10",
        f"{KOTLIN_BASE_PATH}::.log()@L5",
    ) in calls
    assert (f"{KOTLIN_APP_PATH}::main()@L7", f"{KOTLIN_BASE_PATH}::.of()@L8") in calls
    assert (
        f"{KOTLIN_LEGACY_PATH}::.ping()@L6",
        f"{KOTLIN_BASE_PATH}::Base@L4",
    ) in calls
    assert (
        f"{KOTLIN_STORE_PATH}::Store@L6",
        f"{KOTLIN_BASE_PATH}::Base@L4",
    ) in store.relations("inherits")
