-- Competition-day AI評判易 coordination state.
--
-- Full recordings never enter these tables.  The application encrypts the
-- short-lived transcript/result payload and optional TTS audio before insert;
-- only a bounded published summary is exposed through the public projector API.

CREATE TABLE projector_ai_sessions (
    session_id             TEXT PRIMARY KEY,
    display_key            TEXT NOT NULL,
    match_id               TEXT NOT NULL,
    status                 TEXT NOT NULL DEFAULT 'start_requested'
        CHECK (status IN (
            'start_requested', 'recording', 'stop_requested', 'processing',
            'ready', 'published', 'error', 'cleared', 'expired'
        )),
    status_detail          TEXT NOT NULL DEFAULT '',
    recording_started_at   TIMESTAMP,
    recording_duration_seconds DOUBLE PRECISION,
    recording_bytes        BIGINT CHECK (recording_bytes IS NULL OR recording_bytes >= 0),
    result_ciphertext      BYTEA,
    tts_audio_ciphertext   BYTEA,
    tts_mime               TEXT,
    tts_claim_token        TEXT,
    tts_status             TEXT NOT NULL DEFAULT 'not_requested'
        CHECK (tts_status IN (
            'not_requested', 'generating', 'unavailable', 'ready', 'playing',
            'played', 'stopped', 'failed'
        )),
    published              BOOLEAN NOT NULL DEFAULT FALSE,
    publish_revision       BIGINT NOT NULL DEFAULT 0 CHECK (publish_revision >= 0),
    result_expires_at      TIMESTAMP,
    created_at             TIMESTAMP NOT NULL DEFAULT NOW(),
    updated_at             TIMESTAMP NOT NULL DEFAULT NOW(),
    CONSTRAINT fk_projector_ai_session_match
        FOREIGN KEY (match_id) REFERENCES matches(match_id)
        ON DELETE RESTRICT
);

CREATE TABLE projector_ai_controls (
    display_key        TEXT PRIMARY KEY,
    current_session_id TEXT,
    command            TEXT NOT NULL DEFAULT '',
    command_revision   BIGINT NOT NULL DEFAULT 0 CHECK (command_revision >= 0),
    ack_revision       BIGINT NOT NULL DEFAULT 0 CHECK (ack_revision >= 0),
    kiosk_status       TEXT NOT NULL DEFAULT 'offline',
    status_detail      TEXT NOT NULL DEFAULT '',
    command_payload    JSONB NOT NULL DEFAULT '{}'::jsonb,
    hardware_status    JSONB NOT NULL DEFAULT '{}'::jsonb,
    capabilities       JSONB NOT NULL DEFAULT '{}'::jsonb,
    kiosk_last_seen_at TIMESTAMP,
    created_at         TIMESTAMP NOT NULL DEFAULT NOW(),
    updated_at         TIMESTAMP NOT NULL DEFAULT NOW(),
    CONSTRAINT fk_projector_ai_control_session
        FOREIGN KEY (current_session_id) REFERENCES projector_ai_sessions(session_id)
        ON DELETE SET NULL
);

CREATE TABLE projector_ai_markers (
    id             BIGSERIAL PRIMARY KEY,
    session_id     TEXT NOT NULL,
    offset_seconds DOUBLE PRECISION NOT NULL CHECK (offset_seconds >= 0),
    side           TEXT NOT NULL CHECK (side IN ('pro', 'con', 'both', 'unknown')),
    segment        TEXT NOT NULL,
    seg_index      INTEGER NOT NULL CHECK (seg_index >= 0),
    created_at     TIMESTAMP NOT NULL DEFAULT NOW(),
    CONSTRAINT fk_projector_ai_marker_session
        FOREIGN KEY (session_id) REFERENCES projector_ai_sessions(session_id)
        ON DELETE CASCADE
);

CREATE INDEX idx_projector_ai_sessions_display_updated
    ON projector_ai_sessions(display_key, updated_at DESC);
CREATE UNIQUE INDEX idx_projector_ai_sessions_one_active_display
    ON projector_ai_sessions(display_key)
    WHERE status IN ('start_requested','recording','stop_requested','processing');
CREATE INDEX idx_projector_ai_sessions_expiry
    ON projector_ai_sessions(result_expires_at)
    WHERE result_ciphertext IS NOT NULL;
CREATE INDEX idx_projector_ai_markers_session_time
    ON projector_ai_markers(session_id, offset_seconds, id);

REVOKE ALL PRIVILEGES ON TABLE
    projector_ai_sessions, projector_ai_controls, projector_ai_markers
    FROM PUBLIC;
REVOKE ALL PRIVILEGES ON SEQUENCE projector_ai_markers_id_seq
    FROM PUBLIC;

DO $$
DECLARE role_name TEXT;
BEGIN
    IF to_regrole('anon') IS NOT NULL
       AND to_regrole('authenticated') IS NOT NULL THEN
        -- Keep the complete policy statement explicit for offline catalog
        -- validation while still supporting plain PostgreSQL without roles.
        EXECUTE $revoke_tables$
            REVOKE ALL PRIVILEGES ON TABLE
                projector_ai_sessions, projector_ai_controls, projector_ai_markers
                FROM PUBLIC, anon, authenticated;
        $revoke_tables$;
        EXECUTE $revoke_sequence$
            REVOKE ALL PRIVILEGES ON SEQUENCE projector_ai_markers_id_seq
                FROM PUBLIC, anon, authenticated;
        $revoke_sequence$;
    ELSE
        FOR role_name IN
            SELECT rolname FROM pg_roles
            WHERE rolname IN ('anon', 'authenticated')
        LOOP
            EXECUTE
                'REVOKE ALL PRIVILEGES ON TABLE projector_ai_sessions, projector_ai_controls, projector_ai_markers FROM '
                || quote_ident(role_name);
            EXECUTE
                'REVOKE ALL PRIVILEGES ON SEQUENCE projector_ai_markers_id_seq FROM '
                || quote_ident(role_name);
        END LOOP;
    END IF;
END $$;
