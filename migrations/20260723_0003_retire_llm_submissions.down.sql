-- Restore only the retired table structure for application rollback. This
-- does not restore retired submission rows; use the separately retained
-- operational backup if a data restore is explicitly authorised.

SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '60s';

CREATE TABLE public.llm_training_submissions (
    id                    SERIAL      PRIMARY KEY,
    submitted_by          TEXT,
    data_type             TEXT        NOT NULL,
    title                 TEXT,
    topic_text            TEXT,
    side                  TEXT,
    content_text          TEXT        NOT NULL,
    source_note           TEXT,
    anonymized            BOOLEAN     DEFAULT FALSE,
    permission_confirmed  BOOLEAN     DEFAULT FALSE,
    ai_review_status      TEXT
        CHECK (ai_review_status IN ('passed', 'failed', 'error')),
    ai_review_json        TEXT,
    status                TEXT        DEFAULT 'pending'
        CHECK (status IN ('pending', 'accepted', 'rejected', 'withdrawn')),
    review_note           TEXT,
    reviewed_by           TEXT,
    reviewed_at           TIMESTAMP,
    created_at            TIMESTAMP   DEFAULT NOW(),
    CONSTRAINT fk_llm_training_submissions_submitter
        FOREIGN KEY (submitted_by) REFERENCES public.accounts(user_id)
        ON DELETE SET NULL,
    CONSTRAINT fk_llm_training_submissions_reviewer
        FOREIGN KEY (reviewed_by) REFERENCES public.accounts(user_id)
        ON DELETE SET NULL,
    CONSTRAINT llm_training_submissions_ai_review_status_check
        CHECK (ai_review_status IN ('passed', 'failed', 'error'))
);

CREATE INDEX idx_llm_training_status_created
    ON public.llm_training_submissions(status, created_at DESC);
CREATE INDEX idx_llm_training_submitter_created
    ON public.llm_training_submissions(submitted_by, created_at DESC);

REVOKE ALL PRIVILEGES ON TABLE public.llm_training_submissions
    FROM PUBLIC;
REVOKE ALL PRIVILEGES ON SEQUENCE
    public.llm_training_submissions_id_seq FROM PUBLIC;

DO $$
DECLARE
    role_name TEXT;
BEGIN
    FOR role_name IN
        SELECT rolname FROM pg_roles
        WHERE rolname IN ('anon', 'authenticated')
    LOOP
        EXECUTE 'REVOKE ALL PRIVILEGES ON TABLE public.llm_training_submissions FROM '
            || quote_ident(role_name);
        EXECUTE 'REVOKE ALL PRIVILEGES ON SEQUENCE public.llm_training_submissions_id_seq FROM '
            || quote_ident(role_name);
    END LOOP;
END $$;

GRANT SELECT, INSERT, UPDATE, DELETE
    ON TABLE public.llm_training_submissions TO app_backend;
GRANT USAGE, SELECT
    ON SEQUENCE public.llm_training_submissions_id_seq TO app_backend;
