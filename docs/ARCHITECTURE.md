# 系統架構及資料庫地圖

> 最後核對：2026-07-13，程式版本以 `version.py` 為準。本文描述目前真正由 Render 啟動的 HTML + FastAPI 系統，以及同日對 production Supabase 所做的唯讀盤點；不是歷史遷移文件。

## 1. 一眼看懂

```text
Browser / PWA
  └─ frontend/* (HTML/CSS/vanilla JS)
       └─ same-origin /api/* + WebSocket
            ├─ api/*              輸入驗證、權限、HTTP response
            ├─ core/*             可測試的業務規則及 SQL
            └─ deploy/proxy.py    app 組裝、靜態路由、WebSocket、TTS/R2/runtime
                 ├─ PostgreSQL / Supabase：結構化資料及細小 metadata
                 ├─ Cloudflare R2：相片及錄音 binary
                 └─ Gemini / OpenRouter / Azure：按需外部 AI/TTS
```

Production 只執行 `deploy/start.sh` → 單一 Uvicorn process；沒有 Streamlit process。Browser 不會直連 Supabase，亦不應收到 database URL、service-role key 或 R2 secret。相片及錄音經短期 presigned URL 直接在 browser 與 R2 傳送，避免 Render RAM/頻寬和 Supabase database storage 承擔 binary。

## 2. 目錄責任

| 位置 | 單一責任 | 維護規則 |
|---|---|---|
| `frontend/<page>/` | 頁面 markup、CSS、browser interaction | HTML 保持正常縮排；共用行為放 `frontend/shared/`；不要在頁面複製業務門檻 |
| `api/` | FastAPI routers、Pydantic payload、auth gate、pagination | handler 要薄；資料聚合及 transaction 放 `core/` |
| `core/` | domain logic、parameterized SQL、provider/storage adapters、DB engine ownership | 不依賴 UI；所有 DB executor 明確注入；批量查詢避免 N+1；CLI直接用輕量`core/db_runtime.py`，不要import整個app |
| `deploy/proxy.py` | FastAPI app composition、靜態資源、WebSocket room/relay、process-level cache | 只放跨 router/runtime 工作；新普通 CRUD 不再加進此檔 |
| `schema.py` | 只供新、空database使用的bootstrap schema | `CREATE IF NOT EXISTS`不是migration history；既有環境只可走`migrations/` |
| `migrations/`, `core/db_migrations.py` | Production baseline manifest、成對SQL migration、checksum ledger及transaction runner | baseline不重建現有tables；每個新migration必須有`.up.sql`及`.down.sql`，mutation預設dry-run |
| `system_limits.py` | RAM、request、upload、bandwidth、retention 限額唯一來源 | API、runtime、docs 必須由此同步，不另寫 magic number |
| `assets/` | runtime 會讀取的規則、manual、prompt、音效、PDF template及固定 eval cases | 計劃／runbook 放 `docs/`，不要混入 runtime assets |
| `tools/` | 人手執行、可重跑的 migration/cleanup/dataset 工具 | destructive mode 必須有 dry-run、核對及明確 confirmation |
| `tests/` | unit/API/release/resource regression | 每個修 bug 要有 regression；release gate 檢查 HTML、dead runtime及資源界線 |
| `appliance/` | 校內 kiosk／備份 appliance 的獨立部署資產 | 不屬 Render request path |

## 3. 功能分區

| Domain | 入口 | 主要 code | 主要資料 |
|---|---|---|---|
| 首頁及身份 | `/`, `/api/home/*` | `api/home_api.py`, `core/home_logic.py`, `core/auth_logic.py` | `accounts`, `login_records`, `notification_reads`, `app_config` |
| 報名及名單 | `/registration`, `/registration-admin`, `/team-roster` | registration/team-roster APIs, `core/registration_logic.py`, `core/match_logic.py` | registration settings/records, `matches`, `debaters`, `match_roster_links` |
| 賽程及場次 | `/match-info`, `/draw-match-schedule` | match/schedule APIs及 core | `matches`, `debaters`, roster links |
| 電子分紙 | `/judging`, `/review`, `/management`, `/chairperson` | judging/review/management/chairperson APIs及 core | `score_drafts`, `scores`, `debater_scores`, `best_debater_rankings` |
| 辯題投票 | `/vote`, `/open_db` | vote/open-db APIs, `core/vote_logic.py`, `core/open_db_logic.py` | `topics`, proposal/removal tables, ballots, `motion_comments` |
| 影片及圖片 | `/video-replay`, `/video-admin`, `/match-photos` | media APIs, `core/media_logic.py`, `core/r2_storage.py`, proxy progress endpoints | video tables、`match_photos` metadata；binary 在 R2 |
| AI 辯論及訓練 | `/ai-coach`, `/ai-training`, `/practice/*` | AI APIs, providers/RAG, proxy Live/room/TTS runtime | TTS/LLM/RAG/model/eval tables, usage logs；錄音在 R2 |
| 基金 | `/lateness-fund`, `/ai-fund` | `api/funds_api.py`, `core/funds_logic.py` | lateness fund及AI fund tables |
| 營運及開發 | `/admin-hub`, `/db-mgmt`, `/dev-settings`, `/bug-report` | admin/bug APIs及 core | `bug_reports`, typed `app_config`, internal migration ledger；SQL console不准存取設定／內部tables |
| Projector | `/projector`, `/projector/control` | proxy runtime | `projector_state` |

## 4. Database 設計

### 4.1 分區及保留理由

- Identity：`accounts`, `login_records`, `notification_reads`, `push_subscriptions`。
- Competition：`competition_registration_settings`, `competition_registrations`, `matches`, `debaters`, `match_roster_links`。
- Scoring：`score_drafts`, `scores`, `debater_scores`, `best_debater_rankings`。草稿與不可變正式分紙分開是合理設計。
- Topic governance：`topics`, `topic_votes`, `topic_vote_ballots`, `topic_removal_votes`, `topic_removal_vote_ballots`, `motion_comments`。動議與逐人 ballot 分開，避免把票數反覆寫入 JSON。
- Media：`match_videos`, `video_views`, `video_comments`, `video_votes`, `video_chapters`, `video_progress`, `match_photos`。DB 只保存 metadata/R2 keys。
- AI training：consents、scripts、lexicon、recordings、LLM submissions、dataset/model/eval/RAG/audit tables。來源資料、immutable snapshot、model release及評估結果分開，方便撤回及重現。
- Finance：AI fund transactions/usage及 lateness records/expenses/periods 分開，保留可審計 ledger。
- Runtime/resource：`practice_daily_usage`, `bandwidth_usage_logs`, `r2_upload_intents`, `projector_state`。
- Settings：`app_config` 是新 typed store；舊 `system_config` 暫時只作 rollback bridge，確認 production 全部 key 已遷移後才刪。
- Schema control：`schema_migrations`只保存version、name、migration checksum、source checksum及套用時間；不存業務資料，PUBLIC及browser/runtime roles均無權限。

### 4.2 `app_config` 取代大雜燴設定

`app_config` 每項都有 `namespace`、原生 JSONB `value`、`value_type`、`is_secret` 及 `updated_at`。現有分類：

- `auth`：bcrypt password hash、cookie secret；
- `runtime` / `ai`：maintenance、provider及default model；
- `access`：可用戶名單及delegated roles；
- `finance`：基金門檻、付款說明及外部結餘 snapshot；
- `analysis`：可重建的投票分析 cache；
- `resource` / `migration`：用量 snapshot、警報及一次性 marker。

所有讀取都先查 `app_config`，部署過渡期才 fallback `system_config`；新寫入只可用 registry 內已分類的 key。Secret 不可由 public API、developer payload或 SQL console讀出。

### 4.3 2026-07-13 production 唯讀 audit

Audit起始時production有41張public tables；2026-07-13完成typed config預遷移後有42張application tables，再以既定checksum新增一張內部`schema_migrations`，現合共43張。`schema.py`仍包含若干尚未建立的下一階段AI/resource tables；因此不可再把bootstrap DDL當作production truth。

已確認事項：

- 所有43張public tables的RLS均為關閉；內部migration ledger先以table grants隔離，application tables的RLS啟用次序見 `docs/ROADMAP.md`，不可直接一鍵全開。
- `tts_voice_recordings` 148 rows 全都有 R2 key，但舊 `audio_data` 仍佔約 110.9 MB。
- `match_photos` 45 rows 全都有 R2 key，但舊 `image_data` 仍佔約 10.9 MB。
- 合共約 121.8 MB legacy BYTEA 可在完成 browser playback抽查後，用 `tools/finalize_r2_media.py` 先 dry-run/HEAD verify，再於獲明確批准的 maintenance window移除。未核對前不會自動 drop。
- 2026-07-13 finalizer dry-run已核對193 rows／238 R2 objects／122,687,464 bytes全部通過；呢個數包含相片縮圖，所以不等於legacy BYTEA大小。Browser播放、backup及明確drop批准仍未完成，兩個BYTEA columns未改。
- `score_drafts` 有三個等價 unique indexes及重複 match foreign keys；`scores` 亦有重複 match FKs。
- `match_roster_links` 的 token 已有 unique index，另加的普通 token index重複。
- 兩張 ballot 的 primary key 已以 `topic_text` 開頭，額外單欄 topic indexes 對目前 query pattern屬重複。
- Proposal/removal motion以`topic_text`做primary key，實際上無法為同一辯題保存第二輪動議；production的removal motion對`topics`仍使用`ON DELETE CASCADE`，罷免通過刪topic時會一併刪走該輪motion/ballots，令歷史及analytics不完整。新database bootstrap已停止建立該FK；production目標migration仍要改用`motion_id`，另以partial unique限制同一topic只有一個pending round，歷史FK不可跟topic cascade。
- Production的`scores.submitted_time`只有無日期的`TIME`（bootstrap原本更是TEXT），不能可靠跨日審計；應遷移至`submitted_at TIMESTAMPTZ`。Production的`score_drafts.score_payload`已是JSONB，bootstrap亦已對齊；migration要驗證每個值是object並清理任何歷史double-encoded payload，不為整份JSON盲加index。
- Production `app_config`已由`system_config`預遷移22/22 keys，namespace/type/secret metadata完全一致，舊表保留作rollback；但三個password仍非bcrypt。成功legacy login會逐步升級，部署者仍應在4.2.0 workflow驗證後rotate admin/developer/SQL password及cookie secret，未完成前不可移除bridge。
- Read-only reconciliation確認42張application tables中41張有bootstrap owner；唯一production-only `tg_notification_queue`估算0 rows／24,576 bytes，但未排除外部writer前不會drop。Bootstrap另宣告12張production未存在的AI/resource tables；是否啟用要逐feature決定，現有compatibility create path要在staging有正式migration或fail-closed方案後移除。
- Column-name drift只涉及已知未完成migration：`match_photos.image_data`、`tts_voice_recordings.audio_data`仍是production-only legacy BYTEA；consent四欄及recording四個audio metadata欄仍是code-only。精確type/default/constraint/index drift要在staging restore判定。
- 原有20個endpoint/request-path重複indexes及最後一個AI coach startup compatibility index已全部移除；index definition只由`schema.py` bootstrap或versioned migration擁有。Production缺少的index不可再由request偷偷建立，必須先用實際query plan證明價值。
- 已由production及bootstrap共同擁有的request/lazy table/column DDL、process locks及相關dead tests已移除；AI Training亦不再重建五張shared TTS/LLM tables或重加已存在的`audio_sha256`／`r2_key`。RAG reindex不再由request安裝`vector` extension或ALTER embedding欄，未有正式migration會明確503。Resource guard migrations套用後已移除R2 intent、bandwidth、practice及AI coach quota runtime create；production確認兩個legacy BYTEA已nullable後，最後兩個startup ALTER及無caller的`schema.MIGRATIONS/run_migrations`亦已刪除。Scanner現由原基線76個直接source sites降至12個，餘下只在typed-config過渡及未provision AI Training schema。
- `ai_coach_live_briefs`、`ai_coach_prepare_usage`、`projector_state`已由`schema.py`統一擁有；前後兩張既有production table不再startup重建，`ai_coach_prepare_usage`由`20260713_0001`建立。`tg_notification_queue`仍未在repo schema／active code找到owner，要在staging確認資料及外部consumer後才保留或移除。
- SQLAlchemy engine及bounded pool由`core/db_runtime.py`單一擁有，FastAPI保留薄compatibility wrapper；schema/config/media maintenance CLI不再載入全套routers，lifespan結束亦會dispose pool。
- Baseline source checksum為`18b2734a1abea3dfca2afb7e0f9678ef01e7d4d6fa99ffa2f3b5e1d5d235bffb`（42 application tables／321 columns／119 constraints／78 indexes）。新增ledger後checksum為`0a74cd10642c8a00d30c1ffac60d79a74923940497665c91757c6004f4874d1f`（43 tables／326 columns／124 constraints／79 indexes／0 RLS policies）；history在`20260713_0000` head，PUBLIC、`anon`、`authenticated`及`app_backend`均無ledger權限。Checksum只計schema定義，不計浮動row/size metrics。
- `20260713_0001_provision_resource_guards`已strict-additive建立四張成本／quota guard tables及三個index；`20260713_0002_lock_resource_guard_privileges`隨即撤銷Supabase default授予`anon`／`authenticated`的四表及兩sequence權限。Production history在`0002` head，47 public tables／46 application tables、348 columns、135 constraints、86 indexes，checksum `25d04e80310a5b39fdc44d892fe3347f8be34a63fd86c9747409b53757ded2ed`；四表均0 rows，受限角色無table/sequence privileges。

### 4.4 Schema 維護準則

1. 新 table/column/index/policy必須是版本化 migration，附 forward、rollback、data classification及測試。
2. Runtime request不可重複執行 DDL；startup只執行已編號 migration。
3. Index 只為已量度 query shape建立；用 `EXPLAIN (ANALYZE, BUFFERS)` 驗證，避免每次寫入維護無效 index。
4. 大型 payload放 R2；PostgreSQL只保存 key、hash、size、content metadata及狀態。
5. Retention/cleanup必須有界：login、notification、usage、upload intent及room state按 `system_limits.py` 清理。
6. RLS policy以 backend request context為信任來源；絕不相信 client傳入的 user/role。
7. 任何「count → insert」quota或「read ballot → write → resolve」狀態轉移都要在同一transaction內，以constraint或per-resource advisory lock處理併發，而不是依賴單worker剛好順序執行。
8. Supabase會以default privileges把新table授予`anon`／`authenticated`；每個`CREATE TABLE` migration必須在同一版本明確撤銷`PUBLIC`及兩個browser roles。`0001`／`0002`係已套用的歷史補救pair，catalog gate會阻止日後再漏。

## 5. 效能及資源策略

- API list使用 DB `LIMIT/OFFSET`、filtered `COUNT(*)`及有界 page size；不要先 `fetchall` 再由 Pandas切頁。
- 同一 payload需要多個 aggregates時用 conditional aggregation、CTE或少量 batch queries，避免 row-by-row/N+1。
- 只 select 回應需要的 columns；不取錄音/圖片 binary，不在 log寫大型內容或 secrets。
- 外部 HTTP/TTS/AI response有 timeout及最大 bytes；streaming/upload在到達 process前後都驗證 size。
- Video progress只有實際改變才 upsert，view event去重；短 TTL cache只用於可重建資料，而且 key要包含使用者/權限維度。
- Render維持單 worker是因 WebSocket room及部分 process-local state；擴至多 worker前要先把 room/session/cache搬到共享 store。
- Static assets經 Cloudflare cache；HTML及service worker採可更新策略，hashed/shared assets才長 cache。

## 6. 已知架構債及目標次序

1. `deploy/proxy.py` 同時負責 app composition、WebSocket、TTS、resource accounting及projector，超過 4,000 行；按 roadmap逐個拆成 router/service，保持 app factory薄身。
2. `RuntimeDb.query()` 仍把結果materialize成Pandas DataFrame；新hot path應直接使用row mapping／streaming或repository method，舊code逐域替換。
3. Admin session、Live room及部分 cache是單 process記憶體；目前單 worker可接受，但不是水平擴展架構。
4. 29 個 HTML頁仍有少量 style/markup重複；今次先保證可讀縮排及 shared assets，日後以小型共用 component逐步收斂，避免一次引入大型前端 framework。
5. Production migration baseline已建立；`schema.py`與production drift仍要先在staging restore分類及演練forward/rollback，之後才處理重複constraint/index和RLS，不直接在production手改。

## 7. 改動驗收

每次 release 至少執行：Python compile、完整 test suite、release/resource gates、所有 HTML 長行/縮排 gate、敏感 table access gate、主要 API smoke。涉及 database/R2的 destructive步驟，另需 production backup、dry-run輸出、抽樣核對、明確批准及rollback記錄。
