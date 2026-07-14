-- Rebuild the legacy read-fallback table from the typed store. The up
-- migration proved every legacy key already existed in app_config, so
-- backfilling from app_config restores equivalent bridge data (values in
-- their JSON scalar text form). The pre-hardening PUBLIC/anon/authenticated
-- grants are deliberately not restored.

SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '60s';

CREATE TABLE public.system_config (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT
);

REVOKE ALL PRIVILEGES ON TABLE
    system_config
FROM PUBLIC, anon, authenticated;

INSERT INTO public.system_config (key, value, updated_at)
SELECT key, COALESCE(value #>> '{}', ''), updated_at::text
FROM public.app_config;
