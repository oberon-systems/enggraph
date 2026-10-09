"""Record what the dashboard API answers, or check a dashboard against a recording.

    parity.py record --url http://127.0.0.1:53002
    parity.py check  --url http://127.0.0.1:53002

Both walk the same requests over the eval corpus: every address once, then
its refusals, then writes that put back what they changed. Times, hashes,
durations and serial ids are masked, since two runs never agree on them.
"""

from __future__ import annotations

import argparse
import difflib
import json
import re
import sys
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx

ROOT = Path(__file__).resolve().parents[3]
ANSWERS = ROOT / "eval" / "parity" / "web.json"
TIME = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?Z")
SHA256 = re.compile(r"\b[0-9a-f]{64}\b")
MASKED = {"ms", "stale_seconds", "stats_at", "checked_at", "since"}
# What the dashboard answers when something under it broke: the text is the
# runtime's own and no two runtimes word it alike.
UNABLE = 503
PROJECT = "alpha"
PLAN = "parity-plan"
PROMPT = "parity-prompt"
ROADMAP = "parity-roadmap"
SKILL = "---\nname: parity-skill\ndescription: The parity walk.\n---\n\nText.\n"
JSON = {"content-type": "application/json"}

Call = tuple[str, str, Any]

# {node} is the first node of the project and {skill} the id an import
# answered with: both are read from the server being walked.
READS: list[Call] = [
    ("GET", "/health", None),
    ("GET", "/projects", None),
    ("GET", f"/projects/{PROJECT}", None),
    ("GET", "/projects/gamma", None),
    ("GET", f"/projects/{PROJECT}/settings", None),
    ("GET", f"/projects/{PROJECT}/file-types", None),
    ("GET", f"/projects/{PROJECT}/nodes?limit=5", None),
    ("GET", f"/projects/{PROJECT}/nodes?q=index&type=file", None),
    ("GET", f"/projects/{PROJECT}/nodes?limit=0", None),
    ("GET", f"/projects/{PROJECT}/nodes?offset=-1", None),
    ("GET", f"/projects/{PROJECT}/node?id={{node}}", None),
    ("GET", f"/projects/{PROJECT}/node?id={{node}}&content=1", None),
    ("GET", f"/projects/{PROJECT}/node", None),
    ("GET", f"/projects/{PROJECT}/node?id=gamma", None),
    ("GET", f"/projects/{PROJECT}/neighbors?id={{node}}", None),
    ("GET", f"/projects/{PROJECT}/neighbors?id={{node}}&direction=in", None),
    ("GET", f"/projects/{PROJECT}/neighbors?id={{node}}&direction=up", None),
    ("GET", f"/projects/{PROJECT}/files", None),
    ("GET", f"/projects/{PROJECT}/files?q=py&limit=3", None),
    ("GET", f"/projects/{PROJECT}/knowledge?id={{node}}", None),
    ("GET", f"/projects/{PROJECT}/organizations", None),
    ("GET", f"/projects/{PROJECT}/members", None),
    ("GET", f"/projects/{PROJECT}/index", None),
    ("GET", f"/projects/{PROJECT}/schedule", None),
    ("GET", f"/projects/{PROJECT}/features", None),
    ("GET", f"/projects/{PROJECT}/failures", None),
    ("GET", f"/projects/{PROJECT}/drop-report", None),
    ("GET", f"/projects/{PROJECT}/skills", None),
    ("GET", f"/projects/{PROJECT}/links", None),
    ("GET", f"/projects/{PROJECT}/links?depth=2&direction=out", None),
    ("GET", f"/projects/{PROJECT}/trace?node=./", None),
    ("GET", f"/projects/{PROJECT}/trace", None),
    ("GET", "/plans", None),
    ("GET", "/plans?project=_global&status=active&type=plan&q=par", None),
    ("GET", "/plans/facets", None),
    ("GET", "/plan?id=gamma", None),
    ("GET", "/plan", None),
    ("GET", "/prompts", None),
    ("GET", "/prompts?project=_global&status=active&plan=gamma&q=par", None),
    ("GET", "/prompts/facets", None),
    ("GET", "/prompt?id=gamma", None),
    ("GET", "/prompt", None),
    ("GET", "/roadmaps", None),
    ("GET", f"/roadmaps?project={PROJECT}&status=active", None),
    ("GET", "/roadmap?id=gamma", None),
    ("GET", "/roadmap", None),
    ("GET", "/memories", None),
    ("GET", "/memories?about=_global&q=par", None),
    ("GET", "/memories/facets", None),
    ("GET", "/memory?id=gamma", None),
    ("GET", "/suggestions", None),
    ("GET", f"/suggestions?about={PROJECT}&status=open&kind=no-parser", None),
    ("GET", "/suggestions/facets", None),
    ("GET", "/suggestions/groups", None),
    ("GET", "/suggestions/groups?by=directory&status=open", None),
    ("GET", "/suggestions/groups?by=gamma", None),
    ("GET", "/suggestion?id=gamma", None),
    ("GET", "/records/plan/nodes?id=gamma", None),
    ("GET", "/records/gamma/nodes?id=gamma", None),
    ("GET", "/settings", None),
    ("GET", "/settings/features", None),
    ("GET", "/embeddings", None),
    ("GET", "/summaries", None),
    ("GET", "/skills", None),
    ("GET", "/skills?owner=_global", None),
    ("GET", f"/skills?owner={PROJECT}", None),
    ("GET", "/skill?id=gamma", None),
    ("GET", "/skill?id=999999", None),
    ("GET", "/ask/tools", None),
    ("GET", f"/ask/tools?project={PROJECT}", None),
    ("GET", "/gamma", None),
]

WRITES: list[Call] = [
    ("POST", "/plans", {"id": PLAN, "title": "Parity", "content": "# Parity\n"}),
    ("POST", "/plans", {"id": PLAN, "title": "Parity", "content": "# Again\n"}),
    ("POST", "/plans", {"id": PLAN}),
    ("GET", f"/plan?id={PLAN}", None),
    ("PATCH", f"/plan?id={PLAN}", {"status": "completed", "project": PROJECT}),
    ("PATCH", f"/plan?id={PLAN}", {"project": ""}),
    ("PATCH", "/plan?id=gamma", {"status": "completed"}),
    ("GET", "/plans/facets", None),
    (
        "POST",
        f"/records/plan/nodes?id={PLAN}",
        {"project": PROJECT, "node_id": "{node}"},
    ),
    (
        "POST",
        f"/records/plan/nodes?id={PLAN}",
        {"project": PROJECT, "node_id": "{node}"},
    ),
    (
        "POST",
        f"/records/plan/nodes?id={PLAN}",
        {"project": PROJECT, "node_id": "gamma"},
    ),
    ("GET", f"/records/plan/nodes?id={PLAN}", None),
    ("GET", f"/projects/{PROJECT}/knowledge?id={{node}}", None),
    (
        "DELETE",
        f"/records/plan/nodes?id={PLAN}&project={PROJECT}&node_id={{node}}",
        None,
    ),
    (
        "DELETE",
        f"/records/plan/nodes?id={PLAN}&project={PROJECT}&node_id={{node}}",
        None,
    ),
    (
        "POST",
        "/prompts",
        {"id": PROMPT, "plan_id": PLAN, "title": "Parity", "content": "# Parity\n"},
    ),
    (
        "POST",
        "/prompts",
        {"id": PROMPT, "plan_id": PLAN, "title": "Parity", "content": "# Again\n"},
    ),
    (
        "POST",
        "/prompts",
        {"id": PROMPT, "plan_id": "gamma", "title": "Parity", "content": "# No\n"},
    ),
    ("POST", "/prompts", {"id": PROMPT}),
    ("PATCH", f"/prompt?id={PROMPT}", {"status": "completed"}),
    ("PATCH", "/prompt?id=gamma", {"status": "completed"}),
    ("PATCH", f"/plan?id={PLAN}", {"project": PROJECT}),
    ("GET", f"/prompt?id={PROMPT}", None),
    ("GET", f"/prompts?plan={PLAN}", None),
    ("GET", "/prompts/facets", None),
    ("DELETE", f"/prompt?id={PROMPT}", None),
    ("DELETE", f"/prompt?id={PROMPT}", None),
    (
        "POST",
        "/prompts",
        {"id": PROMPT, "plan_id": PLAN, "title": "Parity", "content": "# Parity\n"},
    ),
    ("POST", "/roadmaps", {"id": ROADMAP, "title": "Parity", "project": PROJECT}),
    ("POST", "/roadmaps", {"id": ROADMAP, "title": "Parity", "project": PROJECT}),
    ("POST", "/roadmaps", {"id": ROADMAP}),
    ("PATCH", f"/roadmap?id={ROADMAP}", {"content": "# Parity\n"}),
    ("PATCH", "/roadmap?id=gamma", {"status": "completed"}),
    (
        "POST",
        f"/roadmap/items?id={ROADMAP}",
        {"id": "first", "title": "First", "plan_id": PLAN},
    ),
    ("POST", f"/roadmap/items?id={ROADMAP}", {"id": "second", "title": "Second"}),
    ("POST", f"/roadmap/items?id={ROADMAP}", {"id": "second", "title": "Second"}),
    ("POST", f"/roadmap/items?id={ROADMAP}", {"id": "third"}),
    (
        "POST",
        f"/roadmap/items?id={ROADMAP}",
        {"id": "third", "title": "Third", "plan_id": "gamma"},
    ),
    ("POST", "/roadmap/items?id=gamma", {"id": "first", "title": "First"}),
    ("PATCH", f"/roadmap/item?id={ROADMAP}&item=second", {"position": 1}),
    ("PATCH", f"/roadmap/item?id={ROADMAP}&item=second", {"status": "done"}),
    ("PATCH", f"/roadmap/item?id={ROADMAP}&item=gamma", {"status": "done"}),
    ("GET", f"/roadmap?id={ROADMAP}", None),
    ("GET", f"/roadmaps?project={PROJECT}", None),
    ("DELETE", f"/plan?id={PLAN}", None),
    ("DELETE", f"/plan?id={PLAN}", None),
    ("GET", f"/roadmap?id={ROADMAP}", None),
    ("DELETE", f"/roadmap/item?id={ROADMAP}&item=second", None),
    ("DELETE", f"/roadmap/item?id={ROADMAP}&item=second", None),
    ("DELETE", f"/roadmap?id={ROADMAP}", None),
    ("DELETE", f"/roadmap?id={ROADMAP}", None),
    ("GET", f"/prompt?id={PROMPT}", None),
    ("PUT", f"/projects/{PROJECT}/settings", {"ignore_patterns": "  build/\n"}),
    ("GET", f"/projects/{PROJECT}/settings", None),
    ("PUT", f"/projects/{PROJECT}/settings", {"ignore_patterns": ""}),
    (
        "PUT",
        f"/projects/{PROJECT}/indexing",
        {"mode": "periodic", "interval_minutes": 30, "enabled": True},
    ),
    ("PUT", f"/projects/{PROJECT}/indexing", {"mode": "hourly"}),
    ("PUT", f"/projects/{PROJECT}/indexing", {"interval_minutes": 0}),
    ("PUT", f"/projects/{PROJECT}/indexing", {"interval_minutes": 1.5}),
    ("GET", f"/projects/{PROJECT}/schedule", None),
    ("PUT", f"/projects/{PROJECT}/indexing", {}),
    (
        "PUT",
        f"/projects/{PROJECT}/features/embedding",
        {"enabled": True, "batch": 4, "tick_seconds": 9, "server_url": "http://x/"},
    ),
    ("PUT", f"/projects/{PROJECT}/features/embedding", {"batch": None}),
    ("PUT", f"/projects/{PROJECT}/features/embedding", {"server_url": "ftp://x"}),
    ("PUT", f"/projects/{PROJECT}/features/embedding", {"enabled": "yes"}),
    ("PUT", f"/projects/{PROJECT}/features/gamma", {"enabled": True}),
    ("GET", f"/projects/{PROJECT}/settings", None),
    ("GET", f"/projects/{PROJECT}/features", None),
    ("PUT", f"/projects/{PROJECT}/features/embedding", {}),
    ("DELETE", f"/projects/{PROJECT}/settings", None),
    ("PUT", "/settings", {"ignore_patterns": "vendor/"}),
    ("GET", "/settings", None),
    ("PUT", "/settings", {"ignore_patterns": ""}),
    ("PUT", "/settings/indexing", {"allowed": True, "mode": "off"}),
    ("PUT", "/settings/indexing", {}),
    ("PUT", "/settings/features/summarize", {"allowed": True, "budget_seconds": 60}),
    ("GET", "/settings/features", None),
    ("PUT", "/settings/features/summarize", {}),
    ("PATCH", f"/projects/{PROJECT}", {"description": "  The parity corpus.  "}),
    ("PATCH", f"/projects/{PROJECT}", {"description": ""}),
    ("PATCH", f"/projects/{PROJECT}", {"description": "x" * 501}),
    ("PATCH", f"/projects/{PROJECT}", {"type": "plans"}),
    ("PATCH", f"/projects/{PROJECT}", {"type": "gamma"}),
    ("PATCH", f"/projects/{PROJECT}", {"type": "docs"}),
    ("PATCH", f"/projects/{PROJECT}", {"type": "codebase"}),
    ("PATCH", f"/projects/{PROJECT}", {}),
    ("PATCH", f"/projects/{PROJECT}", {"type": 5}),
    ("DELETE", f"/projects/{PROJECT}", {}),
    ("POST", f"/projects/{PROJECT}/rename", {"project": " "}),
    ("POST", f"/projects/{PROJECT}/members", {"project": "gamma"}),
    ("PUT", f"/projects/{PROJECT}/skills/gamma", {"enabled": True}),
    ("PUT", f"/projects/{PROJECT}/skills/1", {"enabled": "yes"}),
    ("POST", "/skills", {"content": SKILL, "owner": PROJECT}),
    ("GET", f"/projects/{PROJECT}/skills", None),
    ("PUT", f"/projects/{PROJECT}/skills/{{skill}}", {"enabled": False}),
    ("GET", "/skill?id={skill}", None),
    ("DELETE", "/skill?id={skill}", None),
    ("DELETE", "/skill?id={skill}", None),
    ("POST", "/skills", {"content": "no frontmatter"}),
    ("POST", "/skills", {"content": SKILL, "owner": "gamma"}),
    (
        "POST",
        f"/projects/{PROJECT}/links",
        {"to": "beta", "relation": "documents", "note": "parity"},
    ),
    ("GET", f"/projects/{PROJECT}/links", None),
    ("DELETE", f"/projects/{PROJECT}/links", {"to": "beta", "relation": "documents"}),
    ("POST", f"/projects/{PROJECT}/links", {"from": "beta", "to": "beta"}),
    ("POST", f"/projects/{PROJECT}/exports", {"kind": "image", "name": "parity"}),
    ("DELETE", f"/projects/{PROJECT}/exports", {"kind": "image", "name": "parity"}),
    ("POST", f"/projects/{PROJECT}/exports", {"kind": "image"}),
    ("POST", "/ask", {"tool": "list_projects"}),
    ("POST", "/ask", {"tool": "describe_project", "project": PROJECT}),
    (
        "POST",
        "/ask",
        {"tool": "search_code_nodes", "project": PROJECT, "arguments": {"query": "a"}},
    ),
    ("POST", "/ask", {"tool": "drop_project"}),
    ("POST", "/ask", {"tool": "list_projects", "arguments": []}),
    ("POST", "/ask", {}),
    ("POST", "/gamma", {}),
]


def masked(value: Any, key: str = "") -> Any:  # noqa: ANN401
    """Take out what two runs of one server would not agree on."""
    if key in MASKED and value is not None:
        return "<masked>"
    if key in ("id", "skill_id") and str(value).isdigit():
        return "<id>"
    if isinstance(value, dict):
        return {name: masked(inner, name) for name, inner in value.items()}
    if isinstance(value, list):
        return [masked(inner) for inner in value]
    if isinstance(value, str):
        return SHA256.sub("<sha256>", TIME.sub("<time>", value))
    return value


def answer(client: httpx.Client, call: Call, held: dict[str, str]) -> dict[str, Any]:
    """Make one request and return its status and its body."""
    method, path, body = call
    for name, value in held.items():
        path = path.replace(f"{{{name}}}", quote(value, safe=""))
        if isinstance(body, dict):
            body = {
                key: value if inner == f"{{{name}}}" else inner
                for key, inner in body.items()
            }
    headers = JSON if method != "GET" else {}
    content = None if method == "GET" else json.dumps({} if body is None else body)
    response = client.request(method, f"/api{path}", headers=headers, content=content)
    try:
        said = response.json()
    except ValueError:
        said = response.text
    return {"status": response.status_code, "body": said}


def refusals(client: httpx.Client) -> list[dict[str, Any]]:
    """Ask what the guard turns away: another host, another origin, no JSON."""
    asked = [
        ("GET", "/api/health", {"host": "gamma.example.com"}, None),
        ("POST", "/api/plans", {"origin": "http://gamma.example.com", **JSON}, "{}"),
        ("POST", "/api/plans", {"content-type": "text/plain"}, "id=x"),
        ("POST", "/api/plans", JSON, "{"),
    ]
    out = []
    for method, path, headers, content in asked:
        response = client.request(method, path, headers=headers, content=content)
        status = response.status_code
        # How a body that is not JSON is refused is the framework's own wording.
        body = response.json() if content != "{" else "<refused>"
        held = status if content != "{" else status >= 400
        out.append({"status": held, "body": body})
    return out


def walk(url: str) -> dict[str, Any]:
    """Ask every request of one dashboard and return its answers by request."""
    held: dict[str, str] = {}
    answers: dict[str, Any] = {}
    with httpx.Client(base_url=url, timeout=300) as client:
        for at, call in enumerate(READS + WRITES):
            try:
                said = answer(client, call, held)
            except httpx.HTTPError as error:
                raise SystemExit(f"{call[0]} {call[1]}: {error}") from error
            body = said["body"]
            if call[1].endswith("/nodes?limit=5") and "node" not in held:
                held["node"] = body["items"][0]["id"]
            if call[:2] == ("POST", "/skills") and "skill" not in held:
                held["skill"] = str(body["id"])
            name = f"{at:03d} {call[0]} {call[1]} {json.dumps(call[2])}"
            answers[name] = masked(said)
        for at, said in enumerate(refusals(client)):
            answers[f"guard {at}"] = masked(said)
    return answers


def settled(said: Any) -> Any:  # noqa: ANN401
    """Leave out how a failure nobody raised on purpose is worded."""
    if isinstance(said, dict) and said.get("status") == UNABLE:
        return {"status": UNABLE, "body": "<failed>"}
    return said


def spelled(value: Any) -> list[str]:  # noqa: ANN401
    """Spell an answer over lines, for a difference a reader can follow."""
    return json.dumps(value, indent=2, ensure_ascii=False).splitlines()


def main() -> int:
    """Record the answers, or compare them to a recording."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("mode", choices=["record", "check"])
    parser.add_argument("--url", required=True)
    args = parser.parse_args()
    answers = walk(args.url)
    if args.mode == "record":
        ANSWERS.parent.mkdir(parents=True, exist_ok=True)
        ANSWERS.write_text(
            json.dumps(answers, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        print(f"{len(answers)} answers recorded in {ANSWERS}")
        return 0
    recorded = json.loads(ANSWERS.read_text(encoding="utf-8"))
    recorded = {name: settled(said) for name, said in recorded.items()}
    answers = {name: settled(said) for name, said in answers.items()}
    differ = [name for name in recorded if recorded[name] != answers.get(name)]
    for name in differ:
        print(f"--- {name}")
        lines = difflib.unified_diff(
            spelled(recorded[name]), spelled(answers.get(name)), lineterm="", n=2
        )
        print("\n".join(list(lines)[2:]))
    if differ:
        print(f"{len(differ)} of {len(recorded)} answers differ", file=sys.stderr)
        return 1
    print(f"{len(recorded)} answers match {ANSWERS}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
