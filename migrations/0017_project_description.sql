-- What a project is for, in a sentence.
--
-- `graph_nodes.summary` describes a file or an entity inside one, and there is
-- no node standing for the whole tree, so a sentence about the project itself
-- has had nowhere to live. That was tolerable while a project was addressed by
-- name and read one at a time. An organization is a set, and it is read as a
-- set: an agent connected to one is handed its members and has to pick which
-- of them to read. A list of names does not let it pick.
--
-- Written by hand from the dashboard rather than derived from the tree. A
-- README is the tree explaining itself to whoever opens it, at whatever length
-- it likes; this is the project explaining itself to whoever is choosing
-- between it and nineteen others, and the two are not the same sentence.
--
-- Nullable, and nothing writes it during an index run: `ensure_project` and
-- `register_project` name their columns and neither names this one, so a
-- re-index and a re-install leave the description alone.

-- +goose Up

ALTER TABLE projects ADD COLUMN IF NOT EXISTS description TEXT;

-- +goose Down

ALTER TABLE projects DROP COLUMN IF EXISTS description;
