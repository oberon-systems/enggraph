"""Drain the embedding queue, slowly, for as long as something answers.

A thread of the worker API, beside the index scheduler, and for the same
reason: the trees are mounted here read-only, so this is the only process that
can read the file a chunk is cut from.

Everything about this loop is deliberately unhurried. It takes a handful of
files a tick, it stops the moment the feature is switched off, and a server
that is not there is not an error - the files go back in the queue with their
attempt returned and the tick says nothing until something changes.
"""

from __future__ import annotations

import logging
import os
import posixpath
import threading
import time
from typing import NamedTuple

from psycopg2.extensions import connection as Connection
from psycopg2.extensions import cursor as Cursor

from enggraph import embedjobs, features
from enggraph.chunks import split
from enggraph.config import (
    EMBED_BATCH,
    EMBED_CHUNK_CHARS,
    EMBED_CHUNK_OVERLAP_LINES,
    EMBED_LEASE_SECONDS,
    EMBED_MAX_ATTEMPTS,
    EMBED_MODEL,
    EMBED_TASKS_PER_TICK,
    EMBED_TICK_BUDGET_SECONDS,
    EMBED_TICK_SECONDS,
    FEATURE_EMBEDDING,
    MAX_NODE_ID_LENGTH,
    SETTINGS_PROJECT,
)
from enggraph.discovery import read_source
from enggraph.embedder import Embedder, EmbedError, EmbedRejected, EmbedTimeout
from enggraph.identifiers import project_mount, truncate
from enggraph.storage import (
    get_db_connection,
    list_mountable_projects,
    replace_file_embeddings,
)
from enggraph.summarizeloop import slow

LOG = logging.getLogger(__name__)

# How long between two sweeps for work nobody enqueued. An index run enqueues
# what it changed, so this only catches the other case: a project switched on
# long after it was last indexed. Counted in seconds rather than in ticks,
# because a tick is now as long as there is work to do.
SWEEP_EVERY_SECONDS = 300


class Target(NamedTuple):
    """Where one project's chunks go, how many at a time, and how they are cut."""

    url: str
    key: str
    batch: int
    chunk_chars: int
    chunk_overlap: int


class EmbedLoop:
    """The tick loop: which projects are on, and which files they owe."""

    def __init__(
        self,
        tick_seconds: int = EMBED_TICK_SECONDS,
        model: str = EMBED_MODEL,
    ) -> None:
        """Take how often to look. Every other answer is in the database."""
        self._tick_seconds = max(1, tick_seconds)
        self._model = model
        self._stop = threading.Event()
        self._budget_seconds = EMBED_TICK_BUDGET_SECONDS
        self._swept = 0.0
        self._quiet = False
        self._known: set[str] = set()
        self._embedders: dict[tuple[str, str], Embedder] = {}

    def start(self) -> None:
        """Run the loop on a thread of its own."""
        threading.Thread(target=self.run, name="embed-loop", daemon=True).start()

    def stop(self) -> None:
        """Ask the loop to finish."""
        self._stop.set()

    def run(self) -> None:
        """Tick until stopped. Nothing here is fatal, as in the scheduler."""
        LOG.info(
            "Embedding queue: %d file(s) a claim, up to %ds of work a tick, "
            "polling every %ds when idle",
            EMBED_TASKS_PER_TICK,
            EMBED_TICK_BUDGET_SECONDS,
            self._tick_seconds,
        )
        while True:
            busy = False
            try:
                busy = self.tick()
            except Exception:  # noqa: BLE001 - one bad tick must not end the loop
                LOG.exception("Embedding tick failed")
            if self._stop.wait(0 if busy else self._tick_seconds):
                return

    def tick(self) -> bool:
        """Enqueue what is owed, then embed as much of it as the tick allows."""
        conn = get_db_connection()
        try:
            with conn.cursor() as cursor:
                enabled = self._enabled(cursor)
            conn.commit()
            switched_on = set(enabled) - self._known
            self._known = set(enabled)
            if not enabled:
                return False
            # The loop's own pace comes from the global level: a poll
            # interval and a work budget belong to the process rather than to
            # whichever project it is draining.
            with conn.cursor() as cursor:
                pace = features.resolve(cursor, SETTINGS_PROJECT, FEATURE_EMBEDDING)
            self._tick_seconds = max(1, pace.tick_seconds)
            self._budget_seconds = max(5, pace.budget_seconds)

            now = time.monotonic()
            if now - self._swept >= SWEEP_EVERY_SECONDS:
                self._swept = now
                self._sweep(conn, enabled)
            elif switched_on:
                self._sweep(conn, {name: enabled[name] for name in switched_on})
            return self._drain(conn, enabled)
        finally:
            conn.close()

    def _enabled(self, cursor: Cursor) -> dict[str, Target]:
        """Return the projects the queue may work on, each with where to ask.

        A project with no mount is left out rather than failed: the tree is
        not here, and every file of it would be claimed only to be released.
        """
        enabled: dict[str, Target] = {}
        for project, _ in list_mountable_projects(cursor):
            if not os.path.isdir(project_mount(project)):
                continue
            settled = features.resolve(cursor, project, FEATURE_EMBEDDING)
            if settled.enabled:
                enabled[project] = Target(
                    settled.server_url,
                    settled.server_key,
                    settled.batch,
                    settled.chunk_chars,
                    settled.chunk_overlap,
                )
        return enabled

    def _sweep(self, conn: Connection, enabled: dict[str, Target]) -> None:
        """Enqueue the files of every enabled project that has no vectors."""
        for project, target in sorted(enabled.items()):
            try:
                with conn.cursor() as cursor:
                    written = embedjobs.enqueue_project(
                        cursor, project, self._model, target.chunk_chars
                    )
                conn.commit()
            except Exception:  # noqa: BLE001 - one project must not stop the rest
                conn.rollback()
                LOG.exception("Could not queue %s for embedding", project)
                continue
            if written:
                LOG.info("Queued %d file(s) of %s for embedding", written, project)

    def _drain(self, conn: Connection, enabled: dict[str, Target]) -> bool:
        """Embed for as long as there is work, or until the tick is spent.

        Claim, embed, claim again: a fixed handful per tick and then a sleep
        made a server that answers in milliseconds sit idle between batches,
        and a large project would have taken days at twelve files a minute.
        The budget is what keeps the switches from going unread while a big
        queue drains.
        """
        deadline = time.monotonic() + self._budget_seconds
        live = dict(enabled)
        while live and time.monotonic() < deadline and not self._stop.is_set():
            # Claimed a project at a time, because the batch is a per-project
            # setting: that is how one project is given more of the machine
            # than another.
            for project, target in sorted(live.items()):
                embedder = self.embedder_for(target.url, target.key)
                if not embedder.available():
                    del live[project]
                    continue
                with slow(f"claiming {project}"), conn.cursor() as cursor:
                    tasks = embedjobs.claim(
                        cursor, [project], target.batch, EMBED_LEASE_SECONDS
                    )
                conn.commit()
                if not tasks:
                    del live[project]
                    continue
                for index, task in enumerate(tasks):
                    try:
                        with slow(f"embedding {task['file_path']} of {project}"):
                            done = self.embed_file(conn, embedder, task, target)
                    except Exception as error:  # noqa: BLE001 - one file, not all
                        LOG.exception("Embedding %s failed", task["file_path"])
                        conn.rollback()
                        self._settle(
                            conn, task, error=f"{type(error).__name__}: {error}"
                        )
                        done = True
                    if not done:
                        # This server stopped answering: the rest of the claim
                        # goes back now, and other servers' projects carry on.
                        with conn.cursor() as cursor:
                            for rest in tasks[index + 1 :]:
                                embedjobs.release(cursor, rest["id"])
                        conn.commit()
                        del live[project]
                        break
        return bool(live) and not self._stop.is_set()

    def embedder_for(self, url: str, key: str) -> Embedder:
        """Return the client for one server, the same one every tick."""
        if (url, key) not in self._embedders:
            self._embedders[(url, key)] = Embedder(stored_url=url, stored_key=key)
        return self._embedders[(url, key)]

    def embed_file(
        self,
        conn: Connection,
        embedder: Embedder,
        task: dict,
        target: Target | None = None,
    ) -> bool:
        """Embed one file. False means no server answered and the drain stops."""
        cut = target or Target("", "", 1, EMBED_CHUNK_CHARS, EMBED_CHUNK_OVERLAP_LINES)
        project = task["project"]
        rel_path = task["file_path"]
        node_id = truncate(rel_path, MAX_NODE_ID_LENGTH)
        full_path = posixpath.join(project_mount(project), rel_path)
        text, reason = read_source(full_path, rel_path)
        if text is None:
            self._settle(conn, task, error=f"{rel_path} is {reason}")
            return True

        pieces = split(text, cut.chunk_chars, cut.chunk_overlap)
        if not pieces:
            # An empty file is done, not failed: there is nothing to embed and
            # nothing about it will change until it is written to.
            with conn.cursor() as cursor:
                replace_file_embeddings(
                    cursor,
                    project,
                    node_id,
                    task["content_hash"],
                    self._model,
                    [],
                    cut.chunk_chars,
                )
                embedjobs.finish(cursor, task["id"])
            conn.commit()
            return True

        try:
            vectors = self._vectors(embedder, [piece.text for piece in pieces])
        except (EmbedTimeout, EmbedRejected) as slow:
            # One chunk that cannot be embedded inside the timeout is this
            # file's own problem rather than the configuration's: it counts an
            # attempt and is given up on rather than retried for ever.
            self._settle(conn, task, error=str(slow))
            return True
        except EmbedError as refused:
            # Nobody's fault but the configuration's. The file keeps its place
            # in the queue and its attempts, and the reason is said once.
            with conn.cursor() as cursor:
                embedjobs.release(cursor, task["id"])
            conn.commit()
            if not self._quiet:
                LOG.info("%s", refused)
                self._quiet = True
            return False
        self._quiet = False

        rows = [
            (piece.index, piece.start_line, piece.end_line, piece.text, vector)
            for piece, vector in zip(pieces, vectors, strict=True)
        ]
        with conn.cursor() as cursor:
            written = replace_file_embeddings(
                cursor,
                project,
                node_id,
                task["content_hash"],
                self._model,
                rows,
                cut.chunk_chars,
            )
            embedjobs.finish(cursor, task["id"])
        conn.commit()
        LOG.debug("Embedded %s of %s in %d chunk(s)", rel_path, project, written)
        return True

    def _vectors(self, embedder: Embedder, texts: list[str]) -> list[list[float]]:
        """Embed every chunk of a file, a batch at a time."""
        vectors: list[list[float]] = []
        for start in range(0, len(texts), EMBED_BATCH):
            vectors.extend(embedder.embed(texts[start : start + EMBED_BATCH]))
        return vectors

    def _settle(self, conn: Connection, task: dict, error: str) -> None:
        """Record a file this loop cannot embed, and stop retrying it."""
        with conn.cursor() as cursor:
            embedjobs.fail(cursor, task["id"], error, EMBED_MAX_ATTEMPTS)
        conn.commit()
        LOG.info("Not embedding %s: %s", task["file_path"], error)
