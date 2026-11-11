"""Point one codebase's agent configuration at the enggraph MCP server.

Reached through `make install`. Writes `.mcp.json` for Claude Code and
`.gemini/settings.json` for the Gemini CLI, which differ only in the key the
address is spelled with. Both files belong to the codebase being onboarded,
not to this repository.

It also lets Claude call the server's read tools without a prompt, by adding
one allow rule per read tool to `~/.claude/settings.json`. Every tool that
writes - save, drop, a file hash - is left to ask each time: indexed text
reaches the agent, and what it says must not rewrite records unseen. That file
is the user's, not the codebase's, so the rules cover every project onboarded
now or later; `PERMISSIONS=0` in the environment skips the step. Gemini asks
before every call unless `GEMINI_TRUST=1` writes `trust: true`, which skips
the prompt for writes too.

The rule throughout is that nothing existing is overwritten: a file that is
already there gains the `enggraph` server beside whatever else it holds, and an
`enggraph` entry that already names an address is left exactly as it is. A URL
someone chose is a decision, and this script is not the place to reverse it.
The permission step reads the same way: a server already denied, or already
set to ask, keeps the answer whoever wrote it wanted.

The one exception is the rename: this server was registered as `context` while
the project was called claude-context-mcp. An entry of that name pointing at
this stack is moved under the new key rather than left beside it, because two
keys on one address would list every tool twice.

Both files carry the header `Authorization: Bearer ${ENGGRAPH_TOKEN}` and never
the token: the operator puts the token into the agent's environment, for
Claude the `env` block of its settings.json. An entry already pointing at this
stack gains the header when it has no Authorization header of its own; nothing
else in it changes.
"""

from __future__ import annotations

import json
import os
import sys
from typing import Any

SERVER = "enggraph"
LEGACY_SERVER = "context"
ALLOW_RULE = f"mcp__{SERVER}"
LEGACY_ALLOW_RULE = f"mcp__{LEGACY_SERVER}"
# The tools that change nothing, named one by one: a bare server name would
# cover the writes too. test_mcp_register.py holds this to tools.json.
READ_TOOLS = (
    "describe_project",
    "find_callees",
    "find_callers",
    "find_definition",
    "find_implementations",
    "find_linked_name",
    "find_references",
    "find_tests",
    "get_code_graph_neighbors",
    "get_context",
    "get_file_hash",
    "get_issues",
    "get_memory",
    "get_node_summary",
    "get_overview",
    "get_plans",
    "get_project_links",
    "get_prompts",
    "get_roadmaps",
    "get_skill",
    "get_suggestions",
    "impact_analysis",
    "list_indexed_files",
    "list_projects",
    "list_skills",
    "search_code",
    "search_code_nodes",
    "search_text",
    "shortest_path",
    "trace",
)
ALLOW_RULES = tuple(f"mcp__{SERVER}__{tool}" for tool in READ_TOOLS)
DROP_TOOLS = ("drop_project", "drop_memory", "drop_plan", "drop_suggestion")
AUTHORIZATION = {"Authorization": "Bearer ${ENGGRAPH_TOKEN}"}
LEGACY_RULES = (
    LEGACY_ALLOW_RULE,
    *(f"mcp__{LEGACY_SERVER}__{tool}" for tool in DROP_TOOLS),
)


def load(path: str) -> tuple[dict[str, Any] | None, str]:
    """Return the JSON object held in path, and why it could not be read."""
    if not os.path.isfile(path):
        return {}, ""
    try:
        with open(path, encoding="utf-8") as handle:
            document = json.load(handle)
    except (OSError, ValueError) as error:
        return None, str(error)
    if not isinstance(document, dict):
        return None, "not a JSON object"
    return document, ""


def save(path: str, document: dict[str, Any]) -> None:
    """Write the document back, in the shape prettier would leave it."""
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, mode=0o700, exist_ok=True)
    # A new file may come to hold a token, so it starts as its owner's alone.
    opened = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(opened, "w", encoding="utf-8") as handle:
        json.dump(document, handle, indent=2)
        handle.write("\n")


def address_of(entry: object) -> str:
    """Return the URL an mcpServers entry names, under either spelling."""
    if not isinstance(entry, dict):
        return ""
    url = entry.get("url") or entry.get("httpUrl")
    return url if isinstance(url, str) else ""


def same_stack(entry: object, url: str) -> bool:
    """Say whether an entry reaches the stack at url, whichever project it names."""
    prefix = url.rsplit("/", 1)[0] + "/"
    return address_of(entry).startswith(prefix)


def authorize(entry: dict[str, Any]) -> bool:
    """Give an entry the Authorization header unless it already carries one."""
    headers = entry.setdefault("headers", {})
    if not isinstance(headers, dict):
        return False
    if any(key.lower() == "authorization" for key in headers):
        return False
    headers.update(AUTHORIZATION)
    return True


def rename_legacy(servers: dict[str, Any], url: str) -> bool:
    """Move a `context` entry of this stack under the new key.

    Only one pointing at the same host and port: a `context` server reaching
    anywhere else belongs to somebody else and keeps its name.
    """
    if SERVER in servers or LEGACY_SERVER not in servers:
        return False
    if not same_stack(servers[LEGACY_SERVER], url):
        return False
    servers[SERVER] = servers.pop(LEGACY_SERVER)
    return True


def register(path: str, entry: dict[str, Any]) -> str:
    """Add the enggraph server to one configuration file, and say what happened."""
    document, error = load(path)
    if document is None:
        return f"left alone ({error})"
    existed = os.path.isfile(path)
    servers = document.setdefault("mcpServers", {})
    if not isinstance(servers, dict):
        return "left alone (mcpServers is not an object)"
    renamed = rename_legacy(servers, address_of(entry))
    current = servers.get(SERVER)
    if isinstance(current, dict):
        authorized = same_stack(current, address_of(entry)) and authorize(current)
        if renamed or authorized:
            save(path, document)
        said = f"renamed from {LEGACY_SERVER}" if renamed else ""
        if authorized:
            return f"{said}, token header added" if said else "token header added"
        if said:
            return said
        address = current.get("url") or current.get("httpUrl") or "an address"
        return f"kept ({SERVER} already points at {address})"
    servers[SERVER] = entry
    save(path, document)
    return "merged" if existed else "written"


def settings_path() -> str:
    """Where Claude Code keeps the settings that apply to every project."""
    root = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(
        os.path.expanduser("~"), ".claude"
    )
    return os.path.join(root, "settings.json")


def permit(path: str) -> str:
    """Allow the server's read tools in Claude's user settings; say what happened."""
    document, error = load(path)
    if document is None:
        return f"left alone ({error})"
    permissions = document.setdefault("permissions", {})
    if not isinstance(permissions, dict):
        return "left alone (permissions is not an object)"
    rules: dict[str, list[str]] = {}
    for key in ("allow", "ask", "deny"):
        listed = permissions.get(key, [])
        if not isinstance(listed, list):
            return f"left alone (permissions.{key} is not a list)"
        rules[key] = list(listed)
    if ALLOW_RULE in rules["deny"] or ALLOW_RULE in rules["ask"]:
        return "kept (the server is already ruled on)"
    # What earlier versions of this script wrote: the rules for the `context`
    # key, and the bare rule that let every write tool run without a prompt.
    stale = (*LEGACY_RULES, ALLOW_RULE)
    dropped = len([rule for rule in rules["allow"] if rule in stale]) + sum(
        len([rule for rule in rules[key] if rule in LEGACY_RULES])
        for key in ("ask", "deny")
    )
    rules["allow"] = [rule for rule in rules["allow"] if rule not in stale]
    for key in ("ask", "deny"):
        rules[key] = [rule for rule in rules[key] if rule not in LEGACY_RULES]
    known = {rule for listed in rules.values() for rule in listed}
    added = [rule for rule in ALLOW_RULES if rule not in known]
    if not added and not dropped:
        return "kept (every rule is already there)"
    rules["allow"].extend(added)
    for key, listed in rules.items():
        if listed:
            permissions[key] = listed
        else:
            permissions.pop(key, None)
    save(path, document)
    if dropped:
        return f"{len(added)} read rules added, {dropped} wider rules dropped"
    return f"{len(added)} read rules added"


def main() -> None:
    """Register the server in both agents' files for one codebase."""
    if len(sys.argv) != 4:
        print("usage: mcp_register.py ROOT PROJECT PORT|STACK_URL", file=sys.stderr)
        raise SystemExit(2)
    root, project, stack = sys.argv[1:]
    base = stack.rstrip("/") if "://" in stack else f"http://localhost:{stack}"
    url = f"{base}/mcp/{project}"
    claude = os.path.join(root, ".mcp.json")
    gemini = os.path.join(root, ".gemini", "settings.json")
    entry: dict[str, Any] = {"type": "http", "url": url, "headers": AUTHORIZATION}
    print(f"  .mcp.json               {register(claude, entry)}")
    entry = {"httpUrl": url, "type": "http", "headers": AUTHORIZATION}
    # trust skips the confirmation of every call, writes included, so it is
    # written only when asked for.
    if os.environ.get("GEMINI_TRUST") == "1":
        entry["trust"] = True
    print(f"  .gemini/settings.json   {register(gemini, entry)}")
    if os.environ.get("PERMISSIONS") == "0":
        print("  claude permissions      skipped (PERMISSIONS=0)")
    else:
        print(f"  claude permissions      {permit(settings_path())}")
    print(f"  address                 {url}")


if __name__ == "__main__":
    main()
