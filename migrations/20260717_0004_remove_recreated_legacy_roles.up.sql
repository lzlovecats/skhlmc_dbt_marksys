-- Reconcile legacy role aliases that the old app recreated after 0003 ran,
-- then retire them now that production serves the consolidated-role release.

SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '60s';

DO $migration$
DECLARE
    ai_accounts JSONB;
    senior_accounts JSONB;
BEGIN
    SELECT COALESCE(jsonb_agg(account_name ORDER BY account_name), '[]'::jsonb)
    INTO ai_accounts
    FROM (
        SELECT DISTINCT account_name
        FROM public.app_config config,
             LATERAL jsonb_array_elements_text(
                 CASE WHEN jsonb_typeof(config.value)='array'
                      THEN config.value ELSE '[]'::jsonb END
             ) AS items(account_name)
        WHERE config.key IN (
            'ai_managers', 'tts_recording_reviewers', 'ai_fund_treasurers'
        )
          AND BTRIM(account_name) <> ''
    ) merged_ai;

    SELECT COALESCE(jsonb_agg(account_name ORDER BY account_name), '[]'::jsonb)
    INTO senior_accounts
    FROM (
        SELECT DISTINCT account_name
        FROM public.app_config config,
             LATERAL jsonb_array_elements_text(
                 CASE WHEN jsonb_typeof(config.value)='array'
                      THEN config.value ELSE '[]'::jsonb END
             ) AS items(account_name)
        WHERE config.key IN (
            'senior_committee_members', 'lateness_fund_managers'
        )
          AND BTRIM(account_name) <> ''
    ) merged_senior;

    UPDATE public.app_config
    SET value=ai_accounts,updated_at=NOW()
    WHERE key='ai_managers';

    UPDATE public.app_config
    SET value=senior_accounts,updated_at=NOW()
    WHERE key='senior_committee_members';

    DELETE FROM public.app_config
    WHERE key IN (
        'tts_recording_reviewers',
        'ai_fund_treasurers',
        'lateness_fund_managers'
    );
END
$migration$;
