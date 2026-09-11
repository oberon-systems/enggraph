"""Push the summary queue at a llama.cpp server, for as long as one answers.

The queue has been drained two ways: `make summarize` loads the weights in the
graphify container and works through it, and a worker on a machine with a GPU
claims leases from the API over HTTP. Both need somebody to start them.

This is the third way and the only unattended one. It is the same queue, the
same leases and the same gates - nothing here writes a summary the pull path
would not have written - with the API taking the worker's side of the
conversation itself, against the address stored on the settings page.

It does nothing at all unless a server URL is set. That is deliberate: a
summary costs seconds of somebody's GPU, and a queue that starts pushing at an
address nobody named would be spending it uninvited.
"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from psycopg2.extensions import connection as Connection
from psycopg2.extensions import cursor as Cursor

from enggraph import features, jobs
from enggraph.config import (
    FEATURE_DEFAULTS,
    FEATURE_SUMMARIZE,
    LLM_INPUT_CHARS,
    LLM_MAX_TOKENS,
    SETTINGS_PROJECT,
    SUMMARIZE_BATCH,
    SUMMARIZE_TICK_SECONDS,
    WORKER_LEASE_SECONDS,
    WORKER_MAX_ATTEMPTS,
)
from enggraph.llamachat import Chat, ChatError, ChatRejected
from enggraph.storage import (
    get_cached_summary,
    get_db_connection,
    list_mountable_projects,
    put_cached_summary,
)
from enggraph.summary_text import SYSTEM_PROMPT, content_key, shape, strip_preamble

LOG = logging.getLogger(__name__)

# A step slower than this is logged with what it was, so a queue that stands
# still says where rather than going quiet.
SLOW_SECONDS = 10.0

# Which worker the leases are held by. It is recorded on every task, so a
# reader of the queue can tell the API's own batches from a remote worker's.
WORKER_ID = "worker-api"


@contextmanager
def slow(what: str) -> Iterator[None]:
    """Log `what` when it takes longer than SLOW_SECONDS."""
    started = time.monotonic()
    try:
        yield
    finally:
        spent = time.monotonic() - started
        if spent >= SLOW_SECONDS:
            LOG.info("Slow: %s took %.1fs", what, spent)


class SummarizeLoop:
    """The tick loop: which projects have a server, and what it is handed."""

    def __init__(self, tick_seconds: int = SUMMARIZE_TICK_SECONDS) -> None:
        """Take how often to look. Every other answer is in the database."""
        self._tick_seconds = max(1, tick_seconds)
        self._budget_seconds = int(
            FEATURE_DEFAULTS[FEATURE_SUMMARIZE][features.BUDGET_SECONDS]
        )
        self._stop = threading.Event()
        # Kept across ticks, so a dead server's probe window outlives the tick.
        self._chats: dict[tuple[str, str], Chat] = {}
        self._opened: set[str] = set()

    def start(self) -> None:
        """Run the loop on a thread of its own."""
        threading.Thread(target=self.run, name="summarize-loop", daemon=True).start()

    def stop(self) -> None:
        """Ask the loop to finish."""
        self._stop.set()

    def run(self) -> None:
        """Tick until stopped. Nothing here is fatal, as in the scheduler."""
        LOG.info(
            "Summary push ticking every %d seconds, %d file(s) a batch",
            self._tick_seconds,
            SUMMARIZE_BATCH,
        )
        while True:
            busy = False
            try:
                busy = self.tick()
            except Exception:  # noqa: BLE001 - one bad tick must not end the loop
                LOG.exception("Summary push tick failed")
            if self._stop.wait(0 if busy else self._tick_seconds):
                return

    def tick(self) -> bool:
        """Describe until the work or the tick is spent. True means work is left."""
        conn = get_db_connection()
        try:
            with conn.cursor() as cursor:
                targets = self.targets(cursor)
                # The loop's own pace is the global level's; the batch is each
                # project's own.
                pace = features.resolve(cursor, SETTINGS_PROJECT, FEATURE_SUMMARIZE)
            conn.commit()
            self._tick_seconds = max(1, pace.tick_seconds)
            self._budget_seconds = max(5, pace.budget_seconds)
            return self.drain(conn, targets)
        finally:
            conn.close()

    def drain(self, conn: Connection, targets: dict[str, tuple[str, str, int]]) -> bool:
        """Push one batch per project per round, until the budget or the work ends.

        A project leaves the drain when its server is down or it has nothing
        left, so neither case costs the projects still being described.
        """
        deadline = time.monotonic() + self._budget_seconds
        live = dict(targets)
        self._opened = set()
        while live and time.monotonic() < deadline and not self._stop.is_set():
            for project, (url, key, batch) in sorted(live.items()):
                try:
                    pushed = self.push(conn, project, url, key, batch)
                except Exception:  # noqa: BLE001 - one project, not all of them
                    LOG.exception("Summarizing %s failed", project)
                    conn.rollback()
                    pushed = 0
                if not pushed:
                    del live[project]
        return bool(live) and not self._stop.is_set()

    def chat_for(self, url: str, key: str) -> Chat:
        """Return the client for one server, the same one every tick."""
        if (url, key) not in self._chats:
            self._chats[(url, key)] = Chat(stored_url=url, stored_key=key)
        return self._chats[(url, key)]

    def targets(self, cursor: Cursor) -> dict[str, tuple[str, str, int]]:
        """Return the projects switched on that name a server to push at."""
        wanted: dict[str, tuple[str, str, int]] = {}
        for project, _ in list_mountable_projects(cursor):
            settled = features.resolve(cursor, project, FEATURE_SUMMARIZE)
            if not settled.enabled:
                continue
            # The switch alone is not an invitation: without an address there
            # is nothing to push at, and the pull paths still work.
            chat = Chat(stored_url=settled.server_url)
            if chat.urls:
                wanted[project] = (
                    settled.server_url,
                    settled.server_key,
                    settled.batch,
                )
        return wanted

    def push(
        self,
        conn: Connection,
        project: str,
        url: str,
        key: str = "",
        batch: int = SUMMARIZE_BATCH,
    ) -> int:
        """Describe one batch of one project. Returns the files it settled.

        Zero means there is nothing to give this project now: its server is
        down, it went away mid-batch, or the project has nothing left.
        """
        chat = self.chat_for(url, key)
        if not chat.available():
            return 0

        with slow(f"opening the job of {project}"), conn.cursor() as cursor:
            job = self.job_for(cursor, project)
        conn.commit()
        if job is None:
            return 0

        token = str(uuid.uuid4())
        with slow(f"claiming a batch of {project}"), conn.cursor() as cursor:
            tasks, settled = self.take_batch(cursor, job, project, token, batch)
        conn.commit()
        if not tasks:
            # Only files closed from the cache count: a skipped file is owed
            # again by the next job, and counting it made the drain spin.
            with conn.cursor() as cursor:
                jobs.finish_job_if_drained(cursor, int(job["id"]))
            conn.commit()
            return settled

        for done, task in enumerate(tasks):
            if not self.describe(conn, chat, job, project, task):
                # The server went away mid-batch. What is left goes back with
                # its attempt returned, and the next tick starts over.
                with conn.cursor() as cursor:
                    jobs.hand_back(cursor, int(job["id"]), token)
                conn.commit()
                return done

        with conn.cursor() as cursor:
            jobs.finish_job_if_drained(cursor, int(job["id"]))
        conn.commit()
        LOG.info(
            "Summarized %d file(s) of %s as job %d", len(tasks), project, job["id"]
        )
        return len(tasks)

    def job_for(self, cursor: Cursor, project: str) -> dict[str, Any] | None:
        """Return the job to feed: the one open, or a new one over what is left.

        None when there is nothing to describe, which is the ordinary state of
        a project the model has been through.
        """
        open_job = jobs.running_job(cursor, project)
        if open_job is not None:
            return open_job
        # One new job per project per drain: if a skip mark did not hold, the
        # same files would otherwise be re-queued round after round.
        if project in self._opened:
            return None
        self._opened.add(project)
        job_id = jobs.create_job(
            cursor, project, LLM_INPUT_CHARS, False, WORKER_LEASE_SECONDS, None
        )
        if jobs.populate_job(cursor, job_id, project, False) == 0:
            jobs.finish_job_if_drained(cursor, job_id)
            return None
        LOG.info("Opened summary job %d for %s", job_id, project)
        return jobs.job_row(cursor, job_id)

    def take_batch(
        self,
        cursor: Cursor,
        job: dict[str, Any],
        project: str,
        token: str,
        batch: int = SUMMARIZE_BATCH,
    ) -> tuple[list[dict[str, Any]], int]:
        """Take a batch and read its text, settling what needs no model.

        Returns the tasks the model is needed for, and how many were handled
        without it: closed from the cache, or failed with an attempt spent.

        The same three settlements the lease route makes, in the same order: a
        lease that ran out goes back to the queue, a file the cache already
        answers is closed from the cache, and a file with no text to show is
        failed with the reason, and marked once its attempts are spent.
        """
        job_id = int(job["id"])
        input_chars = int(job["input_chars"])
        from enggraph.workerapi import apply_summary

        settled = jobs.fail_spent(cursor, job_id, project, WORKER_MAX_ATTEMPTS)
        jobs.reclaim_expired(cursor, job_id)
        for _, rel_path, summary in jobs.settle_cached(cursor, job_id, project):
            apply_summary(cursor, project, rel_path, summary)
            settled += 1

        claimed = jobs.claim_batch(
            cursor,
            job_id,
            batch,
            token,
            WORKER_ID,
            WORKER_LEASE_SECONDS,
            WORKER_MAX_ATTEMPTS,
        )
        if not claimed:
            return [], settled
        texts = jobs.read_task_content(
            cursor, project, [task["task_id"] for task in claimed], input_chars
        )

        ready: list[dict[str, Any]] = []
        for task in claimed:
            task_id = int(task["task_id"])
            text, reason = texts.get(task_id, ("", jobs.NO_FILE))
            if not text:
                state = jobs.fail_and_mark(
                    cursor,
                    task_id,
                    project,
                    str(task["file_path"]),
                    reason,
                    WORKER_MAX_ATTEMPTS,
                )
                LOG.info(
                    "No text in %s of %s (%s), attempt %s: %s",
                    task["file_path"],
                    project,
                    reason,
                    task.get("attempts"),
                    state,
                )
                # An attempt spent is progress: the next round takes the next
                # one at once, so three tries take seconds rather than ticks.
                settled += 1
                continue
            digest = content_key(text)
            if digest != task["content_hash"]:
                jobs.set_task_hash(cursor, task_id, digest)
            cached = get_cached_summary(cursor, project, digest)
            if cached is not None:
                apply_summary(cursor, project, str(task["file_path"]), cached)
                jobs.settle_task(cursor, task_id)
                settled += 1
                continue
            ready.append({**task, "task_id": task_id, "digest": digest, "text": text})
        return ready, settled

    def describe(
        self,
        conn: Connection,
        chat: Chat,
        job: dict[str, Any],
        project: str,
        task: dict[str, Any],
    ) -> bool:
        """Describe one file. False means the server stopped answering.

        Anything else - a refused answer, a node that has gone, a reply that
        says no more than the file name - is this file's own outcome and is
        recorded on it, exactly as the pull path records a worker's.
        """
        from enggraph.workerapi import apply_summary

        rel_path = str(task["file_path"])
        try:
            with slow(f"asking {chat.chosen} about {rel_path} of {project}"):
                reply = chat.ask(
                    SYSTEM_PROMPT,
                    f"File: {rel_path}\n\n{task['text']}",
                    LLM_MAX_TOKENS,
                )
        except ChatRejected as refused:
            with conn.cursor() as cursor:
                jobs.fail_and_mark(
                    cursor,
                    task["task_id"],
                    project,
                    rel_path,
                    str(refused),
                    WORKER_MAX_ATTEMPTS,
                )
            conn.commit()
            return True
        except ChatError:
            return False

        summary = strip_preamble(shape(reply), rel_path)
        with slow(f"writing {rel_path} of {project}"), conn.cursor() as cursor:
            # Cached before it is judged, and judged on every pass: an answer
            # the model will give again is not worth asking for again.
            put_cached_summary(cursor, project, str(task["digest"]), summary)
            applied, note = apply_summary(cursor, project, rel_path, summary)
            if applied:
                jobs.finish_task(cursor, task["task_id"], None)
            else:
                jobs.finish_task(cursor, task["task_id"], note)
        conn.commit()
        return True
