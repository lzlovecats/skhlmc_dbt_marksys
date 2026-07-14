-- Retire the legacy untyped config bucket. Typed app_config has been the only
-- write path since the 4.2.x config migration; the 2026-07-14 inventory
-- confirmed every system_config key already exists in app_config (zero
-- fallback-only rows) and the remaining bridge readers tolerate a missing
-- table. Dropping it also removes the last pre-hardening anon/authenticated
-- table grants on config data.

SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '60s';

DO $migration$
DECLARE
    fallback_only BIGINT;
BEGIN
    IF to_regclass('public.system_config') IS NULL THEN
        RAISE EXCEPTION
            'system_config does not match the expected pre-migration schema';
    END IF;

    IF to_regclass('public.app_config') IS NULL THEN
        RAISE EXCEPTION
            'app_config must exist before system_config can be retired';
    END IF;

    SELECT COUNT(*)
    INTO fallback_only
    FROM public.system_config sc
    WHERE NOT EXISTS (
        SELECT 1 FROM public.app_config ac WHERE ac.key = sc.key
    );

    -- Driver-safe: this whole file (comments included) must avoid the
    -- percent character, because the runner executes it through
    -- exec_driver_sql and psycopg2 applies printf-style interpolation to the
    -- raw statement text.
    IF fallback_only <> 0 THEN
        RAISE EXCEPTION
            'refusing to drop system_config: keys exist only in the legacy table';
    END IF;
END
$migration$;

DROP TABLE public.system_config;
