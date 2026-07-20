-- Compute committee activity in set-based joins.  The public column contract
-- and each account's active_since boundary remain unchanged.

SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '60s';

CREATE OR REPLACE VIEW public.committee_vote_activity_view AS
WITH eligible_accounts AS (
    SELECT a.user_id, a.account_status, a.active_since
    FROM public.accounts a
    WHERE LOWER(a.user_id) NOT IN ('admin', 'developer', 'kiosk', 'gemini')
      AND a.user_id<>''
      AND COALESCE(a.account_disabled, FALSE)=FALSE
),
all_events AS (
    SELECT tv.topic_text, tv.created_at, 'tv'::TEXT AS vote_source
    FROM public.topic_votes tv
    JOIN (
        SELECT DISTINCT topic_text
        FROM public.topic_vote_ballots
    ) ballots ON ballots.topic_text=tv.topic_text
    UNION ALL
    SELECT removal.topic_text, removal.created_at, 'tdv'::TEXT AS vote_source
    FROM public.topic_removal_votes removal
    JOIN (
        SELECT DISTINCT topic_text
        FROM public.topic_removal_vote_ballots
    ) ballots ON ballots.topic_text=removal.topic_text
),
event_ballots AS (
    SELECT
        ballot.topic_text,
        ballot.user_id,
        ballot.vote_choice,
        'tv'::TEXT AS vote_source
    FROM public.topic_vote_ballots ballot
    UNION ALL
    SELECT
        ballot.topic_text,
        ballot.user_id,
        ballot.vote_choice,
        'tdv'::TEXT AS vote_source
    FROM public.topic_removal_vote_ballots ballot
),
eligible_activity AS (
    SELECT
        account.user_id,
        event.topic_text,
        event.vote_source,
        event.created_at,
        ballot.vote_choice,
        ROW_NUMBER() OVER (
            PARTITION BY account.user_id
            ORDER BY event.created_at DESC
        ) AS event_recency
    FROM eligible_accounts account
    JOIN all_events event
      ON account.active_since IS NULL
      OR event.created_at::DATE>=account.active_since
    LEFT JOIN event_ballots ballot
      ON ballot.topic_text=event.topic_text
     AND ballot.vote_source=event.vote_source
     AND ballot.user_id=account.user_id
),
base_stats AS (
    SELECT
        account.user_id,
        account.account_status,
        COUNT(activity.topic_text) AS total_votes,
        COUNT(activity.vote_choice) AS participated_votes,
        COUNT(activity.vote_choice) FILTER (
            WHERE activity.event_recency<=10
        ) AS last10_participated,
        COUNT(activity.vote_choice) AS total_ballots,
        COUNT(activity.vote_choice) FILTER (
            WHERE activity.vote_choice='agree'
        ) AS agree_ballots
    FROM eligible_accounts account
    LEFT JOIN eligible_activity activity
      ON activity.user_id=account.user_id
    GROUP BY account.user_id, account.account_status
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

