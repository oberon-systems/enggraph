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

from dataclasses import dataclass

from enggraph.config import EMBED_CHUNK_CHARS, EMBED_CHUNK_OVERLAP_LINES


@dataclass(frozen=True)
class Chunk:
    """One window of a file, and where in the file it was."""

    index: int
    start_line: int
    end_line: int
    text: str


def split(
    text: str,
    max_chars: int = EMBED_CHUNK_CHARS,
    overlap_lines: int = EMBED_CHUNK_OVERLAP_LINES,
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
    """
    if not text.strip():
        return []
    window = max(1, max_chars)
    overlap = max(0, overlap_lines)

    lines = text.splitlines()
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

        body = "\n".join(lines[start:end])
        if body.strip():
            chunks.append(
                Chunk(
                    index=len(chunks),
                    start_line=start + 1,
                    end_line=end,
                    text=body,
                )
            )

        if end >= len(lines):
            break
        # Step back by the overlap, never past the start of this window: a
        # window of one line with an overlap of five would otherwise stand
        # still and the loop would never end.
        start = max(start + 1, end - overlap)
    return chunks
