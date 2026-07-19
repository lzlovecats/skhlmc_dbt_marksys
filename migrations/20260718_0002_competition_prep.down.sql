DELETE FROM ai_fund_usage_logs WHERE feature = 'competition_prep';

ALTER TABLE ai_fund_usage_logs DROP CONSTRAINT IF EXISTS ai_fund_usage_logs_feature_check;
ALTER TABLE ai_fund_usage_logs ADD CONSTRAINT ai_fund_usage_logs_feature_check CHECK (
    feature IN ('speech_review', 'strategy', 'web_research', 'fact_check', 'free_debate_live', 'full_mock_live', 'vote_review', 'vote_analysis', 'vote_discussion', 'tts_review', 'tts_script_analysis', 'llm_review', 'kiosk_match_review', 'tts', 'kiosk_match_review_tts')
);

DROP TABLE IF EXISTS competition_prep_ai_runs;
DROP TABLE IF EXISTS competition_prep_weaknesses;
DROP TABLE IF EXISTS competition_prep_evidence_cards;
DROP TABLE IF EXISTS competition_prep_strategy_cards;
DROP TABLE IF EXISTS competition_prep_manuscripts;
DROP TABLE IF EXISTS competition_prep_members;
DROP TABLE IF EXISTS competition_prep_projects;
