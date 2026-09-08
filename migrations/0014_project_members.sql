-- Which projects an organization holds, without any of them moving into it.
--
-- A project of named directories is one graph assembled from slices, and
-- `project_sources.root_path` is UNIQUE because one host directory belongs to
-- one project. That is the right rule for a monorepo cut into pieces and the
-- wrong one for a thematic container: a project belongs to as many of those as
-- it is relevant to, and folding its tree into one of them would take it away
-- from the others, dissolve its name, and index it again per container.
--
-- An organization references projects instead. Each member keeps its own name,
-- its own `/mcp/<name>` address and its own graph, indexed once; the
-- organization is the set, and a search over it is a search over its members.
-- Nothing is duplicated and nothing moves.
--
-- Membership is not ownership, so it is not a foreign key a drop may follow
-- quietly: a project that belongs to an organization refuses to be dropped or
-- moved until it is taken out of it. The cascades below are for the case the
-- guards cannot reach - an organization dropped as a whole - rather than the
-- ordinary path.

-- +goose Up

CREATE TABLE IF NOT EXISTS project_members (
    organization VARCHAR(64) NOT NULL REFERENCES projects (
        name
    ) ON DELETE CASCADE,
    project VARCHAR(64) NOT NULL REFERENCES projects (
        name
    ) ON DELETE CASCADE,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (organization, project),
    -- An organization holding itself would answer a search about it with
    -- itself, forever.
    CONSTRAINT project_members_not_itself CHECK (organization <> project)
);

-- The reverse question - which organizations is this project part of - is
-- asked on every project page, and the primary key answers only the forward
-- one.
CREATE INDEX IF NOT EXISTS idx_project_members_project ON project_members (
    project
);

-- +goose Down

DROP INDEX IF EXISTS idx_project_members_project;
DROP TABLE IF EXISTS project_members;
