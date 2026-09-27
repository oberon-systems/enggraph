-- The keep list goes, and the ignore list is all that is left of the selection.
--
-- What is indexed is every file some producer reads, and the formats a run
-- found are recorded on the project instead of being written down by hand.
-- The ignore documents of the global default, the organizations and the
-- project are summed rather than the nearest one winning.

-- +goose Up

ALTER TABLE project_settings DROP COLUMN IF EXISTS ctxkeep;

ALTER TABLE project_settings RENAME COLUMN ctxignore TO ignore_patterns;

ALTER TABLE projects DROP COLUMN IF EXISTS keep_source;

ALTER TABLE projects DROP COLUMN IF EXISTS ignore_source;

ALTER TABLE projects
ADD COLUMN IF NOT EXISTS formats TEXT [] NOT NULL DEFAULT '{}';

ALTER TABLE projects ADD COLUMN IF NOT EXISTS formats_at TIMESTAMP WITH TIME ZONE;

-- Agent state rather than code: every generated ignore document named these,
-- and with no per-project document generated any more the default carries them.
UPDATE project_settings
SET ignore_patterns = E'.claude/\n.gemini/\n', updated_at = CURRENT_TIMESTAMP
WHERE project = '_settings' AND COALESCE(TRIM(ignore_patterns), '') = '';

-- +goose Down

ALTER TABLE projects DROP COLUMN IF EXISTS formats_at;

ALTER TABLE projects DROP COLUMN IF EXISTS formats;

ALTER TABLE projects ADD COLUMN IF NOT EXISTS ignore_source VARCHAR(16);

ALTER TABLE projects ADD COLUMN IF NOT EXISTS keep_source VARCHAR(16);

ALTER TABLE project_settings RENAME COLUMN ignore_patterns TO ctxignore;

ALTER TABLE project_settings ADD COLUMN IF NOT EXISTS ctxkeep TEXT;
