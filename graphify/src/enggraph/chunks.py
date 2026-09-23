"""Cut a file into the pieces a vector is written for.

A file node names a file and nothing finer, and entity nodes have carried no
source text since the content column was emptied, so a chunk is what points a
reader at the part of a file that matched. It carries its line range for that
reason alone.

The window is measured in characters and the overlap in lines: nothing here
has a tokenizer, and a declaration that falls across a boundary should be
whole in one of the two chunks rather than halved in both.

Nothing in this module reads a database or a file. It takes text and returns
pieces of it.
"""

from __future__ import annotations

from bisect import bisect_right
from collections.abc import Sequence
from dataclasses import dataclass

from enggraph.config import EMBED_CHUNK_CHARS, EMBED_CHUNK_OVERLAP_LINES

# Raised whenever the cut or the embedded text changes: stored on every chunk,
# a stale revision puts the file back on the embedding queue.
CHUNKER_REVISION = 2

# A window that fills up is cut before an entity starting in its last part.
BOUNDARY_SLACK = 0.4


@dataclass(frozen=True)
class Chunk:
    """One window of a file, and where in the file it was."""

    index: int
    start_line: int
    end_line: int
    text: str
    entity: str | None = None


def _entity_of(
    starts: list[int], names: list[str], start_line: int, end_line: int
) -> str | None:
    at = bisect_right(starts, start_line)
    if at > 0:
        return names[at - 1]
    if starts and starts[0] <= end_line:
        return names[0]
    return None


def split(
    text: str,
    max_chars: int = EMBED_CHUNK_CHARS,
    overlap_lines: int = EMBED_CHUNK_OVERLAP_LINES,
    entities: Sequence[tuple[int, str]] = (),
) -> list[Chunk]:
    """Cut text into overlapping windows, on line boundaries.

    Lines are numbered from one and both ends are inclusive, the way an editor
    counts them. A line longer than the window is not split: a minified bundle
    is one line and cutting it mid-token would embed neither half usefully, so
    it becomes one oversized chunk and the caller's size limits decide the
    rest.

    A file of nothing but whitespace yields no chunks. Embedding it would
    write a vector of the model's opinion of an empty string, which every
    other empty file would then match.

    `entities` are (start line, name) pairs: a full window is cut before an
    entity starting in its last part, and each chunk names the entity it is in.
    """
    if not text.strip():
        return []
    window = max(1, max_chars)
    overlap = max(0, overlap_lines)

    lines = text.splitlines()
    ordered = sorted(entities)
    starts = [line for line, _ in ordered]
    names = [name for _, name in ordered]
    # Zero-based index of the line an entity starts on.
    cuts = sorted({line - 1 for line in starts if line > 1})
    chunks: list[Chunk] = []
    start = 0
    while start < len(lines):
        size = 0
        end = start
        while end < len(lines):
            # The newline the split removed counts: it is what the model reads
            # between two lines, and leaving it out lets a window overfill.
            length = len(lines[end]) + 1
            if end > start and size + length > window:
                break
            size += length
            end += 1

        clean = False
        if end < len(lines) and cuts:
            floor = start + max(1, int((end - start) * (1 - BOUNDARY_SLACK)))
            at = bisect_right(cuts, end) - 1
            if at >= 0 and floor <= cuts[at] <= end:
                end = cuts[at]
                clean = True

        body = "\n".join(lines[start:end])
        if body.strip():
            chunks.append(
                Chunk(
                    index=len(chunks),
                    start_line=start + 1,
                    end_line=end,
                    text=body,
                    entity=_entity_of(starts, names, start + 1, end),
                )
            )

        if end >= len(lines):
            break
        # Step back by the overlap, never past the start of this window: a
        # window of one line with an overlap of five would otherwise stand
        # still and the loop would never end.
        start = end if clean else max(start + 1, end - overlap)
    return chunks
