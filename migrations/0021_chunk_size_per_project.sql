-- A chunk records the size it was cut to.
--
-- The window was one number for the whole stack, read from the environment.
-- It is a property of the tree rather than of the process: a repository of
-- terse configuration files and one of long source files are not well served
-- by the same window, and the setting belongs beside the others a project
-- already has.
--
-- Storing it on the row is what makes changing it take effect. A file is
-- re-embedded when its hash moves or when the model changes; without this
-- column, cutting the same file differently would leave the old chunks in
-- place, because nothing about the file itself had changed.

-- +goose Up

ALTER TABLE code_embeddings
ADD COLUMN IF NOT EXISTS chunk_chars INTEGER NOT NULL DEFAULT 0;

-- Rows written before this migration were cut to the built-in 1500. Saying
-- so rather than leaving 0 keeps them from being re-embedded for no reason.
UPDATE code_embeddings SET chunk_chars = 1500
WHERE chunk_chars = 0;

-- +goose Down

ALTER TABLE code_embeddings
DROP COLUMN IF EXISTS chunk_chars;
