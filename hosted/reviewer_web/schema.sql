-- The hosted reviewer site's database (Postgres). Applied by `scripts/hosted_admin.py init-db`;
-- every statement can be run again. Invented data only in examples; no content lives here.
--
-- DATA's section: reviewers, the published bundle, its images, the append-only events and the
-- anchor on the local log. The web app's own tables follow in WEB's section at the end.

CREATE TABLE IF NOT EXISTS reviewer (
    id               BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    alias            TEXT NOT NULL UNIQUE,          -- REMOTE-<n>: all that tools and logs print
    login            TEXT NOT NULL UNIQUE,          -- casefolded sign-in
    login_shown      TEXT NOT NULL,                 -- as the reviewers file spells it (records carry it)
    name             TEXT NOT NULL DEFAULT '',
    role             TEXT NOT NULL CHECK (role IN ('REVIEWER', 'APPROVER')),
    access           TEXT NOT NULL CHECK (access IN ('FULL', 'RESTRICTED')),
    scope_sequences  INT NOT NULL CHECK (scope_sequences >= 1),
    is_test          BOOLEAN NOT NULL DEFAULT FALSE,
    revoked_at       TIMESTAMPTZ,
    invited_at       TIMESTAMPTZ,
    first_sign_in_at TIMESTAMPTZ,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS bundle (
    bundle_id        TEXT PRIMARY KEY CHECK (bundle_id ~ '^[0-9a-f]{64}$'),
    manifest         BYTEA NOT NULL,
    sequences        BYTEA NOT NULL,
    items            BYTEA NOT NULL,
    posts            BYTEA NOT NULL,
    is_demo          BOOLEAN NOT NULL DEFAULT FALSE,
    source_sequences INT NOT NULL,                  -- sequences of the local bundle it was cut from
    scope_sequences  INT NOT NULL,                  -- the first that many are published with content
    published_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    active           BOOLEAN NOT NULL DEFAULT FALSE
);
CREATE UNIQUE INDEX IF NOT EXISTS bundle_one_active ON bundle (active) WHERE active;

CREATE TABLE IF NOT EXISTS media (
    bundle_id    TEXT NOT NULL REFERENCES bundle (bundle_id) ON DELETE CASCADE,
    name         TEXT NOT NULL,
    post_ref     TEXT NOT NULL,                     -- the first post that lists it
    content_type TEXT NOT NULL,
    bytes        BYTEA NOT NULL,
    sha256       TEXT NOT NULL,
    PRIMARY KEY (bundle_id, name)
);

-- One row per annotation line. LEGACY: a remote reviewer's own line of the local log, verbatim
-- (seq and prev_sha256 as in the line). HOSTED: written by the site, seq 1000001, 1000002, ...,
-- each chained on the previous HOSTED line and the first on chain_anchor.snapshot_head_sha256.
-- RETURN: an approver's local decision to return a reading, stored for its reviewer by the
-- operator tool; outside the hosted chain (seq above 2000000), never written by the site.
CREATE TABLE IF NOT EXISTS event (
    id              BIGSERIAL PRIMARY KEY,
    seq             INT NOT NULL UNIQUE,
    prev_sha256     TEXT NOT NULL CHECK (prev_sha256 ~ '^[0-9a-f]{64}$'),
    sha256          TEXT NOT NULL UNIQUE CHECK (sha256 ~ '^[0-9a-f]{64}$'),
    line            BYTEA NOT NULL,                 -- the exact line, without its newline
    reviewer_id     BIGINT NOT NULL REFERENCES reviewer (id),
    bundle_id       TEXT NOT NULL,
    item_id         TEXT NOT NULL,
    phase           TEXT NOT NULL,
    at              TIMESTAMPTZ NOT NULL,
    origin          TEXT NOT NULL CHECK (origin IN ('LEGACY', 'HOSTED', 'RETURN')),
    idempotency_key TEXT,                           -- on the FIRST line of a save only
    batch           INT NOT NULL,                   -- seq of the first line of the same save
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (reviewer_id, idempotency_key),
    CHECK (CASE origin WHEN 'LEGACY' THEN seq BETWEEN 1 AND 999999
                       WHEN 'HOSTED' THEN seq BETWEEN 1000001 AND 1999999
                       ELSE seq > 2000000 END),
    CHECK (batch <= seq)
);
CREATE INDEX IF NOT EXISTS event_by_reviewer ON event (reviewer_id, seq);

-- No update or delete of an event, ever (a later line supersedes, as in the local log).
CREATE OR REPLACE FUNCTION event_is_append_only() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'EVENT_IS_APPEND_ONLY' USING ERRCODE = 'restrict_violation';
END
$$;
CREATE OR REPLACE TRIGGER event_no_update_or_delete BEFORE UPDATE OR DELETE ON event
    FOR EACH ROW EXECUTE FUNCTION event_is_append_only();
CREATE OR REPLACE TRIGGER event_no_truncate BEFORE TRUNCATE ON event
    FOR EACH STATEMENT EXECUTE FUNCTION event_is_append_only();

-- The local log's state at the cutover: the hosted chain continues exactly that log.
CREATE TABLE IF NOT EXISTS chain_anchor (
    id                   INT PRIMARY KEY CHECK (id = 1),
    snapshot_count       INT NOT NULL CHECK (snapshot_count >= 0),
    snapshot_head_sha256 TEXT NOT NULL CHECK (snapshot_head_sha256 ~ '^[0-9a-f]{64}$'),
    snapshot_file_sha256 TEXT NOT NULL CHECK (snapshot_file_sha256 ~ '^[0-9a-f]{64}$'),
    imported_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------------------------------------
-- WEB's section (sign-in codes, sessions, rate limits) starts below this line.
-- ---------------------------------------------------------------------------------------------
