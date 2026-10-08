"""Record what an MCP server answers, or check a server against a recording.

    parity.py record --url http://127.0.0.1:53000
    parity.py check  --url http://127.0.0.1:53001

Both walk the same calls over the eval corpus: every tool once, then other
scopes, shapes and refusals. What differs between two runs of one server is
taken out: times and hashes are masked, text hits are put in one order, and
the walk that counts is the second, once the writes of the first are in.
"""

from __future__ import annotations

import argparse
import difflib
import json
import re
import sys
from pathlib import Path
from typing import Any

import anyio
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "packages" / "mcp" / "tests"))

from cases import CASES, EXTRA, PROJECT, Case  # noqa: E402

ANSWERS = ROOT / "eval" / "parity" / "answers.json"
TIME = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?Z")
SHA256 = re.compile(r"\b[0-9a-f]{64}\b")


def text_files(value: Any, into: set[str]) -> set[str]:  # noqa: ANN401
    """Collect the files an answer names on text evidence."""
    if isinstance(value, dict):
        if value.get("evidence") == "text":
            into.add(str(value.get("file_path")))
        for inner in value.values():
            text_files(inner, into)
    elif isinstance(value, list):
        for inner in value:
            text_files(inner, into)
    return into


def ordered(value: Any, files: set[str]) -> Any:  # noqa: ANN401
    """Sort text hits within the places they hold, leaving the rest where it is."""
    if isinstance(value, dict):
        return {key: ordered(inner, files) for key, inner in value.items()}
    if not isinstance(value, list):
        return value
    items = [ordered(inner, files) for inner in value]
    if all(isinstance(one, str) for one in items):
        places = [at for at, one in enumerate(items) if one in files]
        moved = sorted(items[at] for at in places)
    else:
        places = [
            at
            for at, one in enumerate(items)
            if isinstance(one, dict) and one.get("evidence") == "text"
        ]
        moved = sorted(
            (items[at] for at in places),
            key=lambda one: (str(one.get("project")), str(one.get("id"))),
        )
    for at, one in zip(places, moved, strict=True):
        items[at] = one
    return items


def settled(text: str) -> str:
    """Give text hits one order: the tree search returns files as it finds them."""
    try:
        value = json.loads(text)
    except ValueError:
        return text
    files = text_files(value, set())
    if not files:
        return text
    return json.dumps(ordered(value, files), indent=2, ensure_ascii=False)


def masked(text: str, tool: str) -> str:
    """Hide what changes between two runs of the same server."""
    text = settled(SHA256.sub("<sha256>", TIME.sub("<time>", text)))
    # The skill text is the repository's own file, not the server's answer.
    return text.split("\n\n", 1)[0] if tool == "get_skill" else text


def kept(body: str, key: str) -> str:
    """Read a value out of the first row of an answer, for a later call."""
    rows = json.loads(body)
    value = rows[0].get(key) if rows else None
    return value if isinstance(value, str) else ""


async def walk(url: str, project: str | None, cases: list[Case]) -> dict[str, Any]:
    """Open one session and return its tool list and its answers."""
    address = f"{url}/mcp/{project}" if project else f"{url}/mcp"
    answers: list[dict[str, Any]] = []
    state: dict[str, str] = {}
    async with (
        streamablehttp_client(address) as (read, write, _),
        ClientSession(read, write) as session,
    ):
        opened = await session.initialize()
        listed = await session.list_tools()
        for case in cases:
            args = case.args(state)
            result = await session.call_tool(case.tool, args)
            body = "\n".join(getattr(part, "text", "") for part in result.content)
            if case.keep is not None and not result.isError:
                state[case.keep] = kept(body, case.keep)
            answers.append(
                {
                    "tool": case.tool,
                    "args": args,
                    "error": bool(result.isError),
                    "text": masked(body, case.tool),
                }
            )
    return {
        "instructions": masked(opened.instructions or "", ""),
        "tools": [
            {
                "name": tool.name,
                "description": tool.description,
                "inputSchema": tool.inputSchema,
            }
            for tool in listed.tools
        ],
        "answers": answers,
    }


async def gather(url: str) -> dict[str, Any]:
    """Walk a session on the project, then one opened on no project."""
    unnamed = [case for case in EXTRA if case.tool.startswith(("search", "list_"))]
    # The walk writes a summary and a hash that its own earlier calls read, so
    # a fresh stack is walked once before the walk that counts.
    await walk(url, PROJECT, [*CASES, *EXTRA])
    return {
        "named": await walk(url, PROJECT, [*CASES, *EXTRA]),
        "unnamed": await walk(url, None, [CASES[0], *unnamed]),
    }


def differences(recorded: Any, found: Any, where: str) -> list[str]:  # noqa: ANN401
    """Return a readable difference for every place two answers disagree."""
    if isinstance(recorded, dict) and isinstance(found, dict):
        out = []
        for key in dict.fromkeys([*recorded, *found]):
            out += differences(recorded.get(key), found.get(key), f"{where}.{key}")
        return out
    if isinstance(recorded, list) and isinstance(found, list):
        out = []
        if len(recorded) != len(found):
            out.append(f"{where}: {len(recorded)} recorded, {len(found)} found")
        for index, (one, other) in enumerate(zip(recorded, found, strict=False)):
            label = one.get("tool", index) if isinstance(one, dict) else index
            out += differences(one, other, f"{where}[{label}]")
        return out
    if recorded == found:
        return []
    if isinstance(recorded, str) and isinstance(found, str):
        diff = difflib.unified_diff(
            recorded.splitlines(), found.splitlines(), "recorded", "found", lineterm=""
        )
        return [f"{where}:", *list(diff)[:60]]
    return [f"{where}: recorded {recorded!r}, found {found!r}"]


def main() -> int:
    """Run one of the two commands."""
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("command", choices=["record", "check"])
    parser.add_argument("--url", required=True, help="where the server answers")
    parser.add_argument("--file", type=Path, default=ANSWERS)
    asked = parser.parse_args()

    found = anyio.run(gather, asked.url.rstrip("/"))
    if asked.command == "record":
        asked.file.parent.mkdir(parents=True, exist_ok=True)
        asked.file.write_text(
            json.dumps(found, indent=1, ensure_ascii=True) + "\n", encoding="utf-8"
        )
        calls = sum(len(one["answers"]) for one in found.values())
        print(f"recorded {calls} answers to {asked.file}")
        return 0

    recorded = json.loads(asked.file.read_text(encoding="utf-8"))
    report = differences(recorded, found, "answers")
    if report:
        print("\n".join(report))
        print(f"\n{sum(1 for line in report if line.endswith(':'))} answers differ")
        return 1
    calls = sum(len(one["answers"]) for one in found.values())
    print(f"{calls} answers match {asked.file}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
