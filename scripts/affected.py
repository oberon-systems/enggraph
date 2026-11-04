"""Run the test suites a change reaches, and only those: `make test`.

The change is the working tree against HEAD, untracked files included, or the
last commit when the tree is clean. A package's Python tests run when a file
changes in that package, in one its tests import, or in a package either of
those depends on through its pyproject: the graph is read from the tree, so a
new dependency needs no edit here. The eval stack runs when the change touches
what that stack runs or what its benchmark measures. The host agent's tests run
when the crate changes or something they read from outside it does.

`make test-all` runs every suite regardless. With `--lint <goal>`, the script is
a cargo pre-commit hook: it runs that make goal only when the crate changed.
"""

from __future__ import annotations

import os
import re
import shlex
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PACKAGES_DIR = "packages"
DEPENDENCY = re.compile(r'"enggraph-([a-z]+)')
IMPORT = re.compile(r"^\s*(?:from|import)\s+enggraph\.([a-z]+)", re.MULTILINE)
SHARED_TESTS = re.compile(r"packages/([a-z]+)/tests\s*$", re.MULTILINE)
# Files every Python suite reads: a change to either reruns all of them.
EVERY_SUITE = ("pytest.ini", "requirements-dev.txt")
# The eval stack runs the api image (the indexer and the skills it carries)
# and the MCP server over core's schema; the benchmark measures what they
# answer.
STACK_PACKAGES = ("core", "indexer", "api", "mcp")
STACK_PATHS = ("eval/", "docker-compose.eval.yaml", "skills/")
AGENT_DIR = "packages/agent/"
# What the crate's tests read from core, which pins the same answers in Python.
AGENT_SHARED = (
    "packages/core/tests/data/agent_signing_vectors.json",
    "packages/core/tests/data/agent_protocol_examples.json",
    "packages/core/tests/data/tree_hash_vectors.json",
    "packages/core/src/enggraph/core/config.py",
)


def package_names(root: Path) -> list[str]:
    """Return the packages of the tree, the directories holding a pyproject."""
    base = root / PACKAGES_DIR
    return sorted(one.parent.name for one in base.glob("*/pyproject.toml"))


def shared_tests(root: Path) -> set[str]:
    """Return the packages whose tests pytest.ini puts on every suite's path."""
    settings = root / "pytest.ini"
    if not settings.exists():
        return set()
    return set(SHARED_TESTS.findall(settings.read_text()))


def code_uses(root: Path, name: str, known: set[str]) -> set[str]:
    """Return the packages one package's pyproject depends on."""
    manifest = root / PACKAGES_DIR / name / "pyproject.toml"
    return (set(DEPENDENCY.findall(manifest.read_text())) & known) - {name}


def test_uses(root: Path, name: str, known: set[str]) -> set[str]:
    """Return the packages one package's tests import."""
    found: set[str] = set()
    for test in sorted((root / PACKAGES_DIR / name / "tests").rglob("*.py")):
        found.update(IMPORT.findall(test.read_text(encoding="utf-8")))
    return (found & known) - {name}


def reach(root: Path) -> dict[str, set[str]]:
    """Return, per package, every package its suite depends on, itself too.

    The suite needs the package and what its tests import, and each of those
    needs its own code dependencies. Another package's test imports are not
    followed: they are that suite's, not this one's.
    """
    known = package_names(root)
    code = {name: code_uses(root, name, set(known)) for name in known}
    closed: dict[str, set[str]] = {}
    for name in known:
        seen = {name} | test_uses(root, name, set(known))
        frontier = list(seen)
        while frontier:
            for other in code.get(frontier.pop(), set()) - seen:
                seen.add(other)
                frontier.append(other)
        closed[name] = seen
    return closed


def owner(rel_path: str) -> tuple[str, bool]:
    """Return the package a path lies in, and whether it is one of its tests."""
    parts = rel_path.split("/")
    if len(parts) > 2 and parts[0] == PACKAGES_DIR:
        return parts[1], parts[2] == "tests"
    return "", False


def select(
    files: list[str], reached: dict[str, set[str]], shared: set[str]
) -> tuple[list[str], bool, list[str]]:
    """Return the suites to run, whether the stack runs, and the unread files.

    A change to a package's tests reaches its own suite and no image; tests
    other suites import (`shared`) reach those suites as code would.
    """
    touched: set[str] = set()
    own: set[str] = set()
    every = False
    stack = False
    ignored: list[str] = []
    for rel_path in files:
        package, test = owner(rel_path)
        if package in reached and test and package not in shared:
            own.add(package)
        elif package in reached:
            touched.add(package)
            stack = stack or (package in STACK_PACKAGES and not test)
        elif rel_path in EVERY_SUITE:
            every = True
        elif rel_path.startswith(STACK_PATHS):
            stack = True
        elif not rel_path.startswith(AGENT_DIR):
            ignored.append(rel_path)
    suites = [
        name
        for name, uses in sorted(reached.items())
        if every or name in own or uses & touched
    ]
    return suites, stack, ignored


def agent_linted(files: list[str]) -> bool:
    """Return whether a change lies in the host agent's crate."""
    return any(one.startswith(AGENT_DIR) for one in files)


def agent_tested(files: list[str]) -> bool:
    """Return whether a change reaches the host agent's tests."""
    return agent_linted(files) or any(one in files for one in AGENT_SHARED)


def porcelain_paths(output: str) -> list[str]:
    """Return every path `git status --porcelain -z` names, both sides of a rename."""
    entries = output.split("\0")
    paths: list[str] = []
    index = 0
    while index < len(entries):
        entry = entries[index]
        index += 1
        if len(entry) < 4:
            continue
        paths.append(entry[3:])
        if entry[0] in "RC" and index < len(entries):
            paths.append(entries[index])
            index += 1
    return paths


def git(*args: str) -> str:
    """Run git in the tree and return what it printed."""
    return subprocess.run(
        ["git", *args], cwd=ROOT, check=True, capture_output=True, text=True
    ).stdout


def changed_files() -> tuple[list[str], str]:
    """Return the changed paths and what they were compared against."""
    files = porcelain_paths(git("status", "--porcelain", "-z", "--untracked-files=all"))
    if files:
        return sorted(set(files)), "the working tree against HEAD"
    try:
        listed = git("diff", "--name-only", "HEAD~1", "HEAD")
    except subprocess.CalledProcessError:
        return [], "a clean tree with no earlier commit"
    return sorted(set(listed.split())), "the last commit, the tree being clean"


def run_make(goal: str) -> int:
    """Run one make goal in the tree, ARGS being pytest's, and return its status."""
    make = os.environ.get("MAKE_BIN", "make")
    command = [make, "--no-print-directory", goal, "ARGS="]
    return subprocess.run(command, cwd=ROOT).returncode


def lint(goal: str) -> int:
    """Run one of the crate's lint goals when the crate changed."""
    files, _ = changed_files()
    return run_make(goal) if agent_linted(files) else 0


def main() -> int:
    """Pick the suites, say why, run them, and return the first failure."""
    reached = reach(ROOT)
    files, against = changed_files()
    suites, stack, ignored = select(files, reached, shared_tests(ROOT))
    agent = agent_tested(files)
    print(f"Changes: {len(files)} files, {against}.")
    for rel_path in ignored:
        print(f"  no suite reads {rel_path}")
    print(f"Python suites: {', '.join(suites) or 'none'}")
    print(f"Eval stack: {'yes' if stack else 'no'}")
    print(f"Host agent: {'yes' if agent else 'no'}")

    status = 0
    if suites:
        pytest = os.environ.get("PYTEST", str(ROOT / ".venv" / "bin" / "pytest"))
        paths = [f"{PACKAGES_DIR}/{name}/tests" for name in suites]
        extra = shlex.split(os.environ.get("ARGS", ""))
        status = subprocess.run([pytest, "-q", *paths, *extra], cwd=ROOT).returncode
    if stack:
        status = run_make("test-eval") or status
    if agent:
        status = run_make("agent-test") or status
    if not suites and not stack and not agent:
        print("Nothing to test.")
    return status


if __name__ == "__main__":
    sys.exit(lint(sys.argv[2]) if sys.argv[1:2] == ["--lint"] else main())
