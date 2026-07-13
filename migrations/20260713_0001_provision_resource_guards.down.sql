-- Roll back only before these ledgers contain data that must be retained.

DROP TABLE ai_coach_prepare_usage;
DROP TABLE r2_upload_intents;
DROP TABLE bandwidth_usage_logs;
DROP TABLE practice_daily_usage;
