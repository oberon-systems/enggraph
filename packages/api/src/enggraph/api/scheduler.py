"""Start the runs the schedule asks for, without anyone asking.

This is the only process that indexes: the trees are mounted here read-only
and the parsers are in this image, so a scheduled run is the same thread a
button press starts, decided by `enggraph.api.schedule` instead of by a request.

Two signals drive it, and both end in `indexjobs.open_run`: the timer of a
`periodic` project, and the watch over the directories in `auto`. An `auto`
project is indexed only when the watch reports a change; a watch that went
quiet is reported and retried, never covered by a full run nobody asked for.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import pathspec
from psycopg2.extensions import connection as Connection
from psycopg2.extensions import cursor as Cursor

from enggraph.api import indexjobs, schedule
from enggraph.core import features, trees
from enggraph.core.config import (
    FEATURE_INDEXING,
    SCHEDULER_STARTS_PER_TICK,
    SCHEDULER_TICK_SECONDS,
)
from enggraph.core.selection import resolve as resolve_selection
from enggraph.core.storage import get_db_connection, list_mountable_projects
from enggraph.indexer.discovery import selects

LOG = logging.getLogger(__name__)

# How long a watch that died is left dead before it is tried again. A watch
# fails for reasons a retry does not fix - a filesystem that reports nothing,
# a limit the container cannot raise - so retrying is worth doing rarely.
WATCH_RETRY_SECONDS = 600


class Watcher:
    """One watch over every tree of a project set to `auto`.

    A single watch for all of them rather than one per project, and each
    change is kept only when the project's ignore spec selects the path.
    """

    def __init__(
        self,
        targets: dict[str, pathspec.PathSpec | None],
        mark: Callable[[str], None],
    ) -> None:
        """Take each watched project's ignore spec and what to call on a change."""
        self.targets = targets
        self._mark = mark
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._run, name="index-watch", daemon=True
        )

    def start(self) -> None:
        """Begin watching, on a thread of its own."""
        self._thread.start()

    def stop(self) -> None:
        """Ask the watch to finish."""
        self._stop.set()

    def alive(self) -> bool:
        """Whether the watch is still running, rather than dead of an error."""
        return self._thread.is_alive()

    def selected(self, project: str, rel_path: str) -> bool:
        """Whether a changed path is one the graph describes."""
        return selects(rel_path, self.targets[project], trees.of(project))

    def _run(self) -> None:
        try:
            for project, rel_path in trees.changes(self.targets, self._stop):
                if self.selected(project, rel_path):
                    self._mark(project)
        except OSError:
            # An exhausted watch limit lands here: a host sysctl this container
            # cannot raise. Those projects are indexed by hand until it returns.
            LOG.exception(
                "Watching %d directories failed; their changes go unnoticed "
                "until the watch is rebuilt",
                len(self.targets),
            )
        except Exception:  # noqa: BLE001 - a dead thread must say why
            LOG.exception("The index watch stopped")


class Scheduler:
    """The tick loop: what is due, what is watched, and what gets started."""

    def __init__(self, tick_seconds: int = SCHEDULER_TICK_SECONDS) -> None:
        """Take how often to look, leaving every other answer to the database."""
        self._tick_seconds = max(1, tick_seconds)
        self._stop = threading.Event()
        self._lock = threading.Lock()
        # The last change the watch reported per project, since its last run.
        self._dirty: dict[str, datetime] = {}
        self._watcher: Watcher | None = None
        self._built: datetime | None = None

    def start(self) -> None:
        """Run the loop on a thread of its own."""
        threading.Thread(target=self.run, name="index-scheduler", daemon=True).start()

    def stop(self) -> None:
        """Ask the loop and its watch to finish."""
        self._stop.set()
        if self._watcher is not None:
            self._watcher.stop()

    def mark(self, project: str) -> None:
        """Record that a watched file of a project changed."""
        with self._lock:
            self._dirty[project] = datetime.now(UTC)

    def run(self) -> None:
        """Clear what a dead process left behind, then tick until stopped.

        Nothing here is fatal. The database can be a moment behind this
        service at start up and can go away later, and a scheduler that ended
        on the first such tick would leave every project unindexed until
        somebody noticed the thread was gone.
        """
        LOG.info(
            "Index scheduler ticking every %d seconds, starting at most %d run(s) "
            "a tick",
            self._tick_seconds,
            SCHEDULER_STARTS_PER_TICK,
        )
        swept = False
        while True:
            try:
                if not swept:
                    self.sweep_orphans()
                    swept = True
                self.tick()
            except Exception:  # noqa: BLE001 - one bad tick must not end the loop
                LOG.exception("Index scheduler tick failed")
            if self._stop.wait(self._tick_seconds):
                return

    def sweep_orphans(self) -> None:
        """Close the runs an earlier process was killed in the middle of."""
        conn = get_db_connection()
        try:
            with conn.cursor() as cursor:
                orphaned = indexjobs.fail_orphaned(cursor, datetime.now(UTC))
            conn.commit()
        finally:
            conn.close()
        for job_id, project in orphaned:
            LOG.warning(
                "Index job %d of %s was left running by an earlier process; "
                "closed as failed",
                job_id,
                project,
            )

    def tick(self) -> None:
        """Resolve every project, rebuild the watch, and start what is owed.

        What was asked for while every slot was taken goes first: somebody is
        waiting on it, and nobody is waiting on the schedule.
        """
        now = datetime.now(UTC)
        indexjobs.start_queued()
        conn = get_db_connection()
        try:
            with conn.cursor() as cursor:
                targets, owed = self._plan(cursor, now)
            conn.commit()
            self._watch(targets)
            for project, root_path, reason in owed[:SCHEDULER_STARTS_PER_TICK]:
                if indexjobs.at_capacity():
                    break
                self._begin(conn, project, root_path, reason)
        finally:
            conn.close()

    def _plan(
        self, cursor: Cursor, now: datetime
    ) -> tuple[dict[str, pathspec.PathSpec | None], list[tuple[str, str, str]]]:
        """Say what should be watched and which projects are owed a run.

        Ordered by how long each has waited, so a tick that may start one run
        starts the most overdue rather than the alphabetically first.
        """
        targets: dict[str, pathspec.PathSpec | None] = {}
        owed: list[tuple[datetime | None, str, str, str]] = []
        for project, root_path in list_mountable_projects(cursor):
            if not trees.of(project).available():
                continue
            # The switch is asked before the schedule: off is off, whatever
            # mode the project set for itself.
            if not features.resolve(cursor, project, FEATURE_INDEXING).enabled:
                continue
            settled = schedule.resolve(cursor, project)
            targets.update(self._targets(cursor, project, settled.watched))
            with self._lock:
                changed_at = self._dirty.get(project)
            last = indexjobs.last_run(cursor, project)
            reason = schedule.due(settled, last, changed_at, now)
            if reason is not None:
                owed.append((last, project, root_path, reason))
        owed.sort(key=lambda one: (one[0] is not None, one[0]))
        return targets, [(project, root, why) for _, project, root, why in owed]

    def _targets(
        self, cursor: Cursor, project: str, watched: bool
    ) -> dict[str, pathspec.PathSpec | None]:
        """Return the ignore spec a watched project's tree is filtered by."""
        if not watched or not trees.of(project).available():
            return {}
        return {project: resolve_selection(cursor, project).ignore}

    def _watch(self, targets: dict[str, pathspec.PathSpec | None]) -> None:
        """Replace the watch when what it should be watching changed, or died.

        Not on every tick: the specs are re-read each time, and rebuilding a
        watch every 30 seconds would drop the events arriving while it is
        being built. A watch that died is a separate case - it is retried, but
        slowly.
        """
        current = {} if self._watcher is None else self._watcher.targets
        dead = self._watcher is not None and not self._watcher.alive()
        if set(current) == set(targets) and not (dead and self._retry_due()):
            return
        if self._watcher is not None:
            self._watcher.stop()
            self._watcher = None
        self._built = datetime.now(UTC)
        if not targets:
            LOG.info("Nothing is set to auto; the index watch is off")
            return
        LOG.info(
            "%s %d director%s for changes",
            "Watching again" if dead else "Watching",
            len(targets),
            "y" if len(targets) == 1 else "ies",
        )
        self._watcher = Watcher(targets, self.mark)
        self._watcher.start()

    def _retry_due(self) -> bool:
        """Whether a dead watch has been dead long enough to try again."""
        if self._built is None:
            return True
        return datetime.now(UTC) - self._built >= timedelta(seconds=WATCH_RETRY_SECONDS)

    def _begin(
        self, conn: Connection, project: str, root_path: str, reason: str
    ) -> None:
        """Open a run and hand it to a thread, as `POST /index` does.

        The mark is cleared once the run is open and before it walks: a file
        written while it goes has not been indexed by it and marks the project
        again. A start that is refused keeps the mark, so it is retried.
        """
        try:
            with conn.cursor() as cursor:
                view = indexjobs.open_run(cursor, project, None, fresh=False)
            conn.commit()
        except RuntimeError as refused:
            conn.rollback()
            LOG.info("Not indexing %s yet: %s", project, refused)
            return
        with self._lock:
            self._dirty.pop(project, None)
        LOG.info("Indexing %s as job %d (%s)", project, view["id"], reason)
        indexjobs.run_in_background(view["id"], project, root_path, None, False)
