-- Audit history is evidence for consent and review decisions. Refuse a
-- destructive rollback once the application has written any audit row.

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM ai_training_audit LIMIT 1) THEN
        RAISE EXCEPTION 'refusing to drop non-empty ai_training_audit';
    END IF;
END
$$;

DROP TABLE ai_training_audit;
