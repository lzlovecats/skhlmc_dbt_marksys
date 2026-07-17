-- Give every projector display one database-authoritative Kiosk owner lease.

SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '30s';

CREATE TABLE public.projector_kiosk_devices (
    device_id             TEXT PRIMARY KEY,
    label                 TEXT NOT NULL,
    enabled               BOOLEAN NOT NULL DEFAULT TRUE,
    credential_generation BIGINT NOT NULL DEFAULT 1
        CHECK (credential_generation >= 1),
    created_at            TIMESTAMP NOT NULL DEFAULT NOW(),
    last_seen_at          TIMESTAMP,
    revoked_at            TIMESTAMP,
    CHECK (CHAR_LENGTH(device_id) BETWEEN 20 AND 80),
    CHECK (CHAR_LENGTH(label) BETWEEN 1 AND 120)
);

ALTER TABLE public.projector_ai_controls
    ADD COLUMN lease_device_id TEXT,
    ADD COLUMN lease_client_id TEXT,
    ADD COLUMN lease_token_hash TEXT,
    ADD COLUMN lease_generation BIGINT NOT NULL DEFAULT 0
        CHECK (lease_generation >= 0),
    ADD COLUMN lease_expires_at TIMESTAMP,
    ADD COLUMN lease_last_seen_at TIMESTAMP,
    ADD COLUMN command_lease_generation BIGINT NOT NULL DEFAULT 0
        CHECK (command_lease_generation >= 0),
    ADD CONSTRAINT fk_projector_ai_control_lease_device
        FOREIGN KEY (lease_device_id)
        REFERENCES public.projector_kiosk_devices(device_id)
        ON DELETE SET NULL;

ALTER TABLE public.projector_ai_sessions
    ADD COLUMN kiosk_device_id TEXT,
    ADD COLUMN kiosk_lease_generation BIGINT
        CHECK (kiosk_lease_generation IS NULL OR kiosk_lease_generation >= 1),
    ADD CONSTRAINT fk_projector_ai_session_kiosk_device
        FOREIGN KEY (kiosk_device_id)
        REFERENCES public.projector_kiosk_devices(device_id)
        ON DELETE SET NULL;

ALTER TABLE public.projector_ai_sessions
    DROP CONSTRAINT projector_ai_sessions_status_check;

ALTER TABLE public.projector_ai_sessions
    ADD CONSTRAINT projector_ai_sessions_status_check
    CHECK (status IN (
        'start_requested', 'recording', 'stop_requested', 'processing',
        'ready', 'published', 'error', 'cancelled', 'interrupted',
        'cleared', 'expired'
    ));

CREATE INDEX idx_projector_kiosk_devices_last_seen
    ON public.projector_kiosk_devices(last_seen_at DESC);

REVOKE ALL PRIVILEGES ON TABLE public.projector_kiosk_devices FROM PUBLIC;

DO $$
DECLARE role_name TEXT;
BEGIN
    FOR role_name IN
        SELECT rolname FROM pg_roles
        WHERE rolname IN ('anon', 'authenticated')
    LOOP
        EXECUTE
            'REVOKE ALL PRIVILEGES ON TABLE public.projector_kiosk_devices FROM '
            || quote_ident(role_name);
    END LOOP;
END $$;
