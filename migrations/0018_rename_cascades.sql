-- A project's name is its identity, so renaming it has to move every row.
--
-- The name keys the graph, addresses the MCP server at `/mcp/<name>`, names
-- the mount at `/code/<name>` and tags every record an agent wrote about the
-- project. A project onboarded under the wrong name had no way back: the only
-- operation that moved rows between projects was absorbing one into another,
-- which is a different thing and takes the directories with it.
--
-- Every foreign key onto `projects (name)` was ON DELETE CASCADE and nothing
-- else, so updating the key was refused outright by the rows that reference
-- it. They become ON UPDATE CASCADE as well, and so do the keys onto
-- `graph_nodes (project, id)`, which is what carries the rename its second
-- hop: projects to graph_nodes to that project's edges and embeddings.
--
-- Rebuilt by looking the constraints up rather than by naming them. Every one
-- was created unnamed and carries whatever Postgres derived, and a name
-- guessed wrong here would not fail - it would add a second constraint and
-- leave the first one refusing the update, which is the one outcome a
-- migration must not have.
--
-- A constraint that cannot be re-validated is re-added NOT VALID rather than
-- dropped or forced. Adding a foreign key re-checks the whole table, and
-- `graph_edges` holds rows whose node is gone - the database reports the
-- constraint as validated, so those rows arrived by a path that does not run
-- its triggers, which is what restoring a dump does. Cleaning them is a
-- decision about data and does not belong in a schema migration: NOT VALID
-- keeps exactly the guarantee that is in force today, new rows checked and
-- old ones left alone, and adds the cascade this migration is for.
--
-- What no foreign key reaches stays the renaming code's job: `index_jobs`
-- references nothing, and the records in the built-in projects carry the name
-- in `metadata ->> 'about'` and in their own ids.

-- +goose Up

-- +goose StatementBegin
DO $$
DECLARE
    reference RECORD;
BEGIN
    FOR reference IN
        SELECT
            c.conname AS name,
            c.conrelid::REGCLASS AS child,
            pg_get_constraintdef(c.oid) AS definition
        FROM pg_constraint AS c
        WHERE
            c.contype = 'f'
            AND c.confrelid IN ('projects'::REGCLASS, 'graph_nodes'::REGCLASS)
            AND pg_get_constraintdef(c.oid) NOT LIKE '%ON UPDATE CASCADE%'
    LOOP
        EXECUTE format(
            'ALTER TABLE %s DROP CONSTRAINT %I', reference.child, reference.name
        );
        BEGIN
            EXECUTE format(
                'ALTER TABLE %s ADD CONSTRAINT %I %s ON UPDATE CASCADE',
                reference.child, reference.name, reference.definition
            );
        EXCEPTION WHEN foreign_key_violation THEN
            EXECUTE format(
                'ALTER TABLE %s ADD CONSTRAINT %I %s ON UPDATE CASCADE NOT VALID',
                reference.child, reference.name, reference.definition
            );
            RAISE NOTICE
                '% on % holds rows it cannot vouch for and was re-added NOT VALID',
                reference.name, reference.child;
        END;
    END LOOP;
END
$$;
-- +goose StatementEnd

-- +goose Down

-- +goose StatementBegin
DO $$
DECLARE
    reference RECORD;
    restored TEXT;
BEGIN
    FOR reference IN
        SELECT
            c.conname AS name,
            c.conrelid::REGCLASS AS child,
            pg_get_constraintdef(c.oid) AS definition
        FROM pg_constraint AS c
        WHERE
            c.contype = 'f'
            AND c.confrelid IN ('projects'::REGCLASS, 'graph_nodes'::REGCLASS)
            AND pg_get_constraintdef(c.oid) LIKE '%ON UPDATE CASCADE%'
    LOOP
        -- pg_get_constraintdef spells an unvalidated constraint out with its
        -- own NOT VALID, so stripping the cascade leaves the rest as it is.
        restored := replace(reference.definition, ' ON UPDATE CASCADE', '');
        EXECUTE format(
            'ALTER TABLE %s DROP CONSTRAINT %I', reference.child, reference.name
        );
        BEGIN
            EXECUTE format(
                'ALTER TABLE %s ADD CONSTRAINT %I %s',
                reference.child, reference.name, restored
            );
        EXCEPTION WHEN foreign_key_violation THEN
            EXECUTE format(
                'ALTER TABLE %s ADD CONSTRAINT %I %s NOT VALID',
                reference.child, reference.name, restored
            );
        END;
    END LOOP;
END
$$;
-- +goose StatementEnd
