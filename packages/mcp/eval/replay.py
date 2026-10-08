"""Ask the live server again what its suggestions say it could not answer.

    REPLAY_MCP_URL=http://127.0.0.1:3000 replay.py

Each suggestion that kept its queries and names nodes is a question with a
known answer. The summary goes to eval/results/replay-<time>.json.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import anyio
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

from enggraph.mcp import jsjson

ROOT = Path(__file__).resolve().parents[3]
RESULTS_DIR = ROOT / "eval" / "results"
SEARCH_LIMIT = 10
CONTEXT_BUDGET = 4000
SUGGESTIONS_READ = 50
# A search across every project outlasts the SDK's default of a minute.
CALL_TIMEOUT = timedelta(seconds=600)

Row = dict[str, Any]


def answer(result: Any) -> Any:  # noqa: ANN401
    """Parse the first block: a session's first call appends a skill check."""
    content = result.content or []
    text = getattr(content[0], "text", None) if content else None
    return json.loads(text if text is not None else "null")


def reaches(hit: Row, node: Row, single: bool) -> bool:
    """Say whether a result is the expected node, its file, or inside its directory."""
    theirs = hit.get("project")
    if not single and theirs is not None and theirs != node["project"]:
        return False
    node_id = node["node_id"]
    file = node_id.split("::", 1)[0]
    if hit["id"] == node_id or hit.get("file_path") == file:
        return True
    return node_id.endswith("/") and (
        hit["id"].startswith(node_id)
        or (hit.get("file_path") or "").startswith(node_id)
    )


async def replay(session: ClientSession, gap: Row, query: str) -> Row:
    """Ask one recorded query of search and of the packet."""
    nodes = [node for node in gap["nodes"] if not node["missing"]]
    projects = list(dict.fromkeys(node["project"] for node in nodes))
    single = len(projects) == 1
    project = projects[0] if single else "*"
    found = answer(
        await session.call_tool(
            "search_code",
            {"query": query, "project": project, "limit": SEARCH_LIMIT},
            read_timeout_seconds=CALL_TIMEOUT,
        )
    )
    rank = next(
        (
            index + 1
            for index, hit in enumerate(found)
            if any(reaches(hit, node, single) for node in nodes)
        ),
        None,
    )
    packet = answer(
        await session.call_tool(
            "get_context",
            {
                "query": query,
                "project": project,
                "token_budget": CONTEXT_BUDGET,
                "include_chunks": False,
            },
            read_timeout_seconds=CALL_TIMEOUT,
        )
    )
    return {
        "suggestion": gap["id"],
        "status": gap["status"],
        "query": query,
        "expect": [f"{node['project']}:{node['node_id']}" for node in nodes],
        "rank": rank,
        "in_context": any(
            reaches(entry, node, single)
            for entry in packet["entries"]
            for node in nodes
        ),
    }


def rate(rows: list[Row], passing: list[Row]) -> float:
    """Return the share of the rows that pass."""
    return jsjson.fixed(len(passing) / len(rows), 4) if rows else 0


async def gather(url: str) -> tuple[list[Row], int]:
    """Read the suggestions and replay every query they kept."""
    rows: list[Row] = []
    async with (
        streamablehttp_client(f"{url}/mcp") as (read, write, _),
        ClientSession(read, write) as session,
    ):
        await session.initialize()
        gaps = answer(
            await session.call_tool(
                "get_suggestions",
                {"about": "*", "status": "*", "limit": SUGGESTIONS_READ},
            )
        )
        for gap in gaps:
            if not gap["queries"] or all(one["missing"] for one in gap["nodes"]):
                continue
            for query in gap["queries"]:
                rows.append(await replay(session, gap, query))
    return rows, len(gaps)


def main() -> int:
    """Run the replay."""
    url = os.environ.get("REPLAY_MCP_URL")
    if not url:
        print(
            "REPLAY_MCP_URL is not set; run it through `make replay`", file=sys.stderr
        )
        return 2
    try:
        rows, read = anyio.run(gather, url)
    except Exception as error:  # noqa: BLE001 - the message is the report
        print(error, file=sys.stderr)
        return 1

    resolved = [row for row in rows if row["status"] == "resolved"]
    summary = {
        "suggestions_read": read,
        "queries": len(rows),
        "hit_at_5": rate(rows, [row for row in rows if (row["rank"] or 99) <= 5]),
        "hit_at_10": rate(rows, [row for row in rows if row["rank"] is not None]),
        "in_context": rate(rows, [row for row in rows if row["in_context"]]),
        "resolved_queries": len(resolved),
        "resolved_hit_at_5": rate(
            resolved, [row for row in resolved if (row["rank"] or 99) <= 5]
        ),
    }
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    stamp = jsjson.instant(datetime.now(UTC)).replace(":", "-").replace(".", "-")
    out = RESULTS_DIR / f"replay-{stamp}.json"
    out.write_text(
        jsjson.dumps({"summary": summary, "rows": rows}, 2) + "\n", encoding="utf-8"
    )

    print(jsjson.dumps(summary, 2))
    for row in rows:
        if row["rank"] is None:
            print(f"miss  {row['suggestion']}: {row['query']}")
    print(f"written to {out}")
    if read == SUGGESTIONS_READ:
        print(f"only the {SUGGESTIONS_READ} most hit suggestions were read")
    return 0


if __name__ == "__main__":
    sys.exit(main())
