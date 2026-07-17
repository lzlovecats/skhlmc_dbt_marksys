# Repository agent instructions

本文件位於 repository root，係整個 `skhlmc_dbt_marksys` repository 嘅工程工作規範。開始任何分析、修正、功能、重構、migration 或 release 工作前，必須先讀本文件同同目錄嘅 [`CAUTION.md`](CAUTION.md)。

`MUST`／「必須」係硬性 gate，唔係建議。若同使用者當次明確指令衝突，先指出衝突並等使用者決定，唔可以靜靜地自行取捨。

## 兩條最高優先規則

### 1. Bug fix：先喺 production 證實根源，禁止估原因

任何被描述為 bug、regression、production incident、資料錯誤、間歇性失敗或「應該唔係咁」嘅工作，都必須先完成以下 root-cause gate，先可以開始改 code：

1. 以 production 當時真正部署版本、真實 request／response、server log、database row、resource／provider 記錄或可重現 workflow 收集證據。預設只做 read-only 檢查；查 production 唔代表獲准 deploy、改 secret 或改資料。
2. 將現象沿 production data/control flow 追到一個可指認嘅失敗點，例如確實嘅 branch、SQL 條件、state transition、cache/version mismatch、provider response 或 race ordering。
3. 記錄「觀察到咩」、「點樣重現」、「證據指向邊段 code／資料」、「點解其他候選原因被排除」。假說、直覺、相似 bug、local-only failure 或單憑 code review 都唔等於根源。
4. 未能存取 production、證據不足、問題已經消失而無可驗證痕跡，或者需要額外權限先查到時，必須停低並問使用者；禁止用「最可能係」做理由開始 patch。
5. 根源證實後，先加一個會喺舊 code 重現失敗嘅 regression test，再做最窄修正。完成後先跑相關 offline gates；只有獲明確 deploy 授權，先可以做 deploy 後 production smoke。

報告 bug fix 時必須分開寫：production 證據、已證實根源、修正、regression 驗證、仍未執行嘅 production 動作。

### 2. 新功能／改功能：細節有取捨就先問使用者

新增功能、改現有行為、UI／文字／流程改動、重構導致 contract 變化時，只要有任何合理選擇會令使用者體驗、業務規則、資料模型、私隱、安全、成本、效能、相容性、預設值、限額、失敗行為、migration 或 rollout 有不同結果，就必須先問使用者，等佢明確決定後先寫受影響部分。

提問時要提供具體 context、2–3 個真實可行方案、各自影響、建議方案，同一條清晰問題。唔可以因為現有 code 似乎偏向某方案、某方案較容易、或者 agent 覺得係「細節」就自行決定。未有答案前可以繼續做不受該決定影響嘅 read-only 研究，但唔可以將其中一個選擇落實成 code、schema 或 UI。

以下通常都係必問取捨：權限對象、公開／私人範圍、保留期、收費／配額、通知時機、空狀態、錯誤 fallback、mobile 行為、舊資料兼容、破壞性 migration、AI provider／model、錄音／個人資料處理、automatic vs manual action，以及任何會影響賽果嘅計算或 tie rule。

### 3. 解決問題用最簡單可以解決根源嘅方案

用可以解決問題嘅最簡單方案。唔好加多餘功能，唔好做無關重構，唔好為咗假設嘅未來需求做設計。

## 系統地圖

```text
frontend/*、templates/*
        │ same-origin HTTP / WebSocket
        ▼
api/* ───────────────► core/* ─────────► PostgreSQL / R2 / AI、TTS providers
        ▲                  ▲
        └──── deploy/proxy.py ─── FastAPI 組裝、page/static routes、middleware、
                                  resource accounting、Live room control
```

- `frontend/`：原生 HTML/CSS/JavaScript，無 build step；每個功能頁有自己入口，共用 browser code 放 `frontend/shared/`。
- `templates/`：practice、P2P room、projector 等由 server 讀取／注入 runtime 值嘅 HTML。
- `api/`：HTTP payload validation、authentication/authorization、pagination、response mapping。
- `core/`：業務規則、SQL、storage/provider adapters；可獨立測試嘅邏輯應留喺呢層。
- `deploy/proxy.py`：composition root，同時包含仍未拆出嘅 Live control plane；唔好再將一般業務邏輯塞入去。
- `schema.py`：只供全新空 database bootstrap；唔係 production migration runner。
- `migrations/`、`core/db_migrations.py`、`tools/manage_db_migrations.py`：既有／production database 唯一 schema 演進路徑。
- `system_limits.py`：runtime、request、DB、Live、AI、storage、retention 等資源限額唯一 code source。
- `appliance/`：Ubuntu 賽務專用機、backup、health、kiosk；佢係 cloud client，唔係離線 server。
- `tests/`：offline regression suite；每個 test 對應真實或高影響失敗模式，唔以 coverage 數字為目標。

Production 目前係 Render 上單一 Uvicorn process。多人 Live room、部分短期 cache／session／lock 係 process-local memory；相關 state 未完整外置到 shared durable store 並通過 adversarial tests 前，唔可以假設 multi-worker／multi-instance 安全。

## 唯一來源與唔可以複製嘅規則

| 範圍 | 唯一來源／主要 contract |
|---|---|
| Release version | `version.py` 嘅 `APP_VERSION` |
| 資源及技術安全限額 | `system_limits.py` |
| AI model label/provider/capability | `ai_model_config.py` |
| 評分公式、辯員排名 | `scoring.py`、`core/judging_logic.py`、`core/results_logic.py` |
| 辯論流程時間 | `debate_timing.py` |
| Prompt | `prompts.py` 及 server 端 prompt builders |
| 帳戶頁面權限 | `account_access.py`、`api/access.py` 及現有 server-side token verifier |
| Typed runtime config | `core/config_store.py` 嘅 `CONFIG_SPECS` 同 `app_config` |
| Database 演進 | immutable `migrations/*.up.sql`／`*.down.sql` + ledger |
| 空 DB bootstrap | `schema.py`；要反映現行可 provision schema，但唔可以取代 migration |
| 操作說明／規則 | `assets/user_manual.md`、`assets/rules.md` |

唔好喺 API、HTML、README 或新 helper 複製 threshold、model mapping、評分公式、權限清單或 version。文件若需要提及會變嘅值，優先連結唯一來源，避免寫死 snapshot。

## 標準工作流程

### 開始前

1. 先讀相關 page、API、core logic、schema/migration、tests，沿完整 request/data flow 審視，唔好只改第一個搜尋結果。
2. 跑 `git status --short`，保留使用者既有改動；唔好清走、覆蓋或順手格式化無關檔案。
3. 判斷工作係 bug fix 定功能／行為改動，執行上面對應 gate。
4. 列出會受影響嘅 contract：auth、data、transaction、cache、resource、privacy、cost、concurrency、rollout、rollback。
5. Database、R2 cleanup、deploy、secret、通知或其他外部 mutation 一律另外確認授權；code change 本身唔包含呢啲權限。

### 實作時

- 維持 `frontend → api → core` 分層；API 唔重寫業務公式，frontend 唔決定 server authority。
- 所有 user value 用 SQL parameters。動態 table／column／sort 只可來自固定 allowlist，唔可以直接插值。
- 同一業務動作多個 write 要用同一 transaction；有競態嘅 claim／finalize／submit 要用 constraint、conditional update、row/advisory lock 或 idempotency key 保證。
- 外部 HTTP／AI／R2 call 唔好長時間佔住 DB transaction。沿用「先原子 reserve/claim，外部 call，最後 settle/finalize」模式，並處理中斷同 orphan。
- 加新 config key 要先登記 `CONFIG_SPECS`、正確 type/namespace/secret classification，再經 config store 讀寫；禁止恢復 `system_config` runtime access。
- 加／改 system resource limit 要改 `system_limits.py` 同對應測試／運維文件；limit 係 process import 時讀取，環境改動要 restart/redeploy 先生效。
- 密碼只存 bcrypt hash；長期 provider key、cookie secret、database URL 唔可以落 browser、log、exception、SQL example 或 repo。
- 保留 response/input size、pagination、timeout、semaphore、retention、rate limit 同 fail-closed checks；唔好為咗「正常 case work」而移除保護。

### 按範圍必查

**Database / migration**

- 新 schema 改動要有新而且成對嘅 `up.sql`／`down.sql`；已套用 migration 永不改名、改內容、刪除或 squash。
- Migration runner 擁有 transaction boundary，所以 SQL 檔禁止 `BEGIN`／`COMMIT`／`ROLLBACK`，亦禁止 literal `%`；新 table 必須 revoke `PUBLIC`、`anon`、`authenticated` 權限。
- 同步審視 `schema.py` 空 DB bootstrap、table constants、indexes、feature readiness、rollback 同 permission verification。刻意未 provision 嘅 dataset/model/eval/RAG bundle 唔可以因一次 request 自動建立。
- Production 先跑 status／audit／reconcile；`baseline` 只可用喺已核對 catalog 嘅既有環境，唔係 drift 修復工具。

**Auth / access**

- 系統有 committee cookie、admin、developer、SQL console、kiosk、match judging token、review token、roster token 同 delegated role 等不同 authority；唔可以因為「已登入」就互相代替。
- 所有權限由 server 重新驗證；client 傳入嘅 user id、role、side、match、price、duration、status、ownership 都唔可信。
- Token/cookie 改動要保留 expiry、credential rotation revocation、disabled-account revocation、rate limit、`HttpOnly`、`SameSite`、secure policy、path 同 logout deletion parity。
- Database console 對 `app_config`、`schema_migrations` 及 secret/internal table 嘅封鎖唔可以放鬆。

**Frontend / cache**

- HTML 直接部署，冇 bundler 幫手；改 shared JS/CSS 要核對所有 consumer。
- 長 cache asset 必須用 `APP_VERSION` cache-buster。需要 server replace 嘅 HTML 用 `__APP_VERSION__`，並為 route 加相應 regression；唔好提交未被 replace 嘅 placeholder。
- 所有 async load/save 要防 stale response 覆蓋新 state；轉 match、judge、room、album 或 retake context 時要 invalidation／generation guard。
- User／AI／database text 用 `textContent` 或既有 safe markdown renderer；唔好直接送入 `innerHTML`。Server 仍要驗證由 UI dropdown 傳返嚟嘅值。

**Media / AI / Live**

- 相片同錄音 binary 只存 private R2；DB 只存 metadata／`r2_key`。Upload 用短期 presigned URL + signed claim + completion verification，唔好加 base64/BYTEA/Render relay fallback。
- MIME、container、duration、dimensions、size 同 object metadata 要由 server probe／HEAD 驗證，唔可信 browser 聲稱。
- Consent、owner/reviewer scope、raw recording deletion、orphan cleanup、audit 同 retention 次序係 privacy contract，改動前逐項畫出 lifecycle。
- AI 每次 operation 要保留 operation ID/stage、provider-attempt semantics、費用及 bandwidth/storage accounting；未真正 call provider 唔可以記成 attempt，重試亦唔可以重複扣數。
- P2P room 只容納兩位已驗證 member，STUN-only、無 TURN、無 Render audio fallback；Render channel 只可以有 signaling/control/text。Mode B 已退役，唔可以無新產品決定就恢復。
- Live state transition、timer、bell、transcript commit、socket replacement、ICE restart、rate bucket 全部有 adversarial tests；相關修改要先讀完整 state machine 同測試。

## 驗證

按改動風險先跑最窄測試，再跑完整 release gates：

```bash
./venv/bin/python -m compileall -q api core deploy
git diff --check
./venv/bin/python tools/manage_db_migrations.py lint
./venv/bin/python -m pytest -q tests
```

若改到 standalone frontend `.js`，另外跑：

```bash
node --check path/to/changed.js
```

若改到 HTML inline script，要以相關 contract test／browser smoke 驗證；`node --check` 唔會自動抽出 inline JavaScript。涉及 database、R2、provider、browser media、WebSocket、appliance 或 production-only 行為時，offline tests 只係其中一個 gate，唔可以聲稱已完成真實環境驗證。

## 完成定義與交代

一項工作只有喺以下條件先叫完成：

- bug 有 production 證據同已證實根源；功能取捨已有使用者決定；
- 改動係最窄而完整，跨層 contract 已同步；
- regression test 對應原本失敗模式，而唔係只測新 implementation 細節；
- 相關及完整 gates 已通過，或者清楚列出未能執行嘅 gate 同原因；
- migration、deploy、secret、data cleanup、通知、provider 或 production smoke 未獲授權時仍然保持未執行；
- 最終交代包括改咗咩、證據／決定、驗證結果、風險、rollback／follow-up，同任何 production 與 repo 狀態差異。
