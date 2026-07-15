-- Restore empty compatibility structures only.  Discarded quota events have
-- no durable value and are intentionally not reconstructed.

SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '30s';

CREATE TABLE public.practice_daily_usage (
    user_id       TEXT,
    practice_kind TEXT CHECK (practice_kind IN ('multiplayer_free', 'multiplayer_mock')),
    usage_date    DATE,
    room_code     TEXT NOT NULL,
    created_at    TIMESTAMP DEFAULT NOW(),
    PRIMARY KEY (user_id, practice_kind, usage_date),
    CONSTRAINT fk_practice_daily_usage_user
        FOREIGN KEY (user_id) REFERENCES public.accounts(user_id)
        ON DELETE CASCADE
);

CREATE TABLE public.ai_coach_prepare_usage (
    id         BIGSERIAL PRIMARY KEY,
    user_id    TEXT NOT NULL,
    created_at TIMESTAMP NOT NULL,
    CONSTRAINT fk_ai_coach_prepare_usage_user
        FOREIGN KEY (user_id) REFERENCES public.accounts(user_id)
        ON DELETE CASCADE
);
CREATE INDEX idx_ai_coach_prepare_usage_user_created
    ON public.ai_coach_prepare_usage(user_id, created_at DESC);

INSERT INTO public.app_config
    (key, namespace, value, value_type, is_secret, updated_at)
VALUES
    ('solo_quota_exemptions', 'access', '{}'::jsonb, 'object', FALSE, NOW())
ON CONFLICT (key) DO NOTHING;

REVOKE ALL PRIVILEGES ON TABLE public.practice_daily_usage FROM PUBLIC;
REVOKE ALL PRIVILEGES ON TABLE public.ai_coach_prepare_usage FROM PUBLIC;
DO $$
DECLARE role_name TEXT;
BEGIN
    FOR role_name IN SELECT rolname FROM pg_roles
        WHERE rolname IN ('anon', 'authenticated')
    LOOP
        EXECUTE 'REVOKE ALL PRIVILEGES ON TABLE public.practice_daily_usage FROM '
            || quote_ident(role_name);
        EXECUTE 'REVOKE ALL PRIVILEGES ON TABLE public.ai_coach_prepare_usage FROM '
            || quote_ident(role_name);
    END LOOP;
END $$;

DROP TABLE public.monthly_resource_limits;

DROP INDEX IF EXISTS public.idx_bandwidth_official_bucket;
ALTER TABLE public.bandwidth_usage_logs
    DROP COLUMN official_complete,
    DROP COLUMN bucket_end,
    DROP COLUMN bucket_start,
    DROP COLUMN traffic_category,
    DROP COLUMN official_bucket_id;
ALTER TABLE public.r2_upload_intents DROP COLUMN intent_metadata;
ALTER INDEX IF EXISTS public.idx_r2_upload_intents_lifecycle
    RENAME TO idx_r2_upload_intents_quota;
