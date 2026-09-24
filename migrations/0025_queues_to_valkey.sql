-- The work queues move to Valkey: embedding, summarizing and the index lock.
--
-- Nothing is carried over. Every task is work the graph still owes, and the
-- sweeps queue it again; dropping the tables returns their space at once.
-- index_jobs stays as the history of runs, and its one-running index goes:
-- the lock in Valkey expires with the process that held it.

-- +goose Up

DROP TABLE IF EXISTS summary_tasks;

DROP TABLE IF EXISTS summary_jobs;

DROP TABLE IF EXISTS embed_tasks;

DROP INDEX IF EXISTS idx_index_jobs_one_running;

-- +goose Down

CREATE UNIQUE INDEX IF NOT EXISTS idx_index_jobs_one_running
ON index_jobs (project) WHERE status = 'running';

CREATE TABLE IF NOT EXISTS embed_tasks (
    id SERIAL PRIMARY KEY,
    project VARCHAR(64) NOT NULL REFERENCES projects (
        name
    ) ON DELETE CASCADE ON UPDATE CASCADE,
    file_path TEXT NOT NULL,
    content_hash VARCHAR(32) NOT NULL,
    status VARCHAR(20) NOT NULL DEFAULT 'pending',
    attempts INTEGER NOT NULL DEFAULT 0,
    lease_until TIMESTAMP WITH TIME ZONE,
    error TEXT,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (project, file_path)
);

CREATE INDEX IF NOT EXISTS idx_embed_tasks_pending ON embed_tasks (status, id);

CREATE TABLE IF NOT EXISTS summary_jobs (
    id BIGSERIAL PRIMARY KEY,
    project VARCHAR(64) NOT NULL REFERENCES projects (
        name
    ) ON DELETE CASCADE ON UPDATE CASCADE,
    status VARCHAR(20) NOT NULL DEFAULT 'running',
    input_chars INTEGER NOT NULL,
    refresh BOOLEAN NOT NULL DEFAULT FALSE,
    lease_seconds INTEGER NOT NULL DEFAULT 300,
    model TEXT,
    metadata JSONB DEFAULT '{}'::JSONB,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    finished_at TIMESTAMP WITH TIME ZONE
);

CREATE TABLE IF NOT EXISTS summary_tasks (
    id BIGSERIAL PRIMARY KEY,
    job_id BIGINT NOT NULL REFERENCES summary_jobs (id) ON DELETE CASCADE,
    file_path TEXT NOT NULL,
    content_hash VARCHAR(64) NOT NULL,
    state VARCHAR(20) NOT NULL DEFAULT 'pending',
    attempts INTEGER NOT NULL DEFAULT 0,
    lease_token UUID,
    worker_id VARCHAR(64),
    leased_at TIMESTAMP WITH TIME ZONE,
    lease_expires_at TIMESTAMP WITH TIME ZONE,
    origin VARCHAR(16),
    note TEXT,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    node_id VARCHAR(255) NOT NULL,
    kind VARCHAR(16) NOT NULL DEFAULT 'file',
    rank INTEGER NOT NULL DEFAULT 0,
    CONSTRAINT summary_tasks_job_id_node_id_key UNIQUE (job_id, node_id)
);

CREATE INDEX IF NOT EXISTS idx_summary_tasks_claim
ON summary_tasks (job_id, rank, id) WHERE state = 'pending';

CREATE INDEX IF NOT EXISTS idx_summary_tasks_expiry
ON summary_tasks (lease_expires_at) WHERE state = 'leased';

CREATE INDEX IF NOT EXISTS idx_summary_tasks_job_state
ON summary_tasks (job_id, state);

CREATE INDEX IF NOT EXISTS idx_summary_jobs_project_status
ON summary_jobs (project, status);

CREATE UNIQUE INDEX IF NOT EXISTS idx_summary_jobs_one_running
ON summary_jobs (project) WHERE status = 'running';
