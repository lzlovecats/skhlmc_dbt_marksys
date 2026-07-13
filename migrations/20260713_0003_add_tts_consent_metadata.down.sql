-- Rollback removes only columns introduced by 0003. Any new confirmation or
-- probe metadata stored after the forward migration will be lost.

ALTER TABLE tts_voice_recordings
    DROP COLUMN detected_format,
    DROP COLUMN channel_count,
    DROP COLUMN sample_rate_hz,
    DROP COLUMN measured_duration_seconds;

ALTER TABLE tts_voice_consents
    DROP COLUMN guardian_confirmed,
    DROP COLUMN is_minor,
    DROP COLUMN cloud_processing_confirmed,
    DROP COLUMN voice_cloning_confirmed;
