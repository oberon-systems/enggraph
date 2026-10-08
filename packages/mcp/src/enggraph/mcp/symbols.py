"""Questions about one symbol: where it is, who reaches it, what a change touches."""

from __future__ import annotations

import re
from typing import Any

from enggraph.core import db
from enggraph.mcp import collate
from enggraph.mcp.context import is_test_path, line_from_id
from enggraph.mcp.errors import ToolError
from enggraph.mcp.knowledge import knowledge_for
from enggraph.mcp.links import ancestor_ids, links_into
from enggraph.mcp.worker import grep_trees

DEFAULT_SYMBOL_HOPS = 1
MAX_SYMBOL_HOPS = 3
DEFAULT_IMPACT_DEPTH = 3
TEST_HOPS = 2
MAX_RESOLVED = 10
EDGES_PER_STEP = 50
MAX_GRAPH_ROWS = 200
MAX_TEXT_LINES = 400
LINES_PER_FILE = 10
BUCKET_CAP = 25
NAMES_PER_FILE = 10

CALLS = ["calls"]
INHERITS = ["inherits", "extends", "implements"]
MEMBERSHIP = ["contains", "method"]
CONFIG_RELATIONS = {
    "uses_file",
    "uses_template",
    "reads_vars",
    "uses_role",
    "mounts",
    "builds",
    "notifies",
}

PATH_LIKE = re.compile(
    r"/|\.(ts|tsx|js|jsx|mjs|cjs|py|go|rs|rb|java|kt|cs|php|c|cc|cpp|h|hpp|scala"
    r"|ya?ml|json|toml|tf|hcl|sh|md)\Z",
    re.IGNORECASE,
)
PUBLIC_API_PATH = re.compile(
    r"(^|/)(routes?|controllers?|handlers?|api|server|endpoints?)([./_-]|\Z)",
    re.IGNORECASE,
)
CONFIG_PATH = re.compile(
    r"(^|/)(dockerfile[^/]*|docker-compose[^/]*|config[^/]*|settings[^/]*"
    r"|[^/]*\.(ya?ml|json|toml|ini|cfg|conf|env|tf|hcl|j2))\Z",
    re.IGNORECASE,
)
CALLABLE_TYPES = {"function", "method"}
SPECIAL = re.compile(r"[.*+?^${}()|\[\]\\]")
WORD_CHAR = re.compile(r"[A-Za-z0-9_]")
SEPARATORS = re.compile(r"::|#|\.")

Hit = dict[str, Any]


def bare_name(label: str) -> str:
    """Strip the decoration the upstream extractor puts on a label."""
    return re.sub(r"\A\.+", "", label).removesuffix("()")


def parse_symbol(text: str) -> dict[str, str | None]:
    """Read "Class.method", "function" or a path into owner, name and path."""
    trimmed = text.strip()
    if PATH_LIKE.search(trimmed):
        base = trimmed[trimmed.rfind("/") + 1 :]
        dot = base.find(".")
        return {
            "owner": None,
            "name": base[:dot] if dot > 0 else base,
            "path": trimmed.removeprefix("./"),
        }
    plain = bare_name(trimmed)
    parts = [part for part in SEPARATORS.split(plain) if part != ""]
    name = parts.pop() if parts else plain
    return {"owner": parts.pop() if parts else None, "name": name, "path": None}


def escape_regex(text: str) -> str:
    """Escape what a regular expression would read as syntax."""
    return SPECIAL.sub(lambda match: "\\" + match.group(0), text)


def text_pattern(body: str) -> dict[str, Any]:
    """Return one pattern for PostgreSQL, for Python and for ripgrep.

    Written once in PostgreSQL's ARE dialect; `(?n)` keeps `.` on one line.
    """
    bounded = re.sub(r"\\[mM]", lambda _: "\\b", body)
    return {"pg": f"(?n){body}", "py": re.compile(bounded, re.ASCII), "grep": bounded}


def alternation(names: list[str]) -> str:
    """Return a pattern matching any of the names."""
    unique = list(dict.fromkeys(name for name in names if WORD_CHAR.search(name)))
    if not unique:
        raise ToolError(f'No identifier to match in "{", ".join(names)}"')
    if len(unique) == 1:
        return escape_regex(unique[0])
    return "(" + "|".join(escape_regex(name) for name in unique) + ")"


def word_pattern(names: list[str]) -> dict[str, Any]:
    """Match any of the names as a whole word."""
    return text_pattern(f"\\m{alternation(names)}\\M")


def call_pattern(names: list[str]) -> dict[str, Any]:
    """Match a call of any of the names."""
    return text_pattern(f"\\m{alternation(names)}\\M\\s*\\(")


def implementation_pattern(name: str) -> dict[str, Any]:
    """Match what extends or implements a name."""
    target = escape_regex(name)
    return text_pattern(
        f"\\m(extends|implements)\\s+([\\w.]+\\s*,\\s*)*{target}\\M"
        f"|\\mclass\\s+\\w+\\s*\\([^)]*\\m{target}\\M"
    )


def import_pattern(stem: str) -> dict[str, Any]:
    """Match a line importing a module by its stem."""
    return text_pattern(f"\\m(import|from|require)\\M.*\\m{escape_regex(stem)}\\M")


def line_key(project: str, file: str | None, line: int) -> tuple[str, str, int]:
    """Return what makes two lines the same line."""
    return project, file or "", line


def merge_hits(graph: list[Hit], text: list[Hit]) -> list[Hit]:
    """Return graph hits first, one per node, then text hits on other lines.

    A text hit keeps only the lines no graph hit already stands on, and is
    dropped when none are left.
    """
    seen: set[tuple[str, str]] = set()
    taken: set[tuple[str, str, int]] = set()
    merged: list[Hit] = []
    for hit in graph:
        key = (hit["project"], hit["id"])
        if key in seen:
            continue
        seen.add(key)
        merged.append(hit)
        if hit["line"] is not None:
            taken.add(line_key(hit["project"], hit["file_path"], hit["line"]))
    files: dict[tuple[str, str], Hit] = {}
    for hit in text:
        key = (hit["project"], hit["id"])
        into = files.get(key) or {**hit, "lines": []}
        for line in hit.get("lines") or []:
            where = line_key(hit["project"], hit["file_path"], line)
            if where not in taken and line not in into["lines"]:
                into["lines"].append(line)
        files[key] = into
    for hit in files.values():
        lines = sorted(hit.get("lines") or [])
        if lines:
            merged.append({**hit, "line": lines[0], "lines": lines})
    return merged


def bucket_impact(hits: list[Hit], cross_project: list[Hit] | None = None) -> Hit:
    """Sort what a change reaches into the buckets an agent reads."""
    crossing = cross_project or []
    direct = [hit for hit in hits if hit["hop"] <= 1]
    indirect = [hit for hit in hits if hit["hop"] > 1]
    tests = [hit for hit in hits if is_test_path(hit["file_path"])]
    rest = [hit for hit in hits if not is_test_path(hit["file_path"])]
    public_api = [
        hit
        for hit in rest
        if hit["file_path"] is not None and PUBLIC_API_PATH.search(hit["file_path"])
    ]
    configuration = [
        hit
        for hit in rest
        if hit["relation"] in CONFIG_RELATIONS
        or (hit["file_path"] is not None and CONFIG_PATH.search(hit["file_path"]))
    ]
    files = list(
        dict.fromkeys(
            hit["file_path"]
            for hit in sorted(hits, key=lambda hit: hit["hop"])
            if hit["file_path"] is not None
        )
    )
    return {
        "counts": {
            "direct": len(direct),
            "indirect": len(indirect),
            "tests": len(tests),
            "public_api": len(public_api),
            "configuration": len(configuration),
            "cross_project": len(crossing),
            "files": len(files),
        },
        "files": files,
        "direct": direct[:BUCKET_CAP],
        "indirect": indirect[:BUCKET_CAP],
        "tests": tests[:BUCKET_CAP],
        "public_api": public_api[:BUCKET_CAP],
        "configuration": configuration[:BUCKET_CAP],
        "cross_project": crossing[:BUCKET_CAP],
    }


def cross_project_hits(rows: list[db.Row]) -> list[Hit]:
    """Return what other projects take from nodes, their files or a directory above."""
    return [
        {
            "project": row["source_project"],
            "id": row["source_id"],
            "name": row["node_name"]
            if row["node_name"] is not None
            else row["source_id"],
            "type": row["node_type"] if row["node_type"] is not None else "directory",
            "file_path": row["file_path"],
            "line": line_from_id(row["source_id"]),
            "relation": row["relation_type"],
            "evidence": "link",
            "confidence": None,
            "hop": 1,
            "via": row["target_id"],
            "link": {
                "from": row["source_project"],
                "to": row["target_project"],
                "kind": row["kind"],
                "name": row["name"],
                "origin": row["origin"],
            },
        }
        for row in rows
    ]


def link_targets(nodes: list[dict[str, Any]]) -> list[dict[str, str]]:
    """Return each node, its file and every directory above, once each."""
    seen: set[tuple[str, str]] = set()
    targets: list[dict[str, str]] = []
    for node in nodes:
        file = node.get("file_path")
        ids = [node["id"], *([] if file is None else [file])]
        for one in [*ids, *ancestor_ids(file if file is not None else node["id"])]:
            if (node["project"], one) not in seen:
                seen.add((node["project"], one))
                targets.append({"project": node["project"], "id": one})
    return targets


RESOLVE_FILE = """SELECT n.project, n.id, n.name, n.type, n.file_path, n.summary,
              NULL::text AS owner
         FROM nodes AS n
        WHERE n.project = ANY ($1::text[])
          AND n.type = 'file'
          AND (n.id = $2 OR right(n.id, length($2) + 1) = '/' || $2)
        ORDER BY n.project, length(n.id), n.id
        LIMIT $3"""

RESOLVE_NAME = """SELECT n.project, n.id, n.name, n.type, n.file_path, n.summary,
              o.name AS owner
         FROM nodes AS n
         LEFT JOIN LATERAL (
           SELECT p.name
             FROM edges AS e
             JOIN nodes AS p
               ON p.project = e.project AND p.id = e.source_id
            WHERE e.project = n.project AND e.target_id = n.id
              AND e.relation_type IN ('contains', 'method')
              AND p.type NOT IN ('file', 'directory')
            ORDER BY (p.name = $3::text) DESC, p.id
            LIMIT 1
         ) AS o ON TRUE
        WHERE n.project = ANY ($1::text[])
          AND n.type NOT IN ('file', 'directory')
          AND NOT starts_with(n.type, 'external_')
          AND regexp_replace(ltrim(n.name, '.'), '[(][)]$', '') = $2
          AND ($4::text IS NULL OR n.file_path = $4
               OR right(n.file_path, length($4) + 1) = '/' || $4)
        ORDER BY (o.name IS NOT DISTINCT FROM $3::text) DESC,
                 n.project, n.file_path, n.id
        LIMIT $5"""

WALK = """WITH RECURSIVE walk(project, node_id, hop, via, relation, confidence,
                        path) AS (
       SELECT s.project, s.id, 0, NULL::text, NULL::text, NULL::text,
              ARRAY[s.id]
         FROM UNNEST($1::text[], $2::text[]) AS s(project, id)
       UNION ALL
       SELECT w.project, step.id, w.hop + 1, w.node_id, step.relation,
              step.confidence, w.path || step.id
         FROM walk AS w
         JOIN LATERAL (
           SELECT (CASE WHEN $3::text = 'incoming' THEN e.source_id
                        ELSE e.target_id END)::text AS id,
                  e.relation_type::text AS relation,
                  e.metadata ->> 'confidence' AS confidence
             FROM edges AS e
            WHERE e.project = w.project
              AND (($3::text = 'incoming' AND e.target_id = w.node_id)
                OR ($3::text = 'outgoing' AND e.source_id = w.node_id))
              AND ($4::text[] IS NULL OR e.relation_type = ANY ($4::text[]))
              AND e.relation_type <> ALL ($5::text[])
            ORDER BY e.relation_type, e.source_id, e.target_id
            LIMIT $6
         ) AS step ON TRUE
        WHERE w.hop < $7 AND NOT step.id = ANY (w.path)
     )
     SELECT DISTINCT ON (w.project, w.node_id)
            w.project, w.node_id AS id, w.hop, w.via, w.relation,
            w.confidence, n.name, n.type, n.file_path
       FROM walk AS w
       JOIN nodes AS n ON n.project = w.project AND n.id = w.node_id
      WHERE w.hop > 0 AND NOT starts_with(n.type, 'external_')
      ORDER BY w.project, w.node_id, w.hop
      LIMIT $8"""

MEMBERS = """SELECT e.project, e.target_id AS id, n.name
       FROM UNNEST($1::text[], $2::text[]) AS s(project, id)
       JOIN edges AS e
         ON e.project = s.project AND e.source_id = s.id
        AND e.relation_type = ANY ($3::text[])
       JOIN nodes AS n
         ON n.project = e.project AND n.id = e.target_id
      WHERE NOT starts_with(n.type, 'external_')
      ORDER BY e.project, e.target_id"""

FILE_NODES = """SELECT DISTINCT ON (n.project, n.file_path)
            n.project, n.id, n.name, n.type, n.file_path
       FROM nodes AS n
       JOIN unnest($1::text[], $2::text[]) AS w (project, path)
         ON n.project = w.project AND n.file_path = w.path
      WHERE n.type = 'file'
      ORDER BY n.project, n.file_path, n.id"""


def resolve_symbol(
    members: list[str], ref: dict[str, str | None], file_path: str | None
) -> tuple[list[Hit], list[str]]:
    """Return the nodes a symbol names, and what to say when that is unclear."""
    notes: list[str] = []
    if ref["path"] is not None:
        rows = db.query(RESOLVE_FILE, [members, ref["path"], MAX_RESOLVED])
    else:
        rows = db.query(
            RESOLVE_NAME, [members, ref["name"], ref["owner"], file_path, MAX_RESOLVED]
        )
        if ref["owner"] is not None and rows:
            owned = [row for row in rows if row["owner"] == ref["owner"]]
            if owned:
                rows = owned
            else:
                notes.append(
                    f'No "{ref["name"]}" is linked to "{ref["owner"]}" in the graph; '
                    f'answering for every node named "{ref["name"]}".'
                )
    if not rows:
        named = ref["path"] if ref["path"] is not None else ref["name"]
        notes.append(
            f'No node resolves "{named}"; the extractor may not '
            "declare it (an interface or a type often is not). Text evidence "
            "is still searched by name. search_code_nodes matches by substring."
        )
    elif len(rows) > 1:
        notes.append(
            f"{len(rows)} nodes match; pass file_path to answer for one of them."
        )
    return [{**row, "line": line_from_id(row["id"])} for row in rows], notes


def walk_graph(
    seeds: list[dict[str, Any]],
    include: list[str] | None,
    exclude: list[str],
    direction: str,
    hops: int,
) -> list[Hit]:
    """Walk edges out from the seeds and return each node reached, nearest first."""
    if not seeds or hops < 1:
        return []
    rows = db.query(
        WALK,
        [
            [seed["project"] for seed in seeds],
            [seed["id"] for seed in seeds],
            direction,
            include,
            exclude,
            EDGES_PER_STEP,
            hops,
            MAX_GRAPH_ROWS,
        ],
    )
    hits = [
        {
            "project": row["project"],
            "id": row["id"],
            "name": row["name"],
            "type": row["type"],
            "file_path": row["file_path"],
            "line": line_from_id(row["id"]),
            "relation": row["relation"],
            "evidence": "graph",
            "confidence": row["confidence"],
            "hop": row["hop"],
            "via": row["via"],
        }
        for row in rows
    ]
    return sorted(hits, key=lambda hit: (hit["hop"], collate.key(hit["id"])))


def members_of(seeds: list[Hit]) -> list[db.Row]:
    """Return what the seeds contain: the methods of a class, the symbols of a file."""
    if not seeds:
        return []
    return db.query(
        MEMBERS,
        [
            [seed["project"] for seed in seeds],
            [seed["id"] for seed in seeds],
            MEMBERSHIP,
        ],
    )


TEXT_UNAVAILABLE = (
    "No text evidence: the worker API that reads the mounted trees did not "
    "answer, so the results are graph edges only"
)


def text_hits(
    projects: list[str],
    pattern: dict[str, Any],
    relation: str,
    skip: set[tuple[str, str, int]],
    notes: list[str],
) -> list[Hit]:
    """Return lines of the mounted trees matching a pattern, one hit per file node."""
    try:
        matches = grep_trees(
            {
                "projects": projects,
                "pattern": pattern["grep"],
                "regex": True,
                "loose": False,
                "path": "",
                "limit": MAX_TEXT_LINES,
            }
        )["matches"]
    except Exception:  # noqa: BLE001 - graph edges still answer
        if TEXT_UNAVAILABLE not in notes:
            notes.append(TEXT_UNAVAILABLE)
        return []
    if not matches:
        return []
    rows = db.query(
        FILE_NODES,
        [[one["project"] for one in matches], [one["path"] for one in matches]],
    )
    files = {(row["project"], row["file_path"]): row for row in rows}
    by_file: dict[tuple[str, str], Hit] = {}
    for match in matches:
        key = (match["project"], match["path"])
        node = files.get(key)
        where = line_key(match["project"], match["path"], match["line"])
        if node is None or where in skip:
            continue
        hit = by_file.get(key) or {
            "project": node["project"],
            "id": node["id"],
            "name": node["name"],
            "type": node["type"],
            "file_path": node["file_path"],
            "line": None,
            "lines": [],
            "relation": relation,
            "evidence": "text",
            "confidence": "NAME_MATCH",
            "hop": 1,
        }
        if match["line"] not in hit["lines"]:
            hit["lines"].append(match["line"])
        by_file[key] = hit
    folded = []
    for hit in by_file.values():
        lines = sorted(hit["lines"])[:LINES_PER_FILE]
        folded.append({**hit, "line": lines[0], "lines": lines})
    return folded


def definition_lines(nodes: list[Hit]) -> set[tuple[str, str, int]]:
    """Return the lines the symbol itself is defined on."""
    return {
        line_key(node["project"], node["file_path"], node["line"] or 0)
        for node in nodes
        if node["line"] is not None
    }


CALLS_NOTE = (
    "Calls across files are graph edges for Python, TypeScript and "
    "JavaScript; in other languages a caller in another file is found by "
    "text evidence (NAME_MATCH) alone."
)


def find_definition(projects: list[str], symbol: str, file_path: str | None) -> Hit:
    """Answer where a symbol is defined."""
    nodes, notes = resolve_symbol(projects, parse_symbol(symbol), file_path)
    return {"symbol": symbol, "resolved": nodes, "results": [], "notes": notes}


def find_symbol(
    projects: list[str], tool: str, symbol: str, file_path: str | None, hops: int
) -> Hit:
    """Answer one of the find_* questions about a symbol."""
    ref = parse_symbol(symbol)
    nodes, found_notes = resolve_symbol(projects, ref, file_path)
    name = str(ref["name"])
    names = [name]
    skip = definition_lines(nodes)
    notes = list(found_notes)
    if nodes:
        callable_ = any(node["type"] in CALLABLE_TYPES for node in nodes)
    else:
        callable_ = ref["owner"] is not None or bool(re.match(r"[a-z_]", name))

    graph: list[Hit] = []
    pattern: dict[str, Any] | None = None
    relation = "mentions"

    if tool == "find_callers":
        graph = walk_graph(nodes, CALLS, [], "incoming", hops)
        pattern = call_pattern(names) if callable_ else word_pattern(names)
        relation = "calls" if callable_ else "mentions"
        notes.append(CALLS_NOTE)
    elif tool == "find_callees":
        seeds = nodes
        if any(node["type"] not in CALLABLE_TYPES for node in nodes):
            seeds = [*nodes, *members_of(nodes)]
        graph = walk_graph(seeds, CALLS, [], "outgoing", hops)
        notes.append(
            "Callees come from graph edges alone, so only calls into the same "
            "file are listed."
        )
    elif tool == "find_references":
        graph = walk_graph(nodes, None, MEMBERSHIP, "incoming", hops)
        pattern = word_pattern(names)
    elif tool == "find_implementations":
        graph = walk_graph(nodes, INHERITS, [], "incoming", hops)
        pattern = implementation_pattern(name)
        relation = "implements"
    else:
        graph = walk_graph(nodes, None, MEMBERSHIP, "incoming", max(hops, TEST_HOPS))
        owner = ref["owner"]
        pattern = word_pattern(names if owner is None else [*names, owner])

    text: list[Hit] = []
    if pattern is not None:
        text = text_hits(projects, pattern, relation, skip, notes)
    results = merge_hits(graph, text)
    if tool == "find_tests":
        results = [hit for hit in results if is_test_path(hit["file_path"])]
    return {"symbol": symbol, "resolved": nodes, "results": results, "notes": notes}


def impact_analysis(
    projects: list[str], symbol: str, file_path: str | None, depth: int
) -> Hit:
    """Answer what a change to a symbol or a file would touch."""
    ref = parse_symbol(symbol)
    nodes, found_notes = resolve_symbol(projects, ref, file_path)
    notes = list(found_notes)
    name = str(ref["name"])
    defined = [] if ref["path"] is None else members_of(nodes)
    seeds = [*nodes, *defined]

    graph = walk_graph(seeds, None, MEMBERSHIP, "incoming", depth)

    skip = definition_lines(nodes)
    if ref["path"] is None:
        text = text_hits(projects, word_pattern([name]), "mentions", skip, notes)
    else:
        own = {node["file_path"] for node in nodes}
        importers = text_hits(projects, import_pattern(name), "imports", skip, notes)
        names = [
            one
            for one in dict.fromkeys(bare_name(node["name"]) for node in defined)
            if len(one) > 2
        ][:NAMES_PER_FILE]
        users = (
            text_hits(projects, word_pattern(names), "mentions", skip, notes)
            if names
            else []
        )
        text = [hit for hit in [*importers, *users] if hit["file_path"] not in own]
    notes.append(CALLS_NOTE)
    seeded = {(seed["project"], seed["id"]) for seed in seeds}
    hits = [
        hit
        for hit in merge_hits(graph, text)
        if (hit["project"], hit["id"]) not in seeded
    ]
    # Text matches are leads, not dependents, so only graph hits reach out.
    reached = [*seeds, *[hit for hit in hits if hit["evidence"] == "graph"]]
    cross_project = cross_project_hits(links_into(link_targets(reached)))
    knowledge = knowledge_for(
        [{"project": one["project"], "node_id": one["id"]} for one in reached]
    )
    return {
        "symbol": symbol,
        "resolved": nodes,
        "impact": bucket_impact(hits, cross_project),
        "knowledge": knowledge,
        "notes": notes,
    }
