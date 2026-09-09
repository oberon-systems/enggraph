"""What the mount listing says, which is what the compose override becomes.

Two columns: the project, which names the mount, and the host path bound
there. `scripts/mounts.sh` reads it and checks the paths, so what is pinned
here is the shape and the ordering.
"""

from __future__ import annotations

import pytest

from enggraph import mounts

STORED = {
    "alpha": "/src/alpha",
    "mono": "/mono",
}


@pytest.fixture
def listed(monkeypatch: pytest.MonkeyPatch) -> None:
    """Answer the database lookup with a selection written by hand."""
    monkeypatch.setattr(mounts, "mounted_trees", lambda: dict(STORED))


def run(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    *argv: str,
) -> list[str]:
    """Run the module as the shell script does, and read its listing back."""
    monkeypatch.setattr("sys.argv", ["enggraph.mounts", *argv])
    mounts.main()
    return capsys.readouterr().out.splitlines()


def test_one_line_per_project(
    listed: None, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Sorted by project, so the override is stable between runs."""
    assert run(monkeypatch, capsys) == [
        "alpha\t/src/alpha",
        "mono\t/mono",
    ]


def test_an_unregistered_tree_is_carried(
    listed: None, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A first mount happens before the row exists, which is what --add is."""
    lines = run(monkeypatch, capsys, "--add", "/src/epsilon/")
    assert "epsilon\t/src/epsilon" in lines


def test_a_created_project_is_not_mounted(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """It reads no tree yet, so the path it was onboarded from is not one."""
    monkeypatch.setattr(mounts, "mounted_trees", dict)
    registered: list[tuple[str, ...]] = []

    def record(
        root: str,
        name: str,
        project_type: str,
        with_tree: bool = True,
        selection: tuple[str, str] = ("", ""),
    ) -> str:
        registered.append((root, name, str(with_tree)))
        return name

    monkeypatch.setattr(mounts, "register", record)
    lines = run(
        monkeypatch,
        capsys,
        "--add",
        "/mono",
        "--name",
        "mono",
        "--register",
        "--create",
    )
    assert registered == [("/mono", "mono", "False")]
    assert not any(line.startswith("mono\t/mono") for line in lines)


def test_the_generated_selection_reaches_the_register_step(
    listed: None, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Onboarding no longer writes the pair into the tree, so it stores it.

    The documents travel as variable names rather than as values: an argument
    list is neither the place for kilobytes of globs nor safe from the shell
    that has to build it.
    """
    stored: list[tuple[str, str]] = []

    def record(
        root: str,
        name: str,
        project_type: str,
        with_tree: bool = True,
        selection: tuple[str, str] = ("", ""),
    ) -> str:
        stored.append(selection)
        return name

    monkeypatch.setattr(mounts, "register", record)
    monkeypatch.setenv("KEEP_DOC", "*.py\n")
    monkeypatch.setenv("IGNORE_DOC", "*.pem\n")
    run(
        monkeypatch,
        capsys,
        "--add",
        "/src/epsilon",
        "--register",
        "--ctxkeep-env",
        "KEEP_DOC",
        "--ctxignore-env",
        "IGNORE_DOC",
    )
    assert stored == [("*.py\n", "*.pem\n")]


def test_an_unnamed_variable_stores_nothing(
    listed: None, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A bare `make mounts` must not touch a project's stored selection."""
    stored: list[tuple[str, str]] = []

    def record(
        root: str,
        name: str,
        project_type: str,
        with_tree: bool = True,
        selection: tuple[str, str] = ("", ""),
    ) -> str:
        stored.append(selection)
        return name

    monkeypatch.setattr(mounts, "register", record)
    run(monkeypatch, capsys, "--add", "/src/epsilon", "--register")
    assert stored == [("", "")]
