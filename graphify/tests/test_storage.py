"""How a project row and the directories it reads are registered.

None of it needs a database: the statements are few and shaped by hand, so a
cursor holding two dictionaries answers them and the behaviour that matters
can be pinned - which type a re-index writes, which pairing of a name and a
path is refused, and how a project made of several directories is assembled.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from ctxgraph.config import BUILTIN_PROJECT_TYPES
from ctxgraph.storage import (
    absorb_project,
    add_member,
    add_source,
    clear_settings,
    detach_source,
    drop_member,
    drop_source,
    ensure_project,
    has_settings,
    list_files_without_llm_summary,
    list_members,
    list_memberships,
    list_sources,
    move_source,
    promote_root,
    prune_missing_files,
    read_settings,
    read_settings_json,
    register_project,
    set_selection_origin,
    write_settings,
    write_settings_json,
)


class FakeCursor:
    """Answer the statements `ctxgraph.storage` sends, out of two dictionaries.

    A queue would not do any more: registering one directory now reads the
    projects row, the sources of that project and the owner of that path, in
    an order the tests have no business depending on. Anything not recognised
    falls back to `rows`, which is what the summary-pass tests still use.
    """

    def __init__(
        self,
        rows: list[tuple[Any, ...] | None] | None = None,
        projects: dict[str, tuple[str, str]] | None = None,
        sources: list[tuple[str, str, str]] | None = None,
        settings: dict[tuple[str, str], tuple[str | None, str | None]] | None = None,
        objects: dict[tuple[str, str], dict] | None = None,
        records: list[tuple[str, str, str]] | None = None,
        nodes: list[tuple[str, str]] | None = None,
        members: list[tuple[str, str]] | None = None,
        running: set[str] | None = None,
    ) -> None:
        """Seed the database this cursor pretends to be."""
        self.rows = list(rows or [])
        # name -> (root_path, type)
        self.projects = dict(projects or {})
        # (project, alias, root_path), in the order they were added
        self.sources = list(sources or [])
        # (project, alias) -> (ctxkeep, ctxignore)
        self.settings = dict(settings or {})
        # (project, alias) -> the settings JSONB of that level
        self.objects = {key: dict(value) for key, value in (objects or {}).items()}
        # (built-in project, node id, what it is about), for the records an
        # agent wrote: plans, memories and suggestions
        self.records = list(records or [])
        # (project, node id), what an index run built rather than what an agent
        # wrote
        self.nodes = list(nodes or [])
        # (organization, project), the projects an organization holds
        self.members = list(members or [])
        # the projects with an index run open
        self.running = set(running or set())
        # (project, alias) -> (keep_source, ignore_source)
        self.origins: dict[tuple[str, str], tuple[str, str]] = {}
        self.calls: list[tuple[str, tuple[Any, ...]]] = []
        # What the last statement changed, as psycopg2 reports it.
        self.rowcount = 0
        self.answer: list[tuple[Any, ...]] | None = None

    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> None:
        """Apply the statement to the dictionaries, or record and ignore it."""
        self.calls.append((sql, params))
        text = " ".join(sql.split())
        before = len(self.nodes)
        self.answer = self._run(text, params)
        self.rowcount = before - len(self.nodes)

    def _run(self, text: str, params: tuple[Any, ...]) -> list[tuple[Any, ...]] | None:
        if text.startswith("SELECT type FROM projects WHERE name"):
            stored = self.projects.get(params[0])
            return [(stored[1],)] if stored else []
        if text.startswith("SELECT 1 FROM projects WHERE name"):
            return [(1,)] if params[0] in self.projects else []
        if text.startswith("SELECT name FROM projects WHERE root_path"):
            return [
                (name,)
                for name, (root, _) in self.projects.items()
                if root == params[0] and name != params[1]
            ]
        if text.startswith("SELECT alias, root_path FROM project_sources"):
            return [
                (alias, root)
                for project, alias, root in self.sources
                if project == params[0]
            ]
        if text.startswith("SELECT project FROM project_sources WHERE root_path"):
            return [
                (project,) for project, _, root in self.sources if root == params[0]
            ]
        if text.startswith("SELECT alias FROM project_sources WHERE project"):
            return [
                (alias,)
                for project, alias, root in self.sources
                if project == params[0] and root == params[1]
            ]
        if text.startswith("SELECT ctxkeep, ctxignore FROM project_settings"):
            stored = self.settings.get((params[0], params[1]))
            return [stored] if stored else []
        if text.startswith("SELECT 1 FROM project_settings WHERE project"):
            return [(1,) for project, _ in self.settings if project == params[0]][:1]
        if text.startswith("SELECT settings FROM project_settings"):
            stored = self.objects.get((params[0], params[1]))
            return [(stored,)] if stored is not None else []
        if text.startswith("INSERT INTO project_settings (project, alias, settings)"):
            merged = dict(self.objects.get((params[0], params[1]), {}))
            merged.update(json.loads(params[2]))
            self.objects[(params[0], params[1])] = merged
            return []
        if text.startswith("UPDATE project_settings SET settings = settings -"):
            stored = self.objects.get((params[1], params[2]))
            if stored is not None:
                stored.pop(params[0], None)
            return []
        if text.startswith("INSERT INTO project_settings (project, alias, ctxkeep"):
            self.settings[(params[0], params[1])] = (params[2], params[3])
            return []
        if text.startswith("DELETE FROM project_settings"):
            self.settings.pop((params[0], params[1]), None)
            return []
        if text.startswith("UPDATE project_sources SET keep_source"):
            self.origins[(params[2], params[3])] = (params[0], params[1])
            return []
        if text.startswith("SELECT project FROM project_members"):
            return [
                (project,)
                for organization, project in self.members
                if organization == params[0]
            ]
        if text.startswith("SELECT organization FROM project_members"):
            return [
                (organization,)
                for organization, project in self.members
                if project == params[0]
            ]
        if text.startswith("INSERT INTO project_members"):
            if (params[0], params[1]) not in self.members:
                self.members.append((params[0], params[1]))
            return []
        if text.startswith("DELETE FROM project_members"):
            self.members = [
                entry for entry in self.members if entry != (params[0], params[1])
            ]
            return []
        if text.startswith("SELECT project FROM index_jobs"):
            return [(name,) for name in (params[0], params[1]) if name in self.running]
        if text.startswith("SELECT project, id FROM graph_nodes"):
            return [
                (project, node)
                for project, node, about in self.records
                if project in params[0] and about == params[1]
            ]
        if text.startswith("SELECT id FROM graph_nodes WHERE project"):
            return [
                (node,)
                for project, node, _ in self.records
                if project == params[0] and node in params[1]
            ]
        if text.startswith("UPDATE project_settings SET project"):
            stored = self.settings.pop((params[2], params[3]), None)
            if stored is not None:
                self.settings[(params[0], params[1])] = stored
            return []
        if text.startswith("UPDATE project_sources SET project"):
            self.sources = [
                (params[0], params[1], root)
                if (project, alias) == (params[2], params[3])
                else (project, alias, root)
                for project, alias, root in self.sources
            ]
            return []
        if text.startswith("UPDATE graph_nodes SET metadata"):
            self.records = [
                (project, node, params[0])
                if project == params[1] and about == params[2]
                else (project, node, about)
                for project, node, about in self.records
            ]
            return []
        if text.startswith("UPDATE graph_nodes SET id"):
            self.records = [
                (project, params[0] + node.split("/", 1)[-1], params[1])
                if project in params[2] and about == params[3]
                else (project, node, about)
                for project, node, about in self.records
            ]
            return []
        if text.startswith("DELETE FROM graph_nodes WHERE project = %s AND file_path"):
            keep, scope = params[1], params[2]
            self.nodes = [
                entry
                for entry in self.nodes
                if entry[0] != params[0]
                or entry[1] in keep
                or (scope != "" and not entry[1].startswith(scope))
            ]
            return []
        if text.startswith("DELETE FROM graph_nodes WHERE project = %s AND starts"):
            self.nodes = [
                entry
                for entry in self.nodes
                if not (entry[0] == params[0] and entry[1].startswith(params[1]))
            ]
            return []
        if text.startswith("DELETE FROM graph_nodes WHERE project"):
            self.nodes = [entry for entry in self.nodes if entry[0] != params[0]]
            return []
        if text.startswith("DELETE FROM file_hashes"):
            return []
        if text.startswith("DELETE FROM index_jobs"):
            return []
        if text.startswith("DELETE FROM projects WHERE name"):
            self.projects.pop(params[0], None)
            self.nodes = [entry for entry in self.nodes if entry[0] != params[0]]
            self.sources = [entry for entry in self.sources if entry[0] != params[0]]
            self.settings = {
                key: value
                for key, value in self.settings.items()
                if key[0] != params[0]
            }
            return []
        if text.startswith("INSERT INTO projects"):
            name, root, project_type, default, _ = params
            stored = self.projects.get(name)
            if stored is None:
                self.projects[name] = (root, project_type or default)
            else:
                self.projects[name] = (stored[0], project_type or stored[1])
            return []
        if text.startswith("INSERT INTO project_sources"):
            self.sources.append((params[0], params[1], params[2]))
            return []
        if text.startswith("UPDATE projects SET root_path = %s"):
            stored = self.projects.get(params[1])
            if stored is not None:
                self.projects[params[1]] = (params[0], stored[1])
            return []
        if text.startswith("UPDATE projects SET root_path"):
            primary = next(
                (root for project, _, root in self.sources if project == params[0]),
                None,
            )
            if primary is not None:
                self.projects[params[1]] = (primary, self.projects[params[1]][1])
            return []
        if text.startswith("UPDATE project_sources SET alias"):
            renamed = []
            for project, alias, root in self.sources:
                if project == params[1] and alias == "":
                    alias = params[0]
                renamed.append((project, alias, root))
            self.sources = renamed
            return []
        if text.startswith("DELETE FROM project_sources"):
            self.sources = [
                entry
                for entry in self.sources
                if not (entry[0] == params[0] and entry[1] == params[1])
            ]
            return []
        return None

    def fetchone(self) -> tuple[Any, ...] | None:
        """Answer with the first row of the answer, or the next queued one."""
        if self.answer is not None:
            return self.answer[0] if self.answer else None
        return self.rows.pop(0) if self.rows else None

    def fetchall(self) -> list[tuple[Any, ...] | None]:
        """Answer with every row of the answer, or with what is queued."""
        if self.answer is not None:
            return list(self.answer)
        rows, self.rows = self.rows, []
        return rows


def upsert_params(cursor: FakeCursor) -> tuple[Any, ...]:
    """Read back the parameters of the INSERT into projects."""
    for sql, params in reversed(cursor.calls):
        if sql.lstrip().startswith("INSERT INTO projects"):
            return params
    raise AssertionError("no INSERT INTO projects was sent")


def test_a_first_index_defaults_to_codebase() -> None:
    """A project registered without a type is a codebase."""
    cursor = FakeCursor()
    ensure_project(cursor, "alpha", "/src/alpha")
    assert upsert_params(cursor) == ("alpha", "/src/alpha", None, "codebase", None)


def test_a_named_type_is_written() -> None:
    """TYPE= on the command line reaches the statement."""
    cursor = FakeCursor()
    ensure_project(cursor, "alpha", "/src/alpha", "docs")
    assert upsert_params(cursor) == ("alpha", "/src/alpha", "docs", "codebase", "docs")


def test_a_plain_reindex_leaves_the_stored_type_alone() -> None:
    """None on both sides is what makes the COALESCE keep what is stored."""
    cursor = FakeCursor(
        projects={"alpha": ("/src/alpha", "docs")},
        sources=[("alpha", "", "/src/alpha")],
    )
    ensure_project(cursor, "alpha", "/src/alpha")
    assert upsert_params(cursor)[2] is None
    assert upsert_params(cursor)[4] is None


@pytest.mark.parametrize("project_type", sorted(BUILTIN_PROJECT_TYPES))
def test_refuses_to_index_into_a_builtin_project(project_type: str) -> None:
    """There is no tree behind one of these; a run would prune it empty."""
    root = f"{project_type}://agent"
    cursor = FakeCursor(projects={f"_{project_type}": (root, project_type)})
    with pytest.raises(RuntimeError, match=f"agent {project_type}"):
        ensure_project(cursor, f"_{project_type}", root)


def test_still_refuses_a_name_pointing_at_another_path() -> None:
    """A second checkout of the same basename must not merge into the first."""
    cursor = FakeCursor(
        projects={"alpha": ("/src/other", "codebase")},
        sources=[("alpha", "", "/src/other")],
    )
    with pytest.raises(RuntimeError, match="already indexed from"):
        ensure_project(cursor, "alpha", "/src/alpha")


def test_refuses_a_path_another_project_reads() -> None:
    """One directory belongs to one project, whatever it is called there."""
    cursor = FakeCursor(
        projects={"mono": ("/mono/configs", "codebase")},
        sources=[("mono", "configs", "/mono/configs")],
    )
    with pytest.raises(RuntimeError, match="already indexed as 'mono'"):
        ensure_project(cursor, "configs", "/mono/configs")


def test_a_first_index_records_the_tree_as_the_unnamed_source() -> None:
    """The project a plain index run creates reads its whole tree."""
    cursor = FakeCursor()
    ensure_project(cursor, "alpha", "/src/alpha")
    assert list_sources(cursor, "alpha") == [("", "/src/alpha")]


def test_a_project_with_no_directory_is_not_indexed() -> None:
    """One onboarded ahead of its slices must not adopt the path it was given."""
    cursor = FakeCursor(projects={"mono": ("/mono", "codebase")})
    with pytest.raises(RuntimeError, match="reads no directories yet"):
        ensure_project(cursor, "mono", "/mono")


def test_a_project_of_named_directories_indexes_under_its_registered_root() -> None:
    """The synthetic root is not a directory, and is not added as one.

    `projects.root_path` of a project assembled from named directories is
    `registered://<name>`: there is no one tree to point it at. An index run
    arrives with that value, and registering it as a source would be refused
    by the rule that keeps an unnamed source out of a project that has named
    ones - which is what stopped every such project from indexing at all.
    """
    cursor = FakeCursor(
        projects={"mono": ("registered://mono", "codebase")},
        sources=[("mono", "configs", "/mono/configs")],
    )
    ensure_project(cursor, "mono", "registered://mono")
    assert list_sources(cursor, "mono") == [("configs", "/mono/configs")]


def test_a_project_can_be_registered_without_a_directory() -> None:
    """Onboarding a monorepo writes the row and reads nothing yet."""
    cursor = FakeCursor()
    register_project(cursor, "mono", "/mono", None, "", with_source=False)
    assert list_sources(cursor, "mono") == []


def test_directories_are_added_under_their_alias() -> None:
    """Each slice keeps the name its node ids are prefixed with."""
    cursor = FakeCursor(projects={"mono": ("/mono", "codebase")})
    add_source(cursor, "mono", "configs", "/mono/deploy/configs")
    add_source(cursor, "mono", "agents", "/mono/tools/agents")
    assert list_sources(cursor, "mono") == [
        ("configs", "/mono/deploy/configs"),
        ("agents", "/mono/tools/agents"),
    ]


def test_a_project_of_named_directories_has_no_root_of_its_own() -> None:
    """It is a container rather than a tree, so no slice stands in for it."""
    cursor = FakeCursor(projects={"mono": ("/mono", "codebase")})
    add_source(cursor, "mono", "configs", "/mono/deploy/configs")
    assert cursor.projects["mono"][0] == "registered://mono"


def test_a_named_directory_is_refused_beside_a_whole_tree() -> None:
    """Mixing the two would nest one mount inside another."""
    cursor = FakeCursor(
        projects={"alpha": ("/src/alpha", "codebase")},
        sources=[("alpha", "", "/src/alpha")],
    )
    with pytest.raises(RuntimeError, match="source-promote"):
        add_source(cursor, "alpha", "docs", "/src/alpha-docs")


def test_a_whole_tree_is_refused_beside_named_directories() -> None:
    """The other direction of the same rule."""
    cursor = FakeCursor(
        projects={"mono": ("/mono/configs", "codebase")},
        sources=[("mono", "configs", "/mono/configs")],
    )
    with pytest.raises(RuntimeError, match="pass an alias"):
        add_source(cursor, "mono", "", "/mono")


def test_adding_the_same_directory_twice_is_quiet() -> None:
    """Onboarding is re-run to pick up what is missing, not to fail."""
    cursor = FakeCursor(
        projects={"mono": ("/mono/configs", "codebase")},
        sources=[("mono", "configs", "/mono/configs")],
    )
    add_source(cursor, "mono", "configs", "/mono/configs")
    assert list_sources(cursor, "mono") == [("configs", "/mono/configs")]


def test_an_alias_is_not_repointed_in_place() -> None:
    """The nodes under it would outlive the directory that wrote them."""
    cursor = FakeCursor(
        projects={"mono": ("/mono/configs", "codebase")},
        sources=[("mono", "configs", "/mono/configs")],
    )
    with pytest.raises(RuntimeError, match="drop that source"):
        add_source(cursor, "mono", "configs", "/mono/other")


def test_the_last_directory_is_not_dropped() -> None:
    """A project reading nothing is a project to drop, not to keep."""
    cursor = FakeCursor(
        projects={"mono": ("/mono/configs", "codebase")},
        sources=[("mono", "configs", "/mono/configs")],
    )
    with pytest.raises(RuntimeError, match="only source"):
        drop_source(cursor, "mono", "configs")


def test_dropping_a_directory_leaves_the_container_root_alone() -> None:
    """The column names a tree, and a project of slices is not one."""
    cursor = FakeCursor(
        projects={"mono": ("registered://mono", "codebase")},
        sources=[
            ("mono", "configs", "/mono/configs"),
            ("mono", "agents", "/mono/agents"),
        ],
    )
    drop_source(cursor, "mono", "configs")
    assert list_sources(cursor, "mono") == [("agents", "/mono/agents")]
    assert cursor.projects["mono"][0] == "registered://mono"


def test_promoting_names_the_whole_tree() -> None:
    """What turns a one-directory project into one that can take a second."""
    cursor = FakeCursor(
        projects={"alpha": ("/src/alpha", "codebase")},
        sources=[("alpha", "", "/src/alpha")],
    )
    promote_root(cursor, "alpha", "root")
    assert list_sources(cursor, "alpha") == [("root", "/src/alpha")]
    add_source(cursor, "alpha", "docs", "/src/alpha-docs")
    assert [alias for alias, _ in list_sources(cursor, "alpha")] == ["root", "docs"]


def test_promoting_a_project_that_has_no_whole_tree_is_refused() -> None:
    """There is nothing to rename, and no id would change."""
    cursor = FakeCursor(
        projects={"mono": ("/mono/configs", "codebase")},
        sources=[("mono", "configs", "/mono/configs")],
    )
    with pytest.raises(RuntimeError, match="no unnamed source"):
        promote_root(cursor, "mono", "root")


def test_absorbing_a_project_moves_its_tree_under_an_alias() -> None:
    """The whole point: one directory, one project, one graph."""
    cursor = FakeCursor(
        projects={
            "mono": ("registered://mono", "codebase"),
            "api": ("/src/api", "codebase"),
        },
        sources=[("api", "", "/src/api")],
    )
    absorb_project(cursor, "mono", "api", "")
    assert list_sources(cursor, "mono") == [("api", "/src/api")]
    assert "api" not in cursor.projects
    assert cursor.projects["mono"][0] == "registered://mono"


def test_an_absorbed_tree_takes_the_alias_it_was_given() -> None:
    """The move names the directory, because the old name is about to go."""
    cursor = FakeCursor(
        projects={
            "mono": ("registered://mono", "codebase"),
            "api": ("/src/api", "codebase"),
        },
        sources=[("api", "", "/src/api")],
    )
    absorb_project(cursor, "mono", "api", "services")
    assert list_sources(cursor, "mono") == [("services", "/src/api")]


def test_absorbing_keeps_the_aliases_a_project_already_had() -> None:
    """Its slices are already named, and renaming them names nothing new."""
    cursor = FakeCursor(
        projects={
            "mono": ("/mono/configs", "codebase"),
            "tools": ("/tools/lint", "codebase"),
        },
        sources=[
            ("mono", "configs", "/mono/configs"),
            ("tools", "lint", "/tools/lint"),
            ("tools", "fmt", "/tools/fmt"),
        ],
    )
    absorb_project(cursor, "mono", "tools", "")
    assert list_sources(cursor, "mono") == [
        ("configs", "/mono/configs"),
        ("lint", "/tools/lint"),
        ("fmt", "/tools/fmt"),
    ]


def test_a_named_alias_is_refused_for_a_project_of_slices() -> None:
    """There is no single directory for the alias to name."""
    cursor = FakeCursor(
        projects={
            "mono": ("/mono/configs", "codebase"),
            "tools": ("/tools/lint", "codebase"),
        },
        sources=[
            ("mono", "configs", "/mono/configs"),
            ("tools", "lint", "/tools/lint"),
        ],
    )
    with pytest.raises(RuntimeError, match="each keeps the alias it has"):
        absorb_project(cursor, "mono", "tools", "everything")


def test_a_project_mounted_whole_absorbs_nothing() -> None:
    """Mixing an unnamed source with named ones nests one mount in another."""
    cursor = FakeCursor(
        projects={
            "alpha": ("/src/alpha", "codebase"),
            "api": ("/src/api", "codebase"),
        },
        sources=[("alpha", "", "/src/alpha"), ("api", "", "/src/api")],
    )
    with pytest.raises(RuntimeError, match="source-promote"):
        absorb_project(cursor, "alpha", "api", "")


def test_absorbing_is_refused_when_the_alias_is_taken() -> None:
    """Two directories under one alias would produce one set of node ids."""
    cursor = FakeCursor(
        projects={
            "mono": ("/mono/api", "codebase"),
            "api": ("/src/api", "codebase"),
        },
        sources=[("mono", "api", "/mono/api"), ("api", "", "/src/api")],
    )
    with pytest.raises(RuntimeError, match="already reads 'api'"):
        absorb_project(cursor, "mono", "api", "")


def test_a_project_reading_nothing_is_not_absorbed() -> None:
    """There is no directory to move, so the move is a drop by another name."""
    cursor = FakeCursor(
        projects={
            "mono": ("registered://mono", "codebase"),
            "empty": ("registered://empty", "codebase"),
        },
    )
    with pytest.raises(RuntimeError, match="reads no directory"):
        absorb_project(cursor, "mono", "empty", "")


def test_a_builtin_project_is_not_absorbed() -> None:
    """It holds records written by an agent; no directory is behind it."""
    cursor = FakeCursor(
        projects={
            "mono": ("registered://mono", "codebase"),
            "_memory": ("memory://agent", "memory"),
        },
    )
    with pytest.raises(RuntimeError, match="holds agent memory"):
        absorb_project(cursor, "mono", "_memory", "")


def test_a_project_being_indexed_is_not_absorbed() -> None:
    """The run walks the directories the move is changing under it."""
    cursor = FakeCursor(
        projects={
            "mono": ("registered://mono", "codebase"),
            "api": ("/src/api", "codebase"),
        },
        sources=[("api", "", "/src/api")],
        running={"api"},
    )
    with pytest.raises(RuntimeError, match="is being indexed"):
        absorb_project(cursor, "mono", "api", "")


def test_the_settings_of_a_directory_follow_it() -> None:
    """A selection describes a directory rather than the project it was in."""
    cursor = FakeCursor(
        projects={
            "mono": ("registered://mono", "codebase"),
            "api": ("/src/api", "codebase"),
        },
        sources=[("api", "", "/src/api")],
        settings={("api", ""): ("*.py\n", None)},
    )
    absorb_project(cursor, "mono", "api", "")
    assert cursor.settings == {("mono", "api"): ("*.py\n", None)}


def test_records_follow_the_name_they_are_about() -> None:
    """A plan keeps its id; a memory and a suggestion are scoped by theirs."""
    cursor = FakeCursor(
        projects={
            "mono": ("registered://mono", "codebase"),
            "api": ("/src/api", "codebase"),
        },
        sources=[("api", "", "/src/api")],
        records=[
            ("_plans", "rewrite-the-router", "api"),
            ("_memory", "api/commit-style", "api"),
            ("_suggestions", "api/hcl-no-parser", "api"),
            ("_memory", "alpha/commit-style", "alpha"),
        ],
    )
    report = absorb_project(cursor, "mono", "api", "")
    assert cursor.records == [
        ("_plans", "rewrite-the-router", "mono"),
        ("_memory", "mono/commit-style", "mono"),
        ("_suggestions", "mono/hcl-no-parser", "mono"),
        ("_memory", "alpha/commit-style", "alpha"),
    ]
    assert report["plans"] == 1
    assert report["memories"] == 1
    assert report["suggestions"] == 1


def test_a_record_id_already_taken_refuses_the_move() -> None:
    """Both records are real, and re-scoping one would overwrite the other."""
    cursor = FakeCursor(
        projects={
            "mono": ("registered://mono", "codebase"),
            "api": ("/src/api", "codebase"),
        },
        sources=[("api", "", "/src/api")],
        records=[
            ("_memory", "api/commit-style", "api"),
            ("_memory", "mono/commit-style", "mono"),
        ],
    )
    with pytest.raises(RuntimeError, match="mono/commit-style already exists"):
        absorb_project(cursor, "mono", "api", "")


def test_a_project_does_not_absorb_itself() -> None:
    """Every step of the move would then read and write the same rows."""
    cursor = FakeCursor(projects={"mono": ("/mono", "codebase")})
    with pytest.raises(RuntimeError, match="cannot absorb itself"):
        absorb_project(cursor, "mono", "mono", "")


def test_a_directory_moves_to_another_project_under_its_alias() -> None:
    """The sideways move: neither project is dropped, the directory changes hands."""
    cursor = FakeCursor(
        projects={
            "mono": ("/mono/configs", "codebase"),
            "tools": ("/tools/lint", "codebase"),
        },
        sources=[
            ("mono", "configs", "/mono/configs"),
            ("mono", "lint", "/tools/lint"),
            ("tools", "fmt", "/tools/fmt"),
        ],
    )
    moved = move_source(cursor, "mono", "lint", "tools", "")
    assert moved["alias"] == "lint"
    assert list_sources(cursor, "mono") == [("configs", "/mono/configs")]
    assert ("tools", "lint", "/tools/lint") in cursor.sources


def test_a_moved_directory_takes_the_alias_it_is_given() -> None:
    """The alias is the first segment of every id, so it is worth choosing."""
    cursor = FakeCursor(
        projects={
            "mono": ("/mono/configs", "codebase"),
            "tools": ("/tools/fmt", "codebase"),
        },
        sources=[
            ("mono", "configs", "/mono/configs"),
            ("mono", "lint", "/tools/lint"),
            ("tools", "fmt", "/tools/fmt"),
        ],
    )
    moved = move_source(cursor, "mono", "lint", "tools", "linters")
    assert moved["alias"] == "linters"
    assert ("tools", "linters", "/tools/lint") in cursor.sources


def test_a_whole_tree_moved_in_is_named_after_the_project_it_left() -> None:
    """It has no alias to keep, and its name is what anyone knows it by."""
    cursor = FakeCursor(
        projects={
            "gamma": ("/home/user/acme/gamma", "codebase"),
            "acme": ("/mono/configs", "codebase"),
        },
        sources=[
            ("gamma", "", "/home/user/acme/gamma"),
            ("acme", "configs", "/mono/configs"),
        ],
    )
    moved = move_source(cursor, "gamma", "", "acme", "")
    assert moved["alias"] == "gamma"


def test_moving_the_last_directory_leaves_a_project_reading_nothing() -> None:
    """A project with no tree is a legitimate row, not a refusal."""
    cursor = FakeCursor(
        projects={
            "api": ("/src/api", "codebase"),
            "mono": ("registered://mono", "codebase"),
        },
        sources=[("api", "", "/src/api")],
    )
    move_source(cursor, "api", "", "mono", "api")
    assert list_sources(cursor, "api") == []
    assert cursor.projects["api"][0] == "registered://api"
    assert list_sources(cursor, "mono") == [("api", "/src/api")]


def test_moving_a_project_s_only_directory_moves_the_project() -> None:
    """That is the project moving, so its row and its records go with it."""
    cursor = FakeCursor(
        projects={
            "gamma": ("/acme/gamma", "codebase"),
            "acme": ("registered://acme", "codebase"),
        },
        sources=[
            ("gamma", "", "/acme/gamma"),
            ("acme", "beta", "/acme/beta"),
        ],
        records=[("_plans", "rpm-pipeline", "gamma")],
    )
    moved = move_source(cursor, "gamma", "", "acme", "gamma", drop_empty=True)
    assert moved["dropped"] is True
    assert moved["plans"] == 1
    assert "gamma" not in cursor.projects
    assert cursor.records == [("_plans", "rpm-pipeline", "acme")]


def test_a_move_that_leaves_a_directory_behind_drops_nothing() -> None:
    """The project still reads something, so it is not the project moving."""
    cursor = FakeCursor(
        projects={
            "mono": ("registered://mono", "codebase"),
            "tools": ("registered://tools", "codebase"),
        },
        sources=[
            ("mono", "configs", "/mono/configs"),
            ("mono", "lint", "/tools/lint"),
            ("tools", "fmt", "/tools/fmt"),
        ],
        records=[("_plans", "rewrite-the-linter", "mono")],
    )
    moved = move_source(cursor, "mono", "lint", "tools", "lint", drop_empty=True)
    assert moved["dropped"] is False
    assert "mono" in cursor.projects
    assert cursor.records == [("_plans", "rewrite-the-linter", "mono")]


def test_detaching_never_drops_the_container_it_came_from() -> None:
    """A keeper that hands a slice back is meant to take another."""
    cursor = FakeCursor(
        projects={"acme": ("registered://acme", "codebase")},
        sources=[("acme", "gamma", "/acme/gamma")],
    )
    detach_source(cursor, "acme", "gamma", "gamma", "codebase")
    assert "acme" in cursor.projects
    assert list_sources(cursor, "acme") == []


def test_a_moved_directory_leaves_no_nodes_behind() -> None:
    """Nothing would prune them: a project reading nothing is never indexed."""
    cursor = FakeCursor(
        projects={
            "mono": ("/mono/configs", "codebase"),
            "tools": ("registered://tools", "codebase"),
        },
        sources=[
            ("mono", "configs", "/mono/configs"),
            ("mono", "lint", "/tools/lint"),
        ],
        nodes=[
            ("mono", "lint/main.py"),
            ("mono", "lint/main.py::run"),
            ("mono", "configs/nginx.conf"),
        ],
    )
    move_source(cursor, "mono", "lint", "tools", "lint")
    assert cursor.nodes == [("mono", "configs/nginx.conf")]


def test_a_directory_is_not_moved_into_a_project_mounted_whole() -> None:
    """An unnamed source and a named one cannot share a project."""
    cursor = FakeCursor(
        projects={
            "mono": ("/mono/configs", "codebase"),
            "alpha": ("/src/alpha", "codebase"),
        },
        sources=[
            ("mono", "configs", "/mono/configs"),
            ("alpha", "", "/src/alpha"),
        ],
    )
    with pytest.raises(RuntimeError, match="source-promote"):
        move_source(cursor, "mono", "configs", "alpha", "")


def test_a_move_onto_an_alias_already_read_is_refused() -> None:
    """Two directories under one alias produce one set of node ids."""
    cursor = FakeCursor(
        projects={
            "mono": ("/mono/lint", "codebase"),
            "tools": ("/tools/lint", "codebase"),
        },
        sources=[("mono", "lint", "/mono/lint"), ("tools", "lint", "/tools/lint")],
    )
    with pytest.raises(RuntimeError, match="already reads 'lint'"):
        move_source(cursor, "mono", "lint", "tools", "")


def test_a_directory_is_not_moved_into_a_builtin_project() -> None:
    """It holds records written by an agent; no directory is read into it."""
    cursor = FakeCursor(
        projects={
            "mono": ("/mono/configs", "codebase"),
            "_memory": ("memory://agent", "memory"),
        },
        sources=[("mono", "configs", "/mono/configs")],
    )
    with pytest.raises(RuntimeError, match="holds agent memory"):
        move_source(cursor, "mono", "configs", "_memory", "")


def test_a_move_records_nothing_about_either_project() -> None:
    """Both names survive, so what was written about them still describes them."""
    cursor = FakeCursor(
        projects={
            "mono": ("/mono/lint", "codebase"),
            "tools": ("registered://tools", "codebase"),
        },
        sources=[("mono", "lint", "/mono/lint")],
        records=[("_plans", "rewrite-the-linter", "mono")],
    )
    move_source(cursor, "mono", "lint", "tools", "lint")
    assert cursor.records == [("_plans", "rewrite-the-linter", "mono")]


def test_detaching_makes_a_project_mounted_whole() -> None:
    """The inverse of absorbing: the ids lose the prefix they were given."""
    cursor = FakeCursor(
        projects={"mono": ("/mono/configs", "codebase")},
        sources=[
            ("mono", "configs", "/mono/configs"),
            ("mono", "api", "/src/api"),
        ],
    )
    moved = detach_source(cursor, "mono", "api", "api", "codebase")
    assert moved["project"] == "api"
    assert moved["alias"] == ""
    assert list_sources(cursor, "api") == [("", "/src/api")]
    assert cursor.projects["api"] == ("/src/api", "codebase")
    assert list_sources(cursor, "mono") == [("configs", "/mono/configs")]


def test_detaching_the_last_directory_empties_the_project_it_left() -> None:
    """What the merge did is undone exactly, both rows included."""
    cursor = FakeCursor(
        projects={"mono": ("/src/api", "codebase")},
        sources=[("mono", "api", "/src/api")],
    )
    detach_source(cursor, "mono", "api", "", None)
    assert list_sources(cursor, "mono") == []
    assert cursor.projects["mono"][0] == "registered://mono"
    # The name was derived from the directory rather than given.
    assert list_sources(cursor, "api") == [("", "/src/api")]


def test_detaching_onto_a_name_already_taken_is_refused() -> None:
    """That project exists; the directory is moved into it instead."""
    cursor = FakeCursor(
        projects={
            "mono": ("/mono/configs", "codebase"),
            "api": ("/other/api", "codebase"),
        },
        sources=[
            ("mono", "configs", "/mono/configs"),
            ("mono", "api", "/src/api"),
        ],
    )
    with pytest.raises(RuntimeError, match="already exists"):
        detach_source(cursor, "mono", "api", "api", None)


def test_detaching_under_a_reserved_name_is_refused() -> None:
    """The `_` prefix belongs to the projects holding an agent's records."""
    cursor = FakeCursor(
        projects={"mono": ("/src/api", "codebase")},
        sources=[("mono", "api", "/src/api")],
    )
    with pytest.raises(RuntimeError, match="reserved"):
        detach_source(cursor, "mono", "api", "_memory", None)


def test_detaching_a_directory_the_project_does_not_read_is_refused() -> None:
    """The alias names nothing, so there is nothing to take out."""
    cursor = FakeCursor(
        projects={"mono": ("/mono/configs", "codebase")},
        sources=[("mono", "configs", "/mono/configs")],
    )
    with pytest.raises(RuntimeError, match="has no source 'api'"):
        detach_source(cursor, "mono", "api", "api", None)


def test_pruning_one_directory_leaves_the_others_alone() -> None:
    """The whole reason a directory can be indexed on its own.

    A run that walked one source has discovered nothing about the rest, and
    reading their absence as deletion would take their graph with it.
    """
    cursor = FakeCursor(
        nodes=[
            ("mono", "configs/nginx.conf"),
            ("mono", "configs/gone.conf"),
            ("mono", "agents/main.py"),
        ],
    )
    prune_missing_files(cursor, "mono", ["configs/nginx.conf"], "configs")
    assert cursor.nodes == [
        ("mono", "configs/nginx.conf"),
        ("mono", "agents/main.py"),
    ]


def test_pruning_the_whole_project_still_reaches_every_directory() -> None:
    """Naming no alias is the run that walked all of them."""
    cursor = FakeCursor(
        nodes=[
            ("mono", "configs/nginx.conf"),
            ("mono", "agents/main.py"),
        ],
    )
    prune_missing_files(cursor, "mono", ["configs/nginx.conf"])
    assert cursor.nodes == [("mono", "configs/nginx.conf")]


def test_an_organization_holds_a_project_without_moving_it() -> None:
    """Membership is a reference: the member keeps its tree and its name."""
    cursor = FakeCursor(
        projects={
            "acme": ("registered://acme", "organization"),
            "gamma": ("/acme/gamma", "codebase"),
        },
        sources=[("gamma", "", "/acme/gamma")],
    )
    add_member(cursor, "acme", "gamma")
    assert list_members(cursor, "acme") == ["gamma"]
    assert list_memberships(cursor, "gamma") == ["acme"]
    assert list_sources(cursor, "gamma") == [("", "/acme/gamma")]


def test_a_project_belongs_to_several_organizations() -> None:
    """Which is the whole reason membership is not a move."""
    cursor = FakeCursor(
        projects={
            "acme": ("registered://acme", "organization"),
            "infra": ("registered://infra", "organization"),
            "gamma": ("/acme/gamma", "codebase"),
        },
        sources=[("gamma", "", "/acme/gamma")],
    )
    add_member(cursor, "acme", "gamma")
    add_member(cursor, "infra", "gamma")
    assert list_memberships(cursor, "gamma") == ["acme", "infra"]


def test_only_an_organization_holds_other_projects() -> None:
    """The type is what says a project is a set rather than a tree."""
    cursor = FakeCursor(
        projects={
            "beta": ("/acme/beta", "codebase"),
            "gamma": ("/acme/gamma", "codebase"),
        },
    )
    with pytest.raises(RuntimeError, match="not an organization"):
        add_member(cursor, "beta", "gamma")


def test_an_organization_does_not_hold_itself() -> None:
    """It would answer a search about itself with itself."""
    cursor = FakeCursor(
        projects={"acme": ("registered://acme", "organization")},
    )
    with pytest.raises(RuntimeError, match="cannot be part of itself"):
        add_member(cursor, "acme", "acme")


def test_two_organizations_do_not_hold_each_other() -> None:
    """One of the two has to be the member."""
    cursor = FakeCursor(
        projects={
            "acme": ("registered://acme", "organization"),
            "infra": ("registered://infra", "organization"),
        },
        members=[("infra", "acme")],
    )
    with pytest.raises(RuntimeError, match="already holds"):
        add_member(cursor, "acme", "infra")


def test_adding_the_same_member_twice_is_quiet() -> None:
    """The list is a set, and saying it again says nothing new."""
    cursor = FakeCursor(
        projects={
            "acme": ("registered://acme", "organization"),
            "gamma": ("/acme/gamma", "codebase"),
        },
    )
    add_member(cursor, "acme", "gamma")
    add_member(cursor, "acme", "gamma")
    assert list_members(cursor, "acme") == ["gamma"]


def test_an_organization_reads_no_directory() -> None:
    """Membership is a row: joining one must not re-mount or re-index a tree."""
    cursor = FakeCursor(
        projects={"acme": ("registered://acme", "organization")},
    )
    with pytest.raises(RuntimeError, match="holds projects, not directories"):
        add_source(cursor, "acme", "gamma", "/acme/gamma")


def test_a_directory_is_not_moved_into_an_organization() -> None:
    """The same rule by the route that made a member a directory by mistake."""
    cursor = FakeCursor(
        projects={
            "acme": ("registered://acme", "organization"),
            "gamma": ("/acme/gamma", "codebase"),
        },
        sources=[("gamma", "", "/acme/gamma")],
    )
    with pytest.raises(RuntimeError, match="holds projects, not directories"):
        move_source(cursor, "gamma", "", "acme", "gamma", drop_empty=True)


def test_a_project_is_not_absorbed_into_an_organization() -> None:
    """Absorbing is a move of every directory, and it lands the same way."""
    cursor = FakeCursor(
        projects={
            "acme": ("registered://acme", "organization"),
            "gamma": ("/acme/gamma", "codebase"),
        },
        sources=[("gamma", "", "/acme/gamma")],
    )
    with pytest.raises(RuntimeError, match="holds projects, not directories"):
        absorb_project(cursor, "acme", "gamma", "gamma")


def test_a_held_project_is_not_moved_into_another() -> None:
    """It would dissolve a name an organization is still pointing at."""
    cursor = FakeCursor(
        projects={
            "acme": ("registered://acme", "organization"),
            "gamma": ("/acme/gamma", "codebase"),
            "mono": ("registered://mono", "codebase"),
        },
        sources=[("gamma", "", "/acme/gamma"), ("mono", "beta", "/acme/beta")],
        members=[("acme", "gamma")],
    )
    with pytest.raises(RuntimeError, match="is part of 'acme'"):
        move_source(cursor, "gamma", "", "mono", "gamma", drop_empty=True)


def test_a_held_project_is_not_absorbed() -> None:
    """The same rule by the other route into the same place."""
    cursor = FakeCursor(
        projects={
            "acme": ("registered://acme", "organization"),
            "gamma": ("/acme/gamma", "codebase"),
            "mono": ("registered://mono", "codebase"),
        },
        sources=[("gamma", "", "/acme/gamma")],
        members=[("acme", "gamma")],
    )
    with pytest.raises(RuntimeError, match="is part of 'acme'"):
        absorb_project(cursor, "mono", "gamma", "")


def test_taking_a_project_out_lets_it_move_again() -> None:
    """Detaching from the organization is the step the refusal asks for."""
    cursor = FakeCursor(
        projects={
            "acme": ("registered://acme", "organization"),
            "gamma": ("/acme/gamma", "codebase"),
            "mono": ("registered://mono", "codebase"),
        },
        sources=[("gamma", "", "/acme/gamma"), ("mono", "beta", "/acme/beta")],
        members=[("acme", "gamma")],
    )
    drop_member(cursor, "acme", "gamma")
    move_source(cursor, "gamma", "", "mono", "gamma", drop_empty=True)
    assert "gamma" not in cursor.projects


def test_a_move_that_keeps_the_project_ignores_its_organizations() -> None:
    """Only dissolving the name is refused; a slice changing hands is not."""
    cursor = FakeCursor(
        projects={
            "acme": ("registered://acme", "organization"),
            "mono": ("registered://mono", "codebase"),
            "tools": ("registered://tools", "codebase"),
        },
        sources=[
            ("mono", "configs", "/mono/configs"),
            ("mono", "lint", "/tools/lint"),
            ("tools", "fmt", "/tools/fmt"),
        ],
        members=[("acme", "mono")],
    )
    move_source(cursor, "mono", "lint", "tools", "lint", drop_empty=True)
    assert list_memberships(cursor, "mono") == ["acme"]


def test_files_without_a_summary_come_back_as_paths() -> None:
    """The pass reads files from the mount, so only the path is needed."""
    cursor = FakeCursor([("src/app.py",), ("README.md",)])
    assert list_files_without_llm_summary(cursor, "alpha") == [
        "src/app.py",
        "README.md",
    ]


def test_refresh_widens_the_selection_to_what_the_model_wrote() -> None:
    """The flag reaches the statement; the CASE in it does the widening."""
    cursor = FakeCursor([])
    list_files_without_llm_summary(cursor, "alpha", True)
    assert cursor.calls[-1][1] == ("alpha", True)


def test_a_level_with_no_row_says_nothing_about_the_selection() -> None:
    """A missing row and a row of two NULLs are the same answer."""
    cursor = FakeCursor(settings={("alpha", ""): (None, None)})
    assert read_settings(cursor, "alpha", "") == (None, None)
    assert read_settings(cursor, "alpha", "docs") == (None, None)


def test_a_stored_pair_reads_back_verbatim() -> None:
    """Comments and blank lines are part of the document, not noise."""
    cursor = FakeCursor()
    keep = "# what this tree holds\n\n*.py\n"
    write_settings(cursor, "alpha", "", keep, "*.pem\n")
    assert read_settings(cursor, "alpha", "") == (keep, "*.pem\n")


def test_clearing_one_half_lets_the_level_above_answer() -> None:
    """Writing NULL is how a level stops speaking for a document."""
    cursor = FakeCursor()
    write_settings(cursor, "alpha", "", "*.py\n", "*.pem\n")
    write_settings(cursor, "alpha", "", None, "*.pem\n")
    assert read_settings(cursor, "alpha", "") == (None, "*.pem\n")


def test_a_project_holding_no_row_anywhere_is_reported_empty() -> None:
    """Onboarding writes the generated pair only into a project with none."""
    cursor = FakeCursor()
    assert has_settings(cursor, "alpha") is False
    write_settings(cursor, "alpha", "configs", "*.yaml\n", None)
    assert has_settings(cursor, "alpha") is True
    clear_settings(cursor, "alpha", "configs")
    assert has_settings(cursor, "alpha") is False


def test_the_run_records_where_each_source_read_its_selection() -> None:
    """The dashboard holds no mount, so the run that looked reports it."""
    cursor = FakeCursor(sources=[("mono", "configs", "/mono/configs")])
    set_selection_origin(cursor, "mono", "configs", "file", "global")
    assert cursor.origins[("mono", "configs")] == ("file", "global")


def test_dropping_a_directory_drops_the_settings_it_had() -> None:
    """A row for a directory nothing reads is listed nowhere.

    It would also decide the selection again if that alias came back.
    """
    cursor = FakeCursor(
        projects={"mono": ("/mono/configs", "codebase")},
        sources=[
            ("mono", "configs", "/mono/configs"),
            ("mono", "agents", "/mono/agents"),
        ],
        settings={
            ("mono", "agents"): ("*.py\n", None),
            ("mono", ""): ("*.md\n", None),
        },
    )
    drop_source(cursor, "mono", "agents")
    assert read_settings(cursor, "mono", "agents") == (None, None)
    # The project level is not a directory and is left exactly as it was.
    assert read_settings(cursor, "mono", "") == ("*.md\n", None)


def test_a_level_saying_nothing_holds_an_empty_object() -> None:
    """A missing row and a row with no keys answer the same thing."""
    assert read_settings_json(FakeCursor(), "alpha", "") == {}


def test_one_key_is_written_without_disturbing_the_others() -> None:
    """The column carries every knob, so a write is a merge and not a replace."""
    cursor = FakeCursor(objects={("alpha", ""): {"summarizing": {"mode": "off"}}})
    write_settings_json(cursor, "alpha", "", "indexing", {"mode": "auto"})
    assert read_settings_json(cursor, "alpha", "") == {
        "summarizing": {"mode": "off"},
        "indexing": {"mode": "auto"},
    }


def test_clearing_a_key_sends_the_question_back_up() -> None:
    """Removing it is what inheriting means; an empty object still answers."""
    cursor = FakeCursor(objects={("alpha", ""): {"indexing": {"mode": "auto"}}})
    write_settings_json(cursor, "alpha", "", "indexing", None)
    assert read_settings_json(cursor, "alpha", "") == {}
