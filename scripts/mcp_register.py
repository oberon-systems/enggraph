"""Point one codebase's agent configuration at the enggraph MCP server.

Reached through `make install`. Writes `.mcp.json` for Claude Code and
`.gemini/settings.json` for the Gemini CLI, which differ only in the key the
address is spelled with. Both files belong to the codebase being onboarded,
not to this repository.

It also grants Claude the standing permission Gemini gets from `trust: true`,
by adding one allow rule for the whole server to `~/.claude/settings.json`.
That file is the user's, not the codebase's, so the rule covers every project
onboarded now or later; `PERMISSIONS=0` in the environment skips the step.

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
# A bare server name covers every tool it exposes; the four that delete graph
# state are listed back into ask, which outranks allow.
DROP_TOOLS = ("drop_project", "drop_memory", "drop_plan", "drop_suggestion")
ASK_RULES = tuple(f"mcp__{SERVER}__{tool}" for tool in DROP_TOOLS)
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
        os.makedirs(parent, exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(document, handle, indent=2)
        handle.write("\n")


def address_of(entry: object) -> str:
    """Return the URL an mcpServers entry names, under either spelling."""
    if not isinstance(entry, dict):
        return ""
    url = entry.get("url") or entry.get("httpUrl")
    return url if isinstance(url, str) else ""


def rename_legacy(servers: dict[str, Any], url: str) -> bool:
    """Move a `context` entry of this stack under the new key.

    Only one pointing at the same host and port: a `context` server reaching
    anywhere else belongs to somebody else and keeps its name.
    """
    if SERVER in servers or LEGACY_SERVER not in servers:
        return False
    prefix = url.rsplit("/", 1)[0] + "/"
    if not address_of(servers[LEGACY_SERVER]).startswith(prefix):
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
        if renamed:
            save(path, document)
            return f"renamed from {LEGACY_SERVER}"
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
    """Allow the whole server in Claude's user settings, and say what happened."""
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
    # The rules an earlier version of this script wrote for the `context` key.
    # They name a server nothing registers any more, so they are dropped here
    # rather than left to accumulate; a rule someone wrote by hand for another
    # `context` server would have to be one of these five exactly.
    dropped = sum(
        len([rule for rule in listed if rule in LEGACY_RULES])
        for listed in rules.values()
    )
    for key, listed in rules.items():
        rules[key] = [rule for rule in listed if rule not in LEGACY_RULES]
    known = {rule for listed in rules.values() for rule in listed}
    added = [rule for rule in (ALLOW_RULE, *ASK_RULES) if rule not in known]
    if not added and not dropped:
        return "kept (every rule is already there)"
    if ALLOW_RULE in added:
        rules["allow"].append(ALLOW_RULE)
    rules["ask"].extend(rule for rule in ASK_RULES if rule in added)
    for key, listed in rules.items():
        if listed:
            permissions[key] = listed
        else:
            permissions.pop(key, None)
    save(path, document)
    if dropped:
        return f"{len(added)} rules added, {dropped} legacy rules dropped"
    return f"{len(added)} rules added"


def main() -> None:
    """Register the server in both agents' files for one codebase."""
    if len(sys.argv) != 4:
        print("usage: mcp_register.py ROOT PROJECT PORT", file=sys.stderr)
        raise SystemExit(2)
    root, project, port = sys.argv[1:]
    url = f"http://localhost:{port}/mcp/{project}"
    claude = os.path.join(root, ".mcp.json")
    gemini = os.path.join(root, ".gemini", "settings.json")
    print(f"  .mcp.json               {register(claude, {'type': 'http', 'url': url})}")
    # trust skips the per-call confirmation. Reasonable for a server that only
    # reads a graph of your own code, and unreasonable for one reaching out.
    entry = {"httpUrl": url, "type": "http", "trust": True}
    print(f"  .gemini/settings.json   {register(gemini, entry)}")
    if os.environ.get("PERMISSIONS") == "0":
        print("  claude permissions      skipped (PERMISSIONS=0)")
    else:
        print(f"  claude permissions      {permit(settings_path())}")
    print(f"  address                 {url}")


if __name__ == "__main__":
    main()
