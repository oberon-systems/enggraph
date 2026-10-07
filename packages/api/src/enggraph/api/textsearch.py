"""Read and search file text on the read-only mounts, never from the database.

The graph keeps line ranges and words; whatever has to show the text itself -
a search snippet, a context packet, a literal search - comes through here.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from enggraph.core import sources, trees
from enggraph.core.config import CONTENT_DENIED_NAMES, DEFAULT_IGNORED_DIRS

MAX_LINE_CHARS = 300
WORD = re.compile(r"[A-Za-z0-9]+")


@dataclass(frozen=True)
class Range:
    """Lines start..end, both counted from 1, of one file of one project."""

    project: str
    path: str
    start: int
    end: int


def read_ranges(wanted: list[Range]) -> list[dict[str, object]]:
    """Return the text of each range, or why there is none, reading each file once."""
    files: dict[tuple[str, str], tuple[list[str] | None, str]] = {}
    answered: list[dict[str, object]] = []
    for one in wanted:
        key = (one.project, one.path)
        if key not in files:
            content, reason = sources.read(one.project, one.path)
            files[key] = (None if content is None else content.splitlines(), reason)
        lines, reason = files[key]
        text = None if lines is None else "\n".join(lines[one.start - 1 : one.end])
        answered.append(
            {
                "project": one.project,
                "path": one.path,
                "start": one.start,
                "end": one.end,
                "text": text,
                "reason": reason if lines is None else "",
            }
        )
    return answered


def loose_pattern(text: str) -> str:
    """Return a regex matching a name whatever separates its words.

    `web_01_example_com`, `web-01.example.com` and `WEB 01 example com` match
    one another: only the letters and digits must agree, in order.
    """
    words = WORD.findall(text)
    return r"[\W_]*".join(re.escape(word) for word in words) if words else ""


def grep(
    project: str,
    pattern: str,
    ignore_lines: list[str],
    path_glob: str | None,
    limit: int,
    fixed: bool = False,
) -> list[dict[str, object]]:
    """Return up to `limit` lines of a project's tree matching a regex.

    The tree is read as the indexer reads it: the project's ignore lines, the
    default skip list and the deny list leave the same files out.
    """
    tree = trees.of(project)
    if limit <= 0 or not pattern or not tree.available():
        return []
    ignore = [f"{name}/" for name in sorted(DEFAULT_IGNORED_DIRS)]
    ignore += [*CONTENT_DENIED_NAMES, *ignore_lines]
    return [
        {
            "project": project,
            "path": path,
            "line": number,
            "text": text[:MAX_LINE_CHARS],
        }
        for path, number, text in tree.grep(pattern, ignore, path_glob, limit, fixed)
    ]
