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
    link_files,
    package_mates,
    php_callers,
    to_relink,
    write_file,
)
from enggraph.indexer.parsers.languages import (
    GoParser,
    JavaScriptParser,
    PhpParser,
    PythonParser,
    TSXParser,
    TypeScriptParser,
)
from enggraph.indexer.resolution import (
    basename_index,
    go_mod_candidates,
    go_package_dir,
    resolve_php_include,
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
) -> None:
    """Run both passes over these files, as a run that read them would."""
    for rel_path, content in files.items():
        write_file(None, PROJECT, rel_path, content)
    known = sorted({*files, *(path for path in store.references), *others})
    link_files(None, PROJECT, known, set(files) if dirty is None else dirty, modules)


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
