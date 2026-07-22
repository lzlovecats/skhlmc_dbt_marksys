"""
schema.py — Centralised database schema definitions.

This file is the single source of truth for all table schemas.
"""

from sqlalchemy import text

from account_access import NON_MEMBER_ACCOUNT_DB_KEYS, sql_account_id_literals

TABLE_ACCOUNTS = "accounts"
TABLE_MATCHES = "matches"
TABLE_TOPICS = "topics"
TABLE_DEBATERS = "debaters"
TABLE_SCORES = "scores"
TABLE_DEBATER_SCORES = "debater_scores"
TABLE_SCORE_DRAFTS = "score_drafts"
TABLE_SCORE_SHEET_CONFIRMATIONS = "score_sheet_confirmations"
TABLE_MATCH_TOPIC_RELEASES = "match_topic_releases"
TABLE_TOPIC_VOTES = "topic_votes"
TABLE_TOPIC_VOTE_BALLOTS = "topic_vote_ballots"
TABLE_TOPIC_REMOVAL_VOTES = "topic_removal_votes"
TABLE_TOPIC_REMOVAL_VOTE_BALLOTS = "topic_removal_vote_ballots"
TABLE_LOGIN_RECORDS = "login_records"
TABLE_NOTIFICATION_READS = "notification_reads"
TABLE_PUSH_SUBSCRIPTIONS = "push_subscriptions"
TABLE_COMPETITION_REGISTRATION_SETTINGS = "competition_registration_settings"
TABLE_COMPETITION_REGISTRATIONS = "competition_registrations"
TABLE_MATCH_VIDEOS = "match_videos"
TABLE_VIDEO_VIEWS = "video_views"
TABLE_VIDEO_COMMENTS = "video_comments"
TABLE_VIDEO_VOTES = "video_votes"
TABLE_VIDEO_CHAPTERS = "video_chapters"
TABLE_VIDEO_ROSTER = "video_roster"
TABLE_VIDEO_PROGRESS = "video_progress"
TABLE_MATCH_PHOTOS = "match_photos"
TABLE_RECENT_MATCHES = "recent_matches"
TABLE_RECENT_MATCH_NOTIFICATIONS = "recent_match_notifications"
TABLE_COMPETITION_PREP_PROJECTS = "competition_prep_projects"
TABLE_COMPETITION_PREP_MEMBERS = "competition_prep_members"
TABLE_COMPETITION_PREP_MANUSCRIPTS = "competition_prep_manuscripts"
TABLE_COMPETITION_PREP_STRATEGY_CARDS = "competition_prep_strategy_cards"
TABLE_COMPETITION_PREP_EVIDENCE_CARDS = "competition_prep_evidence_cards"
TABLE_COMPETITION_PREP_WEAKNESSES = "competition_prep_weaknesses"
TABLE_COMPETITION_PREP_AI_RUNS = "competition_prep_ai_runs"
TABLE_COMMITTEE_MEMBERSHIPS = "committee_memberships"
TABLE_HISTORY_EVENTS = "history_events"
TABLE_HISTORY_EVENT_MATCHES = "history_event_matches"
TABLE_HISTORY_EVENT_PHOTOS = "history_event_photos"
TABLE_GHOST_FORUM_THREADS = "ghost_forum_threads"
TABLE_GHOST_FORUM_POSTS = "ghost_forum_posts"
TABLE_GHOST_FORUM_REACTIONS = "ghost_forum_reactions"
TABLE_GHOST_FORUM_THREAD_VIDEOS = "ghost_forum_thread_videos"
TABLE_GHOST_FORUM_THREAD_PHOTOS = "ghost_forum_thread_photos"
TABLE_GHOST_FORUM_THREAD_HISTORY_EVENTS = "ghost_forum_thread_history_events"
TABLE_GHOST_FORUM_USER_PROFILES = "ghost_forum_user_profiles"
TABLE_GHOST_FORUM_THREAD_USER_STATE = "ghost_forum_thread_user_state"
TABLE_GHOST_FORUM_NOTIFICATIONS = "ghost_forum_notifications"
TABLE_TTS_VOICE_CONSENTS = "tts_voice_consents"
TABLE_TTS_VOICE_RECORDINGS = "tts_voice_recordings"
TABLE_TTS_SCRIPTS = "tts_scripts"
TABLE_TTS_LEXICON = "tts_lexicon"
TABLE_LLM_TRAINING_SUBMISSIONS = "llm_training_submissions"
TABLE_AI_DATASET_SNAPSHOTS = "ai_dataset_snapshots"
TABLE_AI_DATASET_SNAPSHOT_ITEMS = "ai_dataset_snapshot_items"
TABLE_AI_MODEL_VERSIONS = "ai_model_versions"
TABLE_RAG_DOCUMENTS = "rag_documents"
TABLE_RAG_CHUNKS = "rag_chunks"
TABLE_AI_TRAINING_AUDIT = "ai_training_audit"
TABLE_AI_FACTORY_SOURCES = "ai_factory_sources"
TABLE_AI_FACTORY_JOBS = "ai_factory_jobs"
TABLE_AI_FACTORY_ATTEMPTS = "ai_factory_attempts"
TABLE_AI_FACTORY_ITEMS = "ai_factory_items"
TABLE_AI_FACTORY_TOPIC_TAGS = "ai_factory_topic_tags"
TABLE_AI_FACTORY_ITEM_TAGS = "ai_factory_item_tags"
TABLE_AI_FACTORY_RELEASES = "ai_factory_releases"
TABLE_AI_FACTORY_RELEASE_ITEMS = "ai_factory_release_items"
TABLE_AI_FACTORY_TRANSCRIPTS = "ai_factory_transcripts"
TABLE_AI_FACTORY_TRANSCRIPT_RUNS = "ai_factory_transcript_runs"
TABLE_AI_FACTORY_TRANSCRIPT_WINDOWS = "ai_factory_transcript_windows"
TABLE_AI_FACTORY_TRANSCRIPT_ATTEMPTS = "ai_factory_transcript_attempts"
TABLE_AI_FACTORY_TRANSCRIPT_SEGMENTS = "ai_factory_transcript_segments"
TABLE_MATCH_ROSTER_LINKS = "match_roster_links"
TABLE_BEST_DEBATER_RANKINGS = "best_debater_rankings"
TABLE_MOTION_COMMENTS = "motion_comments"
TABLE_AI_FUND_TRANSACTIONS = "ai_fund_transactions"
TABLE_AI_FUND_USAGE_LOGS = "ai_fund_usage_logs"
TABLE_LATENESS_FUND_RECORDS = "lateness_fund_records"
TABLE_LATENESS_FUND_EXPENSES = "lateness_fund_expenses"
TABLE_LATENESS_FUND_PERIODS = "lateness_fund_periods"
TABLE_BUG_REPORTS = "bug_reports"
TABLE_BANDWIDTH_USAGE_LOGS = "bandwidth_usage_logs"
TABLE_R2_UPLOAD_INTENTS = "r2_upload_intents"
TABLE_MONTHLY_RESOURCE_LIMITS = "monthly_resource_limits"
TABLE_PROJECTOR_STATE = "projector_state"
TABLE_PROJECTOR_AI_SESSIONS = "projector_ai_sessions"
TABLE_PROJECTOR_AI_CONTROLS = "projector_ai_controls"
TABLE_PROJECTOR_AI_MARKERS = "projector_ai_markers"
TABLE_PROJECTOR_KIOSK_DEVICES = "projector_kiosk_devices"
TABLE_OFFICIAL_AI_JUDGE_RUNS = "official_ai_judge_runs"
TABLE_OFFICIAL_AI_JUDGE_ATTEMPTS = "official_ai_judge_attempts"
TABLE_AI_COACH_LIVE_BRIEFS = "ai_coach_live_briefs"
TABLE_LMC_AI_NODES = "lmc_ai_nodes"
TABLE_WORKSTATION_R2_HEALTH_PROBES = "workstation_r2_health_probes"
TABLE_APP_CONFIG = "app_config"
VIEW_COMMITTEE_VOTE_ACTIVITY = "committee_vote_activity_view"


# Table: ACCOUNTS
# Committee member accounts.
# account_status: 'admin' | 'active' | 'inactive'
# password_hash stores bcrypt hashes. Use core.auth_logic.hash_password when creating/updating accounts.
# Legacy plaintext passwords are still accepted at login (see _verify_password) until migrated.
CREATE_ACCOUNTS = f"""
CREATE TABLE IF NOT EXISTS {TABLE_ACCOUNTS} (
    user_id             VARCHAR(50)  PRIMARY KEY,
    password_hash       VARCHAR(255) NOT NULL,
    account_status      CHAR(10)     NOT NULL DEFAULT 'inactive',
    active_since        DATE    DEFAULT CURRENT_DATE,
    last_login_at       TIMESTAMP,
    account_disabled    BOOLEAN DEFAULT FALSE,
    CONSTRAINT accounts_status_check
        CHECK (BTRIM(account_status) IN ('admin', 'active', 'inactive'))
);
"""

# Table: MATCHES
# Stores debate match metadata. Debater names live in DEBATERS.
CREATE_MATCHES = f"""
CREATE TABLE IF NOT EXISTS {TABLE_MATCHES} (
    match_id               VARCHAR(255) PRIMARY KEY,
    match_date             DATE,
    match_time             TIME,
    topic_text             VARCHAR(255),
    pro_team               VARCHAR(255),
    con_team               VARCHAR(255),
    debate_format          TEXT    NOT NULL DEFAULT '校園隨想',
    free_debate_minutes    NUMERIC(4,1),
    expected_human_judge_count SMALLINT,
    access_code_hash       VARCHAR(255),
    review_password_hash   TEXT,
    CONSTRAINT matches_debate_format_check
        CHECK (debate_format IN ('校園隨想', '聯中', '星島', '基本法盃')),
    CONSTRAINT matches_free_debate_minutes_check
        CHECK (
            free_debate_minutes IS NULL
            OR (
                debate_format = '聯中'
                AND free_debate_minutes BETWEEN 2 AND 10
            )
        ),
    CONSTRAINT matches_expected_human_judge_count_check
        CHECK (
            expected_human_judge_count IS NULL
            OR expected_human_judge_count BETWEEN 1 AND 50
        )
);
"""

# Table: TOPICS
# The approved debate topic bank.
CREATE_TOPICS = f"""
CREATE TABLE IF NOT EXISTS {TABLE_TOPICS} (
    topic_text  VARCHAR(255) PRIMARY KEY,
    author      VARCHAR(50),
    category    VARCHAR(50),
    difficulty  INTEGER,
    CONSTRAINT difficulty_range_check
        CHECK (difficulty BETWEEN 1 AND 3),
    CONSTRAINT topics_author_fkey
        FOREIGN KEY (author) REFERENCES {TABLE_ACCOUNTS}(user_id)
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
# One row per (match_id, judge_name) — enforced by the composite primary key.
CREATE_SCORES = f"""
CREATE TABLE IF NOT EXISTS {TABLE_SCORES} (
    match_id                 VARCHAR(255) NOT NULL,
    judge_name               VARCHAR(100) NOT NULL,
    pro_total_score          INTEGER,
    con_total_score          INTEGER,
    submitted_time           TIME,
    pro_free_debate_score    INTEGER,
    con_free_debate_score    INTEGER,
    pro_deduction_points     INTEGER,
    con_deduction_points     INTEGER,
    pro_coherence_score      INTEGER,
    con_coherence_score      INTEGER,
    judge_kind               TEXT NOT NULL DEFAULT 'human'
                                CHECK (judge_kind IN ('human', 'ai')),
    PRIMARY KEY (match_id, judge_name),
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

# Table: BEST_DEBATER_RANKINGS
# Explicit best-debater rankings given by each judge (1 = best), using
# standard competition ranking for ties (for example 1, 1, 3).
# Falls back to auto-derived rankings from debater_scores when absent.
CREATE_BEST_DEBATER_RANKINGS = f"""
CREATE TABLE IF NOT EXISTS {TABLE_BEST_DEBATER_RANKINGS} (
    match_id    TEXT,
    judge_name  TEXT,
    side        TEXT    CHECK (side IN ('pro', 'con')),
    position    INTEGER CHECK (position BETWEEN 1 AND 4),
    rank        INTEGER CHECK (rank BETWEEN 1 AND 8),
    PRIMARY KEY (match_id, judge_name, side, position),
    CONSTRAINT fk_best_debater_rankings_score
        FOREIGN KEY (match_id, judge_name) REFERENCES {TABLE_SCORES}(match_id, judge_name)
        ON DELETE CASCADE
        ON UPDATE CASCADE
);
"""

# Table: SCORE_DRAFTS
# Cloud auto-save drafts for judges (overwritten on each save).
# `score_payload` stores the validated scoring state as structured JSON.
CREATE_SCORE_DRAFTS = f"""
CREATE TABLE IF NOT EXISTS {TABLE_SCORE_DRAFTS} (
    match_id        VARCHAR(255) NOT NULL,
    judge_name      VARCHAR(255) NOT NULL,
    side            CHAR(10) NOT NULL,
    score_payload   JSONB NOT NULL,
    is_final        BOOLEAN DEFAULT FALSE,
    updated_at      TIMESTAMP,
    PRIMARY KEY (match_id, judge_name, side),
    CONSTRAINT fk_score_drafts_match
        FOREIGN KEY (match_id) REFERENCES {TABLE_MATCHES}(match_id)
        ON DELETE CASCADE
);
"""

# Table: SCORE_SHEET_CONFIRMATIONS
# One private bearer link and acknowledgement state per match side. The score
# count binds each response to the exact set of judge sheets opened by staff.
CREATE_SCORE_SHEET_CONFIRMATIONS = f"""
CREATE TABLE IF NOT EXISTS {TABLE_SCORE_SHEET_CONFIRMATIONS} (
    match_id               TEXT,
    side                   TEXT        CHECK (side IN ('pro', 'con')),
    confirmation_token     TEXT        NOT NULL UNIQUE,
    status                 TEXT        NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'confirmed', 'disputed')),
    dispute_reason         TEXT        NOT NULL DEFAULT '',
    opened_score_count     INTEGER     NOT NULL CHECK (opened_score_count > 0),
    opened_at              TIMESTAMP   NOT NULL,
    responded_at           TIMESTAMP,
    PRIMARY KEY (match_id, side),
    CONSTRAINT score_sheet_confirmations_reason_check
        CHECK (
            char_length(dispute_reason) <= 2000
            AND (status <> 'disputed' OR btrim(dispute_reason) <> '')
        ),
    CONSTRAINT fk_score_sheet_confirmations_match
        FOREIGN KEY (match_id) REFERENCES {TABLE_MATCHES}(match_id)
        ON DELETE CASCADE
);
REVOKE ALL PRIVILEGES ON TABLE {TABLE_SCORE_SHEET_CONFIRMATIONS} FROM PUBLIC;
DO $$
DECLARE role_name TEXT;
BEGIN
    FOR role_name IN
        SELECT rolname FROM pg_roles
        WHERE rolname IN ('anon', 'authenticated')
    LOOP
        EXECUTE format(
            'REVOKE ALL PRIVILEGES ON TABLE {TABLE_SCORE_SHEET_CONFIRMATIONS} FROM %I',
            role_name
        );
    END LOOP;
END $$;
"""

# Table: MATCH_TOPIC_RELEASES
# Audited three-topic draw, side-scoped bearer links, and one veto per team.
CREATE_MATCH_TOPIC_RELEASES = f"""
CREATE TABLE IF NOT EXISTS {TABLE_MATCH_TOPIC_RELEASES} (
    id                       BIGSERIAL   PRIMARY KEY,
    match_id                 TEXT        NOT NULL,
    generation               INTEGER     NOT NULL CHECK (generation > 0),
    release_match_date       DATE        NOT NULL,
    release_match_time       TIME        NOT NULL,
    candidate_1              TEXT        NOT NULL,
    candidate_2              TEXT        NOT NULL,
    candidate_3              TEXT        NOT NULL,
    pro_token                TEXT        NOT NULL UNIQUE,
    con_token                TEXT        NOT NULL UNIQUE,
    first_reveal_at          TIMESTAMP   NOT NULL,
    first_veto_deadline      TIMESTAMP   NOT NULL,
    second_reveal_at         TIMESTAMP   NOT NULL,
    second_veto_deadline     TIMESTAMP   NOT NULL,
    third_reveal_at          TIMESTAMP   NOT NULL,
    expires_at               TIMESTAMP   NOT NULL,
    pro_veto_candidate       SMALLINT    CHECK (pro_veto_candidate IN (1, 2)),
    pro_veto_at              TIMESTAMP,
    con_veto_candidate       SMALLINT    CHECK (con_veto_candidate IN (1, 2)),
    con_veto_at              TIMESTAMP,
    created_at               TIMESTAMP   NOT NULL DEFAULT NOW(),
    tokens_rotated_at        TIMESTAMP,
    revoked_at               TIMESTAMP,
    UNIQUE (match_id, generation),
    CONSTRAINT match_topic_releases_topics_distinct
        CHECK (candidate_1 <> candidate_2 AND candidate_1 <> candidate_3 AND candidate_2 <> candidate_3),
    CONSTRAINT match_topic_releases_topic_lengths
        CHECK (
            char_length(candidate_1) BETWEEN 1 AND 500
            AND char_length(candidate_2) BETWEEN 1 AND 500
            AND char_length(candidate_3) BETWEEN 1 AND 500
        ),
    CONSTRAINT match_topic_releases_schedule_order
        CHECK (
            first_reveal_at < first_veto_deadline
            AND first_veto_deadline < second_reveal_at
            AND second_reveal_at < second_veto_deadline
            AND second_veto_deadline < third_reveal_at
            AND third_reveal_at < expires_at
        ),
    CONSTRAINT match_topic_releases_pro_veto_pair
        CHECK ((pro_veto_candidate IS NULL) = (pro_veto_at IS NULL)),
    CONSTRAINT match_topic_releases_con_veto_pair
        CHECK ((con_veto_candidate IS NULL) = (con_veto_at IS NULL)),
    CONSTRAINT match_topic_releases_distinct_vetoes
        CHECK (
            pro_veto_candidate IS NULL OR con_veto_candidate IS NULL
            OR pro_veto_candidate <> con_veto_candidate
        ),
    CONSTRAINT fk_match_topic_releases_match
        FOREIGN KEY (match_id) REFERENCES {TABLE_MATCHES}(match_id)
        ON DELETE CASCADE
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_match_topic_releases_active_match
    ON {TABLE_MATCH_TOPIC_RELEASES}(match_id) WHERE revoked_at IS NULL;
REVOKE ALL PRIVILEGES ON TABLE {TABLE_MATCH_TOPIC_RELEASES} FROM PUBLIC;
REVOKE ALL PRIVILEGES ON SEQUENCE {TABLE_MATCH_TOPIC_RELEASES}_id_seq FROM PUBLIC;
DO $$
DECLARE role_name TEXT;
BEGIN
    FOR role_name IN
        SELECT rolname FROM pg_roles
        WHERE rolname IN ('anon', 'authenticated')
    LOOP
        EXECUTE format(
            'REVOKE ALL PRIVILEGES ON TABLE {TABLE_MATCH_TOPIC_RELEASES} FROM %I',
            role_name
        );
        EXECUTE format(
            'REVOKE ALL PRIVILEGES ON SEQUENCE {TABLE_MATCH_TOPIC_RELEASES}_id_seq FROM %I',
            role_name
        );
    END LOOP;
END $$;
"""

# Table: TOPIC_VOTES
# Pending/resolved votes on newly proposed topics.
# Per-voter ballots live in TOPIC_VOTE_BALLOTS.
CREATE_TOPIC_VOTES = f"""
CREATE TABLE IF NOT EXISTS {TABLE_TOPIC_VOTES} (
    topic_text          VARCHAR(255) PRIMARY KEY,
    proposer_user_id    VARCHAR(50),
    status              VARCHAR(20) DEFAULT 'pending',
    created_at          TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    deadline_date       DATE,
    approval_threshold  INTEGER NOT NULL,
    category            VARCHAR(50),
    difficulty          INTEGER,
    CONSTRAINT topic_votes_status_check
        CHECK (status IN ('pending', 'passed', 'rejected')),
    CONSTRAINT topic_votes_threshold_check
        CHECK (approval_threshold > 0),
    CONSTRAINT topic_votes_pending_deadline_check
        CHECK (status <> 'pending' OR deadline_date IS NOT NULL),
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
    vote_choice         TEXT    NOT NULL
                                CHECK (vote_choice IN ('agree', 'against')),
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
    topic_text          VARCHAR(255) PRIMARY KEY,
    proposer_user_id    VARCHAR(255),
    status              CHAR(20) DEFAULT 'pending',
    removal_reasons     JSONB NOT NULL DEFAULT '[]',
    created_at          TIMESTAMP NOT NULL,
    deadline_date       DATE,
    approval_threshold  INTEGER NOT NULL,
    CONSTRAINT topic_removal_votes_status_check
        CHECK (BTRIM(status) IN ('pending', 'passed', 'rejected')),
    CONSTRAINT topic_removal_votes_threshold_check
        CHECK (approval_threshold > 0),
    CONSTRAINT topic_removal_votes_pending_deadline_check
        CHECK (BTRIM(status) <> 'pending' OR deadline_date IS NOT NULL),
    CONSTRAINT topic_removal_votes_reasons_array_check
        CHECK (jsonb_typeof(removal_reasons) = 'array'),
    -- Deliberately no FK to topics: a passed removal deletes the bank row but
    -- the resolved motion and its ballots remain as governance history.
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
    vote_choice     TEXT    NOT NULL
                            CHECK (vote_choice IN ('agree', 'against')),
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
CREATE SEQUENCE IF NOT EXISTS login_record_id_seq AS INTEGER;
CREATE TABLE IF NOT EXISTS {TABLE_LOGIN_RECORDS} (
    id              INTEGER     PRIMARY KEY
                                DEFAULT nextval('login_record_id_seq'),
    user_id         TEXT,
    login_type      TEXT,
    logged_in_at    TIMESTAMP,
    CONSTRAINT fk_login_records_user
        FOREIGN KEY (user_id) REFERENCES {TABLE_ACCOUNTS}(user_id)
        ON DELETE SET NULL
);
ALTER SEQUENCE login_record_id_seq
    OWNED BY {TABLE_LOGIN_RECORDS}.id;
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
);
"""

# Table: PUSH_SUBSCRIPTIONS
# Browser Web Push subscriptions for committee vote notifications.
# endpoint is the browser push-service URL and is globally unique per subscription.
CREATE_PUSH_SUBSCRIPTIONS = f"""
CREATE TABLE IF NOT EXISTS {TABLE_PUSH_SUBSCRIPTIONS} (
    endpoint            TEXT        PRIMARY KEY,
    user_id             TEXT,
    subscription_json   TEXT        NOT NULL,
    is_active           BOOLEAN     DEFAULT TRUE,
    created_at          TIMESTAMP,
    updated_at          TIMESTAMP,
    last_error          TEXT,
    CONSTRAINT fk_push_subscriptions_user
        FOREIGN KEY (user_id) REFERENCES {TABLE_ACCOUNTS}(user_id)
        ON DELETE CASCADE
);
"""

# Table: COMPETITION_REGISTRATION_SETTINGS
# Stores the current public registration window and competition edition.
CREATE_COMPETITION_REGISTRATION_SETTINGS = f"""
CREATE TABLE IF NOT EXISTS {TABLE_COMPETITION_REGISTRATION_SETTINGS} (
    id                      INTEGER     PRIMARY KEY CHECK (id = 1),
    competition_edition     INTEGER     NOT NULL,
    registration_start      TIMESTAMP   NOT NULL,
    registration_end        TIMESTAMP   NOT NULL,
    updated_at              TIMESTAMP
);
"""

# Table: COMPETITION_REGISTRATIONS
# Public signup records for the next competition year.
# status: 'submitted' | 'contacted' | 'confirmed' | 'withdrawn'
CREATE_COMPETITION_REGISTRATIONS = f"""
CREATE TABLE IF NOT EXISTS {TABLE_COMPETITION_REGISTRATIONS} (
    id                          SERIAL      PRIMARY KEY,
    competition_edition         INTEGER     NOT NULL,
    team_name                   TEXT        NOT NULL,
    main_debater_name           TEXT        NOT NULL,
    first_deputy_name           TEXT        NOT NULL,
    second_deputy_name          TEXT        NOT NULL,
    closing_debater_name        TEXT        NOT NULL,
    contact_name                TEXT        NOT NULL,
    contact_class               TEXT        NOT NULL,
    contact_phone               TEXT        NOT NULL,
    status                      TEXT        DEFAULT 'submitted'
                                            CHECK (status IN ('submitted', 'contacted', 'confirmed', 'withdrawn')),
    submitted_at                TIMESTAMP   DEFAULT NOW(),
    updated_at                  TIMESTAMP,
    UNIQUE (competition_edition, team_name)
);
"""

# Table: MATCH_VIDEOS
# Public YouTube replay links for matches and legacy standalone videos.
CREATE_MATCH_VIDEOS = f"""
CREATE TABLE IF NOT EXISTS {TABLE_MATCH_VIDEOS} (
    id              SERIAL      PRIMARY KEY,
    match_id        TEXT,
    match_label     TEXT,
    video_title     TEXT        NOT NULL,
    youtube_url     TEXT        NOT NULL,
    standalone_topic_text  TEXT,
    standalone_pro_team    TEXT,
    standalone_con_team    TEXT,
    is_visible      BOOLEAN     DEFAULT TRUE,
    display_order   INTEGER     DEFAULT 0,
    created_at      TIMESTAMP,
    updated_at      TIMESTAMP,
    CONSTRAINT fk_match_videos_match
        FOREIGN KEY (match_id) REFERENCES {TABLE_MATCHES}(match_id)
        ON DELETE CASCADE
);
"""

# Table: VIDEO_VIEWS
# One row per recorded view event. User-level history allows resume and top placement.
CREATE_VIDEO_VIEWS = f"""
CREATE TABLE IF NOT EXISTS {TABLE_VIDEO_VIEWS} (
    id              SERIAL      PRIMARY KEY,
    video_id        INTEGER     NOT NULL,
    user_id         TEXT        NOT NULL,
    viewed_at       TIMESTAMP   DEFAULT NOW(),
    CONSTRAINT fk_video_views_video
        FOREIGN KEY (video_id) REFERENCES {TABLE_MATCH_VIDEOS}(id)
        ON DELETE CASCADE,
    CONSTRAINT fk_video_views_user
        FOREIGN KEY (user_id) REFERENCES {TABLE_ACCOUNTS}(user_id)
        ON DELETE CASCADE
);
"""

# Table: VIDEO_COMMENTS
# Committee discussion under replay videos.
CREATE_VIDEO_COMMENTS = f"""
CREATE TABLE IF NOT EXISTS {TABLE_VIDEO_COMMENTS} (
    id              SERIAL      PRIMARY KEY,
    video_id        INTEGER     NOT NULL,
    user_id         TEXT        NOT NULL,
    comment_text    TEXT        NOT NULL,
    sticker_id      TEXT,
    created_at      TIMESTAMP   DEFAULT NOW(),
    CONSTRAINT chk_video_comments_sticker_id
        CHECK (sticker_id IS NULL OR char_length(sticker_id) BETWEEN 1 AND 200),
    CONSTRAINT fk_video_comments_video
        FOREIGN KEY (video_id) REFERENCES {TABLE_MATCH_VIDEOS}(id)
        ON DELETE CASCADE,
    CONSTRAINT fk_video_comments_user
        FOREIGN KEY (user_id) REFERENCES {TABLE_ACCOUNTS}(user_id)
        ON DELETE CASCADE
);
"""

# Table: VIDEO_VOTES
# One vote per user per video: pro / con / undecided.
CREATE_VIDEO_VOTES = f"""
CREATE TABLE IF NOT EXISTS {TABLE_VIDEO_VOTES} (
    video_id        INTEGER     NOT NULL,
    user_id         TEXT        NOT NULL,
    vote_choice     TEXT        CHECK (vote_choice IN ('pro', 'con', 'undecided')),
    updated_at      TIMESTAMP   DEFAULT NOW(),
    PRIMARY KEY (video_id, user_id),
    CONSTRAINT fk_video_votes_video
        FOREIGN KEY (video_id) REFERENCES {TABLE_MATCH_VIDEOS}(id)
        ON DELETE CASCADE,
    CONSTRAINT fk_video_votes_user
        FOREIGN KEY (user_id) REFERENCES {TABLE_ACCOUNTS}(user_id)
        ON DELETE CASCADE
);
"""

# Table: VIDEO_CHAPTERS
# Per-video jump points for debate roles / sections.
CREATE_VIDEO_CHAPTERS = f"""
CREATE TABLE IF NOT EXISTS {TABLE_VIDEO_CHAPTERS} (
    video_id        INTEGER     NOT NULL,
    chapter_label   TEXT        NOT NULL,
    start_seconds   INTEGER     NOT NULL DEFAULT 0,
    display_order   INTEGER     DEFAULT 0,
    is_best_debater BOOLEAN     NOT NULL DEFAULT FALSE,
    updated_at      TIMESTAMP   DEFAULT NOW(),
    PRIMARY KEY (video_id, chapter_label),
    CONSTRAINT video_chapters_best_role_check
        CHECK (
            is_best_debater = FALSE
            OR chapter_label IN (
                '正主', '反主', '正一', '反一', '正二',
                '反二', '正三', '反三', '反結', '正結'
            )
        ),
    CONSTRAINT fk_video_chapters_video
        FOREIGN KEY (video_id) REFERENCES {TABLE_MATCH_VIDEOS}(id)
        ON DELETE CASCADE
);
"""

# Table: VIDEO_ROSTER
# Per-video links from individual speech roles to committee member accounts.
CREATE_VIDEO_ROSTER = f"""
CREATE TABLE IF NOT EXISTS {TABLE_VIDEO_ROSTER} (
    video_id        INTEGER     NOT NULL,
    role_label      TEXT        NOT NULL
                                CHECK (role_label IN (
                                    '正主', '反主', '正一', '反一', '正二',
                                    '反二', '正三', '反三', '反結', '正結'
                                )),
    member_user_id  TEXT        NOT NULL,
    updated_at      TIMESTAMP   DEFAULT NOW(),
    PRIMARY KEY (video_id, role_label),
    CONSTRAINT fk_video_roster_video
        FOREIGN KEY (video_id) REFERENCES {TABLE_MATCH_VIDEOS}(id)
        ON DELETE CASCADE,
    CONSTRAINT fk_video_roster_member
        FOREIGN KEY (member_user_id) REFERENCES {TABLE_ACCOUNTS}(user_id)
        ON DELETE CASCADE
);
"""

# Keep the member-linked roster backend-only on a fresh ``init_db`` target,
# including Supabase databases whose default grants expose new public tables.
LOCK_VIDEO_ROSTER_PRIVILEGES = f"""
REVOKE ALL PRIVILEGES ON TABLE {TABLE_VIDEO_ROSTER} FROM PUBLIC;
DO $$
DECLARE role_name TEXT;
BEGIN
    FOR role_name IN
        SELECT rolname FROM pg_roles
        WHERE rolname IN ('anon', 'authenticated')
    LOOP
        EXECUTE format(
            'REVOKE ALL PRIVILEGES ON TABLE {TABLE_VIDEO_ROSTER} FROM %I',
            role_name
        );
    END LOOP;
END $$;
"""

# Table: VIDEO_PROGRESS
# Latest watch position per user per video.
CREATE_VIDEO_PROGRESS = f"""
CREATE TABLE IF NOT EXISTS {TABLE_VIDEO_PROGRESS} (
    video_id         INTEGER     NOT NULL,
    user_id          TEXT        NOT NULL,
    watched_seconds  INTEGER     DEFAULT 0,
    duration_seconds INTEGER     DEFAULT 0,
    updated_at       TIMESTAMP   DEFAULT NOW(),
    PRIMARY KEY (video_id, user_id),
    CONSTRAINT fk_video_progress_video
        FOREIGN KEY (video_id) REFERENCES {TABLE_MATCH_VIDEOS}(id)
        ON DELETE CASCADE,
    CONSTRAINT fk_video_progress_user
        FOREIGN KEY (user_id) REFERENCES {TABLE_ACCOUNTS}(user_id)
        ON DELETE CASCADE
);
"""

# Table: MATCH_PHOTOS
# Committee-uploaded highlight photos grouped by replay match/album.
CREATE_MATCH_PHOTOS = f"""
CREATE TABLE IF NOT EXISTS {TABLE_MATCH_PHOTOS} (
    id              SERIAL      PRIMARY KEY,
    match_video_id  INTEGER,
    album_label     TEXT        NOT NULL,
    photo_date      DATE,
    photo_title     TEXT,
    caption         TEXT,
    file_name       TEXT,
    mime_type       TEXT,
    r2_key          TEXT,
    thumbnail_r2_key TEXT,
    byte_size       INTEGER,
    sha256          TEXT,
    width           INTEGER,
    height          INTEGER,
    uploaded_by     TEXT,
    created_at      TIMESTAMP   DEFAULT NOW(),
    CONSTRAINT fk_match_photos_video
        FOREIGN KEY (match_video_id) REFERENCES {TABLE_MATCH_VIDEOS}(id)
        ON DELETE SET NULL,
    CONSTRAINT fk_match_photos_user
        FOREIGN KEY (uploaded_by) REFERENCES {TABLE_ACCOUNTS}(user_id)
        ON DELETE SET NULL
);
"""

# Substring search for Cantonese forum content.  The extension is shared
# database infrastructure, so rollback migrations remove only our indexes.
CREATE_PG_TRGM_EXTENSION = "CREATE EXTENSION IF NOT EXISTS pg_trgm;"


# Committee-only match announcements, history and graduate discussion forum.
CREATE_RECENT_MATCHES = f"""
CREATE TABLE IF NOT EXISTS {TABLE_RECENT_MATCHES} (
    id               BIGSERIAL PRIMARY KEY,
    competition_name TEXT NOT NULL,
    opponent          TEXT NOT NULL,
    match_date        DATE NOT NULL,
    match_time        TIME NOT NULL,
    topic_text        TEXT NOT NULL,
    our_side          TEXT NOT NULL CHECK (our_side IN ('pro','con','unconfirmed')),
    result            TEXT NOT NULL DEFAULT 'unconfirmed'
        CHECK (result IN ('win','loss','draw','unconfirmed')),
    score_text        TEXT NOT NULL DEFAULT '',
    best_debater      TEXT NOT NULL DEFAULT '',
    notes             TEXT NOT NULL DEFAULT '',
    revision          INTEGER NOT NULL DEFAULT 1 CHECK (revision >= 1),
    created_by        TEXT NOT NULL,
    updated_by        TEXT NOT NULL,
    created_at        TIMESTAMP NOT NULL DEFAULT NOW(),
    updated_at        TIMESTAMP NOT NULL DEFAULT NOW()
);
"""

CREATE_RECENT_MATCH_NOTIFICATIONS = f"""
CREATE TABLE IF NOT EXISTS {TABLE_RECENT_MATCH_NOTIFICATIONS} (
    id              BIGSERIAL PRIMARY KEY,
    recent_match_id BIGINT NOT NULL,
    event_kind      TEXT NOT NULL CHECK (event_kind IN ('new_match','result')),
    state           TEXT NOT NULL DEFAULT 'pending'
        CHECK (state IN ('pending','sending','retryable','sent')),
    claim_token     TEXT,
    attempted_at    TIMESTAMP,
    sent_at         TIMESTAMP,
    sent_count      INTEGER NOT NULL DEFAULT 0 CHECK (sent_count >= 0),
    last_error      TEXT NOT NULL DEFAULT '',
    UNIQUE (recent_match_id, event_kind),
    CONSTRAINT fk_recent_match_notification_match
        FOREIGN KEY (recent_match_id) REFERENCES {TABLE_RECENT_MATCHES}(id)
        ON DELETE CASCADE
);
"""

# Collaborative, short-retention workspace for the four Competition Prep
# workflows. Text and structured data stay in PostgreSQL; binary media, if a
# future workflow adds any, must use the existing private-R2 lifecycle.
CREATE_COMPETITION_PREP = f"""
CREATE TABLE IF NOT EXISTS {TABLE_COMPETITION_PREP_PROJECTS} (
    id BIGSERIAL PRIMARY KEY,
    title TEXT NOT NULL CHECK (char_length(title) BETWEEN 1 AND 200),
    recent_match_id BIGINT REFERENCES {TABLE_RECENT_MATCHES}(id) ON DELETE SET NULL,
    topic_text TEXT NOT NULL CHECK (char_length(topic_text) BETWEEN 1 AND 500),
    our_side TEXT NOT NULL CHECK (our_side IN ('pro', 'con')),
    debate_format TEXT NOT NULL CHECK (debate_format IN ('校園隨想', '聯中', '星島', '基本法盃')),
    opponent TEXT NOT NULL DEFAULT '' CHECK (char_length(opponent) <= 200),
    match_date DATE NOT NULL,
    match_time TIME,
    revision INTEGER NOT NULL DEFAULT 1 CHECK (revision >= 1),
    created_by TEXT NOT NULL REFERENCES {TABLE_ACCOUNTS}(user_id) ON DELETE RESTRICT,
    updated_by TEXT NOT NULL REFERENCES {TABLE_ACCOUNTS}(user_id) ON DELETE RESTRICT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at TIMESTAMPTZ NOT NULL
);
CREATE TABLE IF NOT EXISTS {TABLE_COMPETITION_PREP_MEMBERS} (
    project_id BIGINT NOT NULL REFERENCES {TABLE_COMPETITION_PREP_PROJECTS}(id) ON DELETE CASCADE,
    user_id TEXT NOT NULL REFERENCES {TABLE_ACCOUNTS}(user_id) ON DELETE CASCADE,
    role TEXT NOT NULL CHECK (role IN ('owner', 'editor', 'viewer')),
    added_by TEXT NOT NULL REFERENCES {TABLE_ACCOUNTS}(user_id) ON DELETE RESTRICT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (project_id, user_id)
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_competition_prep_project_owner
    ON {TABLE_COMPETITION_PREP_MEMBERS}(project_id) WHERE role = 'owner';
CREATE TABLE IF NOT EXISTS {TABLE_COMPETITION_PREP_MANUSCRIPTS} (
    id BIGSERIAL PRIMARY KEY,
    project_id BIGINT NOT NULL REFERENCES {TABLE_COMPETITION_PREP_PROJECTS}(id) ON DELETE CASCADE,
    slot TEXT NOT NULL CHECK (slot IN ('main', 'dep1', 'dep2', 'dep3', 'closing', 'interaction', 'other')),
    title TEXT NOT NULL CHECK (char_length(title) BETWEEN 1 AND 200),
    body TEXT NOT NULL DEFAULT '',
    assigned_user_id TEXT REFERENCES {TABLE_ACCOUNTS}(user_id) ON DELETE SET NULL,
    status TEXT NOT NULL DEFAULT 'draft' CHECK (status IN ('draft', 'reviewed', 'final')),
    revision INTEGER NOT NULL DEFAULT 1 CHECK (revision >= 1),
    created_by TEXT NOT NULL REFERENCES {TABLE_ACCOUNTS}(user_id) ON DELETE RESTRICT,
    updated_by TEXT NOT NULL REFERENCES {TABLE_ACCOUNTS}(user_id) ON DELETE RESTRICT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (project_id, slot)
);
CREATE TABLE IF NOT EXISTS {TABLE_COMPETITION_PREP_STRATEGY_CARDS} (
    id BIGSERIAL PRIMARY KEY,
    project_id BIGINT NOT NULL REFERENCES {TABLE_COMPETITION_PREP_PROJECTS}(id) ON DELETE CASCADE,
    parent_card_id BIGINT REFERENCES {TABLE_COMPETITION_PREP_STRATEGY_CARDS}(id) ON DELETE SET NULL,
    kind TEXT NOT NULL CHECK (kind IN ('mainline', 'definition', 'standard', 'burden', 'argument', 'opponent_argument', 'attack', 'opponent_answer', 'rebuttal', 'defence_floor', 'concession', 'question')),
    title TEXT NOT NULL CHECK (char_length(title) BETWEEN 1 AND 200),
    content TEXT NOT NULL DEFAULT '',
    assigned_slot TEXT CHECK (assigned_slot IS NULL OR assigned_slot IN ('main', 'dep1', 'dep2', 'dep3', 'closing', 'interaction')),
    priority INTEGER NOT NULL DEFAULT 2 CHECK (priority BETWEEN 1 AND 3),
    status TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'handled', 'risk', 'not_applicable')),
    sort_order INTEGER NOT NULL DEFAULT 0,
    revision INTEGER NOT NULL DEFAULT 1 CHECK (revision >= 1),
    created_by TEXT NOT NULL REFERENCES {TABLE_ACCOUNTS}(user_id) ON DELETE RESTRICT,
    updated_by TEXT NOT NULL REFERENCES {TABLE_ACCOUNTS}(user_id) ON DELETE RESTRICT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE TABLE IF NOT EXISTS {TABLE_COMPETITION_PREP_EVIDENCE_CARDS} (
    id BIGSERIAL PRIMARY KEY,
    project_id BIGINT NOT NULL REFERENCES {TABLE_COMPETITION_PREP_PROJECTS}(id) ON DELETE CASCADE,
    claim_text TEXT NOT NULL CHECK (char_length(claim_text) BETWEEN 1 AND 500),
    excerpt TEXT NOT NULL DEFAULT '',
    source_url TEXT NOT NULL DEFAULT '' CHECK (char_length(source_url) <= 2000),
    source_name TEXT NOT NULL DEFAULT '' CHECK (char_length(source_name) <= 200),
    published_date DATE,
    accessed_date DATE NOT NULL DEFAULT CURRENT_DATE,
    region TEXT NOT NULL DEFAULT '' CHECK (char_length(region) <= 100),
    source_type TEXT NOT NULL DEFAULT 'other' CHECK (source_type IN ('government', 'academic', 'news', 'ngo', 'industry', 'ai_research', 'other')),
    side_scope TEXT NOT NULL DEFAULT 'both' CHECK (side_scope IN ('our', 'opponent', 'both')),
    limitations TEXT NOT NULL DEFAULT '',
    linked_strategy_card_id BIGINT REFERENCES {TABLE_COMPETITION_PREP_STRATEGY_CARDS}(id) ON DELETE SET NULL,
    revision INTEGER NOT NULL DEFAULT 1 CHECK (revision >= 1),
    created_by TEXT NOT NULL REFERENCES {TABLE_ACCOUNTS}(user_id) ON DELETE RESTRICT,
    updated_by TEXT NOT NULL REFERENCES {TABLE_ACCOUNTS}(user_id) ON DELETE RESTRICT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE TABLE IF NOT EXISTS {TABLE_COMPETITION_PREP_WEAKNESSES} (
    id BIGSERIAL PRIMARY KEY,
    project_id BIGINT NOT NULL REFERENCES {TABLE_COMPETITION_PREP_PROJECTS}(id) ON DELETE CASCADE,
    source_type TEXT NOT NULL DEFAULT 'manual' CHECK (source_type IN ('manual', 'audit', 'speech', 'strategy')),
    title TEXT NOT NULL CHECK (char_length(title) BETWEEN 1 AND 200),
    description TEXT NOT NULL DEFAULT '',
    category TEXT NOT NULL DEFAULT 'logic' CHECK (category IN ('logic', 'evidence', 'definition', 'response', 'delivery', 'coordination')),
    assigned_user_id TEXT REFERENCES {TABLE_ACCOUNTS}(user_id) ON DELETE SET NULL,
    priority INTEGER NOT NULL DEFAULT 2 CHECK (priority BETWEEN 1 AND 3),
    status TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'practicing', 'passed')),
    revision INTEGER NOT NULL DEFAULT 1 CHECK (revision >= 1),
    created_by TEXT NOT NULL REFERENCES {TABLE_ACCOUNTS}(user_id) ON DELETE RESTRICT,
    updated_by TEXT NOT NULL REFERENCES {TABLE_ACCOUNTS}(user_id) ON DELETE RESTRICT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE TABLE IF NOT EXISTS {TABLE_COMPETITION_PREP_AI_RUNS} (
    run_id TEXT PRIMARY KEY CHECK (char_length(run_id) BETWEEN 16 AND 200),
    project_id BIGINT NOT NULL REFERENCES {TABLE_COMPETITION_PREP_PROJECTS}(id) ON DELETE CASCADE,
    run_type TEXT NOT NULL CHECK (run_type IN ('team_audit', 'strategy_seed', 'strategy_attack', 'speech_review', 'speech_retake', 'weakness_feedback')),
    source_revision INTEGER NOT NULL CHECK (source_revision >= 1),
    model_label TEXT NOT NULL CHECK (char_length(model_label) BETWEEN 1 AND 120),
    snapshot_json JSONB NOT NULL DEFAULT '{{}}'::jsonb CHECK (jsonb_typeof(snapshot_json) = 'object'),
    output_markdown TEXT NOT NULL,
    created_by TEXT NOT NULL REFERENCES {TABLE_ACCOUNTS}(user_id) ON DELETE RESTRICT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_competition_prep_projects_expiry ON {TABLE_COMPETITION_PREP_PROJECTS}(expires_at, id);
CREATE INDEX IF NOT EXISTS idx_competition_prep_members_user ON {TABLE_COMPETITION_PREP_MEMBERS}(user_id, project_id);
CREATE INDEX IF NOT EXISTS idx_competition_prep_strategy_project ON {TABLE_COMPETITION_PREP_STRATEGY_CARDS}(project_id, sort_order, id);
CREATE INDEX IF NOT EXISTS idx_competition_prep_evidence_project ON {TABLE_COMPETITION_PREP_EVIDENCE_CARDS}(project_id, id);
CREATE INDEX IF NOT EXISTS idx_competition_prep_weakness_project ON {TABLE_COMPETITION_PREP_WEAKNESSES}(project_id, status, priority, id);
CREATE INDEX IF NOT EXISTS idx_competition_prep_ai_runs_project ON {TABLE_COMPETITION_PREP_AI_RUNS}(project_id, created_at DESC);
REVOKE ALL PRIVILEGES ON TABLE {TABLE_COMPETITION_PREP_PROJECTS}, {TABLE_COMPETITION_PREP_MEMBERS}, {TABLE_COMPETITION_PREP_MANUSCRIPTS}, {TABLE_COMPETITION_PREP_STRATEGY_CARDS}, {TABLE_COMPETITION_PREP_EVIDENCE_CARDS}, {TABLE_COMPETITION_PREP_WEAKNESSES}, {TABLE_COMPETITION_PREP_AI_RUNS} FROM PUBLIC;
REVOKE ALL PRIVILEGES ON SEQUENCE {TABLE_COMPETITION_PREP_PROJECTS}_id_seq, {TABLE_COMPETITION_PREP_MANUSCRIPTS}_id_seq, {TABLE_COMPETITION_PREP_STRATEGY_CARDS}_id_seq, {TABLE_COMPETITION_PREP_EVIDENCE_CARDS}_id_seq, {TABLE_COMPETITION_PREP_WEAKNESSES}_id_seq FROM PUBLIC;
DO $$
DECLARE role_name TEXT;
BEGIN
    FOR role_name IN SELECT rolname FROM pg_roles WHERE rolname IN ('anon', 'authenticated')
    LOOP
        EXECUTE 'REVOKE ALL PRIVILEGES ON TABLE {TABLE_COMPETITION_PREP_PROJECTS}, {TABLE_COMPETITION_PREP_MEMBERS}, {TABLE_COMPETITION_PREP_MANUSCRIPTS}, {TABLE_COMPETITION_PREP_STRATEGY_CARDS}, {TABLE_COMPETITION_PREP_EVIDENCE_CARDS}, {TABLE_COMPETITION_PREP_WEAKNESSES}, {TABLE_COMPETITION_PREP_AI_RUNS} FROM ' || quote_ident(role_name);
        EXECUTE 'REVOKE ALL PRIVILEGES ON SEQUENCE {TABLE_COMPETITION_PREP_PROJECTS}_id_seq, {TABLE_COMPETITION_PREP_MANUSCRIPTS}_id_seq, {TABLE_COMPETITION_PREP_STRATEGY_CARDS}_id_seq, {TABLE_COMPETITION_PREP_EVIDENCE_CARDS}_id_seq, {TABLE_COMPETITION_PREP_WEAKNESSES}_id_seq FROM ' || quote_ident(role_name);
    END LOOP;
END $$;
"""

CREATE_COMMITTEE_MEMBERSHIPS = f"""
CREATE TABLE IF NOT EXISTS {TABLE_COMMITTEE_MEMBERSHIPS} (
    id                     BIGSERIAL PRIMARY KEY,
    member_user_id         TEXT,
    display_name           TEXT NOT NULL,
    joined_academic_year   INTEGER NOT NULL
        CHECK (joined_academic_year BETWEEN 1900 AND 2200),
    ended_academic_year    INTEGER
        CHECK (ended_academic_year IS NULL OR ended_academic_year BETWEEN 1900 AND 2200),
    exit_type              TEXT NOT NULL DEFAULT 'current'
        CHECK (exit_type IN ('current','left','graduated')),
    revision               INTEGER NOT NULL DEFAULT 1 CHECK (revision >= 1),
    created_by             TEXT NOT NULL,
    updated_by             TEXT NOT NULL,
    created_at             TIMESTAMP NOT NULL DEFAULT NOW(),
    updated_at             TIMESTAMP NOT NULL DEFAULT NOW(),
    CONSTRAINT committee_membership_exit_consistency CHECK (
        (exit_type='current' AND ended_academic_year IS NULL)
        OR
        (exit_type IN ('left','graduated') AND ended_academic_year IS NOT NULL
         AND ended_academic_year >= joined_academic_year)
    ),
    CONSTRAINT fk_committee_membership_account
        FOREIGN KEY (member_user_id) REFERENCES {TABLE_ACCOUNTS}(user_id)
        ON DELETE SET NULL
);
"""

CREATE_HISTORY_EVENTS = f"""
CREATE TABLE IF NOT EXISTS {TABLE_HISTORY_EVENTS} (
    id                    BIGSERIAL PRIMARY KEY,
    academic_year_start   INTEGER NOT NULL
        CHECK (academic_year_start BETWEEN 1900 AND 2200),
    event_date            DATE,
    title                 TEXT NOT NULL,
    description           TEXT NOT NULL DEFAULT '',
    revision              INTEGER NOT NULL DEFAULT 1 CHECK (revision >= 1),
    created_by            TEXT NOT NULL,
    updated_by            TEXT NOT NULL,
    created_at            TIMESTAMP NOT NULL DEFAULT NOW(),
    updated_at            TIMESTAMP NOT NULL DEFAULT NOW(),
    CONSTRAINT history_event_date_in_academic_year CHECK (
        event_date IS NULL OR event_date BETWEEN
            make_date(academic_year_start, 9, 1)
            AND make_date(academic_year_start + 1, 8, 31)
    )
);
"""

CREATE_HISTORY_EVENT_MATCHES = f"""
CREATE TABLE IF NOT EXISTS {TABLE_HISTORY_EVENT_MATCHES} (
    event_id BIGINT NOT NULL,
    match_id TEXT NOT NULL,
    PRIMARY KEY (event_id, match_id),
    CONSTRAINT fk_history_event_match_event
        FOREIGN KEY (event_id) REFERENCES {TABLE_HISTORY_EVENTS}(id) ON DELETE CASCADE,
    CONSTRAINT fk_history_event_match_match
        FOREIGN KEY (match_id) REFERENCES {TABLE_MATCHES}(match_id) ON DELETE CASCADE
);
"""

CREATE_HISTORY_EVENT_PHOTOS = f"""
CREATE TABLE IF NOT EXISTS {TABLE_HISTORY_EVENT_PHOTOS} (
    event_id BIGINT NOT NULL,
    photo_id INTEGER NOT NULL,
    PRIMARY KEY (event_id, photo_id),
    CONSTRAINT fk_history_event_photo_event
        FOREIGN KEY (event_id) REFERENCES {TABLE_HISTORY_EVENTS}(id) ON DELETE CASCADE,
    CONSTRAINT fk_history_event_photo_photo
        FOREIGN KEY (photo_id) REFERENCES {TABLE_MATCH_PHOTOS}(id) ON DELETE CASCADE
);
"""

CREATE_GHOST_FORUM_THREADS = f"""
CREATE TABLE IF NOT EXISTS {TABLE_GHOST_FORUM_THREADS} (
    id               BIGSERIAL PRIMARY KEY,
    title            TEXT NOT NULL,
    author_user_id   TEXT NOT NULL,
    revision         INTEGER NOT NULL DEFAULT 1 CHECK (revision >= 1),
    created_at       TIMESTAMP NOT NULL DEFAULT NOW(),
    updated_at       TIMESTAMP NOT NULL DEFAULT NOW(),
    last_activity_at TIMESTAMP NOT NULL DEFAULT NOW(),
    deleted_at       TIMESTAMP,
    CONSTRAINT fk_ghost_forum_thread_author
        FOREIGN KEY (author_user_id) REFERENCES {TABLE_ACCOUNTS}(user_id)
        ON DELETE RESTRICT
);
"""

CREATE_GHOST_FORUM_POSTS = f"""
CREATE TABLE IF NOT EXISTS {TABLE_GHOST_FORUM_POSTS} (
    id               BIGSERIAL PRIMARY KEY,
    thread_id        BIGINT NOT NULL,
    author_user_id   TEXT NOT NULL,
    body             TEXT NOT NULL,
    sticker_id       TEXT,
    quoted_post_id   BIGINT,
    is_first_post    BOOLEAN NOT NULL DEFAULT FALSE,
    revision         INTEGER NOT NULL DEFAULT 1 CHECK (revision >= 1),
    created_at       TIMESTAMP NOT NULL DEFAULT NOW(),
    updated_at       TIMESTAMP NOT NULL DEFAULT NOW(),
    deleted_at       TIMESTAMP,
    CONSTRAINT fk_ghost_forum_post_thread
        FOREIGN KEY (thread_id) REFERENCES {TABLE_GHOST_FORUM_THREADS}(id)
        ON DELETE CASCADE,
    CONSTRAINT fk_ghost_forum_post_author
        FOREIGN KEY (author_user_id) REFERENCES {TABLE_ACCOUNTS}(user_id)
        ON DELETE RESTRICT,
    CONSTRAINT fk_ghost_forum_post_quote
        FOREIGN KEY (quoted_post_id) REFERENCES {TABLE_GHOST_FORUM_POSTS}(id)
        ON DELETE SET NULL,
    CONSTRAINT chk_ghost_forum_posts_sticker_id
        CHECK (
            sticker_id IS NULL
            OR (char_length(sticker_id) BETWEEN 1 AND 200 AND body = '')
        )
);
"""

CREATE_GHOST_FORUM_REACTIONS = f"""
CREATE TABLE IF NOT EXISTS {TABLE_GHOST_FORUM_REACTIONS} (
    post_id    BIGINT NOT NULL,
    user_id    TEXT NOT NULL,
    created_at TIMESTAMP NOT NULL DEFAULT NOW(),
    PRIMARY KEY (post_id, user_id),
    CONSTRAINT fk_ghost_forum_reaction_post
        FOREIGN KEY (post_id) REFERENCES {TABLE_GHOST_FORUM_POSTS}(id) ON DELETE CASCADE,
    CONSTRAINT fk_ghost_forum_reaction_user
        FOREIGN KEY (user_id) REFERENCES {TABLE_ACCOUNTS}(user_id) ON DELETE CASCADE
);
"""

CREATE_GHOST_FORUM_THREAD_VIDEOS = f"""
CREATE TABLE IF NOT EXISTS {TABLE_GHOST_FORUM_THREAD_VIDEOS} (
    thread_id BIGINT NOT NULL,
    video_id  INTEGER NOT NULL,
    PRIMARY KEY (thread_id, video_id),
    CONSTRAINT fk_ghost_thread_video_thread
        FOREIGN KEY (thread_id) REFERENCES {TABLE_GHOST_FORUM_THREADS}(id) ON DELETE CASCADE,
    CONSTRAINT fk_ghost_thread_video_video
        FOREIGN KEY (video_id) REFERENCES {TABLE_MATCH_VIDEOS}(id) ON DELETE CASCADE
);
"""

CREATE_GHOST_FORUM_THREAD_PHOTOS = f"""
CREATE TABLE IF NOT EXISTS {TABLE_GHOST_FORUM_THREAD_PHOTOS} (
    thread_id BIGINT NOT NULL,
    photo_id  INTEGER NOT NULL,
    PRIMARY KEY (thread_id, photo_id),
    CONSTRAINT fk_ghost_thread_photo_thread
        FOREIGN KEY (thread_id) REFERENCES {TABLE_GHOST_FORUM_THREADS}(id) ON DELETE CASCADE,
    CONSTRAINT fk_ghost_thread_photo_photo
        FOREIGN KEY (photo_id) REFERENCES {TABLE_MATCH_PHOTOS}(id) ON DELETE CASCADE
);
"""

CREATE_GHOST_FORUM_THREAD_HISTORY_EVENTS = f"""
CREATE TABLE IF NOT EXISTS {TABLE_GHOST_FORUM_THREAD_HISTORY_EVENTS} (
    thread_id BIGINT NOT NULL,
    event_id  BIGINT NOT NULL,
    PRIMARY KEY (thread_id, event_id),
    CONSTRAINT fk_ghost_thread_history_event_thread
        FOREIGN KEY (thread_id) REFERENCES {TABLE_GHOST_FORUM_THREADS}(id) ON DELETE CASCADE,
    CONSTRAINT fk_ghost_thread_history_event_event
        FOREIGN KEY (event_id) REFERENCES {TABLE_HISTORY_EVENTS}(id) ON DELETE CASCADE
);
"""

CREATE_GHOST_FORUM_USER_PROFILES = f"""
CREATE TABLE IF NOT EXISTS {TABLE_GHOST_FORUM_USER_PROFILES} (
    user_id      TEXT PRIMARY KEY,
    unread_since TIMESTAMP NOT NULL,
    created_at   TIMESTAMP NOT NULL DEFAULT NOW(),
    updated_at   TIMESTAMP NOT NULL DEFAULT NOW(),
    CONSTRAINT fk_ghost_forum_profile_user
        FOREIGN KEY (user_id) REFERENCES {TABLE_ACCOUNTS}(user_id) ON DELETE CASCADE
);
"""

CREATE_GHOST_FORUM_THREAD_USER_STATE = f"""
CREATE TABLE IF NOT EXISTS {TABLE_GHOST_FORUM_THREAD_USER_STATE} (
    thread_id         BIGINT NOT NULL,
    user_id           TEXT NOT NULL,
    last_read_post_id BIGINT,
    muted             BOOLEAN NOT NULL DEFAULT FALSE,
    updated_at        TIMESTAMP NOT NULL DEFAULT NOW(),
    PRIMARY KEY (thread_id, user_id),
    CONSTRAINT fk_ghost_forum_state_thread
        FOREIGN KEY (thread_id) REFERENCES {TABLE_GHOST_FORUM_THREADS}(id) ON DELETE CASCADE,
    CONSTRAINT fk_ghost_forum_state_user
        FOREIGN KEY (user_id) REFERENCES {TABLE_ACCOUNTS}(user_id) ON DELETE CASCADE,
    CONSTRAINT fk_ghost_forum_state_post
        FOREIGN KEY (last_read_post_id) REFERENCES {TABLE_GHOST_FORUM_POSTS}(id) ON DELETE SET NULL
);
"""

CREATE_GHOST_FORUM_NOTIFICATIONS = f"""
CREATE TABLE IF NOT EXISTS {TABLE_GHOST_FORUM_NOTIFICATIONS} (
    id           BIGSERIAL PRIMARY KEY,
    post_id      BIGINT NOT NULL UNIQUE,
    event_kind   TEXT NOT NULL CHECK (event_kind IN ('thread','reply')),
    state        TEXT NOT NULL DEFAULT 'pending'
        CHECK (state IN ('pending','sending','retryable','sent')),
    claim_token  TEXT,
    attempted_at TIMESTAMP,
    sent_at      TIMESTAMP,
    sent_count   INTEGER NOT NULL DEFAULT 0 CHECK (sent_count >= 0),
    last_error   TEXT NOT NULL DEFAULT '',
    created_at   TIMESTAMP NOT NULL DEFAULT NOW(),
    CONSTRAINT fk_ghost_forum_notification_post
        FOREIGN KEY (post_id) REFERENCES {TABLE_GHOST_FORUM_POSTS}(id) ON DELETE CASCADE
);
"""

LOCK_COMMUNITY_PRIVILEGES = f"""
REVOKE ALL PRIVILEGES ON TABLE
    {TABLE_RECENT_MATCHES}, {TABLE_RECENT_MATCH_NOTIFICATIONS},
    {TABLE_COMMITTEE_MEMBERSHIPS}, {TABLE_HISTORY_EVENTS},
    {TABLE_HISTORY_EVENT_MATCHES}, {TABLE_HISTORY_EVENT_PHOTOS},
    {TABLE_GHOST_FORUM_THREADS}, {TABLE_GHOST_FORUM_POSTS},
    {TABLE_GHOST_FORUM_REACTIONS}, {TABLE_GHOST_FORUM_THREAD_VIDEOS},
    {TABLE_GHOST_FORUM_THREAD_PHOTOS}, {TABLE_GHOST_FORUM_THREAD_HISTORY_EVENTS},
    {TABLE_GHOST_FORUM_USER_PROFILES},
    {TABLE_GHOST_FORUM_THREAD_USER_STATE}, {TABLE_GHOST_FORUM_NOTIFICATIONS}
FROM PUBLIC;
REVOKE ALL PRIVILEGES ON SEQUENCE
    {TABLE_RECENT_MATCHES}_id_seq, {TABLE_RECENT_MATCH_NOTIFICATIONS}_id_seq,
    {TABLE_COMMITTEE_MEMBERSHIPS}_id_seq, {TABLE_HISTORY_EVENTS}_id_seq,
    {TABLE_GHOST_FORUM_THREADS}_id_seq, {TABLE_GHOST_FORUM_POSTS}_id_seq,
    {TABLE_GHOST_FORUM_NOTIFICATIONS}_id_seq
FROM PUBLIC;
DO $$
DECLARE role_name TEXT;
BEGIN
    FOR role_name IN
        SELECT rolname FROM pg_roles WHERE rolname IN ('anon', 'authenticated')
    LOOP
        EXECUTE format(
            'REVOKE ALL PRIVILEGES ON TABLE '
            '{TABLE_RECENT_MATCHES}, {TABLE_RECENT_MATCH_NOTIFICATIONS}, '
            '{TABLE_COMMITTEE_MEMBERSHIPS}, {TABLE_HISTORY_EVENTS}, '
            '{TABLE_HISTORY_EVENT_MATCHES}, {TABLE_HISTORY_EVENT_PHOTOS}, '
            '{TABLE_GHOST_FORUM_THREADS}, {TABLE_GHOST_FORUM_POSTS}, '
            '{TABLE_GHOST_FORUM_REACTIONS}, {TABLE_GHOST_FORUM_THREAD_VIDEOS}, '
            '{TABLE_GHOST_FORUM_THREAD_PHOTOS}, {TABLE_GHOST_FORUM_THREAD_HISTORY_EVENTS}, '
            '{TABLE_GHOST_FORUM_USER_PROFILES}, '
            '{TABLE_GHOST_FORUM_THREAD_USER_STATE}, {TABLE_GHOST_FORUM_NOTIFICATIONS} '
            'FROM %I', role_name
        );
        EXECUTE format(
            'REVOKE ALL PRIVILEGES ON SEQUENCE '
            '{TABLE_RECENT_MATCHES}_id_seq, {TABLE_RECENT_MATCH_NOTIFICATIONS}_id_seq, '
            '{TABLE_COMMITTEE_MEMBERSHIPS}_id_seq, {TABLE_HISTORY_EVENTS}_id_seq, '
            '{TABLE_GHOST_FORUM_THREADS}_id_seq, {TABLE_GHOST_FORUM_POSTS}_id_seq, '
            '{TABLE_GHOST_FORUM_NOTIFICATIONS}_id_seq '
            'FROM %I', role_name
        );
    END LOOP;
END $$;
"""

# Tables: TTS voice recording dataset
CREATE_TTS_VOICE_CONSENTS = f"""
CREATE TABLE IF NOT EXISTS {TABLE_TTS_VOICE_CONSENTS} (
    user_id          TEXT,
    consent_version  TEXT,
    consent_text     TEXT        NOT NULL,
    consented_at     TIMESTAMP   DEFAULT NOW(),
    withdrawn_at     TIMESTAMP,
    voice_cloning_confirmed BOOLEAN NOT NULL DEFAULT FALSE,
    cloud_processing_confirmed BOOLEAN NOT NULL DEFAULT FALSE,
    is_minor         BOOLEAN NOT NULL DEFAULT FALSE,
    guardian_confirmed BOOLEAN NOT NULL DEFAULT FALSE,
    PRIMARY KEY (user_id, consent_version),
    CONSTRAINT fk_tts_voice_consents_user
        FOREIGN KEY (user_id) REFERENCES {TABLE_ACCOUNTS}(user_id)
        ON DELETE CASCADE
);
"""

CREATE_TTS_VOICE_RECORDINGS = f"""
CREATE TABLE IF NOT EXISTS {TABLE_TTS_VOICE_RECORDINGS} (
    id                SERIAL      PRIMARY KEY,
    speaker_user_id   TEXT,
    script_id         TEXT        NOT NULL,
    prompt_text       TEXT        NOT NULL,
    r2_key            TEXT,
    mime_type         TEXT,
    file_ext          TEXT,
    size_bytes        INTEGER,
    duration_seconds  INTEGER,
    audio_sha256      TEXT,
    measured_duration_seconds NUMERIC,
    sample_rate_hz    INTEGER,
    channel_count     INTEGER,
    detected_format   TEXT,
    ai_review_status  TEXT        CHECK (ai_review_status IN ('passed', 'failed', 'error')),
    ai_review_json    TEXT,
    ai_transcript     TEXT,
    status            TEXT        DEFAULT 'pending'
        CHECK (status IN ('pending', 'accepted', 'rejected', 'withdrawn')),
    review_note       TEXT,
    reviewed_by       TEXT,
    reviewed_at       TIMESTAMP,
    created_at        TIMESTAMP   DEFAULT NOW(),
    CONSTRAINT fk_tts_voice_recordings_speaker
        FOREIGN KEY (speaker_user_id) REFERENCES {TABLE_ACCOUNTS}(user_id)
        ON DELETE SET NULL,
    CONSTRAINT fk_tts_voice_recordings_reviewer
        FOREIGN KEY (reviewed_by) REFERENCES {TABLE_ACCOUNTS}(user_id)
        ON DELETE SET NULL
);
"""

CREATE_BANDWIDTH_USAGE_LOGS = f"""
CREATE TABLE IF NOT EXISTS {TABLE_BANDWIDTH_USAGE_LOGS} (
    id          BIGSERIAL PRIMARY KEY,
    source      TEXT NOT NULL,
    user_id     TEXT,
    bytes_out   BIGINT NOT NULL CHECK (bytes_out >= 0),
    details     TEXT,
    official_bucket_id TEXT,
    traffic_category TEXT,
    bucket_start TIMESTAMP,
    bucket_end TIMESTAMP,
    official_complete BOOLEAN NOT NULL DEFAULT FALSE,
    created_at  TIMESTAMP DEFAULT NOW(),
    CONSTRAINT fk_bandwidth_usage_user
        FOREIGN KEY (user_id) REFERENCES {TABLE_ACCOUNTS}(user_id)
        ON DELETE SET NULL
);
"""

CREATE_R2_UPLOAD_INTENTS = f"""
CREATE TABLE IF NOT EXISTS {TABLE_R2_UPLOAD_INTENTS} (
    intent_id       TEXT PRIMARY KEY,
    user_id         TEXT NOT NULL,
    media_kind      TEXT NOT NULL,
    object_keys     JSONB NOT NULL,
    intent_metadata JSONB NOT NULL DEFAULT '{{}}'::jsonb,
    declared_bytes  BIGINT NOT NULL CHECK (declared_bytes > 0),
    status          TEXT NOT NULL DEFAULT 'issued',
    created_at      TIMESTAMP NOT NULL DEFAULT NOW(),
    completed_at    TIMESTAMP,
    CONSTRAINT r2_upload_intents_object_keys_check
        CHECK (
            jsonb_typeof(object_keys)='array'
            AND jsonb_array_length(object_keys)>0
        ),
    CONSTRAINT r2_upload_intents_status_check
        CHECK (
            status IN (
                'issued', 'completed', 'processing', 'consumed',
                'orphan_deleted'
            )
        ),
    CONSTRAINT r2_upload_intents_completion_check
        CHECK (
            (status IN ('issued', 'processing') AND completed_at IS NULL)
            OR
            (status IN ('completed', 'consumed', 'orphan_deleted')
                AND completed_at IS NOT NULL)
        ),
    CONSTRAINT fk_r2_upload_intent_user
        FOREIGN KEY (user_id) REFERENCES {TABLE_ACCOUNTS}(user_id)
        ON DELETE CASCADE
);
"""

# Monthly system-wide infrastructure and provider budgets.  One row is the
# complete audit record for one key and budget month; browsers never access it
# directly.
CREATE_MONTHLY_RESOURCE_LIMITS = f"""
CREATE TABLE IF NOT EXISTS {TABLE_MONTHLY_RESOURCE_LIMITS} (
    period_month             DATE NOT NULL,
    limit_key                TEXT NOT NULL,
    unit                     TEXT NOT NULL,
    warning_value            NUMERIC(20,4),
    stop_value               NUMERIC(20,4),
    hard_value               NUMERIC(20,4),
    allocated_hkd            NUMERIC(12,2),
    fx_hkd_per_usd           NUMERIC(12,6),
    funding_window_start     TIMESTAMPTZ,
    funding_window_end       TIMESTAMPTZ,
    external_cap_confirmed   BOOLEAN NOT NULL DEFAULT FALSE,
    external_cap_confirmed_by TEXT,
    external_cap_confirmed_at TIMESTAMPTZ,
    notified_by              TEXT,
    notified_at              TIMESTAMPTZ,
    notification_audit       JSONB NOT NULL DEFAULT '{{}}'::jsonb,
    updated_by               TEXT,
    updated_at               TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (period_month, limit_key),
    CONSTRAINT monthly_resource_limits_month_start
        CHECK (period_month = date_trunc('month', period_month)::date),
    CONSTRAINT monthly_resource_limits_key
        CHECK (limit_key IN ('render_bandwidth','r2_storage','ai_fund_available')
               OR (left(limit_key, 9) = 'provider:' AND length(limit_key) > 9)),
    CONSTRAINT monthly_resource_limits_nonnegative CHECK (
        COALESCE(warning_value, 0) >= 0 AND COALESCE(stop_value, 0) >= 0
        AND COALESCE(hard_value, 0) >= 0 AND COALESCE(allocated_hkd, 0) >= 0
        AND COALESCE(fx_hkd_per_usd, 0) >= 0
    ),
    CONSTRAINT monthly_resource_limits_render_order CHECK (
        limit_key <> 'render_bandwidth'
        OR (warning_value IS NOT NULL AND stop_value IS NOT NULL
            AND hard_value IS NOT NULL
            AND warning_value <= stop_value AND stop_value <= hard_value)
    ),
    CONSTRAINT fk_monthly_resource_limits_external_confirmer
        FOREIGN KEY (external_cap_confirmed_by) REFERENCES {TABLE_ACCOUNTS}(user_id)
        ON DELETE SET NULL,
    CONSTRAINT fk_monthly_resource_limits_notifier
        FOREIGN KEY (notified_by) REFERENCES {TABLE_ACCOUNTS}(user_id)
        ON DELETE SET NULL,
    CONSTRAINT fk_monthly_resource_limits_updater
        FOREIGN KEY (updated_by) REFERENCES {TABLE_ACCOUNTS}(user_id)
        ON DELETE SET NULL
);
"""

LOCK_MONTHLY_RESOURCE_LIMITS_PRIVILEGES = f"""
REVOKE ALL PRIVILEGES ON TABLE {TABLE_MONTHLY_RESOURCE_LIMITS} FROM PUBLIC;
DO $$
DECLARE role_name TEXT;
BEGIN
    FOR role_name IN SELECT rolname FROM pg_roles
        WHERE rolname IN ('anon', 'authenticated')
    LOOP
        EXECUTE 'REVOKE ALL PRIVILEGES ON TABLE {TABLE_MONTHLY_RESOURCE_LIMITS} FROM '
            || quote_ident(role_name);
    END LOOP;
END $$;
"""

# Short-lived runtime state. These definitions intentionally match the
# production-compatible tables that used to be created inside request paths.
# Type/FK changes wait for the versioned P1 baseline instead of being guessed.
CREATE_PROJECTOR_STATE = f"""
CREATE TABLE IF NOT EXISTS {TABLE_PROJECTOR_STATE} (
    display_key   TEXT PRIMARY KEY,
    match_id      TEXT,
    debate_format TEXT,
    seg_index     INTEGER DEFAULT 0,
    visible       BOOLEAN DEFAULT TRUE,
    updated_at    TIMESTAMP
);
"""

CREATE_PROJECTOR_KIOSK_DEVICES = f"""
CREATE TABLE IF NOT EXISTS {TABLE_PROJECTOR_KIOSK_DEVICES} (
    device_id             TEXT PRIMARY KEY,
    label                 TEXT NOT NULL,
    enabled               BOOLEAN NOT NULL DEFAULT TRUE,
    credential_generation BIGINT NOT NULL DEFAULT 1 CHECK (credential_generation >= 1),
    created_at            TIMESTAMP NOT NULL DEFAULT NOW(),
    last_seen_at          TIMESTAMP,
    revoked_at            TIMESTAMP,
    CHECK (CHAR_LENGTH(device_id) BETWEEN 20 AND 80),
    CHECK (CHAR_LENGTH(label) BETWEEN 1 AND 120)
);
"""

CREATE_PROJECTOR_AI_SESSIONS = f"""
CREATE TABLE IF NOT EXISTS {TABLE_PROJECTOR_AI_SESSIONS} (
    session_id             TEXT PRIMARY KEY,
    display_key            TEXT NOT NULL,
    match_id               TEXT NOT NULL,
    status                 TEXT NOT NULL DEFAULT 'start_requested'
        CHECK (status IN ('start_requested','recording','stop_requested','processing',
                          'ready','published','error','cancelled','interrupted','cleared','expired')),
    status_detail          TEXT NOT NULL DEFAULT '',
    recording_started_at   TIMESTAMP,
    recording_duration_seconds DOUBLE PRECISION,
    recording_bytes        BIGINT CHECK (recording_bytes IS NULL OR recording_bytes >= 0),
    result_ciphertext      BYTEA,
    tts_audio_ciphertext   BYTEA,
    tts_mime               TEXT,
    tts_claim_token        TEXT,
    tts_status             TEXT NOT NULL DEFAULT 'not_requested'
        CHECK (tts_status IN ('not_requested','generating','unavailable','ready','playing',
                              'played','stopped','failed')),
    published              BOOLEAN NOT NULL DEFAULT FALSE,
    publish_revision       BIGINT NOT NULL DEFAULT 0 CHECK (publish_revision >= 0),
    result_expires_at      TIMESTAMP,
    kiosk_device_id       TEXT,
    kiosk_lease_generation BIGINT CHECK (kiosk_lease_generation IS NULL OR kiosk_lease_generation >= 1),
    created_at             TIMESTAMP NOT NULL DEFAULT NOW(),
    updated_at             TIMESTAMP NOT NULL DEFAULT NOW(),
    CONSTRAINT fk_projector_ai_session_match
        FOREIGN KEY (match_id) REFERENCES {TABLE_MATCHES}(match_id)
        ON DELETE RESTRICT,
    CONSTRAINT fk_projector_ai_session_kiosk_device
        FOREIGN KEY (kiosk_device_id) REFERENCES {TABLE_PROJECTOR_KIOSK_DEVICES}(device_id)
        ON DELETE SET NULL
);
"""

CREATE_PROJECTOR_AI_CONTROLS = f"""
CREATE TABLE IF NOT EXISTS {TABLE_PROJECTOR_AI_CONTROLS} (
    display_key        TEXT PRIMARY KEY,
    current_session_id TEXT,
    command            TEXT NOT NULL DEFAULT '',
    command_revision   BIGINT NOT NULL DEFAULT 0 CHECK (command_revision >= 0),
    ack_revision       BIGINT NOT NULL DEFAULT 0 CHECK (ack_revision >= 0),
    kiosk_status       TEXT NOT NULL DEFAULT 'offline',
    status_detail      TEXT NOT NULL DEFAULT '',
    command_payload    JSONB NOT NULL DEFAULT '{{}}'::jsonb,
    hardware_status    JSONB NOT NULL DEFAULT '{{}}'::jsonb,
    capabilities       JSONB NOT NULL DEFAULT '{{}}'::jsonb,
    kiosk_last_seen_at TIMESTAMP,
    lease_device_id    TEXT,
    lease_client_id    TEXT,
    lease_token_hash   TEXT,
    lease_generation   BIGINT NOT NULL DEFAULT 0 CHECK (lease_generation >= 0),
    lease_expires_at   TIMESTAMP,
    lease_last_seen_at TIMESTAMP,
    command_lease_generation BIGINT NOT NULL DEFAULT 0 CHECK (command_lease_generation >= 0),
    created_at         TIMESTAMP NOT NULL DEFAULT NOW(),
    updated_at         TIMESTAMP NOT NULL DEFAULT NOW(),
    CONSTRAINT fk_projector_ai_control_session
        FOREIGN KEY (current_session_id) REFERENCES {TABLE_PROJECTOR_AI_SESSIONS}(session_id)
        ON DELETE SET NULL,
    CONSTRAINT fk_projector_ai_control_lease_device
        FOREIGN KEY (lease_device_id) REFERENCES {TABLE_PROJECTOR_KIOSK_DEVICES}(device_id)
        ON DELETE SET NULL
);
"""

CREATE_PROJECTOR_AI_MARKERS = f"""
CREATE TABLE IF NOT EXISTS {TABLE_PROJECTOR_AI_MARKERS} (
    id             BIGSERIAL PRIMARY KEY,
    session_id     TEXT NOT NULL,
    offset_seconds DOUBLE PRECISION NOT NULL CHECK (offset_seconds >= 0),
    side           TEXT NOT NULL CHECK (side IN ('pro','con','both','unknown')),
    segment        TEXT NOT NULL,
    seg_index      INTEGER NOT NULL CHECK (seg_index >= 0),
    created_at     TIMESTAMP NOT NULL DEFAULT NOW(),
    CONSTRAINT fk_projector_ai_marker_session
        FOREIGN KEY (session_id) REFERENCES {TABLE_PROJECTOR_AI_SESSIONS}(session_id)
        ON DELETE CASCADE
);
"""

CREATE_OFFICIAL_AI_JUDGE = f"""
CREATE TABLE IF NOT EXISTS {TABLE_OFFICIAL_AI_JUDGE_RUNS} (
    match_id             TEXT PRIMARY KEY,
    projector_session_id TEXT NOT NULL,
    operation_id         TEXT NOT NULL UNIQUE,
    status               TEXT NOT NULL
        CHECK (status IN ('ready','processing','retryable','succeeded','fallback')),
    attempt_count        SMALLINT NOT NULL DEFAULT 0
        CHECK (attempt_count BETWEEN 0 AND 2),
    current_model_label  TEXT,
    final_model_label    TEXT,
    final_judge_name     TEXT,
    last_error           TEXT NOT NULL DEFAULT '',
    current_claim_token  TEXT,
    claim_expires_at     TIMESTAMPTZ,
    created_by           TEXT NOT NULL,
    created_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    completed_at         TIMESTAMPTZ,
    CONSTRAINT fk_official_ai_judge_run_match
        FOREIGN KEY (match_id) REFERENCES {TABLE_MATCHES}(match_id)
        ON DELETE RESTRICT,
    CONSTRAINT fk_official_ai_judge_run_session
        FOREIGN KEY (projector_session_id) REFERENCES {TABLE_PROJECTOR_AI_SESSIONS}(session_id)
        ON DELETE RESTRICT
);

CREATE TABLE IF NOT EXISTS {TABLE_OFFICIAL_AI_JUDGE_ATTEMPTS} (
    id                 BIGSERIAL PRIMARY KEY,
    match_id           TEXT NOT NULL,
    attempt_no         SMALLINT NOT NULL CHECK (attempt_no BETWEEN 1 AND 2),
    model_label        TEXT NOT NULL,
    provider           TEXT NOT NULL,
    human_judge_count  SMALLINT NOT NULL CHECK (human_judge_count >= 2 AND MOD(human_judge_count, 2) = 0),
    pro_deduction      INTEGER NOT NULL CHECK (pro_deduction >= 0),
    con_deduction      INTEGER NOT NULL CHECK (con_deduction >= 0),
    status             TEXT NOT NULL DEFAULT 'claimed'
        CHECK (status IN ('claimed','running','succeeded','failed')),
    provider_attempted BOOLEAN NOT NULL DEFAULT FALSE,
    error_message      TEXT NOT NULL DEFAULT '',
    result_payload     JSONB,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    provider_attempted_at TIMESTAMPTZ,
    completed_at       TIMESTAMPTZ,
    CONSTRAINT fk_official_ai_judge_attempt_run
        FOREIGN KEY (match_id) REFERENCES {TABLE_OFFICIAL_AI_JUDGE_RUNS}(match_id)
        ON DELETE CASCADE,
    UNIQUE (match_id, attempt_no),
    UNIQUE (match_id, model_label)
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_scores_one_official_ai_judge
    ON {TABLE_SCORES}(match_id) WHERE judge_kind='ai';
CREATE INDEX IF NOT EXISTS idx_official_ai_judge_attempts_match_created
    ON {TABLE_OFFICIAL_AI_JUDGE_ATTEMPTS}(match_id, created_at DESC);
"""

LOCK_OFFICIAL_AI_JUDGE_PRIVILEGES = f"""
REVOKE ALL PRIVILEGES ON TABLE
    {TABLE_OFFICIAL_AI_JUDGE_RUNS}, {TABLE_OFFICIAL_AI_JUDGE_ATTEMPTS}
    FROM PUBLIC;
REVOKE ALL PRIVILEGES ON SEQUENCE {TABLE_OFFICIAL_AI_JUDGE_ATTEMPTS}_id_seq
    FROM PUBLIC;
DO $$
DECLARE role_name TEXT;
BEGIN
    FOR role_name IN
        SELECT rolname FROM pg_roles
        WHERE rolname IN ('anon', 'authenticated')
    LOOP
        EXECUTE 'REVOKE ALL PRIVILEGES ON TABLE {TABLE_OFFICIAL_AI_JUDGE_RUNS}, {TABLE_OFFICIAL_AI_JUDGE_ATTEMPTS} FROM '
            || quote_ident(role_name);
        EXECUTE 'REVOKE ALL PRIVILEGES ON SEQUENCE {TABLE_OFFICIAL_AI_JUDGE_ATTEMPTS}_id_seq FROM '
            || quote_ident(role_name);
    END LOOP;
END $$;
"""

# Results, transcripts, speaker markers and TTS audio are backend-only even
# though the payload columns are authenticated-encrypted at rest.
LOCK_PROJECTOR_AI_PRIVILEGES = f"""
REVOKE ALL PRIVILEGES ON TABLE
    {TABLE_PROJECTOR_KIOSK_DEVICES}, {TABLE_PROJECTOR_AI_SESSIONS}, {TABLE_PROJECTOR_AI_CONTROLS},
    {TABLE_PROJECTOR_AI_MARKERS}
    FROM PUBLIC;
REVOKE ALL PRIVILEGES ON SEQUENCE {TABLE_PROJECTOR_AI_MARKERS}_id_seq
    FROM PUBLIC;
DO $$
DECLARE role_name TEXT;
BEGIN
    FOR role_name IN
        SELECT rolname FROM pg_roles
        WHERE rolname IN ('anon', 'authenticated')
    LOOP
        EXECUTE
            'REVOKE ALL PRIVILEGES ON TABLE {TABLE_PROJECTOR_KIOSK_DEVICES}, {TABLE_PROJECTOR_AI_SESSIONS}, {TABLE_PROJECTOR_AI_CONTROLS}, {TABLE_PROJECTOR_AI_MARKERS} FROM '
            || quote_ident(role_name);
        EXECUTE
            'REVOKE ALL PRIVILEGES ON SEQUENCE {TABLE_PROJECTOR_AI_MARKERS}_id_seq FROM '
            || quote_ident(role_name);
    END LOOP;
END $$;
"""

CREATE_AI_COACH_LIVE_BRIEFS = f"""
CREATE TABLE IF NOT EXISTS {TABLE_AI_COACH_LIVE_BRIEFS} (
    brief_id   TEXT PRIMARY KEY,
    user_id    TEXT NOT NULL,
    brief      TEXT NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ NOT NULL,
    CONSTRAINT ai_coach_live_briefs_expiry_check
        CHECK (expires_at > created_at)
);
"""

# Table: TTS_SCRIPTS
# Recording script bank, editable by TTS recording admins. Seeded from the
# built-in default bank in ai_training.py when empty.
CREATE_TTS_SCRIPTS = f"""
CREATE TABLE IF NOT EXISTS {TABLE_TTS_SCRIPTS} (
    script_id        TEXT        PRIMARY KEY,
    category         TEXT        NOT NULL,
    text             TEXT        NOT NULL,
    is_active        BOOLEAN     DEFAULT TRUE,
    sort_order       INTEGER     DEFAULT 0,
    script_type      TEXT        DEFAULT 'short',
    manuscript_id    TEXT,
    manuscript_title TEXT,
    created_by       TEXT,
    created_at       TIMESTAMP   DEFAULT NOW(),
    updated_at       TIMESTAMP   DEFAULT NOW()
);
"""

# Pronunciation override dictionary (讀音層 / RD plan 二). Runtime reads active
# rows in deploy/proxy.py `_preprocess_tts_text` and rewrites `term` → `reading`
# before synthesis (single-player + live-room share the same path). Distinct from
# tts_scripts (which is the recording sentence bank), this holds reading rules.
CREATE_TTS_LEXICON = f"""
CREATE TABLE IF NOT EXISTS {TABLE_TTS_LEXICON} (
    lexicon_id  TEXT        PRIMARY KEY,
    term        TEXT        NOT NULL,
    reading     TEXT        NOT NULL,
    jyutping    TEXT,
    example     TEXT,
    note        TEXT,
    category    TEXT        DEFAULT '',
    is_active   BOOLEAN     DEFAULT TRUE,
    created_by  TEXT,
    created_at  TIMESTAMP   DEFAULT NOW(),
    updated_at  TIMESTAMP   DEFAULT NOW()
);
"""

# Table: LLM_TRAINING_SUBMISSIONS
# Text examples submitted by committee members for debate LLM / RAG training.
CREATE_LLM_TRAINING_SUBMISSIONS = f"""
CREATE TABLE IF NOT EXISTS {TABLE_LLM_TRAINING_SUBMISSIONS} (
    id                    SERIAL      PRIMARY KEY,
    submitted_by          TEXT,
    data_type             TEXT        NOT NULL,
    title                 TEXT,
    topic_text            TEXT,
    side                  TEXT,
    content_text          TEXT        NOT NULL,
    source_note           TEXT,
    anonymized            BOOLEAN     DEFAULT FALSE,
    permission_confirmed  BOOLEAN     DEFAULT FALSE,
    ai_review_status      TEXT        CHECK (ai_review_status IN ('passed', 'failed', 'error')),
    ai_review_json        TEXT,
    status                TEXT        DEFAULT 'pending'
        CHECK (status IN ('pending', 'accepted', 'rejected', 'withdrawn')),
    review_note           TEXT,
    reviewed_by           TEXT,
    reviewed_at           TIMESTAMP,
    created_at            TIMESTAMP   DEFAULT NOW(),
    CONSTRAINT fk_llm_training_submissions_submitter
        FOREIGN KEY (submitted_by) REFERENCES {TABLE_ACCOUNTS}(user_id)
        ON DELETE SET NULL,
    CONSTRAINT fk_llm_training_submissions_reviewer
        FOREIGN KEY (reviewed_by) REFERENCES {TABLE_ACCOUNTS}(user_id)
        ON DELETE SET NULL
);
"""

# Dataset/model, eval and RAG schemas are intentionally absent from the
# bootstrap until their security/readiness gates and versioned migrations are complete.
CREATE_AI_TRAINING_AUDIT = f"""
CREATE TABLE IF NOT EXISTS {TABLE_AI_TRAINING_AUDIT} (
    id             BIGSERIAL PRIMARY KEY,
    actor_user_id  TEXT,
    action         TEXT NOT NULL,
    target_type    TEXT NOT NULL,
    target_id      TEXT,
    details_json   JSONB NOT NULL DEFAULT '{{}}'::jsonb,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT chk_ai_training_audit_actor_length
        CHECK (actor_user_id IS NULL OR char_length(actor_user_id) <= 200),
    CONSTRAINT chk_ai_training_audit_action_length
        CHECK (char_length(action) BETWEEN 1 AND 100),
    CONSTRAINT chk_ai_training_audit_target_type_length
        CHECK (char_length(target_type) BETWEEN 1 AND 100),
    CONSTRAINT chk_ai_training_audit_target_id_length
        CHECK (target_id IS NULL OR char_length(target_id) <= 300),
    CONSTRAINT chk_ai_training_audit_details_object
        CHECK (jsonb_typeof(details_json) = 'object')
);
"""

# ``init_db`` remains available for isolated/bootstrap databases. Mirror the
# migration's privacy boundary so Supabase default grants cannot expose audit
# rows when this path creates the table before the migration runner records it.
LOCK_AI_TRAINING_AUDIT_PRIVILEGES = f"""
REVOKE ALL PRIVILEGES ON TABLE {TABLE_AI_TRAINING_AUDIT} FROM PUBLIC;
REVOKE ALL PRIVILEGES ON SEQUENCE {TABLE_AI_TRAINING_AUDIT}_id_seq FROM PUBLIC;
DO $$
DECLARE role_name TEXT;
BEGIN
    FOR role_name IN
        SELECT rolname FROM pg_roles
        WHERE rolname IN ('anon', 'authenticated')
    LOOP
        EXECUTE format(
            'REVOKE ALL PRIVILEGES ON TABLE {TABLE_AI_TRAINING_AUDIT} FROM %I',
            role_name
        );
        EXECUTE format(
            'REVOKE ALL PRIVILEGES ON SEQUENCE {TABLE_AI_TRAINING_AUDIT}_id_seq FROM %I',
            role_name
        );
    END LOOP;
END $$;
"""

# Versioned, fail-closed V0 debate data factory. These relations retain
# immutable source/provider/release lineage without provisioning pgvector,
# model-registry or evaluation bundles.
CREATE_AI_DATA_FACTORY = f"""
CREATE TABLE IF NOT EXISTS {TABLE_AI_FACTORY_SOURCES} (
    id                    TEXT        PRIMARY KEY,
    source_group_id       TEXT        NOT NULL,
    revision_no           INTEGER     NOT NULL CHECK (revision_no > 0),
    supersedes_source_id  TEXT,
    source_kind           TEXT        NOT NULL
        CHECK (source_kind IN ('llm_submission', 'admin_paste')),
    origin_submission_id  INTEGER,
    data_type             TEXT        NOT NULL,
    title                 TEXT,
    topic_text            TEXT,
    side                  TEXT,
    source_note           TEXT,
    language_code         TEXT        NOT NULL
        CHECK (language_code IN (
            'yue-Hant-HK', 'zh-Hant', 'en', 'mixed', 'other'
        )),
    rights_basis          TEXT        NOT NULL
        CHECK (rights_basis IN (
            'submission_confirmed', 'own_work', 'permission',
            'open_license', 'public_domain', 'other'
        )),
    rights_confirmed_by   TEXT        NOT NULL,
    rights_confirmed_at   TIMESTAMPTZ NOT NULL,
    content_text          TEXT        NOT NULL,
    content_sha256        TEXT        NOT NULL,
    created_by            TEXT        NOT NULL,
    created_at            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    withdrawn_by          TEXT,
    withdrawn_at          TIMESTAMPTZ,
    withdrawal_reason     TEXT,
    CONSTRAINT ai_factory_sources_id_length
        CHECK (char_length(id) BETWEEN 1 AND 64),
    CONSTRAINT ai_factory_sources_group_length
        CHECK (char_length(source_group_id) BETWEEN 1 AND 64),
    CONSTRAINT ai_factory_sources_revision_link
        CHECK (
            (revision_no = 1 AND supersedes_source_id IS NULL)
            OR (revision_no > 1 AND supersedes_source_id IS NOT NULL)
        ),
    CONSTRAINT ai_factory_sources_origin_kind
        CHECK (
            (source_kind = 'llm_submission'
                AND origin_submission_id IS NOT NULL
                AND rights_basis = 'submission_confirmed')
            OR
            (source_kind = 'admin_paste'
                AND origin_submission_id IS NULL
                AND rights_basis <> 'submission_confirmed')
        ),
    CONSTRAINT ai_factory_sources_metadata_lengths
        CHECK (
            char_length(data_type) BETWEEN 1 AND 80
            AND (title IS NULL OR char_length(title) <= 500)
            AND (topic_text IS NULL OR char_length(topic_text) <= 2000)
            AND (side IS NULL OR char_length(side) <= 80)
            AND (source_note IS NULL OR char_length(source_note) <= 1000)
            AND char_length(language_code) BETWEEN 2 AND 35
            AND char_length(rights_confirmed_by) BETWEEN 1 AND 200
            AND char_length(created_by) BETWEEN 1 AND 200
        ),
    CONSTRAINT ai_factory_sources_content_length
        CHECK (char_length(content_text) BETWEEN 1 AND 20000),
    CONSTRAINT ai_factory_sources_content_hash
        CHECK (
            char_length(content_sha256) = 64
            AND content_sha256 = lower(content_sha256)
            AND content_sha256 ~ '^[0-9a-f]+$'
        ),
    CONSTRAINT ai_factory_sources_withdrawal_fields
        CHECK (
            (withdrawn_at IS NULL
                AND withdrawn_by IS NULL
                AND withdrawal_reason IS NULL)
            OR
            (withdrawn_at IS NOT NULL
                AND char_length(withdrawn_by) BETWEEN 1 AND 200
                AND char_length(withdrawal_reason) BETWEEN 1 AND 1000)
        ),
    CONSTRAINT fk_ai_factory_sources_submission
        FOREIGN KEY (origin_submission_id)
        REFERENCES {TABLE_LLM_TRAINING_SUBMISSIONS}(id) ON DELETE RESTRICT,
    CONSTRAINT fk_ai_factory_sources_superseded
        FOREIGN KEY (supersedes_source_id)
        REFERENCES {TABLE_AI_FACTORY_SOURCES}(id) ON DELETE RESTRICT,
    UNIQUE (source_group_id, revision_no)
);

COMMENT ON TABLE {TABLE_AI_FACTORY_SOURCES} IS
    'skhlmc-feature:data_factory:20260720_0009';

CREATE UNIQUE INDEX IF NOT EXISTS idx_ai_factory_sources_submission
    ON {TABLE_AI_FACTORY_SOURCES}(origin_submission_id)
    WHERE origin_submission_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS idx_ai_factory_sources_supersedes
    ON {TABLE_AI_FACTORY_SOURCES}(supersedes_source_id)
    WHERE supersedes_source_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_ai_factory_sources_active_created
    ON {TABLE_AI_FACTORY_SOURCES}(created_at DESC)
    WHERE withdrawn_at IS NULL;

CREATE TABLE IF NOT EXISTS {TABLE_AI_FACTORY_JOBS} (
    id                      TEXT        PRIMARY KEY,
    source_id               TEXT        NOT NULL,
    recipe_key              TEXT        NOT NULL
        CHECK (recipe_key IN (
            'rag_knowledge_card_v1',
            'rag_argument_decomposition_v1',
            'sft_speech_critique_v1',
            'sft_attack_defence_v1'
        )),
    requested_count         SMALLINT    NOT NULL DEFAULT 3
        CHECK (requested_count BETWEEN 1 AND 5),
    instruction_text        TEXT        NOT NULL DEFAULT '',
    status                  TEXT        NOT NULL DEFAULT 'draft'
        CHECK (status IN (
            'draft', 'processing', 'awaiting_review',
            'reviewed', 'failed', 'invalidated'
        )),
    preview_model_label     TEXT,
    preview_provider        TEXT,
    preview_provider_model  TEXT,
    preview_prompt_sha256   TEXT,
    preview_input_sha256    TEXT,
    preview_sha256          TEXT,
    preview_expires_at      TIMESTAMPTZ,
    created_by              TEXT        NOT NULL,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    invalidated_by          TEXT,
    invalidated_at          TIMESTAMPTZ,
    invalidation_reason     TEXT,
    CONSTRAINT ai_factory_jobs_id_length
        CHECK (char_length(id) BETWEEN 1 AND 64),
    CONSTRAINT ai_factory_jobs_instruction_length
        CHECK (char_length(instruction_text) <= 500),
    CONSTRAINT ai_factory_jobs_actor_length
        CHECK (char_length(created_by) BETWEEN 1 AND 200),
    CONSTRAINT ai_factory_jobs_preview_bundle
        CHECK (
            (preview_model_label IS NULL
                AND preview_provider IS NULL
                AND preview_provider_model IS NULL
                AND preview_prompt_sha256 IS NULL
                AND preview_input_sha256 IS NULL
                AND preview_sha256 IS NULL
                AND preview_expires_at IS NULL)
            OR
            (char_length(preview_model_label) BETWEEN 1 AND 200
                AND char_length(preview_provider) BETWEEN 1 AND 80
                AND char_length(preview_provider_model) BETWEEN 1 AND 200
                AND char_length(preview_prompt_sha256) = 64
                AND preview_prompt_sha256 = lower(preview_prompt_sha256)
                AND preview_prompt_sha256 ~ '^[0-9a-f]+$'
                AND char_length(preview_input_sha256) = 64
                AND preview_input_sha256 = lower(preview_input_sha256)
                AND preview_input_sha256 ~ '^[0-9a-f]+$'
                AND char_length(preview_sha256) = 64
                AND preview_sha256 = lower(preview_sha256)
                AND preview_sha256 ~ '^[0-9a-f]+$'
                AND preview_expires_at IS NOT NULL)
        ),
    CONSTRAINT ai_factory_jobs_invalidation_fields
        CHECK (
            (status <> 'invalidated'
                AND invalidated_at IS NULL
                AND invalidated_by IS NULL
                AND invalidation_reason IS NULL)
            OR
            (status = 'invalidated'
                AND invalidated_at IS NOT NULL
                AND char_length(invalidated_by) BETWEEN 1 AND 200
                AND char_length(invalidation_reason) BETWEEN 1 AND 1000)
        ),
    CONSTRAINT fk_ai_factory_jobs_source
        FOREIGN KEY (source_id)
        REFERENCES {TABLE_AI_FACTORY_SOURCES}(id) ON DELETE RESTRICT
);

CREATE INDEX IF NOT EXISTS idx_ai_factory_jobs_source_created
    ON {TABLE_AI_FACTORY_JOBS}(source_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_ai_factory_jobs_status_updated
    ON {TABLE_AI_FACTORY_JOBS}(status, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_ai_factory_jobs_preview_expiry
    ON {TABLE_AI_FACTORY_JOBS}(preview_expires_at)
    WHERE preview_expires_at IS NOT NULL;

CREATE TABLE IF NOT EXISTS {TABLE_AI_FACTORY_ATTEMPTS} (
    id                        TEXT        PRIMARY KEY,
    job_id                    TEXT        NOT NULL,
    attempt_no                SMALLINT    NOT NULL
        CHECK (attempt_no BETWEEN 1 AND 3),
    operation_id              TEXT        NOT NULL,
    model_label               TEXT        NOT NULL,
    provider                  TEXT        NOT NULL,
    provider_model            TEXT        NOT NULL,
    recipe_key                TEXT        NOT NULL
        CHECK (recipe_key IN (
            'rag_knowledge_card_v1',
            'rag_argument_decomposition_v1',
            'sft_speech_critique_v1',
            'sft_attack_defence_v1'
        )),
    recipe_version            TEXT        NOT NULL,
    candidate_count           SMALLINT    NOT NULL
        CHECK (candidate_count BETWEEN 1 AND 5),
    estimated_cost_hkd        NUMERIC(12, 8) NOT NULL DEFAULT 0
        CHECK (estimated_cost_hkd BETWEEN 0 AND 9999),
    budget_provider_name      TEXT,
    budget_period_month       DATE,
    budget_window_start       TIMESTAMP,
    source_sha256             TEXT        NOT NULL,
    prompt_sha256             TEXT        NOT NULL,
    input_sha256              TEXT        NOT NULL,
    preview_sha256            TEXT        NOT NULL,
    previewed_at              TIMESTAMPTZ NOT NULL,
    preview_expires_at        TIMESTAMPTZ NOT NULL,
    confirmation_version      TEXT        NOT NULL,
    anonymization_confirmed   BOOLEAN     NOT NULL,
    rights_confirmed          BOOLEAN     NOT NULL,
    third_party_confirmed     BOOLEAN     NOT NULL,
    pii_warning_count         SMALLINT    NOT NULL DEFAULT 0
        CHECK (pii_warning_count BETWEEN 0 AND 20),
    pii_override_reason       TEXT,
    confirmed_by              TEXT        NOT NULL,
    confirmed_at              TIMESTAMPTZ NOT NULL,
    status                    TEXT        NOT NULL DEFAULT 'claimed'
        CHECK (status IN (
            'claimed', 'running', 'succeeded', 'failed', 'discarded'
        )),
    provider_attempted_at     TIMESTAMPTZ,
    provider_request_id       TEXT,
    resolved_provider_model   TEXT,
    response_sha256           TEXT,
    response_bytes            INTEGER,
    error_code                TEXT,
    completed_at              TIMESTAMPTZ,
    created_at                TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT ai_factory_attempts_id_length
        CHECK (char_length(id) BETWEEN 1 AND 64),
    CONSTRAINT ai_factory_attempts_operation_is_job
        CHECK (operation_id = job_id),
    CONSTRAINT ai_factory_attempts_identity_lengths
        CHECK (
            char_length(model_label) BETWEEN 1 AND 200
            AND char_length(provider) BETWEEN 1 AND 80
            AND char_length(provider_model) BETWEEN 1 AND 200
            AND char_length(recipe_version) BETWEEN 1 AND 80
            AND char_length(confirmation_version) BETWEEN 1 AND 80
            AND char_length(confirmed_by) BETWEEN 1 AND 200
            AND (provider_request_id IS NULL
                OR char_length(provider_request_id) <= 300)
            AND (resolved_provider_model IS NULL
                OR char_length(resolved_provider_model) BETWEEN 1 AND 200)
        ),
    CONSTRAINT ai_factory_attempts_hashes
        CHECK (
            char_length(source_sha256) = 64
            AND source_sha256 = lower(source_sha256)
            AND source_sha256 ~ '^[0-9a-f]+$'
            AND char_length(prompt_sha256) = 64
            AND prompt_sha256 = lower(prompt_sha256)
            AND prompt_sha256 ~ '^[0-9a-f]+$'
            AND char_length(input_sha256) = 64
            AND input_sha256 = lower(input_sha256)
            AND input_sha256 ~ '^[0-9a-f]+$'
            AND char_length(preview_sha256) = 64
            AND preview_sha256 = lower(preview_sha256)
            AND preview_sha256 ~ '^[0-9a-f]+$'
            AND (response_sha256 IS NULL
                OR (
                    char_length(response_sha256) = 64
                    AND response_sha256 = lower(response_sha256)
                    AND response_sha256 ~ '^[0-9a-f]+$'
                ))
        ),
    CONSTRAINT ai_factory_attempts_confirmation
        CHECK (
            anonymization_confirmed = TRUE
            AND rights_confirmed = TRUE
            AND third_party_confirmed = TRUE
            AND confirmed_at >= previewed_at
            AND confirmed_at <= preview_expires_at
            AND (provider_attempted_at IS NULL
                OR provider_attempted_at >= confirmed_at)
        ),
    CONSTRAINT ai_factory_attempts_pii_confirmation
        CHECK (
            (pii_warning_count = 0
                AND pii_override_reason IS NULL)
            OR
            (pii_warning_count > 0
                AND char_length(pii_override_reason) BETWEEN 1 AND 1000)
        ),
    CONSTRAINT ai_factory_attempts_response_size
        CHECK (response_bytes IS NULL OR response_bytes BETWEEN 0 AND 102400),
    CONSTRAINT ai_factory_attempts_status_fields
        CHECK (
            (status = 'claimed'
                AND provider_attempted_at IS NULL
                AND completed_at IS NULL
                AND error_code IS NULL)
            OR
            (status = 'running'
                AND provider_attempted_at IS NOT NULL
                AND completed_at IS NULL
                AND error_code IS NULL)
            OR
            (status = 'succeeded'
                AND provider_attempted_at IS NOT NULL
                AND completed_at IS NOT NULL
                AND response_sha256 IS NOT NULL
                AND response_bytes IS NOT NULL
                AND response_bytes > 0
                AND error_code IS NULL)
            OR
            (status IN ('failed', 'discarded')
                AND completed_at IS NOT NULL
                AND char_length(error_code) BETWEEN 1 AND 120)
        ),
    CONSTRAINT ai_factory_attempts_completion_order
        CHECK (completed_at IS NULL OR completed_at >= provider_attempted_at),
    CONSTRAINT fk_ai_factory_attempts_job
        FOREIGN KEY (job_id)
        REFERENCES {TABLE_AI_FACTORY_JOBS}(id) ON DELETE RESTRICT,
    UNIQUE (id, job_id),
    UNIQUE (job_id, attempt_no)
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_ai_factory_attempts_one_success
    ON {TABLE_AI_FACTORY_ATTEMPTS}(job_id)
    WHERE status = 'succeeded';
CREATE INDEX IF NOT EXISTS idx_ai_factory_attempts_job_created
    ON {TABLE_AI_FACTORY_ATTEMPTS}(job_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_ai_factory_attempts_processing
    ON {TABLE_AI_FACTORY_ATTEMPTS}(provider_attempted_at)
    WHERE status IN ('claimed', 'running');

CREATE TABLE IF NOT EXISTS {TABLE_AI_FACTORY_ITEMS} (
    id                    TEXT        PRIMARY KEY,
    job_id                TEXT        NOT NULL,
    attempt_id            TEXT        NOT NULL,
    ordinal               SMALLINT    NOT NULL CHECK (ordinal BETWEEN 1 AND 5),
    original_json         JSONB       NOT NULL,
    original_sha256       TEXT        NOT NULL,
    reviewed_json         JSONB,
    reviewed_sha256       TEXT,
    review_status         TEXT        NOT NULL DEFAULT 'pending'
        CHECK (review_status IN ('pending', 'approved', 'rejected')),
    review_note           TEXT,
    reviewed_by           TEXT,
    reviewed_at           TIMESTAMPTZ,
    created_at            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    invalidated_by        TEXT,
    invalidated_at        TIMESTAMPTZ,
    invalidation_reason   TEXT,
    CONSTRAINT ai_factory_items_id_length
        CHECK (char_length(id) BETWEEN 1 AND 64),
    CONSTRAINT ai_factory_items_original_object
        CHECK (jsonb_typeof(original_json) = 'object'),
    CONSTRAINT ai_factory_items_reviewed_object
        CHECK (reviewed_json IS NULL OR jsonb_typeof(reviewed_json) = 'object'),
    CONSTRAINT ai_factory_items_hashes
        CHECK (
            char_length(original_sha256) = 64
            AND original_sha256 = lower(original_sha256)
            AND original_sha256 ~ '^[0-9a-f]+$'
            AND (
                (reviewed_json IS NULL AND reviewed_sha256 IS NULL)
                OR
                (reviewed_json IS NOT NULL
                    AND char_length(reviewed_sha256) = 64
                    AND reviewed_sha256 = lower(reviewed_sha256)
                    AND reviewed_sha256 ~ '^[0-9a-f]+$')
            )
        ),
    CONSTRAINT ai_factory_items_review_fields
        CHECK (
            (review_status = 'pending'
                AND reviewed_json IS NULL
                AND reviewed_sha256 IS NULL
                AND reviewed_by IS NULL
                AND reviewed_at IS NULL)
            OR
            (review_status = 'approved'
                AND reviewed_json IS NOT NULL
                AND reviewed_sha256 IS NOT NULL
                AND char_length(reviewed_by) BETWEEN 1 AND 200
                AND reviewed_at IS NOT NULL)
            OR
            (review_status = 'rejected'
                AND char_length(reviewed_by) BETWEEN 1 AND 200
                AND reviewed_at IS NOT NULL)
        ),
    CONSTRAINT ai_factory_items_note_length
        CHECK (review_note IS NULL OR char_length(review_note) <= 2000),
    CONSTRAINT ai_factory_items_invalidation_fields
        CHECK (
            (invalidated_at IS NULL
                AND invalidated_by IS NULL
                AND invalidation_reason IS NULL)
            OR
            (invalidated_at IS NOT NULL
                AND char_length(invalidated_by) BETWEEN 1 AND 200
                AND char_length(invalidation_reason) BETWEEN 1 AND 1000)
        ),
    CONSTRAINT fk_ai_factory_items_attempt_job
        FOREIGN KEY (attempt_id, job_id)
        REFERENCES {TABLE_AI_FACTORY_ATTEMPTS}(id, job_id) ON DELETE RESTRICT,
    UNIQUE (job_id, ordinal)
);

CREATE INDEX IF NOT EXISTS idx_ai_factory_items_attempt
    ON {TABLE_AI_FACTORY_ITEMS}(attempt_id, ordinal);
CREATE INDEX IF NOT EXISTS idx_ai_factory_items_review_queue
    ON {TABLE_AI_FACTORY_ITEMS}(created_at)
    WHERE review_status = 'pending' AND invalidated_at IS NULL;
CREATE UNIQUE INDEX IF NOT EXISTS idx_ai_factory_items_approved_hash
    ON {TABLE_AI_FACTORY_ITEMS}(reviewed_sha256)
    WHERE review_status = 'approved';

CREATE TABLE IF NOT EXISTS {TABLE_AI_FACTORY_TOPIC_TAGS} (
    id                TEXT        PRIMARY KEY,
    label             TEXT        NOT NULL,
    normalized_label  TEXT        NOT NULL UNIQUE,
    approved_by       TEXT        NOT NULL,
    approved_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    retired_by        TEXT,
    retired_at        TIMESTAMPTZ,
    CONSTRAINT ai_factory_topic_tags_id_length
        CHECK (char_length(id) BETWEEN 1 AND 64),
    CONSTRAINT ai_factory_topic_tags_label_length
        CHECK (
            char_length(label) BETWEEN 1 AND 40
            AND char_length(normalized_label) BETWEEN 1 AND 40
            AND char_length(approved_by) BETWEEN 1 AND 200
        ),
    CONSTRAINT ai_factory_topic_tags_retirement_fields
        CHECK (
            (retired_at IS NULL AND retired_by IS NULL)
            OR
            (retired_at IS NOT NULL
                AND char_length(retired_by) BETWEEN 1 AND 200)
        )
);

CREATE INDEX IF NOT EXISTS idx_ai_factory_topic_tags_active
    ON {TABLE_AI_FACTORY_TOPIC_TAGS}(normalized_label)
    WHERE retired_at IS NULL;

CREATE TABLE IF NOT EXISTS {TABLE_AI_FACTORY_ITEM_TAGS} (
    item_id      TEXT        NOT NULL,
    tag_id       TEXT        NOT NULL,
    assigned_by  TEXT        NOT NULL,
    assigned_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT ai_factory_item_tags_actor_length
        CHECK (char_length(assigned_by) BETWEEN 1 AND 200),
    CONSTRAINT fk_ai_factory_item_tags_item
        FOREIGN KEY (item_id)
        REFERENCES {TABLE_AI_FACTORY_ITEMS}(id) ON DELETE RESTRICT,
    CONSTRAINT fk_ai_factory_item_tags_tag
        FOREIGN KEY (tag_id)
        REFERENCES {TABLE_AI_FACTORY_TOPIC_TAGS}(id) ON DELETE RESTRICT,
    PRIMARY KEY (item_id, tag_id)
);

CREATE INDEX IF NOT EXISTS idx_ai_factory_item_tags_tag
    ON {TABLE_AI_FACTORY_ITEM_TAGS}(tag_id, item_id);

CREATE TABLE IF NOT EXISTS {TABLE_AI_FACTORY_RELEASES} (
    id                    TEXT        PRIMARY KEY,
    release_kind          TEXT        NOT NULL
        CHECK (release_kind IN ('rag', 'sft')),
    version_no            INTEGER     NOT NULL CHECK (version_no > 0),
    schema_version        TEXT        NOT NULL,
    jsonl_text            TEXT        NOT NULL,
    jsonl_sha256          TEXT        NOT NULL,
    jsonl_bytes           INTEGER     NOT NULL,
    manifest_json         JSONB       NOT NULL,
    manifest_sha256       TEXT        NOT NULL,
    item_count            INTEGER     NOT NULL,
    published_by          TEXT        NOT NULL,
    published_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    invalidated_by        TEXT,
    invalidated_at        TIMESTAMPTZ,
    invalidation_reason   TEXT,
    CONSTRAINT ai_factory_releases_id_length
        CHECK (char_length(id) BETWEEN 1 AND 64),
    CONSTRAINT ai_factory_releases_metadata_lengths
        CHECK (
            char_length(schema_version) BETWEEN 1 AND 80
            AND char_length(published_by) BETWEEN 1 AND 200
        ),
    CONSTRAINT ai_factory_releases_bounds
        CHECK (
            item_count BETWEEN 1 AND 500
            AND jsonl_bytes BETWEEN 1 AND 5242880
            AND jsonl_bytes = octet_length(jsonl_text)
        ),
    CONSTRAINT ai_factory_releases_json_object
        CHECK (jsonb_typeof(manifest_json) = 'object'),
    CONSTRAINT ai_factory_releases_hashes
        CHECK (
            char_length(jsonl_sha256) = 64
            AND jsonl_sha256 = lower(jsonl_sha256)
            AND jsonl_sha256 ~ '^[0-9a-f]+$'
            AND char_length(manifest_sha256) = 64
            AND manifest_sha256 = lower(manifest_sha256)
            AND manifest_sha256 ~ '^[0-9a-f]+$'
        ),
    CONSTRAINT ai_factory_releases_invalidation_fields
        CHECK (
            (invalidated_at IS NULL
                AND invalidated_by IS NULL
                AND invalidation_reason IS NULL)
            OR
            (invalidated_at IS NOT NULL
                AND char_length(invalidated_by) BETWEEN 1 AND 200
                AND char_length(invalidation_reason) BETWEEN 1 AND 1000)
        ),
    UNIQUE (release_kind, version_no)
);

CREATE INDEX IF NOT EXISTS idx_ai_factory_releases_active
    ON {TABLE_AI_FACTORY_RELEASES}(release_kind, version_no DESC)
    WHERE invalidated_at IS NULL;

CREATE TABLE IF NOT EXISTS {TABLE_AI_FACTORY_RELEASE_ITEMS} (
    release_id         TEXT     NOT NULL,
    item_id            TEXT     NOT NULL,
    ordinal            INTEGER  NOT NULL CHECK (ordinal BETWEEN 1 AND 500),
    item_sha256        TEXT     NOT NULL,
    jsonl_line_sha256  TEXT     NOT NULL,
    CONSTRAINT ai_factory_release_items_hashes
        CHECK (
            char_length(item_sha256) = 64
            AND item_sha256 = lower(item_sha256)
            AND item_sha256 ~ '^[0-9a-f]+$'
            AND char_length(jsonl_line_sha256) = 64
            AND jsonl_line_sha256 = lower(jsonl_line_sha256)
            AND jsonl_line_sha256 ~ '^[0-9a-f]+$'
        ),
    CONSTRAINT fk_ai_factory_release_items_release
        FOREIGN KEY (release_id)
        REFERENCES {TABLE_AI_FACTORY_RELEASES}(id) ON DELETE RESTRICT,
    CONSTRAINT fk_ai_factory_release_items_item
        FOREIGN KEY (item_id)
        REFERENCES {TABLE_AI_FACTORY_ITEMS}(id) ON DELETE RESTRICT,
    PRIMARY KEY (release_id, item_id),
    UNIQUE (release_id, ordinal)
);

CREATE INDEX IF NOT EXISTS idx_ai_factory_release_items_item
    ON {TABLE_AI_FACTORY_RELEASE_ITEMS}(item_id, release_id);
"""

CREATE_AI_FACTORY_TRANSCRIPT_WORKFLOW = f"""
CREATE TABLE IF NOT EXISTS {TABLE_AI_FACTORY_TRANSCRIPTS} (
    id                    TEXT        PRIMARY KEY,
    title                 TEXT        NOT NULL,
    topic_text            TEXT,
    source_note           TEXT        NOT NULL,
    language_code         TEXT        NOT NULL
        CHECK (language_code IN ('yue-Hant-HK', 'zh-Hant', 'en', 'mixed', 'other')),
    rights_basis          TEXT        NOT NULL
        CHECK (rights_basis IN ('own_work', 'permission', 'open_license', 'public_domain', 'other')),
    rights_confirmed_by   TEXT        NOT NULL,
    rights_confirmed_at   TIMESTAMPTZ NOT NULL,
    content_text          TEXT        NOT NULL,
    content_sha256        TEXT        NOT NULL,
    created_by            TEXT        NOT NULL,
    created_at            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    withdrawn_by          TEXT,
    withdrawn_at          TIMESTAMPTZ,
    withdrawal_reason     TEXT,
    CONSTRAINT ai_factory_transcripts_id_length
        CHECK (char_length(id) BETWEEN 1 AND 64),
    CONSTRAINT ai_factory_transcripts_metadata_lengths
        CHECK (
            char_length(title) BETWEEN 1 AND 200
            AND (topic_text IS NULL OR char_length(topic_text) <= 500)
            AND char_length(source_note) BETWEEN 1 AND 1000
            AND char_length(rights_confirmed_by) BETWEEN 1 AND 200
            AND char_length(created_by) BETWEEN 1 AND 200
        ),
    CONSTRAINT ai_factory_transcripts_content_length
        CHECK (char_length(content_text) BETWEEN 1 AND 200000),
    CONSTRAINT ai_factory_transcripts_content_hash
        CHECK (
            char_length(content_sha256) = 64
            AND content_sha256 = lower(content_sha256)
            AND content_sha256 ~ '^[0-9a-f]+$'
        ),
    CONSTRAINT ai_factory_transcripts_withdrawal_fields
        CHECK (
            (withdrawn_at IS NULL AND withdrawn_by IS NULL AND withdrawal_reason IS NULL)
            OR
            (withdrawn_at IS NOT NULL
                AND char_length(withdrawn_by) BETWEEN 1 AND 200
                AND char_length(withdrawal_reason) BETWEEN 1 AND 1000)
        )
);

CREATE INDEX IF NOT EXISTS idx_ai_factory_transcripts_active_created
    ON {TABLE_AI_FACTORY_TRANSCRIPTS}(created_at DESC)
    WHERE withdrawn_at IS NULL;

CREATE TABLE IF NOT EXISTS {TABLE_AI_FACTORY_TRANSCRIPT_RUNS} (
    id                      TEXT        PRIMARY KEY,
    transcript_id           TEXT        NOT NULL,
    recipe_key              TEXT        NOT NULL
        CHECK (recipe_key = 'transcript_structure_v1'),
    model_label             TEXT        NOT NULL,
    provider                TEXT        NOT NULL,
    provider_model          TEXT        NOT NULL,
    prompt_version          TEXT        NOT NULL,
    prompt_template_sha256  TEXT        NOT NULL,
    instruction_text        TEXT        NOT NULL DEFAULT '',
    window_count            SMALLINT    NOT NULL CHECK (window_count BETWEEN 1 AND 40),
    estimated_cost_hkd      NUMERIC(20, 8) NOT NULL DEFAULT 0
        CHECK (estimated_cost_hkd >= 0),
    status                  TEXT        NOT NULL DEFAULT 'draft'
        CHECK (status IN ('draft', 'processing', 'awaiting_review', 'reviewed', 'failed', 'invalidated')),
    preview_manifest_sha256 TEXT        NOT NULL,
    preview_expires_at      TIMESTAMPTZ NOT NULL,
    confirmation_version    TEXT,
    anonymization_confirmed BOOLEAN,
    rights_confirmed        BOOLEAN,
    third_party_confirmed   BOOLEAN,
    pii_warning_count       SMALLINT
        CHECK (pii_warning_count IS NULL OR pii_warning_count BETWEEN 0 AND 20),
    pii_override_reason     TEXT,
    confirmed_by            TEXT,
    confirmed_at            TIMESTAMPTZ,
    created_by              TEXT        NOT NULL,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    invalidated_by          TEXT,
    invalidated_at          TIMESTAMPTZ,
    invalidation_reason     TEXT,
    CONSTRAINT ai_factory_transcript_runs_id_length
        CHECK (char_length(id) BETWEEN 1 AND 64),
    CONSTRAINT ai_factory_transcript_runs_identity_lengths
        CHECK (
            char_length(model_label) BETWEEN 1 AND 200
            AND char_length(provider) BETWEEN 1 AND 80
            AND char_length(provider_model) BETWEEN 1 AND 200
            AND char_length(prompt_version) BETWEEN 1 AND 80
            AND char_length(instruction_text) <= 500
            AND char_length(created_by) BETWEEN 1 AND 200
        ),
    CONSTRAINT ai_factory_transcript_runs_hashes
        CHECK (
            char_length(prompt_template_sha256) = 64
            AND prompt_template_sha256 = lower(prompt_template_sha256)
            AND prompt_template_sha256 ~ '^[0-9a-f]+$'
            AND char_length(preview_manifest_sha256) = 64
            AND preview_manifest_sha256 = lower(preview_manifest_sha256)
            AND preview_manifest_sha256 ~ '^[0-9a-f]+$'
        ),
    CONSTRAINT ai_factory_transcript_runs_confirmation
        CHECK (
            (status IN ('draft', 'invalidated')
                AND confirmation_version IS NULL
                AND anonymization_confirmed IS NULL
                AND rights_confirmed IS NULL
                AND third_party_confirmed IS NULL
                AND pii_warning_count IS NULL
                AND pii_override_reason IS NULL
                AND confirmed_by IS NULL
                AND confirmed_at IS NULL)
            OR
            (status <> 'draft'
                AND char_length(confirmation_version) BETWEEN 1 AND 80
                AND anonymization_confirmed = TRUE
                AND rights_confirmed = TRUE
                AND third_party_confirmed = TRUE
                AND pii_warning_count IS NOT NULL
                AND char_length(confirmed_by) BETWEEN 1 AND 200
                AND confirmed_at IS NOT NULL
                AND confirmed_at <= preview_expires_at
                AND (
                    (pii_warning_count = 0 AND pii_override_reason IS NULL)
                    OR
                    (pii_warning_count > 0
                        AND char_length(pii_override_reason) BETWEEN 1 AND 1000)
                ))
        ),
    CONSTRAINT ai_factory_transcript_runs_invalidation_fields
        CHECK (
            (status <> 'invalidated'
                AND invalidated_by IS NULL AND invalidated_at IS NULL
                AND invalidation_reason IS NULL)
            OR
            (status = 'invalidated'
                AND char_length(invalidated_by) BETWEEN 1 AND 200
                AND invalidated_at IS NOT NULL
                AND char_length(invalidation_reason) BETWEEN 1 AND 1000)
        ),
    CONSTRAINT fk_ai_factory_transcript_runs_transcript
        FOREIGN KEY (transcript_id)
        REFERENCES {TABLE_AI_FACTORY_TRANSCRIPTS}(id) ON DELETE RESTRICT
);

CREATE INDEX IF NOT EXISTS idx_ai_factory_transcript_runs_transcript_created
    ON {TABLE_AI_FACTORY_TRANSCRIPT_RUNS}(transcript_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_ai_factory_transcript_runs_status_updated
    ON {TABLE_AI_FACTORY_TRANSCRIPT_RUNS}(status, updated_at DESC);

CREATE TABLE IF NOT EXISTS {TABLE_AI_FACTORY_TRANSCRIPT_WINDOWS} (
    id                  TEXT        PRIMARY KEY,
    run_id              TEXT        NOT NULL,
    ordinal             SMALLINT    NOT NULL CHECK (ordinal BETWEEN 1 AND 40),
    context_start       INTEGER     NOT NULL CHECK (context_start >= 0),
    context_end         INTEGER     NOT NULL CHECK (context_end > context_start),
    core_start          INTEGER     NOT NULL CHECK (core_start >= context_start),
    core_end            INTEGER     NOT NULL CHECK (core_end > core_start),
    prompt_sha256       TEXT        NOT NULL,
    input_sha256        TEXT        NOT NULL,
    preview_sha256      TEXT        NOT NULL,
    status              TEXT        NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'processing', 'succeeded', 'failed', 'discarded')),
    attempt_count       SMALLINT    NOT NULL DEFAULT 0 CHECK (attempt_count BETWEEN 0 AND 3),
    boundary_json       JSONB,
    boundary_sha256     TEXT,
    error_code          TEXT,
    started_at          TIMESTAMPTZ,
    completed_at        TIMESTAMPTZ,
    CONSTRAINT ai_factory_transcript_windows_id_length
        CHECK (char_length(id) BETWEEN 1 AND 64),
    CONSTRAINT ai_factory_transcript_windows_bounds
        CHECK (core_end <= context_end),
    CONSTRAINT ai_factory_transcript_windows_hashes
        CHECK (
            char_length(prompt_sha256) = 64
            AND prompt_sha256 = lower(prompt_sha256)
            AND prompt_sha256 ~ '^[0-9a-f]+$'
            AND char_length(input_sha256) = 64
            AND input_sha256 = lower(input_sha256)
            AND input_sha256 ~ '^[0-9a-f]+$'
            AND char_length(preview_sha256) = 64
            AND preview_sha256 = lower(preview_sha256)
            AND preview_sha256 ~ '^[0-9a-f]+$'
            AND (
                (boundary_json IS NULL AND boundary_sha256 IS NULL)
                OR
                (jsonb_typeof(boundary_json) = 'array'
                    AND char_length(boundary_sha256) = 64
                    AND boundary_sha256 = lower(boundary_sha256)
                    AND boundary_sha256 ~ '^[0-9a-f]+$')
            )
        ),
    CONSTRAINT ai_factory_transcript_windows_status_fields
        CHECK (
            (status = 'pending' AND started_at IS NULL AND completed_at IS NULL
                AND error_code IS NULL AND boundary_json IS NULL)
            OR
            (status = 'processing' AND started_at IS NOT NULL AND completed_at IS NULL
                AND error_code IS NULL AND boundary_json IS NULL)
            OR
            (status = 'succeeded' AND started_at IS NOT NULL AND completed_at IS NOT NULL
                AND error_code IS NULL AND boundary_json IS NOT NULL)
            OR
            (status IN ('failed', 'discarded') AND completed_at IS NOT NULL
                AND char_length(error_code) BETWEEN 1 AND 120)
        ),
    CONSTRAINT fk_ai_factory_transcript_windows_run
        FOREIGN KEY (run_id)
        REFERENCES {TABLE_AI_FACTORY_TRANSCRIPT_RUNS}(id) ON DELETE RESTRICT,
    UNIQUE (run_id, ordinal),
    UNIQUE (id, run_id)
);

CREATE INDEX IF NOT EXISTS idx_ai_factory_transcript_windows_run_status
    ON {TABLE_AI_FACTORY_TRANSCRIPT_WINDOWS}(run_id, status, ordinal);

CREATE TABLE IF NOT EXISTS {TABLE_AI_FACTORY_TRANSCRIPT_ATTEMPTS} (
    id                        TEXT        PRIMARY KEY,
    run_id                    TEXT        NOT NULL,
    window_id                 TEXT        NOT NULL,
    attempt_no                SMALLINT    NOT NULL CHECK (attempt_no BETWEEN 1 AND 3),
    operation_id              TEXT        NOT NULL,
    model_label               TEXT        NOT NULL,
    provider                  TEXT        NOT NULL,
    provider_model            TEXT        NOT NULL,
    prompt_version            TEXT        NOT NULL,
    prompt_sha256             TEXT        NOT NULL,
    input_sha256              TEXT        NOT NULL,
    preview_sha256            TEXT        NOT NULL,
    estimated_cost_hkd        NUMERIC(20, 8) NOT NULL DEFAULT 0
        CHECK (estimated_cost_hkd >= 0),
    confirmed_by              TEXT        NOT NULL,
    confirmed_at              TIMESTAMPTZ NOT NULL,
    status                    TEXT        NOT NULL DEFAULT 'claimed'
        CHECK (status IN ('claimed', 'running', 'succeeded', 'failed', 'discarded')),
    provider_attempted_at     TIMESTAMPTZ,
    provider_request_id       TEXT,
    resolved_provider_model   TEXT,
    response_sha256           TEXT,
    response_bytes            INTEGER,
    error_code                TEXT,
    completed_at              TIMESTAMPTZ,
    created_at                TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT ai_factory_transcript_attempts_id_length
        CHECK (char_length(id) BETWEEN 1 AND 64),
    CONSTRAINT ai_factory_transcript_attempts_operation
        CHECK (operation_id = run_id),
    CONSTRAINT ai_factory_transcript_attempts_identity_lengths
        CHECK (
            char_length(model_label) BETWEEN 1 AND 200
            AND char_length(provider) BETWEEN 1 AND 80
            AND char_length(provider_model) BETWEEN 1 AND 200
            AND char_length(prompt_version) BETWEEN 1 AND 80
            AND char_length(confirmed_by) BETWEEN 1 AND 200
            AND (provider_request_id IS NULL OR char_length(provider_request_id) <= 300)
            AND (resolved_provider_model IS NULL
                OR char_length(resolved_provider_model) BETWEEN 1 AND 200)
        ),
    CONSTRAINT ai_factory_transcript_attempts_hashes
        CHECK (
            char_length(prompt_sha256) = 64
            AND prompt_sha256 = lower(prompt_sha256)
            AND prompt_sha256 ~ '^[0-9a-f]+$'
            AND char_length(input_sha256) = 64
            AND input_sha256 = lower(input_sha256)
            AND input_sha256 ~ '^[0-9a-f]+$'
            AND char_length(preview_sha256) = 64
            AND preview_sha256 = lower(preview_sha256)
            AND preview_sha256 ~ '^[0-9a-f]+$'
            AND (response_sha256 IS NULL OR (
                char_length(response_sha256) = 64
                AND response_sha256 = lower(response_sha256)
                AND response_sha256 ~ '^[0-9a-f]+$'))
        ),
    CONSTRAINT ai_factory_transcript_attempts_response_size
        CHECK (response_bytes IS NULL OR response_bytes BETWEEN 0 AND 102400),
    CONSTRAINT ai_factory_transcript_attempts_status_fields
        CHECK (
            (status = 'claimed' AND provider_attempted_at IS NULL
                AND completed_at IS NULL AND error_code IS NULL)
            OR
            (status = 'running' AND provider_attempted_at IS NOT NULL
                AND completed_at IS NULL AND error_code IS NULL)
            OR
            (status = 'succeeded' AND provider_attempted_at IS NOT NULL
                AND completed_at IS NOT NULL AND response_sha256 IS NOT NULL
                AND response_bytes > 0 AND error_code IS NULL)
            OR
            (status IN ('failed', 'discarded') AND completed_at IS NOT NULL
                AND char_length(error_code) BETWEEN 1 AND 120)
        ),
    CONSTRAINT fk_ai_factory_transcript_attempts_window_run
        FOREIGN KEY (window_id, run_id)
        REFERENCES {TABLE_AI_FACTORY_TRANSCRIPT_WINDOWS}(id, run_id) ON DELETE RESTRICT,
    UNIQUE (window_id, attempt_no)
);

CREATE INDEX IF NOT EXISTS idx_ai_factory_transcript_attempts_run_created
    ON {TABLE_AI_FACTORY_TRANSCRIPT_ATTEMPTS}(run_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_ai_factory_transcript_attempts_processing
    ON {TABLE_AI_FACTORY_TRANSCRIPT_ATTEMPTS}(provider_attempted_at)
    WHERE status IN ('claimed', 'running');

CREATE TABLE IF NOT EXISTS {TABLE_AI_FACTORY_TRANSCRIPT_SEGMENTS} (
    id                    TEXT        PRIMARY KEY,
    run_id                TEXT        NOT NULL,
    transcript_id         TEXT        NOT NULL,
    origin_window_id      TEXT        NOT NULL,
    start_offset          INTEGER     NOT NULL CHECK (start_offset >= 0),
    end_offset            INTEGER     NOT NULL CHECK (end_offset > start_offset),
    original_json         JSONB       NOT NULL,
    original_sha256       TEXT        NOT NULL,
    reviewed_json         JSONB,
    reviewed_sha256       TEXT,
    review_status         TEXT        NOT NULL DEFAULT 'pending'
        CHECK (review_status IN ('pending', 'approved', 'rejected')),
    review_note           TEXT,
    reviewed_by           TEXT,
    reviewed_at           TIMESTAMPTZ,
    approved_source_id    TEXT,
    created_at            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT ai_factory_transcript_segments_id_length
        CHECK (char_length(id) BETWEEN 1 AND 64),
    CONSTRAINT ai_factory_transcript_segments_json_objects
        CHECK (jsonb_typeof(original_json) = 'object'
            AND (reviewed_json IS NULL OR jsonb_typeof(reviewed_json) = 'object')),
    CONSTRAINT ai_factory_transcript_segments_hashes
        CHECK (
            char_length(original_sha256) = 64
            AND original_sha256 = lower(original_sha256)
            AND original_sha256 ~ '^[0-9a-f]+$'
            AND (
                (reviewed_json IS NULL AND reviewed_sha256 IS NULL)
                OR
                (reviewed_json IS NOT NULL
                    AND char_length(reviewed_sha256) = 64
                    AND reviewed_sha256 = lower(reviewed_sha256)
                    AND reviewed_sha256 ~ '^[0-9a-f]+$')
            )
        ),
    CONSTRAINT ai_factory_transcript_segments_review_fields
        CHECK (
            (review_status = 'pending' AND reviewed_json IS NULL
                AND reviewed_sha256 IS NULL AND reviewed_by IS NULL
                AND reviewed_at IS NULL AND approved_source_id IS NULL)
            OR
            (review_status = 'approved' AND reviewed_json IS NOT NULL
                AND reviewed_sha256 IS NOT NULL
                AND char_length(reviewed_by) BETWEEN 1 AND 200
                AND reviewed_at IS NOT NULL AND approved_source_id IS NOT NULL)
            OR
            (review_status = 'rejected'
                AND char_length(reviewed_by) BETWEEN 1 AND 200
                AND reviewed_at IS NOT NULL AND approved_source_id IS NULL)
        ),
    CONSTRAINT ai_factory_transcript_segments_note_length
        CHECK (review_note IS NULL OR char_length(review_note) <= 2000),
    CONSTRAINT fk_ai_factory_transcript_segments_run
        FOREIGN KEY (run_id)
        REFERENCES {TABLE_AI_FACTORY_TRANSCRIPT_RUNS}(id) ON DELETE RESTRICT,
    CONSTRAINT fk_ai_factory_transcript_segments_transcript
        FOREIGN KEY (transcript_id)
        REFERENCES {TABLE_AI_FACTORY_TRANSCRIPTS}(id) ON DELETE RESTRICT,
    CONSTRAINT fk_ai_factory_transcript_segments_window
        FOREIGN KEY (origin_window_id)
        REFERENCES {TABLE_AI_FACTORY_TRANSCRIPT_WINDOWS}(id) ON DELETE RESTRICT,
    CONSTRAINT fk_ai_factory_transcript_segments_source
        FOREIGN KEY (approved_source_id)
        REFERENCES {TABLE_AI_FACTORY_SOURCES}(id) ON DELETE RESTRICT,
    UNIQUE (run_id, start_offset)
);

CREATE INDEX IF NOT EXISTS idx_ai_factory_transcript_segments_review_queue
    ON {TABLE_AI_FACTORY_TRANSCRIPT_SEGMENTS}(created_at, start_offset)
    WHERE review_status = 'pending';
CREATE INDEX IF NOT EXISTS idx_ai_factory_transcript_segments_run_offset
    ON {TABLE_AI_FACTORY_TRANSCRIPT_SEGMENTS}(run_id, start_offset);
"""

LOCK_AI_FACTORY_TRANSCRIPT_PRIVILEGES = f"""
REVOKE ALL PRIVILEGES ON TABLE
    {TABLE_AI_FACTORY_TRANSCRIPTS},
    {TABLE_AI_FACTORY_TRANSCRIPT_RUNS},
    {TABLE_AI_FACTORY_TRANSCRIPT_WINDOWS},
    {TABLE_AI_FACTORY_TRANSCRIPT_ATTEMPTS},
    {TABLE_AI_FACTORY_TRANSCRIPT_SEGMENTS}
FROM PUBLIC;
DO $$
DECLARE
    role_name TEXT;
    table_name TEXT;
BEGIN
    FOR role_name IN
        SELECT rolname FROM pg_roles
        WHERE rolname IN ('anon', 'authenticated')
    LOOP
        FOREACH table_name IN ARRAY ARRAY[
            '{TABLE_AI_FACTORY_TRANSCRIPTS}',
            '{TABLE_AI_FACTORY_TRANSCRIPT_RUNS}',
            '{TABLE_AI_FACTORY_TRANSCRIPT_WINDOWS}',
            '{TABLE_AI_FACTORY_TRANSCRIPT_ATTEMPTS}',
            '{TABLE_AI_FACTORY_TRANSCRIPT_SEGMENTS}'
        ]
        LOOP
            EXECUTE format(
                'REVOKE ALL PRIVILEGES ON TABLE %I FROM %I',
                table_name,
                role_name
            );
        END LOOP;
    END LOOP;
END $$;
"""

LOCK_AI_DATA_FACTORY_PRIVILEGES = f"""
REVOKE ALL PRIVILEGES ON TABLE
    {TABLE_AI_FACTORY_SOURCES},
    {TABLE_AI_FACTORY_JOBS},
    {TABLE_AI_FACTORY_ATTEMPTS},
    {TABLE_AI_FACTORY_ITEMS},
    {TABLE_AI_FACTORY_TOPIC_TAGS},
    {TABLE_AI_FACTORY_ITEM_TAGS},
    {TABLE_AI_FACTORY_RELEASES},
    {TABLE_AI_FACTORY_RELEASE_ITEMS}
FROM PUBLIC;
DO $$
DECLARE
    role_name TEXT;
    table_name TEXT;
BEGIN
    FOR role_name IN
        SELECT rolname FROM pg_roles
        WHERE rolname IN ('anon', 'authenticated')
    LOOP
        FOREACH table_name IN ARRAY ARRAY[
            '{TABLE_AI_FACTORY_SOURCES}',
            '{TABLE_AI_FACTORY_JOBS}',
            '{TABLE_AI_FACTORY_ATTEMPTS}',
            '{TABLE_AI_FACTORY_ITEMS}',
            '{TABLE_AI_FACTORY_TOPIC_TAGS}',
            '{TABLE_AI_FACTORY_ITEM_TAGS}',
            '{TABLE_AI_FACTORY_RELEASES}',
            '{TABLE_AI_FACTORY_RELEASE_ITEMS}'
        ]
        LOOP
            EXECUTE format(
                'REVOKE ALL PRIVILEGES ON TABLE %I FROM %I',
                table_name,
                role_name
            );
        END LOOP;
    END LOOP;
END $$;
"""

# Table: MATCH_ROSTER_LINKS
# Unguessable per-side links for teams to submit their own roster.
CREATE_MATCH_ROSTER_LINKS = f"""
CREATE TABLE IF NOT EXISTS {TABLE_MATCH_ROSTER_LINKS} (
    match_id        TEXT,
    side            TEXT        CHECK (side IN ('pro', 'con')),
    roster_token    TEXT        NOT NULL UNIQUE,
    submitted_at    TIMESTAMP,
    created_at      TIMESTAMP   DEFAULT NOW(),
    PRIMARY KEY (match_id, side),
    CONSTRAINT fk_match_roster_links_match
        FOREIGN KEY (match_id) REFERENCES {TABLE_MATCHES}(match_id)
        ON DELETE CASCADE
);
"""

# Table: MOTION_COMMENTS
# Discussion comments on pending topic votes and removal motions.
# motion_type: 'topic_vote' | 'topic_removal'
# motion_key: the topic_text identifying the motion.
CREATE_MOTION_COMMENTS = f"""
CREATE TABLE IF NOT EXISTS {TABLE_MOTION_COMMENTS} (
    id              SERIAL      PRIMARY KEY,
    motion_type     TEXT        CHECK (motion_type IN ('topic_vote', 'topic_removal')),
    motion_key      TEXT        NOT NULL,
    user_id         TEXT        NOT NULL,
    comment_text    TEXT        NOT NULL,
    sticker_id      TEXT,
    created_at      TIMESTAMP   DEFAULT NOW(),
    CONSTRAINT chk_motion_comments_sticker_id
        CHECK (sticker_id IS NULL OR char_length(sticker_id) BETWEEN 1 AND 200),
    CONSTRAINT fk_motion_comments_user
        FOREIGN KEY (user_id) REFERENCES {TABLE_ACCOUNTS}(user_id)
        ON DELETE CASCADE
);
"""

# Table: AI_FUND_TRANSACTIONS
# Internal ledger for the AI funding pool.
# transaction_type: 'member_deposit' | 'provider_topup' | 'provider_refund' | 'member_refund' | 'adjustment'
# status: 'pending' | 'confirmed' | 'rejected'
CREATE_AI_FUND_TRANSACTIONS = f"""
CREATE TABLE IF NOT EXISTS {TABLE_AI_FUND_TRANSACTIONS} (
    id                  SERIAL      PRIMARY KEY,
    transaction_type    TEXT        NOT NULL,
    status              TEXT        DEFAULT 'pending'
                                CHECK (status IN ('pending', 'confirmed', 'rejected')),
    amount_hkd          NUMERIC(10, 2) NOT NULL,
    payment_method      TEXT,
    reference_no        TEXT,
    note                TEXT,
    created_by          TEXT,
    created_at          TIMESTAMP   DEFAULT NOW(),
    confirmed_by        TEXT,
    confirmed_at        TIMESTAMP,
    rejected_by         TEXT,
    rejected_at         TIMESTAMP,
    status_note         TEXT,
    provider            TEXT,
    CONSTRAINT chk_ai_fund_transaction_type
        CHECK (
            transaction_type IN (
                'member_deposit', 'provider_topup', 'provider_refund',
                'member_refund', 'adjustment'
            )
        ),
    CONSTRAINT fk_ai_fund_tx_created_by
        FOREIGN KEY (created_by) REFERENCES {TABLE_ACCOUNTS}(user_id)
        ON DELETE SET NULL,
    CONSTRAINT fk_ai_fund_tx_confirmed_by
        FOREIGN KEY (confirmed_by) REFERENCES {TABLE_ACCOUNTS}(user_id)
        ON DELETE SET NULL,
    CONSTRAINT fk_ai_fund_tx_rejected_by
        FOREIGN KEY (rejected_by) REFERENCES {TABLE_ACCOUNTS}(user_id)
        ON DELETE SET NULL
);
"""

# Table: AI_FUND_USAGE_LOGS
# Estimated AI usage costs for transparency and monthly review.
# ``lmc_ai_eval`` is retained only as a legacy shared-ledger value so existing
# usage rows remain readable after the dedicated evaluation tables are removed.
CREATE_AI_FUND_USAGE_LOGS = f"""
CREATE TABLE IF NOT EXISTS {TABLE_AI_FUND_USAGE_LOGS} (
    id                  SERIAL      PRIMARY KEY,
    user_id             TEXT,
    feature             TEXT        NOT NULL
                                CHECK (feature IN ('speech_review', 'strategy', 'competition_prep', 'web_research', 'fact_check', 'free_debate_live', 'full_mock_live', 'vote_review', 'vote_analysis', 'vote_discussion', 'tts_review', 'tts_script_analysis', 'llm_review', 'kiosk_match_review', 'tts', 'kiosk_match_review_tts', 'data_factory_generation', 'official_ai_judge', 'lmc_ai_chat', 'lmc_ai_eval')),
    model_label         TEXT        NOT NULL,
    provider            TEXT,
    estimated_cost_usd  NUMERIC(12, 6) DEFAULT 0,
    estimated_cost_hkd  NUMERIC(10, 4) DEFAULT 0,
    input_tokens        INTEGER     DEFAULT 0,
    output_tokens       INTEGER     DEFAULT 0,
    audio_tokens        INTEGER     DEFAULT 0,
    billable_characters INTEGER     NOT NULL DEFAULT 0
                                CHECK (billable_characters >= 0),
    search_calls        INTEGER     DEFAULT 0,
    provider_duration_ms INTEGER    NOT NULL DEFAULT 0
                                CHECK (provider_duration_ms >= 0),
    operation_id        TEXT        CHECK (
                                operation_id IS NULL
                                OR CHAR_LENGTH(operation_id) BETWEEN 1 AND 200
                            ),
    operation_stage     TEXT        CHECK (
                                operation_stage IS NULL
                                OR CHAR_LENGTH(operation_stage) BETWEEN 1 AND 80
                            ),
    cost_source         TEXT        DEFAULT 'estimate',
    status              TEXT        DEFAULT 'success'
                                CHECK (status IN ('success', 'failed')),
    error_message       TEXT,
    created_at          TIMESTAMP   DEFAULT NOW(),
    CONSTRAINT fk_ai_fund_usage_user
        FOREIGN KEY (user_id) REFERENCES {TABLE_ACCOUNTS}(user_id)
        ON DELETE SET NULL
);
"""

# Private local-AI computer registry. Conversation content and prompts are
# deliberately absent; connection/queue state remains process-local memory.
CREATE_LMC_AI_NODES = f"""
CREATE TABLE IF NOT EXISTS {TABLE_LMC_AI_NODES} (
    node_id               TEXT        PRIMARY KEY,
    display_name          TEXT        NOT NULL,
    token_hash            TEXT        NOT NULL UNIQUE,
    enabled               BOOLEAN     NOT NULL DEFAULT TRUE,
    last_runtime          TEXT,
    last_runtime_version  TEXT,
    last_model            TEXT,
    last_capabilities     JSONB,
    created_at            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_connected_at     TIMESTAMPTZ,
    last_disconnected_at  TIMESTAMPTZ,
    CONSTRAINT lmc_ai_nodes_id_length
        CHECK (CHAR_LENGTH(node_id) BETWEEN 1 AND 64),
    CONSTRAINT lmc_ai_nodes_name_length
        CHECK (CHAR_LENGTH(display_name) BETWEEN 1 AND 80),
    CONSTRAINT lmc_ai_nodes_token_hash
        CHECK (
            CHAR_LENGTH(token_hash) = 64
            AND token_hash = LOWER(token_hash)
            AND token_hash ~ '^[0-9a-f]+$'
        ),
    CONSTRAINT lmc_ai_nodes_capabilities_object
        CHECK (
            last_capabilities IS NULL
            OR JSONB_TYPEOF(last_capabilities) = 'object'
        )
);
CREATE INDEX IF NOT EXISTS idx_lmc_ai_nodes_enabled_created
    ON {TABLE_LMC_AI_NODES}(enabled, created_at DESC);
COMMENT ON TABLE {TABLE_LMC_AI_NODES} IS
    'skhlmc-feature:lmc_ai:20260722_0002';

REVOKE ALL PRIVILEGES ON TABLE {TABLE_LMC_AI_NODES} FROM PUBLIC;
DO $lmc_ai_privileges$
DECLARE
    role_name TEXT;
BEGIN
    FOREACH role_name IN ARRAY ARRAY['anon', 'authenticated']
    LOOP
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname=role_name) THEN
            EXECUTE FORMAT(
                'REVOKE ALL PRIVILEGES ON TABLE {TABLE_LMC_AI_NODES} FROM %I',
                role_name
            );
        END IF;
    END LOOP;
END $lmc_ai_privileges$;
"""

# Short-lived direct-R2 functional probes issued to authenticated Workstations.
# Object bytes remain private in R2 and a retention worker removes abandoned
# uploads. One open row per node provides a hard abuse/concurrency bound.
CREATE_WORKSTATION_R2_HEALTH_PROBES = f"""
CREATE TABLE IF NOT EXISTS {TABLE_WORKSTATION_R2_HEALTH_PROBES} (
    intent_id   TEXT        PRIMARY KEY,
    node_id     TEXT        NOT NULL UNIQUE
                            REFERENCES {TABLE_LMC_AI_NODES}(node_id)
                            ON DELETE CASCADE,
    object_key  TEXT        NOT NULL UNIQUE,
    sha256      TEXT        NOT NULL,
    byte_size   INTEGER     NOT NULL CHECK (byte_size > 0),
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT workstation_r2_health_intent_id
        CHECK (CHAR_LENGTH(intent_id) = 32 AND intent_id ~ '^[0-9a-f]+$'),
    CONSTRAINT workstation_r2_health_object_key
        CHECK (
            CHAR_LENGTH(object_key) BETWEEN 1 AND 512
            AND object_key LIKE 'pending/workstation-health/%'
        ),
    CONSTRAINT workstation_r2_health_sha256
        CHECK (
            CHAR_LENGTH(sha256) = 64
            AND sha256 = LOWER(sha256)
            AND sha256 ~ '^[0-9a-f]+$'
        )
);
CREATE INDEX IF NOT EXISTS idx_workstation_r2_health_created
    ON {TABLE_WORKSTATION_R2_HEALTH_PROBES}(created_at);

REVOKE ALL PRIVILEGES ON TABLE {TABLE_WORKSTATION_R2_HEALTH_PROBES} FROM PUBLIC;
DO $workstation_r2_health_privileges$
DECLARE
    role_name TEXT;
BEGIN
    FOREACH role_name IN ARRAY ARRAY['anon', 'authenticated']
    LOOP
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname=role_name) THEN
            EXECUTE FORMAT(
                'REVOKE ALL PRIVILEGES ON TABLE {TABLE_WORKSTATION_R2_HEALTH_PROBES} FROM %I',
                role_name
            );
        END IF;
    END LOOP;
END $workstation_r2_health_privileges$;
"""

# Table: LATENESS_FUND_RECORDS
# Internal late penalty records.
# Penalty is calculated from display/query logic as nth late record per member × late_minutes.
CREATE_LATENESS_FUND_RECORDS = f"""
CREATE TABLE IF NOT EXISTS {TABLE_LATENESS_FUND_RECORDS} (
    id              SERIAL      PRIMARY KEY,
    late_date       DATE        NOT NULL,
    member_user_id  TEXT,
    late_minutes    INTEGER     NOT NULL CHECK (late_minutes > 0),
    paid_amount     NUMERIC(10, 2) DEFAULT 0,
    note            TEXT,
    created_by      TEXT,
    created_at      TIMESTAMP   DEFAULT NOW(),
    updated_at      TIMESTAMP,
    CONSTRAINT fk_lateness_fund_record_member
        FOREIGN KEY (member_user_id) REFERENCES {TABLE_ACCOUNTS}(user_id)
        ON DELETE SET NULL,
    CONSTRAINT fk_lateness_fund_record_created_by
        FOREIGN KEY (created_by) REFERENCES {TABLE_ACCOUNTS}(user_id)
        ON DELETE SET NULL
);
"""

# Table: LATENESS_FUND_EXPENSES
# Expenses paid out from collected late penalties.
CREATE_LATENESS_FUND_EXPENSES = f"""
CREATE TABLE IF NOT EXISTS {TABLE_LATENESS_FUND_EXPENSES} (
    id              SERIAL      PRIMARY KEY,
    expense_date    DATE        NOT NULL,
    amount_hkd      NUMERIC(10, 2) NOT NULL CHECK (amount_hkd > 0),
    note            TEXT,
    created_by      TEXT,
    created_at      TIMESTAMP   DEFAULT NOW(),
    CONSTRAINT fk_lateness_fund_expense_created_by
        FOREIGN KEY (created_by) REFERENCES {TABLE_ACCOUNTS}(user_id)
        ON DELETE SET NULL
);
"""

# Table: LATENESS_FUND_PERIODS
# Per-fiscal-year opening balance (Bal b/d) for the lateness fund custodian account.
# Closing balance (Bal c/d) is derived: opening_balance + received - expenses in the year.
CREATE_LATENESS_FUND_PERIODS = f"""
CREATE TABLE IF NOT EXISTS {TABLE_LATENESS_FUND_PERIODS} (
    year_label      TEXT        PRIMARY KEY,
    opening_balance NUMERIC(10, 2) DEFAULT 0,
    note            TEXT,
    updated_at      TIMESTAMP   DEFAULT NOW()
);
"""

# Table: BUG_REPORTS
# Committee member bug reports and developer replies.
CREATE_BUG_REPORTS = f"""
CREATE TABLE IF NOT EXISTS {TABLE_BUG_REPORTS} (
    id                    SERIAL PRIMARY KEY,
    reporter_user_id      TEXT,
    affected_page         TEXT        NOT NULL,
    device_info           TEXT,
    reproduction_steps    TEXT        NOT NULL,
    expected_result       TEXT,
    actual_result         TEXT        NOT NULL,
    extra_notes           TEXT,
    status                TEXT        DEFAULT 'open',
    developer_reply       TEXT,
    fixed_version         TEXT,
    created_at            TIMESTAMP   DEFAULT CURRENT_TIMESTAMP,
    updated_at            TIMESTAMP   DEFAULT CURRENT_TIMESTAMP,
    resolved_at           TIMESTAMP,
    CONSTRAINT fk_bug_reports_reporter
        FOREIGN KEY (reporter_user_id) REFERENCES {TABLE_ACCOUNTS}(user_id)
        ON DELETE SET NULL
);
"""

# View: COMMITTEE_VOTE_ACTIVITY
# Canonical source for committee participation metrics used by the API.
# Both eligible motions and cast-ballot counts start at each account's
# active_since date; system/service accounts are never committee members.
CREATE_COMMITTEE_VOTE_ACTIVITY_VIEW = f"""
DROP VIEW IF EXISTS {VIEW_COMMITTEE_VOTE_ACTIVITY};
CREATE VIEW {VIEW_COMMITTEE_VOTE_ACTIVITY} AS
WITH eligible_accounts AS (
    SELECT a.user_id, a.account_status, a.active_since
    FROM {TABLE_ACCOUNTS} a
    WHERE LOWER(a.user_id) NOT IN ({sql_account_id_literals(NON_MEMBER_ACCOUNT_DB_KEYS)})
      AND a.user_id != ''
      AND COALESCE(a.account_disabled, FALSE) = FALSE
),
all_events AS (
    SELECT tv.topic_text, tv.created_at, 'tv'::TEXT AS vote_source
    FROM {TABLE_TOPIC_VOTES} tv
    JOIN (
        SELECT DISTINCT topic_text FROM {TABLE_TOPIC_VOTE_BALLOTS}
    ) ballots ON ballots.topic_text = tv.topic_text
    UNION ALL
    SELECT removal.topic_text, removal.created_at, 'tdv'::TEXT AS vote_source
    FROM {TABLE_TOPIC_REMOVAL_VOTES} removal
    JOIN (
        SELECT DISTINCT topic_text FROM {TABLE_TOPIC_REMOVAL_VOTE_BALLOTS}
    ) ballots ON ballots.topic_text = removal.topic_text
),
event_ballots AS (
    SELECT ballot.topic_text, ballot.user_id, ballot.vote_choice,
           'tv'::TEXT AS vote_source
    FROM {TABLE_TOPIC_VOTE_BALLOTS} ballot
    UNION ALL
    SELECT ballot.topic_text, ballot.user_id, ballot.vote_choice,
           'tdv'::TEXT AS vote_source
    FROM {TABLE_TOPIC_REMOVAL_VOTE_BALLOTS} ballot
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
      OR event.created_at::DATE >= account.active_since
    LEFT JOIN event_ballots ballot
      ON ballot.topic_text = event.topic_text
     AND ballot.vote_source = event.vote_source
     AND ballot.user_id = account.user_id
),
base_stats AS (
    SELECT
        account.user_id,
        account.account_status,
        COUNT(activity.topic_text) AS total_votes,
        COUNT(activity.vote_choice) AS participated_votes,
        COUNT(activity.vote_choice) FILTER (
            WHERE activity.event_recency <= 10
        ) AS last10_participated,
        COUNT(activity.vote_choice) AS total_ballots,
        COUNT(activity.vote_choice) FILTER (
            WHERE activity.vote_choice = 'agree'
        ) AS agree_ballots
    FROM eligible_accounts account
    LEFT JOIN eligible_activity activity
      ON activity.user_id = account.user_id
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
CREATE INDEX IF NOT EXISTS idx_trvb_user_id ON {TABLE_TOPIC_REMOVAL_VOTE_BALLOTS}(user_id);
CREATE INDEX IF NOT EXISTS idx_competition_registrations_edition_status
    ON {TABLE_COMPETITION_REGISTRATIONS}(competition_edition, status);
CREATE INDEX IF NOT EXISTS idx_match_videos_match_id
    ON {TABLE_MATCH_VIDEOS}(match_id);
CREATE INDEX IF NOT EXISTS idx_match_videos_visible_order
    ON {TABLE_MATCH_VIDEOS}(is_visible, display_order);
CREATE INDEX IF NOT EXISTS idx_video_views_video_id
    ON {TABLE_VIDEO_VIEWS}(video_id);
CREATE INDEX IF NOT EXISTS idx_video_views_user_updated
    ON {TABLE_VIDEO_VIEWS}(user_id, viewed_at DESC);
CREATE INDEX IF NOT EXISTS idx_video_comments_video_created
    ON {TABLE_VIDEO_COMMENTS}(video_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_video_comments_user_created
    ON {TABLE_VIDEO_COMMENTS}(user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_video_votes_video_choice
    ON {TABLE_VIDEO_VOTES}(video_id, vote_choice);
CREATE INDEX IF NOT EXISTS idx_video_progress_user_updated
    ON {TABLE_VIDEO_PROGRESS}(user_id, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_video_roster_member_video
    ON {TABLE_VIDEO_ROSTER}(member_user_id, video_id);
CREATE INDEX IF NOT EXISTS idx_match_photos_album_created
    ON {TABLE_MATCH_PHOTOS}(album_label, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_match_photos_date_created
    ON {TABLE_MATCH_PHOTOS}(photo_date DESC, created_at DESC);
CREATE UNIQUE INDEX IF NOT EXISTS idx_match_photos_r2_key
    ON {TABLE_MATCH_PHOTOS}(r2_key) WHERE r2_key IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_recent_matches_date
    ON {TABLE_RECENT_MATCHES}(match_date DESC, match_time DESC, id DESC);
CREATE INDEX IF NOT EXISTS idx_recent_match_notifications_state
    ON {TABLE_RECENT_MATCH_NOTIFICATIONS}(state, attempted_at);
CREATE INDEX IF NOT EXISTS idx_committee_memberships_user_exit
    ON {TABLE_COMMITTEE_MEMBERSHIPS}(member_user_id, exit_type);
CREATE INDEX IF NOT EXISTS idx_committee_memberships_year
    ON {TABLE_COMMITTEE_MEMBERSHIPS}(joined_academic_year DESC, ended_academic_year DESC);
CREATE INDEX IF NOT EXISTS idx_history_events_timeline
    ON {TABLE_HISTORY_EVENTS}(academic_year_start DESC, event_date DESC, id DESC);
CREATE INDEX IF NOT EXISTS idx_ghost_forum_threads_activity
    ON {TABLE_GHOST_FORUM_THREADS}(last_activity_at DESC, id DESC)
    WHERE deleted_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_ghost_forum_posts_thread_created
    ON {TABLE_GHOST_FORUM_POSTS}(thread_id, created_at, id);
CREATE INDEX IF NOT EXISTS idx_ghost_forum_thread_videos_video
    ON {TABLE_GHOST_FORUM_THREAD_VIDEOS}(video_id);
CREATE INDEX IF NOT EXISTS idx_ghost_forum_thread_history_events_event
    ON {TABLE_GHOST_FORUM_THREAD_HISTORY_EVENTS}(event_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_ghost_forum_first_post
    ON {TABLE_GHOST_FORUM_POSTS}(thread_id) WHERE is_first_post=TRUE;
CREATE INDEX IF NOT EXISTS idx_ghost_forum_threads_title_trgm
    ON {TABLE_GHOST_FORUM_THREADS} USING GIN (LOWER(title) gin_trgm_ops)
    WHERE deleted_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_ghost_forum_posts_body_trgm
    ON {TABLE_GHOST_FORUM_POSTS} USING GIN (LOWER(body) gin_trgm_ops)
    WHERE deleted_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_ghost_forum_state_user
    ON {TABLE_GHOST_FORUM_THREAD_USER_STATE}(user_id, muted, thread_id);
CREATE INDEX IF NOT EXISTS idx_ghost_forum_notifications_state
    ON {TABLE_GHOST_FORUM_NOTIFICATIONS}(state, attempted_at, id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_tts_voice_recordings_r2_key
    ON {TABLE_TTS_VOICE_RECORDINGS}(r2_key) WHERE r2_key IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_tts_voice_recordings_speaker_created
    ON {TABLE_TTS_VOICE_RECORDINGS}(speaker_user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_tts_voice_recordings_status_created
    ON {TABLE_TTS_VOICE_RECORDINGS}(status, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_bandwidth_usage_created
    ON {TABLE_BANDWIDTH_USAGE_LOGS}(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_r2_upload_intents_lifecycle
    ON {TABLE_R2_UPLOAD_INTENTS}(media_kind, user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_projector_ai_sessions_display_updated
    ON {TABLE_PROJECTOR_AI_SESSIONS}(display_key, updated_at DESC);
CREATE UNIQUE INDEX IF NOT EXISTS idx_projector_ai_sessions_one_active_display
    ON {TABLE_PROJECTOR_AI_SESSIONS}(display_key)
    WHERE status IN ('start_requested','recording','stop_requested','processing');
CREATE INDEX IF NOT EXISTS idx_projector_ai_sessions_expiry
    ON {TABLE_PROJECTOR_AI_SESSIONS}(result_expires_at)
    WHERE result_ciphertext IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_projector_ai_markers_session_time
    ON {TABLE_PROJECTOR_AI_MARKERS}(session_id, offset_seconds, id);
CREATE INDEX IF NOT EXISTS idx_projector_kiosk_devices_last_seen
    ON {TABLE_PROJECTOR_KIOSK_DEVICES}(last_seen_at DESC);
CREATE INDEX IF NOT EXISTS idx_tts_scripts_active_category
    ON {TABLE_TTS_SCRIPTS}(is_active, category, sort_order);
CREATE INDEX IF NOT EXISTS idx_tts_lexicon_active
    ON {TABLE_TTS_LEXICON}(is_active, category);
CREATE INDEX IF NOT EXISTS idx_motion_comments_motion
    ON {TABLE_MOTION_COMMENTS}(motion_type, motion_key);
CREATE INDEX IF NOT EXISTS idx_push_subscriptions_user_active
    ON {TABLE_PUSH_SUBSCRIPTIONS}(user_id, is_active);
CREATE INDEX IF NOT EXISTS idx_push_subscriptions_inactive_updated
    ON {TABLE_PUSH_SUBSCRIPTIONS}(updated_at) WHERE is_active=FALSE;
CREATE INDEX IF NOT EXISTS idx_login_records_logged_in_at
    ON {TABLE_LOGIN_RECORDS}(logged_in_at);
CREATE INDEX IF NOT EXISTS idx_notification_reads_read_at
    ON {TABLE_NOTIFICATION_READS}(read_at);
CREATE INDEX IF NOT EXISTS idx_llm_training_status_created
    ON {TABLE_LLM_TRAINING_SUBMISSIONS}(status, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_llm_training_submitter_created
    ON {TABLE_LLM_TRAINING_SUBMISSIONS}(submitted_by, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_monthly_resource_limits_updated
    ON {TABLE_MONTHLY_RESOURCE_LIMITS}(updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_tts_scripts_type_manuscript
    ON {TABLE_TTS_SCRIPTS}(script_type, manuscript_id, sort_order);
CREATE INDEX IF NOT EXISTS idx_ai_training_audit_created_at
    ON {TABLE_AI_TRAINING_AUDIT}(created_at)
    WHERE action NOT IN (
        'consent_granted', 'consent_withdrawn', 'submission_withdrawn',
        'factory_source_created', 'factory_source_withdrawn',
        'factory_item_reviewed', 'factory_item_withdrawn',
        'factory_item_invalidated',
        'factory_topic_tag_approved', 'factory_topic_tag_retired',
        'factory_release_published', 'factory_release_invalidated',
        'factory_transcript_withdrawn'
    );
CREATE INDEX IF NOT EXISTS idx_ai_fund_transactions_status
    ON {TABLE_AI_FUND_TRANSACTIONS}(status);
CREATE INDEX IF NOT EXISTS idx_ai_fund_transactions_created_at
    ON {TABLE_AI_FUND_TRANSACTIONS}(created_at);
CREATE INDEX IF NOT EXISTS idx_ai_fund_usage_logs_created_at
    ON {TABLE_AI_FUND_USAGE_LOGS}(created_at);
CREATE INDEX IF NOT EXISTS idx_ai_fund_usage_logs_user_id
    ON {TABLE_AI_FUND_USAGE_LOGS}(user_id);
CREATE INDEX IF NOT EXISTS idx_ai_fund_usage_logs_operation
    ON {TABLE_AI_FUND_USAGE_LOGS}(operation_id, operation_stage)
    WHERE operation_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS idx_bandwidth_official_bucket
    ON {TABLE_BANDWIDTH_USAGE_LOGS}(official_bucket_id)
    WHERE official_bucket_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_lateness_fund_records_member_user_date
    ON {TABLE_LATENESS_FUND_RECORDS}(member_user_id, late_date);
CREATE INDEX IF NOT EXISTS idx_lateness_fund_expenses_date
    ON {TABLE_LATENESS_FUND_EXPENSES}(expense_date);
CREATE INDEX IF NOT EXISTS idx_bug_reports_status_created
    ON {TABLE_BUG_REPORTS}(status, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_bug_reports_reporter_created
    ON {TABLE_BUG_REPORTS}(reporter_user_id, created_at DESC);
"""

# Final empty-database privilege contract.  Browser roles never receive direct
# catalog access; a deployment may later grant app_backend to one secret login.
LOCK_APPLICATION_PRIVILEGES = """
REVOKE ALL PRIVILEGES ON SCHEMA public FROM PUBLIC;
REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA public FROM PUBLIC;
REVOKE ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public FROM PUBLIC;
REVOKE EXECUTE ON ALL FUNCTIONS IN SCHEMA public FROM PUBLIC;

DO $bootstrap$
DECLARE
    role_name TEXT;
    object_name TEXT;
    object_kind "char";
BEGIN
    FOR role_name IN
        SELECT rolname FROM pg_roles
        WHERE rolname IN ('anon', 'authenticated')
    LOOP
        EXECUTE 'REVOKE ALL PRIVILEGES ON SCHEMA public FROM '
            || quote_ident(role_name);
        EXECUTE 'ALTER DEFAULT PRIVILEGES IN SCHEMA public '
            || 'REVOKE ALL PRIVILEGES ON TABLES FROM '
            || quote_ident(role_name);
        EXECUTE 'ALTER DEFAULT PRIVILEGES IN SCHEMA public '
            || 'REVOKE ALL PRIVILEGES ON SEQUENCES FROM '
            || quote_ident(role_name);
        EXECUTE 'ALTER DEFAULT PRIVILEGES IN SCHEMA public '
            || 'REVOKE EXECUTE ON FUNCTIONS FROM '
            || quote_ident(role_name);

        FOR object_name, object_kind IN
            SELECT c.relname, c.relkind
            FROM pg_class c
            JOIN pg_namespace n ON n.oid=c.relnamespace
            WHERE n.nspname='public'
              AND c.relkind IN ('r','p','v','m','f')
        LOOP
            EXECUTE 'REVOKE ALL PRIVILEGES ON TABLE public.'
                || quote_ident(object_name)
                || ' FROM ' || quote_ident(role_name);
        END LOOP;

        FOR object_name IN
            SELECT c.relname
            FROM pg_class c
            JOIN pg_namespace n ON n.oid=c.relnamespace
            WHERE n.nspname='public' AND c.relkind='S'
        LOOP
            EXECUTE 'REVOKE ALL PRIVILEGES ON SEQUENCE public.'
                || quote_ident(object_name)
                || ' FROM ' || quote_ident(role_name);
        END LOOP;
    END LOOP;

    IF NOT EXISTS (
        SELECT 1 FROM pg_roles WHERE rolname='app_backend'
    ) THEN
        CREATE ROLE app_backend
            NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE
            NOINHERIT NOBYPASSRLS;
    END IF;

    FOR object_name, object_kind IN
        SELECT c.relname, c.relkind
        FROM pg_class c
        JOIN pg_namespace n ON n.oid=c.relnamespace
        WHERE n.nspname='public'
          AND c.relkind IN ('r','p','v','m','f')
          AND c.relname<>'schema_migrations'
    LOOP
        IF object_kind IN ('v','m') THEN
            EXECUTE 'GRANT SELECT ON TABLE public.'
                || quote_ident(object_name) || ' TO app_backend';
        ELSE
            EXECUTE 'GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE public.'
                || quote_ident(object_name) || ' TO app_backend';
        END IF;
    END LOOP;

    FOR object_name IN
        SELECT c.relname
        FROM pg_class c
        JOIN pg_namespace n ON n.oid=c.relnamespace
        WHERE n.nspname='public' AND c.relkind='S'
    LOOP
        EXECUTE 'GRANT USAGE, SELECT ON SEQUENCE public.'
            || quote_ident(object_name) || ' TO app_backend';
    END LOOP;
END
$bootstrap$;

ALTER DEFAULT PRIVILEGES IN SCHEMA public
    REVOKE ALL PRIVILEGES ON TABLES FROM PUBLIC;
ALTER DEFAULT PRIVILEGES IN SCHEMA public
    REVOKE ALL PRIVILEGES ON SEQUENCES FROM PUBLIC;
ALTER DEFAULT PRIVILEGES IN SCHEMA public
    REVOKE EXECUTE ON FUNCTIONS FROM PUBLIC;
ALTER DEFAULT PRIVILEGES IN SCHEMA public
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO app_backend;
ALTER DEFAULT PRIVILEGES IN SCHEMA public
    GRANT USAGE, SELECT ON SEQUENCES TO app_backend;

GRANT USAGE ON SCHEMA public TO app_backend;
"""

# Typed, namespaced application configuration.  ``value`` retains its native
# JSON type and ``is_secret`` lets future RLS/column policies distinguish
# credentials from ordinary settings.
CREATE_APP_CONFIG = f"""
CREATE TABLE IF NOT EXISTS {TABLE_APP_CONFIG} (
    key         TEXT        PRIMARY KEY,
    namespace   TEXT        NOT NULL
        CHECK (namespace IN ('auth', 'runtime', 'access', 'ai', 'finance',
                             'analysis', 'resource', 'migration', 'legacy')),
    value       JSONB       NOT NULL,
    value_type  TEXT        NOT NULL
        CHECK (value_type IN ('string', 'boolean', 'number', 'array', 'object')),
    is_secret   BOOLEAN     NOT NULL DEFAULT FALSE,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT app_config_json_type_matches
        CHECK (jsonb_typeof(value) = value_type)
);
"""

# Ordered list of all CREATE statements (dependency order).
# Tables must be created before any table that references them via FK.
ALL_SCHEMAS = [
    CREATE_PG_TRGM_EXTENSION,  # shared extension used by bounded forum search
    CREATE_ACCOUNTS,            # no deps
    CREATE_MATCHES,             # no deps
    CREATE_TOPICS,              # no deps
    CREATE_DEBATERS,            # → matches
    CREATE_SCORES,              # → matches
    CREATE_DEBATER_SCORES,      # → scores
    CREATE_BEST_DEBATER_RANKINGS,  # → scores
    CREATE_SCORE_DRAFTS,        # → matches
    CREATE_SCORE_SHEET_CONFIRMATIONS,  # → matches
    CREATE_MATCH_TOPIC_RELEASES,       # → matches
    CREATE_TOPIC_VOTES,         # → accounts
    CREATE_TOPIC_VOTE_BALLOTS,  # → topic_votes, accounts
    CREATE_TOPIC_REMOVAL_VOTES,         # → topics, accounts
    CREATE_TOPIC_REMOVAL_VOTE_BALLOTS,  # → topic_removal_votes, accounts
    CREATE_LOGIN_RECORDS,              # → accounts
    CREATE_NOTIFICATION_READS,         # → accounts
    CREATE_PUSH_SUBSCRIPTIONS,         # → accounts
    CREATE_COMPETITION_REGISTRATION_SETTINGS,  # no deps
    CREATE_COMPETITION_REGISTRATIONS,           # no deps
    CREATE_MATCH_VIDEOS,              # → matches
    CREATE_VIDEO_VIEWS,               # → match_videos, accounts
    CREATE_VIDEO_COMMENTS,            # → match_videos, accounts
    CREATE_VIDEO_VOTES,               # → match_videos, accounts
    CREATE_VIDEO_CHAPTERS,            # → match_videos
    CREATE_VIDEO_ROSTER,              # → match_videos, accounts
    LOCK_VIDEO_ROSTER_PRIVILEGES,
    CREATE_VIDEO_PROGRESS,            # → match_videos, accounts
    CREATE_MATCH_PHOTOS,              # → match_videos, accounts
    CREATE_RECENT_MATCHES,
    CREATE_RECENT_MATCH_NOTIFICATIONS,  # → recent_matches
    CREATE_COMPETITION_PREP,           # → recent_matches, accounts
    CREATE_COMMITTEE_MEMBERSHIPS,     # → accounts
    CREATE_HISTORY_EVENTS,
    CREATE_HISTORY_EVENT_MATCHES,     # → history_events, matches
    CREATE_HISTORY_EVENT_PHOTOS,      # → history_events, match_photos
    CREATE_GHOST_FORUM_THREADS,       # → accounts
    CREATE_GHOST_FORUM_POSTS,         # → threads, accounts
    CREATE_GHOST_FORUM_REACTIONS,     # → posts, accounts
    CREATE_GHOST_FORUM_THREAD_VIDEOS,  # → threads, match_videos
    CREATE_GHOST_FORUM_THREAD_PHOTOS,   # → threads, match_photos
    CREATE_GHOST_FORUM_THREAD_HISTORY_EVENTS,  # → threads, history_events
    CREATE_GHOST_FORUM_USER_PROFILES,   # → accounts
    CREATE_GHOST_FORUM_THREAD_USER_STATE,  # → threads, posts, accounts
    CREATE_GHOST_FORUM_NOTIFICATIONS,   # → posts
    LOCK_COMMUNITY_PRIVILEGES,
    CREATE_TTS_VOICE_CONSENTS,        # → accounts
    CREATE_TTS_VOICE_RECORDINGS,      # → accounts
    CREATE_TTS_SCRIPTS,               # → (standalone)
    CREATE_TTS_LEXICON,               # → (standalone)
    CREATE_LLM_TRAINING_SUBMISSIONS,  # → accounts
    CREATE_AI_TRAINING_AUDIT,
    LOCK_AI_TRAINING_AUDIT_PRIVILEGES,
    CREATE_AI_DATA_FACTORY,           # → LLM submissions; internal lineage
    LOCK_AI_DATA_FACTORY_PRIVILEGES,
    CREATE_AI_FACTORY_TRANSCRIPT_WORKFLOW,  # → factory sources; full transcript lineage
    LOCK_AI_FACTORY_TRANSCRIPT_PRIVILEGES,
    CREATE_MATCH_ROSTER_LINKS,        # → matches
    CREATE_MOTION_COMMENTS,           # → accounts
    CREATE_AI_FUND_TRANSACTIONS,      # → accounts
    CREATE_AI_FUND_USAGE_LOGS,        # → accounts
    CREATE_LMC_AI_NODES,              # private local-AI computer registry
    CREATE_WORKSTATION_R2_HEALTH_PROBES,  # bounded direct-R2 health intents
    CREATE_LATENESS_FUND_RECORDS,     # → accounts
    CREATE_LATENESS_FUND_EXPENSES,    # → accounts
    CREATE_LATENESS_FUND_PERIODS,     # no deps
    CREATE_BUG_REPORTS,               # → accounts
    CREATE_BANDWIDTH_USAGE_LOGS,        # → accounts
    CREATE_R2_UPLOAD_INTENTS,           # → accounts
    CREATE_MONTHLY_RESOURCE_LIMITS,      # → accounts
    LOCK_MONTHLY_RESOURCE_LIMITS_PRIVILEGES,
    CREATE_PROJECTOR_STATE,             # short-lived projector state
    CREATE_PROJECTOR_KIOSK_DEVICES,     # stable signed Kiosk device identity
    CREATE_PROJECTOR_AI_SESSIONS,        # encrypted two-hour AI評判易 result
    CREATE_PROJECTOR_AI_CONTROLS,        # cross-device command + ACK state
    CREATE_PROJECTOR_AI_MARKERS,         # server-time projector segment events
    LOCK_PROJECTOR_AI_PRIVILEGES,
    CREATE_OFFICIAL_AI_JUDGE,            # durable official third-judge state
    LOCK_OFFICIAL_AI_JUDGE_PRIVILEGES,
    CREATE_AI_COACH_LIVE_BRIEFS,        # short-lived AI coach state
    CREATE_APP_CONFIG,                  # typed runtime configuration
    CREATE_COMMITTEE_VOTE_ACTIVITY_VIEW, # after all tables
    CREATE_INDICES,                      # after all tables
    LOCK_APPLICATION_PRIVILEGES,          # final database-wide ACL contract
]


class ManagedDatabaseBootstrapError(RuntimeError):
    """Raised when the empty-database bootstrap targets a managed database."""


def _assert_bootstrap_target(conn) -> None:
    managed = conn.execute(text(
        "SELECT to_regclass('public.schema_migrations') IS NOT NULL"
    )).scalar()
    if managed:
        raise ManagedDatabaseBootstrapError(
            "Database已有versioned migration ledger；請使用tools/manage_db_migrations.py，"
            "不可再執行empty-database bootstrap。"
        )


def init_db(conn) -> None:
    """
    Bootstrap all current tables for a new, empty database.

    Parameters
    ----------
    conn : SQLAlchemy connection/session or a small wrapper exposing ``session``.

    Example
    -------
    # With a SQLAlchemy engine:
    from sqlalchemy import create_engine
    engine = create_engine("postgresql://...")
    with engine.connect() as raw_conn:
        init_db(raw_conn)
    """
    # Support a session wrapper or a raw SQLAlchemy connection.
    if hasattr(conn, "session"):
        with conn.session as s:
            _assert_bootstrap_target(s)
            for ddl in ALL_SCHEMAS:
                s.execute(text(ddl))
            from core.ai_training_defaults import seed_default_tts_scripts
            seed_default_tts_scripts(s)
            s.commit()
    else:
        _assert_bootstrap_target(conn)
        for ddl in ALL_SCHEMAS:
            conn.execute(text(ddl))
        from core.ai_training_defaults import seed_default_tts_scripts
        seed_default_tts_scripts(conn)
        conn.commit()
