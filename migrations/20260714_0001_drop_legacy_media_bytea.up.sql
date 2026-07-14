-- Permanently remove the legacy PostgreSQL media copies after every row has
-- durable R2 object metadata.  Remote object size/hash/MIME/cache verification
-- and the irreversible approval are release gates outside this transaction.

SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '60s';

LOCK TABLE public.match_photos, public.tts_voice_recordings
    IN ACCESS EXCLUSIVE MODE;

DO $migration$
DECLARE
    legacy_column_count INTEGER;
    unsafe_photos BIGINT;
    unsafe_audio BIGINT;
BEGIN
    SELECT COUNT(*)
    INTO legacy_column_count
    FROM information_schema.columns
    WHERE table_schema = 'public'
      AND (table_name, column_name, data_type) IN (
          ('match_photos', 'image_data', 'bytea'),
          ('tts_voice_recordings', 'audio_data', 'bytea')
      );

    IF legacy_column_count <> 2 THEN
        RAISE EXCEPTION
            'legacy media BYTEA columns do not match the expected pre-migration schema';
    END IF;

    SELECT COUNT(*)
    INTO unsafe_photos
    FROM public.match_photos
    WHERE COALESCE(BTRIM(r2_key), '') !~ '^photos/original/.+'
       OR COALESCE(BTRIM(thumbnail_r2_key), '') !~ '^photos/thumb/.+'
       OR COALESCE(byte_size, 0) <= 0
       OR COALESCE(LOWER(BTRIM(sha256)), '') !~ '^[0-9a-f]{64}$'
       OR NULLIF(BTRIM(mime_type), '') IS NULL
       OR image_data IS NULL
       OR byte_size <> OCTET_LENGTH(image_data);

    SELECT COUNT(*)
    INTO unsafe_audio
    FROM public.tts_voice_recordings
    WHERE COALESCE(BTRIM(r2_key), '') !~ '^audio/tts/.+'
       OR COALESCE(size_bytes, 0) <= 0
       OR COALESCE(LOWER(BTRIM(audio_sha256)), '') !~ '^[0-9a-f]{64}$'
       OR NULLIF(BTRIM(mime_type), '') IS NULL
       OR audio_data IS NULL
       OR size_bytes <> OCTET_LENGTH(audio_data);

    IF unsafe_photos <> 0 OR unsafe_audio <> 0 THEN
        RAISE EXCEPTION
            'refusing legacy media drop: % unsafe photos, % unsafe recordings',
            unsafe_photos, unsafe_audio;
    END IF;
END
$migration$;

ALTER TABLE public.match_photos
    DROP COLUMN image_data;

ALTER TABLE public.tts_voice_recordings
    DROP COLUMN audio_data;
