# 統一後續路線圖：Database、RLS、自家 TTS 及辯論 LLM

> 最後整理：2026-07-13。這是所有未完成研發／基建工作的唯一主計劃；每一步都有前置條件、驗收 gate及停損點。完成一個 gate才進下一個，避免同時改 database權限、模型及production runtime而無法定位問題。

## 0. 現況與優先次序

| 工作線 | 現況 | 下一個可執行動作 |
|---|---|---|
| Repo/runtime簡化 | HTML + FastAPI已接管；舊Streamlit已清理；DB engine/pool已搬到輕量`core/db_runtime.py` | 繼續按bounded domain拆細`deploy/proxy.py`，不用再保留雙軌相容 |
| Typed settings | Production `app_config`已建立並與22個legacy keys/metadata一致；`system_config`仍保留 | 部署4.2.0後驗證全部workflow，rotate三個credential，再移除legacy bridge |
| R2 media | 新讀寫已是 R2-only；production舊 BYTEA仍約121.8 MB | Browser抽樣播放 → finalizer dry-run/HEAD verify → 獲批准後drop legacy columns |
| Versioned DB migrations | Production停在`20260713_0003`；`0004`已完成forward/rollback驗證但刻意pending，runtime DDL清理屬待部署候選 | 先按P0.3部署候選，再套用`0004`並核對catalog；之後才做非additive schema修正 |
| RLS | Production現有46張application tables加內部`schema_migrations`，全部未啟用 | P1 staging forward/rollback通過後，先建立非owner、NOBYPASSRLS runtime role及request transaction context |
| 讀音字典 | `tts_lexicon` UI及文字覆寫runtime已完成 | 收集100–200句固定讀音測試，填高頻term/reading/jyutping |
| 自家粵語聲線 | consent、R2錄音、審核、accepted export及provider abstraction已完成；dataset/model registry未啟用 | 選一位主聲線，先收30–60分鐘accepted音訊及完成snapshot contract，再建正式schema |
| 辯論 LLM | 文字收集、審核及export已完成；待部署候選會令dataset/eval/RAG endpoints按正式marker fail-closed | 先固定eval worker/run contract及20–50條versioned cases，再逐bundle migration，RAG證明不足才LoRA |

建議順序：先做 P0安全收尾 → P1 migration baseline → P2 RLS perimeter → TTS/LLM可平行收集 → 模型實驗 → 最後才做per-member RLS及自家模型production rollout。

---

## P0. Production安全及儲存收尾

### P0.1 部署 typed `app_config`

- [ ] Render production已對`main`開啟auto-deploy；先只推／驗證`develop`，到低流量發布窗口及rollback commit準備好後才push `main`一次。
- [ ] Deploy包含 `app_config`及legacy read bridge的版本；保留舊 `system_config`作一個rollback窗口。
- [x] 2026-07-13已以`tools/migrate_app_config.py` dry-run及versioned confirmation在單一transaction建立／填充typed table；插入22/22 keys、unknown=0，不覆寫typed值、不輸出secret，`system_config`完整保留。
- [x] Production兩表均為22 keys；namespace、JSON type及`is_secret`全部一致，missing／typed-only／metadata mismatch均為0。
- [x] 已用`tools/audit_app_config.py`獨立複核只含metadata及boolean health：migration complete、cookie secret長度合格；三個password仍非bcrypt，因此rotation/bridge removal未完成。
- [ ] 驗證首頁、committee login、admin/dev/SQL login、AI provider設定、基金角色、TTS角色、投票分析cache及resource warnings。
- [ ] 以新密碼rotate `admin_password`、`developer_password`及`sql_password`；全部只存bcrypt hash。
- [ ] Rotate `cookie_secret`會令現有cookies失效，安排低流量時間並預先通知使用者重新登入。
- [ ] 觀察至少一個release；確認沒有fallback-only key後，以versioned migration刪舊table及bridge code。

**Gate P0-A：**typed/legacy key集合一致；所有登入及settings workflow通過；SQL console不能讀寫`app_config`或`system_config`；production沒有plaintext credential。

### P0.2 回收 Supabase舊 binary（需明確批准）

Production audit基線：`tts_voice_recordings` 148/148及`match_photos` 45/45已有R2 key，但舊BYTEA仍分別約110.9 MB及10.9 MB。

- [x] Finalizer已具備bounded keyset讀取、逐object size／SHA／MIME／cache metadata驗證、aggregate JSON報告、fail-fast及transaction lock timeout；本地tests不接觸production。
- [x] 2026-07-13 production dry-run已通過：193 rows、238 R2 objects、122,687,464 bytes全部HEAD metadata一致；148 audio objects為110,926,192 bytes，45張相片原圖＋縮圖90 objects為11,761,272 bytes。兩個legacy BYTEA columns仍完整保留。

1. 在真實browser抽樣播放／下載不同格式錄音、原圖及thumbnail。
2. 確認R2 lifecycle、CORS、presigned expiry及backup符合 `docs/R2_MEDIA_MIGRATION_RUNBOOK.md`。
3. 執行 `tools/finalize_r2_media.py` 的預設dry-run；工具要對每個object做HEAD、size/hash/metadata核對，任何一項失敗即停。
4. 保存dry-run摘要、database backup及rollback說明。
5. 只有獲明確批准及confirmation token後才在maintenance window使用`--apply`移除舊BYTEA columns。
6. `VACUUM (FULL)`會鎖表；不要自動執行。先觀察Supabase實際storage回收方式，再另排maintenance。

**Gate P0-B：**所有R2 objects核對成功、browser抽查通過、backup可還原、使用者批准。未達成時保留columns，不影響新R2-only路徑。

目前只完成object verification；browser抽查、backup restore證明及破壞性操作明確批准仍未完成，所以Gate P0-B未通過，禁止執行`--apply`。

### P0.3 4.2.0候選部署及`0004`套用

Production目前必須維持`0003`，直至同一候選commit全部測試通過及GitHub Auth可即時完成發布。現行`main`仍有舊AI Training runtime DDL，而且未嚴格執行四項consent metadata；發布前應暫停使用AI Training頁面，唔好先單獨套用`0004`。

部署前：

1. 本地候選只可有一個已知commit；跑完整pytest、compileall、`git diff --check`、HTML可讀縮排、migration catalog、schema reconciliation及read-only production status。
2. Production必須仍為head `0003`、只pending `0004`、47 public／46 application tables、356 columns、135 constraints、86 indexes、0 RLS，checksum `5eb61fff916d0030c7c67216e137c50448b927d374cc45649d5087220ee5bd38`。
3. `ai_training_audit`及七張future AI tables必須全部不存在；任何舊runtime偷偷建立的relation都要先停低調查。
4. 先push及驗證`develop`。低流量maintenance window先將同一commit推到`main`，等Render顯示新version健康；呢段短窗口AI Training mutation可能因audit未建立而503，但transaction會完整rollback，唔會留下無audit mutation。
5. 確認新Render code已上線、runtime DDL已停，先執行`manage_db_migrations.py apply` dry-run，再以版本confirmation套用`0004`。唔好反轉次序，避免舊main request建立七張future tables。
6. 套用後預期48 public／47 application tables、363 columns、141 constraints、88 indexes、0 RLS；`PUBLIC`／`anon`／`authenticated`對audit table及sequence均無權限。Migration SQL一經正式套用即freeze，不再改原檔。

部署後：

1. 核對Render version、health及log，特別留意`UndefinedTable`、HTTP 500、DB pool、audit retention或R2 cleanup錯誤。
2. Migration status必須head `0004`、無pending／gap／checksum drift；再跑read-only audit及reconciliation。
3. Smoke首頁、登入、投票、評分、media、AI Coach及Live；AI Training要測成人與未成年/guardian consent、錄音提交、撤回及一次review。Dataset/eval/RAG endpoints應明確503，未啟用RAG不得call embedding provider。
4. 核對audit只為真state transition新增；重複consent／withdraw不可無界增長。驗證retention SQL成功，但唔好製造虛假永久consent證據。
5. 監察至少一個發布窗口的Render RAM、response 5xx、Supabase connections/egress/storage、R2 intent及bandwidth ledger。App rollback可保留已建立的空/有資料audit table；一旦有audit row，down migration會拒絕drop。
6. 完成production驗證後，才把本文件及ARCHITECTURE的`0004 pending`更新成實際post-deploy數字，並開始P0.1 credential rotation及P0.2 browser抽樣。

**Gate P0-C：**同一候選commit已部署、`0004`在新runtime上套用、完整smoke及catalog/permission核對通過；未達成前不得開始RLS或drop BYTEA。

---

## P1. Database migration baseline及schema瘦身

### P1.1 建立production truth

- [x] 加入`tools/audit_db_schema.py`：read-only catalog snapshot涵蓋tables、columns、constraints、indexes、views、functions、triggers、owners、types、default/table/function/schema/sequence grants、RLS policies、sequences、extensions及size／row estimates；schema checksum不受浮動metrics影響，亦不讀application row values。
- [x] 2026-07-13 production read-only summary：PostgreSQL 17.6、42 tables、321 columns、119 constraints、78 indexes、15 sequences、1 view、0 policies／0 RLS tables；estimated 2,141 rows、127,770,624 relation bytes；schema checksum `18b2734a1abea3dfca2afb7e0f9678ef01e7d4d6fa99ffa2f3b5e1d5d235bffb`。
- [x] 以上述checksum及42-table count作immutable source gate，在單一transaction只新增內部`schema_migrations`及一筆`20260713_0000` baseline；沒有ALTER現有application tables。
- [x] Post-baseline只讀複核：migration history在head、無unknown/gap/name/checksum drift；PUBLIC、`anon`、`authenticated`、`app_backend`全部無ledger權限。Catalog現為43 tables、326 columns、124 constraints、79 indexes、0 policies／0 RLS，checksum `0a74cd10642c8a00d30c1ffac60d79a74923940497665c91757c6004f4874d1f`。
- [x] 2026-07-13套用首兩個post-baseline migrations後，production history在`20260713_0002` head、無pending/drift；catalog為47 public tables／46 application tables、348 columns、135 constraints、86 indexes，checksum `25d04e80310a5b39fdc44d892fe3347f8be34a63fd86c9747409b53757ded2ed`。四張新resource guard tables均為0 rows。
- [x] `20260713_0003_add_tts_consent_metadata`套用後history在`0003` head；catalog仍為47 tables，但增至356 columns，checksum `5eb61fff916d0030c7c67216e137c50448b927d374cc45649d5087220ee5bd38`。3份舊consent四個新flags全部false，必須重新明確確認；148段錄音、116 accepted及22 pending完全保留。
- [ ] `20260713_0004_provision_ai_training_audit`已完成SQL、privacy constraints、browser-role revoke、non-empty rollback guard及production forward/rollback驗證；為避免舊main runtime DDL空窗，現已安全rollback，production仍在`0003`且`0004`pending。曾驗證post-apply預期為48 public／47 application tables、363 columns、141 constraints、88 indexes、0 RLS；正式套用須跟P0.3次序，套用後migration檔即freeze。
- [x] 加入`tools/reconcile_db_schema.py`作可重跑只讀inventory gate：production現有46張application tables中45張與bootstrap owner重疊；唯一production-only是estimated 0 rows／24,576 bytes的`tg_notification_queue`；另有8張code-only tables（pending audit加七張dataset/model/eval/RAG），已列入migration清單。
- [x] Column-name reconciliation現只餘兩個production-only legacy BYTEA：`match_photos.image_data`及`tts_voice_recordings.audio_data`。TTS consent四欄及recording四個audio metadata欄已由`0003`對齊；type/default/constraint/index definition仍須在staging restore核對。
- [ ] 在staging restore執行schema-only dump，保存tables、columns、types、defaults、constraints、indexes、views、functions、triggers、owners、grants、RLS及row counts。
- [ ] 在staging restore執行`./venv/bin/python tools/audit_db_schema.py --exact-row-counts`保存baseline JSON；production只用預設estimated mode，避免live `COUNT(*)`全表掃描。之後用`--expect-checksum`做drift gate。
- [x] 已完成table/column name-level `schema.py`分類；production-only、code-only及已知column drift如上。精確definition drift仍須staging schema dump。
- [x] 為現有production狀態建立只作baseline的migration version；沒有把現有42張application tables重新CREATE一次。
- [x] 使用repo內輕量編號SQL runner：每個migration必須有成對forward／rollback、runner-owned transaction及內容checksum；孤兒file、重複version、history gap、unknown version及checksum drift會fail closed。Mutation預設dry-run並要求versioned confirmation。
- [x] 移除`api/`、`core/`及`deploy/`全部request/runtime `CREATE INDEX`：原有20個endpoint-path indexes及最後一個`idx_ai_coach_prepare_usage_user_created` startup compatibility path均已移除；index只由bootstrap或versioned migration擁有。
- [x] 待部署候選已移除所有shared/future request DDL、逐worker eval seed及重複ALTER；七張future tables從bootstrap移除。Advanced feature必須有migration寫入的exact table COMMENT marker加完整real-table bundle先啟用；legacy同名table/view不能開feature。RAG只cache negative readiness，付費embedding前每次重驗positive marker/vector schema，亦刪除JSON-vector fallback。Runtime DDL scanner由原基線76個sites降至1個，只餘P0 typed-config bridge；production要部署後再確認。
- [x] Developer網頁的`/developer/init-db` runtime DDL入口已移除；`schema.init_db()`只供新空database，偵測到`schema_migrations`會在任何DDL前拒絕，避免4.2.0 code上線至套用`0004`之間被手動bootstrap撞表。
- [ ] Startup只跑未套用migration；`schema.py`歷史retrofit及resource/media startup DDL已清空，餘下只係P0 typed-config legacy bridge仍會確保`app_config`存在。觀察一個release及完成credential rotation後移除bridge create/migration call即可完成。
- [x] `20260713_0001_provision_resource_guards`以strict additive SQL新增`practice_daily_usage`、`bandwidth_usage_logs`、`r2_upload_intents`、`ai_coach_prepare_usage`及三個index；production preflight確認四表不存在及history valid後原子套用，沒有更改既有table/row。
- [x] Post-apply發現Supabase default privileges令`anon`／`authenticated`自動取得新表權限；即時以`20260713_0002_lock_resource_guard_privileges`撤銷四表及兩條sequence全部browser-role權限。複核四表仍0 rows、所有受限table/sequence privileges均為false。
- [x] `20260713_0003_add_tts_consent_metadata`以metadata-only ALTER加入四個`NOT NULL DEFAULT FALSE` consent flags及四個nullable錄音probe欄；舊consent不可被推斷為新授權，API所有錄音入口統一要求voice-cloning、cloud及minor/guardian gate。
- [x] 待部署候選令AI Coach、AI Training及Live三條usage writer共用有限頻率、best-effort 400日retention，maintenance DELETE失敗唔會阻當次accounting。Live quota繼續用既有UTC-naive boundaries/rows，避免部署過渡漏計；同表其他writer仍有HKT/legacy混合，正式`TIMESTAMPTZ`遷移列入P1.2，今次唔猜測backfill。
- [x] Consent grant／withdraw、TTS/LLM review及核心mutation連audit同transaction；同意書升至`tts_voice_v3_2026_07`，voice/cloud/minor/guardian全部由UI/API明確提交，舊v2 row保留。Grant、withdraw及recording finalization共用per-user advisory lock及transaction內再次核對，防止撤回後再插錄音。Privacy base withdrawal永遠先落帳；optional legacy/future derived cleanup失敗或partial都唔阻撤回，正式啟用future bundle前必須加durable outbox/cascade。
- [ ] 在staging restore重播`20260713_0001`／`0002` forward及rollback並核對quota query plans；今次因未有staging，只准production執行純additive create及privilege revoke，沒有在production測試drop/grant rollback。任何非additive migration前必須補回此gate。

### P1.2 第一批schema修正

在staging以catalog name及`EXPLAIN`再確認後：

- [ ] `score_drafts`只保留一個真正的 `(match_id, judge_name, side)` unique constraint/index及一個match FK。
- [ ] `scores`只保留一個match FK。
- [ ] 移除`match_roster_links`重複的普通token index，保留UNIQUE。
- [ ] 若ballot常見查詢可用PK leading column，移除兩個額外topic-only indexes；保留user indexes供member history。
- [x] `projector_state`、`ai_coach_live_briefs`及`ai_coach_prepare_usage`均由`schema.py`擁有；`ai_coach_prepare_usage`已由`20260713_0001`建立，三者startup/request DDL全部移除。
- [ ] `tg_notification_queue`現為唯一production-only table，catalog estimate 0 rows／24,576 bytes；仍要查清外部writer/consumer及backup保留需要，確認後才以versioned migration移除。
- [x] 已完成8張AI schema逐表review：只有`ai_training_audit`屬active流程必需，`0004`已就緒但production仍pending；其餘七張不再runtime/bootstrap偷偷建立，待部署候選按dataset/model、eval、RAG三個bundle fail-closed。
- [ ] Dataset/model bundle正式migration前：移除重複manifest/item metadata；加source reverse index、immutable consent/hash、snapshot-kind/model-kind約束、artifact hash及合法status transition；撤回與model block同一transaction，model list只回有界摘要。
- [ ] Eval bundle正式migration前：定義可重現`run_id`、eval-set/content checksum、不可變case version、prompt/provider/model/pipeline/RAG versions、latency/tokens/cost及盲評結果；cases由versioned seed一次建立，placeholder HTTP endpoint不可回`ok:true`假裝已執行worker。
- [ ] RAG bundle正式migration前：只存一份`vector(768)`，不保留`embedding_json`或逐chunk重複metadata；`submission_id`唯一，withdraw/reject直接delete document並cascade chunks；reindex以單job lock、寫回前重新鎖定並核對source，所有row/content/retention上限涵蓋archived資料。
- [ ] 重構topic/removal motions為獨立`motion_id` PK；`topic_text`只是內容欄，為pending狀態加partial unique，ballots/comments引用motion id。遷移現有歷史後，removal motion不再因topic被罷免而cascade刪除；API仍可用pending motion id明確投票。
- [ ] 把`scores.submitted_time`遷移為`submitted_at TIMESTAMPTZ`（以可證明的舊資料時區/日期回填，無法證明則標記unknown而非猜）；production `score_drafts.score_payload`已是JSONB，驗證每個值為object並清理歷史double-encoded payload。
- [ ] 把`ai_fund_usage_logs.created_at`由混合UTC/HKT的naive `TIMESTAMP`遷移為`TIMESTAMPTZ`。先按writer／feature及可證明部署歷史分類，無法證明時區的row標記unknown而唔猜；之後全部writer寫aware UTC，HKT日／週／月報表用明確`AT TIME ZONE 'Asia/Hong_Kong'`。
- [ ] 每個future AI bundle migration要在anchor table寫exact `COMMENT 'skhlmc-feature:<feature>:<version>'`，runtime只讀catalog comment加完整real-table/column gate，唔授予private`schema_migrations`權限。正式啟用derived data前加入withdrawal outbox或DB cascade及restricted-runtime integration test。
- [ ] Registration、match及其他quota的`COUNT → INSERT`用transaction + advisory lock或DB constraint收口，避免兩個request同時越過上限。
- [ ] 為judging access-code及review password驗證加per-IP/per-match rate limit、失敗audit及合理冷卻；不要把全域單processcounter當最終防線。
- [ ] 隊伍roster capability token由query string改為URL fragment在browser讀取，再經專用header送API；加入expiry/rotation，避免token進history、referrer及access logs。

**Gate P1：**乾淨database可由baseline + migrations建立；staging restore可向前及回滾；production catalog與repo migration head一致；核心API smoke及query plans無退化。

---

## P2. Supabase RLS及最小權限

目前app不是Supabase Auth。Browser只call FastAPI，committee身份來自signed cookie，因此不可直接使用`auth.uid()` policy；RLS是API權限後的第二道防線。

### P2.0 Inventory、backup及Data API perimeter

- [ ] 使用staging或production restore，不在live DB首次試policy。
- [ ] 盤點table/view/function owners、grants、policies、role memberships及runtime `current_user`。
- [ ] 確認frontend沒有anon/service/database key或direct PostgREST query。
- [ ] 如browser不需要Supabase Data API，revoke `anon`/`authenticated`對application tables/views的grants。
- [ ] Rotate任何曾出現在log、CI、分享渠道或本機history的高權限credential。

**Gate RLS-A：**anon/publishable key不能讀application rows；現有same-origin API read/write smoke仍通過。

### P2.1 建立受限runtime role

在staging建立唯一production runtime identity：

```sql
create role app_backend
  login nosuperuser nocreatedb nocreaterole noreplication nobypassrls
  noinherit password '<managed-secret>';

grant usage on schema public to app_backend;
grant select, insert, update, delete on all tables in schema public to app_backend;
grant usage, select on all sequences in schema public to app_backend;
```

Default privileges要由真正建立future objects的migration owner設定。`app_backend`不可擁有application tables，migration/emergency用另一個離線保管credential。部署health check只記錄`current_user`及`rolbypassrls=false`，不輸出URL。

**Gate RLS-B：**全部API、background cleanup、push、AI、WebSocket及export在尚未開RLS時都能以`app_backend`完成；沒有owner/superuser依賴。

### P2.2 Request-scoped trusted context

現有executor一次request可跨connection；先改為同一connection + transaction：

1. 驗證cookie後建立`request_transaction(user_id, account_status, capabilities)`。
2. 在transaction內用parameterized `set_config('app.user_id', ..., true)`等transaction-local context。
3. private schema helper只讀trusted settings，固定`search_path`並收窄`EXECUTE`。
4. 未登入public endpoint使用明確anonymous context；missing context預設deny。
5. 用connection pool交錯Member A/B至少100次，驗證context不會跨request洩漏。

**Gate RLS-C：**任何client header/body/URL都不能自行指定trusted identity；cross-user及missing-context automated tests全部deny。

### P2.3 分批policy

每批只做2–5張相依tables：policy → `ENABLE RLS` → role matrix test → `FORCE RLS` → query plan/latency → production canary。

| 批次 | Data | 方向 |
|---|---|---|
| 1 Public read | approved `topics`及真正需要公開的derived data | anonymous只讀批准rows；所有寫入deny |
| 2 Member-owned | notification、push、video progress/views/votes、bug、個人usage/consent | CRUD只限trusted user id；admin另列capability |
| 3 Committee shared | proposal/removal、ballots、comments | active committee可讀；ballot只限本人；status settlement走受控service path |
| 4 Competition/scoring | registration contact、roster token、draft/final scores | public registration只可受控insert；judge/team/admin按match capability；secret/hash columns default deny |
| 5 Highly sensitive | accounts、login、config、fund、TTS/LLM training | default deny；owner/admin最小操作；secret用column privilege或安全function封裝 |
| 6 Views/functions | activity view及所有inventory發現objects | revoke browser roles；security-invoker或移入unexposed schema，驗證無owner bypass |

Policy template必須指定role及operation，不使用`USING (true)`作捷徑：

```sql
alter table public.notification_reads enable row level security;
alter table public.notification_reads force row level security;

create policy notification_reads_self on public.notification_reads
  for all to app_backend
  using (user_id = private.current_user_id())
  with check (user_id = private.current_user_id());
```

每張表的matrix至少測anonymous、active、inactive、另一member、admin、missing context及migration runner。Policy predicate index先用`EXPLAIN (ANALYZE, BUFFERS)`量度才建立。

### P2.4 Production rollout及rollback

- [ ] 先deploy request-context code，再deploy policy；低流量change window逐批canary。
- [ ] 監察401/403/500、permission denied、p95、pool usage及unexpected empty results。
- [ ] 每批migration附drop/restore rollback；事故時回退到上一個已驗證policy version。
- [ ] `DISABLE RLS`只作有incident記錄的短暫緩解；不可把service key/BYPASSRLS放回runtime。
- [ ] 7–14日無unexpected deny後，撤銷舊高權限runtime credential及臨時grants。

**Gate RLS-D（完成定義）：**所有exposed tables都有合適RLS、runtime非owner且NOBYPASSRLS、browser無高權限key、permission matrix及pooled cross-user tests通過、views/functions無bypass、rollback已演練。

---

## P3. 讀音層：先改善現有Azure與未來自家聲

Fine-tune主要解決「似邊把聲」；讀音準確要靠測試集、G2P及字典。兩者分開驗收。

### P3.1 固定讀音測試集

- [ ] 收集100–200句，覆蓋校名、人名、辯論術語、比賽名、DSE/AI/GPT、數字、日期、百分比、金額及多音字。
- [ ] 每句記錄expected文字讀法、jyutping、重要詞及允許變體。
- [ ] 在`tts_lexicon`填term、文字`reading`、`jyutping`、例句、分類及備註；高頻長詞優先。
- [ ] 每次發現讀錯建立regression case，字典更新後跑完整集。

### P3.2 三級接線

- **T0（現況）：**長詞優先的`term → reading`文字覆寫，任何provider可用。
- **T1（推薦下一步）：**ToJyutping/PyCantonese產生初稿，再用`tts_lexicon.jyutping`覆寫指定詞；把音素餵給支援的custom model。先離線prototype，不直接改live Azure path。
- **T2（Azure專用可選）：**由同一字典生成Azure lexicon.xml/SSML phoneme；必須處理escaping、provider limit及fallback。

**Gate P3：**固定集讀音正確率達內部門檻；人名/校名/比賽名不得有critical error；同一preprocess供單人與聯機使用；DB失敗仍可用上一份bounded TTL cache。

---

## P4. 自家粵語TTS

### P4.1 Consent、主聲線及dataset

Consent／recording／review安全收口已在待部署候選；audit ledger migration `0004`仍pending。Dataset/model registry未provision，候選部署後相關API會503而不會runtime建表。

1. 只選一位願意長期授權、聲線清楚、錄音環境穩定的人作v0，避免混聲令音色及撤回責任不清。
2. 同意書明列voice cloning、內部用途、保存、雲GPU、撤回及未成年guardian安排。
3. v0收30–60分鐘accepted音訊；正式版1–3小時。每段1–60秒、單一speaker、安靜、無爆咪、稿音一致。
4. AI quality check只作預篩；管理員逐段試聽。只有accepted可export；rejected寫原因；撤回即withdrawn。
5. 在AI Training按speaker下載`recordings.json`；manifest只含metadata及短效R2 signed URLs，音訊不經Render。離線工具會逐檔下載、核對size/SHA-256及音訊metadata，再產生immutable dataset snapshot、train/validation/test split、manifest及quality report。固定test不可回流train。

2026-07-13 privacy migration已把3份舊consent視為未完成新式明確確認；使用者重新勾選voice cloning、cloud processing及適用的guardian授權前，不可再申請上載、提交錄音或進入dataset eligibility。既有148段錄音保留，但同樣受新consent gate限制。

準備工具：

```bash
python3 tools/prepare_gpt_sovits_dataset.py \
  ~/Downloads/recordings.json \
  --speaker SPEAKER_ID \
  --output-dir /srv/ai/datasets/snapshots/tts-v0 \
  --experiment speaker-yue-v0
```

Signed URLs目前只有短效期限，下載manifest後應立即執行；工具不會把URL/token寫入metadata、snapshot或terminal log，並限制manifest大小、item數、單檔及總下載bytes。較舊版本匯出的ZIP仍可用同一CLI作一次性兼容輸入。

`.list`格式為`/absolute/audio.wav|speaker|yue|文字`。Resume只可沿用同一snapshot及config，新錄音要建立新snapshot/run。

### P4.2 Base model decision

開始實驗前重新核對當時官方license、粵語support及maintainer狀態；repo不鎖死未驗證的型號。

| 用途 | 首輪候選 | 理由／風險 |
|---|---|---|
| v0 few-shot聲線 | GPT-SoVITS粵語 | 少量資料快驗證音色，可接觸G2P；CUDA-first |
| 中期質素/streaming | CosyVoice系列 | zero-shot/SFT及粵語潛力較完整；服務較重 |
| 讀音硬控fallback | MeloTTS-yue或當時可維護的phoneme-native model | jyutping控制較直接，但clone相似度通常較弱 |

不由零pretrain；不因一段demo決定上線。每個candidate記錄base commit/model digest、license snapshot、dataset ID、config、seed、metrics及artifact hash。

### P4.3 隔離workstation

目標環境是RTX 3060 Desktop + Pop!_OS NVIDIA版；正式網站永不直接連入家中workstation。

- [ ] 安裝前加密備份、核對ISO SHA-256、live USB測網絡/聲音/休眠/GPU，安裝full-disk encryption。
- [ ] 建立無sudo的`ai-train`帳戶及mode 700 `/srv/ai/{datasets,models,logs,backups,src}`；資料/API key/checkpoint不入Git或公開同步。
- [ ] 16GB RAM只做TTS/小模型；7B QLoRA前升32GB；保留至少16GB swap/zram。
- [ ] 使用rootless Docker + NVIDIA Container Toolkit；所有WebUI/API只bind `127.0.0.1`。
- [ ] Container建議`mem_limit: 12g`, `shm_size: 4g`；OOM先batch降1，不取消上限硬頂。
- [ ] 記錄GPT-SoVITS commit及container image digest；GPU smoke必須由host及container都見到同一GPU/VRAM。

核心smoke：

```bash
docker run --rm --gpus all nvidia/cuda:12.6.3-base-ubuntu24.04 nvidia-smi
docker run --rm --gpus all pytorch/pytorch:2.7.0-cuda12.6-cudnn9-runtime \
  python -c 'import torch; print(torch.cuda.is_available(), torch.cuda.get_device_name(0))'
```

正式操作準則：更新driver/image前checkpoint及backup；每run只保留best、last、release；ready snapshot/model複製到加密外置碟並核對hash。撤回資料時block所有引用snapshot/checkpoints，重新export及重訓。

### P4.4 評估及rollout

每個candidate至少保存：ASR CER、讀音正確率、3–5人盲聽MOS、speaker consistency、first-audio latency及failure rate。比較同一批句子的Azure baseline。

| 階段 | 使用位置 | 升級條件 |
|---|---|---|
| P0 | 管理員離線A/B及預生成 | 音色、授權、critical讀音先過關 |
| P1 | 非即時示範音／固定內容 | 穩定、cache/lifecycle/成本可控 |
| P2 | 單人Free Debate/Mock | GPU model常駐、first audio約1秒內、timeout自動fallback Azure |
| P3 | 聯機多人廣播 | end-to-end latency及uptime更嚴；房內所有人不會因一個慢request長等 |

Custom endpoint必須TLS、authenticated、rate/size/time bounded；實作`_synthesize_custom`後仍保留Azure default/fallback。Render 512MB不載model；正式推理一定是獨立受認證GPU service。

**Gate P4：**30–60分鐘accepted單speaker data；可重現v0 checkpoint；固定測試優於/接近baseline；撤回演練成功；P0/P1穩定後才討論即時切換。

---

## P5. 自家辯論LLM：RAG first，LoRA last

### P5.1 資料及固定eval

Production eval/RAG tables現時刻意不存在；候選部署後readiness會回`eval_provisioned:false`，advanced endpoints fail-closed，AI Coach不會為未啟用RAG白做embedding。

- [ ] 整理有權使用且已匿名化的規則、評分準則、術語、優秀稿、評語、攻防、主線及逐字稿。
- [ ] 只有accepted submissions進dataset/RAG；撤回會令所有derived chunks/snapshots失效。
- [ ] 固定20–50個versioned eval cases，覆蓋發言評核、主線、追問、反駁、Mock評語及引用規則；每個case保存內容checksum，更新內容必須升eval-set version。
- [ ] Rubric量度：香港粵語自然、事實/引用準確、引用到user內容、具體改善建議、非空泛、無敏感資料。
- [ ] 先定義受控eval worker及`run_id` contract；所有prompt/provider/model/RAG改動都對同一不可變eval set跑，保存版本、latency、tokens、cost及盲評。完成前不建eval tables。

### P5.2 RAG v0

1. Accepted source → immutable snapshot → document/chunk；保留source id、consent/version及hash；一個submission只可有一個document。
2. Index只包含approved資料；reindex由單一受控job持lock，可重跑及原子切換，寫回前重新核對source仍accepted，不在request同步重建。
3. Retrieval先使用簡單可解釋baseline（metadata filter + text/vector search），保存top-k引用及分數。
4. 回答必須區分source內容與模型推論；無可靠context時明示不知道，不捏造賽規。
5. 以eval比較prompt-only vs RAG；只有品質有實質提升才接入更多功能。
6. Schema只存pgvector embedding；撤回／拒絕會在transaction內delete document並靠FK cascade即時清chunks，不保留JSON vector或重複chunk metadata。

### P5.3 是否需要fine-tune的決策gate

只有RAG + prompt已穩定，且eval仍顯示一致的風格/格式/粵語能力缺口，才做LoRA/QLoRA：

- 原始逐字稿不可直接當training pair；先整理成高質instruction/chat pairs並human review。
- 選當時粵語/中文能力合適的open-weight base，核對license、context、quantization及serving成本。
- 訓練租CUDA最合理；Mac/Apple Silicon主要供本地推理/RAG dev，不作CUDA-first fine-tune假設。
- 先3B/4B baseline；7B QLoRA workstation至少32GB RAM。是否買本地機取決於長期self-host inference，不能以訓練需要作購買理由。
- Serving必須在獨立endpoint（如當時合適的vLLM/MLX service），接現有provider abstraction；保留外部provider fallback。

**Gate P5：**固定eval可重現；RAG引用準確及撤回可傳播；fine-tuned candidate在同一盲評顯著優於RAG baseline，且latency、RAM、GPU/API成本及維運負擔可接受，才production canary。

---

## P6. 長期架構收斂

### P6.1 512MB RAM最大lever：移除web runtime Pandas依賴

2026-07-13 audit基線有21個Python檔直接import Pandas：8個production code、13個tests；今次候選已刪除`core/media_logic.py`一個未使用import，變成20個／其中7個production。Tests已由`.dockerignore`排除，數檔案本身亦不等於重複佔RAM；真正問題係正常request首次進入`RuntimeDb.query()`便會載入一次Pandas。相同本機venv量度empty Python peak約15MB、只做`import pandas`約104MB，即約多83MiB；Render Linux要另以同一release前後實測，唔直接把本機數字當production保證。

1. [ ] 先記錄Render cold start、首頁後、投票／評分／管理頁主要workflow後的RSS、p95及DB rows/response parity；加subprocess import gate，證明app startup本身不載Pandas。
2. [ ] 在`RuntimeDb`加入`fetch_all`／`fetch_one`／`fetch_scalar`等SQLAlchemy `RowMapping` primitives；新code不再依賴`.empty`、`.iloc`、`.to_dict()`等DataFrame contract。
3. [ ] 按bounded domain遷移：auth/config/home → vote/open-db → registration/judging/results → AI/media；每批保持query count、JSON shape、timezone/decimal/null serialization及pagination tests。
4. [ ] `score_sheet_pdf`及真正tabular export/離線analysis另行評估；可直接用rows就移除Pandas，確實需要才lazy import並移出正常web request path。Tests可保留Pandas至production contract完成。
5. [ ] 最後令production image/runtime dependency不再因正常workflow載入Pandas/NumPy；比較Render peak/steady RSS及cold-start，確認無response/p95退化才移除compatibility adapter。

**Gate P6-RAM：**startup及主要線上workflow均不載入Pandas；功能／query-count測試全過；Render同一負載下RSS有實測下降並保留充足512MB headroom。唔以單純「import檔案由21變0」代替production量度。

### P6.2 拆細`deploy/proxy.py`

Audit基線約3,870行，今次候選約3,888行，係唯一明顯過大的架構檔；其中page-serving區已有36個GET decorators／25個handlers，多數只係path alias、登入gate、cache header及`FileResponse`差異。

1. [ ] 先把純page-serving routes收斂成一個可審核的dict-driven route table；route spec要明列aliases、auth/maintenance gate、HTML file及cache policy，startup註冊，不用一個catch-all吞掉API/asset路徑。
2. [ ] 抽出Gemini relay module：先搬token/signature helpers及token mint/page接線，再搬`/gemini-live` WebSocket relay；保持origin、signature expiry、frame/byte/time limits、quota及bandwidth accounting完全一致。
3. [ ] 之後逐個搬static/projector router、TTS service、resource accounting；multiplayer room/Gemini orchestration最後獨立一個bounded slice，唔同relay一次大搬。
4. [ ] 最後只留app factory、middleware、router registration及lifespan；每一步獨立commit，可單獨rollback，唔同功能重寫混在一起。

**Gate P6-PROXY：**所有route/alias及OpenAPI parity、HTML cache/security headers、登入/maintenance行為、WebSocket close/error、quota及bandwidth tests全過；Render單workerLive/room smoke無退化後才進下一個slice。

### P6.3 其餘長期工作

- [ ] 把process-local room/admin session/cache搬到Redis或database前，Render維持單worker；完成後才評估多worker/多instance。
- [ ] Custom TTS provider失敗後加入短TTL circuit breaker/cooldown；冷卻期直接走Azure fallback，避免每個request先重複等同一個timeout。
- [ ] `video_views`只保留近期去重所需raw events，另以durable aggregate保存歷史count；先設計backfill及核數，再加retention，避免永久event rows令aggregate愈來愈貴。
- [ ] AI model availability及價格在release前以官方provider資料核對，改成帶生效日期的versioned pricing metadata；不要讓過期硬編碼影響基金核數。
- [ ] R2 exact-usage refresh若擴至多worker，以DB advisory lock或獨立排程協調；process-local lock只適用現行單worker。
- [ ] 遲到基金通知先一次batch讀取所有target subscriptions，再用有上限並行發送及batch更新結果，避免逐會員/逐裝置同步DB與network N+1。
- [ ] 公開`/api/home/status-check`加短TTL cache及IP rate limit，或移入developer gate；避免重複aggregate及披露不必要的營運數字。
- [ ] 為bug reports建立resolved archive/retention；達總量上限時先清理已關閉舊資料，不令新critical report完全無法提交。
- [ ] 將login lifecycle/audit的best-effort broad exceptions改為不含secret的structured warning/metrics，保持登入可用同時讓失敗可觀察。
- [ ] 為Python production dependencies建立可重現lock/constraints及定期升級流程；Render仍用Python 3.11時，CI／release gate至少要用同一major/minor重建乾淨環境，避免未pin套件令build結果漂移。
- [ ] 收斂frontend shared shell/table/form helpers，但維持原生HTML/CSS/JS及可讀source，不引入超出團隊維護能力的build stack。
- [ ] `/open_db`及`/vote/depose-data`改為server-side search/pagination，不再每次把最多2,000題整個topic bank送到browser；保留圖表aggregate及抽取候選題所需的最小fields。
- [ ] 聯中自由辯論若每方各10分鐘，「雙方」segment會超過目前780秒Live session budget；先決定拆成兩個可handoff segment或提高經量度的budget，再改room protocol及成本上限。
- [ ] 由賽制負責人確認敗方賽是否只應包含第一輪敗方；確認前保持現行結果並加入fixture，確認後才改draw algorithm。
- [ ] 每季做一次table/index/retention/R2 orphan/dependency audit；報告只保存結論及可重現query，不累積過期migration diaries。

## 每次完成一階段要記錄甚麼

在相關PR/release記錄：目標、production/staging基線、改動migration/code、測試命令及結果、資源前後差異、security/privacy影響、rollback、owner及下一步。不要另開散落的「暫時計劃」文件；更新本roadmap的checkbox與日期即可。
