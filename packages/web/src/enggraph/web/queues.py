"""Fold the two model queues into what the Queues page shows."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from enggraph.core import jsjson
from enggraph.web import view

LEVELS = (
    ("directories", "Directories", "director(ies)"),
    ("entities", "Symbols", "symbol(s)"),
)
NO_LEVEL = {"total": 0, "described": 0, "manual": 0, "skipped": 0}
NO_EMBEDDING: dict[str, Any] = {"enabled": False, "gated": False, "queue": {}}


def owed(summary: dict[str, Any]) -> int:
    """Count the files a model owes a summary: all but the ones written by hand."""
    return max(0, summary["files"] - summary["manual"])


def level(summary: dict[str, Any], name: str) -> dict[str, int]:
    """Return one level's counts, zeros when the API sent none."""
    return (summary.get("levels") or {}).get(name) or NO_LEVEL


def level_owed(summary: dict[str, Any], name: str) -> int:
    """Count what a model owes at one level."""
    counts = level(summary, name)
    return max(0, counts["total"] - counts["manual"])


def merge(queues: list[dict[str, int]]) -> dict[str, int]:
    """Add up the same queue across every project, state by state."""
    total: dict[str, int] = {}
    for queue in queues:
        for state, count in queue.items():
            total[state] = total.get(state, 0) + count
    return total


def _tile(
    title: str,
    done: int,
    total: int,
    failed: int,
    note: str,
    queue: dict[str, int] | None = None,
    unit: str = "file(s)",
) -> dict[str, Any]:
    waiting = 0 if queue is None else view.waiting_of(queue)
    percent = view.percent_of(done, total)
    return {
        "title": title,
        "done": done,
        "total": total,
        "failed": failed,
        "note": note,
        "unit": unit,
        "queued": queue is not None,
        "waiting": waiting,
        "percent": "-" if percent is None else f"{percent}%",
        "tone": view.coverage_tone(done, total, waiting, failed) or "",
    }


def _push_note(summaries: list[dict[str, Any]]) -> str:
    pushed = [one for one in summaries if one.get("pushed")]
    down = [one for one in pushed if one["server"]["state"] == "down"]
    note = f"{len(pushed)} project(s) pushed at a server"
    if not down:
        return note
    return (
        f"{note}, {len(down)} of them not answering: nothing is queued "
        f"until {down[0]['server']['url']} is back"
    )


def _progress(
    done: int,
    total: int,
    state: dict[str, Any],
    waiting: int,
    failed: int,
    note: str | None = None,
) -> dict[str, Any]:
    return {
        "done": done,
        "total": total,
        "enabled": state["enabled"],
        "gated": state["gated"],
        "waiting": waiting,
        "failed": failed,
        "note": note,
    }


def _row(summary: dict[str, Any], embedding: dict[str, Any] | None) -> dict[str, Any]:
    vectors = embedding or NO_EMBEDDING
    manual = summary["manual"]
    note = None
    if manual != 0:
        note = (
            f"{manual} file(s) written by hand are left out: "
            "the model never overwrites those"
        )
    return {
        "project": summary["project"],
        "summary": summary,
        "embedding": vectors,
        "summarised": _progress(
            summary["described"],
            owed(summary),
            summary,
            view.waiting_of(summary["queue"]),
            summary.get("skipped") or 0,
            note,
        ),
        "levels": [
            _progress(
                level(summary, name)["described"],
                level_owed(summary, name),
                summary,
                0,
                level(summary, name)["skipped"],
            )
            for name, _, _ in LEVELS
        ],
        "embedded": _progress(
            vectors.get("files") or 0,
            vectors.get("indexed_files") or 0,
            vectors,
            view.waiting_of(vectors["queue"]),
            vectors.get("skipped") or 0,
        ),
        "chunks": vectors.get("chunks") or 0,
        "summary_vectors": _progress(
            vectors.get("summaries_embedded") or 0,
            vectors.get("summaries") or 0,
            vectors,
            0,
            0,
        ),
    }


def fold(summaries: dict[str, Any], embeddings: dict[str, Any]) -> dict[str, Any]:
    """Return the tiles and the rows of the Queues page."""
    by_project = {one["project"]: one for one in embeddings["embeddings"]}
    rows = [_row(one, by_project.get(one["project"])) for one in summaries["summaries"]]
    described = [row["summary"] for row in rows]
    vectors = [row["embedding"] for row in rows]
    tiles = [
        _tile(
            "Summarizing",
            sum(one["described"] for one in described),
            sum(owed(one) for one in described),
            sum(one.get("skipped") or 0 for one in described),
            _push_note(described)
            if summaries.get("loop")
            else "the push loop is off in this process",
            merge([one["queue"] for one in described if one["enabled"]]),
        )
    ]
    for name, title, unit in LEVELS:
        tiles.append(
            _tile(
                title,
                sum(level(one, name)["described"] for one in described),
                sum(level_owed(one, name) for one in described),
                sum(level(one, name)["skipped"] for one in described),
                "described from their children, after the files"
                if name == "directories"
                else "described last, from their own lines",
                unit=unit,
            )
        )
    chunks = sum(one.get("chunks") or 0 for one in vectors)
    tiles.append(
        _tile(
            "Embedding",
            sum(one.get("files") or 0 for one in vectors),
            sum(one.get("indexed_files") or 0 for one in vectors),
            sum(one.get("skipped") or 0 for one in vectors),
            f"{chunks:,} chunk(s) of ~{embeddings.get('chunk_chars')} characters, as "
            f"{embeddings.get('model')}",
            merge([one["queue"] for one in vectors if one["enabled"]]),
        )
    )
    tiles.append(
        _tile(
            "Summary vectors",
            sum(one.get("summaries_embedded") or 0 for one in vectors),
            sum(one.get("summaries") or 0 for one in vectors),
            0,
            "one vector per summary, embedded beside the files",
            unit="summaries",
        )
    )
    stats_at = summaries.get("stats_at")
    counted = None
    if stats_at is not None:
        counted = jsjson.instant(datetime.fromtimestamp(stats_at, UTC))
    return {"tiles": tiles, "rows": rows, "counted": counted}
