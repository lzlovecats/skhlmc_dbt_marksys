-- Consolidate management roles, permanently retire the SQL console secret,
-- and provision committee-only match, history and graduate forum data.

SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '60s';

DO $migration$
DECLARE
    ai_accounts JSONB;
    senior_accounts JSONB;
BEGIN
    IF to_regclass('public.app_config') IS NULL THEN
        RAISE EXCEPTION 'app_config must exist before management roles can be consolidated';
    END IF;

    SELECT COALESCE(jsonb_agg(account_name ORDER BY account_name), '[]'::jsonb)
    INTO ai_accounts
    FROM (
        SELECT DISTINCT account_name
        FROM public.app_config config,
             LATERAL jsonb_array_elements_text(
                 CASE WHEN jsonb_typeof(config.value)='array'
                      THEN config.value ELSE '[]'::jsonb END
             ) AS items(account_name)
        WHERE config.key IN ('tts_recording_reviewers', 'ai_fund_treasurers')
          AND BTRIM(account_name) <> ''
    ) existing_ai;

    SELECT COALESCE(jsonb_agg(account_name ORDER BY account_name), '[]'::jsonb)
    INTO senior_accounts
    FROM (
        SELECT DISTINCT account_name
        FROM public.app_config config,
             LATERAL jsonb_array_elements_text(
                 CASE WHEN jsonb_typeof(config.value)='array'
                      THEN config.value ELSE '[]'::jsonb END
             ) AS items(account_name)
        WHERE config.key='lateness_fund_managers'
          AND BTRIM(account_name) <> ''
    ) existing_senior;

    INSERT INTO public.app_config
        (key, namespace, value, value_type, is_secret, updated_at)
    VALUES
        ('ai_managers', 'access', ai_accounts, 'array', FALSE, NOW()),
        ('senior_committee_members', 'access', senior_accounts, 'array', FALSE, NOW())
    ON CONFLICT (key) DO UPDATE SET
        namespace=EXCLUDED.namespace,
        value=EXCLUDED.value,
        value_type=EXCLUDED.value_type,
        is_secret=EXCLUDED.is_secret,
        updated_at=EXCLUDED.updated_at;

    DELETE FROM public.app_config
    WHERE key IN (
        'tts_recording_reviewers', 'ai_fund_treasurers',
        'lateness_fund_managers', 'sql_password'
    );
END
$migration$;

CREATE TABLE public.recent_matches (
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

CREATE TABLE public.recent_match_notifications (
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
        FOREIGN KEY (recent_match_id) REFERENCES public.recent_matches(id)
        ON DELETE CASCADE
);

CREATE TABLE public.committee_memberships (
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
        FOREIGN KEY (member_user_id) REFERENCES public.accounts(user_id)
        ON DELETE SET NULL
);

CREATE TABLE public.history_events (
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

CREATE TABLE public.history_event_matches (
    event_id BIGINT NOT NULL,
    match_id TEXT NOT NULL,
    PRIMARY KEY (event_id, match_id),
    CONSTRAINT fk_history_event_match_event
        FOREIGN KEY (event_id) REFERENCES public.history_events(id) ON DELETE CASCADE,
    CONSTRAINT fk_history_event_match_match
        FOREIGN KEY (match_id) REFERENCES public.matches(match_id) ON DELETE CASCADE
);

CREATE TABLE public.history_event_photos (
    event_id BIGINT NOT NULL,
    photo_id INTEGER NOT NULL,
    PRIMARY KEY (event_id, photo_id),
    CONSTRAINT fk_history_event_photo_event
        FOREIGN KEY (event_id) REFERENCES public.history_events(id) ON DELETE CASCADE,
    CONSTRAINT fk_history_event_photo_photo
        FOREIGN KEY (photo_id) REFERENCES public.match_photos(id) ON DELETE CASCADE
);

CREATE TABLE public.ghost_forum_threads (
    id               BIGSERIAL PRIMARY KEY,
    title            TEXT NOT NULL,
    author_user_id   TEXT NOT NULL,
    revision         INTEGER NOT NULL DEFAULT 1 CHECK (revision >= 1),
    created_at       TIMESTAMP NOT NULL DEFAULT NOW(),
    updated_at       TIMESTAMP NOT NULL DEFAULT NOW(),
    last_activity_at TIMESTAMP NOT NULL DEFAULT NOW(),
    deleted_at       TIMESTAMP,
    CONSTRAINT fk_ghost_forum_thread_author
        FOREIGN KEY (author_user_id) REFERENCES public.accounts(user_id)
        ON DELETE RESTRICT
);

CREATE TABLE public.ghost_forum_posts (
    id               BIGSERIAL PRIMARY KEY,
    thread_id        BIGINT NOT NULL,
    author_user_id   TEXT NOT NULL,
    body             TEXT NOT NULL,
    quoted_post_id   BIGINT,
    is_first_post    BOOLEAN NOT NULL DEFAULT FALSE,
    revision         INTEGER NOT NULL DEFAULT 1 CHECK (revision >= 1),
    created_at       TIMESTAMP NOT NULL DEFAULT NOW(),
    updated_at       TIMESTAMP NOT NULL DEFAULT NOW(),
    deleted_at       TIMESTAMP,
    CONSTRAINT fk_ghost_forum_post_thread
        FOREIGN KEY (thread_id) REFERENCES public.ghost_forum_threads(id)
        ON DELETE CASCADE,
    CONSTRAINT fk_ghost_forum_post_author
        FOREIGN KEY (author_user_id) REFERENCES public.accounts(user_id)
        ON DELETE RESTRICT,
    CONSTRAINT fk_ghost_forum_post_quote
        FOREIGN KEY (quoted_post_id) REFERENCES public.ghost_forum_posts(id)
        ON DELETE SET NULL
);

CREATE TABLE public.ghost_forum_reactions (
    post_id    BIGINT NOT NULL,
    user_id    TEXT NOT NULL,
    created_at TIMESTAMP NOT NULL DEFAULT NOW(),
    PRIMARY KEY (post_id, user_id),
    CONSTRAINT fk_ghost_forum_reaction_post
        FOREIGN KEY (post_id) REFERENCES public.ghost_forum_posts(id) ON DELETE CASCADE,
    CONSTRAINT fk_ghost_forum_reaction_user
        FOREIGN KEY (user_id) REFERENCES public.accounts(user_id) ON DELETE CASCADE
);

CREATE TABLE public.ghost_forum_thread_matches (
    thread_id BIGINT NOT NULL,
    match_id  TEXT NOT NULL,
    PRIMARY KEY (thread_id, match_id),
    CONSTRAINT fk_ghost_thread_match_thread
        FOREIGN KEY (thread_id) REFERENCES public.ghost_forum_threads(id) ON DELETE CASCADE,
    CONSTRAINT fk_ghost_thread_match_match
        FOREIGN KEY (match_id) REFERENCES public.matches(match_id) ON DELETE CASCADE
);

CREATE TABLE public.ghost_forum_thread_photos (
    thread_id BIGINT NOT NULL,
    photo_id  INTEGER NOT NULL,
    PRIMARY KEY (thread_id, photo_id),
    CONSTRAINT fk_ghost_thread_photo_thread
        FOREIGN KEY (thread_id) REFERENCES public.ghost_forum_threads(id) ON DELETE CASCADE,
    CONSTRAINT fk_ghost_thread_photo_photo
        FOREIGN KEY (photo_id) REFERENCES public.match_photos(id) ON DELETE CASCADE
);

CREATE INDEX idx_recent_matches_date
    ON public.recent_matches(match_date DESC, match_time DESC, id DESC);
CREATE INDEX idx_recent_match_notifications_state
    ON public.recent_match_notifications(state, attempted_at);
CREATE INDEX idx_committee_memberships_user_exit
    ON public.committee_memberships(member_user_id, exit_type);
CREATE INDEX idx_committee_memberships_year
    ON public.committee_memberships(joined_academic_year DESC, ended_academic_year DESC);
CREATE INDEX idx_history_events_timeline
    ON public.history_events(academic_year_start DESC, event_date DESC, id DESC);
CREATE INDEX idx_ghost_forum_threads_activity
    ON public.ghost_forum_threads(last_activity_at DESC, id DESC)
    WHERE deleted_at IS NULL;
CREATE INDEX idx_ghost_forum_posts_thread_created
    ON public.ghost_forum_posts(thread_id, created_at, id);
CREATE UNIQUE INDEX idx_ghost_forum_first_post
    ON public.ghost_forum_posts(thread_id) WHERE is_first_post=TRUE;

REVOKE ALL PRIVILEGES ON TABLE public.recent_matches FROM PUBLIC;
DO $$
DECLARE role_name TEXT;
BEGIN
    FOR role_name IN SELECT rolname FROM pg_roles
        WHERE rolname IN ('anon', 'authenticated')
    LOOP
        EXECUTE 'REVOKE ALL PRIVILEGES ON TABLE public.recent_matches FROM '
            || quote_ident(role_name);
    END LOOP;
END $$;

REVOKE ALL PRIVILEGES ON TABLE public.recent_match_notifications FROM PUBLIC;
DO $$
DECLARE role_name TEXT;
BEGIN
    FOR role_name IN SELECT rolname FROM pg_roles
        WHERE rolname IN ('anon', 'authenticated')
    LOOP
        EXECUTE 'REVOKE ALL PRIVILEGES ON TABLE public.recent_match_notifications FROM '
            || quote_ident(role_name);
    END LOOP;
END $$;

REVOKE ALL PRIVILEGES ON TABLE public.committee_memberships FROM PUBLIC;
DO $$
DECLARE role_name TEXT;
BEGIN
    FOR role_name IN SELECT rolname FROM pg_roles
        WHERE rolname IN ('anon', 'authenticated')
    LOOP
        EXECUTE 'REVOKE ALL PRIVILEGES ON TABLE public.committee_memberships FROM '
            || quote_ident(role_name);
    END LOOP;
END $$;

REVOKE ALL PRIVILEGES ON TABLE public.history_events FROM PUBLIC;
DO $$
DECLARE role_name TEXT;
BEGIN
    FOR role_name IN SELECT rolname FROM pg_roles
        WHERE rolname IN ('anon', 'authenticated')
    LOOP
        EXECUTE 'REVOKE ALL PRIVILEGES ON TABLE public.history_events FROM '
            || quote_ident(role_name);
    END LOOP;
END $$;

REVOKE ALL PRIVILEGES ON TABLE public.history_event_matches FROM PUBLIC;
DO $$
DECLARE role_name TEXT;
BEGIN
    FOR role_name IN SELECT rolname FROM pg_roles
        WHERE rolname IN ('anon', 'authenticated')
    LOOP
        EXECUTE 'REVOKE ALL PRIVILEGES ON TABLE public.history_event_matches FROM '
            || quote_ident(role_name);
    END LOOP;
END $$;

REVOKE ALL PRIVILEGES ON TABLE public.history_event_photos FROM PUBLIC;
DO $$
DECLARE role_name TEXT;
BEGIN
    FOR role_name IN SELECT rolname FROM pg_roles
        WHERE rolname IN ('anon', 'authenticated')
    LOOP
        EXECUTE 'REVOKE ALL PRIVILEGES ON TABLE public.history_event_photos FROM '
            || quote_ident(role_name);
    END LOOP;
END $$;

REVOKE ALL PRIVILEGES ON TABLE public.ghost_forum_threads FROM PUBLIC;
DO $$
DECLARE role_name TEXT;
BEGIN
    FOR role_name IN SELECT rolname FROM pg_roles
        WHERE rolname IN ('anon', 'authenticated')
    LOOP
        EXECUTE 'REVOKE ALL PRIVILEGES ON TABLE public.ghost_forum_threads FROM '
            || quote_ident(role_name);
    END LOOP;
END $$;

REVOKE ALL PRIVILEGES ON TABLE public.ghost_forum_posts FROM PUBLIC;
DO $$
DECLARE role_name TEXT;
BEGIN
    FOR role_name IN SELECT rolname FROM pg_roles
        WHERE rolname IN ('anon', 'authenticated')
    LOOP
        EXECUTE 'REVOKE ALL PRIVILEGES ON TABLE public.ghost_forum_posts FROM '
            || quote_ident(role_name);
    END LOOP;
END $$;

REVOKE ALL PRIVILEGES ON TABLE public.ghost_forum_reactions FROM PUBLIC;
DO $$
DECLARE role_name TEXT;
BEGIN
    FOR role_name IN SELECT rolname FROM pg_roles
        WHERE rolname IN ('anon', 'authenticated')
    LOOP
        EXECUTE 'REVOKE ALL PRIVILEGES ON TABLE public.ghost_forum_reactions FROM '
            || quote_ident(role_name);
    END LOOP;
END $$;

REVOKE ALL PRIVILEGES ON TABLE public.ghost_forum_thread_matches FROM PUBLIC;
DO $$
DECLARE role_name TEXT;
BEGIN
    FOR role_name IN SELECT rolname FROM pg_roles
        WHERE rolname IN ('anon', 'authenticated')
    LOOP
        EXECUTE 'REVOKE ALL PRIVILEGES ON TABLE public.ghost_forum_thread_matches FROM '
            || quote_ident(role_name);
    END LOOP;
END $$;

REVOKE ALL PRIVILEGES ON TABLE public.ghost_forum_thread_photos FROM PUBLIC;
DO $$
DECLARE role_name TEXT;
BEGIN
    FOR role_name IN SELECT rolname FROM pg_roles
        WHERE rolname IN ('anon', 'authenticated')
    LOOP
        EXECUTE 'REVOKE ALL PRIVILEGES ON TABLE public.ghost_forum_thread_photos FROM '
            || quote_ident(role_name);
    END LOOP;
END $$;

REVOKE ALL PRIVILEGES ON SEQUENCE
    public.recent_matches_id_seq, public.recent_match_notifications_id_seq,
    public.committee_memberships_id_seq, public.history_events_id_seq,
    public.ghost_forum_threads_id_seq, public.ghost_forum_posts_id_seq
FROM PUBLIC;

DO $privileges$
DECLARE role_name TEXT;
BEGIN
    FOR role_name IN
        SELECT rolname FROM pg_roles WHERE rolname IN ('anon', 'authenticated')
    LOOP
        EXECUTE
            'REVOKE ALL PRIVILEGES ON TABLE public.recent_matches, '
            'public.recent_match_notifications, public.committee_memberships, '
            'public.history_events, public.history_event_matches, '
            'public.history_event_photos, public.ghost_forum_threads, '
            'public.ghost_forum_posts, public.ghost_forum_reactions, '
            'public.ghost_forum_thread_matches, public.ghost_forum_thread_photos FROM '
            || quote_ident(role_name);
        EXECUTE
            'REVOKE ALL PRIVILEGES ON SEQUENCE public.recent_matches_id_seq, '
            'public.recent_match_notifications_id_seq, public.committee_memberships_id_seq, '
            'public.history_events_id_seq, public.ghost_forum_threads_id_seq, '
            'public.ghost_forum_posts_id_seq FROM '
            || quote_ident(role_name);
    END LOOP;
END
$privileges$;
