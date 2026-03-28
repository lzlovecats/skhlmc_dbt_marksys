"""
schema.py — Centralised database schema definitions.

This file is the single source of truth for all table schemas.
"""

from sqlalchemy import text


# Table: ACCOUNTS
# Committee member accounts.
# acc_type: 'admin' | 'active' | 'inactive'
# userpw stores bcrypt hashes. Use hash_password() from functions.py when creating/updating accounts.
# Legacy plaintext passwords are still accepted at login (see _verify_password) until migrated.
# tg_userid / tg_chatid: Telegram user ID and chat ID for push notifications (NULL = not linked).
CREATE_ACCOUNTS = """
CREATE TABLE IF NOT EXISTS accounts (
    userid      TEXT    PRIMARY KEY,
    userpw      TEXT,
    acc_type    TEXT    DEFAULT 'inactive',
    tg_userid   TEXT    UNIQUE,
    tg_chatid   TEXT    UNIQUE
);
"""

# Table: MATCHES
# Stores debate match metadata. Debater names live in DEBATERS.
CREATE_MATCHES = """
CREATE TABLE IF NOT EXISTS matches (
    match_id        TEXT    PRIMARY KEY,
    date            DATE,
    time            TIME,
    topic           TEXT,
    pro_team        TEXT,
    con_team        TEXT,
    access_code     TEXT,
    review_password TEXT
);
"""

# Table: TOPICS
# The approved debate topic bank.
CREATE_TOPICS = """
CREATE TABLE IF NOT EXISTS topics (
    topic       TEXT    PRIMARY KEY,
    author      TEXT,
    category    TEXT,
    difficulty  INTEGER
);
"""

# Table: DEBATERS
# One row per debater per match. Extracted from the old flat pro_1~con_4 columns.
# side: 'pro' | 'con'   position: 1=主辯 2=一副 3=二副 4=結辯
CREATE_DEBATERS = """
CREATE TABLE IF NOT EXISTS debaters (
    match_id    TEXT,
    side        TEXT    CHECK (side IN ('pro', 'con')),
    position    INTEGER CHECK (position BETWEEN 1 AND 4),
    name        TEXT,
    PRIMARY KEY (match_id, side, position),
    CONSTRAINT fk_debaters_match
        FOREIGN KEY (match_id) REFERENCES matches(match_id)
        ON DELETE CASCADE
);
"""

# Table: SCORES
# Stores finalised judge scoresheets (immutable after submission).
# pro_name / con_name removed — derive from matches via JOIN.
# Individual debater scores moved to DEBATER_SCORES.
# One row per (match_id, judge_name) — enforced by UNIQUE constraint.
CREATE_SCORES = """
CREATE TABLE IF NOT EXISTS scores (
    match_id        TEXT,
    judge_name      TEXT,
    pro_total       INTEGER,
    con_total       INTEGER,
    mark_time       TEXT,
    pro_free        INTEGER,
    con_free        INTEGER,
    pro_deduction   INTEGER,
    con_deduction   INTEGER,
    pro_coherence   INTEGER,
    con_coherence   INTEGER,
    UNIQUE (match_id, judge_name),
    CONSTRAINT fk_scores_match
        FOREIGN KEY (match_id) REFERENCES matches(match_id)
        ON DELETE CASCADE
);
"""

# Table: DEBATER_SCORES
# One row per debater per judge per match. Extracted from the old flat pro1_m~con4_m columns.
# side: 'pro' | 'con'   position: 1=主辯 2=一副 3=二副 4=結辯
CREATE_DEBATER_SCORES = """
CREATE TABLE IF NOT EXISTS debater_scores (
    match_id    TEXT,
    judge_name  TEXT,
    side        TEXT    CHECK (side IN ('pro', 'con')),
    position    INTEGER CHECK (position BETWEEN 1 AND 4),
    score       INTEGER,
    PRIMARY KEY (match_id, judge_name, side, position),
    CONSTRAINT fk_debater_scores_score
        FOREIGN KEY (match_id, judge_name) REFERENCES scores(match_id, judge_name)
        ON DELETE CASCADE
        ON UPDATE CASCADE
);
"""

# Table: TEMP_SCORES
# Cloud auto-save drafts for judges (overwritten on each save).
# `data` is a JSON blob containing the full scoring state including
# raw DataFrames serialised to JSON strings.
CREATE_TEMP_SCORES = """
CREATE TABLE IF NOT EXISTS temp_scores (
    match_id    TEXT,
    judge_name  TEXT,
    team_side   TEXT,
    data        TEXT,
    is_final    BOOLEAN DEFAULT FALSE,
    updated_at  TIMESTAMP,
    CONSTRAINT temp_scores_match_judge_side_key
        UNIQUE (match_id, judge_name, team_side),
    CONSTRAINT fk_temp_scores_match
        FOREIGN KEY (match_id) REFERENCES matches(match_id)
        ON DELETE CASCADE
);
"""

# Table: TOPIC_VOTES
# Pending/resolved votes on newly proposed topics.
# Per-voter ballots live in TOPIC_VOTE_BALLOTS.
CREATE_TOPIC_VOTES = """
CREATE TABLE IF NOT EXISTS topic_votes (
    topic       TEXT    PRIMARY KEY,
    author      TEXT,
    status      TEXT    DEFAULT 'pending',
    created_at  TIMESTAMP,
    deadline    DATE,
    threshold   INTEGER,
    category    TEXT,
    difficulty  INTEGER,
    CONSTRAINT fk_topic_votes_author
        FOREIGN KEY (author) REFERENCES accounts(userid)
        ON DELETE SET NULL
);
"""

# Table: TOPIC_VOTE_BALLOTS
# One row per (topic, voter). Extracted from the old agree_users / against_users arrays.
# reasons stores the voter's against-reasons as a JSON array (empty for agree votes).
CREATE_TOPIC_VOTE_BALLOTS = """
CREATE TABLE IF NOT EXISTS topic_vote_ballots (
    topic       TEXT,
    user_id     TEXT,
    vote        TEXT    CHECK (vote IN ('agree', 'against')),
    reasons     JSONB   DEFAULT '[]',
    PRIMARY KEY (topic, user_id),
    CONSTRAINT fk_topic_vote_ballots_topic
        FOREIGN KEY (topic) REFERENCES topic_votes(topic)
        ON DELETE CASCADE,
    CONSTRAINT fk_topic_vote_ballots_user
        FOREIGN KEY (user_id) REFERENCES accounts(userid)
        ON DELETE CASCADE
);
"""

# Table: TOPIC_DEPOSE_VOTES
# Motions to remove an existing topic from the bank.
# proposal_reasons stores the mover's reasons (not per-voter — stays on this table).
# Per-voter ballots live in DEPOSE_VOTE_BALLOTS.
# status: 'pending' | 'passed' | 'rejected'  (mirrors topic_votes lifecycle)
CREATE_TOPIC_DEPOSE_VOTES = """
CREATE TABLE IF NOT EXISTS topic_depose_votes (
    topic               TEXT    PRIMARY KEY,
    mover               TEXT,
    status              TEXT    DEFAULT 'pending',
    proposal_reasons    JSONB   DEFAULT '[]',
    created_at          TIMESTAMP,
    deadline            DATE,
    threshold           INTEGER,
    CONSTRAINT fk_topic_depose_votes_topic
        FOREIGN KEY (topic) REFERENCES topics(topic)
        ON DELETE CASCADE,
    CONSTRAINT fk_topic_depose_votes_mover
        FOREIGN KEY (mover) REFERENCES accounts(userid)
        ON DELETE SET NULL
);
"""

# Table: DEPOSE_VOTE_BALLOTS
# One row per (topic, voter). Extracted from the old agree_users / against_users arrays.
# No per-voter reasons for depose votes (reasons belong to the motion, not voters).
CREATE_DEPOSE_VOTE_BALLOTS = """
CREATE TABLE IF NOT EXISTS depose_vote_ballots (
    topic       TEXT,
    user_id     TEXT,
    vote        TEXT    CHECK (vote IN ('agree', 'against')),
    PRIMARY KEY (topic, user_id),
    CONSTRAINT fk_depose_vote_ballots_topic
        FOREIGN KEY (topic) REFERENCES topic_depose_votes(topic)
        ON DELETE CASCADE,
    CONSTRAINT fk_depose_vote_ballots_user
        FOREIGN KEY (user_id) REFERENCES accounts(userid)
        ON DELETE CASCADE
);
"""

# Table: LOGIN_RECORD
# Audit log for all logins (committee personal accounts, admin, score review).
# login_type: 'committee' | 'admin' | 'score_review'
CREATE_LOGIN_RECORD = """
CREATE TABLE IF NOT EXISTS login_record (
    id          SERIAL      PRIMARY KEY,
    user_id     TEXT,
    login_type  TEXT,
    login_time  TIMESTAMP,
    CONSTRAINT fk_login_record_user
        FOREIGN KEY (user_id) REFERENCES accounts(userid)
        ON DELETE SET NULL
);
"""

# Table: NOTI
# Tracks which committee members have seen each notification.
# notiid    — matches the NOTI_ID defined in assets/noti.md; increment to re-trigger all users.
# notititle — denormalised title stored at read-time for audit convenience.
# userid    — the member who dismissed the popup.
# seen_at   — HKT timestamp when the popup was dismissed.
CREATE_NOTI = """
CREATE TABLE IF NOT EXISTS noti (
    notiid      INT,
    notititle   VARCHAR(255),
    userid      VARCHAR(50),
    seen_at     TIMESTAMP,
    PRIMARY KEY (notiid, userid),
    CONSTRAINT fk_noti_user
        FOREIGN KEY (userid) REFERENCES accounts(userid)
        ON DELETE CASCADE
);
"""


# Table: TG_NOTIFICATION_QUEUE
# Decouples Streamlit from the Telegram bot service.
# Streamlit writes notification events here; the bot's scheduler drains them every 15 minutes.
# noti_type: 'new_topic' | 'new_depose' | 'vote_result'
# payload: JSONB blob with all data needed to render the notification message.
CREATE_TG_NOTIFICATION_QUEUE = """
CREATE TABLE IF NOT EXISTS tg_notification_queue (
    id          SERIAL      PRIMARY KEY,
    noti_type   TEXT        NOT NULL,
    payload     JSONB       NOT NULL,
    created_at  TIMESTAMP   DEFAULT NOW(),
    processed   BOOLEAN     DEFAULT FALSE,
    processing_token        TEXT,
    processing_started_at   TIMESTAMP,
    last_error              TEXT
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
    CREATE_TEMP_SCORES,         # → matches
    CREATE_TOPIC_VOTES,         # → accounts
    CREATE_TOPIC_VOTE_BALLOTS,  # → topic_votes, accounts
    CREATE_TOPIC_DEPOSE_VOTES,  # → topics, accounts
    CREATE_DEPOSE_VOTE_BALLOTS, # → topic_depose_votes, accounts
    CREATE_LOGIN_RECORD,        # → accounts
    CREATE_NOTI,                # → accounts
    CREATE_TG_NOTIFICATION_QUEUE,  # no deps
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
