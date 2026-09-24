-- Summary tasks become nodes of three kinds, claimed files, directories
-- deepest first, then entities; a node's summary can be embedded as a chunk.

-- +goose Up

ALTER TABLE summary_tasks
ADD COLUMN IF NOT EXISTS node_id VARCHAR(255),
ADD COLUMN IF NOT EXISTS kind VARCHAR(16) NOT NULL DEFAULT 'file',
ADD COLUMN IF NOT EXISTS rank INTEGER NOT NULL DEFAULT 0;

UPDATE summary_tasks SET node_id = LEFT(file_path, 255)
WHERE node_id IS NULL;

ALTER TABLE summary_tasks ALTER COLUMN node_id SET NOT NULL;

ALTER TABLE summary_tasks
DROP CONSTRAINT IF EXISTS summary_tasks_job_id_file_path_key;

ALTER TABLE summary_tasks
ADD CONSTRAINT summary_tasks_job_id_node_id_key UNIQUE (job_id, node_id);

DROP INDEX IF EXISTS idx_summary_tasks_claim;

CREATE INDEX idx_summary_tasks_claim
ON summary_tasks (job_id, rank, id)
WHERE state = 'pending';

-- `source` rows are cut from a file; a `summary` row is one node's summary.
ALTER TABLE code_embeddings
ADD COLUMN IF NOT EXISTS kind VARCHAR(16) NOT NULL DEFAULT 'source';

ALTER TABLE code_embeddings
DROP CONSTRAINT IF EXISTS code_embeddings_project_node_id_chunk_index_key;

ALTER TABLE code_embeddings
ADD CONSTRAINT code_embeddings_project_node_id_kind_chunk_index_key
UNIQUE (project, node_id, kind, chunk_index);

-- +goose Down

DELETE FROM code_embeddings
WHERE kind <> 'source';

ALTER TABLE code_embeddings
DROP CONSTRAINT IF EXISTS code_embeddings_project_node_id_kind_chunk_index_key;

ALTER TABLE code_embeddings
ADD CONSTRAINT code_embeddings_project_node_id_chunk_index_key
UNIQUE (project, node_id, chunk_index);

ALTER TABLE code_embeddings DROP COLUMN IF EXISTS kind;

DELETE FROM summary_tasks
WHERE kind <> 'file';

DROP INDEX IF EXISTS idx_summary_tasks_claim;

CREATE INDEX idx_summary_tasks_claim
ON summary_tasks (job_id, id)
WHERE state = 'pending';

ALTER TABLE summary_tasks
DROP CONSTRAINT IF EXISTS summary_tasks_job_id_node_id_key;

ALTER TABLE summary_tasks
ADD CONSTRAINT summary_tasks_job_id_file_path_key UNIQUE (job_id, file_path);

ALTER TABLE summary_tasks
DROP COLUMN IF EXISTS rank,
DROP COLUMN IF EXISTS kind,
DROP COLUMN IF EXISTS node_id;

DELETE FROM graph_nodes
WHERE type = 'directory';
