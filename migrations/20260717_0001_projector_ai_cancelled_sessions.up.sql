-- Give expired or operator-cancelled projector starts an explicit terminal state.

SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '30s';

ALTER TABLE public.projector_ai_sessions
    DROP CONSTRAINT projector_ai_sessions_status_check;

ALTER TABLE public.projector_ai_sessions
    ADD CONSTRAINT projector_ai_sessions_status_check
    CHECK (status IN (
        'start_requested', 'recording', 'stop_requested', 'processing',
        'ready', 'published', 'error', 'cancelled', 'cleared', 'expired'
    ));
