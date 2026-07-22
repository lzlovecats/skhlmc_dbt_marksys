# 聖呂中辯電子賽務系統

聖呂中辯電子分紙及賽務平台。現行production是原生HTML/CSS/JavaScript + FastAPI + PostgreSQL；Render只啟動一個Uvicorn process，沒有Streamlit runtime。版本唯一來源是[`version.py`](version.py)。

系統涵蓋報名、隊伍名單、賽程、電子分紙、賽果、辯題管治、影片／相片、committee帳戶、AI辯論練習、AI訓練資料、基金及營運工具。

## 主要入口

| 身份／功能 | 路徑 | 權限 |
|---|---|---|
| 主頁及身份導航 | `/` | 公開 |
| 比賽報名 | `/registration` | 報名期內公開 |
| 隊伍名單提交 | `/team-roster?token=…` | 場次專屬random token |
| 電子分紙 | `/judging` | 場次access code |
| 查閱分紙 | `/review` | 場次review password |
| 公開辯題庫 | `/open_db` | 公開 |
| 報名／場次／賽程／賽果管理 | `/admin-hub` | 賽會人員password |
| 主席主持易 | `/chairperson` | 賽會人員password |
| 比賽日投影及 AI評判易控制 | `/projector/control` | 同一賽會人員登入 |
| 比賽日 Kiosk 大屏及錄音引擎 | `/projector?kiosk=1` | 固定 `kiosk` account |
| 辯題徵集、投票及罷免 | `/vote` | committee account |
| 近期比賽資訊 | `/recent-matches` | committee account；高級委員修改 |
| 聖呂中辯歷史 | `/team-history` | committee account；高級委員修改 |
| 老鬼專區 | `/ghost-forum` | 高級委員帳戶（手動指定或任期標示為畢業） |
| 影片重溫、相片、AI辯論、AI訓練、基金 | 主頁committee區 | committee account；部分操作另需delegated role |
| Developer settings | `/dev-settings` | developer password |

完整操作以[`assets/user_manual.md`](assets/user_manual.md)為準；比賽規則以[`assets/rules.md`](assets/rules.md)為準。

## 架構

```text
frontend/*  ──same-origin HTTP／多人WebSocket──>  api/*
                                                        └─> core/*
deploy/proxy.py ── app組裝／static／多人Live rooms ───────┘
                    ├─ PostgreSQL / Supabase：結構化資料
                    ├─ Cloudflare R2：相片及錄音binary
                    └─ AI / TTS providers：按需外部請求

Solo Gemini Live：Browser ──WebSocket（一次性ephemeral token）──> Google Gemini Live
```

- `frontend/`：每頁可直接閱讀的HTML及共用browser assets；不用build step。
- `api/`：HTTP payload、權限、pagination及response。
- `core/`：可獨立測試的業務規則、SQL及storage/provider adapters。
- `deploy/proxy.py`：FastAPI app、靜態路由、多人Live WebSocket rooms及process runtime；
  Solo Live只在此完成登入、地區、配額、prompt及一次性token簽發，audio不經Render。
- 多人Live Mode A的repo實作使用STUN-only WebRTC P2P audio；Render只處理低流量
  control／signaling／逐字稿。實際production版本及真機cutover狀態須另行核實。
- `schema.py`：只供新、空database bootstrap；production baseline及後續schema演進由`migrations/`與`core/db_migrations.py`管理，runtime不再執行舊式retrofit清單。
- 空database bootstrap會idempotently seed現行37句TTS基本句庫；dataset/model及RAG schema仍然fail-closed，不會因首次request自動建立。
- `system_limits.py`：request、RAM、upload、bandwidth、storage及retention限額唯一程式碼來源。

Production schema 以 migration ledger 為準，現行 migration head 由 migration lint／status 輸出；
media binary只存private R2，database只保存metadata。未provision的RLS、自家TTS、自家LLM
schema bundle必須繼續fail-closed，唔可以由request-time DDL自動建立。

## 資料及資源原則

- Browser業務API只call同源FastAPI，永不直連Supabase Data API；Solo Gemini Live以
  短期一次性ephemeral token直連Google，長期provider key不會送到browser。
- 相片及TTS錄音以短期presigned URL直接上／下載R2；Render及PostgreSQL只處理metadata，沒有BYTEA fallback。
- List API在database層filter/count/page；大response、external HTTP、AI、TTS及upload均有code-level上限。
- 設定存於typed `app_config`（namespace + JSONB type + secret classification）；舊`system_config`已由migration `20260714_0002`退役。
- 新password寫入只存bcrypt hash；成功使用legacy plaintext credential登入時會即時升級，但production仍應主動rotate。
- Browser 不提供 database／SQL console；`app_config`及內部`schema_migrations`只可經受控 API、migration 或一次性管理工具存取。

## 本機啟動

### 1. Prerequisites

- Python 3.11+
- PostgreSQL
- 系統CJK字型依賴見`packages.txt`

```bash
python -m venv venv
./venv/bin/python -m pip install -r requirements.txt
```

### 2. Secrets

Production優先讀環境變數，否則直接讀Render掛載的`/etc/secrets/secrets.toml`；本機可用已被Git忽略的`.secrets/secrets.toml`。舊`.streamlit/secrets.toml`只保留一個read-only兼容窗口。

最低需要database：

```bash
export DATABASE_URL='postgresql://USER:PASSWORD@HOST:5432/DATABASE'
```

可選功能按需要設定：

| 功能 | Secrets |
|---|---|
| Gemini及Gemini Live | `GEMINI_API_KEY` |
| AI評判易學生全場錄音 | 測試期間可使用上述 `GEMINI_API_KEY` 的 Gemini Free Tier；系統不再因未設定 `GEMINI_PAID_TIER_CONFIRMED` 而主動封鎖，但預檢會清楚警告 Free Tier 內容可能用於改善產品及由人手審閱。正式學生比賽應使用已啟用 billing 的 Paid Tier project 並設定 `GEMINI_PAID_TIER_CONFIRMED=true`。第一輪以原音製作逐字稿；第二輪同時送出同一段原音、逐字稿、正式場次及出賽名單作交叉核對評審。語音只在有可用 TTS provider 時生成，否則只投影文字 |
| OpenRouter models | `OPENROUTER_API_KEY` |
| Azure TTS | `AZURE_SPEECH_KEY`, `AZURE_SPEECH_REGION`, optional voice/rate/output format |
| Custom TTS | `TTS_PROVIDER=custom`, `CUSTOM_TTS_URL`, `CUSTOM_TTS_API_KEY`, `CUSTOM_TTS_MODEL_VERSION` |
| Web Push | `VAPID_PUBLIC_KEY`, `VAPID_PRIVATE_KEY`, `VAPID_SUBJECT` |
| Private media | `R2_ACCOUNT_ID`, `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY`, `R2_BUCKET`, optional `R2_ENDPOINT` |

實際AI model label/provider mapping只以[`ai_model_config.py`](ai_model_config.py)為準；README不複製會過期的model及價格清單。

### 3. 初始化database及登入secrets

新database先執行bootstrap：

```bash
./venv/bin/python -c "from deploy.proxy import _get_db_engine; from schema import init_db; e=_get_db_engine(); c=e.connect(); init_db(c); c.close()"
```

`init_db`只接受未有`schema_migrations` ledger的新空database；既有／production環境一律使用`tools/manage_db_migrations.py`，Developer網頁不提供runtime DDL入口。

先在本機產生兩個不同的bcrypt hash及一個cookie secret，切勿把plaintext放入SQL、repo或shell history：

```bash
./venv/bin/python -c "from core.auth_logic import hash_password; import getpass; print(hash_password(getpass.getpass()))"
./venv/bin/python -c "import secrets; print(secrets.token_urlsafe(48))"
```

再以受控 migration／一次性管理工具把hash/secret寫入typed store；把placeholder換成剛產生的值：

```sql
INSERT INTO app_config (key, namespace, value, value_type, is_secret)
VALUES
  ('admin_password',     'auth', to_jsonb('<ADMIN_BCRYPT>'::text), 'string', TRUE),
  ('developer_password', 'auth', to_jsonb('<DEV_BCRYPT>'::text),   'string', TRUE),
  ('cookie_secret',      'auth', to_jsonb('<RANDOM_SECRET>'::text),'string', TRUE)
ON CONFLICT (key) DO UPDATE SET
  value=EXCLUDED.value,
  namespace=EXCLUDED.namespace,
  value_type=EXCLUDED.value_type,
  is_secret=EXCLUDED.is_secret,
  updated_at=NOW();
```

### 4. Run

```bash
./venv/bin/python -m uvicorn deploy.proxy:app --host 127.0.0.1 --port 8000
```

Production使用[`deploy/Dockerfile`](deploy/Dockerfile)及[`deploy/start.sh`](deploy/start.sh)。`deploy/start.sh`從`system_limits.py`取得Uvicorn concurrency/WebSocket上限，並為512 MB Render instance設定glibc memory tuning。

現有production/staging環境先以唯讀status核對migration history；所有mutation預設只輸出plan，必須另加對應versioned confirmation。`baseline`只供已核對catalog的既有環境使用，不可拿來掩蓋新database或drift：

```bash
./venv/bin/python tools/manage_db_migrations.py status
./venv/bin/python tools/manage_db_migrations.py apply
./venv/bin/python tools/database_health.py --fail-on-issues
```

## 發布前驗證

```bash
./venv/bin/python -m compileall -q api core deploy
git diff --check
./venv/bin/python tools/manage_db_migrations.py lint
./venv/bin/python -m pytest -q tests
```

GitHub Actions（[`.github/workflows/ci.yml`](.github/workflows/ci.yml)）會對每個
push／PR跑同一批gate。

`tests/`只保留最小offline regression suite（無database、無網絡、秒級）：每個
test對應一種真實發生過或會直接影響賽果／quota／費用的失敗模式，修bug時在此加
對應case，不追求coverage數字。發布前另跑相關production smoke；涉及R2或database
destructive操作的工具預設dry-run，任何驗證成功都不等於production data mutation
批准。

## Database domains

Production資料分為：

- identity/access：accounts、login、notifications、push及typed config；
- competition/scoring：registration、matches、rosters、drafts、scores及rankings；
- topic governance：topic bank、proposals/removals、ballots及comments；
- media：video metadata/activity及R2-backed photos；
- AI/training：usage、consent、scripts、lexicon、R2-backed recordings、LLM submissions及private audit；RAG/model lifecycle仍未provision；
- finance/operations：AI fund、lateness fund、bug reports及resource accounting。

Production exact baseline、drift、feature readiness、table activity、R2 metadata coverage、
typed config分類及權限現況屬時間敏感資料，以`tools/database_health.py`的即時read-only輸出為準。
各optional feature的migration marker、table bundle及lifecycle集中在`core/schema_features.py`；
個別`audit_db_schema.py`、`reconcile_db_schema.py`及`audit_db_access.py`工具仍可作深入檢查。
Fresh bootstrap同migrated staging可用`tools/compare_db_catalogs.py`比較語意catalog；
release要求的最低schema migration由`version.py`明確指定，database可在受控staged rollout期間先行升級。

## 營運文件

- [`docs/SERVICES_COSTS_AND_LIMITS.md`](docs/SERVICES_COSTS_AND_LIMITS.md)：外部服務成本速查

## 維護規則

1. 業務規則只放`core/`；API不複製threshold/formula，HTML不自行決定權限。
2. 新SQL必須parameterized，list query有界，寫入同一業務動作用transaction。
3. 新table/column/index/RLS要有versioned migration、rollback及可重現permission驗證。
4. HTML source保持正常縮排，不提交minified inline document；共用樣式/行為放`frontend/shared/`。
5. Runtime需要的assets才放`assets/`；只更新真正受影響的職責文件，不新增散落migration diary。
6. 修bug附可重現驗證步驟，並在`tests/`加入對應regression case。資源改動同時更新`system_limits.py`及對應docs。

Maintained by lzlovecats and contributors.
