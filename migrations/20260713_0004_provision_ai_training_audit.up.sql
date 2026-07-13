-- The audit ledger is required by the active consent, recording-review and
-- LLM-review workflows. Future dataset/eval/RAG tables remain feature-gated
-- until their worker contracts and withdrawal semantics are complete.

CREATE TABLE ai_training_audit (
    id             BIGSERIAL PRIMARY KEY,
    actor_user_id  TEXT,
    action         TEXT NOT NULL,
    target_type    TEXT NOT NULL,
    target_id      TEXT,
    details_json   JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT chk_ai_training_audit_actor_length
        CHECK (actor_user_id IS NULL OR char_length(actor_user_id) <= 200),
    CONSTRAINT chk_ai_training_audit_action_length
        CHECK (char_length(action) BETWEEN 1 AND 100),
    CONSTRAINT chk_ai_training_audit_target_type_length
        CHECK (char_length(target_type) BETWEEN 1 AND 100),
    CONSTRAINT chk_ai_training_audit_target_id_length
        CHECK (target_id IS NULL OR char_length(target_id) <= 300),
    CONSTRAINT chk_ai_training_audit_details_object
        CHECK (jsonb_typeof(details_json) = 'object')
);

CREATE INDEX idx_ai_training_audit_created_at
    ON ai_training_audit (created_at)
    WHERE action NOT IN (
        'consent_granted', 'consent_withdrawn', 'submission_withdrawn'
    );

REVOKE ALL PRIVILEGES ON TABLE ai_training_audit
    FROM PUBLIC, anon, authenticated;
REVOKE ALL PRIVILEGES ON SEQUENCE ai_training_audit_id_seq
    FROM PUBLIC, anon, authenticated;
