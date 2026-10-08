"""Score retrieval over the eval corpus, and gate it against a baseline.

    run.py [--rerank=false] [--update-baseline] [--tolerance=0.02]

Reads EVAL_DATABASE_URL, asks every query of eval/queries.yaml, and writes
eval/results/<mode>.json. A quality metric below the baseline by more than
the tolerance fails the run.
"""

from __future__ import annotations

import json
import math
import os
import re
import sys
import time
from pathlib import Path
from typing import Any

import yaml

from enggraph.mcp import db, jsjson
from enggraph.mcp.context import DEFAULT_EXPAND, DEFAULT_TOKEN_BUDGET, build_context
from enggraph.mcp.rerank import rerank
from enggraph.mcp.search import hybrid_search
from enggraph.mcp.symbols import find_symbol, impact_analysis

ROOT = Path(__file__).resolve().parents[3]
EVAL_DIR = ROOT / "eval"
SEARCH_LIMIT = 10
QUALITY = [
    "recall_at_5",
    "recall_at_10",
    "mrr",
    "context_recall",
    "context_precision",
]
# The kinds a symbol tool answers, scored through that tool as well as search.
KIND_TOOL = {"callers": "find_callers", "tests": "find_tests", "impact": "impact"}

Query = dict[str, Any]
Scored = dict[str, Any]


def options(argv: list[str]) -> dict[str, Any]:
    """Read the flags; anything else is ignored."""

    def flag(name: str) -> str | None:
        return next((arg for arg in argv if arg.startswith(f"--{name}")), None)

    tolerance = flag("tolerance")
    return {
        "rerank": flag("rerank") != "--rerank=false",
        "update_baseline": flag("update-baseline") is not None,
        "tolerance": 0.02 if tolerance is None else float(tolerance.split("=")[1]),
    }


def percentile(values: list[float], p: int) -> float:
    """Return the value a share of the others stay under."""
    if not values:
        return 0
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, math.floor(p / 100 * len(ordered)))]


def mean(values: list[float]) -> float:
    """Return the mean, or zero of nothing."""
    return sum(values) / len(values) if values else 0


def rounded(value: float) -> float:
    """Keep four places."""
    return jsjson.fixed(value, 4)


def recall(expect: list[str], found: list[str]) -> float:
    """Return the share of the expected files that were found."""
    return len([path for path in expect if path in found]) / len(expect)


def place(node: dict[str, Any]) -> str | None:
    """Return what a hit is scored by: a directory has no file path, only an id."""
    if node["file_path"] is not None:
        return str(node["file_path"])
    return str(node["id"]) if node["type"] == "directory" else None


def first_of(files: list[str], expect: list[str]) -> float:
    """Return the reciprocal rank of the first expected file."""
    for index, path in enumerate(files):
        if path in expect:
            return 1 / (index + 1)
    return 0


def noise_by(entries: list[dict[str, Any]], expect: list[str]) -> dict[str, int]:
    """Count the entries outside the expected files by the tier that brought them.

    The relation and direction the `why` of an entry opens with ("calls into",
    "imports_from from", "defined in") name that tier.
    """
    counts: dict[str, int] = {}
    for entry in entries:
        path = place(entry)
        if path is not None and path in expect:
            continue
        if entry["origin"] == "search":
            label = "search"
        else:
            words = re.sub(r"^indirect ", "", entry["why"]).split(" ")
            label = " ".join(words[:2])
        counts[label] = counts.get(label, 0) + 1
    return counts


def score(project: str, q: Query, opts: dict[str, Any]) -> tuple[Scored, bool]:
    """Ask one query of search and of the packet, and score both."""
    expect = q["expect"]
    started = time.perf_counter()
    found = hybrid_search(
        project, None, q["query"], SEARCH_LIMIT, q["kind"] == "overview"
    )
    ranked = rerank(found["rows"], q["query"], SEARCH_LIMIT, opts["rerank"])
    search_ms = (time.perf_counter() - started) * 1000
    places = [place(item["row"]) for item in ranked]
    files = list(dict.fromkeys(path for path in places if path is not None))

    started = time.perf_counter()
    packet = build_context(
        q["query"],
        project,
        None,
        8,
        DEFAULT_TOKEN_BUDGET,
        DEFAULT_EXPAND,
        True,
        opts["rerank"],
        # The code kinds keep the packet the baselines were recorded against.
        "summary" if q["kind"] == "overview" else "source",
    )
    context_ms = (time.perf_counter() - started) * 1000
    entries = packet["entries"]
    in_packet = [path for path in map(place, entries) if path is not None]
    relevant = [entry for entry in entries if place(entry) in expect]

    semantic = found["vectorAvailable"] and any(
        row["vector_rank"] is not None for row in found["rows"]
    )
    return (
        {
            "id": q["id"],
            "kind": q["kind"],
            "recall_at_5": recall(expect, files[:5]),
            "recall_at_10": recall(expect, files[:10]),
            "mrr": first_of(files, expect),
            "context_recall": recall(expect, in_packet),
            "context_precision": len(relevant) / len(entries) if entries else 0,
            "tokens": packet["budget"]["used"],
            "search_ms": search_ms,
            "context_ms": context_ms,
            "missed": [path for path in expect if path not in in_packet],
            "noise": noise_by(entries, expect),
        },
        bool(semantic),
    )


def score_tool(project: str, q: Query) -> Scored | None:
    """Ask the symbol tool a query's kind names, when it names one."""
    tool = KIND_TOOL.get(q["kind"])
    if tool is None or q.get("symbol") is None:
        return None
    if tool == "impact":
        answer = impact_analysis([project], q["symbol"], None, 3)
        found = [node["file_path"] for node in answer["resolved"]]
        found += answer["impact"]["files"]
    else:
        answer = find_symbol([project], tool, q["symbol"], None, 1)
        found = [node["file_path"] for node in answer["resolved"]]
        found += [hit["file_path"] for hit in answer["results"]]
    files = list(dict.fromkeys(path for path in found if path is not None))
    expect = q["expect"]
    return {
        "id": q["id"],
        "tool": "impact_analysis" if tool == "impact" else tool,
        "recall_at_5": recall(expect, files[:5]),
        "recall": recall(expect, files),
        "mrr": first_of(files, expect),
        "missed": [path for path in expect if path not in files],
    }


def summarize_tools(rows: list[Scored]) -> dict[str, dict[str, Any]]:
    """Return the means of each tool's queries."""
    out = {}
    for tool in sorted({row["tool"] for row in rows}):
        mine = [row for row in rows if row["tool"] == tool]
        out[tool] = {
            "queries": len(mine),
            "recall_at_5": rounded(mean([row["recall_at_5"] for row in mine])),
            "recall": rounded(mean([row["recall"] for row in mine])),
            "mrr": rounded(mean([row["mrr"] for row in mine])),
        }
    return out


def summarize(rows: list[Scored]) -> dict[str, Any]:
    """Return the means and the timings of a set of queries."""
    out: dict[str, Any] = {"queries": len(rows)}
    for metric in QUALITY:
        out[metric] = rounded(mean([row[metric] for row in rows]))
    out["tokens_mean"] = math.floor(mean([row["tokens"] for row in rows]) + 0.5)
    for timing in ("search_ms", "context_ms"):
        for p in (50, 95):
            taken = percentile([row[timing] for row in rows], p)
            out[f"{timing}_p{p}"] = rounded(taken)
    return out


def regressions(
    current: dict[str, Any], baseline: dict[str, Any], tolerance: float
) -> list[str]:
    """Name every quality metric that fell below the baseline."""
    return [
        f"{metric}: {current[metric]} < baseline {baseline[metric]} - {tolerance}"
        for metric in QUALITY
        if current[metric] < baseline[metric] - tolerance
    ]


def table(rows: dict[str, Any]) -> str:
    """Lay named rows out as columns: a row is a mapping, or one number."""
    shaped = {
        name: row if isinstance(row, dict) else {"Values": row}
        for name, row in rows.items()
    }
    columns = list(dict.fromkeys(key for row in shaped.values() for key in row))
    lines = [["", *columns]]
    for name, row in shaped.items():
        lines.append([name, *[str(row.get(column, "")) for column in columns]])
    widths = [max(len(line[at]) for line in lines) for at in range(len(lines[0]))]
    return "\n".join(
        "  ".join(
            cell.ljust(width) for cell, width in zip(line, widths, strict=True)
        ).rstrip()
        for line in lines
    )


def written(value: Any) -> str:  # noqa: ANN401
    """Spell a result file as the previous runner did."""
    return jsjson.dumps(value, 2) + "\n"


def main() -> int:
    """Run the benchmark."""
    url = os.environ.get("EVAL_DATABASE_URL")
    if url is None:
        print(
            "EVAL_DATABASE_URL is not set; start the eval stack with `make eval-up`",
            file=sys.stderr,
        )
        return 2
    os.environ["DATABASE_URL"] = url
    opts = options(sys.argv[1:])
    suite = yaml.safe_load((EVAL_DIR / "queries.yaml").read_text(encoding="utf-8"))
    project = suite["project"]

    rows: list[Scored] = []
    tool_rows: list[Scored] = []
    semantic = False
    try:
        for q in suite["queries"]:
            scored, vectors = score(project, q, opts)
            semantic = semantic or vectors
            rows.append(scored)
            by_tool = score_tool(project, q)
            if by_tool is not None:
                tool_rows.append(by_tool)
    finally:
        db.close()

    mode = "hybrid" if semantic else "lexical"
    if not opts["rerank"]:
        mode += "-norerank"
    overall = summarize(rows)
    by_kind = {
        kind: summarize([row for row in rows if row["kind"] == kind])
        for kind in sorted({row["kind"] for row in rows})
    }
    by_tool = summarize_tools(tool_rows)
    noise: dict[str, int] = {}
    for row in rows:
        for label, count in row["noise"].items():
            noise[label] = noise.get(label, 0) + count

    latest = EVAL_DIR / "results" / f"{mode}.json"
    latest.parent.mkdir(parents=True, exist_ok=True)
    latest.write_text(
        written(
            {
                "mode": mode,
                "overall": overall,
                "by_kind": by_kind,
                "by_tool": by_tool,
                "noise": noise,
                "queries": rows,
                "tool_queries": tool_rows,
            }
        ),
        encoding="utf-8",
    )

    print(f"mode {mode}, {len(rows)} queries")
    print(table({"overall": overall, **by_kind}))
    for row in rows:
        if row["missed"]:
            print(f"  {row['id']}: packet missed {', '.join(row['missed'])}")
    print(table(by_tool))
    print("entries outside the expected files, by what brought them in")
    print(table(noise))
    for row in tool_rows:
        if row["missed"]:
            print(f"  {row['id']}: {row['tool']} missed {', '.join(row['missed'])}")

    baseline_path = EVAL_DIR / f"baseline.{mode}.json"
    if opts["update_baseline"]:
        baseline_path.write_text(written(overall), encoding="utf-8")
        print(f"baseline written: {baseline_path}")
        return 0
    try:
        baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        print(f"no baseline for {mode}; run with --update-baseline to record one")
        return 0
    failed = regressions(overall, baseline, opts["tolerance"])
    for line in failed:
        print(f"REGRESSION {line}", file=sys.stderr)
    return 1 if failed else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as error:  # noqa: BLE001 - any failure is exit code 2
        print(error, file=sys.stderr)
        sys.exit(2)
