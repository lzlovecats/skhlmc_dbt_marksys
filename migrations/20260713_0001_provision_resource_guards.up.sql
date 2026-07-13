-- Additive cost/quota guard schema. Apply only after staging forward/rollback
-- verification against the production baseline.

CREATE TABLE practice_daily_usage (
    user_id       TEXT,
    practice_kind TEXT CHECK (
        practice_kind IN ('multiplayer_free', 'multiplayer_mock')
    ),
    usage_date    DATE,
    room_code     TEXT NOT NULL,
    created_at    TIMESTAMP DEFAULT NOW(),
    PRIMARY KEY (user_id, practice_kind, usage_date),
    CONSTRAINT fk_practice_daily_usage_user
        FOREIGN KEY (user_id) REFERENCES accounts(user_id)
        ON DELETE CASCADE
);

CREATE TABLE bandwidth_usage_logs (
    id          BIGSERIAL PRIMARY KEY,
    source      TEXT NOT NULL,
    user_id     TEXT,
    bytes_out   BIGINT NOT NULL CHECK (bytes_out >= 0),
    details     TEXT,
    created_at  TIMESTAMP DEFAULT NOW(),
    CONSTRAINT fk_bandwidth_usage_user
        FOREIGN KEY (user_id) REFERENCES accounts(user_id)
        ON DELETE SET NULL
);

CREATE TABLE r2_upload_intents (
    intent_id       TEXT PRIMARY KEY,
    user_id         TEXT NOT NULL,
    media_kind      TEXT NOT NULL,
    object_keys     TEXT NOT NULL,
    declared_bytes  BIGINT NOT NULL CHECK (declared_bytes > 0),
    status          TEXT NOT NULL DEFAULT 'issued',
    created_at      TIMESTAMP NOT NULL DEFAULT NOW(),
    completed_at    TIMESTAMP,
    CONSTRAINT fk_r2_upload_intent_user
        FOREIGN KEY (user_id) REFERENCES accounts(user_id)
        ON DELETE CASCADE
);

CREATE TABLE ai_coach_prepare_usage (
    id         BIGSERIAL PRIMARY KEY,
    user_id    TEXT NOT NULL,
    created_at TIMESTAMP NOT NULL,
    CONSTRAINT fk_ai_coach_prepare_usage_user
        FOREIGN KEY (user_id) REFERENCES accounts(user_id)
        ON DELETE CASCADE
);

CREATE INDEX idx_bandwidth_usage_created
    ON bandwidth_usage_logs(created_at DESC);

CREATE INDEX idx_r2_upload_intents_quota
    ON r2_upload_intents(media_kind, user_id, created_at DESC);

CREATE INDEX idx_ai_coach_prepare_usage_user_created
    ON ai_coach_prepare_usage(user_id, created_at DESC);
