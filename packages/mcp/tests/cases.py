"""One call of every tool against the eval corpus, in an order that undoes itself.

Each write is read back and undone, so the graph is the same before and
after. The end-to-end test and the parity check both run this list.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, NamedTuple

PROJECT = "alpha"
SCRATCH = "e2e-scratch"
# Indexed beside alpha, taking its image, both packages, a host and a bucket.
LINKED = "beta"
# A module docstring gives this file a summary to read back and restore.
FILE = "worker/reports/daily.py"
# Only the native parsers record a hash; the upstream extractor keeps its own.
HASHED = "deploy/docker-compose.yml"

State = dict[str, str]


class Case(NamedTuple):
    """One call: the tool, its arguments, and what to keep from the answer."""

    tool: str
    args: Callable[[State], dict[str, Any]]
    keep: str | None = None


def fixed(**args: Any) -> Callable[[State], dict[str, Any]]:  # noqa: ANN401
    """Arguments that do not depend on an earlier answer."""
    return lambda state: dict(args)


CASES = [
    Case("list_projects", fixed()),
    Case("describe_project", fixed(project=PROJECT)),
    Case("drop_project", fixed(name=PROJECT)),
    Case("list_indexed_files", fixed()),
    Case("list_skills", fixed()),
    Case("get_skill", fixed(name="enggraph")),
    Case("search_code_nodes", fixed(query="AuthService")),
    Case("search_code", fixed(query="how are tokens signed")),
    Case("search_text", fixed(pattern="web-01.example.com")),
    Case("get_context", fixed(query="refund flow", token_budget=2000)),
    Case("get_code_graph_neighbors", fixed(node_id=FILE)),
    Case("shortest_path", fixed(source_id="worker/reports/export.py", target_id=FILE)),
    Case("find_definition", fixed(symbol="JwtProvider.verify")),
    Case("find_callers", fixed(symbol="PaymentService.refund")),
    Case("find_callees", fixed(symbol="RefundJob")),
    Case("find_references", fixed(symbol="findByEmail")),
    Case("find_implementations", fixed(symbol="PaymentGateway")),
    Case("find_tests", fixed(symbol="RetryPolicy")),
    Case("impact_analysis", fixed(symbol="src/config.ts")),
    Case("get_overview", fixed(path="worker/", depth=1)),
    Case("get_project_links", fixed(depth=2)),
    Case("trace", fixed(node_id="infra/circuits/web/")),
    Case("find_linked_name", fixed(name="web-01.example.com", project="*")),
    Case(
        "save_project_link",
        fixed(target_project=LINKED, relation="documents", note="e2e"),
    ),
    Case("drop_project_link", fixed(target_project=LINKED, relation="documents")),
    Case("save_project_export", fixed(kind="image", name=f"{SCRATCH}:1")),
    Case("drop_project_export", fixed(kind="image", name=SCRATCH)),
    Case("get_node_summary", fixed(node_id=FILE), keep="summary"),
    Case(
        "save_node_summary",
        lambda state: {"node_id": FILE, "summary": state.get("summary", "")},
    ),
    Case("save_plan", fixed(plan_id=SCRATCH, title="e2e", content="e2e")),
    Case("get_plans", fixed()),
    Case("drop_plan", fixed(plan_id=SCRATCH)),
    Case("save_memory", fixed(memory_id=SCRATCH, title="e2e", text="e2e")),
    Case("get_memory", fixed(memory_id=SCRATCH)),
    Case("drop_memory", fixed(memory_id=SCRATCH)),
    Case("save_suggestion", fixed(suggestion_id=SCRATCH, title="e2e", detail="e2e")),
    Case("get_suggestions", fixed()),
    Case("drop_suggestion", fixed(suggestion_id=SCRATCH)),
    Case("get_file_hash", fixed(rel_path=HASHED), keep="hash"),
    Case("clear_file_hash", fixed(rel_path=HASHED)),
    Case(
        "set_file_hash",
        lambda state: {"rel_path": HASHED, "hash": state.get("hash", "")},
    ),
]

# Calls beyond one per tool: other scopes, other shapes, and refusals. None
# of them writes.
EXTRA = [
    Case("search_text", fixed(pattern="web_01_example_com", loose=True, project="*")),
    Case(
        "find_linked_name", fixed(name="WEB_01_example_com", kind="host", project="*")
    ),
    Case("find_linked_name", fixed(name="web-01", kind="host", project="*")),
    Case("get_project_links", fixed(direction="incoming")),
    Case("get_code_graph_neighbors", fixed(node_id="infra/circuits/web/main.tf")),
    Case(
        "get_code_graph_neighbors",
        fixed(project=LINKED, node_id="deploy/data/roles/portal.yaml"),
    ),
    Case("impact_analysis", fixed(symbol="worker/main.py")),
    Case("impact_analysis", fixed(symbol="PaymentService.refund", depth=2)),
    Case("search_code_nodes", fixed(query="Service", project="*", limit=10)),
    Case("search_code_nodes", fixed(query="config", project_type="codebase")),
    Case("search_code_nodes", fixed(query="zzz", project_type="docs")),
    Case("search_code", fixed(query="readLimit", rerank=False, limit=5)),
    Case("search_code", fixed(query='"refund" -test', project="*")),
    Case("get_context", fixed(query="PaymentService.refund", detail="source")),
    Case("get_context", fixed(query="how is the worker organised", detail="summary")),
    Case(
        "get_context",
        fixed(query="refund", project="*", seeds=3, include_chunks=False),
    ),
    Case("get_overview", fixed()),
    Case("get_overview", fixed(path="src/", depth=2, include_entities=True)),
    Case("get_overview", fixed(path="no/such/dir/")),
    Case("get_node_summary", fixed(node_id="no/such/node.ts")),
    Case("shortest_path", fixed(source_id="README.md", target_id="no/such/node.ts")),
    Case("find_callers", fixed(symbol="no_such_symbol")),
    Case("find_references", fixed(symbol="RetryPolicy", max_hops=2)),
    Case("trace", fixed(node_id="worker/", max_steps=5)),
    Case("list_indexed_files", fixed(project=LINKED)),
    Case("describe_project", fixed(project=LINKED)),
    Case("describe_project", fixed(path="/no/such/tree")),
    Case("get_plans", fixed(project="*", type="*", status="completed")),
    Case("get_memory", fixed(about="*", query="e2e")),
    Case("get_suggestions", fixed(group_by="kind")),
    Case("get_suggestions", fixed(group_by="directory", status="*")),
    Case("get_suggestions", fixed(group_by="nonsense")),
    Case("drop_plan", fixed(plan_id="no-such-plan")),
    Case("drop_memory", fixed(memory_id="no-such-memory")),
    Case("drop_suggestion", fixed(suggestion_id="no-such-gap")),
    Case("drop_project_link", fixed(target_project=LINKED, relation="no_such")),
    Case("drop_project_export", fixed(kind="image", name="no-such-image")),
    Case("save_project_export", fixed(kind="nonsense", name="x")),
    Case("save_project_link", fixed(target_project=PROJECT, relation="documents")),
    Case("get_skill", fixed(name="no-such-skill")),
    Case("describe_project", fixed(project="no-such-project")),
    Case("search_code", fixed(query="x", project=PROJECT, project_type="docs")),
    Case("get_code_graph_neighbors", fixed()),
    Case("save_memory", fixed(memory_id="a/b", title="t", text="t")),
    Case(
        "save_memory",
        fixed(memory_id=SCRATCH, title="e2e", text="e2e", nodes=["no/such/node.ts"]),
    ),
    Case("get_context", fixed(query="x", detail="everything")),
    Case("no_such_tool", fixed()),
]
