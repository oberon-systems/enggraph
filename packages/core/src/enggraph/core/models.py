"""The tables of the database, one SQLModel class each.

The schema itself is the DDL in `migrations/versions`: these classes name its
rows for code and create nothing. `nodes` and `edges` have a unique index and
no primary key, so the columns of that index stand in as the key here.

No class is tied to another by a constraint or a mapped attribute. Tables are
tied by plain value columns, and `storage.PROJECT_COLUMNS` lists the ones
naming a project.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR
from sqlmodel import ARRAY, TIMESTAMP, Column, Field, SQLModel, Text

EMBEDDING_DIMENSIONS = 768
STAMP = TIMESTAMP(timezone=True)


class Project(SQLModel, table=True):
    """One indexed codebase, or a built-in holder of records."""

    __tablename__ = "projects"

    name: str = Field(primary_key=True, max_length=64)
    root_path: str = Field(sa_type=Text)
    indexed_at: datetime | None = Field(default=None, sa_type=STAMP)
    type: str = Field(default="codebase", max_length=50)
    description: str | None = Field(default=None, sa_type=Text)
    formats: list[str] = Field(default_factory=list, sa_type=ARRAY(Text))
    formats_at: datetime | None = Field(default=None, sa_type=STAMP)


class IndexJob(SQLModel, table=True):
    """One index run, with its counts once it has ended."""

    __tablename__ = "index_jobs"

    id: int | None = Field(default=None, primary_key=True)
    project: str = Field(max_length=64)
    status: str = Field(default="running", max_length=20)
    fresh: bool = False
    project_type: str | None = Field(default=None, max_length=50)
    files: int | None = None
    with_node: int | None = None
    entities: int | None = None
    edges: int | None = None
    pruned: int | None = None
    failures: int | None = None
    gaps: int | None = None
    error: str | None = Field(default=None, sa_type=Text)
    started_at: datetime | None = Field(default=None, sa_type=STAMP)
    finished_at: datetime | None = Field(default=None, sa_type=STAMP)


class Node(SQLModel, table=True):
    """A node of the graph: a file, a symbol, a directory or a record."""

    __tablename__ = "nodes"

    project: str = Field(primary_key=True, max_length=64)
    id: str = Field(primary_key=True, max_length=255)
    name: str = Field(max_length=255)
    type: str = Field(max_length=50)
    file_path: str | None = Field(default=None, sa_type=Text)
    content: str | None = Field(default=None, sa_type=Text)
    summary: str | None = Field(default=None, sa_type=Text)
    meta: dict[str, Any] | None = Field(
        default_factory=dict,
        # `metadata` is a name SQLModel keeps for itself.
        sa_column=Column("metadata", JSONB),
    )
    created_at: datetime | None = Field(default=None, sa_type=STAMP)


class Edge(SQLModel, table=True):
    """A relation between two nodes of one project."""

    __tablename__ = "edges"

    project: str = Field(primary_key=True, max_length=64)
    source_id: str = Field(primary_key=True, max_length=255)
    target_id: str = Field(primary_key=True, max_length=255)
    relation_type: str = Field(primary_key=True, max_length=50)
    meta: dict[str, Any] | None = Field(
        default_factory=dict,
        # `metadata` is a name SQLModel keeps for itself.
        sa_column=Column("metadata", JSONB),
    )


class Chunk(SQLModel, table=True):
    """A line range of a node: its words and its vector, never its text."""

    __tablename__ = "chunks"

    project: str = Field(primary_key=True, max_length=64)
    node_id: str = Field(primary_key=True, max_length=255)
    kind: str = Field(default="source", primary_key=True, max_length=16)
    chunk_index: int = Field(primary_key=True)
    start_line: int
    end_line: int
    words: str = Field(sa_type=TSVECTOR)
    content_hash: str = Field(max_length=32)
    model: str = Field(max_length=128)
    chunk_chars: int = 0
    chunker: int = 1
    embedding: list[float] | None = Field(
        default=None, sa_type=Vector(EMBEDDING_DIMENSIONS)
    )
    updated_at: datetime | None = Field(default=None, sa_type=STAMP)


class IndexedFile(SQLModel, table=True):
    """The hash a file had when it was last indexed."""

    __tablename__ = "indexed_files"

    project: str = Field(primary_key=True, max_length=64)
    file_path: str = Field(primary_key=True, sa_type=Text)
    hash: str = Field(max_length=32)
    updated_at: datetime | None = Field(default=None, sa_type=STAMP)


class CachedSummary(SQLModel, table=True):
    """A model's summary, kept under the hash of the text it was asked about."""

    __tablename__ = "cached_summaries"

    project: str = Field(primary_key=True, max_length=64)
    content_hash: str = Field(primary_key=True, max_length=64)
    summary: str = Field(sa_type=Text)
    updated_at: datetime | None = Field(default=None, sa_type=STAMP)


class Settings(SQLModel, table=True):
    """What one project overrides; `_settings` holds the global level."""

    __tablename__ = "settings"

    project: str = Field(primary_key=True, max_length=64)
    ignore_patterns: str | None = Field(default=None, sa_type=Text)
    settings: dict[str, Any] = Field(default_factory=dict, sa_type=JSONB)
    updated_at: datetime | None = Field(default=None, sa_type=STAMP)


class OrgMember(SQLModel, table=True):
    """A project that belongs to an organization."""

    __tablename__ = "org_members"

    organization: str = Field(primary_key=True, max_length=64)
    project: str = Field(primary_key=True, max_length=64)
    created_at: datetime | None = Field(default=None, sa_type=STAMP)
    owned: bool = False


class AgentSkill(SQLModel, table=True):
    """A skill handed to agents, built in or imported for one project."""

    __tablename__ = "agent_skills"

    id: int | None = Field(default=None, primary_key=True)
    project: str | None = Field(default=None, max_length=64)
    name: str = Field(max_length=64)
    content: str = Field(sa_type=Text)
    sha256: str = Field(max_length=64)
    source: str = Field(max_length=16)
    updated_at: datetime | None = Field(default=None, sa_type=STAMP)


class SkillSwitch(SQLModel, table=True):
    """Whether one project has one skill switched on."""

    __tablename__ = "skill_switches"

    project: str = Field(primary_key=True, max_length=64)
    skill_id: int = Field(primary_key=True)
    enabled: bool


class ProvidedName(SQLModel, table=True):
    """A name a project provides to others: an image, a package, a host."""

    __tablename__ = "provided_names"

    project: str = Field(primary_key=True, max_length=64)
    kind: str = Field(primary_key=True, max_length=16)
    name: str = Field(primary_key=True, max_length=255)
    node_id: str = Field(default="./", max_length=255)
    origin: str = Field(default="auto", max_length=16)
    created_at: datetime | None = Field(default=None, sa_type=STAMP)


class TakenName(SQLModel, table=True):
    """A name a project takes from wherever it is provided."""

    __tablename__ = "taken_names"

    project: str = Field(primary_key=True, max_length=64)
    kind: str = Field(primary_key=True, max_length=16)
    name: str = Field(primary_key=True, max_length=255)
    source_id: str = Field(primary_key=True, max_length=255)
    relation_type: str = Field(primary_key=True, max_length=50)


class DeclaredLink(SQLModel, table=True):
    """A relation between two projects that somebody wrote down."""

    __tablename__ = "declared_links"

    id: int | None = Field(default=None, primary_key=True)
    source_project: str = Field(max_length=64)
    source_id: str = Field(default="./", max_length=255)
    target_project: str = Field(max_length=64)
    target_id: str = Field(default="./", max_length=255)
    relation_type: str = Field(max_length=50)
    note: str | None = Field(default=None, sa_type=Text)
    created_at: datetime | None = Field(default=None, sa_type=STAMP)


class RecordLink(SQLModel, table=True):
    """The code a memory, a plan or a suggestion is about."""

    __tablename__ = "record_links"

    record_project: str = Field(primary_key=True, max_length=64)
    record_id: str = Field(primary_key=True, max_length=255)
    project: str = Field(primary_key=True, max_length=64)
    node_id: str = Field(primary_key=True, max_length=255)
    relation: str = Field(default="about", max_length=50)
    created_at: datetime | None = Field(default=None, sa_type=STAMP)
