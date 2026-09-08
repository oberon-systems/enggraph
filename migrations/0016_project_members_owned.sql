-- The difference between a project added to an organization and one moved
-- into it.
--
-- Adding is a reference: the project is listed by that organization and stays
-- a project of its own, in the projects list, in as many organizations as are
-- relevant to it. Moving is where it lives: the organization holds it, and it
-- is listed there rather than beside the projects nothing holds.
--
-- Neither is physical. `owned` decides which listing a project appears in and
-- nothing else - its tree, its mount, its node ids and its graph are the same
-- rows before and after, and no index run follows either.

-- +goose Up

ALTER TABLE project_members
ADD COLUMN IF NOT EXISTS owned BOOLEAN NOT NULL DEFAULT FALSE;

-- One organization holds a project; every other listing of it is a reference.
CREATE UNIQUE INDEX IF NOT EXISTS idx_project_members_one_owner
ON project_members (project) WHERE owned;

-- +goose Down

DROP INDEX IF EXISTS idx_project_members_one_owner;
ALTER TABLE project_members DROP COLUMN IF EXISTS owned;
