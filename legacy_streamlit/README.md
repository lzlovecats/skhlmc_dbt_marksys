# Legacy Streamlit Pages

These files are retained as the rendering and behaviour reference for pages now
served directly by FastAPI HTML routes. They are not registered in `main.py`.

> Resource safety: these files are reference-only. Do not register them again.
> Several historical pages contain whole-table, BYTEA/base64 or ZIP workflows
> that intentionally sit outside the current FastAPI bandwidth/RAM/storage guards.
> Port any needed behaviour into the bounded `api/` + `core/` path instead.

| Legacy source | Active HTML route |
| --- | --- |
| `home.py` | `/` |
| `open_db.py` | `/open_db` (`/open-db` remains an alias) |
| `vote.py` | `/vote` |
| `bug_report.py` | `/bug-report` |
| `registration.py` | `/registration` |
| `registration_admin.py` | `/registration-admin` (`/registration_admin` remains an alias) |
| `video_replay.py` | `/video-replay` |
| `video_admin.py` | `/video-admin` (`/video_admin` remains an alias) |
| `match_photos.py` | `/match-photos` |
| `team_roster.py` | `/team-roster` |
| `match_info.py` | `/match-info` (`/match_info` remains an alias) |
| `draw_match_schedule.py` | `/draw-match-schedule` (`/draw_match_schedule` remains an alias) |
| `management.py` | `/management` |
| `judging.py` | `/judging` |
| `review.py` | `/review` |
| `lateness_fund.py` | `/lateness-fund` (`/lateness_fund` remains an alias) |
| `ai_fund.py` | `/ai-fund` (`/ai_fund` remains an alias) |
| `chairperson.py` | `/chairperson` |
| `ai_coach.py` | `/ai-coach` |
| `ai_training.py` | `/ai-training` |
| `db_mgmt.py` | `/db-mgmt` (`/db_mgmt` remains an alias) |
| `dev_settings.py` | `/dev-settings` (`/dev_settings` remains an alias) |
