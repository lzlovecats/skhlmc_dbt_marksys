-- Remove direct browser access to the application catalog and provision a
-- non-login runtime privilege role.  Production continues to connect as the
-- owner until a separately authorised secret cutover grants this role to a
-- dedicated login.

SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '60s';

DO $migration$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_roles WHERE rolname='app_backend'
    ) THEN
        CREATE ROLE app_backend
            NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE
            NOINHERIT NOBYPASSRLS;
    END IF;
END
$migration$;

ALTER ROLE app_backend
    NOLOGIN NOCREATEDB NOCREATEROLE NOINHERIT;

REVOKE ALL PRIVILEGES ON SCHEMA public FROM PUBLIC;

DO $migration$
DECLARE
    role_name TEXT;
    object_name TEXT;
    object_kind "char";
BEGIN
    FOR role_name IN
        SELECT rolname
        FROM pg_roles
        WHERE rolname IN ('anon', 'authenticated')
    LOOP
        EXECUTE 'REVOKE ALL PRIVILEGES ON SCHEMA public FROM '
            || quote_ident(role_name);

        FOR object_name, object_kind IN
            SELECT c.relname, c.relkind
            FROM pg_class c
            JOIN pg_namespace n ON n.oid=c.relnamespace
            WHERE n.nspname='public'
              AND c.relkind IN ('r','p','v','m','f')
        LOOP
            EXECUTE 'REVOKE ALL PRIVILEGES ON TABLE public.'
                || quote_ident(object_name)
                || ' FROM ' || quote_ident(role_name);
        END LOOP;

        FOR object_name IN
            SELECT c.relname
            FROM pg_class c
            JOIN pg_namespace n ON n.oid=c.relnamespace
            WHERE n.nspname='public' AND c.relkind='S'
        LOOP
            EXECUTE 'REVOKE ALL PRIVILEGES ON SEQUENCE public.'
                || quote_ident(object_name)
                || ' FROM ' || quote_ident(role_name);
        END LOOP;
    END LOOP;
END
$migration$;

REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA public FROM PUBLIC;
REVOKE ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public FROM PUBLIC;
REVOKE EXECUTE ON ALL FUNCTIONS IN SCHEMA public FROM PUBLIC;

GRANT USAGE ON SCHEMA public TO app_backend;

DO $migration$
DECLARE
    object_name TEXT;
    object_kind "char";
BEGIN
    FOR object_name, object_kind IN
        SELECT c.relname, c.relkind
        FROM pg_class c
        JOIN pg_namespace n ON n.oid=c.relnamespace
        WHERE n.nspname='public'
          AND c.relkind IN ('r','p','v','m','f')
          AND c.relname<>'schema_migrations'
    LOOP
        IF object_kind IN ('v','m') THEN
            EXECUTE 'GRANT SELECT ON TABLE public.'
                || quote_ident(object_name) || ' TO app_backend';
        ELSE
            EXECUTE 'GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE public.'
                || quote_ident(object_name) || ' TO app_backend';
        END IF;
    END LOOP;

    FOR object_name IN
        SELECT c.relname
        FROM pg_class c
        JOIN pg_namespace n ON n.oid=c.relnamespace
        WHERE n.nspname='public' AND c.relkind='S'
    LOOP
        EXECUTE 'GRANT USAGE, SELECT ON SEQUENCE public.'
            || quote_ident(object_name) || ' TO app_backend';
    END LOOP;

    EXECUTE 'GRANT CONNECT ON DATABASE '
        || quote_ident(current_database()) || ' TO app_backend';
END
$migration$;

ALTER DEFAULT PRIVILEGES IN SCHEMA public
    REVOKE ALL PRIVILEGES ON TABLES FROM PUBLIC, anon, authenticated;
ALTER DEFAULT PRIVILEGES IN SCHEMA public
    REVOKE ALL PRIVILEGES ON SEQUENCES FROM PUBLIC, anon, authenticated;
ALTER DEFAULT PRIVILEGES IN SCHEMA public
    REVOKE EXECUTE ON FUNCTIONS FROM PUBLIC, anon, authenticated;
ALTER DEFAULT PRIVILEGES IN SCHEMA public
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO app_backend;
ALTER DEFAULT PRIVILEGES IN SCHEMA public
    GRANT USAGE, SELECT ON SEQUENCES TO app_backend;

REVOKE ALL PRIVILEGES ON TABLE public.schema_migrations FROM app_backend;
