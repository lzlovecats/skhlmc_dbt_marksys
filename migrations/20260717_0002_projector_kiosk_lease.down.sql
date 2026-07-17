-- Remove projector Kiosk ownership leases and map interrupted sessions safely.

SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '30s';

UPDATE public.projector_ai_sessions
SET status='error',
    status_detail=CASE
        WHEN status_detail='' THEN 'Kiosk 擁有權已中斷'
        ELSE status_detail
    END,
    updated_at=NOW()
WHERE status='interrupted';

ALTER TABLE public.projector_ai_sessions
    DROP CONSTRAINT projector_ai_sessions_status_check;

ALTER TABLE public.projector_ai_sessions
    ADD CONSTRAINT projector_ai_sessions_status_check
    CHECK (status IN (
        'start_requested', 'recording', 'stop_requested', 'processing',
        'ready', 'published', 'error', 'cancelled', 'cleared', 'expired'
    ));

ALTER TABLE public.projector_ai_sessions
    DROP CONSTRAINT fk_projector_ai_session_kiosk_device,
    DROP COLUMN kiosk_lease_generation,
    DROP COLUMN kiosk_device_id;

ALTER TABLE public.projector_ai_controls
    DROP CONSTRAINT fk_projector_ai_control_lease_device,
    DROP COLUMN command_lease_generation,
    DROP COLUMN lease_last_seen_at,
    DROP COLUMN lease_expires_at,
    DROP COLUMN lease_generation,
    DROP COLUMN lease_token_hash,
    DROP COLUMN lease_client_id,
    DROP COLUMN lease_device_id;

DROP TABLE public.projector_kiosk_devices;
