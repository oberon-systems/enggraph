// Every SQL string the dashboard runs.
//
// The graph, plan and suggestion queries are deliberate copies of the MCP
// tools in mcp-server/src/index.ts: the two services share a schema, not a
// process. A field added to a plan touches this file, that one, and
// scripts/backup.sh.

export const PROJECTS = `
  SELECT p.name, p.type, p.description, p.root_path, p.indexed_at,
         EXTRACT(EPOCH FROM (now() - p.indexed_at)) AS stale_seconds,
         (SELECT count(*) FROM graph_nodes AS g
           WHERE g.project = p.name) AS nodes,
         (SELECT count(*) FROM graph_edges AS e
           WHERE e.project = p.name) AS edges,
         (SELECT count(*) FROM graph_nodes AS g
           WHERE g.project = p.name AND g.type = 'file') AS files,
         (SELECT count(*) FROM graph_nodes AS l
           WHERE l.project = '_plans'
             AND l.metadata ->> 'about' = p.name) AS plans,
         (SELECT count(*) FROM project_members AS m
           WHERE m.organization = p.name) AS members,
         -- Where the last index run read each half of the selection from,
         -- which is the only place the dashboard can learn it: it holds no
         -- mount and cannot look.
         p.keep_source, p.ignore_source
    FROM projects AS p
   -- A project moved into an organization is listed there instead. Added to
   -- one it stays here: that is the whole difference between the two, and
   -- nothing about the project itself changes either way.
   WHERE NOT EXISTS (SELECT 1 FROM project_members AS o
                      WHERE o.project = p.name AND o.owned)
   ORDER BY p.name`;

export const PROJECT = `
  SELECT p.name, p.type, p.description, p.root_path, p.indexed_at,
         EXTRACT(EPOCH FROM (now() - p.indexed_at)) AS stale_seconds,
         (SELECT count(*) FROM graph_nodes AS g
           WHERE g.project = p.name) AS nodes,
         (SELECT count(*) FROM graph_edges AS e
           WHERE e.project = p.name) AS edges,
         (SELECT count(*) FROM graph_nodes AS g
           WHERE g.project = p.name AND g.type = 'file') AS files,
         (SELECT count(*) FROM graph_nodes AS l
           WHERE l.project = '_plans'
             AND l.metadata ->> 'about' = p.name) AS plans,
         (SELECT count(*) FROM project_members AS m
           WHERE m.organization = p.name) AS members,
         p.keep_source, p.ignore_source
    FROM projects AS p
   WHERE p.name = $1`;

export const PROJECT_NODE_TYPES = `
  SELECT type, count(*) AS count
    FROM graph_nodes
   WHERE project = $1
   GROUP BY type
   ORDER BY count DESC, type`;

export const PROJECT_RELATIONS = `
  SELECT relation_type, count(*) AS count
    FROM graph_edges
   WHERE project = $1
   GROUP BY relation_type
   ORDER BY count DESC, relation_type`;

export const PROJECT_EXTRAS = `
  SELECT (SELECT count(*) FROM graph_nodes AS g
           WHERE g.project = $1
             AND g.metadata ->> 'summary_source' = 'manual') AS manual_summaries,
         (SELECT count(*) FROM graph_nodes AS g
           WHERE g.project = $1 AND g.summary IS NOT NULL
             AND g.summary <> '') AS summarised,
         (SELECT count(*) FROM file_hashes AS f
           WHERE f.project = $1) AS hashed_files,
         (SELECT count(*) FROM code_embeddings AS c
           WHERE c.project = $1) AS embeddings`;

// Mirrors DROP_REPORT in mcp-server/src/index.ts, so the modal and
// the two callers cannot disagree about what a drop costs.
export const DROP_REPORT = `
  SELECT p.root_path, p.indexed_at,
         (SELECT count(*) FROM graph_nodes AS g
           WHERE g.project = p.name) AS nodes,
         (SELECT count(*) FROM graph_edges AS e
           WHERE e.project = p.name) AS edges,
         (SELECT count(*) FROM file_hashes AS f
           WHERE f.project = p.name) AS hashes,
         (SELECT count(*) FROM code_embeddings AS c
           WHERE c.project = p.name) AS embeddings,
         (SELECT count(*) FROM graph_nodes AS l
           WHERE l.project = '_plans'
             AND l.metadata ->> 'about' = p.name) AS plans,
         (SELECT count(*) FROM graph_nodes AS g
           WHERE g.type = 'suggestion'
             AND (g.project = p.name
                  OR g.metadata ->> 'about' = p.name)) AS suggestions,
         (SELECT count(*) FROM graph_nodes AS g
           WHERE g.project = p.name
             AND g.metadata ->> 'summary_source' = 'manual') AS summaries
    FROM projects AS p
   WHERE p.name = $1`;

// What kind of thing a project is. The column is deliberately unconstrained
// (migration 0006), so the vocabulary is enforced by the route rather than
// here - and the route refuses the built-in types both ways, because an index
// run into a records project deletes every record it holds.
export const PATCH_PROJECT_TYPE = `
  UPDATE projects SET type = $2
   WHERE name = $1
  RETURNING name, type`;

// What a project is for, in a sentence, written by hand rather than derived
// from the tree. An organization listing its members by name alone says
// nothing about which of them to read, and this is what it says instead.
export const PATCH_PROJECT_DESCRIPTION = `
  UPDATE projects SET description = $2
   WHERE name = $1
  RETURNING name, description`;

// What the two writes above both answer with, so the response says the whole
// of what a project is now rather than only the field that moved.
export const PROJECT_IDENTITY = `
  SELECT name, type, description FROM projects WHERE name = $1`;

// The file types actually in the graph, which is what "currently indexed"
// means: the selection says what would be picked up, this says what was.
// A name with no dot counts as itself, so Dockerfile and Makefile are types
// rather than one nameless bucket.
export const PROJECT_FILE_TYPES = `
  WITH named AS (
    SELECT split_part(id, '/', -1) AS file_name
      FROM graph_nodes
     WHERE project = $1 AND type = 'file'
  )
  SELECT CASE
           WHEN strpos(file_name, '.') = 0 THEN file_name
           ELSE '.' || lower(split_part(file_name, '.', -1))
         END AS extension,
         count(*) AS count
    FROM named
   GROUP BY extension
   ORDER BY count DESC, extension`;

// What a project itself says about its selection, beside where the last run
// read it from. The levels above it - the organizations holding it and the
// global default under _settings - are rows of this same table, read by name.
export const PROJECT_SETTINGS = `
  SELECT p.root_path,
         p.keep_source,
         p.ignore_source,
         t.ctxkeep,
         t.ctxignore,
         t.settings,
         t.updated_at
    FROM projects AS p
    LEFT JOIN project_settings AS t ON t.project = p.name
   WHERE p.name = $1`;

export const PROJECT_LEVEL_SETTINGS = `
  SELECT ctxkeep, ctxignore, settings, updated_at
    FROM project_settings
   WHERE project = $1`;

// The same upsert enggraph.storage.write_settings runs. Both documents are
// written every time, NULL included: clearing one is how a level stops
// speaking for it and lets the level above answer instead.
export const SAVE_SETTINGS = `
  INSERT INTO project_settings (project, ctxkeep, ctxignore)
  VALUES ($1, $2, $3)
  ON CONFLICT (project) DO UPDATE SET
    ctxkeep = EXCLUDED.ctxkeep,
    ctxignore = EXCLUDED.ctxignore,
    updated_at = CURRENT_TIMESTAMP
  RETURNING project, ctxkeep, ctxignore, updated_at`;

// The same merge enggraph.storage.write_settings_json runs. Only the one key
// is touched: the column carries every knob a level holds, and a schedule
// being saved must not clear what a later one stores beside it.
export const SAVE_SETTINGS_KEY = `
  INSERT INTO project_settings (project, settings)
  VALUES ($1, $2::jsonb)
  ON CONFLICT (project) DO UPDATE SET
    settings = project_settings.settings || EXCLUDED.settings,
    updated_at = CURRENT_TIMESTAMP
  RETURNING project, settings, updated_at`;

// The same merge, one level deeper. `||` is shallow: writing
// {"embedding": {"enabled": true}} REPLACES the whole embedding object, which
// would drop the URL and the token stored beside the switch. Here the stored
// object is merged with the incoming one, so a field nobody sent survives -
// which is what makes a write-only token field possible at all.
export const MERGE_SETTINGS_KEY = `
  INSERT INTO project_settings (project, settings)
  VALUES ($1, JSONB_BUILD_OBJECT($2::text, $3::jsonb))
  ON CONFLICT (project) DO UPDATE SET
    settings = project_settings.settings || JSONB_BUILD_OBJECT(
      $2::text,
      COALESCE(project_settings.settings -> $2::text, '{}'::jsonb) || $3::jsonb
    ),
    updated_at = CURRENT_TIMESTAMP
  RETURNING project, settings, updated_at`;

// Dropping the key is how a level goes back to inheriting. The row stays: it
// may still hold the selection documents, which are columns of their own.
export const CLEAR_SETTINGS_KEY = `
  UPDATE project_settings
     SET settings = settings - $2, updated_at = CURRENT_TIMESTAMP
   WHERE project = $1
  RETURNING project, settings, updated_at`;

export const CLEAR_SETTINGS = `
  DELETE FROM project_settings
   WHERE project = $1
  RETURNING project`;

export const PROJECT_EXISTS = `SELECT 1 FROM projects WHERE name = $1`;

export const PROJECT_TYPE = `SELECT type FROM projects WHERE name = $1`;

// Membership is a reference rather than ownership, so a drop does not follow
// it: an organization left pointing at a name that stopped existing would
// answer a search with a hole.
// An organization that holds anything keeps being one: its members point at it,
// and a type it no longer has would leave them pointing at a plain project.
export const PROJECT_HOLDINGS = `
  SELECT (SELECT count(*)::int FROM project_members WHERE organization = $1)
    AS members`;

export const PROJECT_ORGANIZATIONS = `
  SELECT organization, owned FROM project_members
   WHERE project = $1 ORDER BY created_at, organization`;

export const DROP_PROJECT = `DELETE FROM projects WHERE name = $1`;

// $2 is the ILIKE pattern or null, $3 the node type, $4 the file path.
export const NODES = `
  SELECT id, name, type, file_path, summary,
         count(*) OVER () AS total
    FROM graph_nodes
   WHERE project = $1
     AND ($2::text IS NULL OR name ILIKE $2 OR id ILIKE $2)
     AND ($3::text IS NULL OR type = $3)
     AND ($4::text IS NULL OR file_path = $4)
   ORDER BY id
   LIMIT $5 OFFSET $6`;

export const NODE = `
  SELECT id, name, type, file_path, summary, metadata, created_at
    FROM graph_nodes
   WHERE project = $1 AND id = $2`;

// The union of both directions, as get_code_graph_neighbors builds it, with
// $3 narrowing it to one and a window count so the page can be walked.
export const NEIGHBORS = `
  WITH neighbours AS (
    SELECT target_id AS node_id, relation_type, 'outgoing' AS direction
      FROM graph_edges
     WHERE project = $1 AND source_id = $2 AND $3::text <> 'in'
    UNION
    SELECT source_id AS node_id, relation_type, 'incoming' AS direction
      FROM graph_edges
     WHERE project = $1 AND target_id = $2 AND $3::text <> 'out'
  )
  SELECT n.node_id, n.relation_type, n.direction,
         g.type, g.file_path, g.summary,
         count(*) OVER () AS total
    FROM neighbours AS n
    LEFT JOIN graph_nodes AS g ON g.project = $1 AND g.id = n.node_id
   ORDER BY n.direction, n.relation_type, n.node_id
   LIMIT $4 OFFSET $5`;

// File nodes, not file_hashes: hashes are written only for the parsers in the
// enggraph package, so that table is not an inventory of the indexed tree.
export const FILES = `
  SELECT f.id, f.file_path, f.summary,
         (SELECT count(*) FROM graph_nodes AS e
           WHERE e.project = $1 AND e.file_path = f.file_path
             AND e.type <> 'file') AS entities,
         h.hash, h.updated_at AS hash_updated_at,
         count(*) OVER () AS total
    FROM graph_nodes AS f
    LEFT JOIN file_hashes AS h
      ON h.project = $1 AND h.file_path = f.id
   WHERE f.project = $1 AND f.type = 'file'
     AND ($2::text IS NULL OR f.id ILIKE $2)
   ORDER BY f.id
   LIMIT $3 OFFSET $4`;

// $1 is the project tag or null for every project, $2 the literal string
// '_global' switch, $3 status, $4 type, $5 the search pattern.
export const PLANS = `
  SELECT id,
         metadata ->> 'about' AS project,
         name AS title,
         metadata ->> 'status' AS status,
         type,
         metadata - 'about' - 'status' - 'summary_source'
           - 'updated_at' AS metadata,
         created_at,
         metadata ->> 'updated_at' AS updated_at,
         length(content) AS content_length,
         count(*) OVER () AS total
    FROM graph_nodes
   WHERE project = '_plans'
     AND ($1::text IS NULL OR metadata ->> 'about' = $1)
     AND ($2::boolean IS NOT TRUE OR metadata ->> 'about' IS NULL)
     AND ($3::text IS NULL OR metadata ->> 'status' = $3)
     AND ($4::text IS NULL OR type = $4)
     AND ($5::text IS NULL OR name ILIKE $5 OR content ILIKE $5)
   ORDER BY (metadata ->> 'about' IS NULL),
            metadata ->> 'updated_at' DESC
   LIMIT $6 OFFSET $7`;

export const PLAN_FACETS = `
  SELECT
    (SELECT array_agg(DISTINCT metadata ->> 'about') FROM graph_nodes
      WHERE project = '_plans'
        AND metadata ->> 'about' IS NOT NULL) AS projects,
    (SELECT array_agg(DISTINCT metadata ->> 'status') FROM graph_nodes
      WHERE project = '_plans') AS statuses,
    (SELECT array_agg(DISTINCT type) FROM graph_nodes
      WHERE project = '_plans') AS types,
    (SELECT count(*) FROM graph_nodes
      WHERE project = '_plans'
        AND metadata ->> 'about' IS NULL) AS global_plans`;

export const PLAN = `
  SELECT id,
         metadata ->> 'about' AS project,
         name AS title,
         content,
         metadata ->> 'status' AS status,
         type,
         metadata - 'about' - 'status' - 'summary_source'
           - 'updated_at' AS metadata,
         created_at,
         metadata ->> 'updated_at' AS updated_at
    FROM graph_nodes
   WHERE project = '_plans' AND id = $1`;

// Run before SAVE_PLAN, for the same reason save_plan re-creates it: the
// project is droppable by name, and its absence would turn every later save
// into a foreign key error.
export const ENSURE_PLANS_PROJECT = `
  INSERT INTO projects (name, root_path, type)
  VALUES ('_plans', 'plans://agent', 'plans')
  ON CONFLICT (name) DO NOTHING`;

// The same upsert save_plan runs in mcp-server/src/index.ts. Keep the two in
// step: a plan written here has to read back identically from the agent.
export const SAVE_PLAN = `
  INSERT INTO graph_nodes (project, id, name, type, content, metadata)
  VALUES ('_plans', $1, $3, $6, $4,
          JSONB_BUILD_OBJECT(
            'about', $2::text,
            'status', $5::text,
            'summary_source', 'manual',
            'updated_at', to_char(
              now() AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS"Z"'
            )
          ))
  ON CONFLICT (project, id) DO UPDATE SET
    name = EXCLUDED.name,
    type = EXCLUDED.type,
    content = EXCLUDED.content,
    metadata = graph_nodes.metadata || EXCLUDED.metadata
  RETURNING (xmax = 0) AS created`;

// COALESCE rather than a rebuilt statement: the list view changes one field
// at a time and has no reason to ship a whole plan back to do it.
export const PATCH_PLAN = `
  UPDATE graph_nodes
     SET name = COALESCE($2, name),
         content = COALESCE($3, content),
         type = COALESCE($5, type),
         metadata = metadata || JSONB_BUILD_OBJECT(
             'status', COALESCE($4, metadata ->> 'status'),
             'updated_at', to_char(
               now() AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS"Z"'
             )
           )
           || CASE WHEN $6::boolean
                THEN JSONB_BUILD_OBJECT('about', $7::text)
                ELSE '{}'::JSONB
              END
   WHERE project = '_plans' AND id = $1
  RETURNING id,
            metadata ->> 'about' AS project,
            name AS title,
            content,
            metadata ->> 'status' AS status,
            type,
            metadata - 'about' - 'status' - 'summary_source'
              - 'updated_at' AS metadata,
            created_at,
            metadata ->> 'updated_at' AS updated_at`;

export const DROP_PLAN = `
  DELETE FROM graph_nodes
   WHERE project = '_plans' AND id = $1
  RETURNING id,
            metadata ->> 'about' AS project,
            name AS title,
            metadata ->> 'status' AS status,
            type`;

// Suggestions are graph nodes under a built-in project rather than a table of
// their own, so every statement below carries the project and the node type.
// Mirrors the save_suggestion / get_suggestions / drop_suggestion tools.

// $1 the about tag or null for every scope, $2 the '_global' switch, $3
// status, $4 kind, $5 the search pattern.
// A memory is a node of the built-in `_memory` project, keyed `<about>/<slug>`
// so two repositories can each hold a `commit-style`. Everything the MCP tools
// write is here: the title as the node name, the gist as the summary, the text
// as the content, and the scope and tags in metadata.
export const MEMORIES = `
  SELECT id, name AS title, summary,
         metadata ->> 'about' AS about,
         COALESCE(metadata -> 'tags', '[]'::jsonb) AS tags,
         metadata ->> 'updated_at' AS updated_at,
         created_at, length(content) AS text_length,
         count(*) OVER () AS total
    FROM graph_nodes
   WHERE project = '_memory' AND type = 'memory'
     AND ($1::text IS NULL OR metadata ->> 'about' = $1)
     AND ($2::boolean IS NOT TRUE OR metadata ->> 'about' IS NULL)
     AND ($3::text IS NULL OR metadata -> 'tags' @> to_jsonb($3::text[]))
     AND ($4::text IS NULL
          OR name ILIKE $4 OR summary ILIKE $4 OR content ILIKE $4)
   ORDER BY COALESCE(metadata ->> 'updated_at', created_at::text) DESC
   LIMIT $5 OFFSET $6`;

export const MEMORY_FACETS = `
  SELECT
    (SELECT array_agg(DISTINCT metadata ->> 'about') FROM graph_nodes
      WHERE project = '_memory' AND type = 'memory'
        AND metadata ->> 'about' IS NOT NULL) AS abouts,
    (SELECT array_agg(DISTINCT tag) FROM graph_nodes,
       LATERAL jsonb_array_elements_text(
         COALESCE(metadata -> 'tags', '[]'::jsonb)
       ) AS tag
      WHERE project = '_memory' AND type = 'memory') AS tags,
    (SELECT count(*) FROM graph_nodes
      WHERE project = '_memory' AND type = 'memory'
        AND metadata ->> 'about' IS NULL) AS global_memories`;

export const MEMORY = `
  SELECT id, name AS title, summary, content AS text,
         metadata ->> 'about' AS about,
         COALESCE(metadata -> 'tags', '[]'::jsonb) AS tags,
         metadata ->> 'updated_at' AS updated_at,
         created_at
    FROM graph_nodes
   WHERE project = '_memory' AND type = 'memory' AND id = $1`;

// The scope and the slug are the node id together, so neither is patched
// here: moving a memory between projects is a different operation from
// correcting one, and doing it by accident would leave two.
export const PATCH_MEMORY = `
  UPDATE graph_nodes
     SET name = COALESCE($2, name),
         summary = COALESCE($3, summary),
         content = COALESCE($4, content),
         metadata = metadata || JSONB_BUILD_OBJECT(
           'tags', COALESCE($5::jsonb, metadata -> 'tags', '[]'::jsonb),
           'updated_at', to_char(
             now() AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS"Z"'
           )
         )
   WHERE project = '_memory' AND type = 'memory' AND id = $1
  RETURNING id, name AS title, summary, content AS text,
            metadata ->> 'about' AS about,
            COALESCE(metadata -> 'tags', '[]'::jsonb) AS tags,
            metadata ->> 'updated_at' AS updated_at,
            created_at`;

export const DROP_MEMORY = `
  DELETE FROM graph_nodes
   WHERE project = '_memory' AND type = 'memory' AND id = $1
  RETURNING id, name AS title, metadata ->> 'about' AS about`;

export const SUGGESTIONS = `
  SELECT id, name AS title, summary,
         metadata ->> 'about' AS about,
         metadata ->> 'kind' AS kind,
         metadata ->> 'lever' AS lever,
         metadata ->> 'status' AS status,
         COALESCE((metadata ->> 'hits')::int, 0) AS hits,
         metadata ->> 'first_seen' AS first_seen,
         metadata ->> 'last_seen' AS last_seen,
         created_at, length(content) AS detail_length,
         count(*) OVER () AS total
    FROM graph_nodes
   WHERE project = '_suggestions' AND type = 'suggestion'
     AND ($1::text IS NULL OR metadata ->> 'about' = $1)
     AND ($2::boolean IS NOT TRUE OR metadata ->> 'about' IS NULL)
     AND ($3::text IS NULL OR metadata ->> 'status' = $3)
     AND ($4::text IS NULL OR metadata ->> 'kind' = $4)
     AND ($5::text IS NULL
          OR name ILIKE $5 OR summary ILIKE $5 OR content ILIKE $5)
   ORDER BY COALESCE((metadata ->> 'hits')::int, 0) DESC,
            metadata ->> 'last_seen' DESC NULLS LAST
   LIMIT $6 OFFSET $7`;

export const SUGGESTION_FACETS = `
  SELECT
    (SELECT array_agg(DISTINCT metadata ->> 'about') FROM graph_nodes
      WHERE project = '_suggestions' AND type = 'suggestion'
        AND metadata ->> 'about' IS NOT NULL) AS abouts,
    (SELECT array_agg(DISTINCT metadata ->> 'status') FROM graph_nodes
      WHERE project = '_suggestions' AND type = 'suggestion') AS statuses,
    (SELECT array_agg(DISTINCT metadata ->> 'kind') FROM graph_nodes
      WHERE project = '_suggestions' AND type = 'suggestion'
        AND metadata ->> 'kind' IS NOT NULL) AS kinds,
    (SELECT count(*) FROM graph_nodes
      WHERE project = '_suggestions' AND type = 'suggestion'
        AND metadata ->> 'about' IS NULL) AS global_suggestions`;

export const SUGGESTION = `
  SELECT id, name AS title, summary, content AS detail,
         metadata ->> 'about' AS about,
         metadata ->> 'kind' AS kind,
         metadata ->> 'lever' AS lever,
         metadata ->> 'status' AS status,
         COALESCE((metadata ->> 'hits')::int, 0) AS hits,
         metadata ->> 'first_seen' AS first_seen,
         metadata ->> 'last_seen' AS last_seen,
         created_at
    FROM graph_nodes
   WHERE project = '_suggestions' AND type = 'suggestion' AND id = $1`;

// Triage, not authorship: the dashboard edits the wording and the lifecycle,
// and never touches hits or first_seen, which are the agent's record of how
// often this gap was actually hit.
export const PATCH_SUGGESTION = `
  UPDATE graph_nodes
     SET name = COALESCE($2, name),
         summary = COALESCE($3, summary),
         content = COALESCE($4, content),
         metadata = metadata || JSONB_BUILD_OBJECT(
           'status', COALESCE($5::text, metadata ->> 'status'),
           'kind', COALESCE($6::text, metadata ->> 'kind'),
           'lever', COALESCE($7::text, metadata ->> 'lever')
         )
   WHERE project = '_suggestions' AND type = 'suggestion' AND id = $1
  RETURNING id, name AS title, summary, content AS detail,
            metadata ->> 'about' AS about,
            metadata ->> 'kind' AS kind,
            metadata ->> 'lever' AS lever,
            metadata ->> 'status' AS status,
            COALESCE((metadata ->> 'hits')::int, 0) AS hits,
            metadata ->> 'first_seen' AS first_seen,
            metadata ->> 'last_seen' AS last_seen,
            created_at`;

export const DROP_SUGGESTION = `
  DELETE FROM graph_nodes
   WHERE project = '_suggestions' AND type = 'suggestion' AND id = $1
  RETURNING id, name AS title,
            metadata ->> 'about' AS about,
            metadata ->> 'status' AS status`;
