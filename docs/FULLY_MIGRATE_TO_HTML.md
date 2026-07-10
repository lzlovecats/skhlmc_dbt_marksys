# Fully Migrate to HTML — 遷移計劃 / Onboarding Handoff

> 本文件係畀**接手／協助呢個遷移嘅開發者**睇。目標：將現有 Streamlit 系統逐頁改寫成
> HTML/CSS/JS 前端 + Python JSON API，最終刪走 Streamlit。讀完呢份文件，你應該可以自己
> 揀一版頁面、跟同一套模式獨立開工。
>
> 狀態日期：2026-07-10 ｜ 版本：3.8.0（vote 頁已遷移並雙軌並行）

---

## 0. TL;DR（畀趕時間嘅人）

- **策略**：逐頁遷移，**Streamlit 同 HTML 雙軌並行**，穩定先刪 Streamlit。已完成第一版：**投票頁（vote）**。
- **唔係用 JS 重寫 backend**。Python 業務邏輯抽入 `core/`（去 Streamlit 化），HTML 前端經 `api/` 嘅 JSON 端點取用。**新舊 UI 共用同一份 `core/` 邏輯 = 單一真相來源。**
- **三個關鍵不變量**：
  1. `core/` 同 `api/` **禁止 `import streamlit` / `st.*`**（proxy 係 512MB、又冇 Streamlit runtime）。
  2. DB 存取用**注入式 executor**（`db=None` 參數），令同一份邏輯喺 Streamlit 同 proxy 兩個 process 都行到。
  3. HTML 頁**外觀／tab 次序要對齊現有 Streamlit**（永遠 dark、同配色、同分頁次序）。
- **每頁遷移 4 步**：① 抽 `core/` 邏輯（行為零改變，Streamlit 仍要正常）→ ② 加 `api/` router → ③ 砌 `frontend/` 頁 → ④ 雙軌並行收 bug → 最後刪 Streamlit 頁。
- 揀新一版頁做之前，**睇「§7 每頁遷移 Playbook」同「§9 坑」**。

---

## 1. 前因後果（點解要做）

### 系統背景
一個校園辯論比賽電子賽務系統（`README.md` 有完整功能列表）：辯題徵集投票、電子分紙、報名、賽程、片段重溫、AI 辯論教練等。約 23,000 行 Python，主體係 **Streamlit** app。

### 系統其實已經係「一半 Streamlit + 一半 HTML/JS」
呢點好重要——遷移唔係由零開始：

| 部分 | 技術 |
|---|---|
| 分紙 / 投票 / 報名 / 賽程 / AI 教練後台 UI | **Streamlit**（Python server 渲染，~15k 行）|
| 即時互動（Gemini Live、聯機房、投影機、練習機、TTS、Web Push、PWA） | **FastAPI 反向代理 + 原生 HTML/JS + WebSocket**（`deploy/proxy.py` + `templates/*.html`）|

即係凡係 Streamlit 做唔到嘅嘢，已經有人手寫咗原生 HTML/JS。**遷移 = 把 Streamlit 嗰半都變成 HTML/JS。**

### 點解遷移
- Streamlit 嘅 rerun 模型令互動慢、狀態易失（切 App 返嚟會 reset、打字消失）。
- 手機 UI 要一大堆 hack（>>/⋯ 同狀態欄重疊、iOS selectbox 點擊偏移、viewport…），而且仲時好時壞。
- 逐頁換 HTML 後：互動即時、完全掌控手機 UI、PWA 體驗一致、可以移除嗰堆 Streamlit hack。

### 策略
- **保留** Python 業務邏輯做後端（唔用 JS 重寫規則——最有價值又最易改壞嘅資產）。
- **逐頁**遷移；**vote 頁先行**（委員使用率最高）。
- **雙軌並行**：舊 Streamlit 頁同新 HTML 頁同時上線，兩邊共用 `core/` 邏輯 → 數據天然一致；穩定先刪 Streamlit。

---

## 2. 執行架構（Runtime）

兩個 process，由 `deploy/start.sh` 一齊起（Docker，見 `deploy/Dockerfile`）：

```
瀏覽器 / PWA
    │
    ▼
FastAPI 反向代理  (deploy/proxy.py, uvicorn, PORT=8000)   ← 對外唯一入口
    ├─ 直接處理：/vote(HTML)、/api/*、/practice、/projector、
    │            /gemini-live、/room/*、PWA(manifest/sw/icons)、push、TTS …
    └─ 其餘一律反代去 ↓
Streamlit  (main.py, 127.0.0.1:8501)                      ← 舊 UI（逐步縮細）
```

- **部署**：Render（Docker），`deploy/render.yaml`（`autoDeploy:false`，但 dashboard 設定會 override，deploy 前要確認）。main 係生產分支。
- **DB**：PostgreSQL，SQLAlchemy。連線資訊喺 `.streamlit/secrets.toml` `[connections.postgresql]`（proxy 亦讀 `DATABASE_URL` env）。
- **記憶體**：Render starter 512MB → `start.sh` 有 malloc 調校。**呢個係「proxy 唔可以 import streamlit」嘅硬理由之一。**
- **PWA**：`static/manifest.json`（`scope:"/"`、`display:standalone`）、`deploy/sw.js`（**冇 fetch handler**，只做 push + 通知點擊；所以唔會快取/攔截 API）。

---

## 3. 目錄結構（Repo Folder 原則）

### 遷移相關（新）
```
core/        純後端業務邏輯，禁 streamlit。DB 靠注入 executor。新舊 UI 共用。
  vote_logic.py    投票/罷免：解析、門檻、ballot 寫入、查詢、自動結算
  members.py       活躍成員數（動態門檻）
  push.py          streamlit-free Web Push 發送
  auth_logic.py    委員登入（bcrypt 校驗 + 登入副作用）
api/         FastAPI JSON router，由 proxy include。Handler 內用 lazy import 避循環依賴。
  vote_api.py      /api/vote/*
  auth_api.py      /api/committee/*
frontend/    HTML/CSS/JS 前端本體（取代 templates/ 角色）。
  vote/index.html  投票頁（目前 self-contained：inline css/js）
  shared/          （預留）共用 css/js
```

### 現有（背景）
```
main.py              Streamlit 入口 + st.navigation（頁面路由，url_path）
<page>.py (根目錄)    每個 Streamlit 頁（vote.py, judging.py, home.py …）← 遷完逐個刪
functions.py         共用 helper（⚠️ 頂層 import streamlit）
db.py                DB primitive（⚠️ 包 st.connection；含 StreamlitDb executor）
auth.py              Streamlit 認證（⚠️ import streamlit）
schema.py            表名常數 + DDL（✅ 無頂層 streamlit，可安全 import）
prompts.py / debate_timing.py / ai_model_config.py   純 helper（✅ 無 streamlit）
deploy/              proxy.py（反代 + API 掛載 + 自有 DB engine）、Dockerfile、start.sh、sw.js
templates/           舊 proxy-served HTML（projector/practice/live/room）← 最後收編入 frontend/
static/ assets/ appliance/   PWA 資源 / 內容檔 / kiosk 運維
```

**原則**：前端（`frontend/`）同後端（`core/` + `api/`）界線清楚；`core/` 語言無關可測；舊 Streamlit 頁留喺根目錄過渡，遷完即刪。

---

## 4. 核心設計原則（最重要——落手前必讀）

### 4.1 單一真相來源（Single Source of Truth）
業務邏輯淨係喺 `core/`。Streamlit 頁同 API **兩邊都 call 同一個 `core/` 函數**。遷移時**先**把邏輯由 Streamlit 頁抽入 `core/` 並令 Streamlit 頁改為呼叫佢（**行為零改變，Streamlit 頁必須仍然正常**），**之後**先砌 HTML。呢步保證新舊一致、亦係最低風險嘅重構。

### 4.2 DB Executor 注入（成個遷移嘅命脈）
`core/` 唔可以用 `db.py` 嘅 `get_connection()`（佢係 `st.connection`，proxy process 冇 Streamlit runtime，行唔到）。所以：

- `core/` 每個掂 DB 嘅函數，最後一個參數 `db=None`；入面 `db = _resolve_db(db)`。
- `_resolve_db(None)` → **lazy import** `db.default_db()`（`StreamlitDb`）。**lazy 好關鍵**：proxy 永遠傳自己嘅 executor，就永遠唔會觸發 `import db`／streamlit。
- 兩個實作，同一 duck-typed 契約：
  ```
  query(sql, params=None)         -> pandas.DataFrame
  execute(sql, params=None)       -> None
  execute_count(sql, params=None) -> int   # 影響行數
  ```
  - `db.StreamlitDb`（Streamlit runtime，包 `execute_query`/`query_params`）
  - `deploy/proxy._ProxyDb`（包 proxy 自己嘅 SQLAlchemy engine，已設 `search_path public, extensions`）
- Streamlit 頁呼叫 `core` 時**唔使傳 db**（自動 fallback），所以呼叫點零改動。

### 4.3 proxy / core / api 禁 streamlit
- `functions.py`、`auth.py`、`db.py` 頂層都 `import streamlit` → **proxy 一律唔可以 import 佢哋**。
- `api/*` 頂層只准 import `fastapi` / `pydantic` / 純 module（`schema`…）。凡係可能拉入 streamlit 嘅嘢（`core.*`、proxy helper）→ **喺 handler / dependency 函數內 lazy import**。呢個亦順便打散 proxy⇄api 嘅循環依賴。
- 驗證方法：`python -c "import sys, core.xxx; print('streamlit' in sys.modules)"` 要係 `False`。

### 4.4 認證（跨 Streamlit / HTML 共用）
- 統一用簽名 cookie **`committee_user`** = `f"{user_id}:{hmac_sha256(cookie_secret, user_id)}"`（`cookie_secret` 喺 `system_config` 表）。
- proxy：`_verify_committee_token` / `_require_committee_user`（Bearer 或 cookie → user_id，否則 401）、`_sign_committee_token`。
- Streamlit：`auth._sign_cookie` / `_verify_cookie`（同一公式）。
- **一邊登入，兩邊都認**。cookie httponly、180 日。

### 4.5 其他慣例
- **快取**：`@st.cache_data` 屬 UI runtime，留喺 Streamlit 頁做薄 wrapper，唔可以入 `core/`。
- **自動結算 idempotent**：`UPDATE ... WHERE status='pending'`，兩邊 UI 同時觸發都安全。
- **通知**：`core/push.notify_committee(db, vapid, …)`（streamlit-free）；proxy `_get_vapid()` 讀 secrets。UI 反饋（toast/banner）留前端，`core` 只回結構化結果。
- **路由碰撞**：Streamlit 頁用 `st.Page(url_path=...)` 佔某 path；proxy 加同名 route 會**蓋過**佢。加 proxy route 前**必查**（見 §9）。

---

## 5. HTML 設計原則

**大方向：新版要同現有 Streamlit「睇落差唔多」**（雙軌期用戶會同時見到兩版；亦方便無縫取代）。

- **主題永遠 dark**（Streamlit `.streamlit/config.toml` `base="dark"`）。**唔好**用 `prefers-color-scheme` 跟系統。用 Streamlit 預設 dark 配色：底 `#0e1117`、卡/次要面 `#262730`、文字 `#fafafa`。
- **語意色**：同意=綠、不同意=紅；**accent 用藍**（唔好用會撞「不同意」紅嘅紅做 accent）。
- **Tab／分頁次序同 label 要對齊對應 Streamlit 頁**（例：vote = 提案 → 辯題投票 → 罷免投票，default 提案，對應 `vote.py` 的 `_tab_options`）。
- **手機優先**：`padding: env(safe-area-inset-top)`、鎖 viewport（`maximum-scale=1`）、input `font-size ≥16px` 防 iOS 縮放。（`>>`/`⋯` 同狀態欄重疊嗰個問題係 **Streamlit 專屬 chrome**，全 HTML 後自然消失。）
- **技術取向**：vanilla JS + `fetch`（一個人都維護到）、事件委派（event delegation）。頁面暫時 self-contained（inline css/js）；規模大時再拆 `frontend/shared/` + proxy `StaticFiles` mount。
- **落手前**：先開對應 Streamlit 頁睇實際樣同分頁，先跟住做。

---

## 6. 已完成 vs 未完成

### ✅ 已完成 —— 投票頁（vote）
- **`core/`**：`vote_logic`（解析/門檻/ballot/查詢/單一動議查詢/自動結算）、`members`（動態門檻）、`push`、`auth_logic`。
- **`api/vote_api.py`**：
  - `GET /api/vote/data` — 待投票 + 已通過/否決 + 動態門檻（live 驗證 200）
  - `POST /api/vote/cast` — 同意/不同意(理由)/撤回/轉投 + 類別佔比警告(`confirm_category`) + 自動結算 + push
  - `GET /api/vote/depose-data` — 待罷免 + 可罷免辯題庫（live 驗證 200）
  - `POST /api/vote/depose` — 提出罷免（active 檢查、≥10 擋、重複擋）
  - `POST /api/vote/propose` — 提出新辯題（類別失衡 `confirm_imbalance`、動態門檻）
- **`api/auth_api.py`**：`POST /api/committee/login|logout`、`GET /api/committee/me`（live 驗證）
- **參與率 slice**：`core.members.get_member_participation_stats` + `GET /api/vote/member-stats` + HTML「參與率」tab（讀 `committee_vote_activity_view`，同 Streamlit 統計表一致）。
- **Vote parity 收尾**：提案 tab 已重新包含「提出新辯題 + 提出罷免動議」；投票/罷免卡補回進度 bar、討論區、理由 expander；辯題投票補回「最近二十個」歷史 expander；AI 審查提案、討論 Tag Gemini、AI 辯題庫/歷史分析改成 streamlit-free API。
- **`frontend/vote/index.html`**：六分頁（提案/投票/罷免/AI分析/成員統計/帳戶）+ 登入表單 + 登出 + push（重用 `/api/push/subscribe`）+ 更改密碼。
- **路徑**：HTML 頁 = **`/vote`**（未來主版）；Streamlit 頁改 `url_path="vote-classic"` = **`/vote-classic`**（`vote.py` 頂有掣去 `/vote`）。
- **驗證**：`py_compile` + import 隔離 + 假 db 單元測 + 本機 live-curl 讀端點 + 使用者確認 Streamlit 頁仍正常。

### ✅ 已完成 —— 投票頁 HTML parity 收尾
對應 `vote.py` `_tab_options = [proposal, topic_vote, depose_vote, bank_analysis, member_stats, account]` 主要功能已補到 HTML/API。
- 已補 `bypass_active_check`、登入時 `refresh_acc_type`、一次性系統公告、黑底 toast、Streamlit 頁內刷新掣、完整活躍狀態提示及投票卡 progress bar。
- 仍需正式環境 smoke：AI provider、Web Push permission、production 寫入端點；呢啲係部署驗證，唔係已知功能缺口。

### ⬜ 未開始 —— 其餘所有 Streamlit 頁
每版都要行同一套 4 步流程。粗略清單（睇 `main.py` `st.Page`）：
下一頁已指定為 `open_db`，遷移基線見 `docs/OPEN_DB_HTML_MIGRATION.md`。其後包括：`home`（主頁導航）、`judging`（電子分紙，核心）、`match_info`、`review`、`draw_match_schedule`、`registration`(+admin)、`team_roster`、`video_replay`/`video_admin`、`match_photos`、`chairperson`、`lateness_fund`、`ai_fund`、`ai_coach`、`ai_training`、`db_mgmt`、`dev_settings`、`bug_report` 等。

---

## 7. 每頁遷移 Playbook（照住做）

> 揀一版 Streamlit 頁 `X.py`，跟以下步驟。可多人並行（各人一版頁），衝突面細。

**Step 1 — 抽 `core/X_logic.py`（風險最低、最重要）**
- 把 `X.py` 入面**非 UI** 嘅嘢（DB 查詢/寫入、計算、規則）搬入 `core/`。禁 `st.*`；DB 掂嘅函數收 `db=None` + `_resolve_db`。
- 把混住 UI 嘅函數拆開：純決策/副作用入 `core`，`st.success/rerun/dialog` 等留 `X.py`。
- 改 `X.py` 改為呼叫 `core`（alias import 令呼叫點少改）。
- **收貨標準**：Streamlit `X` 頁行為同之前一模一樣（自己開 app 走一次）。

**Step 2 — 加 `api/X_api.py`**
- `APIRouter(prefix="/api/X")`，proxy `app.include_router`。
- Auth：`Depends` 一個讀 `_require_committee_user`（lazy import proxy）嘅函數。
- DB：`get_vote_db()`（proxy 的 `_ProxyDb`）傳落 `core`。
- 端點只做「驗證 + call core + 回 JSON」；side-effect（通知）用 `core/push` best-effort。

**Step 3 — 砌 `frontend/X/index.html`**
- 跟 §5 設計原則（dark、配色、tab 次序對齊 Streamlit）。
- proxy 加 route serve（`FileResponse`）。**⚠️ 查路由碰撞**（§9）：如果 Streamlit 該頁 `url_path="X"`，你想 HTML 用 `/X` 就要把 Streamlit 改 `url_path="X-classic"`；未想搶就用 `/X-beta`。
- fetch `/api/X/*`（`credentials:"same-origin"`）。

**Step 4 — 雙軌 → 收 Streamlit**
- 兩版並行，加互跳連結，收 bug（用 `bug_report.py`）。
- 穩定後：`main.py` 移除該 `st.Page`、刪 `X.py`，保留 `core/X_logic.py`。

**每步都要驗證**：
```
python -m py_compile <改動檔>
python -c "import sys, core.X_logic; print('st?', 'streamlit' in sys.modules)"   # 要 False
# 假 db 單元測 core 函數（見 core 既有測試模式）
# 本機起 proxy live-curl（見 §8）
```

---

## 8. 本機開發 / 測試

- venv 曾改過名 → **一律用 `./venv/bin/python -m <module>`**（`pip`/`streamlit`/`uvicorn` 嘅 wrapper script shebang 可能壞）。
- 起全 app：
  ```
  ./venv/bin/python -m streamlit run main.py --server.port 8501 --server.address 127.0.0.1 \
      --server.headless true --server.enableCORS false --server.enableXsrfProtection false \
      --server.fileWatcherType none --browser.gatherUsageStats false &
  ./venv/bin/python -m uvicorn deploy.proxy:app --host 127.0.0.1 --port 8000
  ```
  用 **proxy 個 port 8000** 睇嘢（唔好用 8501）。
- **只測 HTML 頁**（唔使 Streamlit）：只起 proxy，然後 mint cookie 繞過登入——
  ```
  # 產生 cookie 值（uid 改自己）
  ./venv/bin/python -c "import tomllib,hmac,hashlib;from sqlalchemy import create_engine,text;\
  s=tomllib.load(open('.streamlit/secrets.toml','rb'))['connections']['postgresql'];\
  u=f\"{s.get('dialect','postgresql')}://{s['username']}:{s['password']}@{s['host']}:{s['port']}/{s['database']}\";\
  sec=create_engine(u).connect().execute(text(\"SELECT value FROM system_config WHERE key='cookie_secret'\")).scalar();\
  uid='admin';print('committee_user='+uid+':'+hmac.new(str(sec).encode(),uid.encode(),hashlib.sha256).hexdigest())"
  # 瀏覽器開 http://localhost:8000/vote → F12 console: document.cookie="上面嗰串; path=/"
  ```
- 本機 **Streamlit 登入可能失敗**（見 §9 websockets）——所以測 HTML 用上面 cookine 注入法最穩。

---

## 9. 坑 / 注意事項（血淚）

1. **路由碰撞（最容易中）**：Streamlit `st.Page(url_path="X")` 令 `/X` 屬 Streamlit；proxy 加 `@app.get("/X")` 會**靜靜蓋過**佢，連 push 通知(url) 都會跟住去新頁。加 route 前 grep `main.py` 的 `url_path`。（我哋踩過：`/vote` 一度蓋咗 Streamlit vote 頁。）
2. **proxy 唔可以 import streamlit**：`functions/auth/db` 頂層都 import streamlit；`schema/prompts/debate_timing` 就安全。core/api 要用 lazy import 隔開。512MB 記憶體亦唔想 proxy 拖住 streamlit。
3. **`proxy.py` 開機 import `api.*`**：proxy 係大門，import 鏈一爆成個網站起唔到。api 頂層保持只 import fastapi/pydantic；deploy 後即刻確認網站起到。
4. **本機 Streamlit 登入 400**：本機 `websockets`/`streamlit` 版本比生產新（16.x / 1.55），反代 Streamlit 的 `/_stcore/stream` 握手被拒 → 登入 UI 用唔到。生產環境正常。本機測 HTML 用 cookie 注入；要本機開全 app 就 `pip install "websockets==12.0"`（甚至降 streamlit）。
5. **AI 呼叫耦合**：`ai_coach_helpers` / `ai_review_topic` 等可能拉 streamlit。遷 AI 相關功能前，確認相關 helper 係咪 streamlit-free，唔係就要一齊抽（AI 功能可放最後，唔阻核心）。
6. **RLS / 自訂 auth**：現時網站由可信 Python backend 用 direct Postgres connection 存取 DB，委員身份係自訂 cookie，唔係 Supabase Auth JWT。開 RLS 前要先設計 DB role／policy，唔可以假設 `auth.uid()` 等於委員 `user_id`；亦唔可以把 service role、secret key或 DB connection string 放入前端。
7. **並發編輯**：呢個 repo 可能有多個 session／IDE 同時改。commit 前 `git status` 睇清楚，唔好 `git add -A` 掃入唔關你事嘅檔（曾見 `ai_fund.py`、TTS/manual 檔同時被改）。
8. **DB 寫入測試**：唔好隨便對生產 DB 做寫入測試。讀端點可 live-curl；寫端點靠假 db 單元測，或用測試帳戶／staging。

---

## 10. 部署

- Render，Docker。**確認 dashboard service 的 Auto-Deploy 設定**（`render.yaml` 寫 false，dashboard 會 override）。main = 生產分支。
- **要唔要開 maintenance mode**（`system_config.maintenance_mode`，`main.py` gate）？判準睇**性質唔係重要程度**：
  - **要開**：有 DB schema／資料 migration；或改**現有行為**（現有登入/投票/計分邏輯）令使用中用戶撞到半新半舊。
  - **唔使開**：純**新增**功能（新端點/新頁），唔郁 schema、唔改現有流程 → 最多 Render 重啟幾秒 blip。建議揀低使用時段、deploy 後即刻煙測。
- 版本：`version.py` `APP_VERSION`（sidebar + bug-report 都讀佢），每 release bump。

---

## 11. 快速檔案索引

| 想搵 | 睇邊度 |
|---|---|
| 投票業務邏輯 | `core/vote_logic.py` |
| 活躍成員/門檻 | `core/members.py` |
| Web Push 發送 | `core/push.py` |
| 委員登入邏輯 | `core/auth_logic.py` |
| 投票 JSON API | `api/vote_api.py` |
| 登入 JSON API | `api/auth_api.py` |
| 投票 HTML 頁 | `frontend/vote/index.html` |
| 反代 + API 掛載 + DB engine + auth helper | `deploy/proxy.py` |
| DB executor（StreamlitDb） | `db.py` |
| 表名常數 / DDL | `schema.py` |
| Streamlit 頁路由（url_path） | `main.py`（`st.Page`, `st.navigation`）|
| 舊 Streamlit 投票頁（參考行為） | `vote.py` |
| 啟動腳本 / Docker | `deploy/start.sh`, `deploy/Dockerfile` |
| PWA | `static/manifest.json`, `deploy/sw.js` |

---

## 12. 建議下一步（可並行分工）

**A. 投票頁收尾（令 HTML 完全對等，之後即可刪 Streamlit vote 頁）** — 見 §6「未完成」1–5。可拆畀不同人：
- 討論留言（core + `/api/vote/comments` + 前端）
- 成員統計（core + `/api/vote/member-stats` + 前端表）
- 帳戶管理（push 設定，端點已有）
- 辯題庫分析 + AI 審題（要先確認 AI helper streamlit-free）

**B. 開下一版頁**（跟 §7 playbook）。建議先揀**高使用、低複雜**嘅：`home`（純導航）或 `open_db`（唯讀辯題庫）練手，再啃 `judging`（分紙，核心但複雜）。

> 每位協作者：認一版頁 / 一個分頁，跟 §7 四步 + §5 設計 + §4 原則，過 §7 驗證，就可以獨立出 PR。
