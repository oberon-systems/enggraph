"""The tree interface, and that nothing reads a tree around it."""

from __future__ import annotations

import ast
import hashlib
import io
import json
import os
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
# mounts.py prints the mount list the compose override is generated from.
ALLOWED = {"trees.py", "mounts.py"}
FORBIDDEN_NAMES = {"CODE_ROOT", "SCAN_PATH"}
FORBIDDEN_MODULES = {"watchfiles"}
VECTORS = pathlib.Path(__file__).parent / "data" / "tree_hash_vectors.json"


def test_a_project_is_read_under_its_own_name() -> None:
    """The compose override is generated from the same name."""
    assert trees.of("alpha").where == "/code/alpha"


def test_a_live_directory_is_available(tmp_path: pathlib.Path) -> None:
    """The ordinary case."""
    assert trees.at(str(tmp_path)).available()


def test_a_missing_path_or_a_file_is_not_available(tmp_path: pathlib.Path) -> None:
    """Nothing there, or something that is not a tree."""
    (tmp_path / "beta").write_text("")
    assert not trees.at(str(tmp_path / "alpha")).available()
    assert not trees.at(str(tmp_path / "beta")).available()


def test_a_deleted_directory_behind_a_mount_is_not_available(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A bind mount keeps a replaced host directory: empty, with no links."""
    fields = list(os.stat(tmp_path))
    fields[3] = 0
    monkeypatch.setattr(trees.os, "stat", lambda path: os.stat_result(fields))
    assert not trees.at(str(tmp_path)).available()


def test_a_walk_is_sorted_and_pruned(tmp_path: pathlib.Path) -> None:
    """Directories and files are filtered by their relative paths."""
    (tmp_path / "b" / "skip").mkdir(parents=True)
    (tmp_path / "b" / "skip" / "x.py").write_text("")
    (tmp_path / "b" / "two.py").write_text("")
    (tmp_path / "a.py").write_text("")
    (tmp_path / "a.txt").write_text("")
    found = trees.at(str(tmp_path)).walk(
        "", lambda rel: rel != "b/skip", lambda rel: rel.endswith(".py")
    )
    assert list(found) == ["a.py", "b/two.py"]


def test_a_linked_file_pointing_out_is_not_contained(tmp_path: pathlib.Path) -> None:
    """A linked file is indexed, and it can point anywhere."""
    root = tmp_path / "alpha"
    root.mkdir()
    (tmp_path / "outside.txt").write_text("not yours")
    (root / "escape.txt").symlink_to(tmp_path / "outside.txt")
    (root / "inside.txt").write_text("yours")
    tree = trees.at(str(root))
    assert tree.contains("inside.txt")
    assert not tree.contains("escape.txt")
    assert not tree.contains("../outside.txt")


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


def test_a_project_without_a_binding_is_read_from_its_mount(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Nothing bound: everything is as it always was."""
    _bind(monkeypatch)
    assert isinstance(trees.of("alpha"), trees.MountTree)


def test_a_binding_reads_through_the_agent_and_unbinding_is_the_way_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Binding is the switch, and nothing is reindexed either way."""
    _bind(monkeypatch, _source())
    assert isinstance(trees.of("alpha"), trees.RemoteTree)
    _bind(monkeypatch)
    assert isinstance(trees.of("alpha"), trees.MountTree)


def test_a_database_that_stops_answering_leaves_the_last_answer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A hiccup must not flip a bound project back to a mount."""
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
    """A process with no database has mounts and nothing else, and does not fail."""

    def fail(sql: str) -> list[dict[str, Any]]:
        raise failure

    monkeypatch.setattr(trees.db, "query", fail)
    assert trees._read_sources() is None


def test_a_remote_walk_goes_as_a_mount_walk_does(
    served: tuple[FakeTransport, trees.RemoteTree],
) -> None:
    """Same order, same filters, and what is pruned is never asked for."""
    transport, tree = served
    found = tree.walk("", lambda rel: rel != "b/skip", lambda rel: rel.endswith(".py"))
    assert list(found) == ["a.py", "b/two.py"]
    assert [args["top"] for op, args in transport.asked] == ["", "b"]


def test_a_remote_walk_agrees_with_the_mount_on_the_same_tree(
    served: tuple[FakeTransport, trees.RemoteTree],
) -> None:
    """The two implementations of one interface, side by side."""
    transport, tree = served
    assert list(tree.walk()) == list(trees.at(str(transport.root)).walk())


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


def test_a_remote_read_decodes_as_a_mount_read_does(
    served: tuple[FakeTransport, trees.RemoteTree],
) -> None:
    """The hash stored for a file must not move when its reader does."""
    transport, tree = served
    mounted = trees.at(str(transport.root))
    assert tree.read("a.py") == mounted.read("a.py") == ("one\nbad\n", "")


def test_a_remote_read_gives_the_reason_there_is_none(
    served: tuple[FakeTransport, trees.RemoteTree],
) -> None:
    """A file-level refusal is a reason, as a mount gives one."""
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


def test_a_remote_grep_answers_as_a_mount_grep_does(
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
    """Ripgrep exits on one and a mount answers no lines."""
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


def test_the_hash_vectors_hold_for_what_a_mount_read_makes_of_the_bytes(
    tmp_path: pathlib.Path,
) -> None:
    """The agent's tests read the same file; the two stay one hash."""
    for vector in json.loads(VECTORS.read_text()):
        raw = bytes.fromhex(vector["bytes_hex"])
        (tmp_path / "file").write_bytes(raw)
        text, _ = trees.at(str(tmp_path)).read("file")
        assert text is not None
        assert text == trees.decode(raw), vector["name"]
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
        if path.name not in ALLOWED and path.name != "config.py"
        for one in _offences(path)
    ]
    assert offences == []
