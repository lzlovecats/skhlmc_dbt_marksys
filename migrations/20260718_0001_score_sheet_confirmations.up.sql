-- Add side-specific score-sheet confirmation links and acknowledgement state.

SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '30s';

CREATE TABLE public.score_sheet_confirmations (
    match_id               TEXT,
    side                   TEXT        CHECK (side IN ('pro', 'con')),
    confirmation_token     TEXT        NOT NULL UNIQUE,
    status                 TEXT        NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'confirmed', 'disputed')),
    dispute_reason         TEXT        NOT NULL DEFAULT '',
    opened_score_count     INTEGER     NOT NULL CHECK (opened_score_count > 0),
    opened_at              TIMESTAMP   NOT NULL,
    responded_at           TIMESTAMP,
    PRIMARY KEY (match_id, side),
    CONSTRAINT score_sheet_confirmations_reason_check
        CHECK (
            char_length(dispute_reason) <= 2000
            AND (status <> 'disputed' OR btrim(dispute_reason) <> '')
        ),
    CONSTRAINT fk_score_sheet_confirmations_match
        FOREIGN KEY (match_id) REFERENCES public.matches(match_id)
        ON DELETE CASCADE
);

REVOKE ALL PRIVILEGES ON TABLE public.score_sheet_confirmations FROM PUBLIC;
DO $$
DECLARE role_name TEXT;
BEGIN
    FOR role_name IN
        SELECT rolname FROM pg_roles
        WHERE rolname IN ('anon', 'authenticated')
    LOOP
        EXECUTE 'REVOKE ALL PRIVILEGES ON TABLE public.score_sheet_confirmations FROM '
            || quote_ident(role_name);
    END LOOP;
END $$;
