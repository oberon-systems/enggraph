"""Read and search file text on the read-only mounts, never from the database.

The graph keeps line ranges and words; whatever has to show the text itself -
a search snippet, a context packet, a literal search - comes through here.
"""

from __future__ import annotations

import os
import re
import subprocess
import tempfile
import threading
from dataclasses import dataclass

from enggraph import sources
from enggraph.config import CONTENT_DENIED_NAMES, DEFAULT_IGNORED_DIRS
from enggraph.identifiers import is_mounted, project_mount

RIPGREP = "rg"
MAX_LINE_CHARS = 300
GREP_TIMEOUT_SECONDS = 60
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


def _ignore_file(lines: list[str]) -> str:
    handle, path = tempfile.mkstemp(prefix="enggraph-grep-", suffix=".ignore")
    with os.fdopen(handle, "w", encoding="utf-8") as out:
        for name in sorted(DEFAULT_IGNORED_DIRS):
            out.write(f"{name}/\n")
        for name in CONTENT_DENIED_NAMES:
            out.write(f"{name}\n")
        for line in lines:
            out.write(f"{line}\n")
    return path


def _parse(line: str) -> tuple[str, int, str] | None:
    path, separator, rest = line.partition("\0")
    number, separator2, text = rest.partition(":")
    if not separator or not separator2 or not number.isdigit():
        return None
    return path.removeprefix("./"), int(number), text[:MAX_LINE_CHARS]


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
    mount = project_mount(project)
    if limit <= 0 or not pattern or not is_mounted(mount):
        return []
    ignore = _ignore_file(ignore_lines)
    command = [
        RIPGREP,
        "--no-ignore",
        "--hidden",
        "--no-heading",
        "--with-filename",
        "--null",
        "--line-number",
        "--color=never",
        "--ignore-case",
        "--ignore-file",
        ignore,
        "--max-count",
        str(limit),
    ]
    if fixed:
        command.append("--fixed-strings")
    if path_glob:
        command += ["--glob", path_glob]
    command += ["--regexp", pattern, "."]
    found: list[dict[str, object]] = []
    try:
        with subprocess.Popen(
            command,
            cwd=mount,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            errors="replace",
        ) as process:
            # A search over a large tree is cut off rather than left running.
            timer = threading.Timer(GREP_TIMEOUT_SECONDS, process.kill)
            timer.start()
            try:
                for raw in process.stdout or ():
                    parsed = _parse(raw.rstrip("\n"))
                    if parsed is None:
                        continue
                    path, number, text = parsed
                    found.append(
                        {
                            "project": project,
                            "path": path,
                            "line": number,
                            "text": text,
                        }
                    )
                    if len(found) >= limit:
                        break
            finally:
                timer.cancel()
                process.kill()
                process.wait()
    finally:
        os.unlink(ignore)
    return found
