-- Provision the reviewed V0 debate data factory. This bundle deliberately
-- excludes embeddings, pgvector, model registration and evaluation workers.

SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '30s';

CREATE TABLE public.ai_factory_sources (
    id                    TEXT        PRIMARY KEY,
    source_group_id       TEXT        NOT NULL,
    revision_no           INTEGER     NOT NULL CHECK (revision_no > 0),
    supersedes_source_id  TEXT,
    source_kind           TEXT        NOT NULL
        CHECK (source_kind IN ('llm_submission', 'admin_paste')),
    origin_submission_id  INTEGER,
    data_type             TEXT        NOT NULL,
    title                 TEXT,
    topic_text            TEXT,
    side                  TEXT,
    source_note           TEXT,
    language_code         TEXT        NOT NULL
        CHECK (language_code IN (
            'yue-Hant-HK', 'zh-Hant', 'en', 'mixed', 'other'
        )),
    rights_basis          TEXT        NOT NULL
        CHECK (rights_basis IN (
            'submission_confirmed', 'own_work', 'permission',
            'open_license', 'public_domain', 'other'
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
    CONSTRAINT ai_factory_sources_id_length
        CHECK (char_length(id) BETWEEN 1 AND 64),
    CONSTRAINT ai_factory_sources_group_length
        CHECK (char_length(source_group_id) BETWEEN 1 AND 64),
    CONSTRAINT ai_factory_sources_revision_link
        CHECK (
            (revision_no = 1 AND supersedes_source_id IS NULL)
            OR (revision_no > 1 AND supersedes_source_id IS NOT NULL)
        ),
    CONSTRAINT ai_factory_sources_origin_kind
        CHECK (
            (source_kind = 'llm_submission'
                AND origin_submission_id IS NOT NULL
                AND rights_basis = 'submission_confirmed')
            OR
            (source_kind = 'admin_paste'
                AND origin_submission_id IS NULL
                AND rights_basis <> 'submission_confirmed')
        ),
    CONSTRAINT ai_factory_sources_metadata_lengths
        CHECK (
            char_length(data_type) BETWEEN 1 AND 80
            AND (title IS NULL OR char_length(title) <= 500)
            AND (topic_text IS NULL OR char_length(topic_text) <= 2000)
            AND (side IS NULL OR char_length(side) <= 80)
            AND (source_note IS NULL OR char_length(source_note) <= 1000)
            AND char_length(language_code) BETWEEN 2 AND 35
            AND char_length(rights_confirmed_by) BETWEEN 1 AND 200
            AND char_length(created_by) BETWEEN 1 AND 200
        ),
    CONSTRAINT ai_factory_sources_content_length
        CHECK (char_length(content_text) BETWEEN 1 AND 20000),
    CONSTRAINT ai_factory_sources_content_hash
        CHECK (
            char_length(content_sha256) = 64
            AND content_sha256 = lower(content_sha256)
            AND content_sha256 ~ '^[0-9a-f]+$'
        ),
    CONSTRAINT ai_factory_sources_withdrawal_fields
        CHECK (
            (withdrawn_at IS NULL
                AND withdrawn_by IS NULL
                AND withdrawal_reason IS NULL)
            OR
            (withdrawn_at IS NOT NULL
                AND char_length(withdrawn_by) BETWEEN 1 AND 200
                AND char_length(withdrawal_reason) BETWEEN 1 AND 1000)
        ),
    CONSTRAINT fk_ai_factory_sources_submission
        FOREIGN KEY (origin_submission_id)
        REFERENCES public.llm_training_submissions(id) ON DELETE RESTRICT,
    CONSTRAINT fk_ai_factory_sources_superseded
        FOREIGN KEY (supersedes_source_id)
        REFERENCES public.ai_factory_sources(id) ON DELETE RESTRICT,
    UNIQUE (source_group_id, revision_no)
);

COMMENT ON TABLE public.ai_factory_sources IS
    'skhlmc-feature:data_factory:20260720_0001';

CREATE UNIQUE INDEX idx_ai_factory_sources_submission
    ON public.ai_factory_sources(origin_submission_id)
    WHERE origin_submission_id IS NOT NULL;
CREATE UNIQUE INDEX idx_ai_factory_sources_supersedes
    ON public.ai_factory_sources(supersedes_source_id)
    WHERE supersedes_source_id IS NOT NULL;
CREATE INDEX idx_ai_factory_sources_active_created
    ON public.ai_factory_sources(created_at DESC)
    WHERE withdrawn_at IS NULL;

CREATE TABLE public.ai_factory_jobs (
    id                      TEXT        PRIMARY KEY,
    source_id               TEXT        NOT NULL,
    recipe_key              TEXT        NOT NULL
        CHECK (recipe_key IN (
            'rag_knowledge_card_v1',
            'rag_argument_decomposition_v1',
            'sft_speech_critique_v1',
            'sft_attack_defence_v1'
        )),
    requested_count         SMALLINT    NOT NULL DEFAULT 3
        CHECK (requested_count BETWEEN 1 AND 5),
    instruction_text        TEXT        NOT NULL DEFAULT '',
    status                  TEXT        NOT NULL DEFAULT 'draft'
        CHECK (status IN (
            'draft', 'processing', 'awaiting_review',
            'reviewed', 'failed', 'invalidated'
        )),
    preview_model_label     TEXT,
    preview_provider        TEXT,
    preview_provider_model  TEXT,
    preview_prompt_sha256   TEXT,
    preview_input_sha256    TEXT,
    preview_sha256          TEXT,
    preview_expires_at      TIMESTAMPTZ,
    created_by              TEXT        NOT NULL,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    invalidated_by          TEXT,
    invalidated_at          TIMESTAMPTZ,
    invalidation_reason     TEXT,
    CONSTRAINT ai_factory_jobs_id_length
        CHECK (char_length(id) BETWEEN 1 AND 64),
    CONSTRAINT ai_factory_jobs_instruction_length
        CHECK (char_length(instruction_text) <= 500),
    CONSTRAINT ai_factory_jobs_actor_length
        CHECK (char_length(created_by) BETWEEN 1 AND 200),
    CONSTRAINT ai_factory_jobs_preview_bundle
        CHECK (
            (preview_model_label IS NULL
                AND preview_provider IS NULL
                AND preview_provider_model IS NULL
                AND preview_prompt_sha256 IS NULL
                AND preview_input_sha256 IS NULL
                AND preview_sha256 IS NULL
                AND preview_expires_at IS NULL)
            OR
            (char_length(preview_model_label) BETWEEN 1 AND 200
                AND char_length(preview_provider) BETWEEN 1 AND 80
                AND char_length(preview_provider_model) BETWEEN 1 AND 200
                AND char_length(preview_prompt_sha256) = 64
                AND preview_prompt_sha256 = lower(preview_prompt_sha256)
                AND preview_prompt_sha256 ~ '^[0-9a-f]+$'
                AND char_length(preview_input_sha256) = 64
                AND preview_input_sha256 = lower(preview_input_sha256)
                AND preview_input_sha256 ~ '^[0-9a-f]+$'
                AND char_length(preview_sha256) = 64
                AND preview_sha256 = lower(preview_sha256)
                AND preview_sha256 ~ '^[0-9a-f]+$'
                AND preview_expires_at IS NOT NULL)
        ),
    CONSTRAINT ai_factory_jobs_invalidation_fields
        CHECK (
            (status <> 'invalidated'
                AND invalidated_at IS NULL
                AND invalidated_by IS NULL
                AND invalidation_reason IS NULL)
            OR
            (status = 'invalidated'
                AND invalidated_at IS NOT NULL
                AND char_length(invalidated_by) BETWEEN 1 AND 200
                AND char_length(invalidation_reason) BETWEEN 1 AND 1000)
        ),
    CONSTRAINT fk_ai_factory_jobs_source
        FOREIGN KEY (source_id)
        REFERENCES public.ai_factory_sources(id) ON DELETE RESTRICT
);

CREATE INDEX idx_ai_factory_jobs_source_created
    ON public.ai_factory_jobs(source_id, created_at DESC);
CREATE INDEX idx_ai_factory_jobs_status_updated
    ON public.ai_factory_jobs(status, updated_at DESC);
CREATE INDEX idx_ai_factory_jobs_preview_expiry
    ON public.ai_factory_jobs(preview_expires_at)
    WHERE preview_expires_at IS NOT NULL;

CREATE TABLE public.ai_factory_attempts (
    id                        TEXT        PRIMARY KEY,
    job_id                    TEXT        NOT NULL,
    attempt_no                SMALLINT    NOT NULL
        CHECK (attempt_no BETWEEN 1 AND 3),
    operation_id              TEXT        NOT NULL,
    model_label               TEXT        NOT NULL,
    provider                  TEXT        NOT NULL,
    provider_model            TEXT        NOT NULL,
    recipe_key                TEXT        NOT NULL
        CHECK (recipe_key IN (
            'rag_knowledge_card_v1',
            'rag_argument_decomposition_v1',
            'sft_speech_critique_v1',
            'sft_attack_defence_v1'
        )),
    recipe_version            TEXT        NOT NULL,
    candidate_count           SMALLINT    NOT NULL
        CHECK (candidate_count BETWEEN 1 AND 5),
    estimated_cost_hkd        NUMERIC(12, 8) NOT NULL DEFAULT 0
        CHECK (estimated_cost_hkd BETWEEN 0 AND 9999),
    budget_provider_name      TEXT,
    budget_period_month       DATE,
    budget_window_start       TIMESTAMP,
    source_sha256             TEXT        NOT NULL,
    prompt_sha256             TEXT        NOT NULL,
    input_sha256              TEXT        NOT NULL,
    preview_sha256            TEXT        NOT NULL,
    previewed_at              TIMESTAMPTZ NOT NULL,
    preview_expires_at        TIMESTAMPTZ NOT NULL,
    confirmation_version      TEXT        NOT NULL,
    anonymization_confirmed   BOOLEAN     NOT NULL,
    rights_confirmed          BOOLEAN     NOT NULL,
    third_party_confirmed     BOOLEAN     NOT NULL,
    pii_warning_count         SMALLINT    NOT NULL DEFAULT 0
        CHECK (pii_warning_count BETWEEN 0 AND 20),
    pii_override_reason       TEXT,
    confirmed_by              TEXT        NOT NULL,
    confirmed_at              TIMESTAMPTZ NOT NULL,
    status                    TEXT        NOT NULL DEFAULT 'claimed'
        CHECK (status IN (
            'claimed', 'running', 'succeeded', 'failed', 'discarded'
        )),
    provider_attempted_at     TIMESTAMPTZ,
    provider_request_id       TEXT,
    resolved_provider_model   TEXT,
    response_sha256           TEXT,
    response_bytes            INTEGER,
    error_code                TEXT,
    completed_at              TIMESTAMPTZ,
    created_at                TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT ai_factory_attempts_id_length
        CHECK (char_length(id) BETWEEN 1 AND 64),
    CONSTRAINT ai_factory_attempts_operation_is_job
        CHECK (operation_id = job_id),
    CONSTRAINT ai_factory_attempts_identity_lengths
        CHECK (
            char_length(model_label) BETWEEN 1 AND 200
            AND char_length(provider) BETWEEN 1 AND 80
            AND char_length(provider_model) BETWEEN 1 AND 200
            AND char_length(recipe_version) BETWEEN 1 AND 80
            AND char_length(confirmation_version) BETWEEN 1 AND 80
            AND char_length(confirmed_by) BETWEEN 1 AND 200
            AND (provider_request_id IS NULL
                OR char_length(provider_request_id) <= 300)
            AND (resolved_provider_model IS NULL
                OR char_length(resolved_provider_model) BETWEEN 1 AND 200)
        ),
    CONSTRAINT ai_factory_attempts_budget_reservation
        CHECK (
            (estimated_cost_hkd = 0
                AND budget_provider_name IS NULL
                AND budget_period_month IS NULL
                AND budget_window_start IS NULL)
            OR
            (estimated_cost_hkd > 0
                AND char_length(budget_provider_name) BETWEEN 1 AND 80
                AND budget_period_month IS NOT NULL
                AND budget_window_start IS NOT NULL)
        ),
    CONSTRAINT ai_factory_attempts_hashes
        CHECK (
            char_length(source_sha256) = 64
            AND source_sha256 = lower(source_sha256)
            AND source_sha256 ~ '^[0-9a-f]+$'
            AND char_length(prompt_sha256) = 64
            AND prompt_sha256 = lower(prompt_sha256)
            AND prompt_sha256 ~ '^[0-9a-f]+$'
            AND char_length(input_sha256) = 64
            AND input_sha256 = lower(input_sha256)
            AND input_sha256 ~ '^[0-9a-f]+$'
            AND char_length(preview_sha256) = 64
            AND preview_sha256 = lower(preview_sha256)
            AND preview_sha256 ~ '^[0-9a-f]+$'
            AND (response_sha256 IS NULL
                OR (
                    char_length(response_sha256) = 64
                    AND response_sha256 = lower(response_sha256)
                    AND response_sha256 ~ '^[0-9a-f]+$'
                ))
        ),
    CONSTRAINT ai_factory_attempts_confirmation
        CHECK (
            anonymization_confirmed = TRUE
            AND rights_confirmed = TRUE
            AND third_party_confirmed = TRUE
            AND confirmed_at >= previewed_at
            AND confirmed_at <= preview_expires_at
            AND (provider_attempted_at IS NULL
                OR provider_attempted_at >= confirmed_at)
        ),
    CONSTRAINT ai_factory_attempts_pii_confirmation
        CHECK (
            (pii_warning_count = 0
                AND pii_override_reason IS NULL)
            OR
            (pii_warning_count > 0
                AND char_length(pii_override_reason) BETWEEN 1 AND 1000)
        ),
    CONSTRAINT ai_factory_attempts_response_size
        CHECK (response_bytes IS NULL OR response_bytes BETWEEN 0 AND 102400),
    CONSTRAINT ai_factory_attempts_status_fields
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
                AND response_bytes IS NOT NULL
                AND response_bytes > 0
                AND error_code IS NULL)
            OR
            (status IN ('failed', 'discarded')
                AND completed_at IS NOT NULL
                AND char_length(error_code) BETWEEN 1 AND 120)
        ),
    CONSTRAINT ai_factory_attempts_completion_order
        CHECK (completed_at IS NULL OR completed_at >= provider_attempted_at),
    CONSTRAINT fk_ai_factory_attempts_job
        FOREIGN KEY (job_id)
        REFERENCES public.ai_factory_jobs(id) ON DELETE RESTRICT,
    UNIQUE (id, job_id),
    UNIQUE (job_id, attempt_no)
);

CREATE UNIQUE INDEX idx_ai_factory_attempts_one_success
    ON public.ai_factory_attempts(job_id)
    WHERE status = 'succeeded';
CREATE INDEX idx_ai_factory_attempts_job_created
    ON public.ai_factory_attempts(job_id, created_at DESC);
CREATE INDEX idx_ai_factory_attempts_processing
    ON public.ai_factory_attempts(provider_attempted_at)
    WHERE status IN ('claimed', 'running');

CREATE TABLE public.ai_factory_items (
    id                    TEXT        PRIMARY KEY,
    job_id                TEXT        NOT NULL,
    attempt_id            TEXT        NOT NULL,
    ordinal               SMALLINT    NOT NULL CHECK (ordinal BETWEEN 1 AND 5),
    original_json         JSONB       NOT NULL,
    original_sha256       TEXT        NOT NULL,
    reviewed_json         JSONB,
    reviewed_sha256       TEXT,
    review_status         TEXT        NOT NULL DEFAULT 'pending'
        CHECK (review_status IN ('pending', 'approved', 'rejected')),
    review_note           TEXT,
    reviewed_by           TEXT,
    reviewed_at           TIMESTAMPTZ,
    created_at            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    invalidated_by        TEXT,
    invalidated_at        TIMESTAMPTZ,
    invalidation_reason   TEXT,
    CONSTRAINT ai_factory_items_id_length
        CHECK (char_length(id) BETWEEN 1 AND 64),
    CONSTRAINT ai_factory_items_original_object
        CHECK (jsonb_typeof(original_json) = 'object'),
    CONSTRAINT ai_factory_items_reviewed_object
        CHECK (reviewed_json IS NULL OR jsonb_typeof(reviewed_json) = 'object'),
    CONSTRAINT ai_factory_items_hashes
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
    CONSTRAINT ai_factory_items_review_fields
        CHECK (
            (review_status = 'pending'
                AND reviewed_json IS NULL
                AND reviewed_sha256 IS NULL
                AND reviewed_by IS NULL
                AND reviewed_at IS NULL)
            OR
            (review_status = 'approved'
                AND reviewed_json IS NOT NULL
                AND reviewed_sha256 IS NOT NULL
                AND char_length(reviewed_by) BETWEEN 1 AND 200
                AND reviewed_at IS NOT NULL)
            OR
            (review_status = 'rejected'
                AND char_length(reviewed_by) BETWEEN 1 AND 200
                AND reviewed_at IS NOT NULL)
        ),
    CONSTRAINT ai_factory_items_note_length
        CHECK (review_note IS NULL OR char_length(review_note) <= 2000),
    CONSTRAINT ai_factory_items_invalidation_fields
        CHECK (
            (invalidated_at IS NULL
                AND invalidated_by IS NULL
                AND invalidation_reason IS NULL)
            OR
            (invalidated_at IS NOT NULL
                AND char_length(invalidated_by) BETWEEN 1 AND 200
                AND char_length(invalidation_reason) BETWEEN 1 AND 1000)
        ),
    CONSTRAINT fk_ai_factory_items_attempt_job
        FOREIGN KEY (attempt_id, job_id)
        REFERENCES public.ai_factory_attempts(id, job_id) ON DELETE RESTRICT,
    UNIQUE (job_id, ordinal)
);

CREATE INDEX idx_ai_factory_items_attempt
    ON public.ai_factory_items(attempt_id, ordinal);
CREATE INDEX idx_ai_factory_items_review_queue
    ON public.ai_factory_items(created_at)
    WHERE review_status = 'pending' AND invalidated_at IS NULL;
CREATE UNIQUE INDEX idx_ai_factory_items_approved_hash
    ON public.ai_factory_items(reviewed_sha256)
    WHERE review_status = 'approved';

CREATE TABLE public.ai_factory_topic_tags (
    id                TEXT        PRIMARY KEY,
    label             TEXT        NOT NULL,
    normalized_label  TEXT        NOT NULL UNIQUE,
    approved_by       TEXT        NOT NULL,
    approved_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    retired_by        TEXT,
    retired_at        TIMESTAMPTZ,
    CONSTRAINT ai_factory_topic_tags_id_length
        CHECK (char_length(id) BETWEEN 1 AND 64),
    CONSTRAINT ai_factory_topic_tags_label_length
        CHECK (
            char_length(label) BETWEEN 1 AND 40
            AND char_length(normalized_label) BETWEEN 1 AND 40
            AND char_length(approved_by) BETWEEN 1 AND 200
        ),
    CONSTRAINT ai_factory_topic_tags_retirement_fields
        CHECK (
            (retired_at IS NULL AND retired_by IS NULL)
            OR
            (retired_at IS NOT NULL
                AND char_length(retired_by) BETWEEN 1 AND 200)
        )
);

CREATE INDEX idx_ai_factory_topic_tags_active
    ON public.ai_factory_topic_tags(normalized_label)
    WHERE retired_at IS NULL;

CREATE TABLE public.ai_factory_item_tags (
    item_id      TEXT        NOT NULL,
    tag_id       TEXT        NOT NULL,
    assigned_by  TEXT        NOT NULL,
    assigned_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT ai_factory_item_tags_actor_length
        CHECK (char_length(assigned_by) BETWEEN 1 AND 200),
    CONSTRAINT fk_ai_factory_item_tags_item
        FOREIGN KEY (item_id)
        REFERENCES public.ai_factory_items(id) ON DELETE RESTRICT,
    CONSTRAINT fk_ai_factory_item_tags_tag
        FOREIGN KEY (tag_id)
        REFERENCES public.ai_factory_topic_tags(id) ON DELETE RESTRICT,
    PRIMARY KEY (item_id, tag_id)
);

CREATE INDEX idx_ai_factory_item_tags_tag
    ON public.ai_factory_item_tags(tag_id, item_id);

CREATE TABLE public.ai_factory_releases (
    id                    TEXT        PRIMARY KEY,
    release_kind          TEXT        NOT NULL
        CHECK (release_kind IN ('rag', 'sft')),
    version_no            INTEGER     NOT NULL CHECK (version_no > 0),
    schema_version        TEXT        NOT NULL,
    jsonl_text            TEXT        NOT NULL,
    jsonl_sha256          TEXT        NOT NULL,
    jsonl_bytes           INTEGER     NOT NULL,
    manifest_json         JSONB       NOT NULL,
    manifest_sha256       TEXT        NOT NULL,
    item_count            INTEGER     NOT NULL,
    published_by          TEXT        NOT NULL,
    published_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    invalidated_by        TEXT,
    invalidated_at        TIMESTAMPTZ,
    invalidation_reason   TEXT,
    CONSTRAINT ai_factory_releases_id_length
        CHECK (char_length(id) BETWEEN 1 AND 64),
    CONSTRAINT ai_factory_releases_metadata_lengths
        CHECK (
            char_length(schema_version) BETWEEN 1 AND 80
            AND char_length(published_by) BETWEEN 1 AND 200
        ),
    CONSTRAINT ai_factory_releases_bounds
        CHECK (
            item_count BETWEEN 1 AND 500
            AND jsonl_bytes BETWEEN 1 AND 5242880
            AND jsonl_bytes = octet_length(jsonl_text)
        ),
    CONSTRAINT ai_factory_releases_json_object
        CHECK (jsonb_typeof(manifest_json) = 'object'),
    CONSTRAINT ai_factory_releases_hashes
        CHECK (
            char_length(jsonl_sha256) = 64
            AND jsonl_sha256 = lower(jsonl_sha256)
            AND jsonl_sha256 ~ '^[0-9a-f]+$'
            AND char_length(manifest_sha256) = 64
            AND manifest_sha256 = lower(manifest_sha256)
            AND manifest_sha256 ~ '^[0-9a-f]+$'
        ),
    CONSTRAINT ai_factory_releases_invalidation_fields
        CHECK (
            (invalidated_at IS NULL
                AND invalidated_by IS NULL
                AND invalidation_reason IS NULL)
            OR
            (invalidated_at IS NOT NULL
                AND char_length(invalidated_by) BETWEEN 1 AND 200
                AND char_length(invalidation_reason) BETWEEN 1 AND 1000)
        ),
    UNIQUE (release_kind, version_no)
);

CREATE INDEX idx_ai_factory_releases_active
    ON public.ai_factory_releases(release_kind, version_no DESC)
    WHERE invalidated_at IS NULL;

CREATE TABLE public.ai_factory_release_items (
    release_id         TEXT     NOT NULL,
    item_id            TEXT     NOT NULL,
    ordinal            INTEGER  NOT NULL CHECK (ordinal BETWEEN 1 AND 500),
    item_sha256        TEXT     NOT NULL,
    jsonl_line_sha256  TEXT     NOT NULL,
    CONSTRAINT ai_factory_release_items_hashes
        CHECK (
            char_length(item_sha256) = 64
            AND item_sha256 = lower(item_sha256)
            AND item_sha256 ~ '^[0-9a-f]+$'
            AND char_length(jsonl_line_sha256) = 64
            AND jsonl_line_sha256 = lower(jsonl_line_sha256)
            AND jsonl_line_sha256 ~ '^[0-9a-f]+$'
        ),
    CONSTRAINT fk_ai_factory_release_items_release
        FOREIGN KEY (release_id)
        REFERENCES public.ai_factory_releases(id) ON DELETE RESTRICT,
    CONSTRAINT fk_ai_factory_release_items_item
        FOREIGN KEY (item_id)
        REFERENCES public.ai_factory_items(id) ON DELETE RESTRICT,
    PRIMARY KEY (release_id, item_id),
    UNIQUE (release_id, ordinal)
);

CREATE INDEX idx_ai_factory_release_items_item
    ON public.ai_factory_release_items(item_id, release_id);

-- Factory governance evidence survives the ordinary operational audit prune.
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
        'factory_release_invalidated'
    );

ALTER TABLE public.ai_fund_usage_logs
    DROP CONSTRAINT ai_fund_usage_logs_feature_check;
ALTER TABLE public.ai_fund_usage_logs
    ADD CONSTRAINT ai_fund_usage_logs_feature_check CHECK (
        feature IN (
            'speech_review', 'strategy', 'competition_prep', 'web_research',
            'fact_check', 'free_debate_live', 'full_mock_live', 'vote_review',
            'vote_analysis', 'vote_discussion', 'tts_review',
            'tts_script_analysis', 'llm_review', 'kiosk_match_review', 'tts',
            'kiosk_match_review_tts', 'data_factory_generation'
        )
    );

REVOKE ALL PRIVILEGES ON TABLE public.ai_factory_sources FROM PUBLIC;
DO $$
DECLARE role_name TEXT;
BEGIN
    FOR role_name IN SELECT rolname FROM pg_roles
        WHERE rolname IN ('anon', 'authenticated')
    LOOP
        EXECUTE 'REVOKE ALL PRIVILEGES ON TABLE public.ai_factory_sources FROM '
            || quote_ident(role_name);
    END LOOP;
END $$;

REVOKE ALL PRIVILEGES ON TABLE public.ai_factory_jobs FROM PUBLIC;
DO $$
DECLARE role_name TEXT;
BEGIN
    FOR role_name IN SELECT rolname FROM pg_roles
        WHERE rolname IN ('anon', 'authenticated')
    LOOP
        EXECUTE 'REVOKE ALL PRIVILEGES ON TABLE public.ai_factory_jobs FROM '
            || quote_ident(role_name);
    END LOOP;
END $$;

REVOKE ALL PRIVILEGES ON TABLE public.ai_factory_attempts FROM PUBLIC;
DO $$
DECLARE role_name TEXT;
BEGIN
    FOR role_name IN SELECT rolname FROM pg_roles
        WHERE rolname IN ('anon', 'authenticated')
    LOOP
        EXECUTE 'REVOKE ALL PRIVILEGES ON TABLE public.ai_factory_attempts FROM '
            || quote_ident(role_name);
    END LOOP;
END $$;

REVOKE ALL PRIVILEGES ON TABLE public.ai_factory_items FROM PUBLIC;
DO $$
DECLARE role_name TEXT;
BEGIN
    FOR role_name IN SELECT rolname FROM pg_roles
        WHERE rolname IN ('anon', 'authenticated')
    LOOP
        EXECUTE 'REVOKE ALL PRIVILEGES ON TABLE public.ai_factory_items FROM '
            || quote_ident(role_name);
    END LOOP;
END $$;

REVOKE ALL PRIVILEGES ON TABLE public.ai_factory_topic_tags FROM PUBLIC;
DO $$
DECLARE role_name TEXT;
BEGIN
    FOR role_name IN SELECT rolname FROM pg_roles
        WHERE rolname IN ('anon', 'authenticated')
    LOOP
        EXECUTE 'REVOKE ALL PRIVILEGES ON TABLE public.ai_factory_topic_tags FROM '
            || quote_ident(role_name);
    END LOOP;
END $$;

REVOKE ALL PRIVILEGES ON TABLE public.ai_factory_item_tags FROM PUBLIC;
DO $$
DECLARE role_name TEXT;
BEGIN
    FOR role_name IN SELECT rolname FROM pg_roles
        WHERE rolname IN ('anon', 'authenticated')
    LOOP
        EXECUTE 'REVOKE ALL PRIVILEGES ON TABLE public.ai_factory_item_tags FROM '
            || quote_ident(role_name);
    END LOOP;
END $$;

REVOKE ALL PRIVILEGES ON TABLE public.ai_factory_releases FROM PUBLIC;
DO $$
DECLARE role_name TEXT;
BEGIN
    FOR role_name IN SELECT rolname FROM pg_roles
        WHERE rolname IN ('anon', 'authenticated')
    LOOP
        EXECUTE 'REVOKE ALL PRIVILEGES ON TABLE public.ai_factory_releases FROM '
            || quote_ident(role_name);
    END LOOP;
END $$;

REVOKE ALL PRIVILEGES ON TABLE public.ai_factory_release_items FROM PUBLIC;
DO $$
DECLARE role_name TEXT;
BEGIN
    FOR role_name IN SELECT rolname FROM pg_roles
        WHERE rolname IN ('anon', 'authenticated')
    LOOP
        EXECUTE 'REVOKE ALL PRIVILEGES ON TABLE public.ai_factory_release_items FROM '
            || quote_ident(role_name);
    END LOOP;
END $$;
