"""Order fused search rows by what the graph and the query say about them."""

from __future__ import annotations

import math
import re
from typing import Any

Row = dict[str, Any]

# Every weight is a fraction of a top-ranked hit in one half, 1/(60 + 1),
# so a bonus of 0.25 is worth as much as a quarter of that hit.
UNIT = 1 / 61
IDENTIFIER_BONUS = 0.75
WORD_NAME_BONUS = 0.25
PATH_TOKEN_BONUS = 0.1
PATH_TOKEN_CAP = 3
POOL_LINK_BONUS = 0.05
POOL_LINK_CAP = 3
DEGREE_BONUS = 0.03
DEGREE_CAP = 3
PROSE_PENALTY = -0.25
EXTERNAL_PENALTY = -0.5
VENDOR_PENALTY = -0.5
TEST_PENALTY = -0.15

VENDOR_DIRS = {
    "node_modules",
    "vendor",
    "3rdparty",
    "third_party",
    "site-packages",
    ".venv",
}
TEST_WORDS = {"test", "tests", "spec", "specs"}
PROSE_TYPES = {"heading", "image"}
PROSE_PROJECTS = {"docs", "memory", "suggestions"}
STOPWORDS = {
    "and",
    "are",
    "does",
    "for",
    "from",
    "how",
    "into",
    "the",
    "that",
    "this",
    "what",
    "when",
    "where",
    "which",
    "who",
    "why",
    "with",
}

WORD = re.compile(r"[A-Za-z0-9_$]+")
SHAPED = re.compile(r"[_$0-9]|[a-z][A-Z]")
CAMEL = re.compile(r"([a-z0-9])([A-Z])")
NOT_WORD = re.compile(r"[^A-Za-z0-9]+")
EXTENSION = re.compile(r"\.[A-Za-z0-9]+\Z")
TEST_FILE = re.compile(r"^test_|_test\.|\.(test|spec)\.[A-Za-z0-9]+\Z")


def keep(word: str) -> bool:
    """Say whether a word is worth matching on."""
    return len(word) >= 3 and word not in STOPWORDS


def identifiers(text: str) -> dict[str, bool]:
    """Return the words of a text, each with whether it is shaped like a symbol.

    An identifier-shaped token (readLimit, queue_embeddings) names a symbol; a
    plain word that happens to equal a name is weaker evidence.
    """
    found: dict[str, bool] = {}
    for word in WORD.findall(text):
        lower = word.lower()
        if keep(lower):
            found[lower] = found.get(lower, False) or bool(SHAPED.search(word))
    return found


def subwords(text: str) -> list[str]:
    """Return the distinct words of a text, camelCase split, in order."""
    split = [word.lower() for word in NOT_WORD.split(CAMEL.sub(r"\1 \2", text))]
    return list(dict.fromkeys(word for word in split if keep(word)))


def without_extension(path: str) -> str:
    """Drop the extension of a path."""
    return EXTENSION.sub("", path)


def bare_name(name: str) -> str:
    """Return a node name as a query would spell it."""
    return name.removesuffix("()").removeprefix(".").lower()


def is_vendored(path: str) -> bool:
    """Say whether a path lies in somebody else's code."""
    return any(segment in VENDOR_DIRS for segment in path.split("/"))


def is_test(path: str) -> bool:
    """Say whether a path is a test."""
    segments = path.split("/")
    file = segments.pop() if segments else ""
    return any(segment in ("test", "tests") for segment in segments) or bool(
        TEST_FILE.search(file)
    )


def is_prose(row: Row) -> bool:
    """Say whether a row is prose in a tree that is not prose."""
    return row["type"] in PROSE_TYPES and row["project_type"] not in PROSE_PROJECTS


def is_weak(row: Row) -> bool:
    """Say whether a row is weak evidence that its file answers the query."""
    return row["type"].startswith("external_") or is_prose(row)


def pool_links(rows: list[Row]) -> list[int]:
    """Count, per row, the other strong rows of the pool in the same file."""
    per_file: dict[tuple[str, str], int] = {}
    for row in rows:
        if row["file_path"] is not None and not is_weak(row):
            key = (row["project"], row["file_path"])
            per_file[key] = per_file.get(key, 0) + 1
    links = []
    for row in rows:
        same = (
            0
            if row["file_path"] is None
            else per_file.get((row["project"], row["file_path"]), 0)
        )
        links.append(max(same - (0 if is_weak(row) else 1), 0))
    return links


def bonus(
    row: Row, links: int, query_identifiers: dict[str, bool], query_words: list[str]
) -> float:
    """Return what a row gains or loses on top of its fused rank."""
    units = 0.0

    shaped = query_identifiers.get(bare_name(row["name"]))
    if shaped is not None:
        units += IDENTIFIER_BONUS if shaped else WORD_NAME_BONUS

    # A directory carries no file path; its id is the path its words are in.
    path = row["file_path"]
    if path is None and row["type"] == "directory":
        path = row["id"]
    if path is not None:
        segments = subwords(without_extension(path))
        if row["type"] == "directory":
            hits = sum(
                any(word.startswith(part) or part.startswith(word) for part in segments)
                for word in query_words
            )
        else:
            hits = sum(word in segments for word in query_words)
        units += min(hits, PATH_TOKEN_CAP) * PATH_TOKEN_BONUS
        if is_vendored(path):
            units += VENDOR_PENALTY
        elif is_test(path) and not any(word in TEST_WORDS for word in query_words):
            units += TEST_PENALTY

    if row["type"].startswith("external_"):
        units += EXTERNAL_PENALTY
    elif is_prose(row):
        units += PROSE_PENALTY

    units += min(links, POOL_LINK_CAP) * POOL_LINK_BONUS
    units += min(math.log1p(row["in_degree"]), DEGREE_CAP) * DEGREE_BONUS

    return units * UNIT


def rerank(
    rows: list[Row], query: str, limit: int, enabled: bool
) -> list[dict[str, Any]]:
    """Order fused candidates and cut them to `limit`.

    Projects are interleaved, so each one's best row comes before any
    project's second. With `enabled` false the score is the fused RRF alone.
    """
    query_identifiers = identifiers(query)
    query_words = subwords(query)
    links = pool_links(rows)

    scored = [
        {
            "row": row,
            "score": row["rrf"]
            + (
                bonus(row, links[index], query_identifiers, query_words)
                if enabled
                else 0
            ),
        }
        for index, row in enumerate(rows)
    ]
    scored.sort(key=lambda item: (-item["score"], item["row"]["id"]))

    seen: dict[str, int] = {}
    placed = []
    for item in scored:
        rank = seen.get(item["row"]["project"], 0) + 1
        seen[item["row"]["project"]] = rank
        placed.append((rank, item))
    placed.sort(key=lambda pair: (pair[0], -pair[1]["score"], pair[1]["row"]["id"]))

    return [item for _, item in placed[:limit]]
