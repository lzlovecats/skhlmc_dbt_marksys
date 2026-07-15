-- Replace short-lived member/session quotas with audited monthly system-wide
-- infrastructure and provider budgets.

SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '30s';

CREATE TABLE public.monthly_resource_limits (
    period_month               DATE NOT NULL,
    limit_key                  TEXT NOT NULL,
    unit                       TEXT NOT NULL,
    warning_value              NUMERIC(20,4),
    stop_value                 NUMERIC(20,4),
    hard_value                 NUMERIC(20,4),
    allocated_hkd              NUMERIC(12,2),
    fx_hkd_per_usd             NUMERIC(12,6),
    funding_window_start       TIMESTAMPTZ,
    funding_window_end         TIMESTAMPTZ,
    external_cap_confirmed     BOOLEAN NOT NULL DEFAULT FALSE,
    external_cap_confirmed_by  TEXT,
    external_cap_confirmed_at  TIMESTAMPTZ,
    notified_by                TEXT,
    notified_at                TIMESTAMPTZ,
    notification_audit         JSONB NOT NULL DEFAULT '{}'::jsonb,
    updated_by                 TEXT,
    updated_at                 TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (period_month, limit_key),
    CONSTRAINT monthly_resource_limits_month_start
        CHECK (period_month = date_trunc('month', period_month)::date),
    CONSTRAINT monthly_resource_limits_key
        CHECK (limit_key IN ('render_bandwidth','r2_storage','ai_fund_available')
               OR (left(limit_key, 9) = 'provider:' AND length(limit_key) > 9)),
    CONSTRAINT monthly_resource_limits_nonnegative CHECK (
        COALESCE(warning_value, 0) >= 0 AND COALESCE(stop_value, 0) >= 0
        AND COALESCE(hard_value, 0) >= 0 AND COALESCE(allocated_hkd, 0) >= 0
        AND COALESCE(fx_hkd_per_usd, 0) >= 0
    ),
    CONSTRAINT monthly_resource_limits_render_order CHECK (
        limit_key <> 'render_bandwidth'
        OR (warning_value IS NOT NULL AND stop_value IS NOT NULL
            AND hard_value IS NOT NULL
            AND warning_value <= stop_value AND stop_value <= hard_value)
    ),
    CONSTRAINT fk_monthly_resource_limits_external_confirmer
        FOREIGN KEY (external_cap_confirmed_by) REFERENCES public.accounts(user_id)
        ON DELETE SET NULL,
    CONSTRAINT fk_monthly_resource_limits_notifier
        FOREIGN KEY (notified_by) REFERENCES public.accounts(user_id)
        ON DELETE SET NULL,
    CONSTRAINT fk_monthly_resource_limits_updater
        FOREIGN KEY (updated_by) REFERENCES public.accounts(user_id)
        ON DELETE SET NULL
);

CREATE INDEX idx_monthly_resource_limits_updated
    ON public.monthly_resource_limits(updated_at DESC);

ALTER TABLE public.bandwidth_usage_logs
    ADD COLUMN official_bucket_id TEXT,
    ADD COLUMN traffic_category TEXT,
    ADD COLUMN bucket_start TIMESTAMP,
    ADD COLUMN bucket_end TIMESTAMP,
    ADD COLUMN official_complete BOOLEAN NOT NULL DEFAULT FALSE;
CREATE UNIQUE INDEX idx_bandwidth_official_bucket
    ON public.bandwidth_usage_logs(official_bucket_id)
    WHERE official_bucket_id IS NOT NULL;

ALTER TABLE public.r2_upload_intents
    ADD COLUMN intent_metadata JSONB NOT NULL DEFAULT '{}'::jsonb;
ALTER INDEX IF EXISTS public.idx_r2_upload_intents_quota
    RENAME TO idx_r2_upload_intents_lifecycle;

INSERT INTO public.monthly_resource_limits
    (period_month, limit_key, unit, warning_value, stop_value, hard_value)
VALUES
    (date_trunc('month', NOW() AT TIME ZONE 'Asia/Hong_Kong')::date,
     'render_bandwidth', 'bytes', 3000000000, 3500000000, 4000000000),
    (date_trunc('month', NOW() AT TIME ZONE 'Asia/Hong_Kong')::date,
     'r2_storage', 'bytes', 7000000000, 8000000000, 8000000000)
ON CONFLICT (period_month, limit_key) DO NOTHING;

-- Retain only the current/future month and completed historical months whose
-- month-end is no more than 62 days old.
DELETE FROM public.monthly_resource_limits
WHERE period_month < date_trunc('month', NOW() AT TIME ZONE 'Asia/Hong_Kong')::date
  AND (period_month + INTERVAL '1 month')
      < (NOW() AT TIME ZONE 'Asia/Hong_Kong') - INTERVAL '62 days';

DROP TABLE IF EXISTS public.practice_daily_usage;
DROP TABLE IF EXISTS public.ai_coach_prepare_usage;
DELETE FROM public.app_config WHERE key = 'solo_quota_exemptions';
DELETE FROM public.app_config
WHERE key = 'bandwidth_developer_warning'
   OR left(key, 19) = 'bandwidth_3gb_push_';

REVOKE ALL PRIVILEGES ON TABLE public.monthly_resource_limits FROM PUBLIC;
DO $$
DECLARE role_name TEXT;
BEGIN
    FOR role_name IN SELECT rolname FROM pg_roles
        WHERE rolname IN ('anon', 'authenticated')
    LOOP
        EXECUTE 'REVOKE ALL PRIVILEGES ON TABLE public.monthly_resource_limits FROM '
            || quote_ident(role_name);
    END LOOP;
END $$;
