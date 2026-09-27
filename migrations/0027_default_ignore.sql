-- The global ignore default, in full.
--
-- 0026 left it holding the agent directories alone. A default still empty or
-- still that seed is replaced by the whole document; one somebody has edited
-- keeps every line and gains the ones it lacks.

-- +goose Up

INSERT INTO project_settings (project)
VALUES ('_settings')
ON CONFLICT DO NOTHING;

WITH
default_ignore AS (
    SELECT $doc$# Secrets. Pruned by the indexer even when removed from here: the text
# of every indexed file is stored in the graph.
.env
.env.*
*.env
!.env.example
!.env.sample
!.env.template
*.pem
*.key
*.crt
*.cer
*.der
*.p12
*.pfx
*.jks
*.keystore
*.ppk
*.gpg
*.kdbx
*.jwt
*.tfvars
*.tfstate
*.tfstate.*
*.sops.*
vault.yml
vault.yaml
*.vault.yml
id_rsa*
id_ecdsa*
id_ed25519*
id_dsa*
authorized_keys
known_hosts
.htpasswd
.netrc
.pgpass
credentials
credentials.json
kubeconfig
.ssh/
.gnupg/
.aws/
.kube/
.docker/config.json

# Agent state rather than code of the tree.
.claude/
.gemini/
.codex/
.cursor/

# Lock files and generated code, saying nothing their source does not.
package-lock.json
npm-shrinkwrap.json
yarn.lock
pnpm-lock.yaml
composer.lock
poetry.lock
Pipfile.lock
Gemfile.lock
Cargo.lock
go.sum
*.min.js
*.min.css
*.map
*_pb2.py
*.pb.go

# Build and tool output the built-in skip list does not cover.
bower_components/
.nuxt/
.svelte-kit/
.turbo/
.parcel-cache/
.angular/
.yarn/
.pnpm-store/
.eggs/
*.egg-info/
htmlcov/
.nyc_output/
.serverless/
.direnv/
.hypothesis/
*.log
$doc$ AS doc
),

missing AS (
    SELECT
        STRING_AGG(
            l.line, E'\n'
            ORDER BY l.n
        ) AS added
    FROM default_ignore AS d
    CROSS JOIN
        LATERAL UNNEST(STRING_TO_ARRAY(d.doc, E'\n'))
        WITH ORDINALITY AS l (line, n)
    CROSS JOIN project_settings AS s
    WHERE
        s.project = '_settings'
        AND TRIM(l.line) <> ''
        AND l.line NOT LIKE '#%'
        AND TRIM(l.line) <> ALL(
            STRING_TO_ARRAY(COALESCE(s.ignore_patterns, ''), E'\n')
        )
)

UPDATE project_settings AS s
SET
    ignore_patterns = CASE
        WHEN
            COALESCE(BTRIM(s.ignore_patterns, E' \t\r\n'), '')
            IN ('', E'.claude/\n.gemini/')
            THEN d.doc
        ELSE
            s.ignore_patterns
            || E'\n# Added by migration 0027\n'
            || m.added
            || E'\n'
    END,
    updated_at = CURRENT_TIMESTAMP
FROM default_ignore AS d
CROSS JOIN missing AS m
WHERE s.project = '_settings' AND m.added IS NOT NULL;

-- +goose Down

UPDATE project_settings
SET ignore_patterns = E'.claude/\n.gemini/\n', updated_at = CURRENT_TIMESTAMP
WHERE project = '_settings';
