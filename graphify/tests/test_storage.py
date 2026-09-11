"""How a project row is registered, and what it may be renamed or joined to.

None of it needs a database: the statements are few and shaped by hand, so a
cursor holding two dictionaries answers them and the behaviour that matters
can be pinned - which type a re-index writes, which pairing of a name and a
path is refused, and which organization holds what.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from enggraph.config import BUILTIN_PROJECT_TYPES
from enggraph.storage import (
    add_member,
    clear_settings,
    ensure_project,
    has_settings,
    list_files_without_llm_summary,
    list_members,
    list_memberships,
    list_owned,
    owner_of,
    project_root,
    project_rows,
    prune_missing_files,
    read_settings,
    read_settings_json,
    register_project,
    rename_project,
    set_memberships,
    set_selection_origin,
    write_settings,
    write_settings_json,
)


class FakeCursor:
    """Answer the statements `enggraph.storage` sends, out of two dictionaries.

    A queue would not do any more: registering a project now reads the
    projects row and the owner of that path, in an order the tests have no
    business depending on. Anything not recognised falls back to `rows`, which
    is what the summary-pass tests still use.
    """

    def __init__(
        self,
        rows: list[tuple[Any, ...] | None] | None = None,
        projects: dict[str, tuple[str, str]] | None = None,
        settings: dict[str, tuple[str | None, str | None]] | None = None,
        objects: dict[str, dict] | None = None,
        records: list[tuple[str, str, str]] | None = None,
        nodes: list[tuple[str, str]] | None = None,
        members: list[tuple[str, str]] | None = None,
        descriptions: dict[str, str] | None = None,
        running: set[str] | None = None,
    ) -> None:
        """Seed the database this cursor pretends to be."""
        self.rows = list(rows or [])
        # name -> (root_path, type)
        self.projects = dict(projects or {})
        # project -> (ctxkeep, ctxignore)
        self.settings = dict(settings or {})
        # project -> the settings JSONB of that level
        self.objects = {key: dict(value) for key, value in (objects or {}).items()}
        # (built-in project, node id, what it is about), for the records an
        # agent wrote: plans, memories and suggestions
        self.records = list(records or [])
        # (project, node id), what an index run built rather than what an agent
        # wrote
        self.nodes = list(nodes or [])
        # (organization, project) or (organization, project, owned): the
        # projects an organization holds, and whether it holds them by
        # reference or as where they live
        self.members = list(members or [])
        # name -> the sentence written about that project, for the projects
        # somebody wrote one for
        self.descriptions = dict(descriptions or {})
        # the projects with an index run open
        self.running = set(running or set())
        # project -> (keep_source, ignore_source)
        self.origins: dict[str, tuple[str, str]] = {}
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

    def memberships(self) -> list[tuple[str, str, bool]]:
        """Read the members with the kind of membership each one is.

        A fixture writes a pair when the difference does not matter to it,
        which is a reference: the project stays listed as its own.
        """
        return [
            (one[0], one[1], bool(one[2]) if len(one) > 2 else False)
            for one in self.members
        ]

    def _run(self, text: str, params: tuple[Any, ...]) -> list[tuple[Any, ...]] | None:
        if text.startswith("SELECT name, description, root_path, indexed_at"):
            return [
                (name, self.descriptions.get(name), self.projects[name][0], None, None)
                for name in params[0]
                if name in self.projects
            ]
        if text.startswith("SELECT root_path FROM projects WHERE name"):
            stored = self.projects.get(params[0])
            return [(stored[0],)] if stored else []
        if text.startswith("SELECT type FROM projects WHERE name"):
            stored = self.projects.get(params[0])
            return [(stored[1],)] if stored else []
        if text.startswith("SELECT 1 FROM projects WHERE name"):
            return [(1,)] if params[0] in self.projects else []
        if text.startswith("SELECT name FROM projects WHERE root_path"):
            # `path_owner` asks with the path alone; the identity check asks
            # for anyone but the project it is about.
            return [
                (name,)
                for name, (root, _) in self.projects.items()
                if root == params[0] and (len(params) < 2 or name != params[1])
            ]
        if text.startswith("SELECT ctxkeep, ctxignore FROM project_settings"):
            stored = self.settings.get(params[0])
            return [stored] if stored else []
        if text.startswith("SELECT 1 FROM project_settings WHERE project"):
            return [(1,)] if params[0] in self.settings else []
        if text.startswith("SELECT settings FROM project_settings"):
            stored = self.objects.get(params[0])
            return [(stored,)] if stored is not None else []
        if text.startswith("INSERT INTO project_settings (project, settings)"):
            merged = dict(self.objects.get(params[0], {}))
            merged.update(json.loads(params[1]))
            self.objects[params[0]] = merged
            return []
        if text.startswith("UPDATE project_settings SET settings = settings -"):
            stored = self.objects.get(params[1])
            if stored is not None:
                stored.pop(params[0], None)
            return []
        if text.startswith("INSERT INTO project_settings (project, ctxkeep"):
            self.settings[params[0]] = (params[1], params[2])
            return []
        if text.startswith("DELETE FROM project_settings"):
            self.settings.pop(params[0], None)
            return []
        if text.startswith("UPDATE projects SET keep_source"):
            self.origins[params[2]] = (params[0], params[1])
            return []
        if text.startswith("SELECT project FROM project_members"):
            return [
                (project,)
                for organization, project, owned in self.memberships()
                if organization == params[0] and (owned or "AND owned" not in text)
            ]
        if text.startswith("SELECT organization FROM project_members"):
            return [
                (organization,)
                for organization, project, owned in self.memberships()
                if project == params[0] and (owned or "AND owned" not in text)
            ]
        if text.startswith("INSERT INTO project_members"):
            if (params[0], params[1]) not in [one[:2] for one in self.members]:
                self.members.append((params[0], params[1], bool(params[2])))
            return []
        if text.startswith("DELETE FROM project_members"):
            self.members = [
                entry for entry in self.members if entry[:2] != (params[0], params[1])
            ]
            return []
        if text.startswith("SELECT project FROM index_jobs"):
            return [(name,) for name in (params[0], params[1]) if name in self.running]
        if text.startswith("SELECT 1 FROM index_jobs"):
            return [(1,)] if params[0] in self.running else []
        if text.startswith("UPDATE graph_edges SET project") or text.startswith(
            "UPDATE code_embeddings SET project"
        ):
            return []
        if text.startswith("UPDATE index_jobs SET project"):
            self.running = {
                params[0] if name == params[1] else name for name in self.running
            }
            return []
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
            self.nodes = [
                entry
                for entry in self.nodes
                if entry[0] != params[0] or entry[1] in params[1]
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
            self.settings.pop(params[0], None)
            return []
        if text.startswith("INSERT INTO projects"):
            name, root, project_type, default, _ = params
            stored = self.projects.get(name)
            if stored is None:
                self.projects[name] = (root, project_type or default)
            else:
                self.projects[name] = (stored[0], project_type or stored[1])
            return []
        if text.startswith("UPDATE projects SET name"):
            # Every foreign key onto projects (name) is ON UPDATE CASCADE
            # (migration 0018), so the rows that name it follow it here too.
            new_name, old_name = params[0], params[1]
            self.projects[new_name] = self.projects.pop(old_name)
            self.nodes = [
                (new_name if project == old_name else project, node)
                for project, node in self.nodes
            ]
            self.members = [
                tuple(new_name if one == old_name else one for one in entry[:2])
                + tuple(entry[2:])
                for entry in self.members
            ]
            self.settings = {
                (new_name if project == old_name else project): value
                for project, value in self.settings.items()
            }
            self.objects = {
                (new_name if project == old_name else project): value
                for project, value in self.objects.items()
            }
            return []
        # The rename gives a project registered before it read anything the
        # synthetic root built from the name it has now.
        if text.startswith("UPDATE projects SET root_path"):
            stored = self.projects.get(params[1])
            if stored is not None and stored[0] == params[2]:
                self.projects[params[1]] = (params[0], stored[1])
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
    )
    with pytest.raises(RuntimeError, match="already indexed from"):
        ensure_project(cursor, "alpha", "/src/alpha")


def test_refuses_a_path_another_project_reads() -> None:
    """One directory belongs to one project, whatever it is called there."""
    cursor = FakeCursor(
        projects={"mono": ("/mono/configs", "codebase")},
    )
    with pytest.raises(RuntimeError, match="already indexed as 'mono'"):
        ensure_project(cursor, "configs", "/mono/configs")


def test_a_project_with_no_tree_is_not_indexed() -> None:
    """One registered ahead of a tree must not adopt the path it was given."""
    cursor = FakeCursor(projects={"acme": ("registered://acme", "organization")})
    with pytest.raises(RuntimeError, match="reads no directory yet"):
        ensure_project(cursor, "acme", "registered://acme")


def test_a_project_can_be_registered_without_a_tree() -> None:
    """An organization is a row, an address and nothing to walk."""
    cursor = FakeCursor()
    register_project(cursor, "acme", "registered://acme", "organization")
    assert project_root(cursor, "acme") == "registered://acme"


def test_pruning_takes_the_files_the_run_did_not_find() -> None:
    """A re-index never visits a file that was renamed or deleted."""
    cursor = FakeCursor(
        nodes=[
            ("mono", "configs/nginx.conf"),
            ("mono", "agents/main.py"),
        ],
    )
    prune_missing_files(cursor, "mono", ["configs/nginx.conf"])
    assert cursor.nodes == [("mono", "configs/nginx.conf")]


def test_a_rename_keeps_everything_the_project_has() -> None:
    """Rows are re-keyed where they stand: no tree is read again."""
    cursor = FakeCursor(
        projects={"old-name": ("/src/thing", "codebase")},
        nodes=[("old-name", "README.md")],
    )
    answer = rename_project(cursor, "old-name", "thing")
    assert answer["project"] == "thing"
    assert answer["was"] == "old-name"
    assert "old-name" not in cursor.projects
    assert answer["root_path"] == "/src/thing"
    assert cursor.nodes == [("thing", "README.md")]
    # It is a tree, so the column keeps naming it rather than the new name.
    assert cursor.projects["thing"][0] == "/src/thing"


def test_a_renamed_container_carries_its_synthetic_root() -> None:
    """The root of a project that is no tree is built from its name."""
    cursor = FakeCursor(
        projects={"acme": ("registered://acme", "organization")},
    )
    rename_project(cursor, "acme", "acme-group")
    assert cursor.projects["acme-group"][0] == "registered://acme-group"


def test_a_rename_takes_the_records_written_about_the_old_name() -> None:
    """A plan about a name that stopped existing is a plan about nothing."""
    cursor = FakeCursor(
        projects={"gamma": ("/acme/gamma", "codebase")},
        records=[
            ("_plans", "rpm-pipeline", "gamma"),
            ("_memory", "gamma/commit-style", "gamma"),
        ],
    )
    rename_project(cursor, "gamma", "gamma-builder")
    assert cursor.records == [
        ("_plans", "rpm-pipeline", "gamma-builder"),
        ("_memory", "gamma-builder/commit-style", "gamma-builder"),
    ]


def test_a_rename_onto_a_taken_name_is_refused() -> None:
    """A name belongs to one project, and the graph is keyed on it."""
    cursor = FakeCursor(
        projects={
            "gamma": ("/acme/gamma", "codebase"),
            "delta": ("/acme/delta", "codebase"),
        },
    )
    with pytest.raises(RuntimeError, match="already exists"):
        rename_project(cursor, "gamma", "delta")


def test_a_rename_is_cleaned_by_the_rule_that_names_a_project() -> None:
    """The name travels in /mcp/<name>, so it is held to what a URL carries."""
    cursor = FakeCursor(projects={"gamma": ("/acme/gamma", "codebase")})
    assert (
        rename_project(cursor, "gamma", "Gamma Builder")["project"] == "gamma-builder"
    )


def test_a_rename_into_the_builtin_prefix_is_refused() -> None:
    """`_` belongs to the projects holding an agent's records."""
    cursor = FakeCursor(projects={"gamma": ("/acme/gamma", "codebase")})
    with pytest.raises(RuntimeError, match="reserved"):
        rename_project(cursor, "gamma", "_memory")


def test_a_builtin_project_is_not_renamed() -> None:
    """Its name is what every tool addresses it by, not a choice."""
    cursor = FakeCursor(projects={"_memory": ("memory://agent", "memory")})
    with pytest.raises(RuntimeError, match="holds agent memory"):
        rename_project(cursor, "_memory", "notes")


def test_a_rename_waits_for_a_run_that_is_open() -> None:
    """It writes rows under the name being changed."""
    cursor = FakeCursor(
        projects={"gamma": ("/acme/gamma", "codebase")},
        running={"gamma"},
    )
    with pytest.raises(RuntimeError, match="being indexed"):
        rename_project(cursor, "gamma", "gamma-builder")


def test_a_rename_onto_a_name_a_record_already_uses_is_refused() -> None:
    """A record outlives the project it is about, so the id may be taken."""
    cursor = FakeCursor(
        projects={"gamma": ("/acme/gamma", "codebase")},
        records=[
            ("_memory", "gamma/commit-style", "gamma"),
            ("_memory", "builder/commit-style", "builder"),
        ],
    )
    with pytest.raises(RuntimeError, match="already exists under"):
        rename_project(cursor, "gamma", "builder")


def test_a_member_row_is_read_for_every_member_at_once() -> None:
    """One statement: an organization asks this about all its members."""
    cursor = FakeCursor(
        projects={
            "gamma": ("/acme/gamma", "codebase"),
            "delta": ("/acme/delta", "codebase"),
        },
        descriptions={"gamma": "the package builder"},
    )
    rows = project_rows(cursor, ["gamma", "delta"])
    assert rows["gamma"]["description"] == "the package builder"
    assert rows["gamma"]["root_path"] == "/acme/gamma"
    assert rows["delta"]["description"] is None


def test_describing_nothing_asks_nothing() -> None:
    """An organization holding no project sends no statement at all."""
    cursor = FakeCursor()
    assert project_rows(cursor, []) == {}
    assert cursor.calls == []


def test_an_organization_holds_a_project_without_moving_it() -> None:
    """Membership is a reference: the member keeps its tree and its name."""
    cursor = FakeCursor(
        projects={
            "acme": ("registered://acme", "organization"),
            "gamma": ("/acme/gamma", "codebase"),
        },
    )
    add_member(cursor, "acme", "gamma")
    assert list_members(cursor, "acme") == ["gamma"]
    assert list_memberships(cursor, "gamma") == ["acme"]
    assert project_root(cursor, "gamma") == "/acme/gamma"


def test_a_project_belongs_to_several_organizations() -> None:
    """Which is the whole reason membership is not a move."""
    cursor = FakeCursor(
        projects={
            "acme": ("registered://acme", "organization"),
            "infra": ("registered://infra", "organization"),
            "gamma": ("/acme/gamma", "codebase"),
        },
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


def test_moving_a_project_into_an_organization_is_rows_only() -> None:
    """A project belonging to none joins the one it is moved into."""
    cursor = FakeCursor(
        projects={
            "acme": ("registered://acme", "organization"),
            "gamma": ("/acme/gamma", "codebase"),
        },
    )
    set_memberships(cursor, "gamma", ["acme"])
    assert list_memberships(cursor, "gamma") == ["acme"]
    assert project_root(cursor, "gamma") == "/acme/gamma"


def test_a_project_already_held_is_not_moved_out_by_a_move_in() -> None:
    """Leaving an organization is its own decision, taken by taking it out."""
    cursor = FakeCursor(
        projects={
            "acme": ("registered://acme", "organization"),
            "infra": ("registered://infra", "organization"),
            "gamma": ("/acme/gamma", "codebase"),
        },
        members=[("acme", "gamma")],
    )
    with pytest.raises(RuntimeError, match="take it out of that one first"):
        set_memberships(cursor, "gamma", ["infra"])
    assert list_memberships(cursor, "gamma") == ["acme"]


def test_a_project_moved_in_is_held_by_that_organization_alone() -> None:
    """Moving is where it lives; adding is a reference beside it."""
    cursor = FakeCursor(
        projects={
            "acme": ("registered://acme", "organization"),
            "infra": ("registered://infra", "organization"),
            "gamma": ("/acme/gamma", "codebase"),
        },
    )
    set_memberships(cursor, "gamma", ["acme"])
    assert owner_of(cursor, "gamma") == "acme"
    assert list_owned(cursor, "acme") == ["gamma"]
    with pytest.raises(RuntimeError, match="was moved into 'acme'"):
        add_member(cursor, "infra", "gamma")


def test_a_project_added_to_an_organization_is_owned_by_none() -> None:
    """It stays a project of its own, listed beside the others."""
    cursor = FakeCursor(
        projects={
            "acme": ("registered://acme", "organization"),
            "gamma": ("/acme/gamma", "codebase"),
        },
    )
    add_member(cursor, "acme", "gamma")
    assert owner_of(cursor, "gamma") is None
    assert list_owned(cursor, "acme") == []
    assert list_members(cursor, "acme") == ["gamma"]


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
    cursor = FakeCursor(settings={"alpha": (None, None)})
    assert read_settings(cursor, "alpha") == (None, None)
    assert read_settings(cursor, "unwritten") == (None, None)


def test_a_stored_pair_reads_back_verbatim() -> None:
    """Comments and blank lines are part of the document, not noise."""
    cursor = FakeCursor()
    keep = "# what this tree holds\n\n*.py\n"
    write_settings(cursor, "alpha", keep, "*.pem\n")
    assert read_settings(cursor, "alpha") == (keep, "*.pem\n")


def test_clearing_one_half_lets_the_level_above_answer() -> None:
    """Writing NULL is how a level stops speaking for a document."""
    cursor = FakeCursor()
    write_settings(cursor, "alpha", "*.py\n", "*.pem\n")
    write_settings(cursor, "alpha", None, "*.pem\n")
    assert read_settings(cursor, "alpha") == (None, "*.pem\n")


def test_a_project_holding_no_row_anywhere_is_reported_empty() -> None:
    """Onboarding writes the generated pair only into a project with none."""
    cursor = FakeCursor()
    assert has_settings(cursor, "alpha") is False
    write_settings(cursor, "alpha", "*.yaml\n", None)
    assert has_settings(cursor, "alpha") is True
    clear_settings(cursor, "alpha")
    assert has_settings(cursor, "alpha") is False


def test_the_run_records_where_it_read_the_selection() -> None:
    """The dashboard holds no mount, so the run that looked reports it."""
    cursor = FakeCursor()
    set_selection_origin(cursor, "mono", "file", "global")
    assert cursor.origins["mono"] == ("file", "global")


def test_a_level_saying_nothing_holds_an_empty_object() -> None:
    """A missing row and a row with no keys answer the same thing."""
    assert read_settings_json(FakeCursor(), "alpha") == {}


def test_one_key_is_written_without_disturbing_the_others() -> None:
    """The column carries every knob, so a write is a merge and not a replace."""
    cursor = FakeCursor(objects={"alpha": {"summarizing": {"mode": "off"}}})
    write_settings_json(cursor, "alpha", "indexing", {"mode": "auto"})
    assert read_settings_json(cursor, "alpha") == {
        "summarizing": {"mode": "off"},
        "indexing": {"mode": "auto"},
    }


def test_clearing_a_key_sends_the_question_back_up() -> None:
    """Removing it is what inheriting means; an empty object still answers."""
    cursor = FakeCursor(objects={"alpha": {"indexing": {"mode": "auto"}}})
    write_settings_json(cursor, "alpha", "indexing", None)
    assert read_settings_json(cursor, "alpha") == {}
