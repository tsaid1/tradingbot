-- The hosted reviewer site's database (Postgres). Applied by `scripts/hosted_admin.py init-db`;
-- every statement can be run again. Invented data only in examples; no content lives here.
--
-- DATA's section: reviewers, the published bundles, their images, the append-only events and
-- the anchor on the local log. The web app's own tables follow in WEB's section at the end.

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
-- Two bundles may be active at once: the real one, and an invented one for test identities.
DROP INDEX IF EXISTS bundle_one_active;
CREATE UNIQUE INDEX IF NOT EXISTS bundle_one_active_real ON bundle (active)
    WHERE active AND NOT is_demo;
CREATE UNIQUE INDEX IF NOT EXISTS bundle_one_active_demo ON bundle (active)
    WHERE active AND is_demo;

-- The ONE bundle a reviewer works on: forms, images and records of no other bundle are served
-- to that reviewer. A test identity is bound to an invented bundle, the reviewers to the real
-- one. Assigned by the operator tool; once assigned it is never NULL again.
ALTER TABLE reviewer ADD COLUMN IF NOT EXISTS bundle_id TEXT REFERENCES bundle (bundle_id);
CREATE OR REPLACE FUNCTION reviewer_keeps_a_bundle() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF OLD.bundle_id IS NOT NULL AND NEW.bundle_id IS NULL THEN
        RAISE EXCEPTION 'REVIEWER_KEEPS_A_BUNDLE' USING ERRCODE = 'restrict_violation';
    END IF;
    RETURN NEW;
END
$$;
CREATE OR REPLACE TRIGGER reviewer_bundle_stays_assigned BEFORE UPDATE ON reviewer
    FOR EACH ROW EXECUTE FUNCTION reviewer_keeps_a_bundle();

-- The address a sign-in code is mailed to is not the identity whose lines are written. ``login``
-- (casefolded ``login_shown``) is the identity: what its records carry. ``sign_in`` (casefolded)
-- and ``address`` (as given) are where its codes go. One address may have TWO identities, one of
-- each kind: the owner's real reviewer identity and a test identity. A database made by an
-- earlier version of this file gets its rows' own login as their address.
ALTER TABLE reviewer ADD COLUMN IF NOT EXISTS sign_in TEXT;
ALTER TABLE reviewer ADD COLUMN IF NOT EXISTS address TEXT;
UPDATE reviewer SET sign_in = login, address = login_shown WHERE sign_in IS NULL OR address IS NULL;
ALTER TABLE reviewer ALTER COLUMN sign_in SET NOT NULL;
ALTER TABLE reviewer ALTER COLUMN address SET NOT NULL;
-- A row written without an address (an older operator tool) has its login as its address.
CREATE OR REPLACE FUNCTION reviewer_address_defaults() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    NEW.address := COALESCE(NEW.address, NEW.login_shown);
    NEW.sign_in := COALESCE(NEW.sign_in, NEW.login);
    RETURN NEW;
END
$$;
CREATE OR REPLACE TRIGGER reviewer_address_by_default BEFORE INSERT ON reviewer
    FOR EACH ROW EXECUTE FUNCTION reviewer_address_defaults();
-- When an import-log of the local log last considered this row: until then a reviewer row (not
-- a test identity) is closed for saving, whenever it was written, so that its local history is
-- on the site before anything continues it.
ALTER TABLE reviewer ADD COLUMN IF NOT EXISTS imported_at TIMESTAMPTZ;
CREATE UNIQUE INDEX IF NOT EXISTS reviewer_one_of_each_kind_per_sign_in
    ON reviewer (sign_in, is_test);

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
-- TEST: a line of a test identity, written by the site: a stream of its own (seq 3000001, ...,
-- chained among themselves from a fixed value). It never touches the reviewers' chain or its
-- anchor, and is never imported, pulled or pushed.
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
    origin          TEXT NOT NULL,
    idempotency_key TEXT,                           -- on the FIRST line of a save only
    batch           INT NOT NULL,                   -- seq of the first line of the same save
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (reviewer_id, idempotency_key),
    CONSTRAINT event_batch CHECK (batch <= seq)
);
CREATE INDEX IF NOT EXISTS event_by_reviewer ON event (reviewer_id, seq);
-- The streams. Stated again at every init-db, so that a database made by an earlier version of
-- this file gets the present rule (the three DROPs name that version's constraints).
ALTER TABLE event DROP CONSTRAINT IF EXISTS event_origin_check;
ALTER TABLE event DROP CONSTRAINT IF EXISTS event_check;
ALTER TABLE event DROP CONSTRAINT IF EXISTS event_stream;
ALTER TABLE event ADD CONSTRAINT event_stream CHECK (
    CASE origin WHEN 'LEGACY' THEN seq BETWEEN 1 AND 999999
                WHEN 'HOSTED' THEN seq BETWEEN 1000001 AND 1999999
                WHEN 'RETURN' THEN seq BETWEEN 2000001 AND 2999999
                WHEN 'TEST' THEN seq > 3000000
                ELSE FALSE END);
-- A test identity's lines are TEST lines and no one else's are: whoever inserts.
CREATE OR REPLACE FUNCTION event_in_its_reviewer_s_stream() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF (NEW.origin = 'TEST') IS DISTINCT FROM
            (SELECT is_test FROM reviewer WHERE id = NEW.reviewer_id) THEN
        RAISE EXCEPTION 'EVENT_NOT_IN_ITS_REVIEWER_S_STREAM' USING ERRCODE = 'restrict_violation';
    END IF;
    -- The site writes HOSTED and TEST lines only. LEGACY and RETURN lines are the operator's,
    -- who owns the table: a role that does not (the site's own) may not add one.
    IF NEW.origin NOT IN ('HOSTED', 'TEST') AND NOT pg_has_role(
            current_user, (SELECT relowner FROM pg_class WHERE oid = TG_RELID), 'USAGE') THEN
        RAISE EXCEPTION 'EVENT_ORIGIN_IS_THE_OPERATOR_S' USING ERRCODE = 'insufficient_privilege';
    END IF;
    RETURN NEW;
END
$$;
CREATE OR REPLACE TRIGGER event_stream_of_its_reviewer BEFORE INSERT ON event
    FOR EACH ROW EXECUTE FUNCTION event_in_its_reviewer_s_stream();

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

-- Who wrote the anchor: IMPORT (import-log of the local log whose first lines hash to
-- snapshot_file_sha256), RESTORE (restore of an export), SEED (the acceptance seed of a
-- preview); NULL for an anchor written by an earlier version. import-log, pull-to-local and
-- push-returns continue only an anchor of IMPORT or RESTORE.
ALTER TABLE chain_anchor ADD COLUMN IF NOT EXISTS written_by TEXT
    CHECK (written_by IN ('IMPORT', 'RESTORE', 'SEED'));

-- A database initialised as a PREVIEW (init-db --preview): only there may the acceptance seed
-- write. Production is initialised without it.
CREATE TABLE IF NOT EXISTS database_kind (
    id        INT PRIMARY KEY CHECK (id = 1),
    kind      TEXT NOT NULL CHECK (kind = 'PREVIEW'),
    marked_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------------------------------------
-- WEB's section (sign-in codes, sessions, rate limits, queued mail) starts below this line.
-- ---------------------------------------------------------------------------------------------
-- Sign-in codes, sessions and rate limits of the web app (hosted/reviewer_web/auth.py).
-- No address, code or token is stored: a code as HMAC-SHA256 under the server's secret, a session
-- token as SHA-256, a limit bucket as a keyed hash. Every statement can be run again.

CREATE TABLE IF NOT EXISTS login_code (
    id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    reviewer_id BIGINT NOT NULL REFERENCES reviewer (id) ON DELETE CASCADE,
    code_hmac   TEXT NOT NULL CHECK (code_hmac ~ '^[0-9a-f]{64}$'),
    created_at  TIMESTAMPTZ NOT NULL,
    expires_at  TIMESTAMPTZ NOT NULL,          -- ten minutes after created_at
    attempts    INT NOT NULL DEFAULT 0,        -- every try, right or wrong; at most five
    used_at     TIMESTAMPTZ                    -- set by the sign-in, or by a newer code
);
CREATE INDEX IF NOT EXISTS login_code_by_reviewer ON login_code (reviewer_id, id);
-- The browser of a code's first successful sign-in may repeat it for a minute (a second press of
-- "Sign in" whose first answer it dropped): the SHA-256 of the cookie the code request was
-- answered with, kept at that sign-in only when the signing-in browser carried it (else NULL:
-- no repeat), and cleared by a wrong code in the minute; when this code signed in. The column
-- delivered_sha256 is no longer written (sessions of one code end at the first use of one).
ALTER TABLE login_code ADD COLUMN IF NOT EXISTS browser_sha256 TEXT;
ALTER TABLE login_code ADD COLUMN IF NOT EXISTS signed_in_at TIMESTAMPTZ;
ALTER TABLE login_code ADD COLUMN IF NOT EXISTS delivered_sha256 TEXT;

CREATE TABLE IF NOT EXISTS web_session (
    token_sha256 TEXT PRIMARY KEY CHECK (token_sha256 ~ '^[0-9a-f]{64}$'),
    reviewer_id  BIGINT NOT NULL REFERENCES reviewer (id) ON DELETE CASCADE,
    created_at   TIMESTAMPTZ NOT NULL,
    last_seen_at TIMESTAMPTZ NOT NULL,
    expires_at   TIMESTAMPTZ NOT NULL          -- thirty days after last_seen_at (sliding),
                                               -- never later than ninety days after created_at
);
CREATE INDEX IF NOT EXISTS web_session_by_reviewer ON web_session (reviewer_id);
-- The code that made a session: every session one code made stays live until one of them is
-- used; that first use ends the others (the browser kept one answer, whichever it was).
ALTER TABLE web_session ADD COLUMN IF NOT EXISTS from_code BIGINT;

CREATE TABLE IF NOT EXISTS rate_limit (
    id     BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    bucket TEXT NOT NULL,                      -- 'to:', 'net:' or 'try:' and a keyed hash
    at     TIMESTAMPTZ NOT NULL
);
CREATE INDEX IF NOT EXISTS rate_limit_by_bucket ON rate_limit (bucket, at);

-- A form's DRAFT: what the page last sent for one reviewer's form in one phase, kept by the
-- applicability round trip and by a refused save, deleted in the transaction of the form's
-- successful save. Never an event: never exported, pulled, counted or shown to anybody else; a
-- draft of another phase than the form's current one is dropped when the form is opened.
CREATE TABLE IF NOT EXISTS draft (
    reviewer_id BIGINT NOT NULL REFERENCES reviewer (id) ON DELETE CASCADE,
    bundle_id   TEXT NOT NULL,
    item_id     TEXT NOT NULL,
    phase       TEXT NOT NULL CHECK (phase IN ('INDEPENDENT', 'AFTER_REVEAL')),
    fields      JSONB NOT NULL,                -- [[name, value], ...] as posted
    saved_at    TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (reviewer_id, bundle_id, item_id)
);

-- After a right code for an address with two identities: the choice of the identity to open,
-- made only when a right code was entered (it holds the address's key row and its times, not the
-- code). Single use, five minutes, held by its browser in a cookie of its
-- own; only the token's SHA-256 is stored. ``sign_in_key``: the address's key row (the lowest id
-- of the rows of that address), under which its codes and counters are kept.
CREATE TABLE IF NOT EXISTS sign_in_choice (
    token_sha256 TEXT PRIMARY KEY CHECK (token_sha256 ~ '^[0-9a-f]{64}$'),
    sign_in_key  BIGINT NOT NULL REFERENCES reviewer (id) ON DELETE CASCADE,
    created_at   TIMESTAMPTZ NOT NULL,
    expires_at   TIMESTAMPTZ NOT NULL,
    used_at      TIMESTAMPTZ
);

-- Mail the operator asks the site to send (the mail password exists only where the site runs):
-- one invitation per row. No address and no text is stored: the reviewer's row gives the address
-- when the site sends, the kind gives the text. Written by the operator tool's `invite`, sent by
-- the site's own sender; not part of an export.
CREATE TABLE IF NOT EXISTS mail_queue (
    id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    reviewer_id BIGINT NOT NULL REFERENCES reviewer (id) ON DELETE CASCADE,
    kind        TEXT NOT NULL CHECK (kind IN ('INVITE_CONTINUE', 'INVITE_START')),
    queued_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    sent_at     TIMESTAMPTZ,                   -- set when the message went out
    result      TEXT,                          -- SENT, or the constant code of the last failure
    attempts    INT NOT NULL DEFAULT 0 CHECK (attempts BETWEEN 0 AND 3)
);
-- One row of a kind waits per reviewer (not sent, tries left).
CREATE UNIQUE INDEX IF NOT EXISTS mail_queue_one_waiting ON mail_queue (reviewer_id, kind)
    WHERE sent_at IS NULL AND attempts < 3;

-- A try is counted, and its start noted, BEFORE the message is handed to the mail server. A row
-- whose sending began and whose outcome was never recorded (the site was stopped in between) is
-- not sent again by itself: the operator asks the reviewer and queues it again.
ALTER TABLE mail_queue ADD COLUMN IF NOT EXISTS sending_at TIMESTAMPTZ;
DROP INDEX IF EXISTS mail_queue_one_waiting;
CREATE UNIQUE INDEX IF NOT EXISTS mail_queue_one_to_send ON mail_queue (reviewer_id, kind)
    WHERE sent_at IS NULL AND attempts < 3 AND (sending_at IS NULL OR result IS NOT NULL);
