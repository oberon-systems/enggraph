"""Describe a file in one line, without reading it for the caller."""

from __future__ import annotations

import posixpath

from enggraph.config import (
    COMMENT_MARKERS,
    DOCSTRING_SCAN_LINES,
    MAX_SUMMARY_LENGTH,
    SUMMARY_ENTITY_LIMIT,
    SUMMARY_SCAN_LINES,
)
from enggraph.identifiers import truncate
from enggraph.parsers import MarkdownParser, parser_class


def markdown_title(content: str) -> str:
    """Return the title of a Markdown document, atx or setext style."""
    lines = content.splitlines()[:SUMMARY_SCAN_LINES]
    for index, raw_line in enumerate(lines):
        line = raw_line.strip()
        # Skips blank lines, front matter fences and setext underlines.
        if not line or set(line) <= {"-", "="}:
            continue
        if line.startswith("#"):
            return line.lstrip("#").strip()
        next_line = lines[index + 1].strip() if index + 1 < len(lines) else ""
        if next_line and set(next_line) in ({"="}, {"-"}):
            return line
        # A document that opens with prose has no title to take.
        return ""
    return ""


def leading_comment(content: str) -> str:
    """Return the first line of the comment block at the top of a file.

    Only that block counts: a comment further down describes the code around
    it, not the file.
    """
    for raw_line in content.splitlines()[:SUMMARY_SCAN_LINES]:
        line = raw_line.strip()
        if not line or line.startswith("#!"):
            continue
        marker = next(
            (marker for marker in COMMENT_MARKERS if line.startswith(marker)), None
        )
        if marker is None:
            return ""
        text = line[len(marker) :].strip(" \t*/-<>!").strip("\"'").strip()
        if text:
            return text
    return ""


def extract_summary(rel_path: str, content: str, entities: list[dict[str, str]]) -> str:
    """Summarize a file by its title, its leading comment, or what it declares."""
    if parser_class(rel_path) is MarkdownParser:
        title = markdown_title(content)
        if title:
            return truncate(title, MAX_SUMMARY_LENGTH)

    comment = leading_comment(content)
    if comment:
        return truncate(comment, MAX_SUMMARY_LENGTH)

    # No prose to quote: name what the file declares, which beats a line count
    # for deciding whether the file is worth opening.
    if entities:
        groups: dict[str, list[str]] = {}
        for entity in entities:
            groups.setdefault(entity["type"], []).append(entity["name"])
        parts = []
        for kind, names in groups.items():
            listed = ", ".join(names[:SUMMARY_ENTITY_LIMIT])
            if len(names) > SUMMARY_ENTITY_LIMIT:
                listed += f", +{len(names) - SUMMARY_ENTITY_LIMIT} more"
            parts.append(f"{kind}: {listed}")
        return truncate("; ".join(parts), MAX_SUMMARY_LENGTH)

    lines = len(content.splitlines())
    return f"{posixpath.basename(rel_path)} ({lines} line{'' if lines == 1 else 's'})"


def _comment_text(line: str) -> str | None:
    """Return the prose of a comment line, or None when it is not a comment."""
    marker = next((m for m in COMMENT_MARKERS if line.startswith(m)), None)
    if marker is None or line.startswith("#!"):
        return None
    return line[len(marker) :].strip(" \t*/-<>!#;").strip("\"'").strip()


def _comment_above(lines: list[str], start_line: int) -> str:
    """Return the first prose line of the comment block right above an entity."""
    index = start_line - 2
    while index >= 0 and lines[index].strip().startswith("@"):
        index -= 1
    block: list[str] = []
    while index >= 0:
        stripped = lines[index].strip()
        text = _comment_text(stripped)
        if text is None or stripped.startswith(('"""', "'''")):
            break
        block.append(text)
        index -= 1
    return next(
        (text for text in reversed(block) if text and not text.startswith("@")), ""
    )


def _docstring_below(lines: list[str], start_line: int) -> str:
    """Return the first line of a docstring opening the body of an entity."""
    end = min(len(lines), start_line - 1 + DOCSTRING_SCAN_LINES)
    opened = (at for at in range(start_line - 1, end) if lines[at].rstrip()[-1:] == ":")
    body = next((at + 1 for at in opened), None)
    while body is not None and body < len(lines) and not lines[body].strip():
        body += 1
    if body is None or body >= len(lines):
        return ""
    stripped = lines[body].strip()
    quote = next((q for q in ('"""', "'''") if stripped.startswith(q)), None)
    if quote is None:
        return ""
    text = stripped[len(quote) :].split(quote, 1)[0].strip()
    if not text and body + 1 < len(lines):
        text = lines[body + 1].strip().split(quote, 1)[0].strip()
    return text


def entity_summary(lines: list[str], start_line: int) -> str:
    """Summarize an entity by its docstring, else by the comment above it.

    `start_line` counts from one; decorators above the entity are stepped over.
    """
    if start_line < 1 or start_line > len(lines):
        return ""
    text = _docstring_below(lines, start_line) or _comment_above(lines, start_line)
    return truncate(text, MAX_SUMMARY_LENGTH) if text else ""
