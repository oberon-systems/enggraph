"""Scan a tree before it is a project: its name, its formats, and a report.

Run as `python -m enggraph.indexer.bootstrap` by `make install`, against the same
read-only mount the indexer walks. Nothing here reaches the database, so it
runs before the stack is up; the formats it prints are the ones the first
index run records on the project.
"""

from __future__ import annotations

import sys
from collections import Counter

from enggraph.core import trees
from enggraph.core.config import MAX_FILE_BYTES, PROJECT_NAME, PROJECT_ROOT
from enggraph.core.identifiers import project_name
from enggraph.core.trees import Tree
from enggraph.indexer.discovery import walk_selected
from enggraph.indexer.formats import format_of

# A directory is only a bulk candidate when it is both large in absolute
# terms and a real share of the tree, so a small repository never reports one.
BULK_MIN_FILES = 200
BULK_MIN_SHARE = 0.2
DETAIL_LIMIT = 20


def detail(label: str, items: list[str]) -> list[str]:
    """Return a capped detail block for the report."""
    if not items:
        return []
    lines = [f"{label} ({len(items)}):"]
    lines.extend(f"  {item}" for item in sorted(items)[:DETAIL_LIMIT])
    if len(items) > DETAIL_LIMIT:
        lines.append(f"  ... and {len(items) - DETAIL_LIMIT} more")
    return lines


def scan(tree: Tree) -> tuple[list[str], list[str]]:
    """Walk the tree as the indexer would; return its formats and a report."""
    files = list(walk_selected(tree, None))
    formats: Counter[str] = Counter()
    top: Counter[str] = Counter()
    oversized: list[str] = []
    scripts: list[str] = []
    for rel_path in files:
        found = format_of(tree, rel_path)
        formats[found] += 1
        if found.startswith("#!"):
            scripts.append(f"{rel_path} ({found})")
        if "/" in rel_path:
            top[rel_path.split("/")[0]] += 1
        size = tree.size(rel_path)
        if size is not None and size > MAX_FILE_BYTES:
            oversized.append(rel_path)
    total = len(files) or 1
    bulk = [
        f"{name}/ ({count} files)"
        for name, count in top.most_common()
        if count >= BULK_MIN_FILES and count / total >= BULK_MIN_SHARE
    ]
    report = [f"selected: {len(files)}  formats: {len(formats)}"]
    report.extend(
        detail("formats", [f"{name} ({count})" for name, count in formats.items()])
    )
    report.extend(detail("scripts admitted by their shebang", scripts))
    report.extend(detail("over the 1 MB limit, read and then dropped", oversized))
    report.extend(detail("bulk directories, worth an ignore line?", bulk))
    return sorted(formats), report


def emit(section: str, lines: list[str]) -> None:
    """Print one section of the output contract."""
    print(f"#--- {section} ---")
    for line in lines:
        print(line)


def main() -> None:
    """Scan the mounted tree and print the name, the formats and the report."""
    tree = trees.pending()
    if not tree.is_dir(""):
        print(f"{tree.where} is not a directory", file=sys.stderr)
        raise SystemExit(1)
    formats, report = scan(tree)
    emit("name", [project_name(PROJECT_NAME, PROJECT_ROOT or tree.where)])
    emit("formats", formats)
    emit("report", report)


if __name__ == "__main__":
    main()
