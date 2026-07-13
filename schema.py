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
TABLE_PUSH_SUBSCRIPTIONS = "push_subscriptions"
TABLE_COMPETITION_REGISTRATION_SETTINGS = "competition_registration_settings"
TABLE_COMPETITION_REGISTRATIONS = "competition_registrations"
TABLE_MATCH_VIDEOS = "match_videos"
TABLE_VIDEO_VIEWS = "video_views"
TABLE_VIDEO_COMMENTS = "video_comments"
TABLE_VIDEO_VOTES = "video_votes"
TABLE_VIDEO_CHAPTERS = "video_chapters"
TABLE_VIDEO_PROGRESS = "video_progress"
TABLE_MATCH_PHOTOS = "match_photos"
TABLE_TTS_VOICE_CONSENTS = "tts_voice_consents"
TABLE_TTS_VOICE_RECORDINGS = "tts_voice_recordings"
TABLE_TTS_SCRIPTS = "tts_scripts"
TABLE_TTS_LEXICON = "tts_lexicon"
TABLE_LLM_TRAINING_SUBMISSIONS = "llm_training_submissions"
TABLE_AI_DATASET_SNAPSHOTS = "ai_dataset_snapshots"
TABLE_AI_DATASET_SNAPSHOT_ITEMS = "ai_dataset_snapshot_items"
TABLE_AI_MODEL_VERSIONS = "ai_model_versions"
TABLE_AI_EVAL_CASES = "ai_eval_cases"
TABLE_AI_EVAL_RUNS = "ai_eval_runs"
TABLE_RAG_DOCUMENTS = "rag_documents"
TABLE_RAG_CHUNKS = "rag_chunks"
TABLE_AI_TRAINING_AUDIT = "ai_training_audit"
TABLE_MATCH_ROSTER_LINKS = "match_roster_links"
TABLE_BEST_DEBATER_RANKINGS = "best_debater_rankings"
TABLE_MOTION_COMMENTS = "motion_comments"
TABLE_AI_FUND_TRANSACTIONS = "ai_fund_transactions"
TABLE_AI_FUND_USAGE_LOGS = "ai_fund_usage_logs"
TABLE_LATENESS_FUND_RECORDS = "lateness_fund_records"
TABLE_LATENESS_FUND_EXPENSES = "lateness_fund_expenses"
TABLE_LATENESS_FUND_PERIODS = "lateness_fund_periods"
TABLE_BUG_REPORTS = "bug_reports"
TABLE_PRACTICE_DAILY_USAGE = "practice_daily_usage"
TABLE_BANDWIDTH_USAGE_LOGS = "bandwidth_usage_logs"
TABLE_R2_UPLOAD_INTENTS = "r2_upload_intents"
TABLE_PROJECTOR_STATE = "projector_state"
TABLE_AI_COACH_LIVE_BRIEFS = "ai_coach_live_briefs"
TABLE_AI_COACH_PREPARE_USAGE = "ai_coach_prepare_usage"
TABLE_APP_CONFIG = "app_config"
VIEW_COMMITTEE_VOTE_ACTIVITY = "committee_vote_activity_view"


# Table: ACCOUNTS
# Committee member accounts.
# account_status: 'admin' | 'active' | 'inactive'
# password_hash stores bcrypt hashes. Use core.auth_logic.hash_password when creating/updating accounts.
# Legacy plaintext passwords are still accepted at login (see _verify_password) until migrated.
CREATE_ACCOUNTS = f"""
CREATE TABLE IF NOT EXISTS {TABLE_ACCOUNTS} (
    user_id             TEXT    PRIMARY KEY,
    password_hash       TEXT,
    account_status      TEXT    DEFAULT 'inactive',
    active_since        DATE    DEFAULT CURRENT_DATE,
    last_login_at       TIMESTAMP,
    account_disabled    BOOLEAN DEFAULT FALSE
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
    submitted_time           TIME,
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

# Table: BEST_DEBATER_RANKINGS
# Explicit best-debater rankings given by each judge (1 = best, 8 = worst).
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
    match_id        TEXT,
    judge_name      TEXT,
    side            TEXT,
    score_payload   JSONB,
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
    created_at      TIMESTAMP   DEFAULT NOW(),
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
    updated_at      TIMESTAMP   DEFAULT NOW(),
    PRIMARY KEY (video_id, chapter_label),
    CONSTRAINT fk_video_chapters_video
        FOREIGN KEY (video_id) REFERENCES {TABLE_MATCH_VIDEOS}(id)
        ON DELETE CASCADE
);
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

# One consumed slot per committee member, practice category and Hong Kong day.
# room_code makes reconnecting to the same room idempotent while preventing a
# second room of the same category that day.
CREATE_PRACTICE_DAILY_USAGE = f"""
CREATE TABLE IF NOT EXISTS {TABLE_PRACTICE_DAILY_USAGE} (
    user_id       TEXT,
    practice_kind TEXT CHECK (practice_kind IN ('multiplayer_free', 'multiplayer_mock')),
    usage_date    DATE,
    room_code     TEXT NOT NULL,
    created_at    TIMESTAMP DEFAULT NOW(),
    PRIMARY KEY (user_id, practice_kind, usage_date),
    CONSTRAINT fk_practice_daily_usage_user
        FOREIGN KEY (user_id) REFERENCES {TABLE_ACCOUNTS}(user_id)
        ON DELETE CASCADE
);
"""

CREATE_BANDWIDTH_USAGE_LOGS = f"""
CREATE TABLE IF NOT EXISTS {TABLE_BANDWIDTH_USAGE_LOGS} (
    id          BIGSERIAL PRIMARY KEY,
    source      TEXT NOT NULL,
    user_id     TEXT,
    bytes_out   BIGINT NOT NULL CHECK (bytes_out >= 0),
    details     TEXT,
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
    object_keys     TEXT NOT NULL,
    declared_bytes  BIGINT NOT NULL CHECK (declared_bytes > 0),
    status          TEXT NOT NULL DEFAULT 'issued',
    created_at      TIMESTAMP NOT NULL DEFAULT NOW(),
    completed_at    TIMESTAMP,
    CONSTRAINT fk_r2_upload_intent_user
        FOREIGN KEY (user_id) REFERENCES {TABLE_ACCOUNTS}(user_id)
        ON DELETE CASCADE
);
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

CREATE_AI_COACH_LIVE_BRIEFS = f"""
CREATE TABLE IF NOT EXISTS {TABLE_AI_COACH_LIVE_BRIEFS} (
    brief_id   TEXT PRIMARY KEY,
    user_id    TEXT NOT NULL,
    brief      TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""

CREATE_AI_COACH_PREPARE_USAGE = f"""
CREATE TABLE IF NOT EXISTS {TABLE_AI_COACH_PREPARE_USAGE} (
    id         BIGSERIAL PRIMARY KEY,
    user_id    TEXT NOT NULL,
    created_at TIMESTAMP NOT NULL,
    CONSTRAINT fk_ai_coach_prepare_usage_user
        FOREIGN KEY (user_id) REFERENCES {TABLE_ACCOUNTS}(user_id)
        ON DELETE CASCADE
);
"""

CREATE_AI_COACH_PREPARE_USAGE_INDEX = f"""
CREATE INDEX IF NOT EXISTS idx_ai_coach_prepare_usage_user_created
    ON {TABLE_AI_COACH_PREPARE_USAGE}(user_id, created_at DESC);
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

# Reproducible AI datasets, model releases, evaluation and RAG governance.
# The pgvector column is added by an isolated migration below so a database
# without permission to enable extensions can still run the rest of the app.
CREATE_AI_DATASET_SNAPSHOTS = f"""
CREATE TABLE IF NOT EXISTS {TABLE_AI_DATASET_SNAPSHOTS} (
    snapshot_id       TEXT PRIMARY KEY,
    dataset_kind      TEXT NOT NULL CHECK (dataset_kind IN ('tts', 'llm')),
    speaker_user_id   TEXT,
    consent_version   TEXT,
    item_count        INTEGER NOT NULL DEFAULT 0,
    total_seconds     NUMERIC NOT NULL DEFAULT 0,
    manifest_sha256   TEXT NOT NULL,
    manifest_json     JSONB NOT NULL DEFAULT '{{}}'::jsonb,
    status            TEXT NOT NULL DEFAULT 'ready'
        CHECK (status IN ('draft', 'ready', 'withdrawn', 'archived')),
    created_by        TEXT,
    created_at        TIMESTAMP DEFAULT NOW()
);
"""

CREATE_AI_DATASET_SNAPSHOT_ITEMS = f"""
CREATE TABLE IF NOT EXISTS {TABLE_AI_DATASET_SNAPSHOT_ITEMS} (
    snapshot_id       TEXT NOT NULL REFERENCES {TABLE_AI_DATASET_SNAPSHOTS}(snapshot_id) ON DELETE CASCADE,
    source_table      TEXT NOT NULL,
    source_id         TEXT NOT NULL,
    source_sha256     TEXT,
    consent_version   TEXT,
    split_name        TEXT CHECK (split_name IN ('train', 'validation', 'test')),
    metadata_json     JSONB NOT NULL DEFAULT '{{}}'::jsonb,
    PRIMARY KEY (snapshot_id, source_table, source_id)
);
"""

CREATE_AI_MODEL_VERSIONS = f"""
CREATE TABLE IF NOT EXISTS {TABLE_AI_MODEL_VERSIONS} (
    model_id           TEXT PRIMARY KEY,
    model_type         TEXT NOT NULL CHECK (model_type IN ('tts', 'llm', 'embedding')),
    base_model         TEXT NOT NULL,
    dataset_snapshot_id TEXT REFERENCES {TABLE_AI_DATASET_SNAPSHOTS}(snapshot_id),
    artifact_uri       TEXT,
    status             TEXT NOT NULL DEFAULT 'research'
        CHECK (status IN ('research', 'candidate', 'deployable', 'retired', 'blocked')),
    config_json        JSONB NOT NULL DEFAULT '{{}}'::jsonb,
    metrics_json       JSONB NOT NULL DEFAULT '{{}}'::jsonb,
    created_by         TEXT,
    created_at         TIMESTAMP DEFAULT NOW(),
    updated_at         TIMESTAMP DEFAULT NOW()
);
"""

CREATE_AI_EVAL_CASES = f"""
CREATE TABLE IF NOT EXISTS {TABLE_AI_EVAL_CASES} (
    case_id          TEXT PRIMARY KEY,
    task_type        TEXT NOT NULL,
    title            TEXT NOT NULL,
    input_json       JSONB NOT NULL,
    rubric_json      JSONB NOT NULL,
    reference_text   TEXT,
    is_active        BOOLEAN DEFAULT TRUE,
    created_at       TIMESTAMP DEFAULT NOW()
);
"""

CREATE_AI_EVAL_RUNS = f"""
CREATE TABLE IF NOT EXISTS {TABLE_AI_EVAL_RUNS} (
    id               BIGSERIAL PRIMARY KEY,
    case_id          TEXT NOT NULL REFERENCES {TABLE_AI_EVAL_CASES}(case_id),
    model_label      TEXT NOT NULL,
    pipeline_version TEXT NOT NULL,
    output_text      TEXT NOT NULL,
    score_json       JSONB NOT NULL DEFAULT '{{}}'::jsonb,
    usage_json       JSONB NOT NULL DEFAULT '{{}}'::jsonb,
    created_by       TEXT,
    created_at       TIMESTAMP DEFAULT NOW()
);
"""

CREATE_RAG_DOCUMENTS = f"""
CREATE TABLE IF NOT EXISTS {TABLE_RAG_DOCUMENTS} (
    document_id       TEXT PRIMARY KEY,
    submission_id     INTEGER REFERENCES {TABLE_LLM_TRAINING_SUBMISSIONS}(id) ON DELETE CASCADE,
    title             TEXT,
    data_type         TEXT,
    topic_text        TEXT,
    side              TEXT,
    source_note       TEXT,
    content_sha256    TEXT NOT NULL,
    status            TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'withdrawn', 'archived')),
    embedding_model   TEXT NOT NULL,
    embedding_version TEXT NOT NULL,
    indexed_at        TIMESTAMP DEFAULT NOW()
);
"""

CREATE_RAG_CHUNKS = f"""
CREATE TABLE IF NOT EXISTS {TABLE_RAG_CHUNKS} (
    chunk_id          TEXT PRIMARY KEY,
    document_id       TEXT NOT NULL REFERENCES {TABLE_RAG_DOCUMENTS}(document_id) ON DELETE CASCADE,
    chunk_index       INTEGER NOT NULL,
    content_text      TEXT NOT NULL,
    token_estimate    INTEGER NOT NULL DEFAULT 0,
    embedding_model   TEXT NOT NULL,
    embedding_version TEXT NOT NULL,
    embedding_json    JSONB,
    metadata_json     JSONB NOT NULL DEFAULT '{{}}'::jsonb,
    created_at        TIMESTAMP DEFAULT NOW(),
    UNIQUE (document_id, chunk_index)
);
"""

CREATE_AI_TRAINING_AUDIT = f"""
CREATE TABLE IF NOT EXISTS {TABLE_AI_TRAINING_AUDIT} (
    id             BIGSERIAL PRIMARY KEY,
    actor_user_id  TEXT,
    action         TEXT NOT NULL,
    target_type    TEXT NOT NULL,
    target_id      TEXT,
    details_json   JSONB NOT NULL DEFAULT '{{}}'::jsonb,
    created_at     TIMESTAMP DEFAULT NOW()
);
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
    created_at      TIMESTAMP   DEFAULT NOW(),
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
    transaction_type    TEXT        NOT NULL
                                CHECK (transaction_type IN ('member_deposit', 'provider_topup', 'provider_refund', 'member_refund', 'adjustment')),
    status              TEXT        DEFAULT 'pending'
                                CHECK (status IN ('pending', 'confirmed', 'rejected')),
    provider            TEXT,
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
CREATE_AI_FUND_USAGE_LOGS = f"""
CREATE TABLE IF NOT EXISTS {TABLE_AI_FUND_USAGE_LOGS} (
    id                  SERIAL      PRIMARY KEY,
    user_id             TEXT,
    feature             TEXT        NOT NULL
                                CHECK (feature IN ('speech_review', 'strategy', 'web_research', 'fact_check', 'free_debate_live', 'full_mock_live', 'vote_review', 'vote_analysis', 'vote_discussion', 'tts_review', 'tts_script_analysis', 'llm_review')),
    model_label         TEXT        NOT NULL,
    provider            TEXT,
    estimated_cost_usd  NUMERIC(12, 6) DEFAULT 0,
    estimated_cost_hkd  NUMERIC(10, 4) DEFAULT 0,
    input_tokens        INTEGER     DEFAULT 0,
    output_tokens       INTEGER     DEFAULT 0,
    audio_tokens        INTEGER     DEFAULT 0,
    search_calls        INTEGER     DEFAULT 0,
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
CREATE_COMMITTEE_VOTE_ACTIVITY_VIEW = f"""
DROP VIEW IF EXISTS {VIEW_COMMITTEE_VOTE_ACTIVITY};
CREATE VIEW {VIEW_COMMITTEE_VOTE_ACTIVITY} AS
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
        a.account_status,
        (
            SELECT COUNT(*) FROM all_events ae
            WHERE a.active_since IS NULL
               OR ae.created_at::date >= a.active_since
        ) AS total_votes,
        (
            SELECT COUNT(*) FROM all_events ae
            WHERE (a.active_since IS NULL OR ae.created_at::date >= a.active_since)
              AND (
                  (
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
CREATE INDEX IF NOT EXISTS idx_match_photos_album_created
    ON {TABLE_MATCH_PHOTOS}(album_label, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_match_photos_date_created
    ON {TABLE_MATCH_PHOTOS}(photo_date DESC, created_at DESC);
CREATE UNIQUE INDEX IF NOT EXISTS idx_match_photos_r2_key
    ON {TABLE_MATCH_PHOTOS}(r2_key) WHERE r2_key IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS idx_tts_voice_recordings_r2_key
    ON {TABLE_TTS_VOICE_RECORDINGS}(r2_key) WHERE r2_key IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_tts_voice_recordings_speaker_created
    ON {TABLE_TTS_VOICE_RECORDINGS}(speaker_user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_tts_voice_recordings_status_created
    ON {TABLE_TTS_VOICE_RECORDINGS}(status, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_bandwidth_usage_created
    ON {TABLE_BANDWIDTH_USAGE_LOGS}(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_r2_upload_intents_quota
    ON {TABLE_R2_UPLOAD_INTENTS}(media_kind, user_id, created_at DESC);
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
CREATE INDEX IF NOT EXISTS idx_ai_training_audit_created_at
    ON {TABLE_AI_TRAINING_AUDIT}(created_at);
CREATE INDEX IF NOT EXISTS idx_ai_fund_transactions_status
    ON {TABLE_AI_FUND_TRANSACTIONS}(status);
CREATE INDEX IF NOT EXISTS idx_ai_fund_transactions_created_at
    ON {TABLE_AI_FUND_TRANSACTIONS}(created_at);
CREATE INDEX IF NOT EXISTS idx_ai_fund_usage_logs_created_at
    ON {TABLE_AI_FUND_USAGE_LOGS}(created_at);
CREATE INDEX IF NOT EXISTS idx_ai_fund_usage_logs_user_id
    ON {TABLE_AI_FUND_USAGE_LOGS}(user_id);
CREATE INDEX IF NOT EXISTS idx_lateness_fund_records_member_user_date
    ON {TABLE_LATENESS_FUND_RECORDS}(member_user_id, late_date);
CREATE INDEX IF NOT EXISTS idx_lateness_fund_expenses_date
    ON {TABLE_LATENESS_FUND_EXPENSES}(expense_date);
CREATE INDEX IF NOT EXISTS idx_bug_reports_status_created
    ON {TABLE_BUG_REPORTS}(status, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_bug_reports_reporter_created
    ON {TABLE_BUG_REPORTS}(reporter_user_id, created_at DESC);
"""

# Typed, namespaced application configuration.  ``value`` retains its native
# JSON type and ``is_secret`` lets future RLS/column policies distinguish
# credentials from ordinary settings.  The old system_config table remains for
# one rollback window only; active code writes app_config.
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

# Legacy rollback bridge.  New code must not write this table.
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
    CREATE_BEST_DEBATER_RANKINGS,  # → scores
    CREATE_SCORE_DRAFTS,        # → matches
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
    CREATE_VIDEO_PROGRESS,            # → match_videos, accounts
    CREATE_MATCH_PHOTOS,              # → match_videos, accounts
    CREATE_TTS_VOICE_CONSENTS,        # → accounts
    CREATE_TTS_VOICE_RECORDINGS,      # → accounts
    CREATE_TTS_SCRIPTS,               # → (standalone)
    CREATE_TTS_LEXICON,               # → (standalone)
    CREATE_LLM_TRAINING_SUBMISSIONS,  # → accounts
    CREATE_AI_DATASET_SNAPSHOTS,
    CREATE_AI_DATASET_SNAPSHOT_ITEMS,
    CREATE_AI_MODEL_VERSIONS,
    CREATE_AI_EVAL_CASES,
    CREATE_AI_EVAL_RUNS,
    CREATE_RAG_DOCUMENTS,
    CREATE_RAG_CHUNKS,
    CREATE_AI_TRAINING_AUDIT,
    CREATE_MATCH_ROSTER_LINKS,        # → matches
    CREATE_MOTION_COMMENTS,           # → accounts
    CREATE_AI_FUND_TRANSACTIONS,      # → accounts
    CREATE_AI_FUND_USAGE_LOGS,        # → accounts
    CREATE_LATENESS_FUND_RECORDS,     # → accounts
    CREATE_LATENESS_FUND_EXPENSES,    # → accounts
    CREATE_LATENESS_FUND_PERIODS,     # no deps
    CREATE_BUG_REPORTS,               # → accounts
    CREATE_PRACTICE_DAILY_USAGE,       # → accounts
    CREATE_BANDWIDTH_USAGE_LOGS,        # → accounts
    CREATE_R2_UPLOAD_INTENTS,           # → accounts
    CREATE_PROJECTOR_STATE,             # short-lived projector state
    CREATE_AI_COACH_LIVE_BRIEFS,        # short-lived AI coach state
    CREATE_AI_COACH_PREPARE_USAGE,      # AI coach quota ledger
    CREATE_AI_COACH_PREPARE_USAGE_INDEX,
    CREATE_APP_CONFIG,                  # typed runtime configuration
    CREATE_SYSTEM_CONFIG,                # no deps
    CREATE_COMMITTEE_VOTE_ACTIVITY_VIEW, # after all tables
    CREATE_INDICES,                      # after all tables
]


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
            for ddl in ALL_SCHEMAS:
                s.execute(text(ddl))
            s.commit()
    else:
        for ddl in ALL_SCHEMAS:
            conn.execute(text(ddl))
        conn.commit()
