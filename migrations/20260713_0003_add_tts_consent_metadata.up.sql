-- Privacy-safe additive TTS schema. Existing consent rows are deliberately
-- treated as not having separately confirmed voice cloning/cloud processing;
-- members must reconfirm before any new recording or dataset use.

ALTER TABLE tts_voice_consents
    ADD COLUMN voice_cloning_confirmed BOOLEAN NOT NULL DEFAULT FALSE,
    ADD COLUMN cloud_processing_confirmed BOOLEAN NOT NULL DEFAULT FALSE,
    ADD COLUMN is_minor BOOLEAN NOT NULL DEFAULT FALSE,
    ADD COLUMN guardian_confirmed BOOLEAN NOT NULL DEFAULT FALSE;

ALTER TABLE tts_voice_recordings
    ADD COLUMN measured_duration_seconds NUMERIC,
    ADD COLUMN sample_rate_hz INTEGER,
    ADD COLUMN channel_count INTEGER,
    ADD COLUMN detected_format TEXT;
