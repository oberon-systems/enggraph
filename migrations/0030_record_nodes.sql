-- The code nodes a memory, plan or suggestion is about. Code nodes are keyed to
-- `projects` only: an index run re-inserts them and must not cascade.

-- +goose Up

CREATE TABLE IF NOT EXISTS record_nodes (
    record_project VARCHAR(64) NOT NULL,
    record_id VARCHAR(255) NOT NULL,
    project VARCHAR(64) NOT NULL REFERENCES projects (
        name
    ) ON DELETE CASCADE ON UPDATE CASCADE,
    node_id VARCHAR(255) NOT NULL,
    relation VARCHAR(50) NOT NULL DEFAULT 'about',
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (record_project, record_id, project, node_id),
    CONSTRAINT record_nodes_record FOREIGN KEY (
        record_project, record_id
    ) REFERENCES graph_nodes (
        project, id
    ) ON DELETE CASCADE ON UPDATE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_record_nodes_node ON record_nodes (
    project, node_id
);

-- +goose Down

DROP TABLE IF EXISTS record_nodes;
