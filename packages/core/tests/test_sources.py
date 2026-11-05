"""What may be read of a project's tree, and what may never leave it."""

from __future__ import annotations

import pathlib

import pytest
from dirtree import DirTree

from enggraph.core import sources, trees


@pytest.mark.parametrize(
    "rel_path",
    [
        ".env",
        "deploy/.env.production",
        "certs/server.pem",
        "infra/terraform.tfvars",
        "home/id_rsa",
        ".htpasswd",
    ],
)
def test_a_secret_is_never_served(rel_path: str) -> None:
    """The tree holds whatever the checkout holds; this list is the guard."""
    assert sources.is_denied(rel_path)


def test_ordinary_source_is_served() -> None:
    """The list is a denial, not an allowance."""
    assert not sources.is_denied("src/app.py")


@pytest.fixture
def project_tree(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> pathlib.Path:
    """Build a real tree with a secret, and a symlink pointing out of it."""
    root = tmp_path / "alpha"
    (root / "src").mkdir(parents=True)
    (root / "src" / "app.py").write_text("import os\n")
    (root / ".env").write_text("TOKEN=hunter2")
    outside = tmp_path / "outside.txt"
    outside.write_text("not yours")
    (root / "escape.txt").symlink_to(outside)
    monkeypatch.setattr(
        trees, "of", lambda project: DirTree(project, tmp_path / project)
    )
    return root


def test_reads_a_file_of_the_tree(project_tree: pathlib.Path) -> None:
    """The ordinary case: a path relative to the project root."""
    assert sources.read("alpha", "src/app.py") == ("import os\n", "")


def test_a_limit_truncates(project_tree: pathlib.Path) -> None:
    """The worker is shown a slice, not the whole file."""
    assert sources.read("alpha", "src/app.py", 6) == ("import", "")


def test_refuses_a_path_that_climbs_out(project_tree: pathlib.Path) -> None:
    """`..` must not reach another project, or the host."""
    assert sources.read("alpha", "../outside.txt") == (None, sources.ESCAPES)


def test_refuses_a_symlink_out_of_the_tree(project_tree: pathlib.Path) -> None:
    """os.walk never descends a symlinked directory, but it yields a file."""
    assert sources.read("alpha", "escape.txt") == (None, sources.ESCAPES)


def test_refuses_a_denied_name_before_touching_the_disk(
    project_tree: pathlib.Path,
) -> None:
    """The file is right there; the answer is still no."""
    assert sources.read("alpha", ".env") == (None, sources.DENIED)


def test_a_project_nothing_serves_says_so(project_tree: pathlib.Path) -> None:
    """A project nothing serves reads differently from a missing file."""
    assert sources.read("absent", "src/app.py") == (None, sources.UNSERVED)


def test_a_missing_file_carries_its_reason(project_tree: pathlib.Path) -> None:
    """The graph is ahead of the tree, which is not the same as denied."""
    content, reason = sources.read("alpha", "src/gone.py")
    assert content is None
    assert reason not in (sources.DENIED, sources.ESCAPES, sources.UNSERVED)


def test_a_key_file_is_never_served(project_tree: pathlib.Path) -> None:
    """Whatever the file is called within the list, the answer is the same."""
    (project_tree / "server.key").write_text("alpha-fake-key")
    (project_tree / "certs").mkdir()
    (project_tree / "certs" / "id_ed25519").write_text("alpha-fake-key")
    assert sources.read("alpha", "server.key") == (None, sources.DENIED)
    assert sources.read("alpha", "certs/id_ed25519") == (None, sources.DENIED)


def test_an_absolute_path_is_refused(project_tree: pathlib.Path) -> None:
    """`os.path.join` would otherwise let it replace the root altogether."""
    outside = project_tree.parent / "outside.txt"
    assert sources.read("alpha", str(outside)) == (None, sources.ESCAPES)


def test_a_directory_linked_out_is_refused_for_what_lies_below_it(
    project_tree: pathlib.Path,
) -> None:
    """A link in the middle of the path escapes as well as one at its end."""
    (project_tree / "linked").symlink_to(project_tree.parent)
    assert sources.read("alpha", "linked/outside.txt") == (None, sources.ESCAPES)


def test_an_agent_going_away_reads_as_the_project_being_unreachable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Whoever asked gets a reason, not a stack trace."""

    class Gone:
        def available(self) -> bool:
            return True

        def contains(self, rel_path: str) -> bool:
            raise trees.TreeUnavailable("gone")

    monkeypatch.setattr(sources.trees, "of", lambda project: Gone())
    assert sources.read("alpha", "src/app.py") == (None, sources.UNSERVED)
