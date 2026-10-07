"""Bridge between the graphifyy extractor and the graph stored in PostgreSQL.

graphifyy is used as a library, not as a command. Its CLI only installs a
skill; the pipeline the skill drives is a handful of functions over a networkx
graph, which is what makes it usable from here at all.

Two directions cross this module:

    extraction dict  ->  nodes / edges      (import_extraction)
    nodes / edges  ->  networkx.Graph       (db_to_graph)

The second one is what lets everything graphifyy builds on top of a graph -
its HTML view, its clustering, its own stdio MCP server - read our database
instead of the file it normally expects.
"""

from __future__ import annotations

import hashlib
import json
import logging
import posixpath
import shutil
from pathlib import Path

from graphify import cache as extractor_cache
from graphify import extract as extractor_extract
from psycopg2.extensions import cursor as Cursor

from enggraph.core.config import (
    GRAPHIFY_OUT_DIR,
    MAX_NODE_ID_LENGTH,
    SOURCE_GRAPHIFYY,
)
from enggraph.core.identifiers import truncate
from enggraph.core.storage import (
    ensure_external_node,
    insert_edge,
    upsert_extracted_node,
    upsert_file_node,
)
from enggraph.indexer.summaries import extract_summary

LOG = logging.getLogger(__name__)

# graphifyy names a node after its label alone, so `index` stands for 475
# different files in a tree with a node_modules in it. Only the file and the
# line together tell two of them apart.
_KEY_TEMPLATE = "{path}::{label}@{location}"


def cache_root(project: str) -> Path:
    """Return the extractor cache directory of one project."""
    return Path(GRAPHIFY_OUT_DIR) / "cache" / project


def install_extractor_cache(project: str, fresh: bool = False) -> int:
    """Scope the extractor's own cache to one project. Returns entries cleared.

    Two things are wrong with the cache as shipped, and both have to be fixed
    from here because it has no options.

    It is written next to the common parent of the paths it was handed, which
    for us is inside the project mount - read only by contract, so the write
    fails and takes the extraction with it. That is what `cache_dir` is
    redirected for, and it is what makes a re-index skip unchanged files.

    Worse, an entry is keyed by file content alone, and a hit is returned
    verbatim - carrying the `source_file` of whichever file was extracted
    first. Every tree is mounted at the same path, so two codebases cannot even
    be told apart: one empty `__init__.py` answers for every empty
    `__init__.py` ever indexed, under a path that is not in the tree being
    indexed, and the file it stood in for gets no node at all. The key here is
    the path and the content together, under a directory of the project's own,
    so neither collision can happen.

    This reaches into the extractor rather than going through an option because
    it has none. The version is pinned, and the failure modes if the internals
    move are loud: the write goes back to the read-only mount, and an
    unpatched `extract` module reports a gap for every file at the end of the
    run. The lookups are replaced on `graphify.extract` rather than on
    `graphify.cache`, because that module binds them by value at import time.
    """
    root = cache_root(project)
    root.mkdir(parents=True, exist_ok=True)
    extractor_cache.cache_dir = lambda _root=None: root

    def entry_for(path: Path) -> Path:
        digest = hashlib.sha256(f"{path}\0".encode())
        digest.update(path.read_bytes())
        return root / f"{digest.hexdigest()}.json"

    def load_cached(path: Path, _root: Path | None = None) -> dict | None:
        try:
            entry = entry_for(Path(path))
            return json.loads(entry.read_text())
        except (OSError, json.JSONDecodeError):
            return None

    def save_cached(path: Path, result: dict, _root: Path | None = None) -> None:
        try:
            entry_for(Path(path)).write_text(json.dumps(result))
        except OSError:
            LOG.warning("Failed to cache the extraction of %s", path)

    extractor_extract.load_cached = load_cached
    extractor_extract.save_cached = save_cached

    if not fresh:
        return 0
    cleared = len(list(root.glob("*.json")))
    extractor_cache.clear_cache()
    return cleared


def prune_extractor_caches(known: set[str]) -> tuple[int, int]:
    """Drop cached extractions no project owns. Returns projects, entries.

    A project can leave the database through the dashboard or through the
    `drop_project` tool, and neither can reach this volume: the tool runs in
    another container, and the script talks to postgres only. So the cache is
    collected here instead, against the projects that still exist.

    The loose entries are the ones written before the cache was scoped, when
    one directory held every project at once. Nothing can read them now, and
    they are the entries that answered for the wrong tree.
    """
    root = Path(GRAPHIFY_OUT_DIR) / "cache"
    if not root.is_dir():
        return 0, 0

    entries = 0
    for stale in root.glob("*.json"):
        stale.unlink(missing_ok=True)
        entries += 1

    projects = 0
    for child in sorted(root.iterdir()):
        if not child.is_dir() or child.name in known:
            continue
        entries += len(list(child.glob("*.json")))
        shutil.rmtree(child, ignore_errors=True)
        projects += 1
    return projects, entries


def node_type(label: str, source_file: str | None) -> str:
    """Classify a graphifyy node, which carries no kind of its own.

    The extraction only reports a label and where it was found, so the kind
    has to be read off the label. `foo()` is a function, `.foo()` is a method
    reached through an instance, a capitalised bare name is a class, and a
    label equal to the file name is the file itself.
    """
    if source_file and label == posixpath.basename(source_file):
        return "file"
    if label.startswith(".") and label.endswith("()"):
        return "method"
    if label.endswith("()"):
        return "function"
    if label[:1].isupper():
        return "class"
    return "entity"


def node_id(node: dict[str, str]) -> str:
    """Build an id unique across the tree for one graphifyy node."""
    source_file = node.get("source_file") or ""
    label = node.get("label") or node.get("id", "")
    if not source_file:
        return truncate(label, MAX_NODE_ID_LENGTH)
    if label == posixpath.basename(source_file):
        # File nodes share our own convention so an edge from an Ansible
        # playbook and an edge from a Python import land on the same node.
        return truncate(source_file, MAX_NODE_ID_LENGTH)
    return truncate(
        _KEY_TEMPLATE.format(
            path=source_file,
            label=label,
            location=node.get("source_location", ""),
        ),
        MAX_NODE_ID_LENGTH,
    )


def _resolve(
    their_id: str,
    source_file: str,
    by_file: dict[tuple[str, str], str],
    by_id: dict[str, set[str]],
) -> tuple[str | None, bool]:
    """Map one graphifyy id to ours; the flag says it named several nodes."""
    local = by_file.get((their_id, source_file))
    if local is not None:
        return local, False
    ours = by_id.get(their_id)
    if not ours:
        return None, False
    if len(ours) == 1:
        return next(iter(ours)), False
    return None, True


def link_extraction(
    nodes: list[dict[str, str]], edges: list[dict[str, str]]
) -> tuple[list[tuple[str, str, bool, dict[str, str]]], int, int]:
    """Resolve graphifyy's edges onto our node ids.

    graphifyy builds an id from the file stem, so two `utils.py` declare the
    same ids. An endpoint is looked up in the file that emitted the edge first,
    then by id alone while that id names one node; an edge whose endpoint
    names several is dropped rather than attached to the wrong file.

    Returns `(source, target, external, edge)` for every edge to write, how
    many ids more than one file declared, and how many edges were dropped.
    A target with no node of its own is `external`: something outside the
    tree, which gets the same placeholder our own resolver makes.
    """
    by_file: dict[tuple[str, str], str] = {}
    by_id: dict[str, set[str]] = {}
    for node in nodes:
        their_id = node.get("id", "")
        key = (their_id, node.get("source_file") or "")
        our_id = by_file.setdefault(key, node_id(node))
        by_id.setdefault(their_id, set()).add(our_id)
    reused = sum(1 for ours in by_id.values() if len(ours) > 1)

    links: list[tuple[str, str, bool, dict[str, str]]] = []
    dropped = 0
    for edge in edges:
        source_file = edge.get("source_file") or ""
        source_id, ambiguous = _resolve(
            edge.get("source", ""), source_file, by_file, by_id
        )
        if source_id is None:
            dropped += ambiguous
            continue
        their_target = edge.get("target", "")
        target_id, ambiguous = _resolve(their_target, source_file, by_file, by_id)
        if ambiguous:
            dropped += 1
            continue
        external = target_id is None
        if target_id is None:
            target_id = truncate(their_target, MAX_NODE_ID_LENGTH)
        if target_id == source_id:
            continue
        links.append((source_id, target_id, external, edge))
    return links, reused, dropped


def import_extraction(
    cursor: Cursor,
    project: str,
    extraction: dict[str, list[dict[str, str]]],
    contents: dict[str, str],
) -> tuple[int, int, set[str]]:
    """Store one graphifyy extraction.

    Returns the nodes written, the edges written, and the paths a file node was
    written for - which is what tells a run whose extraction came back short
    from a clean one.

    `contents` maps a project relative path to the head of that file, which is
    what the summary is written from: graphifyy writes no summary of its own,
    and a graph of bare labels sends the agent back to opening files one by
    one. The head, not the whole file - it is what bounds both the memory of
    this pass and the prompt the model is given.
    """
    nodes = extraction.get("nodes", [])
    edges = extraction.get("edges", [])

    entities_by_file: dict[str, list[dict[str, str]]] = {}
    for node in nodes:
        source_file = node.get("source_file")
        label = node.get("label", "")
        if not source_file or label == posixpath.basename(source_file):
            continue
        entities_by_file.setdefault(source_file, []).append(
            {"name": label, "type": node_type(label, source_file)}
        )

    written = 0
    files: set[str] = set()
    for node in nodes:
        their_id = node.get("id", "")
        our_id = node_id(node)
        label = node.get("label", their_id)
        source_file = node.get("source_file")
        kind = node_type(label, source_file)

        if kind == "file" and source_file:
            summary = extract_summary(
                source_file,
                contents.get(source_file, ""),
                entities_by_file.get(source_file, []),
            )
            upsert_file_node(
                cursor,
                project,
                source_file,
                summary,
                source=SOURCE_GRAPHIFYY,
            )
            files.add(source_file)
        else:
            upsert_extracted_node(
                cursor,
                project,
                our_id,
                label,
                kind,
                source_file,
                "",
                {
                    "source": SOURCE_GRAPHIFYY,
                    "graphifyy_id": their_id,
                    "source_location": node.get("source_location", ""),
                },
            )
        written += 1

    links, reused, dropped = link_extraction(nodes, edges)
    if reused or dropped:
        LOG.info(
            "graphifyy reused %d node ids across files, %d edges dropped as ambiguous",
            reused,
            dropped,
        )

    linked = 0
    for source_id, target_id, external, edge in links:
        if external:
            ensure_external_node(cursor, project, target_id, "external_import")
        insert_edge(
            cursor,
            project,
            source_id,
            target_id,
            edge.get("relation", "uses"),
            {
                "source": SOURCE_GRAPHIFYY,
                "confidence": edge.get("confidence", "EXTRACTED"),
                "weight": edge.get("weight", 1.0),
            },
        )
        linked += 1

    return written, linked, files
