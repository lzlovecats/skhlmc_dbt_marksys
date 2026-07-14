-- Restore the previous calculation, including its historical all-time ballot
-- denominator.  This is retained solely to make the migration reversible.

CREATE OR REPLACE VIEW committee_vote_activity_view AS
WITH tv_events AS (
    SELECT DISTINCT tv.topic_text, tv.created_at
    FROM topic_votes tv
    WHERE EXISTS (
        SELECT 1 FROM topic_vote_ballots b
        WHERE b.topic_text = tv.topic_text
    )
),
tdv_events AS (
    SELECT DISTINCT tdv.topic_text, tdv.created_at
    FROM topic_removal_votes tdv
    WHERE EXISTS (
        SELECT 1 FROM topic_removal_vote_ballots b
        WHERE b.topic_text = tdv.topic_text
    )
),
all_events AS (
    SELECT topic_text, created_at, 'tv' AS vote_source FROM tv_events
    UNION ALL
    SELECT topic_text, created_at, 'tdv' AS vote_source FROM tdv_events
),
ballot_summary AS (
    SELECT
        user_id,
        COUNT(*) AS total_ballots,
        SUM(CASE WHEN vote_choice = 'agree' THEN 1 ELSE 0 END) AS agree_ballots
    FROM (
        SELECT user_id, vote_choice FROM topic_vote_ballots
        UNION ALL
        SELECT user_id, vote_choice FROM topic_removal_vote_ballots
    ) combined_ballots
    GROUP BY user_id
),
base_stats AS (
    SELECT
        a.user_id,
        a.account_status,
        (
            SELECT COUNT(*) FROM all_events ae
            WHERE a.active_since IS NULL
               OR ae.created_at::date >= a.active_since
        ) AS total_votes,
        (
            SELECT COUNT(*) FROM all_events ae
            WHERE (
                a.active_since IS NULL
                OR ae.created_at::date >= a.active_since
            )
              AND (
                  (
                      ae.vote_source = 'tv'
                      AND EXISTS (
                          SELECT 1 FROM topic_vote_ballots b
                          WHERE b.topic_text = ae.topic_text
                            AND b.user_id = a.user_id
                      )
                  ) OR (
                      ae.vote_source = 'tdv'
                      AND EXISTS (
                          SELECT 1 FROM topic_removal_vote_ballots b
                          WHERE b.topic_text = ae.topic_text
                            AND b.user_id = a.user_id
                      )
                  )
              )
        ) AS participated_votes,
        (
            SELECT COUNT(*) FROM (
                SELECT ae.topic_text, ae.vote_source
                FROM all_events ae
                WHERE a.active_since IS NULL
                   OR ae.created_at::date >= a.active_since
                ORDER BY ae.created_at DESC
                LIMIT 10
            ) p
            WHERE (
                p.vote_source = 'tv'
                AND EXISTS (
                    SELECT 1 FROM topic_vote_ballots b
                    WHERE b.topic_text = p.topic_text
                      AND b.user_id = a.user_id
                )
            ) OR (
                p.vote_source = 'tdv'
                AND EXISTS (
                    SELECT 1 FROM topic_removal_vote_ballots b
                    WHERE b.topic_text = p.topic_text
                      AND b.user_id = a.user_id
                )
            )
        ) AS last10_participated,
        COALESCE(bs.total_ballots, 0) AS total_ballots,
        COALESCE(bs.agree_ballots, 0) AS agree_ballots
    FROM accounts a
    LEFT JOIN ballot_summary bs ON bs.user_id = a.user_id
    WHERE a.user_id NOT IN ('admin', 'developer', '')
      AND COALESCE(a.account_disabled, FALSE) = FALSE
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
        WHEN total_votes > 0
        THEN ROUND(participated_votes::numeric / total_votes * 100, 1)
        ELSE 0
    END AS overall_rate_pct,
    CASE
        WHEN total_ballots > 0
        THEN ROUND(agree_ballots::numeric / total_ballots * 100, 1)
        ELSE NULL
    END AS agree_rate_pct,
    CASE
        WHEN total_votes > 0
             AND participated_votes::numeric / total_votes >= 0.4
             AND last10_participated >= 3
        THEN TRUE
        ELSE FALSE
    END AS is_active
FROM base_stats;
