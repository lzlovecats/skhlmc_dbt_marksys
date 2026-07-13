# 統一後續路線圖：Database、RLS、自家 TTS 及辯論 LLM

> 最後整理：2026-07-13。這是所有未完成研發／基建工作的唯一主計劃；每一步都有前置條件、驗收 gate及停損點。完成一個 gate才進下一個，避免同時改 database權限、模型及production runtime而無法定位問題。

## 0. 現況與優先次序

| 工作線 | 現況 | 下一個可執行動作 |
|---|---|---|
| Repo/runtime簡化 | HTML + FastAPI已完全接管；舊 Streamlit runtime已清理 | 繼續按 domain拆細 `deploy/proxy.py`，不用再保留雙軌相容 |
| Typed settings | `app_config` schema、registry、legacy fallback及統一secret loader已落地 | 部署後核對 key數／secret分類，rotate舊 credential，再移除 `system_config` bridge |
| R2 media | 新讀寫已是 R2-only；production舊 BYTEA仍約121.8 MB | Browser抽樣播放 → finalizer dry-run/HEAD verify → 獲批准後drop legacy columns |
| Versioned DB migrations | `schema.py`仍是bootstrap + retrofit混合 | 建立production baseline及migration runner，才清重複indexes/FKs |
| RLS | Production 41張 public tables全部未啟用 | 先建立非owner、NOBYPASSRLS runtime role及request transaction context |
| 讀音字典 | `tts_lexicon` UI及文字覆寫runtime已完成 | 收集100–200句固定讀音測試，填高頻term/reading/jyutping |
| 自家粵語聲線 | consent、R2錄音、審核、accepted export及provider abstraction已完成 | 選一位主聲線，收30–60分鐘accepted音訊，做immutable snapshot |
| 辯論 LLM | 文字收集、審核、export及部分eval/RAG基建已存在 | 固定20–50條eval cases，先做RAG A/B，證明不足才LoRA |

建議順序：先做 P0安全收尾 → P1 migration baseline → P2 RLS perimeter → TTS/LLM可平行收集 → 模型實驗 → 最後才做per-member RLS及自家模型production rollout。

---

## P0. Production安全及儲存收尾

### P0.1 部署 typed `app_config`

- [ ] Render production已對`main`開啟auto-deploy；先只推／驗證`develop`，到低流量發布窗口及rollback commit準備好後才push `main`一次。
- [ ] Deploy包含 `app_config`及legacy read bridge的版本；保留舊 `system_config`作一個rollback窗口。
- [ ] 在production只讀比對兩表全部 keys、namespace、type及`is_secret`；不可在log輸出value。
- [ ] 使用`tools/audit_app_config.py`保存只含metadata及boolean health checks的audit結果；不可加入secret value或hash。
- [ ] 驗證首頁、committee login、admin/dev/SQL login、AI provider設定、基金角色、TTS角色、投票分析cache及resource warnings。
- [ ] 以新密碼rotate `admin_password`、`developer_password`及`sql_password`；全部只存bcrypt hash。
- [ ] Rotate `cookie_secret`會令現有cookies失效，安排低流量時間並預先通知使用者重新登入。
- [ ] 觀察至少一個release；確認沒有fallback-only key後，以versioned migration刪舊table及bridge code。

**Gate P0-A：**typed/legacy key集合一致；所有登入及settings workflow通過；SQL console不能讀寫`app_config`或`system_config`；production沒有plaintext credential。

### P0.2 回收 Supabase舊 binary（需明確批准）

Production audit基線：`tts_voice_recordings` 148/148及`match_photos` 45/45已有R2 key，但舊BYTEA仍分別約110.9 MB及10.9 MB。

- [x] Finalizer已具備bounded keyset讀取、逐object size／SHA／MIME／cache metadata驗證、aggregate JSON報告、fail-fast及transaction lock timeout；本地tests不接觸production。

1. 在真實browser抽樣播放／下載不同格式錄音、原圖及thumbnail。
2. 確認R2 lifecycle、CORS、presigned expiry及backup符合 `docs/R2_MEDIA_MIGRATION_RUNBOOK.md`。
3. 執行 `tools/finalize_r2_media.py` 的預設dry-run；工具要對每個object做HEAD、size/hash/metadata核對，任何一項失敗即停。
4. 保存dry-run摘要、database backup及rollback說明。
5. 只有獲明確批准及confirmation token後才在maintenance window使用`--apply`移除舊BYTEA columns。
6. `VACUUM (FULL)`會鎖表；不要自動執行。先觀察Supabase實際storage回收方式，再另排maintenance。

**Gate P0-B：**所有R2 objects核對成功、browser抽查通過、backup可還原、使用者批准。未達成時保留columns，不影響新R2-only路徑。

---

## P1. Database migration baseline及schema瘦身

### P1.1 建立production truth

- [ ] 在staging restore執行schema-only dump，保存tables、columns、types、defaults、constraints、indexes、views、functions、triggers、owners、grants、RLS及row counts。
- [ ] 對比`schema.py`：production-only、code-only及definition drift逐項分類。
- [ ] 為現有production狀態建立只作baseline的migration version；不要把41張表重新CREATE一次。
- [ ] 選用輕量versioned runner（Alembic或repo內編號SQL均可），每個migration有forward、rollback、transaction界線及checksum。
- [ ] Startup只跑未套用migration；移除endpoint-local DDL及request-time `CREATE INDEX/TABLE`。

### P1.2 第一批schema修正

在staging以catalog name及`EXPLAIN`再確認後：

- [ ] `score_drafts`只保留一個真正的 `(match_id, judge_name, side)` unique constraint/index及一個match FK。
- [ ] `scores`只保留一個match FK。
- [ ] 移除`match_roster_links`重複的普通token index，保留UNIQUE。
- [ ] 若ballot常見查詢可用PK leading column，移除兩個額外topic-only indexes；保留user indexes供member history。
- [ ] 把`projector_state`、`ai_coach_live_briefs`正式加入migration/schema owner。
- [ ] 查清`tg_notification_queue`的writer/consumer；無owner、無runtime引用及保留需要才備份後移除。
- [ ] 只建立實際啟用AI功能需要的snapshot/model/eval/RAG/resource tables，避免「schema宣稱存在、production其實沒有」。
- [ ] 重構topic/removal motions為獨立`motion_id` PK；`topic_text`只是內容欄，為pending狀態加partial unique，ballots/comments引用motion id。遷移現有歷史後，removal motion不再因topic被罷免而cascade刪除；API仍可用pending motion id明確投票。
- [ ] 把`scores.submitted_time`遷移為`submitted_at TIMESTAMPTZ`（以可證明的舊資料時區/日期回填，無法證明則標記unknown而非猜）；production `score_drafts.score_payload`已是JSONB，驗證每個值為object並清理歷史double-encoded payload。
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

1. 只選一位願意長期授權、聲線清楚、錄音環境穩定的人作v0，避免混聲令音色及撤回責任不清。
2. 同意書明列voice cloning、內部用途、保存、雲GPU、撤回及未成年guardian安排。
3. v0收30–60分鐘accepted音訊；正式版1–3小時。每段1–60秒、單一speaker、安靜、無爆咪、稿音一致。
4. AI quality check只作預篩；管理員逐段試聽。只有accepted可export；rejected寫原因；撤回即withdrawn。
5. 在AI Training按speaker下載`recordings.json`；manifest只含metadata及短效R2 signed URLs，音訊不經Render。離線工具會逐檔下載、核對size/SHA-256及音訊metadata，再產生immutable dataset snapshot、train/validation/test split、manifest及quality report。固定test不可回流train。

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

- [ ] 整理有權使用且已匿名化的規則、評分準則、術語、優秀稿、評語、攻防、主線及逐字稿。
- [ ] 只有accepted submissions進dataset/RAG；撤回會令所有derived chunks/snapshots失效。
- [ ] 固定20–50個eval cases，覆蓋發言評核、主線、追問、反駁、Mock評語及引用規則。
- [ ] Rubric量度：香港粵語自然、事實/引用準確、引用到user內容、具體改善建議、非空泛、無敏感資料。
- [ ] 所有prompt/provider/model/RAG改動都對同一eval set跑，保存版本、latency、tokens、cost及盲評。

### P5.2 RAG v0

1. Accepted source → immutable snapshot → document/chunk；保留source id、consent/version及hash。
2. Index只包含approved資料；reindex可重跑及原子切換，不在request同步重建。
3. Retrieval先使用簡單可解釋baseline（metadata filter + text/vector search），保存top-k引用及分數。
4. 回答必須區分source內容與模型推論；無可靠context時明示不知道，不捏造賽規。
5. 以eval比較prompt-only vs RAG；只有品質有實質提升才接入更多功能。

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

- [ ] 把`deploy/proxy.py`分成app factory、static/projector router、Gemini relay、practice room service、TTS service及resource accounting；每次只搬一個bounded domain並保持tests。
- [ ] 把process-local room/admin session/cache搬到Redis或database前，Render維持單worker；完成後才評估多worker/多instance。
- [ ] Custom TTS provider失敗後加入短TTL circuit breaker/cooldown；冷卻期直接走Azure fallback，避免每個request先重複等同一個timeout。
- [ ] `video_views`只保留近期去重所需raw events，另以durable aggregate保存歷史count；先設計backfill及核數，再加retention，避免永久event rows令aggregate愈來愈貴。
- [ ] AI model availability及價格在release前以官方provider資料核對，改成帶生效日期的versioned pricing metadata；不要讓過期硬編碼影響基金核數。
- [ ] R2 exact-usage refresh若擴至多worker，以DB advisory lock或獨立排程協調；process-local lock只適用現行單worker。
- [ ] 遲到基金通知先一次batch讀取所有target subscriptions，再用有上限並行發送及batch更新結果，避免逐會員/逐裝置同步DB與network N+1。
- [ ] 公開`/api/home/status-check`加短TTL cache及IP rate limit，或移入developer gate；避免重複aggregate及披露不必要的營運數字。
- [ ] 為bug reports建立resolved archive/retention；達總量上限時先清理已關閉舊資料，不令新critical report完全無法提交。
- [ ] 將login lifecycle/audit的best-effort broad exceptions改為不含secret的structured warning/metrics，保持登入可用同時讓失敗可觀察。
- [ ] 逐步以row mapping/repository method取代hot path Pandas DataFrame，保留Pandas只在真正tabular export/analysis。
- [ ] 收斂frontend shared shell/table/form helpers，但維持原生HTML/CSS/JS及可讀source，不引入超出團隊維護能力的build stack。
- [ ] `/open_db`及`/vote/depose-data`改為server-side search/pagination，不再每次把最多2,000題整個topic bank送到browser；保留圖表aggregate及抽取候選題所需的最小fields。
- [ ] 聯中自由辯論若每方各10分鐘，「雙方」segment會超過目前780秒Live session budget；先決定拆成兩個可handoff segment或提高經量度的budget，再改room protocol及成本上限。
- [ ] 由賽制負責人確認敗方賽是否只應包含第一輪敗方；確認前保持現行結果並加入fixture，確認後才改draw algorithm。
- [ ] 每季做一次table/index/retention/R2 orphan/dependency audit；報告只保存結論及可重現query，不累積過期migration diaries。

## 每次完成一階段要記錄甚麼

在相關PR/release記錄：目標、production/staging基線、改動migration/code、測試命令及結果、資源前後差異、security/privacy影響、rollback、owner及下一步。不要另開散落的「暫時計劃」文件；更新本roadmap的checkbox與日期即可。
