-- The removed campaign answers were intentionally deleted and cannot be
-- reconstructed by recreating empty tables. Restore a pre-migration database
-- backup together with the matching application release instead.

DO $$
BEGIN
    RAISE EXCEPTION
        '20260722_0001 is irreversible; restore the pre-migration database backup';
END $$;
