"""Retrieve, expand, deduplicate and budget one question into a packet."""

from __future__ import annotations

import re
from typing import Any

from enggraph.core import db, jsjson
from enggraph.mcp.knowledge import knowledge_for
from enggraph.mcp.rerank import identifiers, rerank
from enggraph.mcp.search import hybrid_search, semantic_note
from enggraph.mcp.worker import read_ranges

DEFAULT_TOKEN_BUDGET = 12000
MAX_TOKEN_BUDGET = 60000
MIN_TOKEN_BUDGET = 200
DEFAULT_SEEDS = 8
MAX_SEEDS = 25
MAX_HOPS = 2
# What one seed may pull in per round. A hub node has hundreds of edges and
# none of them are worth the whole budget.
NEIGHBOURS_PER_SEED = 40
# And what one seed may keep per tier: the packet wants a map of a module,
# not its whole index.
PER_SEED_CAP = {
    "caller": 8,
    "test": 4,
    "defines": 8,
    "callee": 8,
    "import": 3,
    "container": 2,
    "ancestor": 16,
}
# Importers share the caller tier but not its budget, and decay like imports.
IMPORTER_CAP = PER_SEED_CAP["import"]
MAX_ANCESTOR_DEPTH = 16
# Below this budget source text cannot fit beside a map, so `auto` picks one.
SUMMARY_BUDGET = 3000
# A summary packet spends source text on this many of its best seeds only.
SUMMARY_CHUNK_SEEDS = 2
# An estimate on purpose: the server holds no tokenizer, and a token is
# roughly four characters of text.
CHARS_PER_TOKEN = 4

DEFAULT_EXPAND = {"caller": 1, "callee": 1, "test": 1, "import": 1, "defines": 1}

# The order tiers are spent in: what depends on this explains it better than
# what it depends on, and a test explains it better than an import.
TIER_ORDER = ["caller", "test", "defines", "callee", "import", "container", "ancestor"]

# A broad question is answered by the map first: where a hit sits, then what
# sits beside it, and only then what calls it.
SUMMARY_TIER_ORDER = [
    "ancestor",
    "defines",
    "container",
    "caller",
    "test",
    "callee",
    "import",
]

# The vocabulary is open, so this set names the dependency edges that are
# known and everything else is placed by its direction alone.
IMPORT_RELATIONS = {
    "imports",
    "imports_from",
    "depends_on",
    "includes",
    "extends",
    "requires",
}

TEST_PATH = re.compile(
    r"(^|/)(tests?|spec|specs)/|(^|/)test_[^/]*\Z|[._-](test|spec)\.[^/]+\Z"
    r"|_test\.[^/]+\Z",
    re.IGNORECASE,
)
NAMES_CODE = re.compile(r"[\w-]+/[\w.-]+|\w\(\)", re.ASCII)
LINE_IN_ID = re.compile(r"@L([0-9]+)\Z")

Entry = dict[str, Any]
Candidate = dict[str, Any]


def estimate_tokens(text: str) -> int:
    """Estimate what a text costs, counting characters as JavaScript does."""
    units = len(text.encode("utf-16-le")) // 2
    return -(-units // CHARS_PER_TOKEN)


def is_test_path(path: str | None) -> bool:
    """Say whether a path is a test."""
    return path is not None and bool(TEST_PATH.search(path))


def classify(relation_type: str, direction: str, file_path: str | None) -> str:
    """Return which tier an edge puts a neighbour in.

    A search hit is usually a file, so what the file defines is the substance
    of the hit, and being imported is this graph's nearest thing to a caller.
    """
    if relation_type == "contains":
        return "container" if direction == "incoming" else "defines"
    if is_test_path(file_path):
        return "test"
    if relation_type in IMPORT_RELATIONS:
        return "caller" if direction == "incoming" else "import"
    return "caller" if direction == "incoming" else "callee"


def is_importer(relation_type: str, direction: str) -> bool:
    """Say whether an edge reached a caller only by importing the hit's file."""
    return direction == "incoming" and relation_type in IMPORT_RELATIONS


def cap_for(tier: str, seed_order: int, importer: bool = False) -> int:
    """Return what a hit at rank `seed_order` may keep in a tier.

    The bulk tiers decay with the rank: a map of the best hit's file is
    context, the same map of the eighth hit is a table of contents.
    """
    if importer:
        return max(IMPORTER_CAP - seed_order, 0)
    if tier == "defines":
        return max(PER_SEED_CAP[tier] - seed_order * 2, 0)
    if tier == "import":
        return max(PER_SEED_CAP[tier] - seed_order, 0)
    return PER_SEED_CAP[tier]


def hops_for(tier: str, expand: dict[str, int]) -> int:
    """Return how many hops a tier follows."""
    return 1 if tier in ("container", "ancestor") else expand[tier]


def resolve_detail(detail: str, query: str, token_budget: int) -> str:
    """Settle `auto`: a question naming code gets source, one in words a map."""
    if detail != "auto":
        return detail
    if token_budget < SUMMARY_BUDGET:
        return "summary"
    named = any(identifiers(query).values()) or bool(NAMES_CODE.search(query))
    return "source" if named else "summary"


def line_from_id(node_id: str) -> int | None:
    """Return the line a symbol node carries in its id (`path::Name@L70`)."""
    found = LINE_IN_ID.search(node_id)
    return None if found is None else int(found.group(1))


def shorten(node_id: str) -> str:
    """Name a seed by the tail of its id: the entry beside it has the path."""
    cut = node_id.find("::")
    path = node_id if cut == -1 else node_id[:cut]
    slash = path.rfind("/")
    return (path if slash == -1 else path[slash + 1 :]) + node_id[len(path) :]


def why(tier: str, relation_type: str, seed_name: str) -> str:
    """Say why an expanded entry is in the packet."""
    what = {
        "ancestor": "holds",
        "container": "contained in",
        "defines": "defined in",
        "test": "test near",
        "caller": f"{relation_type} into",
    }.get(tier, f"{relation_type} from")
    return f"{what} {seed_name}"


ANCESTORS = """WITH RECURSIVE up AS (
       SELECT s.project, s.id AS seed_id, s.id AS node_id, 0 AS depth
         FROM UNNEST($1::text[], $2::text[]) AS s(project, id)
       UNION
       SELECT u.project, u.seed_id, e.source_id, u.depth + 1
         FROM up AS u
         JOIN edges AS e
           ON e.project = u.project AND e.target_id = u.node_id
          AND e.relation_type = 'contains'
        WHERE u.depth < $3
     )
     SELECT u.project, p.type AS project_type, u.seed_id, u.node_id,
            u.depth, n.name, n.type, n.file_path, n.summary
       FROM up AS u
       JOIN nodes AS n ON n.project = u.project AND n.id = u.node_id
       JOIN projects AS p ON p.name = u.project
      WHERE u.depth > 0 AND NOT starts_with(n.type, 'external_')
      ORDER BY u.project, u.seed_id, u.depth, u.node_id"""

# The project is carried through the CTE rather than joined on a scope: two
# members of an organization hold the same node id.
NEIGHBOURS = """WITH seed AS (
       SELECT * FROM UNNEST($1::text[], $2::text[]) AS s(project, id)
     ),
     links AS (
       SELECT e.project, e.source_id AS seed_id, e.target_id AS node_id,
              e.relation_type, 'outgoing' AS direction
         FROM edges AS e
         JOIN seed AS s ON s.project = e.project AND s.id = e.source_id
       UNION
       SELECT e.project, e.target_id AS seed_id, e.source_id AS node_id,
              e.relation_type, 'incoming' AS direction
         FROM edges AS e
         JOIN seed AS s ON s.project = e.project AND s.id = e.target_id
     ),
     joined AS (
       SELECT l.project, l.seed_id, l.node_id, l.relation_type, l.direction,
              n.name, n.type, n.file_path, n.summary
         FROM links AS l
         JOIN nodes AS n
           ON n.project = l.project AND n.id = l.node_id
        WHERE NOT starts_with(n.type, 'external_')
     ),
     capped AS (
       SELECT j.*, ROW_NUMBER() OVER (
                PARTITION BY j.project, j.seed_id
                -- A described node first: a summary is what makes an entry
                -- worth its tokens, and a hub file has more children than
                -- any packet can hold.
                ORDER BY (j.summary IS NULL), j.relation_type, j.node_id
              ) AS rn
         FROM joined AS j
     )
     SELECT c.project, p.type AS project_type, c.seed_id, c.node_id,
            c.relation_type, c.direction, c.name, c.type, c.file_path,
            c.summary, chunk.start_line, chunk.end_line,
            NULL::text AS chunk
       FROM capped AS c
       JOIN projects AS p ON p.name = c.project
       LEFT JOIN LATERAL (
         SELECT e.start_line, e.end_line
           FROM chunks AS e
          WHERE e.project = c.project AND e.node_id = c.node_id
            AND e.kind = 'source'
          ORDER BY e.chunk_index
          LIMIT 1
       ) AS chunk ON TRUE
      WHERE c.rn <= $3
      -- rn, not the id: the caps downstream count in this order.
      ORDER BY c.project, c.seed_id, c.rn"""


def fetch_ancestors(frontier: list[dict[str, str]]) -> list[db.Row]:
    """Climb `contains` from each seed: its file, then every directory above."""
    if not frontier:
        return []
    return db.query(
        ANCESTORS,
        [
            [seed["project"] for seed in frontier],
            [seed["id"] for seed in frontier],
            MAX_ANCESTOR_DEPTH,
        ],
    )


def with_chunks(rows: list[db.Row]) -> list[db.Row]:
    """Read each neighbour's first chunk from the mounted tree."""
    wanted = [
        {
            "project": row["project"],
            "path": row["file_path"],
            "start": row["start_line"],
            "end": row["end_line"],
        }
        if row["file_path"] is not None
        and row["start_line"] is not None
        and row["end_line"] is not None
        else None
        for row in rows
    ]
    texts = iter(read_ranges([one for one in wanted if one is not None]))
    return [
        row if one is None else {**row, "chunk": next(texts)}
        for row, one in zip(rows, wanted, strict=True)
    ]


def fetch_neighbours(frontier: list[dict[str, str]]) -> list[db.Row]:
    """Return what the frontier's edges reach, a described node first."""
    if not frontier:
        return []
    return db.query(
        NEIGHBOURS,
        [
            [seed["project"] for seed in frontier],
            [seed["id"] for seed in frontier],
            NEIGHBOURS_PER_SEED,
        ],
    )


def seed_candidate(row: db.Row, score: float, seed_order: int) -> Candidate:
    """Turn one ranked search row into a candidate for the packet."""
    if row["lexical_rank"] is not None and row["vector_rank"] is not None:
        reason = "lexical and semantic hit"
    elif row["vector_rank"] is not None:
        reason = "semantic hit"
    else:
        reason = "lexical hit"
    return {
        "key": (row["project"], row["id"]),
        "tier": None,
        "chunk": row["snippet"],
        "seedOrder": seed_order,
        "place": 0,
        "entry": {
            "project": row["project"],
            "project_type": row["project_type"],
            "id": row["id"],
            "name": row["name"],
            "type": row["type"],
            "file_path": row["file_path"],
            "start_line": row["start_line"],
            "end_line": row["end_line"],
            "origin": "search",
            "why": reason,
            "score": jsjson.fixed(score, 5),
            "summary": row["summary"],
        },
    }


def overlaps(kept: list[Entry], entry: Entry) -> bool:
    """Say whether an entry's lines are already covered by a kept entry."""
    start = entry["start_line"]
    end = entry["end_line"]
    if entry["file_path"] is None or start is None or end is None:
        return False
    for other in kept:
        if (
            other.get("project") != entry.get("project")
            or other["file_path"] != entry["file_path"]
            or other["start_line"] is None
            or other["end_line"] is None
        ):
            continue
        if other["start_line"] <= end and start <= other["end_line"]:
            return True
    return False


def cost(entry: Entry) -> int:
    """Return what an entry costs in the packet."""
    return estimate_tokens(jsjson.dumps(entry))


def assemble(
    seeds: list[Candidate],
    expanded: list[Candidate],
    budget: int,
    include_chunks: bool,
    tier_order: list[str] | None = None,
    upgrade_chunks: bool = True,
) -> dict[str, Any]:
    """Fill the budget: the search hits first, then the tiers in order.

    An expanded node brings its summary; its chunk is an upgrade spent only
    once everything that fits has been placed.
    """
    entries: list[Entry] = []
    upgradable: list[Candidate] = []
    used = 0
    truncated = False

    for candidate in seeds:
        entry = dict(candidate["entry"])
        if include_chunks and candidate["chunk"] is not None:
            entry["chunk"] = candidate["chunk"]
        price = cost(entry)
        if used + price > budget:
            # The reference alone is worth keeping when the text is not.
            bare = dict(candidate["entry"])
            truncated = True
            if used + cost(bare) > budget:
                continue
            used += cost(bare)
            entries.append(bare)
            continue
        used += price
        entries.append(entry)

    for tier in tier_order or TIER_ORDER:
        in_tier = sorted(
            (candidate for candidate in expanded if candidate["tier"] == tier),
            key=lambda one: (one["place"], one["seedOrder"], one["entry"]["id"]),
        )
        for candidate in in_tier:
            if overlaps(entries, candidate["entry"]):
                continue
            price = cost(candidate["entry"])
            if used + price > budget:
                truncated = True
                continue
            used += price
            entries.append(candidate["entry"])
            if include_chunks and upgrade_chunks and candidate["chunk"] is not None:
                upgradable.append(candidate)

    for candidate in upgradable:
        entry = next(
            (
                kept
                for kept in entries
                if kept["id"] == candidate["entry"]["id"]
                and kept.get("project") == candidate["entry"].get("project")
            ),
            None,
        )
        if entry is None or candidate["chunk"] is None:
            continue
        price = estimate_tokens(candidate["chunk"])
        if used + price > budget:
            truncated = True
            break
        used += price
        entry["chunk"] = candidate["chunk"]

    return {"entries": entries, "used": used, "truncated": truncated}


def build_context(
    query: str,
    named: str | None,
    kind: str | None,
    seeds: int,
    token_budget: int,
    expand: dict[str, int],
    include_chunks: bool,
    rerank_enabled: bool,
    detail: str = "auto",
) -> dict[str, Any]:
    """Retrieve, expand, deduplicate and budget one question into a packet.

    Deterministic end to end: the reranked order decides the seeds, the tier
    order decides the expansion, and nothing is sampled anywhere.
    """
    detail = resolve_detail(detail, query, token_budget)
    found = hybrid_search(named, kind, query, seeds, detail == "summary")
    ranked = rerank(found["rows"], query, seeds, rerank_enabled)
    seed_list: list[Candidate] = []
    for index, item in enumerate(ranked):
        seed = seed_candidate(item["row"], item["score"], index)
        # The summary is in the entry already, and a map keeps source for its best.
        if item["row"].get("matched") == "summary" or (
            detail == "summary" and index >= SUMMARY_CHUNK_SEEDS
        ):
            seed["chunk"] = None
        seed_list.append(seed)

    taken = {seed["key"] for seed in seed_list}
    expanded: list[Candidate] = []
    relationships: list[dict[str, Any]] = []
    # Which seed a node was reached from, and how much that seed has already
    # spent on the tier: what keeps one hub file from filling the packet.
    order = {seed["key"]: at for at, seed in enumerate(seed_list)}
    tier_spend: dict[tuple[str, str, str], int] = {}
    frontier = [
        {"project": item["row"]["project"], "id": item["row"]["id"]} for item in ranked
    ]
    reached: dict[tuple[str, str], str] = {}

    hop = 1
    while hop <= MAX_HOPS and frontier:
        rows = fetch_neighbours(frontier)
        if include_chunks:
            rows = with_chunks(rows)
        following: list[dict[str, str]] = []
        for row in rows:
            tier = classify(row["relation_type"], row["direction"], row["file_path"])
            # A source packet leaves directories out; a summary one takes them
            # through the ancestor tier, which it spends first.
            if tier == "container" and row["type"] == "directory":
                continue
            seed_key = (row["project"], row["seed_id"])
            # A second hop stays in the tier that reached it: the caller of a
            # caller is still what calls this code, an import of a caller is not.
            if hop > 1 and reached.get(seed_key) != tier:
                continue
            if hop > hops_for(tier, expand):
                continue
            importer = tier == "caller" and is_importer(
                row["relation_type"], row["direction"]
            )
            group = (*seed_key, "importer" if importer else tier)
            place = tier_spend.get(group, 0)
            seed_order = order.get(seed_key, len(seed_list))
            if place >= cap_for(tier, seed_order, importer):
                continue
            relationships.append(
                {
                    "project": row["project"],
                    "from": row["seed_id"],
                    "relation": row["relation_type"],
                    "to": row["node_id"],
                    "direction": row["direction"],
                }
            )
            key = (row["project"], row["node_id"])
            if key in taken:
                continue
            taken.add(key)
            reached[key] = tier
            tier_spend[group] = place + 1
            order[key] = seed_order
            start_line = row["start_line"]
            if start_line is None:
                start_line = line_from_id(row["node_id"])
            reason = why(tier, row["relation_type"], shorten(row["seed_id"]))
            expanded.append(
                {
                    "key": key,
                    "tier": tier,
                    "chunk": row["chunk"],
                    "seedOrder": seed_order,
                    "place": place,
                    "entry": {
                        "project": row["project"],
                        "project_type": row["project_type"],
                        "id": row["node_id"],
                        "name": row["name"],
                        "type": row["type"],
                        "file_path": row["file_path"],
                        "start_line": start_line,
                        "end_line": row["end_line"],
                        "origin": "expansion",
                        "why": ("indirect " if hop > 1 else "") + reason,
                        "summary": row["summary"],
                    },
                }
            )
            if hop < hops_for(tier, expand):
                following.append({"project": row["project"], "id": row["node_id"]})
        frontier = following
        hop += 1

    if detail == "summary":
        ancestors = fetch_ancestors(
            [
                {"project": item["row"]["project"], "id": item["row"]["id"]}
                for item in ranked
            ]
        )
        for row in ancestors:
            key = (row["project"], row["node_id"])
            if key in taken:
                continue
            taken.add(key)
            expanded.append(
                {
                    "key": key,
                    "tier": "ancestor",
                    "chunk": None,
                    "seedOrder": order.get((row["project"], row["seed_id"]), 0),
                    "place": row["depth"],
                    "entry": {
                        "project": row["project"],
                        "project_type": row["project_type"],
                        "id": row["node_id"],
                        "name": row["name"],
                        "type": row["type"],
                        "file_path": row["file_path"],
                        "start_line": line_from_id(row["node_id"]),
                        "end_line": None,
                        "origin": "expansion",
                        "why": why("ancestor", "contains", shorten(row["seed_id"])),
                        "summary": row["summary"],
                    },
                }
            )

    # A slice of the budget is held back for the links: a packet of entries
    # that never says how they connect is half an answer.
    reserve = token_budget // 8
    packed = assemble(
        seed_list,
        expanded,
        max(token_budget - reserve, 0),
        include_chunks,
        SUMMARY_TIER_ORDER if detail == "summary" else TIER_ORDER,
        detail == "source",
    )
    entries: list[Entry] = packed["entries"]

    # What was written about the hits is spent first from the held-back slice:
    # a decision about the code outranks how two of its entries connect.
    spent: int = packed["used"]
    links_cut = False
    knowledge: list[dict[str, Any]] = []
    hits = [
        {"project": entry["project"], "node_id": entry["id"]}
        for entry in entries
        if entry["origin"] == "search" and entry.get("project")
    ]
    for record in knowledge_for(hits):
        item = {
            "record": record["record_id"],
            "type": record["record_type"],
            "title": record["title"],
            "summary": record["summary"],
            "status": record["status"],
            "project": record["project"],
            "attached_to": record["node_id"],
            "for": record["via"],
        }
        price = estimate_tokens(jsjson.dumps(item))
        if spent + price > token_budget:
            links_cut = True
            break
        spent += price
        knowledge.append(item)

    # A relation to something the budget left out explains nothing, so the
    # links are cut to the entries that survived, and then to what is left.
    kept = {(entry.get("project") or "", entry["id"]) for entry in entries}
    links: list[dict[str, Any]] = []
    by_weight = [
        *[link for link in relationships if link["relation"] != "contains"],
        *[link for link in relationships if link["relation"] == "contains"],
    ]
    for link in by_weight:
        scope = link.get("project") or ""
        if (scope, link["from"]) not in kept or (scope, link["to"]) not in kept:
            continue
        price = estimate_tokens(jsjson.dumps(link))
        if spent + price > token_budget:
            links_cut = True
            break
        spent += price
        links.append(link)

    # The project columns are noise when every row carries the same two values.
    projects = sorted(
        name
        for name in dict.fromkeys(entry.get("project") or "" for entry in entries)
        if name != ""
    )
    if named is not None and len(projects) <= 1:
        for entry in entries:
            entry.pop("project", None)
            entry.pop("project_type", None)
        for link in links:
            link.pop("project", None)

    notes: list[str] = []
    if detail == "summary":
        notes.append(
            "Summary packet: the entries are where the hits sit and what they "
            "hold, with source for the best hits only. Ask with "
            'detail: "source" for the code, or drill down with get_overview.'
        )
    semantic = semantic_note(found)
    if semantic is not None:
        notes.append(semantic)
    if not entries:
        notes.append(
            "Nothing matched: no node of this scope answered the query. Ask it in "
            'other words, widen the scope with project: "*", or check that '
            "the project has been indexed."
        )
    elif (
        include_chunks
        and detail == "source"
        and all("chunk" not in entry for entry in entries)
    ):
        notes.append(
            "No source text in this packet: either the projects it reached hold "
            "no chunks yet - switch embedding on for them in the dashboard "
            "settings - or the worker API that reads the mounted trees did not "
            "answer, so the entries are references and summaries."
        )
    truncated = bool(packed["truncated"]) or links_cut
    if truncated:
        notes.append(
            "Budget spent: entries or relations were left out. Raise "
            "token_budget, narrow the query, or switch a tier off through expand."
        )

    return {
        "query": query,
        "detail": detail,
        "projects": projects,
        "budget": {"limit": token_budget, "used": spent, "truncated": truncated},
        "entries": entries,
        "knowledge": knowledge,
        "relationships": links,
        "notes": notes,
    }
