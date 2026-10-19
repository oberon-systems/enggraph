"""Which suites `make test` picks for a change, over a tree made up here."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "affected.py"


def load() -> ModuleType:
    """Import scripts/affected.py, which lives outside every package."""
    spec = importlib.util.spec_from_file_location("affected", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


affected = load()
SHARED = {"core"}


def package(root: Path, name: str, needs: list[str], test_imports: list[str]) -> None:
    """Write one package: its pyproject's dependencies and one test file."""
    base = root / "packages" / name
    (base / "tests").mkdir(parents=True)
    listed = ", ".join(f'"enggraph-{one}==1.0.0"' for one in needs)
    (base / "pyproject.toml").write_text(f"dependencies = [{listed}]\n")
    lines = "".join(f"from enggraph.{one} import x\n" for one in test_imports)
    (base / "tests" / "test_one.py").write_text(lines)


def tree(root: Path) -> dict[str, set[str]]:
    """Lay out core, an indexer core's tests import, an api and a viewer."""
    (root / "pytest.ini").write_text(
        "[pytest]\npythonpath =\n    packages/core/src\n    packages/core/tests\n"
    )
    package(root, "core", [], ["indexer"])
    package(root, "indexer", ["core"], ["core"])
    package(root, "api", ["core", "indexer"], [])
    package(root, "viewer", ["core"], [])
    return affected.reach(root)


def test_reach_follows_pyproject_and_test_imports(tmp_path: Path) -> None:
    """Core's tests import the indexer; the viewer, using core, does not."""
    assert tree(tmp_path) == {
        "api": {"api", "core", "indexer"},
        "core": {"core", "indexer"},
        "indexer": {"indexer", "core"},
        "viewer": {"viewer", "core"},
    }


def test_a_leaf_package_runs_its_own_suite_alone(tmp_path: Path) -> None:
    """The viewer is used by nothing else and runs no stack."""
    reached = tree(tmp_path)
    files = ["packages/viewer/src/enggraph/viewer/page.py"]
    assert affected.select(files, reached, SHARED) == (["viewer"], False, [])


def test_core_reaches_every_suite_and_the_stack(tmp_path: Path) -> None:
    """Every package depends on core."""
    reached = tree(tmp_path)
    files = ["packages/core/src/enggraph/core/storage.py"]
    assert affected.select(files, reached, SHARED) == (
        ["api", "core", "indexer", "viewer"],
        True,
        [],
    )


def test_a_test_alone_runs_its_own_suite_and_no_stack(tmp_path: Path) -> None:
    """A package's own tests reach no image and no other suite."""
    reached = tree(tmp_path)
    files = ["packages/indexer/tests/test_one.py"]
    assert affected.select(files, reached, SHARED) == (["indexer"], False, [])


def test_shared_tests_reach_every_suite_using_them(tmp_path: Path) -> None:
    """Core's tests hold the fakes others import, and still build no image."""
    reached = tree(tmp_path)
    assert affected.shared_tests(tmp_path) == SHARED
    files = ["packages/core/tests/test_one.py"]
    assert affected.select(files, reached, SHARED) == (
        ["api", "core", "indexer", "viewer"],
        False,
        [],
    )


def test_docs_reach_nothing_and_are_named(tmp_path: Path) -> None:
    """A file no suite reads is listed, not tested."""
    reached = tree(tmp_path)
    files = ["README.md", "docs/usage.md", "nginx/default.conf"]
    assert affected.select(files, reached, SHARED) == ([], False, files)


def test_the_eval_inputs_run_the_stack_alone(tmp_path: Path) -> None:
    """The corpus, the eval compose file and the skills feed the stack only."""
    reached = tree(tmp_path)
    files = ["eval/queries.yaml", "docker-compose.eval.yaml", "skills/a/SKILL.md"]
    assert affected.select(files, reached, SHARED) == ([], True, [])


def test_pytest_settings_run_every_suite(tmp_path: Path) -> None:
    """pytest.ini is read by every suite."""
    reached = tree(tmp_path)
    assert affected.select(["pytest.ini"], reached, SHARED)[0] == [
        "api",
        "core",
        "indexer",
        "viewer",
    ]


def test_porcelain_names_both_sides_of_a_rename() -> None:
    """Modified, untracked and renamed entries all count."""
    output = " M a.py\0?? new/b.py\0R  c2.py\0c1.py\0"
    assert affected.porcelain_paths(output) == ["a.py", "new/b.py", "c2.py", "c1.py"]
