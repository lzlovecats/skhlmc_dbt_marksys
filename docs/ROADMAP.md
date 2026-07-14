# 統一後續路線圖：Live、Database、Security、自家 TTS 及辯論 LLM

> 最後核實：2026-07-14（Asia/Hong_Kong）。本文件是 repo 唯一的未來工作計劃；已完成工作的細節以 Git history、migration ledger 及 release log 為準，不再在 docs 累積 migration diary。

## 使用原則

- 一次只推進一個有明確 rollback 的 gate；database 權限、破壞性 schema、模型及 production runtime 不在同一批改。
- Production schema 只可由 immutable baseline 加 versioned migrations 演進。已套用的 migration 不刪、不改、不 squash。
- 任何 media、consent 或 training derived data 清理，先證明 backup／撤回路徑，再做破壞性操作。
- 新功能未有完整 schema bundle、permission、retention、audit 及 rollback 前一律 fail closed。
- 完成項目只在本文件保留一行現況；詳細過程不另開臨時計劃文件。

## 已核實 production 基線

| 範圍 | 2026-07-14 現況 |
|---|---|
| App | Render production 已運行 `4.2.1`；maintenance mode 關閉 |
| Database | Supabase PostgreSQL 17.6，Singapore pooler；未發現獨立 staging database |
| Migration | Head `20260714_0002`；pending、gap、unknown version、name/checksum mismatch 全部為 0 |
| Catalog | 46 public tables（45 application + `schema_migrations`）、0 production-only tables、0 RLS |
| Canonical checksum | `eb25cd2eeb7291ade3fcd2d84dd851baea8db67a7039757d21fcc644a8907599`（2026-07-14，`system_config`退役後）；reconciliation 0 drift、0 runtime DDL site（budget已收緊至0） |
| Migration files | `baseline.json`加6對已freeze up/down，共13個正式檔案；全部永久保留 |
| R2 media | 193 DB rows、238 objects、122,687,464 bytes 已經 HEAD size/hash/MIME/metadata 核對；新讀寫為 R2-only |
| Legacy media | `20260714_0001`已永久移除`tts_voice_recordings.audio_data`及`match_photos.image_data`；rows及R2 objects post-check無損 |
| Config | typed `app_config` 23 keys；`system_config`、read bridge及startup config migration已隨`20260714_0002`退役；developer password已bcrypt，admin／SQL password rotation仍待做 |
| Security | Runtime仍使用可 `BYPASSRLS` 的 `postgres`；46 public tables均未開RLS，legacy `anon`／`authenticated` grants仍在 |
| Intentional future schema | Dataset/model、eval及RAG共7張表刻意未建立；未 provision 的 endpoint應明確503 |

`tg_notification_queue`已於2026-07-14隨舊Telegram Cloudflare Worker一併移除（0 rows；table從不屬於repo schema或migration catalog，ledger不受影響）；reconcile確認0 production-only tables。Repo本身沒有需要再刪的「舊 migration 大堆檔案」。

`4.2.1`已部署並由production public API確認版本；登入後完整workflow smoke仍屬持續release checklist。

Repo release `4.2.2`已完成AI Coach／resource-limit收尾（尚未deploy）：Solo Live改為
browser直連Google，加入HK地區gate、受約束ephemeral token、context compression、
resumption／GoAway、Mock JIT token、原子quota reserve、Free雙方各10分鐘及30分鐘overall
deadline；同批補齊request-body／WebSocket queue、錄音probe、provider response、push及cache
邊界，並修正AI Training同版本immutable JS造成的白頁。Developer可為指定委員設定
有期限的Solo個人次數豁免；全系統月限及安全gate不受影響。沒有改動database schema或
migration catalog。普通AI及多人Live仍經Render。

---

## 未完成工作盤點

以下只列尚未完成的未來能力；已達成項目留在後文作audit trail。直接面向委員的功能是
P0聯機Live direct media、P3粵語讀音、P4自家TTS、P5自家辯論LLM及P6大型資料搜尋；
其餘項目是這些功能安全上線所需的運維、資料庫及runtime基礎。

| Phase | 未來能力／結果 | 類型 | 現況及下一個gate |
|---|---|---|---|
| **P0** | 聯機Live實時音訊media plane離開Render | 使用者功能／成本 | 未開始；先完成架構、威脅、香港網絡及成本gate，再developer-only prototype |
| **P0** | `4.2.2`授權deploy後smoke、admin／SQL credential rotation | 運維／安全 | Repo已ready，production仍是`4.2.1`；本roadmap不授權deploy或secret變更 |
| **P1** | 可重現staging／空DB、schema cleanup、不可變motion ID、時區及roster token收口 | 資料庫 | 先完成staging restore及migration replay |
| **P2** | Supabase最小權限、trusted request context及全面RLS | 安全 | Runtime仍使用`BYPASSRLS`角色，需分批canary |
| **P3** | 粵語讀音回歸集、字典、G2P及provider-neutral preprocessing | 使用者功能 | 未建固定eval corpus，以現有Azure path做baseline |
| **P4** | 成人授權語料snapshot、自家粵語TTS、獨立authenticated inference及Azure fallback | 使用者功能／模型 | Dataset／model registry未provision，現階段fail closed |
| **P5** | 固定eval、辯論RAG，達gate後才考慮LoRA／QLoRA | 使用者功能／模型 | Eval／RAG schema未provision，現階段fail closed |
| **P6** | 移除web runtime Pandas、拆細`proxy.py`、control-plane多worker state、TTS circuit breaker、retention、server-side search／pagination及dependency lock | 效能／可維護性 | 按可獨立rollback的小步驟進行 |

---

## P0. 聯機Live direct media、部署後收尾及repo整理

### P0.1 聯機Live實時音訊不經Render（未開始）

**現況：**Mode A真人對真人的microphone PCM經`/room/{code}`由Render fan-out；Mode B
多人對AI由Render持有每房唯一Gemini Live socket，在Free／Mock自由辯論段轉發人聲，再廣播
Gemini／Azure／custom TTS audio；Mock其餘段落主要由browser transcript作text cue。Room state
仍是single-worker process-local。

**目標邊界：**「不經Render」專指連續實時media plane：microphone PCM、peer audio、
Gemini input/output及Live TTS audio payload都不進入Render。Render繼續作低流量control
plane，負責登入、房間權限／roster／role、precheck、原子quota reserve、authoritative
timer／turn／deadline、短期憑證、WebRTC signaling、audit、transcript metadata及評判。
普通AI、database及整個網站不會因此搬離Render。

**候選架構：**Mode A優先驗證browser-to-browser WebRTC，只在NAT／firewall需要時使用有
預算上限的managed TURN。Mode B production預設由trusted bot／edge media agent持有Gemini
context及受控publish權限，room transport可選用managed SFU；任何時刻每房最多一個active
Gemini connection，Mock沿用現有session partition作JIT。普通委員browser coordinator只可用作
prototype；除非另行接受較弱
trust model，它不得進production。每位房員各自建立Gemini session會令context分叉及成本
倍增，明確不採用。

#### Phase 0 deliverables

1. [ ] **Baseline及ADR：**`4.2.2`獲授權deploy後，分開量度Mode A、Mode B Free及完整Mock的
   Render ingress／egress、p95 latency、WebSocket close、reconnect、RAM及provider cost；比較
   WebRTC mesh + TURN、managed SFU + trusted bot／media agent、獨立edge agent及classic browser
   coordinator。量度必須使用固定人數、時長及scripted audio duty cycle的可重現fixture，ADR必須記錄香港／
   VPN路徑、ICE地址privacy、資料地域、retention、vendor outage、月費／egress及rollback；
   未通過gate前不購買或鎖定任何新vendor。
2. [ ] **Protocol及信任邊界：**定義authenticated SDP／ICE signaling、room/member capability、
   media-agent epoch／lease、active-speaker、host election／stale-host fencing及短期TURN／SFU credential。
   實作時重新核對Google官方ephemeral token、supported regions、GoAway、session resumption及價格；
   長期provider key不得進browser；raw IP不得進URL、log或持久儲存。ICE candidate只可在
   authenticated、short-TTL signaling內轉發且不記log，ADR另行比較mDNS及relay-only privacy及成本。如候選
   topology必須在browser續接，resumption handle只可在當前trusted coordinator記憶體內短暫保留，
   不可放入URL、room signaling、log或持久儲存。Mode A必須有room/member-bound media
   identity、單一audio track／bitrate上限、signed fenced turn grant及TURN per-room/member quota；如P2P
   vendor無法對hostile member強制，必須改用SFU／edge enforcement或由owner明確接受Mode A較弱
   threat model並同步修改本gate／release disclosure，不可仍聲稱server-authoritative media enforcement。
3. [ ] **Developer-only prototype：**Mode A先以WebRTC完成真人audio；Mode B使用唯一trusted
   edge coordinator直接連Gemini，再經新media plane向房員發布AI audio。先以fake provider及
   browser E2E測Mode A兩人及Mode B當時server-authoritative容量，覆蓋Wi-Fi、流動網絡、學校／
   嚴格firewall、TURN-only、香港／VPN、tab sleep、device switch、GoAway、Mock JIT session及
   coordinator失效。Fake測試通過後，另獲授權才做cost-capped真實Google spike，實測短期token、
   GoAway／resumption、edge出口及supported region。
4. [ ] **Security及成本收口：**以fenced、idempotent saga處理跨系統狀態：DB transaction在
   advisory lock內reserve quota及指定media-agent epoch，外部provisioning使用idempotency key；失敗要
   compensation／expiry，向client披露credential前再重驗roster及active epoch。Token／lease expiry不得超過
   server計算的absolute deadline。任何時刻每房最多一個active provider connection；race不得造成
   額外mint、並行生成或重複扣quota；fenced failover／Mock JIT可有一枚可追蹤、有成本上限的
   replacement token。Signaling／control／transcript均有byte、message-rate、TTL及reconnect-stable
   limits。新media成本以每房worst-case reservation及月度dashboard reconciliation執行hard gate；
   Render tracker仍記錄control／signaling，但不代表TURN／SFU／edge成本。
5. [ ] **功能parity：**保留Mode A、Mode B Free／Mock、現有房間容量／roster／position、開始順序、
   timer／bells／turn、empty grace／reconnect、transcript、評判及member-only result。同一
   coordinator process的graceful GoAway可使用記憶體handle續接；任何coordinator異常crash後先
   fence舊epoch，handle遺失時只可由server-held prompt／transcript重開或安全結束，不得宣稱
   seamless context handoff，不得重複扣quota或並行生成。Azure／custom TTS若由edge發布，
   edge secret storage／rotation／least privilege及provider egress accounting必須先通過gate；否則
   direct mode只用Gemini native audio，不可暗中fallback經Render binary relay。
6. [ ] **Canary及退役：**以feature flag按developer allowlist依次開Mode A → Mode B Free →
   Mode B Mock，保留legacy relay rollback，量度及演練rollback。穩定觀察窗口通過後才刪
   Render PCM fan-out、room Gemini socket、native audio buffer、TTS audio fan-out及relay-media-specific
   accounting；保留Render control／signaling bandwidth accounting及現有global protection gate，再更新
   README、Services、limits及測試。

**Gate P0-LIVE-CANARY：**Direct-mode room channel不收發PCM、Gemini `inlineData`、`tts_audio`或
其他media bytes；以ADR鎖定的固定人數／時長／scripted-audio fixture比較Live-path bytes，Render較
baseline下降至少95%且control frame有絕對上限。Mode A、Mode B Free及Mode B Mock通過
desktop Chrome、Android Chrome、iOS Safari及至少一個TURN-only網絡。預設edge-agent
方案必須保留現有香港不需VPN的Mode B路徑；如候選方案要求VPN，必須由owner另行明確
接受功能倒退，而且在reserve quota或簽發token前檢查及提示。同一房在任何時刻無
split-brain、雙provider session、雙AI audio或重複扣數；server仍權威執行roster、turn、時限、
room TTL及安全結束。長期secret不入browser，新media service有cost ceiling及fail-closed gate；
legacy relay可在feature flag後作rollback，但developer canary的direct path必須已經zero-media-through-Render。
固定fixture下的peer one-way latency、AI first-audio latency、setup成功率、packet loss及重複音訊率
均要達到ADR在開發前鎖定的QoS threshold，不可只靠media能夠連線就過gate。

**Gate P0-LIVE-RETIRE：**全流程parity、真機／真網絡canary、成本上限及rollback演練通過，
再經穩定觀察窗口後，由P0刪除legacy media relay及專屬accounting；全局room WebSocket不再
接受media frame，但保留有上限的control／signaling及其Render bandwidth accounting。

**非目標：**Phase 0不放寬現有人數、次數、時長或concurrent-room限制，不新增音訊
儲存，不讓browser直連Supabase Data API，不同時做Redis／multi-worker。新vendor、production
service、database migration及正式cutover都需另行授權；此roadmap更新本身不改production行為。

### P0.2 Deploy後workflow smoke

- [x] `4.2.1` deploy後核對production version及public home health（2026-07-14再核對6個公開頁）。
- [x] 2026-07-14已對`4.2.1`核對登入、投票、評分、AI Coach、Live、media及AI Training audit；無新增DB pool、5xx、WebSocket close或quota異常。
- [ ] `4.2.2`只在獲明確deploy授權後上線；上線後重做version／health、AI Training非白頁、
  新Solo quota、developer個人豁免、HK／VPN、Solo browser-direct Free／Mock及現有多人relay smoke，
  並核對adult-only consent行為沒有改變。

**Gate P0-A（`4.2.1`達成 2026-07-14）：**主要登入workflow已smoke，production無新增5xx、DB pool、WebSocket或quota異常。`4.2.2` deploy後smoke仍是open item。

### P0.3 Cloudflare R2及舊BYTEA

- [x] 已在Cloudflare Dashboard確認bucket保持private，application token只限該bucket（2026-07-14）。
- [x] CORS已核對：正式origin、`GET`／`HEAD`／`PUT`、content／cache／SHA metadata headers及expose `etag`（2026-07-14）。
- [x] Lifecycle已設定只對`pending/`於2日後清理；不得擴至`photos/`或`audio/tts/`。
- [x] 已用真實browser抽查相片及錄音上載、播放／下載；沒有CORS、403或signature mismatch（2026-07-14）。
- [x] R2 backup已建立並演練還原（2026-07-14）；media destructive cleanup解封，但每次仍需獨立irreversible approval。
- [x] Final verification保存193 rows／238 objects／122,687,464 bytes摘要，post-drop再次全量通過。
- [x] `20260714_0001`已原子刪除兩個legacy BYTEA columns；是否`VACUUM FULL`另排maintenance及鎖表評估。
- [x] 一次性media migrator、finalizer及過渡runbook已退役；保留日常orphan cleanup。

**Gate P0-B：**2026-07-14已按明確irreversible approval完成BYTEA drop；ledger、193 rows、238 objects及新checksum均post-check通過。Down SQL不能重建已刪binary，外部backup應保留至完整browser觀察窗口完結。

### P0.4 清理舊Telegram Cloudflare資源

- [x] 已移除舊`skhlmc-telegram-worker`的routes/custom domains、cron triggers、Hyperdrive binding及Worker secrets；現役R2資源保持不動（2026-07-14）。
- [x] 已撤銷舊Telegram webhook及該Worker專用database credential（2026-07-14）。
- [x] `tg_notification_queue`（0 rows）已於2026-07-14確認無外部writer後移除。此table從不在repo schema或migration catalog內，故在ledger外直接DROP，事後reconcile為0 drift；catalog checksum已更新至上方基線。所有屬於repo catalog的table仍然只可經versioned migration改動。

**Gate P0-C（達成 2026-07-14）：**Cloudflare及Telegram均無舊依賴，production-only table已清零，reconcile無drift。

### P0.5 Typed config及文件收斂

- [ ] Smoke全部settings/login workflow，rotate admin及SQL password為bcrypt；按登入失效窗口決定cookie secret rotation，developer password只需核實現有bcrypt。
- [x] `system_config`已由`20260714_0002`退役（owner批准免release觀察窗口）：audit確認22 keys全部已在`app_config`、0 fallback-only；migration up有fallback-only precondition，down由`app_config` backfill重建bridge table，屬可rollback。Read bridge、startup `migrate_legacy_config`及runtime DDL site已一併移除；已部署的`4.2.1`bridge code對missing table fail-safe，production drop後smoke正常。
- [x] `migrate_app_config.py`及`audit_app_config.py`已隨bridge一併刪除；reconcile runtime DDL budget已收緊至0。
- [x] `docs/`只保留本Roadmap及`SERVICES_COSTS_AND_LIMITS.md`；current architecture留在README，deploy／limits／Cloudflare／R2操作已合併入Services。
- [ ] 保留網站runtime實際讀取的user manual、rules及通知／主席templates；appliance停止支援時才連其README一併退役。

**Gate P0-D（config／文件subgate達成 2026-07-14）：**production不再讀legacy config（`system_config`已刪，typed store係唯一來源），repo只有一份roadmap，其他文件各有單一職責。P0仍有admin／SQL password rotation未完成。

---

## P1. Database schema瘦身及migration紀律

### P1.1 建立可重複的staging gate

- [ ] 從production backup建立隔離staging restore；保存schema-only dump、exact row counts及`audit_db_schema.py`輸出。
- [ ] 每個非additive migration在staging重播forward／rollback，核對constraints、grants、query plan、row preservation及checksum。
- [x] CI（GitHub Actions `ci.yml`）以`manage_db_migrations.py lint`做offline catalog檢查：orphan/stray file、duplicate version、未配對或空SQL、內嵌transaction control及browser-privilege revoke。history gap、unknown version及checksum drift屬ledger-side，照舊由operator以`status`對target database執行。
- [ ] 定義新空database的正式可重現流程：短期可由`schema.init_db()`建立current-head schema，再以完整catalog checksum核對及受控stamp current head；或者另建真正可執行的clean-database baseline。現有`baseline.json`只有metadata，未完成此流程前不得聲稱可由baseline重建tables。
- [ ] 有ledger的database永遠禁止`schema.py` bootstrap或runtime DDL；後續只可套用未執行的versioned migrations。

### P1.2 Schema backlog

- [ ] 清理`score_drafts`、`scores`及`match_roster_links`已證明重複的constraint／index；刪前先用catalog及`EXPLAIN`確認。
- [ ] Topic/removal motion改用不可變`motion_id`，歷史資料backfill後先轉FK及API。
- [ ] `scores.submitted_time`及`ai_fund_usage_logs.created_at`遷移為`TIMESTAMPTZ`；無法證明舊row時區時標記unknown，不猜測。
- [x] 2026-07-14 audit：所有binding quota消費路徑（prepare-live、solo live reserve、聯機房daily/monthly、R2 upload intents、LLM training submissions、video comments）均已在transaction內advisory lock或以unique constraint/`ON CONFLICT`收口；無鎖的`COUNT`只屬advisory pre-check，實際消費一律在鎖內重驗。video view屬dedupe非quota，push device屬self-trimming，不需鎖。
- [ ] Roster capability token由query string移到fragment +專用header，加入expiry／rotation。
- [ ] 建立未來AI schema共用migration/security contract：每個bundle要有version marker、permission、retention、withdrawal propagation及rollback；實際dataset/model、eval及RAG交付只分別由P4／P5追蹤，不在P1重複計作功能checkbox。

### P1.3 保留／移除工具的界線

- 長期保留：`manage_db_migrations.py`、`core/db_migrations.py`、schema audit／reconcile、R2 orphan cleanup及dataset preparation。
- 過渡工具已全部退役（media migrator/finalizer及typed-config migration/audit工具均已刪除），runtime DDL reconcile budget為0，杜絕日後誤跑舊式`ALTER ... IF NOT EXISTS`造成unmanaged drift。
- 現有13個`migrations/`正式檔案永久保留；將來每個cleanup自然增加一對up/down，檔案變多不等於repo不乾淨。

**Gate P1：**staging restore可逐步升級；新空database可由已驗證current-head bootstrap +受控stamp或真正executable baseline重現；production catalog與ledger head一致；runtime沒有DDL；所有非additive migration可rollback或有明確irreversible approval。

---

## P2. Supabase RLS及最小權限

現時browser只call FastAPI，身份來自signed cookie，不能直接套用`auth.uid()`。先換runtime role及request context，再逐批開policy。

### P2.1 收窄database perimeter

- [ ] 確認frontend沒有Supabase direct Data API依賴；revoke `anon`／`authenticated`對application tables、views及sequences的legacy grants。
- [ ] 建立非owner、`NOSUPERUSER NOBYPASSRLS NOINHERIT`的`app_backend` login；migration/emergency credential離線保管。
- [ ] Render只使用`app_backend`；health check核對`current_user`及`rolbypassrls=false`，不輸出connection URL。

### P2.2 Trusted request context

- [ ] 每個request固定同一connection + transaction，以`set_config(..., true)`寫入已驗證的user id、account status及capabilities。
- [ ] Missing context預設deny；任何header/body/URL都不可自行指定trusted identity。
- [ ] 用connection pool交錯兩個會員至少100次，證明context不會跨request洩漏。

### P2.3 分批RLS

1. Public approved read。
2. Member-owned notification、progress、usage、consent。
3. Committee proposals、ballots、comments。
4. Competition registration、roster、draft/final scores。
5. Accounts、config、fund、TTS／LLM training等敏感資料。
6. Views／functions及owner-bypass audit。

每批只做2–5張相依tables，順序為policy → `ENABLE RLS` → role matrix → `FORCE RLS` → query plan → production canary；至少測anonymous、active、inactive、另一member、admin、missing context及migration runner。

**Gate P2：**runtime非owner且NOBYPASSRLS；browser role不能直讀application rows；所有exposed relations都有permission matrix、pooled cross-user test及已演練rollback。

---

## P3. 讀音層：先改善現有Azure與未來自家聲

Fine-tune主要改善聲線；讀音準確要靠固定測試、G2P及字典，兩條工作線分開驗收。

### P3.1 固定讀音測試集

- [ ] 收集100–200句，覆蓋校名、人名、辯論術語、比賽名、DSE／AI／GPT、數字、日期、百分比、金額及多音字。
- [ ] 每句保存expected文字讀法、jyutping、重要詞及可接受變體；每次讀錯加入regression case。
- [ ] 在`tts_lexicon`以長詞優先維護term、reading、jyutping、例句、分類及備註。
- [ ] 建立provider-neutral preprocess測試，單人及Live共用；DB暫時失敗時使用上一份bounded TTL cache。

### P3.2 三級接線

- **T0：**繼續使用`term → reading`文字覆寫，所有provider可用。
- **T1：**離線prototype粵語G2P，再由`tts_lexicon.jyutping`覆寫；先接custom model，不直接改live Azure path。
- **T2：**如Azure lexicon／SSML phoneme確有穩定收益，才由同一字典生成provider-specific輸出及fallback。

**Gate P3：**固定集達內部正確率門檻；人名／校名／比賽名沒有critical error；同一輸入在單人、Live及custom provider結果一致。

---

## P4. 自家粵語TTS

Consent、R2錄音、review及accepted export已存在；dataset/model registry仍刻意未provision。

### P4.1 Dataset contract

- [ ] 先選一位有長期授權的主聲線；v0收30–60分鐘accepted單speaker音訊，正式版再擴至1–3小時。
- [ ] 只接受已成年委員同意，明列voice cloning、內部用途、雲GPU、保存及撤回；不新增未成年／guardian flow，撤回立即令引用snapshot/model blocked。
- [ ] 只有accepted錄音可進immutable snapshot；保存source/hash/consent version、固定train/validation/test split及quality report，signed URL／token不得落盤。
- [ ] 以versioned dataset/model bundle建立`ai_dataset_snapshots`、items及`ai_model_versions`；正式啟用前先完成withdrawal outbox/cascade及artifact hash驗證。

### P4.2 模型實驗

- [ ] 實驗開始時重新核對當時官方license、粵語support及maintainer狀態，不在roadmap鎖死過期model版本。
- [ ] 首輪以少量資料可驗證的粵語voice-cloning模型做baseline；另設phoneme-native候選檢驗jyutping硬控。
- [ ] 記錄base commit/model digest、license snapshot、dataset ID、config、seed、metrics及artifact hash；不由零pretrain。
- [ ] RTX 3060 workstation只供隔離訓練；full-disk encryption、無sudo training account、rootless container、localhost bind及加密backup全部先完成。

### P4.3 評估及rollout

- [ ] 與同一批Azure baseline比較ASR CER、固定集讀音、盲聽MOS、speaker consistency、first-audio latency及failure rate。
- [ ] 先管理員離線A/B，再預生成固定內容，再單人即時；多人Live最後先試。
- [ ] Custom inference必須是獨立TLS/auth/rate-bounded GPU service；Render 512MB不載model，Azure保留default/fallback。

**Gate P4：**可重現snapshot/checkpoint、固定集接近或優於baseline、撤回演練成功、P0/P1穩定；達標前不接production即時TTS。

---

## P5. 自家辯論LLM：RAG first，LoRA last

### P5.1 資料及固定eval

- [ ] 只收有權使用、已匿名化且accepted的規則、評分準則、術語、優秀稿、評語、攻防、主線及逐字稿。
- [ ] 建立20–50條不可變versioned eval cases，保存content checksum；覆蓋評核、主線、追問、反駁、Mock評語及引用規則。
- [ ] Rubric量度香港粵語自然度、事實／引用準確、具體改善建議、非空泛及無敏感資料。
- [ ] 先定義受控eval worker及`run_id`；保存prompt/provider/model/pipeline/RAG versions、latency、tokens、cost及盲評結果，再以versioned bundle建立eval tables。

### P5.2 RAG v0

- [ ] Accepted source → immutable document/chunks；一個submission一個document，保留source id、consent version及hash。
- [ ] Index只含approved資料；reindex以單job lock、可重跑及原子切換，寫回前再次確認source仍accepted。
- [ ] 先比較prompt-only與簡單metadata/text/vector retrieval；回答分開source內容與推論，context不足時明示不知道。
- [ ] Schema只存一份pgvector embedding；撤回／拒絕在transaction刪document並cascade chunks，不保留JSON-vector fallback。
- [ ] RAG bundle未有marker、完整schema及permission前保持503，亦不可白做付費embedding。

### P5.3 Fine-tune決策gate

- [ ] 只有RAG + prompt已穩定，而固定eval仍有一致風格／格式／粵語缺口，才整理human-reviewed instruction pairs做LoRA/QLoRA。
- [ ] 先小模型baseline；選型時重新核對license、context、quantization、serving成本及當時硬件需要。
- [ ] Serving在獨立authenticated endpoint，接現有provider abstraction並保留外部provider fallback。

**Gate P5：**eval可重現、RAG引用準確、撤回可傳播；fine-tuned candidate在同一盲評顯著優於RAG baseline，而且latency、RAM、GPU/API成本及維運負擔可接受，才production canary。

---

## P6. 長期架構收斂

### P6.1 512MB RAM最大lever：移除web runtime Pandas

原始audit找到21個Python檔直接import Pandas；移除一個unused import後，目前仍有20個；
2026-07-14重新核對production路徑仍恰好7個。真正問題不是檔案數，而是正常request
第一次進`RuntimeDb.query()`會把Pandas／NumPy載入，曾在同一開發環境量到約80 MiB級
額外peak；正式收益以Render同release前後量度為準。

1. [ ] 記錄cold start、首頁、投票／評分／管理頁後的RSS、p95、query count及response parity。
2. [ ] 在`RuntimeDb`加入SQLAlchemy `RowMapping`形式的`fetch_all`／`fetch_one`／`fetch_scalar` primitives；新code不再建立DataFrame contract。
3. [ ] 按auth/config/home → vote/open-db → registration/judging/results → AI/media分批遷移，保留timezone、Decimal、null、pagination及JSON shape測試。
4. [ ] PDF／真正tabular export如仍需要Pandas，只可lazy import並移離正常web request；tests可最後處理。
5. [ ] 主要workflow不再載入Pandas後，比較Render peak/steady RSS及cold-start，再決定是否由production dependencies移除Pandas/NumPy。

**Gate P6-RAM：**startup及主要線上workflow均不載入Pandas；功能及query-count測試全過；512MB instance的RSS有可重複實測下降。

### P6.2 拆細`deploy/proxy.py`

`proxy.py`仍超過5,000行，是唯一明顯過大的架構檔；30多條page-serving routes大多只差path alias、登入／maintenance gate、HTML檔及cache policy。

1. [ ] 先以可審核的dict-driven route table收斂page-serving routes；spec明列aliases、auth、maintenance、HTML及cache，不用catch-all吞API/assets。
2. [ ] P0-LIVE-RETIRE完成後，抽出多人Live餘下的room control／signaling／quota module。P0架構決定前只修復legacy relay必要問題，不先大型重構將退役的Render PCM／Gemini／TTS audio path；media relay由P0負責刪除，P6只重構已剩下的control plane。Solo browser-direct token／page接線維持獨立。
3. [ ] 再逐個抽static/projector router及resource accounting；完整multiplayer room orchestration最後獨立處理。
4. [ ] 最終`proxy.py`只留app factory、middleware、router registration及lifespan；每步獨立commit及可rollback。

**Gate P6-PROXY：**route/alias及OpenAPI parity、headers、login/maintenance、Solo direct token／resumption／GoAway、多人control／signaling close/error、capability／epoch、quota、Render control-bandwidth及external-media cost accounting測試全過；zero-media-through-Render regression、單worker Live／room smoke無退化。

### P6.3 其餘長期工作

- [x] 最小offline regression suite已重建（2026-07-14）；4.2.1基線為六個檔29個case，
  4.2.2已按同一原則擴充AI Coach、Live、resource及security回歸。每個case對應真實
  失敗模式，CI與發布清單均會跑，日後新功能繼續沿用。
- [ ] P0-LIVE-RETIRE後只將仍留在Render的control-plane room metadata／session／cache搬到Redis或
  database；PCM、provider socket及media-session state不在此項。完成前維持單worker，之後才評估
  multi-worker／multi-instance。
- [ ] 非Live Render TTS timeout加短TTL circuit breaker，冷卻期直接fallback Azure；Live edge path
  只可在edge完成TTS fallback或改用Gemini native audio，不得重新將audio經Render。
- [ ] `video_views`設計近期raw retention + durable aggregates，先backfill及核數。
- [ ] AI provider availability及價格改成帶生效日期的versioned metadata，每次release以官方資料核對。
- [ ] Public status endpoint加短TTL cache／IP rate limit，或移入developer gate。
- [ ] Open DB及depose-data改server-side search/pagination，不再每次傳最多2,000題到browser。
- [ ] 建立可重現Python dependency lock，CI與Render用同一Python major/minor及乾淨環境。
- [ ] 每季做table/index/retention/R2 orphan/dependency audit，只保存結論及可重現query。

## 完成一個gate時要記錄

在相關PR／release記錄：目標、production/staging基線、migration/code、測試結果、資源前後差異、security/privacy影響、rollback、owner及下一步。完成後更新本roadmap checkbox；不要再建立散落的計劃或migration diary。
