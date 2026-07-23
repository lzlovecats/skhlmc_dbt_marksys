-- The removed source text, attempts, jobs, usage and audit rows were
-- intentionally deleted and cannot be reconstructed by recreating empty
-- tables. Restore a pre-migration database backup together with the matching
-- application release instead.

DO $$
BEGIN
    RAISE EXCEPTION
        '20260723_0001 is irreversible; restore the pre-migration database backup';
END $$;
