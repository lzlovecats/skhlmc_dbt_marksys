-- Add scalable search, first-visit unread baselines, per-thread read/mute state,
-- and a durable author-retryable notification outbox for the graduate forum.

SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '60s';

CREATE EXTENSION IF NOT EXISTS pg_trgm;

CREATE TABLE public.ghost_forum_user_profiles (
    user_id      TEXT PRIMARY KEY,
    unread_since TIMESTAMP NOT NULL,
    created_at   TIMESTAMP NOT NULL DEFAULT NOW(),
    updated_at   TIMESTAMP NOT NULL DEFAULT NOW(),
    CONSTRAINT fk_ghost_forum_profile_user
        FOREIGN KEY (user_id) REFERENCES public.accounts(user_id) ON DELETE CASCADE
);

CREATE TABLE public.ghost_forum_thread_user_state (
    thread_id         BIGINT NOT NULL,
    user_id           TEXT NOT NULL,
    last_read_post_id BIGINT,
    muted             BOOLEAN NOT NULL DEFAULT FALSE,
    updated_at        TIMESTAMP NOT NULL DEFAULT NOW(),
    PRIMARY KEY (thread_id, user_id),
    CONSTRAINT fk_ghost_forum_state_thread
        FOREIGN KEY (thread_id) REFERENCES public.ghost_forum_threads(id) ON DELETE CASCADE,
    CONSTRAINT fk_ghost_forum_state_user
        FOREIGN KEY (user_id) REFERENCES public.accounts(user_id) ON DELETE CASCADE,
    CONSTRAINT fk_ghost_forum_state_post
        FOREIGN KEY (last_read_post_id) REFERENCES public.ghost_forum_posts(id) ON DELETE SET NULL
);

CREATE TABLE public.ghost_forum_notifications (
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
        FOREIGN KEY (post_id) REFERENCES public.ghost_forum_posts(id) ON DELETE CASCADE
);

CREATE INDEX idx_ghost_forum_threads_title_trgm
    ON public.ghost_forum_threads USING GIN (LOWER(title) gin_trgm_ops)
    WHERE deleted_at IS NULL;
CREATE INDEX idx_ghost_forum_posts_body_trgm
    ON public.ghost_forum_posts USING GIN (LOWER(body) gin_trgm_ops)
    WHERE deleted_at IS NULL;
CREATE INDEX idx_ghost_forum_state_user
    ON public.ghost_forum_thread_user_state(user_id, muted, thread_id);
CREATE INDEX idx_ghost_forum_notifications_state
    ON public.ghost_forum_notifications(state, attempted_at, id);

REVOKE ALL PRIVILEGES ON TABLE public.ghost_forum_user_profiles FROM PUBLIC;
DO $$
DECLARE role_name TEXT;
BEGIN
    FOR role_name IN SELECT rolname FROM pg_roles
        WHERE rolname IN ('anon', 'authenticated')
    LOOP
        EXECUTE 'REVOKE ALL PRIVILEGES ON TABLE public.ghost_forum_user_profiles FROM '
            || quote_ident(role_name);
    END LOOP;
END $$;

REVOKE ALL PRIVILEGES ON TABLE public.ghost_forum_thread_user_state FROM PUBLIC;
DO $$
DECLARE role_name TEXT;
BEGIN
    FOR role_name IN SELECT rolname FROM pg_roles
        WHERE rolname IN ('anon', 'authenticated')
    LOOP
        EXECUTE 'REVOKE ALL PRIVILEGES ON TABLE public.ghost_forum_thread_user_state FROM '
            || quote_ident(role_name);
    END LOOP;
END $$;

REVOKE ALL PRIVILEGES ON TABLE public.ghost_forum_notifications FROM PUBLIC;
DO $$
DECLARE role_name TEXT;
BEGIN
    FOR role_name IN SELECT rolname FROM pg_roles
        WHERE rolname IN ('anon', 'authenticated')
    LOOP
        EXECUTE 'REVOKE ALL PRIVILEGES ON TABLE public.ghost_forum_notifications FROM '
            || quote_ident(role_name);
    END LOOP;
END $$;

REVOKE ALL PRIVILEGES ON SEQUENCE
    public.ghost_forum_notifications_id_seq
FROM PUBLIC;

DO $$
DECLARE role_name TEXT;
BEGIN
    FOR role_name IN
        SELECT rolname FROM pg_roles WHERE rolname IN ('anon', 'authenticated')
    LOOP
        EXECUTE
            'REVOKE ALL PRIVILEGES ON SEQUENCE public.ghost_forum_notifications_id_seq FROM '
            || quote_ident(role_name);
    END LOOP;
END
$$;
