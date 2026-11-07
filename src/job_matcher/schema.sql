CREATE EXTENSION IF NOT EXISTS vector;

-- array_to_string is only STABLE, and generated columns need IMMUTABLE expressions.
CREATE OR REPLACE FUNCTION jm_join(text[]) RETURNS text
    LANGUAGE sql IMMUTABLE PARALLEL SAFE AS $$ SELECT array_to_string($1, ' ') $$;

CREATE TABLE IF NOT EXISTS offers (
    id            text PRIMARY KEY,
    source        text NOT NULL,
    url           text NOT NULL,
    company       text NOT NULL,
    title         text NOT NULL,
    seniority     text,
    locations     text[] NOT NULL DEFAULT '{}',
    remote        text,
    salary        jsonb NOT NULL DEFAULT '[]',
    skills_must   text[] NOT NULL DEFAULT '{}',
    skills_nice   text[] NOT NULL DEFAULT '{}',
    description   text NOT NULL DEFAULT '',
    published_at  timestamptz,
    first_seen    timestamptz,
    last_seen     timestamptz,
    closed        boolean NOT NULL DEFAULT false,
    -- Full-text side of hybrid search. 'simple' because offers mix Polish and English
    -- and Postgres ships no Polish stemmer.
    tsv tsvector GENERATED ALWAYS AS (
        setweight(to_tsvector('simple', title), 'A') ||
        setweight(to_tsvector('simple', jm_join(skills_must || skills_nice)), 'B') ||
        setweight(to_tsvector('simple', description), 'C')
    ) STORED
);
CREATE INDEX IF NOT EXISTS offers_tsv ON offers USING gin (tsv);

-- Structured fields an LLM pulled out of the ad; one row per extractor so providers can be compared.
CREATE TABLE IF NOT EXISTS extractions (
    offer_id    text NOT NULL REFERENCES offers(id) ON DELETE CASCADE,
    extractor   text NOT NULL,          -- e.g. "openai:gpt-5-mini", "gemini:gemini-3.8-flash"
    data        jsonb NOT NULL,
    created_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (offer_id, extractor)
);

-- One vector per offer per embedding model. Dimensions differ between models, so the column
-- is untyped; at a few thousand rows an exact scan is fast and needs no ANN index.
CREATE TABLE IF NOT EXISTS embeddings (
    offer_id   text NOT NULL REFERENCES offers(id) ON DELETE CASCADE,
    model      text NOT NULL,
    embedding  vector NOT NULL,
    PRIMARY KEY (offer_id, model)
);
