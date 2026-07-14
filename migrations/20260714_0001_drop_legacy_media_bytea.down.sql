-- This migration destroys the legacy binary copies and is intentionally
-- irreversible.  Re-adding empty BYTEA columns would not restore the media and
-- would give a false rollback signal; restore the approved pre-migration
-- database/R2 backup instead.

DO $migration$
BEGIN
    RAISE EXCEPTION
        '20260714_0001 is irreversible; restore the pre-migration database/R2 backup';
END
$migration$;
