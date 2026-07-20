-- Add the durable, resumable full-transcript structure workflow.

SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '30s';

CREATE TABLE public.ai_factory_transcripts (
    id                    TEXT        PRIMARY KEY,
    title                 TEXT        NOT NULL,
    topic_text            TEXT,
    source_note           TEXT        NOT NULL,
    language_code         TEXT        NOT NULL
        CHECK (language_code IN (
            'yue-Hant-HK', 'zh-Hant', 'en', 'mixed', 'other'
        )),
    rights_basis          TEXT        NOT NULL
        CHECK (rights_basis IN (
            'own_work', 'permission', 'open_license',
            'public_domain', 'other'
        )),
    rights_confirmed_by   TEXT        NOT NULL,
    rights_confirmed_at   TIMESTAMPTZ NOT NULL,
    content_text          TEXT        NOT NULL,
    content_sha256        TEXT        NOT NULL,
    created_by            TEXT        NOT NULL,
    created_at            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    withdrawn_by          TEXT,
    withdrawn_at          TIMESTAMPTZ,
    withdrawal_reason     TEXT,
    CONSTRAINT ai_factory_transcripts_id_length
        CHECK (char_length(id) BETWEEN 1 AND 64),
    CONSTRAINT ai_factory_transcripts_metadata_lengths
        CHECK (
            char_length(title) BETWEEN 1 AND 200
            AND (topic_text IS NULL OR char_length(topic_text) <= 500)
            AND char_length(source_note) BETWEEN 1 AND 1000
            AND char_length(rights_confirmed_by) BETWEEN 1 AND 200
            AND char_length(created_by) BETWEEN 1 AND 200
        ),
    CONSTRAINT ai_factory_transcripts_content_length
        CHECK (char_length(content_text) BETWEEN 1 AND 200000),
    CONSTRAINT ai_factory_transcripts_content_hash
        CHECK (
            char_length(content_sha256) = 64
            AND content_sha256 = lower(content_sha256)
            AND content_sha256 ~ '^[0-9a-f]+$'
        ),
    CONSTRAINT ai_factory_transcripts_withdrawal_fields
        CHECK (
            (withdrawn_at IS NULL
                AND withdrawn_by IS NULL
                AND withdrawal_reason IS NULL)
            OR
            (withdrawn_at IS NOT NULL
                AND char_length(withdrawn_by) BETWEEN 1 AND 200
                AND char_length(withdrawal_reason) BETWEEN 1 AND 1000)
        )
);

CREATE INDEX idx_ai_factory_transcripts_active_created
    ON public.ai_factory_transcripts(created_at DESC)
    WHERE withdrawn_at IS NULL;

CREATE TABLE public.ai_factory_transcript_runs (
    id                      TEXT        PRIMARY KEY,
    transcript_id           TEXT        NOT NULL,
    recipe_key              TEXT        NOT NULL
        CHECK (recipe_key = 'transcript_structure_v1'),
    model_label             TEXT        NOT NULL,
    provider                TEXT        NOT NULL,
    provider_model          TEXT        NOT NULL,
    prompt_version          TEXT        NOT NULL,
    prompt_template_sha256  TEXT        NOT NULL,
    instruction_text        TEXT        NOT NULL DEFAULT '',
    window_count            SMALLINT    NOT NULL
        CHECK (window_count BETWEEN 1 AND 40),
    estimated_cost_hkd      NUMERIC(20, 8) NOT NULL DEFAULT 0
        CHECK (estimated_cost_hkd >= 0),
    status                  TEXT        NOT NULL DEFAULT 'draft'
        CHECK (status IN (
            'draft', 'processing', 'awaiting_review',
            'reviewed', 'failed', 'invalidated'
        )),
    preview_manifest_sha256 TEXT        NOT NULL,
    preview_expires_at      TIMESTAMPTZ NOT NULL,
    confirmation_version    TEXT,
    anonymization_confirmed BOOLEAN,
    rights_confirmed        BOOLEAN,
    third_party_confirmed   BOOLEAN,
    pii_warning_count       SMALLINT
        CHECK (pii_warning_count IS NULL OR pii_warning_count BETWEEN 0 AND 20),
    pii_override_reason     TEXT,
    confirmed_by            TEXT,
    confirmed_at            TIMESTAMPTZ,
    created_by              TEXT        NOT NULL,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    invalidated_by          TEXT,
    invalidated_at          TIMESTAMPTZ,
    invalidation_reason     TEXT,
    CONSTRAINT ai_factory_transcript_runs_id_length
        CHECK (char_length(id) BETWEEN 1 AND 64),
    CONSTRAINT ai_factory_transcript_runs_identity_lengths
        CHECK (
            char_length(model_label) BETWEEN 1 AND 200
            AND char_length(provider) BETWEEN 1 AND 80
            AND char_length(provider_model) BETWEEN 1 AND 200
            AND char_length(prompt_version) BETWEEN 1 AND 80
            AND char_length(instruction_text) <= 500
            AND char_length(created_by) BETWEEN 1 AND 200
        ),
    CONSTRAINT ai_factory_transcript_runs_hashes
        CHECK (
            char_length(prompt_template_sha256) = 64
            AND prompt_template_sha256 = lower(prompt_template_sha256)
            AND prompt_template_sha256 ~ '^[0-9a-f]+$'
            AND char_length(preview_manifest_sha256) = 64
            AND preview_manifest_sha256 = lower(preview_manifest_sha256)
            AND preview_manifest_sha256 ~ '^[0-9a-f]+$'
        ),
    CONSTRAINT ai_factory_transcript_runs_confirmation
        CHECK (
            (status IN ('draft', 'invalidated')
                AND confirmation_version IS NULL
                AND anonymization_confirmed IS NULL
                AND rights_confirmed IS NULL
                AND third_party_confirmed IS NULL
                AND pii_warning_count IS NULL
                AND pii_override_reason IS NULL
                AND confirmed_by IS NULL
                AND confirmed_at IS NULL)
            OR
            (status <> 'draft'
                AND char_length(confirmation_version) BETWEEN 1 AND 80
                AND anonymization_confirmed = TRUE
                AND rights_confirmed = TRUE
                AND third_party_confirmed = TRUE
                AND pii_warning_count IS NOT NULL
                AND char_length(confirmed_by) BETWEEN 1 AND 200
                AND confirmed_at IS NOT NULL
                AND confirmed_at <= preview_expires_at
                AND (
                    (pii_warning_count = 0 AND pii_override_reason IS NULL)
                    OR
                    (pii_warning_count > 0
                        AND char_length(pii_override_reason) BETWEEN 1 AND 1000)
                ))
        ),
    CONSTRAINT ai_factory_transcript_runs_invalidation_fields
        CHECK (
            (status <> 'invalidated'
                AND invalidated_by IS NULL
                AND invalidated_at IS NULL
                AND invalidation_reason IS NULL)
            OR
            (status = 'invalidated'
                AND char_length(invalidated_by) BETWEEN 1 AND 200
                AND invalidated_at IS NOT NULL
                AND char_length(invalidation_reason) BETWEEN 1 AND 1000)
        ),
    CONSTRAINT fk_ai_factory_transcript_runs_transcript
        FOREIGN KEY (transcript_id)
        REFERENCES public.ai_factory_transcripts(id) ON DELETE RESTRICT
);

CREATE INDEX idx_ai_factory_transcript_runs_transcript_created
    ON public.ai_factory_transcript_runs(transcript_id, created_at DESC);
CREATE INDEX idx_ai_factory_transcript_runs_status_updated
    ON public.ai_factory_transcript_runs(status, updated_at DESC);

CREATE TABLE public.ai_factory_transcript_windows (
    id                  TEXT        PRIMARY KEY,
    run_id              TEXT        NOT NULL,
    ordinal             SMALLINT    NOT NULL CHECK (ordinal BETWEEN 1 AND 40),
    context_start       INTEGER     NOT NULL CHECK (context_start >= 0),
    context_end         INTEGER     NOT NULL CHECK (context_end > context_start),
    core_start          INTEGER     NOT NULL CHECK (core_start >= context_start),
    core_end            INTEGER     NOT NULL CHECK (core_end > core_start),
    prompt_sha256       TEXT        NOT NULL,
    input_sha256        TEXT        NOT NULL,
    preview_sha256      TEXT        NOT NULL,
    status              TEXT        NOT NULL DEFAULT 'pending'
        CHECK (status IN (
            'pending', 'processing', 'succeeded', 'failed', 'discarded'
        )),
    attempt_count       SMALLINT    NOT NULL DEFAULT 0
        CHECK (attempt_count BETWEEN 0 AND 3),
    boundary_json       JSONB,
    boundary_sha256     TEXT,
    error_code          TEXT,
    started_at          TIMESTAMPTZ,
    completed_at        TIMESTAMPTZ,
    CONSTRAINT ai_factory_transcript_windows_id_length
        CHECK (char_length(id) BETWEEN 1 AND 64),
    CONSTRAINT ai_factory_transcript_windows_bounds
        CHECK (core_end <= context_end),
    CONSTRAINT ai_factory_transcript_windows_hashes
        CHECK (
            char_length(prompt_sha256) = 64
            AND prompt_sha256 = lower(prompt_sha256)
            AND prompt_sha256 ~ '^[0-9a-f]+$'
            AND char_length(input_sha256) = 64
            AND input_sha256 = lower(input_sha256)
            AND input_sha256 ~ '^[0-9a-f]+$'
            AND char_length(preview_sha256) = 64
            AND preview_sha256 = lower(preview_sha256)
            AND preview_sha256 ~ '^[0-9a-f]+$'
            AND (
                (boundary_json IS NULL AND boundary_sha256 IS NULL)
                OR
                (jsonb_typeof(boundary_json) = 'array'
                    AND char_length(boundary_sha256) = 64
                    AND boundary_sha256 = lower(boundary_sha256)
                    AND boundary_sha256 ~ '^[0-9a-f]+$')
            )
        ),
    CONSTRAINT ai_factory_transcript_windows_status_fields
        CHECK (
            (status = 'pending'
                AND started_at IS NULL
                AND completed_at IS NULL
                AND error_code IS NULL
                AND boundary_json IS NULL)
            OR
            (status = 'processing'
                AND started_at IS NOT NULL
                AND completed_at IS NULL
                AND error_code IS NULL
                AND boundary_json IS NULL)
            OR
            (status = 'succeeded'
                AND started_at IS NOT NULL
                AND completed_at IS NOT NULL
                AND error_code IS NULL
                AND boundary_json IS NOT NULL)
            OR
            (status IN ('failed', 'discarded')
                AND completed_at IS NOT NULL
                AND char_length(error_code) BETWEEN 1 AND 120)
        ),
    CONSTRAINT fk_ai_factory_transcript_windows_run
        FOREIGN KEY (run_id)
        REFERENCES public.ai_factory_transcript_runs(id) ON DELETE RESTRICT,
    UNIQUE (run_id, ordinal),
    UNIQUE (id, run_id)
);

CREATE INDEX idx_ai_factory_transcript_windows_run_status
    ON public.ai_factory_transcript_windows(run_id, status, ordinal);

CREATE TABLE public.ai_factory_transcript_attempts (
    id                        TEXT        PRIMARY KEY,
    run_id                    TEXT        NOT NULL,
    window_id                 TEXT        NOT NULL,
    attempt_no                SMALLINT    NOT NULL CHECK (attempt_no BETWEEN 1 AND 3),
    operation_id              TEXT        NOT NULL,
    model_label               TEXT        NOT NULL,
    provider                  TEXT        NOT NULL,
    provider_model            TEXT        NOT NULL,
    prompt_version            TEXT        NOT NULL,
    prompt_sha256             TEXT        NOT NULL,
    input_sha256              TEXT        NOT NULL,
    preview_sha256            TEXT        NOT NULL,
    estimated_cost_hkd        NUMERIC(20, 8) NOT NULL DEFAULT 0
        CHECK (estimated_cost_hkd >= 0),
    confirmed_by              TEXT        NOT NULL,
    confirmed_at              TIMESTAMPTZ NOT NULL,
    status                    TEXT        NOT NULL DEFAULT 'claimed'
        CHECK (status IN ('claimed', 'running', 'succeeded', 'failed', 'discarded')),
    provider_attempted_at     TIMESTAMPTZ,
    provider_request_id       TEXT,
    resolved_provider_model   TEXT,
    response_sha256           TEXT,
    response_bytes            INTEGER,
    error_code                TEXT,
    completed_at              TIMESTAMPTZ,
    created_at                TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT ai_factory_transcript_attempts_id_length
        CHECK (char_length(id) BETWEEN 1 AND 64),
    CONSTRAINT ai_factory_transcript_attempts_operation
        CHECK (operation_id = run_id),
    CONSTRAINT ai_factory_transcript_attempts_identity_lengths
        CHECK (
            char_length(model_label) BETWEEN 1 AND 200
            AND char_length(provider) BETWEEN 1 AND 80
            AND char_length(provider_model) BETWEEN 1 AND 200
            AND char_length(prompt_version) BETWEEN 1 AND 80
            AND char_length(confirmed_by) BETWEEN 1 AND 200
            AND (provider_request_id IS NULL OR char_length(provider_request_id) <= 300)
            AND (resolved_provider_model IS NULL
                OR char_length(resolved_provider_model) BETWEEN 1 AND 200)
        ),
    CONSTRAINT ai_factory_transcript_attempts_hashes
        CHECK (
            char_length(prompt_sha256) = 64
            AND prompt_sha256 = lower(prompt_sha256)
            AND prompt_sha256 ~ '^[0-9a-f]+$'
            AND char_length(input_sha256) = 64
            AND input_sha256 = lower(input_sha256)
            AND input_sha256 ~ '^[0-9a-f]+$'
            AND char_length(preview_sha256) = 64
            AND preview_sha256 = lower(preview_sha256)
            AND preview_sha256 ~ '^[0-9a-f]+$'
            AND (response_sha256 IS NULL OR (
                char_length(response_sha256) = 64
                AND response_sha256 = lower(response_sha256)
                AND response_sha256 ~ '^[0-9a-f]+$'
            ))
        ),
    CONSTRAINT ai_factory_transcript_attempts_response_size
        CHECK (response_bytes IS NULL OR response_bytes BETWEEN 0 AND 102400),
    CONSTRAINT ai_factory_transcript_attempts_status_fields
        CHECK (
            (status = 'claimed'
                AND provider_attempted_at IS NULL
                AND completed_at IS NULL
                AND error_code IS NULL)
            OR
            (status = 'running'
                AND provider_attempted_at IS NOT NULL
                AND completed_at IS NULL
                AND error_code IS NULL)
            OR
            (status = 'succeeded'
                AND provider_attempted_at IS NOT NULL
                AND completed_at IS NOT NULL
                AND response_sha256 IS NOT NULL
                AND response_bytes > 0
                AND error_code IS NULL)
            OR
            (status IN ('failed', 'discarded')
                AND completed_at IS NOT NULL
                AND char_length(error_code) BETWEEN 1 AND 120)
        ),
    CONSTRAINT fk_ai_factory_transcript_attempts_window_run
        FOREIGN KEY (window_id, run_id)
        REFERENCES public.ai_factory_transcript_windows(id, run_id) ON DELETE RESTRICT,
    UNIQUE (window_id, attempt_no)
);

CREATE INDEX idx_ai_factory_transcript_attempts_run_created
    ON public.ai_factory_transcript_attempts(run_id, created_at DESC);
CREATE INDEX idx_ai_factory_transcript_attempts_processing
    ON public.ai_factory_transcript_attempts(provider_attempted_at)
    WHERE status IN ('claimed', 'running');

CREATE TABLE public.ai_factory_transcript_segments (
    id                    TEXT        PRIMARY KEY,
    run_id                TEXT        NOT NULL,
    transcript_id         TEXT        NOT NULL,
    origin_window_id      TEXT        NOT NULL,
    start_offset          INTEGER     NOT NULL CHECK (start_offset >= 0),
    end_offset            INTEGER     NOT NULL CHECK (end_offset > start_offset),
    original_json         JSONB       NOT NULL,
    original_sha256       TEXT        NOT NULL,
    reviewed_json         JSONB,
    reviewed_sha256       TEXT,
    review_status         TEXT        NOT NULL DEFAULT 'pending'
        CHECK (review_status IN ('pending', 'approved', 'rejected')),
    review_note           TEXT,
    reviewed_by           TEXT,
    reviewed_at           TIMESTAMPTZ,
    approved_source_id    TEXT,
    created_at            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT ai_factory_transcript_segments_id_length
        CHECK (char_length(id) BETWEEN 1 AND 64),
    CONSTRAINT ai_factory_transcript_segments_json_objects
        CHECK (
            jsonb_typeof(original_json) = 'object'
            AND (reviewed_json IS NULL OR jsonb_typeof(reviewed_json) = 'object')
        ),
    CONSTRAINT ai_factory_transcript_segments_hashes
        CHECK (
            char_length(original_sha256) = 64
            AND original_sha256 = lower(original_sha256)
            AND original_sha256 ~ '^[0-9a-f]+$'
            AND (
                (reviewed_json IS NULL AND reviewed_sha256 IS NULL)
                OR
                (reviewed_json IS NOT NULL
                    AND char_length(reviewed_sha256) = 64
                    AND reviewed_sha256 = lower(reviewed_sha256)
                    AND reviewed_sha256 ~ '^[0-9a-f]+$')
            )
        ),
    CONSTRAINT ai_factory_transcript_segments_review_fields
        CHECK (
            (review_status = 'pending'
                AND reviewed_json IS NULL
                AND reviewed_sha256 IS NULL
                AND reviewed_by IS NULL
                AND reviewed_at IS NULL
                AND approved_source_id IS NULL)
            OR
            (review_status = 'approved'
                AND reviewed_json IS NOT NULL
                AND reviewed_sha256 IS NOT NULL
                AND char_length(reviewed_by) BETWEEN 1 AND 200
                AND reviewed_at IS NOT NULL
                AND approved_source_id IS NOT NULL)
            OR
            (review_status = 'rejected'
                AND char_length(reviewed_by) BETWEEN 1 AND 200
                AND reviewed_at IS NOT NULL
                AND approved_source_id IS NULL)
        ),
    CONSTRAINT ai_factory_transcript_segments_note_length
        CHECK (review_note IS NULL OR char_length(review_note) <= 2000),
    CONSTRAINT fk_ai_factory_transcript_segments_run
        FOREIGN KEY (run_id)
        REFERENCES public.ai_factory_transcript_runs(id) ON DELETE RESTRICT,
    CONSTRAINT fk_ai_factory_transcript_segments_transcript
        FOREIGN KEY (transcript_id)
        REFERENCES public.ai_factory_transcripts(id) ON DELETE RESTRICT,
    CONSTRAINT fk_ai_factory_transcript_segments_window
        FOREIGN KEY (origin_window_id)
        REFERENCES public.ai_factory_transcript_windows(id) ON DELETE RESTRICT,
    CONSTRAINT fk_ai_factory_transcript_segments_source
        FOREIGN KEY (approved_source_id)
        REFERENCES public.ai_factory_sources(id) ON DELETE RESTRICT,
    UNIQUE (run_id, start_offset)
);

CREATE INDEX idx_ai_factory_transcript_segments_review_queue
    ON public.ai_factory_transcript_segments(created_at, start_offset)
    WHERE review_status = 'pending';
CREATE INDEX idx_ai_factory_transcript_segments_run_offset
    ON public.ai_factory_transcript_segments(run_id, start_offset);

COMMENT ON TABLE public.ai_factory_sources IS
    'skhlmc-feature:data_factory:20260720_0009';

DROP INDEX IF EXISTS public.idx_ai_training_audit_created_at;
CREATE INDEX idx_ai_training_audit_created_at
    ON public.ai_training_audit(created_at)
    WHERE action NOT IN (
        'consent_granted',
        'consent_withdrawn',
        'submission_withdrawn',
        'factory_source_created',
        'factory_source_withdrawn',
        'factory_item_reviewed',
        'factory_item_withdrawn',
        'factory_item_invalidated',
        'factory_topic_tag_approved',
        'factory_topic_tag_retired',
        'factory_release_published',
        'factory_release_invalidated',
        'factory_transcript_withdrawn'
    );

REVOKE ALL PRIVILEGES ON TABLE public.ai_factory_transcripts FROM PUBLIC;
DO $$
DECLARE role_name TEXT;
BEGIN
    FOR role_name IN SELECT rolname FROM pg_roles
        WHERE rolname IN ('anon', 'authenticated')
    LOOP
        EXECUTE 'REVOKE ALL PRIVILEGES ON TABLE public.ai_factory_transcripts FROM '
            || quote_ident(role_name);
    END LOOP;
END $$;

REVOKE ALL PRIVILEGES ON TABLE public.ai_factory_transcript_runs FROM PUBLIC;
DO $$
DECLARE role_name TEXT;
BEGIN
    FOR role_name IN SELECT rolname FROM pg_roles
        WHERE rolname IN ('anon', 'authenticated')
    LOOP
        EXECUTE 'REVOKE ALL PRIVILEGES ON TABLE public.ai_factory_transcript_runs FROM '
            || quote_ident(role_name);
    END LOOP;
END $$;

REVOKE ALL PRIVILEGES ON TABLE public.ai_factory_transcript_windows FROM PUBLIC;
DO $$
DECLARE role_name TEXT;
BEGIN
    FOR role_name IN SELECT rolname FROM pg_roles
        WHERE rolname IN ('anon', 'authenticated')
    LOOP
        EXECUTE 'REVOKE ALL PRIVILEGES ON TABLE public.ai_factory_transcript_windows FROM '
            || quote_ident(role_name);
    END LOOP;
END $$;

REVOKE ALL PRIVILEGES ON TABLE public.ai_factory_transcript_attempts FROM PUBLIC;
DO $$
DECLARE role_name TEXT;
BEGIN
    FOR role_name IN SELECT rolname FROM pg_roles
        WHERE rolname IN ('anon', 'authenticated')
    LOOP
        EXECUTE 'REVOKE ALL PRIVILEGES ON TABLE public.ai_factory_transcript_attempts FROM '
            || quote_ident(role_name);
    END LOOP;
END $$;

REVOKE ALL PRIVILEGES ON TABLE public.ai_factory_transcript_segments FROM PUBLIC;
DO $$
DECLARE role_name TEXT;
BEGIN
    FOR role_name IN SELECT rolname FROM pg_roles
        WHERE rolname IN ('anon', 'authenticated')
    LOOP
        EXECUTE 'REVOKE ALL PRIVILEGES ON TABLE public.ai_factory_transcript_segments FROM '
            || quote_ident(role_name);
    END LOOP;
END $$;
