-- Mark one enabled individual chapter as the replay's best-debater jump point.

ALTER TABLE video_chapters
    ADD COLUMN is_best_debater BOOLEAN NOT NULL DEFAULT FALSE;

ALTER TABLE video_chapters
    ADD CONSTRAINT video_chapters_best_role_check
    CHECK (
        is_best_debater = FALSE
        OR chapter_label IN (
            '正主', '反主', '正一', '反一', '正二',
            '反二', '正三', '反三', '反結', '正結'
        )
    );

CREATE UNIQUE INDEX idx_video_chapters_one_best_debater
    ON video_chapters (video_id)
    WHERE is_best_debater = TRUE;

-- Link each individual replay role to a normal committee account. The
-- application enforces the normal-member subset because account lifecycle
-- state is mutable and therefore cannot be expressed as a static FK check.

CREATE TABLE video_roster (
    video_id        INTEGER NOT NULL,
    role_label      TEXT NOT NULL CHECK (role_label IN (
        '正主', '反主', '正一', '反一', '正二',
        '反二', '正三', '反三', '反結', '正結'
    )),
    member_user_id  TEXT NOT NULL,
    updated_at      TIMESTAMP DEFAULT NOW(),
    PRIMARY KEY (video_id, role_label),
    CONSTRAINT fk_video_roster_video
        FOREIGN KEY (video_id) REFERENCES match_videos(id)
        ON DELETE CASCADE,
    CONSTRAINT fk_video_roster_member
        FOREIGN KEY (member_user_id) REFERENCES accounts(user_id)
        ON DELETE CASCADE
);

CREATE INDEX idx_video_roster_member_video
    ON video_roster (member_user_id, video_id);

REVOKE ALL PRIVILEGES ON TABLE video_roster
    FROM PUBLIC, anon, authenticated;
