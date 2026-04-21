"""
schema.py — Centralised database schema definitions.

This file is the single source of truth for all table schemas.
"""

from sqlalchemy import text

TABLE_ACCOUNTS = "accounts"
TABLE_MATCHES = "matches"
TABLE_TOPICS = "topics"
TABLE_DEBATERS = "debaters"
TABLE_SCORES = "scores"
TABLE_DEBATER_SCORES = "debater_scores"
TABLE_SCORE_DRAFTS = "score_drafts"
TABLE_TOPIC_VOTES = "topic_votes"
TABLE_TOPIC_VOTE_BALLOTS = "topic_vote_ballots"
TABLE_TOPIC_REMOVAL_VOTES = "topic_removal_votes"
TABLE_TOPIC_REMOVAL_VOTE_BALLOTS = "topic_removal_vote_ballots"
TABLE_LOGIN_RECORDS = "login_records"
TABLE_NOTIFICATION_READS = "notification_reads"
TABLE_TELEGRAM_NOTIFICATION_QUEUE = "telegram_notification_queue"
TABLE_TELEGRAM_LINK_TOKENS = "telegram_link_tokens"
VIEW_COMMITTEE_VOTE_ACTIVITY = "committee_vote_activity_view"


# Table: ACCOUNTS
# Committee member accounts.
# account_status: 'admin' | 'active' | 'inactive'
# password_hash stores bcrypt hashes. Use hash_password() from functions.py when creating/updating accounts.
# Legacy plaintext passwords are still accepted at login (see _verify_password) until migrated.
# telegram_user_id / telegram_chat_id: Telegram user ID and chat ID for push notifications (NULL = not linked).
CREATE_ACCOUNTS = f"""
CREATE TABLE IF NOT EXISTS {TABLE_ACCOUNTS} (
    user_id             TEXT    PRIMARY KEY,
    password_hash       TEXT,
    account_status      TEXT    DEFAULT 'inactive',
    telegram_user_id    TEXT    UNIQUE,
    telegram_chat_id    TEXT    UNIQUE
);
"""

# Table: MATCHES
# Stores debate match metadata. Debater names live in DEBATERS.
CREATE_MATCHES = f"""
CREATE TABLE IF NOT EXISTS {TABLE_MATCHES} (
    match_id               TEXT    PRIMARY KEY,
    match_date             DATE,
    match_time             TIME,
    topic_text             TEXT,
    pro_team               TEXT,
    con_team               TEXT,
    access_code_hash       TEXT,
    review_password_hash   TEXT
);
"""

# Table: TOPICS
# The approved debate topic bank.
CREATE_TOPICS = f"""
CREATE TABLE IF NOT EXISTS {TABLE_TOPICS} (
    topic_text  TEXT    PRIMARY KEY,
    author      TEXT,
    category    TEXT,
    difficulty  INTEGER
);
"""

# Table: DEBATERS
# One row per debater per match. Extracted from the old flat pro_1~con_4 columns.
# side: 'pro' | 'con'   position: 1=主辯 2=一副 3=二副 4=結辯
CREATE_DEBATERS = f"""
CREATE TABLE IF NOT EXISTS {TABLE_DEBATERS} (
    match_id        TEXT,
    side            TEXT    CHECK (side IN ('pro', 'con')),
    position        INTEGER CHECK (position BETWEEN 1 AND 4),
    debater_name    TEXT,
    PRIMARY KEY (match_id, side, position),
    CONSTRAINT fk_debaters_match
        FOREIGN KEY (match_id) REFERENCES {TABLE_MATCHES}(match_id)
        ON DELETE CASCADE
);
"""

# Table: SCORES
# Stores finalised judge scoresheets (immutable after submission).
# pro_name / con_name removed — derive from matches via JOIN.
# Individual debater scores moved to DEBATER_SCORES.
# One row per (match_id, judge_name) — enforced by UNIQUE constraint.
CREATE_SCORES = f"""
CREATE TABLE IF NOT EXISTS {TABLE_SCORES} (
    match_id                 TEXT,
    judge_name               TEXT,
    pro_total_score          INTEGER,
    con_total_score          INTEGER,
    submitted_time           TEXT,
    pro_free_debate_score    INTEGER,
    con_free_debate_score    INTEGER,
    pro_deduction_points     INTEGER,
    con_deduction_points     INTEGER,
    pro_coherence_score      INTEGER,
    con_coherence_score      INTEGER,
    UNIQUE (match_id, judge_name),
    CONSTRAINT fk_scores_match
        FOREIGN KEY (match_id) REFERENCES {TABLE_MATCHES}(match_id)
        ON DELETE CASCADE
);
"""

# Table: DEBATER_SCORES
# One row per debater per judge per match. Extracted from the old flat pro1_m~con4_m columns.
# side: 'pro' | 'con'   position: 1=主辯 2=一副 3=二副 4=結辯
CREATE_DEBATER_SCORES = f"""
CREATE TABLE IF NOT EXISTS {TABLE_DEBATER_SCORES} (
    match_id        TEXT,
    judge_name      TEXT,
    side            TEXT    CHECK (side IN ('pro', 'con')),
    position        INTEGER CHECK (position BETWEEN 1 AND 4),
    debater_score   INTEGER,
    PRIMARY KEY (match_id, judge_name, side, position),
    CONSTRAINT fk_debater_scores_score
        FOREIGN KEY (match_id, judge_name) REFERENCES {TABLE_SCORES}(match_id, judge_name)
        ON DELETE CASCADE
        ON UPDATE CASCADE
);
"""

# Table: SCORE_DRAFTS
# Cloud auto-save drafts for judges (overwritten on each save).
# `score_payload` is a JSON blob containing the full scoring state including
# raw DataFrames serialised to JSON strings.
CREATE_SCORE_DRAFTS = f"""
CREATE TABLE IF NOT EXISTS {TABLE_SCORE_DRAFTS} (
    match_id        TEXT,
    judge_name      TEXT,
    side            TEXT,
    score_payload   TEXT,
    is_final        BOOLEAN DEFAULT FALSE,
    updated_at      TIMESTAMP,
    CONSTRAINT score_drafts_match_judge_side_key
        UNIQUE (match_id, judge_name, side),
    CONSTRAINT fk_score_drafts_match
        FOREIGN KEY (match_id) REFERENCES {TABLE_MATCHES}(match_id)
        ON DELETE CASCADE
);
"""

# Table: TOPIC_VOTES
# Pending/resolved votes on newly proposed topics.
# Per-voter ballots live in TOPIC_VOTE_BALLOTS.
CREATE_TOPIC_VOTES = f"""
CREATE TABLE IF NOT EXISTS {TABLE_TOPIC_VOTES} (
    topic_text          TEXT    PRIMARY KEY,
    proposer_user_id    TEXT,
    status              TEXT    DEFAULT 'pending',
    created_at          TIMESTAMP,
    deadline_date       DATE,
    approval_threshold  INTEGER,
    category            TEXT,
    difficulty          INTEGER,
    CONSTRAINT fk_topic_votes_proposer_user
        FOREIGN KEY (proposer_user_id) REFERENCES {TABLE_ACCOUNTS}(user_id)
        ON DELETE SET NULL
);
"""

# Table: TOPIC_VOTE_BALLOTS
# One row per (topic, voter). Extracted from the old agree_users / against_users arrays.
# against_reasons stores the voter's against-reasons as a JSON array (empty for agree votes).
CREATE_TOPIC_VOTE_BALLOTS = f"""
CREATE TABLE IF NOT EXISTS {TABLE_TOPIC_VOTE_BALLOTS} (
    topic_text          TEXT,
    user_id             TEXT,
    vote_choice         TEXT    CHECK (vote_choice IN ('agree', 'against')),
    against_reasons     JSONB   DEFAULT '[]',
    PRIMARY KEY (topic_text, user_id),
    CONSTRAINT fk_topic_vote_ballots_topic
        FOREIGN KEY (topic_text) REFERENCES {TABLE_TOPIC_VOTES}(topic_text)
        ON DELETE CASCADE,
    CONSTRAINT fk_topic_vote_ballots_user
        FOREIGN KEY (user_id) REFERENCES {TABLE_ACCOUNTS}(user_id)
        ON DELETE CASCADE
);
"""

# Table: TOPIC_REMOVAL_VOTES
# Motions to remove an existing topic from the bank.
# removal_reasons stores the mover's reasons (not per-voter — stays on this table).
# Per-voter ballots live in TOPIC_REMOVAL_VOTE_BALLOTS.
# status: 'pending' | 'passed' | 'rejected'  (mirrors topic_votes lifecycle)
CREATE_TOPIC_REMOVAL_VOTES = f"""
CREATE TABLE IF NOT EXISTS {TABLE_TOPIC_REMOVAL_VOTES} (
    topic_text          TEXT    PRIMARY KEY,
    proposer_user_id    TEXT,
    status              TEXT    DEFAULT 'pending',
    removal_reasons     JSONB   DEFAULT '[]',
    created_at          TIMESTAMP,
    deadline_date       DATE,
    approval_threshold  INTEGER,
    CONSTRAINT fk_topic_removal_votes_topic
        FOREIGN KEY (topic_text) REFERENCES {TABLE_TOPICS}(topic_text)
        ON DELETE CASCADE,
    CONSTRAINT fk_topic_removal_votes_proposer_user
        FOREIGN KEY (proposer_user_id) REFERENCES {TABLE_ACCOUNTS}(user_id)
        ON DELETE SET NULL
);
"""

# Table: TOPIC_REMOVAL_VOTE_BALLOTS
# One row per (topic, voter). Extracted from the old agree_users / against_users arrays.
# No per-voter reasons for removal votes (reasons belong to the motion, not voters).
CREATE_TOPIC_REMOVAL_VOTE_BALLOTS = f"""
CREATE TABLE IF NOT EXISTS {TABLE_TOPIC_REMOVAL_VOTE_BALLOTS} (
    topic_text      TEXT,
    user_id         TEXT,
    vote_choice     TEXT    CHECK (vote_choice IN ('agree', 'against')),
    PRIMARY KEY (topic_text, user_id),
    CONSTRAINT fk_topic_removal_vote_ballots_topic
        FOREIGN KEY (topic_text) REFERENCES {TABLE_TOPIC_REMOVAL_VOTES}(topic_text)
        ON DELETE CASCADE,
    CONSTRAINT fk_topic_removal_vote_ballots_user
        FOREIGN KEY (user_id) REFERENCES {TABLE_ACCOUNTS}(user_id)
        ON DELETE CASCADE
);
"""

# Table: LOGIN_RECORDS
# Audit log for all logins (committee personal accounts, admin, score review).
# login_type: 'committee' | 'admin' | 'score_review'
CREATE_LOGIN_RECORDS = f"""
CREATE TABLE IF NOT EXISTS {TABLE_LOGIN_RECORDS} (
    id              SERIAL      PRIMARY KEY,
    user_id         TEXT,
    login_type      TEXT,
    logged_in_at    TIMESTAMP,
    CONSTRAINT fk_login_records_user
        FOREIGN KEY (user_id) REFERENCES {TABLE_ACCOUNTS}(user_id)
        ON DELETE SET NULL
);
"""

# Table: NOTIFICATION_READS
# Tracks which committee members have seen each notification.
# notification_id    — matches the NOTI_ID defined in assets/noti.md; increment to re-trigger all users.
# notification_title — denormalised title stored at read-time for audit convenience.
# user_id            — the member who dismissed the popup.
# read_at            — HKT timestamp when the popup was dismissed.
CREATE_NOTIFICATION_READS = f"""
CREATE TABLE IF NOT EXISTS {TABLE_NOTIFICATION_READS} (
    notification_id      INT,
    notification_title   VARCHAR(255),
    user_id              VARCHAR(50),
    read_at              TIMESTAMP,
    PRIMARY KEY (notification_id, user_id),
    CONSTRAINT fk_notification_reads_user
        FOREIGN KEY (user_id) REFERENCES {TABLE_ACCOUNTS}(user_id)
        ON DELETE CASCADE
);
"""


# Table: TELEGRAM_NOTIFICATION_QUEUE
# Decouples Streamlit from the Telegram bot service.
# Streamlit writes notification events here; the bot's scheduler drains them every 15 minutes.
# notification_type: 'new_topic' | 'new_depose' | 'vote_result'
# payload: JSONB blob with all data needed to render the notification message.
CREATE_TELEGRAM_NOTIFICATION_QUEUE = f"""
CREATE TABLE IF NOT EXISTS {TABLE_TELEGRAM_NOTIFICATION_QUEUE} (
    id                      SERIAL      PRIMARY KEY,
    notification_type       TEXT        NOT NULL,
    payload                 JSONB       NOT NULL,
    created_at              TIMESTAMP   DEFAULT NOW(),
    is_processed            BOOLEAN     DEFAULT FALSE,
    processing_token        TEXT,
    processing_started_at   TIMESTAMP,
    last_error_message      TEXT
);
"""

# Table: TELEGRAM_LINK_TOKENS
# Stores one-time Telegram linking codes generated from the website account page.
# token_hash stores the SHA-256 hash of the normalized code; plaintext codes are never persisted.
CREATE_TELEGRAM_LINK_TOKENS = f"""
CREATE TABLE IF NOT EXISTS {TABLE_TELEGRAM_LINK_TOKENS} (
    token_hash       TEXT        PRIMARY KEY,
    user_id          TEXT        NOT NULL,
    issued_at        TIMESTAMP   NOT NULL,
    expires_at       TIMESTAMP   NOT NULL,
    consumed_at      TIMESTAMP,
    CONSTRAINT fk_telegram_link_tokens_user
        FOREIGN KEY (user_id) REFERENCES {TABLE_ACCOUNTS}(user_id)
        ON DELETE CASCADE
);
"""

# View: COMMITTEE_VOTE_ACTIVITY
# Canonical source for committee participation metrics used by both Streamlit and the Telegram Worker.
CREATE_COMMITTEE_VOTE_ACTIVITY_VIEW = f"""
CREATE OR REPLACE VIEW {VIEW_COMMITTEE_VOTE_ACTIVITY} AS
WITH tv_events AS (
    SELECT DISTINCT tv.topic_text, tv.created_at
    FROM {TABLE_TOPIC_VOTES} tv
    WHERE EXISTS (
        SELECT 1 FROM {TABLE_TOPIC_VOTE_BALLOTS} b
        WHERE b.topic_text = tv.topic_text
    )
),
tdv_events AS (
    SELECT DISTINCT tdv.topic_text, tdv.created_at
    FROM {TABLE_TOPIC_REMOVAL_VOTES} tdv
    WHERE EXISTS (
        SELECT 1 FROM {TABLE_TOPIC_REMOVAL_VOTE_BALLOTS} b
        WHERE b.topic_text = tdv.topic_text
    )
),
all_events AS (
    SELECT topic_text, created_at, 'tv' AS vote_source FROM tv_events
    UNION ALL
    SELECT topic_text, created_at, 'tdv' AS vote_source FROM tdv_events
),
event_count AS (
    SELECT COUNT(*) AS total_votes FROM all_events
),
past_10 AS (
    SELECT topic_text, vote_source
    FROM all_events
    ORDER BY created_at DESC
    LIMIT 10
),
ballot_summary AS (
    SELECT
        user_id,
        COUNT(*) AS total_ballots,
        SUM(CASE WHEN vote_choice = 'agree' THEN 1 ELSE 0 END) AS agree_ballots
    FROM (
        SELECT user_id, vote_choice FROM {TABLE_TOPIC_VOTE_BALLOTS}
        UNION ALL
        SELECT user_id, vote_choice FROM {TABLE_TOPIC_REMOVAL_VOTE_BALLOTS}
    ) combined_ballots
    GROUP BY user_id
),
base_stats AS (
    SELECT
        a.user_id,
        a.telegram_chat_id,
        a.account_status,
        COALESCE((SELECT total_votes FROM event_count), 0) AS total_votes,
        (
            SELECT COUNT(*) FROM all_events ae
            WHERE (
                ae.vote_source = 'tv'
                AND EXISTS (
                    SELECT 1 FROM {TABLE_TOPIC_VOTE_BALLOTS} b
                    WHERE b.topic_text = ae.topic_text
                      AND b.user_id = a.user_id
                )
            ) OR (
                ae.vote_source = 'tdv'
                AND EXISTS (
                    SELECT 1 FROM {TABLE_TOPIC_REMOVAL_VOTE_BALLOTS} b
                    WHERE b.topic_text = ae.topic_text
                      AND b.user_id = a.user_id
                )
            )
        ) AS participated_votes,
        (
            SELECT COUNT(*) FROM past_10 p
            WHERE (
                p.vote_source = 'tv'
                AND EXISTS (
                    SELECT 1 FROM {TABLE_TOPIC_VOTE_BALLOTS} b
                    WHERE b.topic_text = p.topic_text
                      AND b.user_id = a.user_id
                )
            ) OR (
                p.vote_source = 'tdv'
                AND EXISTS (
                    SELECT 1 FROM {TABLE_TOPIC_REMOVAL_VOTE_BALLOTS} b
                    WHERE b.topic_text = p.topic_text
                      AND b.user_id = a.user_id
                )
            )
        ) AS last10_participated,
        COALESCE(bs.total_ballots, 0) AS total_ballots,
        COALESCE(bs.agree_ballots, 0) AS agree_ballots
    FROM {TABLE_ACCOUNTS} a
    LEFT JOIN ballot_summary bs ON bs.user_id = a.user_id
    WHERE a.user_id NOT IN ('admin', 'developer', '')
)
SELECT
    user_id,
    telegram_chat_id,
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
"""

# Indices — created after tables so FK targets exist.
# idx_tv_status: speeds up the WHERE status='pending' filter in get_vote_data
# idx_tvb_user_id / idx_trvb_user_id: speed up participation stats UNION ALL queries filtering by user_id
CREATE_INDICES = f"""
CREATE INDEX IF NOT EXISTS idx_tv_status ON {TABLE_TOPIC_VOTES}(status);
CREATE INDEX IF NOT EXISTS idx_tvb_user_id ON {TABLE_TOPIC_VOTE_BALLOTS}(user_id);
CREATE INDEX IF NOT EXISTS idx_tvb_topic_text ON {TABLE_TOPIC_VOTE_BALLOTS}(topic_text);
CREATE INDEX IF NOT EXISTS idx_trvb_user_id ON {TABLE_TOPIC_REMOVAL_VOTE_BALLOTS}(user_id);
CREATE INDEX IF NOT EXISTS idx_trvb_topic_text ON {TABLE_TOPIC_REMOVAL_VOTE_BALLOTS}(topic_text);
CREATE INDEX IF NOT EXISTS idx_telegram_notification_queue_claim
    ON {TABLE_TELEGRAM_NOTIFICATION_QUEUE}(is_processed, processing_token, created_at);
CREATE INDEX IF NOT EXISTS idx_telegram_link_tokens_user_id
    ON {TABLE_TELEGRAM_LINK_TOKENS}(user_id, consumed_at, expires_at);
"""

# System-wide configuration (e.g. hashed passwords managed via the 開發者設定 page)
CREATE_SYSTEM_CONFIG = """
CREATE TABLE IF NOT EXISTS system_config (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at TEXT
);
"""

# Ordered list of all CREATE statements (dependency order).
# Tables must be created before any table that references them via FK.
ALL_SCHEMAS = [
    CREATE_ACCOUNTS,            # no deps
    CREATE_MATCHES,             # no deps
    CREATE_TOPICS,              # no deps
    CREATE_DEBATERS,            # → matches
    CREATE_SCORES,              # → matches
    CREATE_DEBATER_SCORES,      # → scores
    CREATE_SCORE_DRAFTS,        # → matches
    CREATE_TOPIC_VOTES,         # → accounts
    CREATE_TOPIC_VOTE_BALLOTS,  # → topic_votes, accounts
    CREATE_TOPIC_REMOVAL_VOTES,         # → topics, accounts
    CREATE_TOPIC_REMOVAL_VOTE_BALLOTS,  # → topic_removal_votes, accounts
    CREATE_LOGIN_RECORDS,              # → accounts
    CREATE_NOTIFICATION_READS,         # → accounts
    CREATE_TELEGRAM_NOTIFICATION_QUEUE,  # no deps
    CREATE_TELEGRAM_LINK_TOKENS,         # → accounts
    CREATE_SYSTEM_CONFIG,                # no deps
    CREATE_COMMITTEE_VOTE_ACTIVITY_VIEW, # after all tables
    CREATE_INDICES,                      # after all tables
]


def init_db(conn) -> None:
    """
    Create all tables if they do not already exist.

    Parameters
    ----------
    conn : SQLAlchemy connection / session, or a Streamlit SQLConnection object.
           Accepts both raw SQLAlchemy sessions and Streamlit's st.connection wrapper.

    Example
    -------
    # With Streamlit connection:
    from schema import init_db
    import .streamlit as st
    conn = st.connection("postgresql", type="sql")
    init_db(conn)

    # With a raw SQLAlchemy engine:
    from sqlalchemy import create_engine
    engine = create_engine("postgresql://...")
    with engine.connect() as raw_conn:
        init_db(raw_conn)
    """
    # Support both Streamlit SQLConnection and raw SQLAlchemy session/connection
    if hasattr(conn, "session"):
        with conn.session as s:
            for ddl in ALL_SCHEMAS:
                s.execute(text(ddl))
            s.commit()
    else:
        for ddl in ALL_SCHEMAS:
            conn.execute(text(ddl))
        conn.commit()
