# HTML migration parity matrix (4.0.0)

Audit basis: direct comparison of `legacy_streamlit/*.py` with the registered
HTML route, browser code, API router and extracted domain logic. `PASS` means
the checked surface has code and an automated/static regression check;
`FIXED` means this migration changed a confirmed defect; `BLOCKED` means the
legacy capability is absent or has not had an authenticated, production-like
browser/database acceptance run. A `BLOCKED` row is a release blocker.

## Priority acceptance evidence

| Area | Status | Evidence and remaining acceptance |
|---|---|---|
| Home | BLOCKED | HTML provides role navigation, manual/rules modal, install/PWA affordance and status UI. Must run the legacy status checks and role visibility with production secrets in a browser. |
| Vote | PASS | HTML/API cover login, proposal, voting/switch/withdraw, objections, comments, depose flow, AI analysis, participation and account controls. Authenticated role/API acceptance passes; production push delivery remains a post-deploy smoke check. |
| Judging | PASS | Match gate, judge identity normalization, per-side cloud drafts, 120-second autosave, final transactional submission lock, receipt and unique 1–8 best-debater ranks are present; score bounds are enforced again in `core/judging_logic.py`. |
| Chairperson | FIXED | Start bells have strictly positive offsets, so entry/reset cannot ring; timer uses `performance.now()`, renders milliseconds, and stage schedules come from the shared four-format timing config. Automated timing invariants pass. Browser audio permission and fake-clock sequence remain staging checks. |
| AI Coach | PASS | Global model/cost/quota UI, strategy, motion sources, three speech practice modes, research, fact-check, audio review, researched Live debate and standalone segmented Mock are migrated. Format restrictions and usage accounting are enforced server-side. |
| AI Training | PASS | Seeded sentence library, consent, recording/owner/admin playback, AI/manual pre-check, five-part admin panel, 35-character manuscript segmentation, lexicon, lifecycle, coverage, review and filtered exports are migrated using one renderer. |
| Admin Hub | FIXED | Uses the common 84rem shell and legacy card order, has no return-home control, and opens each management tool as a normal page to avoid nested iframe chrome and overflow. |

## All migrated routes

| Legacy page → HTML route | Status | Functional evidence / blocker |
|---|---|---|
| `home.py` → `/` | BLOCKED | See priority row; production status checks not exercised. |
| `vote.py` → `/vote` | PASS | See priority row; agree/reject colours and shared UI contract are covered by regression checks. |
| `bug_report.py` → `/bug-report` | BLOCKED | Create/list/update surfaces exist; role-based E2E outstanding. |
| `open_db.py` → `/open-db` | BLOCKED | Login/query/table selection exist; production read-only SQL controls and exports require E2E. |
| `registration.py` → `/registration` | BLOCKED | Team/player form and API exist; duplicate/update/closed-window paths require DB E2E. |
| `registration_admin.py` → `/registration-admin` | BLOCKED | Review/edit/export controls exist; mail/export and iframe mobile acceptance outstanding. |
| `match_info.py` → `/match-info` | BLOCKED | Match CRUD/open-state controls exist; destructive confirmation and DB E2E outstanding. |
| `draw_match_schedule.py` → `/draw-match-schedule` | BLOCKED | Draw, save and export surfaces exist; seeded deterministic and overwrite cases outstanding. |
| `video_admin.py` → `/video-admin` | BLOCKED | Video/chapter CRUD and import surfaces exist; YouTube/API E2E outstanding. |
| `video_replay.py` → `/video-replay` | FIXED | Chapter clicks call `seekTo` on the existing player instead of recreating it; playback/share/progress needs real YouTube E2E. |
| `match_photos.py` → `/match-photos` | BLOCKED | Gallery/upload/download surfaces exist; object storage and authorization E2E outstanding. |
| `team_roster.py` → `/team-roster` | BLOCKED | View/edit roster surfaces exist; permission and conflict E2E outstanding. |
| `management.py` → `/management` | BLOCKED | Results dashboard exists; legacy export/result equivalence requires fixture comparison. |
| `judging.py` → `/judging` | PASS | See priority row; domain validation and transactional final write retained. |
| `review.py` → `/review` | BLOCKED | Score/draft/result review exists; PDF/CSV output parity requires golden-file acceptance. |
| `admin_hub.py` → `/admin-hub` | FIXED | See priority row. |
| `chairperson.py` → `/chairperson` | FIXED | See priority row. |
| `ai_coach.py` → `/ai-coach` | PASS | See priority row; external provider reachability remains a deployment smoke check rather than a code-parity gap. |
| `ai_training.py` → `/ai-training` | PASS | See priority row; live microphone permission and provider reachability remain deployment smoke checks rather than code-parity gaps. |
| `db_mgmt.py` → `/db-mgmt` | BLOCKED | Admin DB operations exist; backup/restore/destructive-path staging run outstanding. |
| `dev_settings.py` → `/dev-settings` | BLOCKED | Configuration CRUD exists; secret masking and every legacy setting require role E2E. |
| `lateness_fund.py` → `/lateness-fund` | FIXED | Empty year is no longer sent on first load; CRUD/carry/payment/export surfaces exist. Production-like empty and multi-year DB run outstanding. |
| `ai_fund.py` → `/ai-fund` | BLOCKED | Usage/budget/payment views exist; legacy cost/export calculations need fixture parity. |

## Release decision

**GO for a 4.0.0 candidate commit on `develop`; NO-GO for production deployment.**
The four release-critical pages (Vote, Judging, AI Coach and AI Training) pass
their code/API parity gates. The remaining `BLOCKED` rows are explicitly deferred
for committee feedback and production-like browser acceptance. Do not merge to
`main`, tag or deploy until those release gates and post-deploy service checks are
authorised separately.
