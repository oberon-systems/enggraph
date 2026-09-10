-- The vector half of search: chunks that carry a vector, and the queue that
-- fills them.
--
-- `code_embeddings` has existed since 0001 and has never held a row: nothing
-- in this repository chunked a file, called an embedding model or wrote to
-- the table. So it is rewritten rather than altered - there is no data to
-- migrate, and the shape it needs is different in three ways.
--
-- One: 1536 dimensions were chosen for hosted models. The model this stack
-- runs is nomic-embed-text-v1.5 at 768, and HNSW needs the dimension fixed at
-- the column, so a model of another width is another migration rather than a
-- setting.
--
-- Two: a chunk has to say which lines it came from. A file node names a file
-- and nothing finer, and entity nodes have carried no source text since 0009,
-- so the line range on the chunk is the only thing that can point a reader at
-- the part of the file that matched.
--
-- Three: the chunk records the hash of the file it was cut from, which is
-- what makes the pass resumable. A file whose hash still matches is already
-- embedded and is not queued again.
--
-- pg_trgm is the lexical half. `search_code_nodes` matches with ILIKE, which
-- no index can serve and which ranks nothing; a trigram index makes the same
-- match indexable and gives a similarity to fuse the vector hits against.

-- +goose Up

CREATE EXTENSION IF NOT EXISTS pg_trgm;

DROP TABLE IF EXISTS code_embeddings;

CREATE TABLE code_embeddings (
    id SERIAL PRIMARY KEY,
    project VARCHAR(64) NOT NULL,
    node_id VARCHAR(255) NOT NULL,
    -- Which chunk of the node this is, in file order. Unique with the node,
    -- so re-embedding a file replaces its rows instead of doubling them.
    chunk_index INTEGER NOT NULL,
    -- 1-based and inclusive, the way an editor counts.
    start_line INTEGER NOT NULL,
    end_line INTEGER NOT NULL,
    content_chunk TEXT NOT NULL,
    -- The hash of the whole file this chunk was cut from, as `file_hashes`
    -- records it. Equal means the chunk is current.
    content_hash VARCHAR(32) NOT NULL,
    -- Which model wrote the vector. Two models never share a vector space, so
    -- a row from another model is stale however fresh its hash is.
    model VARCHAR(128) NOT NULL,
    embedding VECTOR(768),
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (project, node_id) REFERENCES graph_nodes (
        project, id
    ) ON DELETE CASCADE ON UPDATE CASCADE,
    UNIQUE (project, node_id, chunk_index)
);

-- What a file's embedding state is asked by: the project and the hash.
CREATE INDEX idx_code_embeddings_hash ON code_embeddings (
    project, content_hash
);

-- HNSW over cosine distance: the intended lookup is `embedding <=> query`.
-- Unlike ivfflat this index does not need training data, so it can be created
-- before any embedding rows exist.
CREATE INDEX idx_code_embeddings_vector
ON code_embeddings USING hnsw (embedding vector_cosine_ops);

-- The lexical half over the same chunks. `simple` rather than `english`:
-- identifiers are not English words, and stemming turns `parses` and `parser`
-- into one token while losing the spelling the reader typed.
CREATE INDEX idx_code_embeddings_text
ON code_embeddings USING gin (TO_TSVECTOR('simple', content_chunk));

-- What makes an unanchored ILIKE indexable. Both columns are searched today
-- and neither has an index that a leading wildcard can use.
CREATE INDEX idx_graph_nodes_name_trgm
ON graph_nodes USING gin (name gin_trgm_ops);

CREATE INDEX idx_graph_nodes_id_trgm
ON graph_nodes USING gin (id gin_trgm_ops);

-- One row per file waiting for a vector. Kept apart from `summary_jobs` for
-- the reason that queue is kept apart from `storage`: it describes work over
-- files already in the graph, and an index run recognises nothing in it.
--
-- Keyed on the file rather than on the run, so a file that changes twice
-- before it is embedded is one task with the newer hash, not two.
CREATE TABLE embed_tasks (
    id SERIAL PRIMARY KEY,
    project VARCHAR(64) NOT NULL REFERENCES projects (
        name
    ) ON DELETE CASCADE ON UPDATE CASCADE,
    file_path TEXT NOT NULL,
    content_hash VARCHAR(32) NOT NULL,
    -- pending, running, done or failed. Free text for the same reason a
    -- plan's status is: the set grows, and a constraint here would make that
    -- a migration.
    status VARCHAR(20) NOT NULL DEFAULT 'pending',
    -- How many times a worker took this file and did not finish it. A file
    -- that reliably kills the loop is dropped rather than retried forever.
    attempts INTEGER NOT NULL DEFAULT 0,
    -- When a claim expires. A loop that died holding tasks releases them by
    -- this rather than by anyone noticing.
    lease_until TIMESTAMP WITH TIME ZONE,
    error TEXT,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (project, file_path)
);

-- The claim reads pending tasks oldest first, over every project at once.
CREATE INDEX idx_embed_tasks_pending ON embed_tasks (status, id);

-- +goose Down

DROP TABLE IF EXISTS embed_tasks;

DROP INDEX IF EXISTS idx_graph_nodes_id_trgm;

DROP INDEX IF EXISTS idx_graph_nodes_name_trgm;

DROP TABLE IF EXISTS code_embeddings;

-- Put back the 0001 shape. Nothing is lost: the rows this migration's table
-- held were vectors of a model at another width, and no query can read them
-- as this column's type.
CREATE TABLE code_embeddings (
    id SERIAL PRIMARY KEY,
    project VARCHAR(64) NOT NULL,
    node_id VARCHAR(255) NOT NULL,
    content_chunk TEXT NOT NULL,
    embedding VECTOR(1536),
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (project, node_id) REFERENCES graph_nodes (
        project, id
    ) ON DELETE CASCADE ON UPDATE CASCADE
);

CREATE INDEX idx_code_embeddings_node ON code_embeddings (project, node_id);

CREATE INDEX idx_code_embeddings_vector
ON code_embeddings USING hnsw (embedding vector_cosine_ops);

-- pg_trgm stays. Dropping an extension takes its operator classes with it,
-- and nothing else in this database owns it - the same reason 0001 leaves
-- `vector` in place.
