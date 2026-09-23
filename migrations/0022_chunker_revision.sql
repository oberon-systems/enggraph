-- A chunk records the revision of the code that cut it, so a change to how
-- files are cut or what is embedded with them re-embeds every file.

-- +goose Up

ALTER TABLE code_embeddings
ADD COLUMN IF NOT EXISTS chunker INTEGER NOT NULL DEFAULT 1;

-- +goose Down

ALTER TABLE code_embeddings
DROP COLUMN IF EXISTS chunker;
