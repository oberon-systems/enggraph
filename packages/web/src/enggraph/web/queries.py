"""Every statement the dashboard runs, as its SQL was written."""

PROJECTS = """
  SELECT p.name, p.type, p.description, p.root_path, p.indexed_at,
         EXTRACT(EPOCH FROM (now() - p.indexed_at)) AS stale_seconds,
         (SELECT count(*) FROM nodes AS l
           WHERE l.project = '_plans'
             AND l.metadata ->> 'about' = p.name) AS plans,
         (SELECT count(*) FROM org_members AS m
           WHERE m.organization = p.name) AS members
    FROM projects AS p
   -- A project moved into an organization is listed there instead. Added to
   -- one it stays here: that is the whole difference between the two, and
   -- nothing about the project itself changes either way.
   WHERE NOT EXISTS (SELECT 1 FROM org_members AS o
                      WHERE o.project = p.name AND o.owned)
   ORDER BY p.name"""

PROJECT_COUNTS = """
  SELECT p.name,
         (SELECT count(*) FROM nodes AS g
           WHERE g.project = p.name) AS nodes,
         (SELECT count(*) FROM edges AS e
           WHERE e.project = p.name) AS edges,
         (SELECT count(*) FROM nodes AS g
           WHERE g.project = p.name AND g.type = 'file') AS files
    FROM unnest($1::text[]) AS p (name)"""

PROJECT = """
  SELECT p.name, p.type, p.description, p.root_path, p.indexed_at,
         EXTRACT(EPOCH FROM (now() - p.indexed_at)) AS stale_seconds,
         (SELECT count(*) FROM nodes AS g
           WHERE g.project = p.name) AS nodes,
         (SELECT count(*) FROM edges AS e
           WHERE e.project = p.name) AS edges,
         (SELECT count(*) FROM nodes AS g
           WHERE g.project = p.name AND g.type = 'file') AS files,
         (SELECT count(*) FROM nodes AS l
           WHERE l.project = '_plans'
             AND l.metadata ->> 'about' = p.name) AS plans,
         (SELECT count(*) FROM org_members AS m
           WHERE m.organization = p.name) AS members
    FROM projects AS p
   WHERE p.name = $1"""

PROJECT_NODE_TYPES = """
  SELECT type, count(*) AS count
    FROM nodes
   WHERE project = $1
   GROUP BY type
   ORDER BY count DESC, type"""

PROJECT_RELATIONS = """
  SELECT relation_type, count(*) AS count
    FROM edges
   WHERE project = $1
   GROUP BY relation_type
   ORDER BY count DESC, relation_type"""

PROJECT_EXTRAS = """
  SELECT (SELECT count(*) FROM nodes AS g
           WHERE g.project = $1
             AND g.metadata ->> 'summary_source' = 'manual') AS manual_summaries,
         (SELECT count(*) FROM nodes AS g
           WHERE g.project = $1 AND g.summary IS NOT NULL
             AND g.summary <> '') AS summarised,
         (SELECT count(*) FROM indexed_files AS f
           WHERE f.project = $1) AS hashed_files,
         (SELECT count(*) FROM chunks AS c
           WHERE c.project = $1) AS embeddings"""

DROP_REPORT = """
  SELECT p.root_path, p.indexed_at,
         (SELECT count(*) FROM nodes AS g
           WHERE g.project = p.name) AS nodes,
         (SELECT count(*) FROM edges AS e
           WHERE e.project = p.name) AS edges,
         (SELECT count(*) FROM indexed_files AS f
           WHERE f.project = p.name) AS hashes,
         (SELECT count(*) FROM chunks AS c
           WHERE c.project = p.name) AS embeddings,
         (SELECT count(*) FROM nodes AS l
           WHERE l.project = '_plans'
             AND l.metadata ->> 'about' = p.name) AS plans,
         (SELECT count(*) FROM nodes AS g
           WHERE g.type = 'suggestion'
             AND (g.project = p.name
                  OR g.metadata ->> 'about' = p.name)) AS suggestions,
         (SELECT count(*) FROM nodes AS g
           WHERE g.project = p.name
             AND g.metadata ->> 'summary_source' = 'manual') AS summaries,
         (SELECT count(*) FROM declared_links AS r
           WHERE r.source_project = p.name
              OR r.target_project = p.name) AS relations,
         (SELECT count(*) FROM provided_names AS x
           WHERE x.project = p.name AND x.origin = 'manual') AS exports,
         (SELECT count(*) FROM record_links AS k
           WHERE k.project = p.name) AS record_links
    FROM projects AS p
   WHERE p.name = $1"""

PATCH_PROJECT_TYPE = """
  UPDATE projects SET type = $2
   WHERE name = $1
  RETURNING name, type"""

PATCH_PROJECT_DESCRIPTION = """
  UPDATE projects SET description = $2
   WHERE name = $1
  RETURNING name, description"""

PROJECT_IDENTITY = """
  SELECT name, type, description FROM projects WHERE name = $1"""

PROJECT_FILE_TYPES = """
  WITH named AS (
    SELECT split_part(id, '/', -1) AS file_name
      FROM nodes
     WHERE project = $1 AND type = 'file'
  )
  SELECT CASE
           WHEN strpos(file_name, '.') = 0 THEN file_name
           ELSE '.' || lower(split_part(file_name, '.', -1))
         END AS extension,
         count(*) AS count
    FROM named
   GROUP BY extension
   ORDER BY count DESC, extension"""

PROJECT_SETTINGS = """
  SELECT p.root_path,
         p.formats,
         p.formats_at,
         t.ignore_patterns,
         t.settings,
         t.updated_at
    FROM projects AS p
    LEFT JOIN settings AS t ON t.project = p.name
   WHERE p.name = $1"""

PROJECT_LEVEL_SETTINGS = """
  SELECT ignore_patterns, settings, updated_at
    FROM settings
   WHERE project = $1"""

PROJECT_INHERITED_IGNORE = """
  SELECT name, document, origin
    FROM (SELECT 'global' AS origin, s.project AS name,
                 s.ignore_patterns AS document, 0 AS rank,
                 NULL::timestamptz AS joined
            FROM settings AS s
           WHERE s.project = $2
          UNION ALL
          SELECT 'organization', m.organization, s.ignore_patterns, 1,
                 m.created_at
            FROM org_members AS m
            JOIN settings AS s ON s.project = m.organization
           WHERE m.project = $1) AS levels
   WHERE coalesce(trim(document), '') <> ''
   ORDER BY rank, joined, name"""

SAVE_IGNORE = """
  INSERT INTO settings (project, ignore_patterns)
  VALUES ($1, $2)
  ON CONFLICT (project) DO UPDATE SET
    ignore_patterns = EXCLUDED.ignore_patterns,
    updated_at = CURRENT_TIMESTAMP
  RETURNING project, ignore_patterns, updated_at"""

SAVE_SETTINGS_KEY = """
  INSERT INTO settings (project, settings)
  VALUES ($1, $2::jsonb)
  ON CONFLICT (project) DO UPDATE SET
    settings = settings.settings || EXCLUDED.settings,
    updated_at = CURRENT_TIMESTAMP
  RETURNING project, settings, updated_at"""

MERGE_SETTINGS_KEY = """
  INSERT INTO settings (project, settings)
  VALUES ($1, JSONB_BUILD_OBJECT($2::text, JSONB_STRIP_NULLS($3::jsonb)))
  ON CONFLICT (project) DO UPDATE SET
    settings = settings.settings || JSONB_BUILD_OBJECT(
      $2::text,
      JSONB_STRIP_NULLS(
        COALESCE(settings.settings -> $2::text, '{}'::jsonb) || $3::jsonb
      )
    ),
    updated_at = CURRENT_TIMESTAMP
  RETURNING project, settings, updated_at"""

CLEAR_SETTINGS_KEY = """
  UPDATE settings
     SET settings = settings - $2, updated_at = CURRENT_TIMESTAMP
   WHERE project = $1
  RETURNING project, settings, updated_at"""

CLEAR_SETTINGS = """
  DELETE FROM settings
   WHERE project = $1
  RETURNING project"""

PROJECT_EXISTS = """SELECT 1 FROM projects WHERE name = $1"""

PROJECT_TYPE = """SELECT type FROM projects WHERE name = $1"""

PROJECT_HOLDINGS = """
  SELECT (SELECT count(*)::int FROM org_members WHERE organization = $1)
    AS members"""

PROJECT_ORGANIZATIONS = """
  SELECT organization, owned FROM org_members
   WHERE project = $1 ORDER BY created_at, organization"""

NODES = """
  SELECT id, name, type, file_path, summary,
         count(*) OVER () AS total
    FROM nodes
   WHERE project = $1
     AND ($2::text IS NULL OR name ILIKE $2 OR id ILIKE $2)
     AND ($3::text IS NULL OR type = $3)
     AND ($4::text IS NULL OR file_path = $4)
   ORDER BY id
   LIMIT $5 OFFSET $6"""

NODE = """
  SELECT id, name, type, file_path, summary, metadata, created_at
    FROM nodes
   WHERE project = $1 AND id = $2"""

NEIGHBORS = """
  WITH neighbours AS (
    SELECT target_id AS node_id, relation_type, 'outgoing' AS direction
      FROM edges
     WHERE project = $1 AND source_id = $2 AND $3::text <> 'in'
    UNION
    SELECT source_id AS node_id, relation_type, 'incoming' AS direction
      FROM edges
     WHERE project = $1 AND target_id = $2 AND $3::text <> 'out'
  )
  SELECT n.node_id, n.relation_type, n.direction,
         g.type, g.file_path, g.summary,
         count(*) OVER () AS total
    FROM neighbours AS n
    LEFT JOIN nodes AS g ON g.project = $1 AND g.id = n.node_id
   ORDER BY n.direction, n.relation_type, n.node_id
   LIMIT $4 OFFSET $5"""

FILES = """
  SELECT f.id, f.file_path, f.summary,
         (SELECT count(*) FROM nodes AS e
           WHERE e.project = $1 AND e.file_path = f.file_path
             AND e.type <> 'file') AS entities,
         h.hash, h.updated_at AS hash_updated_at,
         count(*) OVER () AS total
    FROM nodes AS f
    LEFT JOIN indexed_files AS h
      ON h.project = $1 AND h.file_path = f.id
   WHERE f.project = $1 AND f.type = 'file'
     AND ($2::text IS NULL OR f.id ILIKE $2)
   ORDER BY f.id
   LIMIT $3 OFFSET $4"""

PLANS = """
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
    FROM nodes
   WHERE project = '_plans'
     AND ($1::text IS NULL OR metadata ->> 'about' = $1)
     AND ($2::boolean IS NOT TRUE OR metadata ->> 'about' IS NULL)
     AND ($3::text IS NULL OR metadata ->> 'status' = $3)
     AND ($4::text IS NULL OR type = $4)
     AND ($5::text IS NULL OR name ILIKE $5 OR content ILIKE $5)
   ORDER BY (metadata ->> 'about' IS NULL),
            metadata ->> 'updated_at' DESC
   LIMIT $6 OFFSET $7"""

PLAN_FACETS = """
  SELECT
    (SELECT array_agg(DISTINCT metadata ->> 'about') FROM nodes
      WHERE project = '_plans'
        AND metadata ->> 'about' IS NOT NULL) AS projects,
    (SELECT array_agg(DISTINCT metadata ->> 'status') FROM nodes
      WHERE project = '_plans') AS statuses,
    (SELECT array_agg(DISTINCT type) FROM nodes
      WHERE project = '_plans') AS types,
    (SELECT count(*) FROM nodes
      WHERE project = '_plans'
        AND metadata ->> 'about' IS NULL) AS global_plans"""

PLAN_TARGETS = """
  SELECT p.name, p.type,
         COALESCE(
           array_agg(m.organization ORDER BY m.organization)
             FILTER (WHERE m.organization IS NOT NULL),
           '{}'
         ) AS organizations
    FROM projects AS p
    LEFT JOIN org_members AS m ON m.project = p.name
   WHERE left(p.name, 1) <> '_'
   GROUP BY p.name, p.type
   ORDER BY p.name"""

PLAN = """
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
    FROM nodes
   WHERE project = '_plans' AND id = $1"""

ENSURE_PLANS_PROJECT = """
  INSERT INTO projects (name, root_path, type)
  VALUES ('_plans', 'plans://agent', 'plans')
  ON CONFLICT (name) DO NOTHING"""

SAVE_PLAN = """
  INSERT INTO nodes (project, id, name, type, content, metadata)
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
    metadata = nodes.metadata || EXCLUDED.metadata
  RETURNING (xmax = 0) AS created"""

PATCH_PLAN = """
  UPDATE nodes
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
            metadata ->> 'updated_at' AS updated_at"""

DROP_PLAN = """
  WITH gone AS (
    DELETE FROM nodes
     WHERE project = '_plans' AND id = $1
    RETURNING id,
              metadata ->> 'about' AS project,
              name AS title,
              metadata ->> 'status' AS status,
              type
  ), links AS (
    DELETE FROM record_links AS r USING gone
     WHERE r.record_project = '_plans' AND r.record_id = gone.id
  )
  SELECT id, project, title, status, type FROM gone"""

MEMORIES = """
  SELECT id, name AS title, summary,
         metadata ->> 'about' AS about,
         COALESCE(metadata -> 'tags', '[]'::jsonb) AS tags,
         metadata ->> 'updated_at' AS updated_at,
         created_at, length(content) AS text_length,
         count(*) OVER () AS total
    FROM nodes
   WHERE project = '_memory' AND type = 'memory'
     AND ($1::text IS NULL OR metadata ->> 'about' = $1)
     AND ($2::boolean IS NOT TRUE OR metadata ->> 'about' IS NULL)
     AND ($3::text IS NULL OR metadata -> 'tags' @> to_jsonb($3::text[]))
     AND ($4::text IS NULL
          OR name ILIKE $4 OR summary ILIKE $4 OR content ILIKE $4)
   ORDER BY COALESCE(metadata ->> 'updated_at', created_at::text) DESC
   LIMIT $5 OFFSET $6"""

MEMORY_FACETS = """
  SELECT
    (SELECT array_agg(DISTINCT metadata ->> 'about') FROM nodes
      WHERE project = '_memory' AND type = 'memory'
        AND metadata ->> 'about' IS NOT NULL) AS abouts,
    (SELECT array_agg(DISTINCT tag) FROM nodes,
       LATERAL jsonb_array_elements_text(
         COALESCE(metadata -> 'tags', '[]'::jsonb)
       ) AS tag
      WHERE project = '_memory' AND type = 'memory') AS tags,
    (SELECT count(*) FROM nodes
      WHERE project = '_memory' AND type = 'memory'
        AND metadata ->> 'about' IS NULL) AS global_memories"""

MEMORY = """
  SELECT id, name AS title, summary, content AS text,
         metadata ->> 'about' AS about,
         COALESCE(metadata -> 'tags', '[]'::jsonb) AS tags,
         metadata ->> 'updated_at' AS updated_at,
         created_at
    FROM nodes
   WHERE project = '_memory' AND type = 'memory' AND id = $1"""

PATCH_MEMORY = """
  UPDATE nodes
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
            created_at"""

DROP_MEMORY = """
  WITH gone AS (
    DELETE FROM nodes
     WHERE project = '_memory' AND type = 'memory' AND id = $1
    RETURNING id, name AS title, metadata ->> 'about' AS about
  ), links AS (
    DELETE FROM record_links AS r USING gone
     WHERE r.record_project = '_memory' AND r.record_id = gone.id
  )
  SELECT id, title, about FROM gone"""

SUGGESTIONS = """
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
    FROM nodes
   WHERE project = '_suggestions' AND type = 'suggestion'
     AND ($1::text IS NULL OR metadata ->> 'about' = $1)
     AND ($2::boolean IS NOT TRUE OR metadata ->> 'about' IS NULL)
     AND ($3::text IS NULL OR metadata ->> 'status' = $3)
     AND ($4::text IS NULL OR metadata ->> 'kind' = $4)
     AND ($5::text IS NULL
          OR name ILIKE $5 OR summary ILIKE $5 OR content ILIKE $5)
   ORDER BY COALESCE((metadata ->> 'hits')::int, 0) DESC,
            metadata ->> 'last_seen' DESC NULLS LAST
   LIMIT $6 OFFSET $7"""

SUGGESTION_FACETS = """
  SELECT
    (SELECT array_agg(DISTINCT metadata ->> 'about') FROM nodes
      WHERE project = '_suggestions' AND type = 'suggestion'
        AND metadata ->> 'about' IS NOT NULL) AS abouts,
    (SELECT array_agg(DISTINCT metadata ->> 'status') FROM nodes
      WHERE project = '_suggestions' AND type = 'suggestion') AS statuses,
    (SELECT array_agg(DISTINCT metadata ->> 'kind') FROM nodes
      WHERE project = '_suggestions' AND type = 'suggestion'
        AND metadata ->> 'kind' IS NOT NULL) AS kinds,
    (SELECT count(*) FROM nodes
      WHERE project = '_suggestions' AND type = 'suggestion'
        AND metadata ->> 'about' IS NULL) AS global_suggestions"""

SUGGESTION = """
  SELECT id, name AS title, summary, content AS detail,
         metadata ->> 'about' AS about,
         metadata ->> 'kind' AS kind,
         metadata ->> 'lever' AS lever,
         metadata ->> 'status' AS status,
         COALESCE((metadata ->> 'hits')::int, 0) AS hits,
         metadata ->> 'first_seen' AS first_seen,
         metadata ->> 'last_seen' AS last_seen,
         COALESCE(metadata -> 'queries', '[]'::jsonb) AS queries,
         created_at
    FROM nodes
   WHERE project = '_suggestions' AND type = 'suggestion' AND id = $1"""

PATCH_SUGGESTION = """
  UPDATE nodes
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
            created_at"""

DROP_SUGGESTION = """
  WITH gone AS (
    DELETE FROM nodes
     WHERE project = '_suggestions' AND type = 'suggestion' AND id = $1
    RETURNING id, name AS title,
              metadata ->> 'about' AS about,
              metadata ->> 'status' AS status
  ), links AS (
    DELETE FROM record_links AS r USING gone
     WHERE r.record_project = '_suggestions' AND r.record_id = gone.id
  )
  SELECT id, title, about, status FROM gone"""

RECORD_NODES = """
  SELECT r.project, r.node_id, r.relation,
         n.type, n.summary, n.id IS NULL AS missing
    FROM record_links AS r
    LEFT JOIN nodes AS n
      ON n.project = r.project AND n.id = r.node_id
   WHERE r.record_project = $1 AND r.record_id = $2
   ORDER BY r.project, r.node_id"""

ADD_RECORD_NODE = """
  INSERT INTO record_links (record_project, record_id, project, node_id)
  SELECT $1::text, $2::text, $3::text, $4::text
   WHERE EXISTS (SELECT 1 FROM nodes
                  WHERE project = $1::text AND id = $2::text)
     AND EXISTS (SELECT 1 FROM nodes
                  WHERE project = $3::text AND id = $4::text)
  ON CONFLICT DO NOTHING
  RETURNING project, node_id"""

DROP_RECORD_NODE = """
  DELETE FROM record_links
   WHERE record_project = $1 AND record_id = $2
     AND project = $3 AND node_id = $4
  RETURNING project, node_id"""

NODE_KNOWLEDGE = """
  SELECT DISTINCT ON (r.record_project, r.record_id)
         r.record_project, r.record_id, n.type, n.name AS title, n.summary,
         n.metadata ->> 'status' AS status, r.node_id AS attached_to
    FROM record_links AS r
    JOIN nodes AS n
      ON n.project = r.record_project AND n.id = r.record_id
   WHERE r.project = $1 AND r.node_id = ANY ($2::text[])
   ORDER BY r.record_project, r.record_id, length(r.node_id) DESC"""

SUGGESTION_GROUPS = """
  SELECT COALESCE(metadata ->> $1, '(none)') AS key,
         NULL::text AS project, count(*)::int AS records,
         sum(COALESCE((metadata ->> 'hits')::int, 0))::int AS hits,
         (array_agg(
            jsonb_build_object(
              'id', id, 'title', name,
              'hits', COALESCE((metadata ->> 'hits')::int, 0)
            )
            ORDER BY COALESCE((metadata ->> 'hits')::int, 0) DESC, id
          ))[1:5] AS top
    FROM nodes
   WHERE project = '_suggestions' AND type = 'suggestion'
     AND ($2::text IS NULL OR metadata ->> 'status' = $2)
   GROUP BY 1
   ORDER BY hits DESC, records DESC, key"""

SUGGESTION_DIRECTORIES = """
  WITH placed AS (
    SELECT DISTINCT g.id, g.name,
           COALESCE((g.metadata ->> 'hits')::int, 0) AS hits, r.project,
           COALESCE(
             NULLIF(
               substring(split_part(r.node_id, '::', 1) FROM '^(.*/)'), ''
             ),
             './'
           ) AS directory
      FROM nodes AS g
      JOIN record_links AS r
        ON r.record_project = g.project AND r.record_id = g.id
     WHERE g.project = '_suggestions' AND g.type = 'suggestion'
       AND ($1::text IS NULL OR g.metadata ->> 'status' = $1)
  )
  SELECT directory AS key, project, count(*)::int AS records,
         sum(hits)::int AS hits,
         (array_agg(
            jsonb_build_object('id', id, 'title', name, 'hits', hits)
            ORDER BY hits DESC, id
          ))[1:5] AS top
    FROM placed
   GROUP BY project, directory
   ORDER BY hits DESC, records DESC, project, directory"""

SKILLS = """
  SELECT id, name, project AS owner, source, sha256,
         length(content) AS length, updated_at
    FROM agent_skills
   WHERE ($2 AND project IS NULL)
      OR (NOT $2 AND ($1::text IS NULL OR project = $1))
   ORDER BY project NULLS FIRST, name"""

SKILL = """
  SELECT id, name, project AS owner, source, sha256, content, updated_at
    FROM agent_skills
   WHERE id = $1"""

IMPORT_SKILL = """
  INSERT INTO agent_skills (project, name, content, sha256, source)
  VALUES ($1, $2, $3, $4, 'import')
  ON CONFLICT (COALESCE(project, ''), name) DO UPDATE
     SET content = EXCLUDED.content,
         sha256 = EXCLUDED.sha256,
         updated_at = CURRENT_TIMESTAMP
   WHERE agent_skills.source = 'import'
  RETURNING id, name, project AS owner, source, sha256"""

DROP_SKILL = """
  WITH gone AS (
    DELETE FROM agent_skills
     WHERE id = $1 AND source = 'import'
    RETURNING id, name, project AS owner
  ), switches AS (
    DELETE FROM skill_switches WHERE skill_id IN (SELECT id FROM gone)
  )
  SELECT id, name, owner FROM gone"""

PROJECT_SKILLS = """
  WITH orgs AS (
    SELECT organization AS name FROM org_members WHERE project = $1
  )
  SELECT s.id, s.name, s.project AS owner, s.source, s.sha256,
         (s.name = 'enggraph' AND s.source = 'repo') AS locked,
         e.enabled AS explicit,
         (s.name = 'enggraph' AND s.source = 'repo') OR COALESCE(
           e.enabled,
           (SELECT bool_or(o.enabled) FROM skill_switches AS o
             WHERE o.skill_id = s.id
               AND o.project IN (SELECT name FROM orgs)),
           s.source = 'repo' OR s.project IS NOT NULL
         ) AS enabled
    FROM agent_skills AS s
    LEFT JOIN skill_switches AS e
      ON e.project = $1 AND e.skill_id = s.id
   WHERE s.project IS NULL OR s.project = $1
      OR s.project IN (SELECT name FROM orgs)
   ORDER BY s.name, s.project NULLS LAST"""

SET_SKILL_ENABLED = """
  INSERT INTO skill_switches (project, skill_id, enabled)
  SELECT $1::varchar, s.id, $3::boolean
    FROM agent_skills AS s
   WHERE s.id = $2
     AND NOT (s.name = 'enggraph' AND s.source = 'repo')
     AND (s.project IS NULL OR s.project = $1
          OR s.project IN (SELECT organization FROM org_members
                            WHERE project = $1))
  ON CONFLICT (project, skill_id) DO UPDATE SET enabled = EXCLUDED.enabled
  RETURNING skill_id, enabled"""
