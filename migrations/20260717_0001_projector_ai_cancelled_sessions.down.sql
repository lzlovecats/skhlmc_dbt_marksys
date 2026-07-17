-- Map cancelled starts to the previous generic terminal state before rollback.

SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '30s';

UPDATE public.projector_ai_sessions
SET status='error',
    status_detail=CASE
        WHEN status_detail='' THEN '開始錄音已取消'
        ELSE status_detail
    END,
    updated_at=NOW()
WHERE status='cancelled';

ALTER TABLE public.projector_ai_sessions
    DROP CONSTRAINT projector_ai_sessions_status_check;

ALTER TABLE public.projector_ai_sessions
    ADD CONSTRAINT projector_ai_sessions_status_check
    CHECK (status IN (
        'start_requested', 'recording', 'stop_requested', 'processing',
        'ready', 'published', 'error', 'cleared', 'expired'
    ));
