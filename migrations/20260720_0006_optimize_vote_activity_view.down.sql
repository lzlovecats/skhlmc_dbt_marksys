-- Restore the previous correlated implementation while preserving its fixed
-- active_since ballot denominator.

SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '60s';

CREATE OR REPLACE VIEW public.committee_vote_activity_view AS
WITH tv_events AS (
    SELECT DISTINCT tv.topic_text, tv.created_at
    FROM public.topic_votes tv
    WHERE EXISTS (
        SELECT 1 FROM public.topic_vote_ballots ballot
        WHERE ballot.topic_text=tv.topic_text
    )
),
tdv_events AS (
    SELECT DISTINCT removal.topic_text, removal.created_at
    FROM public.topic_removal_votes removal
    WHERE EXISTS (
        SELECT 1 FROM public.topic_removal_vote_ballots ballot
        WHERE ballot.topic_text=removal.topic_text
    )
),
all_events AS (
    SELECT topic_text, created_at, 'tv' AS vote_source FROM tv_events
    UNION ALL
    SELECT topic_text, created_at, 'tdv' AS vote_source FROM tdv_events
),
combined_ballots AS (
    SELECT ballot.user_id, ballot.vote_choice, vote.created_at
    FROM public.topic_vote_ballots ballot
    JOIN public.topic_votes vote ON vote.topic_text=ballot.topic_text
    UNION ALL
    SELECT ballot.user_id, ballot.vote_choice, removal.created_at
    FROM public.topic_removal_vote_ballots ballot
    JOIN public.topic_removal_votes removal
      ON removal.topic_text=ballot.topic_text
),
ballot_summary AS (
    SELECT
        account.user_id,
        COUNT(ballot.vote_choice) AS total_ballots,
        COUNT(ballot.vote_choice) FILTER (
            WHERE ballot.vote_choice='agree'
        ) AS agree_ballots
    FROM public.accounts account
    LEFT JOIN combined_ballots ballot
      ON ballot.user_id=account.user_id
     AND (
         account.active_since IS NULL
         OR ballot.created_at::DATE>=account.active_since
     )
    GROUP BY account.user_id
),
base_stats AS (
    SELECT
        account.user_id,
        account.account_status,
        (
            SELECT COUNT(*) FROM all_events event
            WHERE account.active_since IS NULL
               OR event.created_at::DATE>=account.active_since
        ) AS total_votes,
        (
            SELECT COUNT(*) FROM all_events event
            WHERE (
                account.active_since IS NULL
                OR event.created_at::DATE>=account.active_since
            )
              AND (
                  (
                      event.vote_source='tv'
                      AND EXISTS (
                          SELECT 1 FROM public.topic_vote_ballots ballot
                          WHERE ballot.topic_text=event.topic_text
                            AND ballot.user_id=account.user_id
                      )
                  ) OR (
                      event.vote_source='tdv'
                      AND EXISTS (
                          SELECT 1
                          FROM public.topic_removal_vote_ballots ballot
                          WHERE ballot.topic_text=event.topic_text
                            AND ballot.user_id=account.user_id
                      )
                  )
              )
        ) AS participated_votes,
        (
            SELECT COUNT(*) FROM (
                SELECT event.topic_text, event.vote_source
                FROM all_events event
                WHERE account.active_since IS NULL
                   OR event.created_at::DATE>=account.active_since
                ORDER BY event.created_at DESC
                LIMIT 10
            ) recent
            WHERE (
                recent.vote_source='tv'
                AND EXISTS (
                    SELECT 1 FROM public.topic_vote_ballots ballot
                    WHERE ballot.topic_text=recent.topic_text
                      AND ballot.user_id=account.user_id
                )
            ) OR (
                recent.vote_source='tdv'
                AND EXISTS (
                    SELECT 1 FROM public.topic_removal_vote_ballots ballot
                    WHERE ballot.topic_text=recent.topic_text
                      AND ballot.user_id=account.user_id
                )
            )
        ) AS last10_participated,
        COALESCE(summary.total_ballots, 0) AS total_ballots,
        COALESCE(summary.agree_ballots, 0) AS agree_ballots
    FROM public.accounts account
    LEFT JOIN ballot_summary summary ON summary.user_id=account.user_id
    WHERE LOWER(account.user_id)
            NOT IN ('admin', 'developer', 'kiosk', 'gemini')
      AND account.user_id<>''
      AND COALESCE(account.account_disabled, FALSE)=FALSE
)
SELECT
    user_id,
    account_status,
    total_votes,
    participated_votes,
    last10_participated,
    total_ballots,
    agree_ballots,
    CASE
        WHEN total_votes>0
        THEN ROUND(participated_votes::NUMERIC / total_votes * 100, 1)
        ELSE 0
    END AS overall_rate_pct,
    CASE
        WHEN total_ballots>0
        THEN ROUND(agree_ballots::NUMERIC / total_ballots * 100, 1)
        ELSE NULL
    END AS agree_rate_pct,
    CASE
        WHEN total_votes>0
         AND participated_votes::NUMERIC / total_votes>=0.4
         AND last10_participated>=3
        THEN TRUE
        ELSE FALSE
    END AS is_active
FROM base_stats;
