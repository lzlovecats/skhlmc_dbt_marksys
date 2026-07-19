-- Add audited three-topic release, private team links, and side-scoped vetoes.

SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '30s';

CREATE TABLE public.match_topic_releases (
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
        FOREIGN KEY (match_id) REFERENCES public.matches(match_id)
        ON DELETE CASCADE
);

CREATE UNIQUE INDEX idx_match_topic_releases_active_match
    ON public.match_topic_releases(match_id) WHERE revoked_at IS NULL;

REVOKE ALL PRIVILEGES ON TABLE public.match_topic_releases FROM PUBLIC;
DO $$
DECLARE role_name TEXT;
BEGIN
    FOR role_name IN
        SELECT rolname FROM pg_roles
        WHERE rolname IN ('anon', 'authenticated')
    LOOP
        EXECUTE 'REVOKE ALL PRIVILEGES ON TABLE public.match_topic_releases FROM '
            || quote_ident(role_name);
    END LOOP;
END $$;
REVOKE ALL PRIVILEGES ON SEQUENCE public.match_topic_releases_id_seq FROM PUBLIC;
DO $$
DECLARE role_name TEXT;
BEGIN
    FOR role_name IN
        SELECT rolname FROM pg_roles
        WHERE rolname IN ('anon', 'authenticated')
    LOOP
        EXECUTE 'REVOKE ALL PRIVILEGES ON SEQUENCE public.match_topic_releases_id_seq FROM '
            || quote_ident(role_name);
    END LOOP;
END $$;
