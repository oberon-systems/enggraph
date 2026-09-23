-- The lexical half reads chunks through one function, so the index and the
-- query split camelCase the same way: `PendingRefund` indexes pending, refund.

-- +goose NO TRANSACTION
-- +goose Up

-- +goose StatementBegin
CREATE OR REPLACE FUNCTION lexical_words(body TEXT) RETURNS TSVECTOR
LANGUAGE sql IMMUTABLE STRICT PARALLEL SAFE
AS $$
    SELECT to_tsvector(
        'simple'::REGCONFIG,
        regexp_replace(
            regexp_replace(body, '([a-z0-9])([A-Z])', '\1 \2', 'g'),
            '([A-Z]+)([A-Z][a-z])', '\1 \2', 'g'
        )
    )
$$;
-- +goose StatementEnd

CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_code_embeddings_words
ON code_embeddings USING gin (lexical_words(content_chunk));

DROP INDEX CONCURRENTLY IF EXISTS idx_code_embeddings_text;

-- +goose Down

CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_code_embeddings_text
ON code_embeddings USING gin (to_tsvector('simple', content_chunk));

DROP INDEX CONCURRENTLY IF EXISTS idx_code_embeddings_words;

DROP FUNCTION IF EXISTS lexical_words(TEXT);
