"""The worker API: who may call it, and what it does with what it is told.

No database here, the way test_summarizer fakes one: the queue functions are
monkeypatched at their `enggraph.workerapi` binding and the cursor is a mock.
What is worth testing is the boundary - the token, and the refusal to trust a
worker's answer or its expired lease.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from enggraph import workerapi

TOKEN = "0123456789abcdef0123456789abcdef"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
LEASE = "11111111-2222-3333-4444-555555555555"


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    """Build an app with a token set and no database behind it."""
    monkeypatch.setattr(workerapi, "WORKER_API_TOKEN", TOKEN)
    # The suite is not the service: an app built here must not start indexing
    # trees of its own on a thread nothing in the test is waiting for.
    monkeypatch.setattr(workerapi, "SCHEDULER_ENABLED", False)
    # Nor may it start draining the embedding queue: that thread would reach
    # for a database and for whatever EMBED_* the developer has set.
    monkeypatch.setattr(workerapi, "EMBED_LOOP_ENABLED", False)
    monkeypatch.setattr(workerapi, "SUMMARIZE_LOOP_ENABLED", False)
    cursor = MagicMock()

    @contextmanager
    def transaction() -> Iterator[MagicMock]:
        yield cursor

    monkeypatch.setattr(workerapi, "transaction", transaction)
    app = workerapi.create_app()
    with TestClient(app) as testing:
        testing.cursor = cursor
        yield testing


def test_health_needs_no_token(client: TestClient) -> None:
    """The compose healthcheck cannot carry one."""
    assert client.get("/health").status_code == 200


@pytest.mark.parametrize(
    "header",
    [None, "", "Bearer ", "Basic " + TOKEN, "Bearer wrong", "Bearer parole"],
)
def test_a_bad_header_is_refused(client: TestClient, header: str | None) -> None:
    """Including a non-ASCII one, which compare_digest would raise on."""
    headers = {} if header is None else {"Authorization": header}
    assert client.get("/jobs", headers=headers).status_code == 401


def test_the_docs_are_not_published(client: TestClient) -> None:
    """FastAPI cannot put them behind the token, so they stay off."""
    assert client.get("/docs").status_code == 404


def test_no_token_configured_refuses_to_serve(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """This service is published to the network and serves file text."""
    monkeypatch.setattr(workerapi, "WORKER_API_TOKEN", "")
    with pytest.raises(RuntimeError, match="WORKER_API_TOKEN"):
        workerapi.create_app()

    monkeypatch.setattr(workerapi, "WORKER_API_TOKEN", "short")
    with pytest.raises(RuntimeError, match="WORKER_API_TOKEN"):
        workerapi.create_app()


def test_an_oversize_reply_is_refused_before_it_is_shaped(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A worker cannot stuff a megabyte into the summary cache."""
    locked = MagicMock()
    monkeypatch.setattr(workerapi.jobs, "lock_leased_task", locked)
    answer = client.post(
        "/tasks/1/result",
        headers=AUTH,
        json={
            "worker_id": "w",
            "lease_token": LEASE,
            "summary": "x" * (workerapi.WORKER_MAX_REPLY_CHARS + 1),
        },
    )
    assert answer.status_code == 413
    locked.assert_not_called()


def test_a_result_on_an_expired_lease_touches_nothing(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The task has already been given to someone else."""
    saved = MagicMock()
    monkeypatch.setattr(workerapi.jobs, "lock_leased_task", lambda *_: None)
    monkeypatch.setattr(workerapi, "save_llm_summary", saved)
    monkeypatch.setattr(workerapi, "put_cached_summary", saved)
    answer = client.post(
        "/tasks/1/result",
        headers=AUTH,
        json={"worker_id": "w", "lease_token": LEASE, "summary": "Runs the thing."},
    )
    assert answer.status_code == 409
    saved.assert_not_called()


def test_an_answer_that_says_nothing_is_cached_but_not_applied(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The files a small model is worst at must not be re-asked every pass."""
    monkeypatch.setattr(
        workerapi.jobs,
        "lock_leased_task",
        lambda *_: {
            "task_id": 1,
            "job_id": 1,
            "file_path": "CHANGELOG.md",
            "content_hash": "a" * 64,
            "attempts": 1,
            "project": "demo",
            "job_status": "running",
        },
    )
    cached = MagicMock()
    saved = MagicMock(return_value=True)
    monkeypatch.setattr(workerapi, "put_cached_summary", cached)
    monkeypatch.setattr(workerapi, "save_llm_summary", saved)
    monkeypatch.setattr(workerapi.jobs, "finish_task", MagicMock())
    monkeypatch.setattr(workerapi.jobs, "finish_job_if_drained", MagicMock())
    monkeypatch.setattr(workerapi.jobs, "job_row", lambda *_: {"status": "running"})

    answer = client.post(
        "/tasks/1/result",
        headers=AUTH,
        json={"worker_id": "w", "lease_token": LEASE, "summary": "CHANGELOG.md"},
    )
    body = answer.json()
    assert answer.status_code == 200
    assert body["applied"] is False
    assert body["reason"] == workerapi.NOT_USEFUL
    cached.assert_called_once()
    saved.assert_not_called()


def test_a_manual_summary_is_never_overwritten(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """save_llm_summary refuses it, and the answer says so rather than lying."""
    monkeypatch.setattr(
        workerapi.jobs,
        "lock_leased_task",
        lambda *_: {
            "task_id": 1,
            "job_id": 1,
            "file_path": "app.py",
            "content_hash": "a" * 64,
            "attempts": 1,
            "project": "demo",
            "job_status": "running",
        },
    )
    monkeypatch.setattr(workerapi, "put_cached_summary", MagicMock())
    monkeypatch.setattr(workerapi, "save_llm_summary", MagicMock(return_value=False))
    monkeypatch.setattr(workerapi.jobs, "finish_task", MagicMock())
    monkeypatch.setattr(workerapi.jobs, "finish_job_if_drained", MagicMock())
    monkeypatch.setattr(workerapi.jobs, "job_row", lambda *_: {"status": "running"})

    body = client.post(
        "/tasks/1/result",
        headers=AUTH,
        json={
            "worker_id": "w",
            "lease_token": LEASE,
            "summary": "Serves the application over HTTP.",
        },
    ).json()
    assert body["applied"] is False
    assert "manual" in body["reason"]


def test_a_project_can_be_registered_before_it_reads_anything(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The organization case: the row and the address exist, no tree does."""
    registered: list[tuple[str, str, str | None]] = []

    def record(
        cursor: object,
        project: str,
        root_path: str,
        project_type: str | None = None,
    ) -> None:
        registered.append((project, root_path, project_type))

    monkeypatch.setattr(workerapi, "register_project", record)
    monkeypatch.setattr(workerapi, "project_rows", lambda cursor, names: {})
    response = client.post(
        "/projects", headers=AUTH, json={"name": "Mono Repo", "project_type": "docs"}
    )
    assert response.status_code == 201
    # The name is cleaned by the rule that names every project, and the row
    # still needs a root_path the column will accept.
    assert registered == [("mono-repo", "registered://mono-repo", "docs")]
    assert "make mounts" in response.json()["mounts"]


def test_a_reserved_project_name_is_refused_rather_than_crashing(
    client: TestClient,
) -> None:
    """`_settings` and its siblings hold records, not a tree."""
    response = client.post("/projects", headers=AUTH, json={"name": "_settings"})
    assert response.status_code == 409
    assert "reserved" in response.json()["detail"]


def test_a_registered_path_becomes_the_tree_the_project_reads(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The name is derived from the path, and the path is stored as it is."""
    registered: list[tuple[str, str, str | None]] = []

    def record(
        cursor: object,
        project: str,
        root_path: str,
        project_type: str | None = None,
    ) -> None:
        registered.append((project, root_path, project_type))

    monkeypatch.setattr(workerapi, "register_project", record)
    monkeypatch.setattr(workerapi, "project_rows", lambda cursor, names: {})
    response = client.post(
        "/projects", headers=AUTH, json={"name": "", "root_path": "/src/alpha/"}
    )
    assert response.status_code == 201
    assert registered == [("alpha", "/src/alpha", None)]


def test_scanning_a_tree_that_is_not_mounted_is_refused(client: TestClient) -> None:
    """Nothing is mounted at /code here, and a scan reads the tree."""
    response = client.post("/projects/mono/scan", headers=AUTH)
    assert response.status_code == 409
    assert "recreated before it can be scanned" in response.json()["detail"]


def test_the_settings_of_an_unmounted_project_report_only_that(
    client: TestClient,
) -> None:
    """Where a selection comes from cannot be answered without the tree."""
    body = client.get("/projects/mono/settings", headers=AUTH).json()
    assert body == {"project": "mono", "mounted": False}


def test_a_project_already_indexing_is_refused(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The guard `open_run` holds is the answer the dashboard already knew."""

    def refuse(
        cursor: object,
        project: str,
        project_type: str | None,
        fresh: bool,
    ) -> None:
        raise RuntimeError("job 7 is already indexing this project")

    monkeypatch.setattr(workerapi.indexjobs, "open_run", refuse)
    answer = client.post("/index", json={"project": "alpha"}, headers=AUTH)
    assert answer.status_code == 409
    assert answer.json()["detail"] == "job 7 is already indexing this project"


def test_an_accepted_run_is_handed_to_a_thread(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The row is answered with, and the work starts after the transaction."""
    started: list[tuple] = []
    monkeypatch.setattr(
        workerapi.indexjobs,
        "open_run",
        lambda cursor, project, project_type, fresh: {
            "id": 11,
            "project": project,
        },
    )
    monkeypatch.setattr(
        workerapi.indexjobs,
        "run_in_background",
        lambda *args: started.append(args),
    )
    answer = client.post(
        "/index", json={"project": "alpha", "root_path": "/src/alpha"}, headers=AUTH
    )
    assert answer.status_code == 202
    assert answer.json()["id"] == 11
    assert started == [(11, "alpha", "/src/alpha", None, False)]


def test_the_schedule_of_a_project_says_which_level_decided_it(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The resolution is the API's, so the dashboard never repeats it."""
    monkeypatch.setattr(
        workerapi.schedule,
        "resolve",
        lambda cursor, project: workerapi.schedule.Schedule(
            "auto", 60, 5, {"mode": "organization"}
        ),
    )
    monkeypatch.setattr(workerapi.indexjobs, "last_run", lambda cursor, project: None)
    body = client.get("/projects/mono/schedule", headers=AUTH).json()
    assert body["mode"] == "auto"
    assert body["watched"] is True
    assert body["origin"] == "organization"
    assert body["origins"]["mode"] == "organization"
    assert body["next_run"] is None


def test_the_schedules_listing_resolves_every_project_the_same_way(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The listing and the per-project answer are one rule, not two."""
    monkeypatch.setattr(
        workerapi,
        "list_mountable_projects",
        lambda cursor: [("mono", "/mono"), ("alpha", "/src/alpha")],
    )
    monkeypatch.setattr(
        workerapi.schedule,
        "resolve",
        lambda cursor, project: workerapi.schedule.Schedule(
            "auto" if project == "mono" else "off",
            60,
            5,
            {"mode": "project" if project == "mono" else "global"},
        ),
    )
    body = client.get("/schedules", headers=AUTH).json()
    assert [one["project"] for one in body["schedules"]] == ["alpha", "mono"]
    listed = {one["project"]: one for one in body["schedules"]}
    assert listed["mono"]["mode"] == "auto"
    assert listed["mono"]["watched"] is True
    assert listed["mono"]["origin"] == "project"
    assert listed["alpha"]["mode"] == "off"
    assert listed["alpha"]["origin"] == "global"


def run_row(project: str, **fields: object) -> dict:
    """Build a row shaped like the one `open_run` answers with."""
    return {
        "id": 1,
        "project": project,
        "status": "running",
        "error": None,
        "started_at": "2026-09-08T15:00:00Z",
        "finished_at": None,
        **fields,
    }


def organization(
    monkeypatch: pytest.MonkeyPatch,
    modes: dict[str, str],
    members: list[str],
) -> None:
    """Make `acme` an organization holding some members.

    `modes` names what each member resolves to, so a test says only which of
    them are off.
    """
    monkeypatch.setattr(
        workerapi,
        "stored_type",
        lambda cursor, project: "organization" if project == "acme" else "codebase",
    )
    monkeypatch.setattr(workerapi, "list_members", lambda cursor, project: members)
    monkeypatch.setattr(
        workerapi.schedule,
        "resolve",
        lambda cursor, project: workerapi.schedule.Schedule(
            modes.get(project, "auto"), 30, 5, {"mode": "global"}
        ),
    )


def test_an_organization_indexes_every_project_it_holds(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One run per member; the organization reads no tree of its own."""
    started: list[tuple] = []
    opened: list[str] = []

    def open_run(
        cursor: object,
        project: str,
        project_type: str | None,
        fresh: bool,
    ) -> dict:
        opened.append(project)
        return run_row(project, id=len(opened))

    organization(monkeypatch, {}, ["delta", "beta"])
    monkeypatch.setattr(workerapi.indexjobs, "open_run", open_run)
    monkeypatch.setattr(
        workerapi.indexjobs, "run_in_background", lambda *args: started.append(args)
    )
    answer = client.post("/index", json={"project": "acme"}, headers=AUTH)
    assert answer.status_code == 202
    assert opened == ["delta", "beta"]
    assert [one[1] for one in started] == ["delta", "beta"]
    assert answer.json()["status"] == "running"


def test_what_is_off_is_left_out_of_an_organization_run(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`off` is how a member asks to be indexed by hand only."""
    opened: list[str] = []
    organization(monkeypatch, {"beta": "off"}, ["delta", "beta"])
    monkeypatch.setattr(
        workerapi.indexjobs,
        "open_run",
        lambda cursor, project, project_type, fresh: (
            opened.append(project) or run_row(project)
        ),
    )
    monkeypatch.setattr(workerapi.indexjobs, "run_in_background", lambda *args: None)
    client.post("/index", json={"project": "acme"}, headers=AUTH)
    assert opened == ["delta"]


def test_an_organization_with_nothing_to_index_says_so(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every member off is a refusal, not an empty run."""
    organization(monkeypatch, {"delta": "off"}, ["delta"])
    answer = client.post("/index", json={"project": "acme"}, headers=AUTH)
    assert answer.status_code == 409
    assert "every" in answer.json()["detail"]


def test_a_member_already_indexing_is_skipped_not_refused(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The rest of the fan-out is what the caller asked for, and it runs."""

    def open_run(
        cursor: object,
        project: str,
        project_type: str | None,
        fresh: bool,
    ) -> dict:
        if project == "delta":
            raise RuntimeError("job 7 is already indexing this project")
        return run_row(project)

    organization(monkeypatch, {}, ["delta", "beta"])
    monkeypatch.setattr(workerapi.indexjobs, "open_run", open_run)
    monkeypatch.setattr(workerapi.indexjobs, "run_in_background", lambda *args: None)
    answer = client.post("/index", json={"project": "acme"}, headers=AUTH)
    assert answer.status_code == 202
    body = answer.json()
    assert body["skipped"] == [
        {"project": "delta", "why": "job 7 is already indexing this project"}
    ]


def test_a_project_is_answered_by_its_last_run(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One row, newest first, which is what the dashboard polls."""
    asked: list[str] = []
    monkeypatch.setattr(workerapi, "stored_type", lambda cursor, project: "codebase")
    monkeypatch.setattr(
        workerapi.indexjobs,
        "recent_jobs",
        lambda cursor, project, limit: (
            asked.append(project) or [{"id": 3, "project": project, "status": "done"}]
        ),
    )
    body = client.get("/projects/mono/index", headers=AUTH)
    assert body.json()["id"] == 3
    assert asked == ["mono"]


def test_an_organization_is_answered_by_every_run_under_it(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Failed if anything under it failed, and each failure names its project."""
    rows = {
        "beta": {"status": "done", "error": None, "files": 2},
        "delta": {"status": "failed", "error": "no mount", "files": None},
    }
    monkeypatch.setattr(
        workerapi,
        "stored_type",
        lambda cursor, project: "organization" if project == "acme" else "codebase",
    )
    monkeypatch.setattr(
        workerapi, "list_members", lambda cursor, project: ["beta", "delta"]
    )
    monkeypatch.setattr(
        workerapi.indexjobs,
        "recent_jobs",
        lambda cursor, project, limit: [run_row(project, **rows[project])],
    )
    body = client.get("/projects/acme/index", headers=AUTH).json()
    assert body["status"] == "failed"
    assert body["error"] == "delta: no mount"
    assert body["files"] == 2
    assert [one["project"] for one in body["runs"]] == ["beta", "delta"]


def test_embedding_a_query_says_which_server_answered(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The MCP server asks for this, and for nothing else, at search time."""

    class Answering:
        model = "nomic"
        chosen = "http://embedder:8080"

        def embed_one(self, text: str) -> list[float]:
            return [0.25, 0.5]

    monkeypatch.setattr(workerapi, "Embedder", lambda **_: Answering())
    answer = client.post("/embed", json={"text": "how does auth work"}, headers=AUTH)
    assert answer.status_code == 200
    assert answer.json() == {
        "model": "nomic",
        "dimensions": 2,
        "server": "http://embedder:8080",
        "embedding": [0.25, 0.5],
    }


def test_no_embedding_server_is_a_503_rather_than_a_500(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The caller drops to its lexical half on this, which a 500 would not say."""

    class Refusing:
        model = "nomic"
        chosen = None

        def embed_one(self, text: str) -> list[float]:
            raise workerapi.EmbedError("no embedding server answered at nowhere")

    monkeypatch.setattr(workerapi, "Embedder", lambda **_: Refusing())
    answer = client.post("/embed", json={"text": "anything"}, headers=AUTH)
    assert answer.status_code == 503
    assert "no embedding server answered" in answer.json()["detail"]


def test_probing_a_dead_url_is_an_answer_rather_than_an_error(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The settings page renders what it says; a 500 would render nothing."""

    class Refusing:
        model = "nomic"
        chosen = None
        urls = ["http://typo:8080"]

        def embed_one(self, text: str) -> list[float]:
            raise workerapi.EmbedError(
                "no embedding server answered at http://typo:8080"
            )

    monkeypatch.setattr(workerapi, "Embedder", lambda **_: Refusing())
    answer = client.post(
        "/embeddings/probe", json={"url": "http://typo:8080"}, headers=AUTH
    )
    assert answer.status_code == 200
    assert answer.json()["ok"] is False


def test_a_summary_job_is_refused_while_summarizing_is_switched_off(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The switch has to stop the work, not only hide the button that starts it."""
    client.cursor.fetchone.return_value = (1,)
    monkeypatch.setattr(
        workerapi.features,
        "resolve",
        lambda cursor, project, feature: workerapi.features.Feature(
            name=feature,
            allowed=False,
            enabled=False,
            server_url="",
            server_key="",
            key_saved_at="",
            batch=4,
            tick_seconds=30,
            budget_seconds=60,
            origins={"enabled": "global", "server_url": "global"},
        ),
    )
    answer = client.post("/jobs", json={"project": "alpha"}, headers=AUTH)
    assert answer.status_code == 409
    assert "switched off globally" in answer.json()["detail"]


def test_probing_a_chat_server_reports_what_it_runs(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A chat server is not an embeddings server, so it has a probe of its own."""

    class Answering:
        chosen = "http://gpu:8080"
        model = "qwen2.5-coder-1.5b-instruct-q4_k_m.gguf"
        n_ctx = 8192
        urls = ["http://gpu:8080"]

        def props(self) -> dict[str, object]:
            return {}

    monkeypatch.setattr(workerapi, "Chat", lambda **_: Answering())
    answer = client.post(
        "/summaries/probe", json={"url": "http://gpu:8080"}, headers=AUTH
    )
    assert answer.status_code == 200
    assert answer.json() == {
        "ok": True,
        "server": "http://gpu:8080",
        "model": "qwen2.5-coder-1.5b-instruct-q4_k_m.gguf",
        "context": 8192,
    }


def test_a_chat_server_that_is_not_there_is_an_answer_not_an_error(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The settings page renders what it says; a 500 would render nothing."""

    class Refusing:
        chosen = None
        model = ""
        n_ctx = 0
        urls = ["http://typo:8080"]

        def props(self) -> dict[str, object]:
            raise workerapi.ChatError(
                "no llama.cpp server answered at http://typo:8080"
            )

    monkeypatch.setattr(workerapi, "Chat", lambda **_: Refusing())
    answer = client.post(
        "/summaries/probe", json={"url": "http://typo:8080"}, headers=AUTH
    )
    assert answer.status_code == 200
    assert answer.json()["ok"] is False
