"""The tree interface, and that nothing reads a tree around it."""

from __future__ import annotations

import ast
import hashlib
import io
import json
import pathlib
import tarfile
import threading
import time
from collections.abc import Callable
from typing import Any

import psycopg2
import pytest

from enggraph.core import trees

PACKAGES = pathlib.Path(__file__).resolve().parents[2]
FORBIDDEN_NAMES = {"CODE_ROOT", "MountTree"}
FORBIDDEN_MODULES = {"watchfiles"}
VECTORS = pathlib.Path(__file__).parent / "data" / "tree_hash_vectors.json"


def _source(approved: bool = True) -> trees.Source:
    return trees.Source("alpha", "agent-1", "alpha-export", "/srv/alpha", approved)


class FakeTransport:
    """An agent serving a directory, answering as protocol v1 does."""

    def __init__(self, root: pathlib.Path) -> None:
        """Serve a directory of the test's."""
        self.root = root
        self.online = True
        self.asked: list[tuple[str, dict[str, Any]]] = []
        self.listener: trees.Listener | None = None
        self.refusal: trees.TreeRefused | None = None

    def connected(self, source: trees.Source) -> bool:
        """Say whether the agent is there."""
        return self.online

    def _check(self, op: str, args: dict[str, Any]) -> None:
        assert args["export"] == "alpha-export"
        self.asked.append((op, args))
        if not self.online:
            raise trees.TreeUnavailable("offline")
        if self.refusal is not None:
            raise self.refusal

    def call(
        self, source: trees.Source, op: str, args: dict[str, Any]
    ) -> dict[str, Any]:
        """Answer the operations that answer with JSON."""
        self._check(op, args)
        if op == "walk":
            base = self.root / args["top"]
            entries, dirs = [], []
            for one in sorted(base.iterdir()) if base.is_dir() else []:
                rel = one.relative_to(self.root).as_posix()
                if one.is_dir():
                    dirs.append(rel)
                else:
                    digest = hashlib.md5(one.read_bytes()).hexdigest()
                    md5 = digest if args["md5"] else None
                    entries.append({"path": rel, "size": 1, "mtime": 1, "md5": md5})
            return {"entries": entries, "dirs": dirs, "skipped": []}
        if op == "stat":
            found = self.root / args["paths"][0]
            return {
                "stats": [
                    {
                        "path": args["paths"][0],
                        "is_file": found.is_file(),
                        "is_dir": found.is_dir(),
                        "size": found.stat().st_size if found.exists() else None,
                        "contained": ".." not in args["paths"][0],
                    }
                ]
            }
        assert op == "grep"
        return {"matches": [{"path": "a.py", "line": 3, "text": args["pattern"]}]}

    def data(self, source: trees.Source, op: str, args: dict[str, Any]) -> bytes:
        """Answer the operations that answer with bytes."""
        self._check(op, args)
        if op == "fetch":
            out = io.BytesIO()
            with tarfile.open(fileobj=out, mode="w") as archive:
                for path in args["paths"]:
                    archive.add(self.root / path, arcname=path)
            return out.getvalue()
        found = self.root / args["path"]
        if not found.is_file():
            raise trees.TreeRefused(trees.UNREADABLE)
        raw = found.read_bytes()
        return raw[: args["n"]] if op == "head" else raw

    def subscribe(self, listener: trees.Listener) -> Callable[[], None]:
        """Keep the one listener."""
        self.listener = listener
        return lambda: None


@pytest.fixture
def served(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[FakeTransport, trees.RemoteTree]:
    """Return a small tree and the agent serving it."""
    (tmp_path / "b" / "skip").mkdir(parents=True)
    (tmp_path / "b" / "skip" / "x.py").write_text("")
    (tmp_path / "b" / "two.py").write_text("two\n")
    (tmp_path / "a.py").write_bytes(b"one\r\nbad\xff\n")
    (tmp_path / "a.txt").write_text("")
    transport = FakeTransport(tmp_path)
    monkeypatch.setattr(trees, "_transport", transport)
    return transport, trees.RemoteTree(_source())


def _bind(monkeypatch: pytest.MonkeyPatch, *rows: trees.Source) -> None:
    monkeypatch.setattr(trees, "_read_sources", lambda: {r.project: r for r in rows})
    monkeypatch.setattr(trees, "_sources_read", 0.0)
    monkeypatch.setattr(trees, "_sources", {})


def test_a_project_without_a_binding_is_read_by_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Never available, and a read raises rather than answers as an empty tree."""
    _bind(monkeypatch)
    tree = trees.of("alpha")
    assert isinstance(tree, trees.Unserved)
    assert not tree.available()
    with pytest.raises(trees.TreeUnavailable, match="alpha cannot be read"):
        list(tree.walk())
    with pytest.raises(trees.TreeUnavailable):
        tree.read("a.py")


def test_binding_is_what_reads_a_project_and_unbinding_stops_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Binding is the switch, and nothing is reindexed either way."""
    _bind(monkeypatch, _source())
    assert isinstance(trees.of("alpha"), trees.RemoteTree)
    _bind(monkeypatch)
    assert isinstance(trees.of("alpha"), trees.Unserved)


def test_a_database_that_stops_answering_leaves_the_last_answer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A hiccup must not make a bound project read as unbound."""
    _bind(monkeypatch, _source())
    assert isinstance(trees.of("alpha"), trees.RemoteTree)
    monkeypatch.setattr(trees, "_read_sources", lambda: None)
    monkeypatch.setattr(trees, "_sources_read", 0.0)
    assert isinstance(trees.source("alpha"), trees.Source)


@pytest.mark.parametrize(
    "failure", [RuntimeError("DATABASE_URL is not set"), psycopg2.OperationalError()]
)
def test_a_database_that_cannot_say_is_no_answer(
    failure: Exception, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A process with no database reads no tree, and does not fail."""

    def fail(sql: str) -> list[dict[str, Any]]:
        raise failure

    monkeypatch.setattr(trees.db, "query", fail)
    assert trees._read_sources() is None


def test_a_remote_walk_is_sorted_and_pruned(
    served: tuple[FakeTransport, trees.RemoteTree],
) -> None:
    """Same order, same filters, and what is pruned is never asked for."""
    transport, tree = served
    found = tree.walk("", lambda rel: rel != "b/skip", lambda rel: rel.endswith(".py"))
    assert list(found) == ["a.py", "b/two.py"]
    assert [args["top"] for op, args in transport.asked] == ["", "b"]


def test_a_root_answering_nothing_raises_rather_than_reads_as_empty(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An empty walk would prune a whole graph."""
    monkeypatch.setattr(trees, "_transport", FakeTransport(tmp_path))
    with pytest.raises(trees.TreeUnavailable):
        list(trees.RemoteTree(_source()).walk())


def test_a_remote_tree_says_what_a_path_is(
    served: tuple[FakeTransport, trees.RemoteTree],
) -> None:
    """Each question is one `stat`."""
    _, tree = served
    assert tree.is_file("a.py") and not tree.is_file("b")
    assert tree.is_dir("b") and not tree.is_dir("a.py")
    assert tree.contains("a.py") and not tree.contains("../outside.txt")
    assert tree.size("b/two.py") == 4
    assert tree.size("gone.py") is None


def test_a_remote_read_decodes_the_bytes(
    served: tuple[FakeTransport, trees.RemoteTree],
) -> None:
    """The hash stored for a file must not move when its reader does."""
    _, tree = served
    assert tree.read("a.py") == ("one\nbad\n", "")


def test_a_remote_read_gives_the_reason_there_is_none(
    served: tuple[FakeTransport, trees.RemoteTree],
) -> None:
    """A file-level refusal is a reason."""
    transport, tree = served
    assert tree.read("gone.py") == (None, trees.UNREADABLE)
    assert tree.head("gone.py", 10) == b""
    for code, reason in (
        ("denied", trees.DENIED),
        ("over the size limit", trees.OVERSIZED),
        ("outside", trees.UNREADABLE),
    ):
        transport.refusal = trees.TreeRefused(code)
        assert tree.read("a.py") == (None, reason)


def test_a_refusal_about_the_agent_says_nothing_of_the_file(
    served: tuple[FakeTransport, trees.RemoteTree],
) -> None:
    """An export gone is not a file gone; a run must stop, not lose the file."""
    transport, tree = served
    transport.refusal = trees.TreeRefused("unknown_export")
    with pytest.raises(trees.TreeUnavailable):
        tree.read("a.py")
    with pytest.raises(trees.TreeUnavailable):
        tree.head("a.py", 1)
    with pytest.raises(trees.TreeUnavailable):
        tree.is_file("a.py")
    with pytest.raises(trees.TreeUnavailable):
        list(tree.walk())


def test_an_agent_offline_raises_from_every_reading_call(
    served: tuple[FakeTransport, trees.RemoteTree],
) -> None:
    """Never an empty answer: an empty answer prunes."""
    transport, tree = served
    transport.online = False
    assert not tree.available()
    for call in (
        lambda: list(tree.walk()),
        lambda: tree.read("a.py"),
        lambda: tree.head("a.py", 1),
        lambda: tree.is_file("a.py"),
        lambda: tree.grep("x", [], None, 1, False),
    ):
        with pytest.raises(trees.TreeUnavailable):
            call()


def test_available_needs_the_export_approved_and_the_agent_connected(
    served: tuple[FakeTransport, trees.RemoteTree],
) -> None:
    """Connected is not enough once an operator took the approval back."""
    transport, tree = served
    assert tree.available()
    assert not trees.RemoteTree(_source(approved=False)).available()
    transport.online = False
    assert not tree.available()


def test_a_remote_grep_carries_what_the_search_was_narrowed_by(
    served: tuple[FakeTransport, trees.RemoteTree],
) -> None:
    """Tuples, and everything the search was narrowed by goes along."""
    transport, tree = served
    assert tree.grep("web", ["build/"], "*.py", 5, True) == [("a.py", 3, "web")]
    assert transport.asked[-1] == (
        "grep",
        {
            "export": "alpha-export",
            "pattern": "web",
            "ignore_lines": ["build/"],
            "path_glob": "*.py",
            "limit": 5,
            "fixed": True,
        },
    )


def test_a_bad_pattern_finds_nothing(
    served: tuple[FakeTransport, trees.RemoteTree],
) -> None:
    """A bad pattern answers no lines."""
    transport, tree = served
    transport.refusal = trees.TreeRefused(trees.BAD_ARGS)
    assert tree.grep("(", [], None, 5, False) == []


def test_a_fetch_gives_a_directory_for_as_long_as_it_is_held(
    served: tuple[FakeTransport, trees.RemoteTree],
) -> None:
    """The files sit at their relative paths, and then they are gone."""
    _, tree = served
    with tree.local(["b/two.py"]) as directory:
        path = pathlib.Path(directory) / "b" / "two.py"
        assert path.read_text() == "two\n"
    assert not path.exists()


def test_the_hash_vectors_hold_for_what_decoding_makes_of_the_bytes() -> None:
    """The agent's tests read the same file; the two stay one hash."""
    for vector in json.loads(VECTORS.read_text()):
        text = trees.decode(bytes.fromhex(vector["bytes_hex"]))
        assert text.encode().hex() == vector["text_hex"], vector["name"]
        assert hashlib.md5(text.encode()).hexdigest() == vector["md5"], vector["name"]


def test_changes_hear_the_agent_of_a_bound_project(
    served: tuple[FakeTransport, trees.RemoteTree],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A path goes through as it is, and a resync as the one word for a full pass."""
    transport, _ = served
    _bind(monkeypatch, _source())
    stop = threading.Event()
    changes = trees.changes(["alpha"], stop)
    heard: list[tuple[str, str]] = []

    def read() -> None:
        for change in changes:
            heard.append(change)

    reading = threading.Thread(target=read, daemon=True)
    reading.start()
    while transport.listener is None:
        time.sleep(0.01)
    transport.listener("agent-1", "alpha-export", ["a.py", "b/two.py"])
    transport.listener("agent-1", "alpha-export", None)
    transport.listener("agent-1", "another-export", ["c.py"])
    transport.listener("agent-2", "alpha-export", ["d.py"])
    while len(heard) < 3:
        time.sleep(0.01)
    stop.set()
    reading.join(timeout=5)
    assert heard == [
        ("alpha", "a.py"),
        ("alpha", "b/two.py"),
        ("alpha", trees.RESYNC),
    ]


def test_another_process_cannot_listen_for_changes() -> None:
    """Only the worker API holds the sockets, and only it schedules."""
    with pytest.raises(trees.TreeUnavailable):
        trees.HttpTransport().subscribe(lambda *_: None)


def test_http_reads_as_unavailable_when_there_is_nowhere_to_ask(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Another container with no address says the tree cannot answer."""
    monkeypatch.setattr(trees, "WORKER_API_URL", "")
    transport = trees.HttpTransport()
    assert not transport.connected(_source())
    with pytest.raises(trees.TreeUnavailable):
        transport.call(_source(), "stat", {"paths": ["a.py"]})


def _offences(path: pathlib.Path) -> list[str]:
    found: list[str] = []
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Name | ast.alias):
            name = node.id if isinstance(node, ast.Name) else node.name
            if name in FORBIDDEN_NAMES or name.split(".")[0] in FORBIDDEN_MODULES:
                found.append(f"{path.name}: {name}")
        if isinstance(node, ast.ImportFrom) and node.module in FORBIDDEN_MODULES:
            found.append(f"{path.name}: {node.module}")
        if isinstance(node, ast.Attribute) and node.attr == "walk":
            if isinstance(node.value, ast.Name) and node.value.id == "os":
                found.append(f"{path.name}: os.walk")
        if isinstance(node, ast.Constant) and node.value == "rg":
            found.append(f"{path.name}: ripgrep")
    return found


def test_nothing_reads_a_tree_around_the_interface() -> None:
    """Where the trees are, how they are walked, searched and watched is one module."""
    offences = [
        one
        for path in sorted(PACKAGES.glob("*/src/**/*.py"))
        for one in _offences(path)
    ]
    assert offences == []


def test_an_export_no_longer_approved_is_never_asked(
    served: tuple[FakeTransport, trees.RemoteTree],
) -> None:
    """Taking the approval back keeps the binding, so every call checks it."""
    transport, _ = served
    tree = trees.RemoteTree(_source(approved=False))
    for call in (
        lambda: list(tree.walk()),
        lambda: tree.read("a.py"),
        lambda: tree.head("a.py", 1),
        lambda: tree.is_file("a.py"),
        lambda: tree.grep("x", [], None, 1, False),
    ):
        with pytest.raises(trees.TreeUnavailable, match="not approved"):
            call()
    assert transport.asked == []


def test_a_fetch_answered_with_more_than_was_asked_writes_nothing(
    served: tuple[FakeTransport, trees.RemoteTree], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The archive is the agent's to make, so it is weighed before it is unpacked."""
    transport, tree = served
    answer = transport.data
    extra = lambda source, op, args: answer(  # noqa: E731
        source, op, {**args, "paths": [*args["paths"], "a.py"]}
    )
    monkeypatch.setattr(transport, "data", extra)
    with pytest.raises(trees.TreeUnavailable, match="with more"):
        with tree.local(["b/two.py"]):
            pass
    monkeypatch.setattr(transport, "data", answer)
    monkeypatch.setattr(trees, "MAX_FETCH_BYTES", 1)
    with pytest.raises(trees.TreeUnavailable, match="with more"):
        with tree.local(["b/two.py"]):
            pass
