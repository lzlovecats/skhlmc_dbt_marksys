# Supabase RLS 啟用計劃

> 狀態：設計完成，**尚未對資料庫執行任何 RLS／grant／role 變更**。
>
> 目標：在不中斷現有 Streamlit + FastAPI 雙軌期的前提下，把 Supabase PostgreSQL
> 由「後端高權限直連、RLS 未開」遷至可驗證、可回滾的最小權限模型。

## 1. 先決結論

目前系統不是 Supabase Auth：委員身份是 `committee_user` HMAC cookie，後端由
`deploy/proxy.py`／Streamlit 以 PostgreSQL connection string 直接查表。因此：

1. 不可直接套用 `auth.uid() = user_id` policy。現時沒有 JWT claim，`auth.uid()` 不會等於
   `accounts.user_id`。
2. 用 `postgres`、table owner、`service_role` 或任何 `BYPASSRLS` role 連線，會令 RLS
   失去保護作用。table owner 亦會繞過 RLS，除非設定 `FORCE ROW LEVEL SECURITY`。
3. Browser 目前不會直連 Supabase Data API；正確方向是 browser 只 call 同源 FastAPI，
   FastAPI 再用受限 DB role。不要把 database URL、secret key 或 service-role key 放進 HTML。
4. RLS 是第二道防線，不會取代現有 API authentication、input validation、CSRF/XSS 防護和
   audit log。

Supabase 要求 exposed schema 的表應啟用 RLS；service role 會繞過 RLS，view 預設亦可能以
建立者權限執行。[Supabase RLS 文件](https://supabase.com/docs/guides/database/postgres/row-level-security)

## 2. 建議最終架構

```text
Browser
  -> same-origin HTML / FastAPI API
  -> verify signed committee_user cookie
  -> request-scoped DB transaction sets trusted app.user_id / app.role context
  -> app_backend (LOGIN, NOBYPASSRLS, non-owner)
  -> public tables with RLS policies

Migration / emergency only
  -> separate break-glass credential (never in app/runtime/frontend)
```

`app_backend` 是唯一 production runtime DB user。它只可登入、不可建立 role/database、不可
superuser、不可 replication、不可 `BYPASSRLS`，亦不應擁有 application tables。schema migration
和 emergency repair 用獨立、短期保管的管理憑證。

## 3. 分期執行與停損點

### Phase 0 - Inventory、備份、staging 基線

**目的：先知道實際資料庫狀態，不能以 `schema.py` 取代 production inventory。**

1. 在 Supabase 建立 staging project 或 production restore 的隔離副本；確認 extension、view、
   trigger、function、role membership 一併存在。
2. 匯出 schema-only、roles/grants、RLS policy、row count、關鍵 workflow fixture；production
   先做可還原 backup 和 change window。
3. 在 SQL editor 以管理帳戶執行以下唯讀 audit，輸出存入變更 PR：

```sql
select c.relname, pg_get_userbyid(c.relowner) as owner,
       c.relrowsecurity as rls_enabled, c.relforcerowsecurity as force_rls
from pg_class c join pg_namespace n on n.oid = c.relnamespace
where n.nspname = 'public' and c.relkind = 'r'
order by c.relname;

select table_name, grantee, privilege_type
from information_schema.role_table_grants
where table_schema = 'public'
order by table_name, grantee, privilege_type;

select schemaname, tablename, policyname, roles, cmd, qual, with_check
from pg_policies where schemaname = 'public'
order by tablename, policyname;

select c.relname as view_name, pg_get_viewdef(c.oid, true) as definition
from pg_class c join pg_namespace n on n.oid = c.relnamespace
where n.nspname = 'public' and c.relkind = 'v';
```

**Gate A：**全表、view、function、trigger、role grant inventory 已由第二人 review；staging 可由
同一 image 啟動、可登入及可讀取現有頁面。未達成不得 enable RLS。

### Phase 1 - 收窄 Data API 與 credential surface

1. 確認 frontend 沒有 Supabase publishable/anon key、service key、database URL 或 direct
   PostgREST query。現時 HTML 已透過 FastAPI，保持此模型。
2. 若不需要 browser Supabase Data API，從 `anon`、`authenticated` 移除 application table／view
   的 grants；只保留 Supabase 內建 schema 所需 access。若將來需要 direct API，必須把該資料改到
   explicit allowlist 表並另設 RLS policies，不能把全個 `public` 打開。
3. Rotate 曾被放進 CI、log、repo、前端或已分享渠道的 database/service credentials。確保
   `.streamlit/secrets.toml`、Render `DATABASE_URL` 及本機 secret 均不會被 commit。

**Gate B：**用 anon/publishable key 驗證不能讀 application rows；現有 `/api/*` read smoke 仍過。

### Phase 2 - 建立受限 runtime DB role

在 staging 先建立 application role。password 經 secret manager 注入，下例中的 placeholder
不可直接提交或複製到 shell history：

```sql
create role app_backend
  login nosuperuser nocreatedb nocreaterole noreplication nobypassrls
  noinherit password '<managed-secret>';

grant usage on schema public, extensions to app_backend;
grant select, insert, update, delete on all tables in schema public to app_backend;
grant usage, select on all sequences in schema public to app_backend;
alter default privileges in schema public
  grant select, insert, update, delete on tables to app_backend;
alter default privileges in schema public
  grant usage, select on sequences to app_backend;
```

1. 將 proxy `DATABASE_URL` 和 Streamlit PostgreSQL connection 改成 `app_backend`；它不可為
   table owner。不要使用 `postgres` 或 `service_role` 作 runtime identity。
2. 先不開 RLS，以 staging 跑完整 smoke，找出漏掉的 table、sequence、schema 或 function grant。
3. 寫一條 deploy health check：`current_user` 必須是 `app_backend`，且
   `rolbypassrls = false`。此 check 僅記錄 role metadata，不輸出 connection string。

**Gate C：**所有頁面、background task、push、AI、Streamlit 和 FastAPI 都以 `app_backend` 成功運作；
沒有 runtime SQL 依賴 table owner／superuser。

### Phase 3 - 建立可被 policy 信任的 request context

現時 `_ProxyDb` 的每次 `query/execute` 可使用不同 transaction；不可在 request 開頭 `SET` 一次
再假定後續 query 仍持有該 user id。這是本遷移的主要程式改動。

推薦做法：

1. 在 DB access layer 加 `request_transaction(user_id, account_status)`；在同一 connection +
   transaction 內以 parameterized `set_config(..., true)` 設置 `app.user_id`、`app.account_status`，
   並執行該 request 的所有 SQL。只可由已驗證 cookie/bearer identity 設值，絕不可直接取 header／
   frontend payload。
2. 在私有 schema 建 helper，例如 `private.current_user_id()`，讀取
   `current_setting('app.user_id', true)`；設定固定 `search_path`，restrict function execute，避免
   `SECURITY DEFINER` search-path injection。
3. Public HTML endpoint（例如 open-db）以 `NULL` context 加最少讀取 policy；committee endpoint
   必須先經 `_require_committee_user`，再進 request transaction。
4. 若短期不能把 DB executor 改為 request-scoped transaction，RLS 只能做「backend can access、
   browser roles cannot access」的 perimeter policy，不能聲稱已做到 per-member row isolation。此時
   停在 Phase 4a，不能啟用 user-owned policies。

**Gate D：**同一 connection pool 下交錯兩位 test member 的 100 次 request，均不可讀到或寫到對方
private rows；missing context 必須 deny，而不是 fallback to all rows。

### Phase 4 - 按敏感度逐批政策遷移

每張 table 先在 staging：寫 policy -> `ENABLE ROW LEVEL SECURITY` -> dual-role test -> `FORCE ROW
LEVEL SECURITY`（確認 owner case）-> explain/analyze。每批只涵蓋 2-5 張相依表，觀察後才進下一批。

| 批次 | Tables / views | policy 方向 |
|---|---|---|
| 4a Public read | `topics`、已完成 `topic_votes` aggregate 所需資料、公開 `match_videos` / `match_photos`（以 inventory 為準） | anon only read approved/visible rows；寫入一律 deny。open-db 目前只需要 `topics` + `topic_votes` read。 |
| 4b Member-owned | `notification_reads`、`push_subscriptions`、`video_progress`、`video_views`、`video_votes`、`tts_voice_consents`、`bug_reports`、`ai_fund_usage_logs` | SELECT/INSERT/UPDATE/DELETE 均限制 `user_id` 或 reporter/owner = trusted context；admin read 另加明確 policy。 |
| 4c Committee shared | `topic_votes`、兩張 ballot、兩張 removal vote、`motion_comments` | active committee 才可 read pending/shared records；ballot insert/update/delete 限本人，proposer only create；status 結算必須走受控 admin path。 |
| 4d Highly sensitive | `accounts`、`login_records`、`system_config`、`scores`、`score_drafts`、`ai_fund_transactions`、lateness tables、TTS recording / training submissions、roster access code fields | 預設 deny；只容許 owner 或 defined admin capability。`password_hash`、cookie secret、access/review hashes 不可由一般 query / view 暴露。必要時以 column privilege 或安全 function 封裝。 |
| 4e Derived data | `committee_vote_activity_view` 及所有 inventory 新發現 view/function | 先 revoke anon/authenticated；Postgres 15+ 的公開 view 明確加 `security_invoker = true`，否則搬到 unexposed schema 或由 API 查 base table。 |

每張 table policy 的格式必須明確 `TO app_backend` 及 operation。例子（僅為 template，table／column
名和帳戶狀態要先以 Phase 0 schema 確認）：

```sql
alter table public.notification_reads enable row level security;
alter table public.notification_reads force row level security;

create policy notification_reads_self on public.notification_reads
  for all to app_backend
  using (user_id = private.current_user_id())
  with check (user_id = private.current_user_id());
```

不要用一條 `USING (true)` 作快捷做法；那只是在 RLS 名義下重新開放資料。對於 admin action，另建
`private.is_admin()` policy，並以 request context 的 verified `accounts.account_status` 取值；不得相信
client 自報 role。

**Gate E：**逐表 permission matrix（anon、一般 active、inactive、admin、missing context、
`app_backend` migration runner）有 automated test；所有原本使用的 path 都有 allow，所有不應有的
row／column／operation 都有 deny。

### Phase 5 - Production canary、觀察與收尾

1. 排定低流量 change window，先部署支援 request context 的 code，再 deploy policy migration。
2. 先開 4a，再 4b，最後 shared/sensitive；每批之間監察 401/403/500、Postgres permission denied、
   latency、connection pool、RLS policy scan、前端 error rate。
3. 增加 policy predicates 所用欄位索引，例如 `user_id`、`reporter_user_id`、`status`、
   `is_visible`，但每個 index 先 `EXPLAIN (ANALYZE, BUFFERS)` 量度，避免盲加。
4. 將 RLS DDL、grants、view/function definitions 納入 migration 檔及 CI schema check；新建 public
   table 的 PR template 必須包含 data classification、grants、RLS policy、indexes 和 test。
5. 完成 7-14 日沒有 unexpected deny/error 後，移除舊高權限 runtime credentials 和任何臨時 broad
   grant。

## 4. 測試清單

- `open-db` public read：可得到現在的 `topics` / resolved vote statistics，不能寫入。
- 未登入：不能讀 account hash、cookie secret、login records、private push endpoint、score draft、
  AI usage / fund / lateness / voice recording。
- Member A：只能處理自身 notification、push subscription、video progress、bug report、ballot。
- Member B：嘗試猜 ID、改 payload 的 `user_id`、改 URL／body 均拒絕。
- Inactive member：依 `vote.py` 現行 active rules，不能投票或提出動議。
- Admin：只在現行 UI 已有 admin capability 的操作放行；每次敏感寫入記錄 actor/time/object。
- API 和 Streamlit：同一 test fixture 結果一致；cache 不能跨 member 泄漏 private response。
- View/function：direct query 確認不會以 owner/security-definer 權限繞過 policy。
- Load test：member list、vote statistics、open-db、video list 在 RLS 後 p95 不惡化到不可接受。

## 5. Rollback

每批 migration 必須隨附同版本 rollback SQL：drop 新 policy、restore 前一版 view/function/grant；
`DISABLE ROW LEVEL SECURITY` 只作短暫事故緩解，需 incident log 和最短 expiry。不要以授權
`BYPASSRLS`／把 service key 放回 runtime 作為 rollback。若 Phase 4 導致登入或核心寫入中斷：

1. 暫停該 deployment，保留 request/error logs 和 policy version。
2. 回退至上一批已驗證的 policy/grant 版本或 application image。
3. 以 staging fixture 重現後修正；只有完成 deny/allow regression test 才重試 production。

## 6. 可驗收定義

RLS 不是「Dashboard 顯示 enabled」便完成。完成條件是：所有 public tables 都有 RLS，runtime role
沒有 bypass/owner escape，browser 沒有高權限 key，所有 app paths 通過 permission matrix，private row
isolation 已用 pooled connection cross-user test 證實，view/function 沒有 privilege bypass，及 rollback
已演練。官方參考：[RLS](https://supabase.com/docs/guides/database/postgres/row-level-security)、
[Postgres roles](https://supabase.com/docs/guides/database/postgres/roles)、
[securing the Data API](https://supabase.com/docs/guides/api/securing-your-api)。
