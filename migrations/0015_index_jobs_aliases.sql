-- Which directories a run walked, so a project of named directories can say
-- what happened to each of them.
--
-- A run over one slice already existed; the row could not name it, so every
-- directory of a project reported the same last run and the same failure.
-- NULL means the whole project, which is what every run before this was.

-- +goose Up

ALTER TABLE index_jobs ADD COLUMN IF NOT EXISTS aliases TEXT [];

-- +goose Down

ALTER TABLE index_jobs DROP COLUMN IF EXISTS aliases;
