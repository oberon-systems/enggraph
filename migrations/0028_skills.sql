-- Skills the MCP server hands out: no project is global, `repo` rows mirror
-- skills/ read-only. Unset switches: on for built-ins and a scope's own skills.

-- +goose Up

CREATE TABLE IF NOT EXISTS skills (
    id SERIAL PRIMARY KEY,
    project VARCHAR(64) REFERENCES projects (
        name
    ) ON DELETE CASCADE ON UPDATE CASCADE,
    name VARCHAR(64) NOT NULL,
    content TEXT NOT NULL,
    sha256 CHAR(64) NOT NULL,
    source VARCHAR(16) NOT NULL,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT skills_source CHECK (source IN ('repo', 'import')),
    CONSTRAINT skills_repo_global CHECK (source = 'import' OR project IS NULL)
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_skills_scope_name ON skills (
    COALESCE(project, ''), name
);

CREATE TABLE IF NOT EXISTS skill_enablement (
    project VARCHAR(64) NOT NULL REFERENCES projects (
        name
    ) ON DELETE CASCADE ON UPDATE CASCADE,
    skill_id INTEGER NOT NULL REFERENCES skills (id) ON DELETE CASCADE,
    enabled BOOLEAN NOT NULL,
    PRIMARY KEY (project, skill_id)
);

-- +goose Down

DROP TABLE IF EXISTS skill_enablement;
DROP INDEX IF EXISTS idx_skills_scope_name;
DROP TABLE IF EXISTS skills;
