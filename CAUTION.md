# Coding cautions

本文件位於 repository root，由現行 FastAPI、frontend、PostgreSQL migration、R2、AI、Live room、appliance 同 regression suite 嘅實際 contract 整理而成。寫 code 前先讀 [`AGENTS.md`](AGENTS.md)；下面每一項都係呢個 repo 容易出現「local 正常、production 錯」或者靜默破壞資料／賽果／私隱／費用嘅位置。

## 先停手嘅情況

- **Bug 根源未喺 production 證實：**唔好 patch 猜測。無 production access／log／data 證據就向使用者交代 blocker。
- **功能細節有多過一個合理答案：**唔好代使用者揀。先列方案、影響同建議，問清楚先做受影響部分。
- **需要 deploy、production write、migration apply/rollback、secret rotation、R2 delete、發通知或付費 provider action：**repo 修改唔等於有外部 mutation 授權。
- **現有 working tree 有不明改動而會同工作重疊：**唔好覆蓋；先查清楚或問使用者。

## 1. 唔好打破 production 單 process 假設

Render 只開一個 Uvicorn process。Live rooms、部分 token retry cache、console sessions、locks、semaphores、short-lived results 同 maintenance cache 係 process-local。

- 唔好加 worker、background process 或第二 instance 後假設 state 會共享。
- 唔好用普通 `threading.Lock`／dict 當 database-wide consistency；跨 request／process 正確性要靠 database constraint、conditional write、transaction 或 advisory lock。
- Process restart 會清短期 state。需要 durable 嘅資料要明確設計 schema、retention 同 recovery，唔好悄悄將 in-memory object serialize 落任意 config。
- Room control 未完整拆分並將共享 state 外置前，唔好將 `deploy/proxy.py` room globals 搬一半，否則 cleanup、capacity、socket replacement 同 judgement semaphore 會失去共同狀態。

## 2. `deploy/proxy.py` 路由次序同 catch-all 好容易吞 request

`deploy/proxy.py` 同時組裝 routers、middleware、static/page routes、Live HTTP/WebSocket，最後仲有 WebSocket 同 HTTP catch-all。

- 新 router 要喺 catch-all 前 register；新 page alias 要核對 maintenance/auth/cache headers。
- 唔好用 broad catch-all 代替明確 API 或 asset route，亦唔好令 page route 同 API prefix 撞名。
- Middleware 次序會影響 body buffering、gzip、CORS、bandwidth gate 同 error response；改之前讀完整 app setup。
- TLS 喺 proxy terminate；WebSocket same-origin check 需要尊重 forwarded protocol。唔好直接用 backend socket scheme 判斷 production origin。

## 3. API、core、frontend 唔可以互相複製 authority

正確方向係 frontend 收集輸入、API 驗證身份／payload、core 執行業務規則。常見錯法係喺 HTML 隱藏按鈕就當有權，或者喺 API 重寫 core formula。

- UI visibility 只係 UX，唔係 authorization。每個 read/write endpoint 都要 server-side require 正確 page/capability/owner。
- 唔可信 client 傳入嘅 `user_id`、admin flag、role、side、owner、match metadata、status、duration、price、usage、MIME 或 R2 key。
- Committee、registration admin、developer、DB/SQL、kiosk、judging、review、roster link 同 delegated manager/treasurer/reviewer 係不同權限，唔可以互相當同一種 login。
- 改 page access 要同步 `account_access.py`、API gate、home navigation 同 access-policy tests，唔好只改其中一層。

## 4. Authentication 改動要保留 revocation 同限流

現有 session/token 唔只驗 signature；部分亦綁 password hash、account status、match credential 同 expiry，令改密碼／停用帳戶／換 access code 可以即時 revoke。

- 唔好為方便改成永不過期 cookie，亦唔好移除 credential version binding。
- Login rate limit 要喺 database lookup 同 bcrypt 前執行，否則匿名 request 可消耗 DB/CPU；保留 per-account/client/global bounds。
- Cookie issue 同 delete 要同 path 對稱，並保留 `HttpOnly`、`SameSite`、secure policy 同 max age。
- 普通會員 login 禁止 reserved/system identity；kiosk 身份由 server 固定，唔可以由 caller 自選。
- Password 只存 bcrypt；讀到 legacy plaintext 時只可沿現有成功登入後升級路徑，唔好將 plaintext 回傳、log 或複製去新 table。
- Signed claim/token 要同時驗 size、format、signature、expiry、subject、server-authoritative fields 同 replay/state；只驗 signature 通常唔夠。

## 5. Database 有兩條路，唔好混用

`schema.py:init_db()` 只畀全新空 database；production／既有 database 用 migration ledger。常見災難係修 production drift 時重跑 bootstrap，或者只改 `schema.py` 以為 production 會自動更新。

- Production runtime 禁止 request-time DDL／auto-retrofit。
- 每次 schema change 新增 immutable、成對 up/down migration；已 apply 嘅檔案唔改 checksum。
- 同步核對 table constants、bootstrap DDL、indexes、foreign keys、feature readiness、tooling、tests 同 rollback。Bootstrap parity 唔代表可以 skip migration。
- `baseline.json` 只描述已核實既有 catalog；唔好用 baseline 掩蓋 pending migration、unknown version 或 drift。
- Migration SQL 唔可以包含 transaction control 或 literal `%`；runner 自己包 transaction，而 psycopg2 raw execution 會將 `%` 當 interpolation。
- 新 table 要明確 revoke `PUBLIC`、`anon`、`authenticated`；現時 backend role 仍可 `BYPASSRLS`，全面 RLS 尚未完成，唔好錯誤聲稱 migration 已得到完整 RLS 保護。
- Dataset/model/eval/RAG 係有意 fail-closed 嘅 feature bundles。未完成 schema marker、permission、retention、audit 同 rollback 前，endpoint 應 503，唔好自動 create table 或先做付費工作。

## 6. SQL parameter 唔包括 identifier

Values 必須用 `:param`；table／column／`ORDER BY` 不能用 value parameter，所以只可由 hard-coded constant 或 allowlist 揀出。

```python
# 錯：user input 直接插入 SQL
sql = f"SELECT * FROM records ORDER BY {requested_sort}"

# 對：先映射到固定 identifier，其他值仍然 parameterized
sort_sql = {"newest": "created_at DESC", "name": "name ASC"}[sort_key]
rows = db.query(f"SELECT * FROM records WHERE owner=:owner ORDER BY {sort_sql}", {"owner": owner})
```

唔好用手動 quote、replace apostrophe 或 string concatenation 冒充 parameterization。

## 7. `RuntimeDb.query()` 回傳 Pandas DataFrame

Production 正常 request 仍可能載入 Pandas。DataFrame contract 有幾個常見坑：

- `if frame:` 會拋 ambiguous truth error；用 `frame.empty`。
- 單值要先處理 empty，再用 `.iloc[0]`；唔好假設 row 一定存在。
- SQL `NULL` 可能變 `NaN`／`NaT`，numeric 可能係 NumPy/Decimal，直接交畀 JSONResponse 會失敗或產生非標準 JSON。沿用 `api.pagination.json_safe` 或現有 domain normalizer。
- `.astype(str)` 會將 null 變成字串 `"nan"`／`"None"`；對 token、password、owner、date 特別危險。
- DataFrame merge/pivot 可能改 type、sort 或丟 duplicate semantics；評分、排名、pagination 改動要用現有 regression data 驗證。
- 新 web code 唔好擴大 DataFrame contract；長期方向係逐步改用 lightweight row mappings，但呢個轉換本身係行為改動，不能順手做。

## 8. Transaction 邊界要包住成個業務動作

`RuntimeDb.execute()` 每次自己開 transaction；連續 call 三次唔等於同一 transaction。喺 `with db.transaction() as conn:` 裡面再 call `db.execute()`／`db.query()`，更加會開另一條 connection。

- 評判正式提交要原子寫正反兩邊、總分、八位辯員細分，並阻止重複提交。
- Roster token claim、upload reservation/completion、AI budget notification、room judgement、resource reservation 等要 atomic compare-and-set／lock／unique constraint。
- Check-then-insert 唔能夠防 concurrent requests；將 invariant 放入 DB constraint，再妥善處理 conflict。
- 唔好將慢 external call 放喺 open transaction；先短 transaction reserve，call 完再短 transaction settle。Failure path 要標示 failed/retryable，唔好留下永久 `processing`。
- Idempotency 唔係只 return 200；要確保 retry 唔會重複寫、重複通知、重複 charge 或覆蓋另一個 request 結果。

## 9. 評分／賽果係高風險 domain，唔好順手「簡化」

評分 formula、正反方總分、四位辯員分、最佳辯論員 submitted ranking、derived fallback、tie 同 PDF/review consistency 係連成一套 contract。

- Formula 只改 `scoring.py`／相關 core source，唔好喺 JS、API、PDF 各自重寫常數。
- 正式分紙兩邊同八個細分要一個 transaction；duplicate submit 必須喺任何 score write 前截停。
- 每位 judge 獨立採用佢提交嘅 ranking；缺失先 derived。唔好因另一位 judge 有 ranking 就改晒全場來源。
- Exact tie 要保留畀評判團處理，唔好私自加 alphabetical、side 或 row-order tie breaker。
- Non-finite、缺欄、舊 draft payload 要 fail closed／清楚報錯，唔好靜默補 0。
- 改動要同步 judging UI、draft、final tables、results、review、PDF 同 tests。

## 10. 時區、月份同日期唔係全部 UTC

業務日界、會員 active、投票 deadline、基金年度、AI budget cycle、resource month 主要按 `Asia/Hong_Kong`；provider/storage timestamp 多數用 UTC。Database 同舊 columns 同時存在 naive `TIMESTAMP`、aware `TIMESTAMPTZ` 同 text ISO。

- 唔好全域將 `datetime.now()` 換成 UTC 或反過來；先確認該欄位同業務規則。
- Aware/naive 比較前明確 normalize；唔好靠本機 timezone。CI 通常係 UTC，production／使用者係香港。
- 月度 window 用 half-open `[start, end)`；香港午夜、每月 25 號 AI budget cutoff、學年／財政年邊界要有 boundary tests。
- Browser `<input type=datetime-local>` 無 timezone；server 要按香港本地時間解讀並拒絕模糊／過期值。
- SQL `CURRENT_DATE`／`NOW()` 取決於 DB session timezone；需要香港日界時明寫 `AT TIME ZONE 'Asia/Hong_Kong'` 或傳入已計算 boundary。

## 11. `system_limits.py` 先係限額來源

唔好將 bytes、rows、TTL、timeout、concurrency、retention 或 provider output 限額散落成新 magic number。

- 新資源 limit 用 `_limit()` 設 default/min/max/group/description，再由 consumer import resolved constant。
- Env override 會被 clamp，而且 import/startup 讀一次；改環境變數後要 restart/redeploy。
- `deploy/start.sh` 對 startup contract 採 fail-fast；改 startup limit 名要同步 export format 同 shell case。
- Pydantic 字段長度可以留喺 payload model，但保護 Render/R2/provider/database 嘅界限要放 `system_limits.py`。
- 移除 quota 唔代表可以移除 technical safety：frame size、rate、room capacity、timeout、system-wide monthly gate、upload size 仍要保留。

## 12. R2 只放 private binary，DB 只放 metadata

相片、AI Coach 錄音、TTS training 錄音、Kiosk 全場錄音都唔應經 Render body/base64 或落 PostgreSQL BYTEA。

- 正確 lifecycle：server reserve intent → 簽短期 owner-bound claim/presigned URL → browser 直傳 private R2 → server HEAD/probe/metadata verify → atomic finalize DB metadata。
- Browser 提供嘅 size、hash、MIME、duration、dimensions、key 全部只係聲稱。要驗 object path/scope、HEAD metadata、實際 container/codec/duration/image properties。
- Completion 失敗／中斷要保持可由 orphan sweeper 找到；唔好先刪 intent row 再做最後驗證。
- 任何 object delete 預設 dry-run／需明確授權。DB row delete 同 R2 delete 有次序同 rollback 風險，要逐個 lifecycle 設計。
- Presigned download URL 有期限而且係 bearer access；唔好存入 DB、training manifest provenance、log 或長期 cache。
- 舊 `audio_data`／`image_data` path 已退役；唔好加 fallback「救急」，否則會重新引入 RAM、DB size 同 privacy 問題。

## 13. Media validation 唔可以只睇 extension/MIME

`core/media_probe.py` 用 ffprobe/ffmpeg 驗實際內容同 bounded transcode。

- Extension、browser MIME 同 magic/container/codec 要一致；browser 常見 MIME variant 要 canonicalize，但唔好接受任意組合。
- Duration 要由 probe 讀，並同聲稱 duration 做容許範圍比較；缺 duration 時唔好 decode 成完整 PCM 塞爆 RAM。
- Transcode output 要 mono、bounded bitrate/size，讀入 memory 前先 stat size。
- ffmpeg/ffprobe missing、timeout、corrupt output 應視為 service unavailable 或 validation failure，唔好 fail open 畀 provider。
- 相片除 bytes 外要驗 dimensions、thumbnail scope、album/video pair 同 owner；metadata edit 唔可以改 storage identity。

## 14. AI call 要同費用／資源記錄一齊設計

AI provider abstraction 唔代表 call site 可以忽略 operation accounting。Coach、vote、training、TTS、Kiosk review 等 feature 各有 operation ID/stage 同不同 privacy flow。

- 先做 auth、schema readiness、input bound、budget/resource gate，再決定 provider；未真正送 request 唔可以記 `provider_attempted`。
- 同一 user action 多階段／retry 共用穩定 operation ID，stage 要可區分；避免重複扣費、phantom attempt 或將 transcript spend 當 judgement spend。
- Provider key missing 同本地 validation failure唔係 provider failure；HTTP 已送出但 timeout/5xx 先按既有 attempt semantics 記錄。
- Input/output、web-search results、source count、response bytes、timeout 同 concurrency semaphore 全部要 bounded。
- Provider error 對使用者要 sanitize；唔好回傳 headers、signed URL、key、raw SDK response 或敏感 prompt/data。
- Model label/provider mapping 只由 `ai_model_config.py` 取；模型價格／可用性會變，功能取捨同外部資料更新要先問使用者及重新核對官方來源。

## 15. AI training 同 Kiosk 錄音有嚴格私隱次序

- Consent 係 versioned data；撤回要阻止新收集，並按既有規則清 recordings／derived state。唔好將 consent 當單一 frontend checkbox。
- Owner、allowed user、reviewer/admin 係三個 scope；list、audio link、review、export 每條 endpoint 都要分開驗。
- Kiosk 全場錄音係短期 object。分析前後嘅 download、probe、raw R2 delete、兩輪 provider pass、結果保存同 cleanup 次序已有 tests；delete 未確認時必須取消 provider/result path。
- AI Coach 舊 base64 contract 已明確 gone；唔好為兼容舊 frontend 恢復。
- Export manifest 要檢查 R2 reachability/readability，presigned URL 唔可以進 provenance hash；dataset 要單 speaker、去 duplicate audio、stable split。
- RAG/eval/model endpoint 未 provision 時要喺任何 embedding/provider/cost 前 fail closed。

## 16. Live room 係 state machine，唔係普通 chat WebSocket

現行 multiplayer 只係兩人 STUN-only WebRTC P2P；Render 傳 signaling、roster、turn、timer、transcript 同 judgement control，唔傳 audio。

- 禁止重新加入 `peer_audio`、PCM、TTS audio、TURN/SFU 或 Render media fallback；Mode B 明確 400。
- Preflight 必須由 server 見到兩邊當前 roster generation 嘅 RTC connected；舊 socket／舊 transcript／caller 自報 ready 都唔算。
- Turn、stop、transcript commit、bell、RTC pause/restart 都有 generation/turn ID；stale event 唔可以改新 turn。
- Transcript finalization 要 idempotent、保 word boundary、在 forced stop/timeout 標 partial；唔可以靜默掉 active chunks。
- Socket replacement、send failure、leave/finally、simultaneous disconnect 有 race。唔好喺 lock 內 await 可能 block/fail 嘅 network send；沿用 serialized member send 同 monotonic state sequence。
- RTC drop 要 freeze server-authoritative time，只做一次 10 秒 ICE restart；兩邊恢復先 resume，失敗安全完場。
- Control message 即使 malformed/ignored 都要消耗 rate bucket；但 exhausted normal bucket 仍要畀必要 commit/safe turn-end 完成，避免資料丟失。
- Room end 唔會自動 call AI；只有 host 手動、一次、正反雙方各有 transcript 先可開始 judgement。
- 改任何 room code 前，先讀 `tests/test_multiplayer_adversarial.py` 全部相關 case；只測 happy path 幾乎一定漏 race。

## 17. Frontend 無 build step，但有 production cache

HTML/CSS/JS 原檔直接 serve。Shared asset 會被 cache，HTML 亦有 `stale-while-revalidate`。

- 改 shared JS 後，每個 HTML consumer 都要用 `?v=APP_VERSION` 或 server replace 嘅 `__APP_VERSION__`。漏一頁會造成「部分人已修、部分人仲舊版」。
- 唔好將 `__APP_VERSION__` 放入 server 無 replace 嘅 FileResponse；加 regression 確保 response 無 placeholder。
- `node --check` 只驗 standalone `.js`；HTML inline script 要抽取／browser smoke／contract test。
- Page route 改成 FileResponse 前，核對原本有冇 runtime injection、auth gate、cache header 或 CSP assumptions。
- Service worker 主要處理 push subscription/notification，唔係 offline app cache；唔好假設佢會幫 asset invalidation。
- 原生 `<select multiple>` 唔等於可直接點選嘅 multi-select：普通點擊會清走之前選項，桌面要靠 Ctrl／Cmd，touch 行為亦唔清楚。需要同時選多個帳戶時用獨立 checkbox、可見已選數量及需要時加搜尋，提交時讀 checked set；regression 要驗互動 contract，唔可以只驗有 `multiple` attribute。

## 18. Async UI 一定要防 stale response

用戶可以喺舊 request 未返之前轉 judge、match、album、room、retake context 或重按 save。慢 response 如果無 guard，會覆蓋新畫面或清走新 dirty state。

- Load/save request 綁 current generation/context key；response 返嚟要再比較先 render/mutate state。
- AbortController 只係優化，唔係唯一正確性保障；server 可能已處理，response 亦可能已進 event queue。
- Save 成功只可清對應 request 嘅 dirty snapshot；唔好清使用者其後新增嘅 edit。
- 對 destructive/one-shot action disable/re-enable 要按 matching request ID，唔好由舊 finally 打開新 action。
- 多 tab／socket replacement 情況要靠 server revision/ACK/conditional write，唔好只靠 browser flag。

## 19. DOM rendering 要假設所有文字不可信

會員名、隊名、辯題、comment、bug report、AI output、provider source、file name 同 database text 都可能包含 HTML。

- Plain text 用 `textContent`；需要 markdown 用現有 safe renderer同其 allowlist，唔好直接 `innerHTML = value`。
- 用 template string 組 DOM 時，escape function 必須覆蓋 attribute 同 text context；最安全係建立 element 再 set property。
- URL 要限制 scheme/origin/path。站內 notification URL 要 `/` 開頭、拒絕 `//` 同 backslash；外部 link 加 `noopener/noreferrer`。
- Hidden input、`data-*`、select option 都可以被 DevTools 改；server 仍要 allowlist/ownership check。

## 20. Pagination、export 同大 response 要喺 SQL 層有界

- List endpoint 用 count + `LIMIT/OFFSET`／既有 pagination helper；唔好先 load 全 table 再 Pandas/JS filter。
- Export 用 `EXPORT_MAX_ROWS + 1` 探測 overflow，再按 `EXPORT_MAX_BYTES` 截停；唔好無界 stringify 成 memory blob。
- Search/sort/filter parameter 要 bound length、allowlist、index-aware；避免 `%term%` 全表 scan 直接放上 public endpoint。
- Provider/R2 stream 要有 response/input byte cap；`Content-Length` 缺失唔等於無限制。
- 唔好將 binary、大 prompt、transcript 或完整 audit 塞入 normal JSON data endpoint。

## 21. Error handling 要 fail closed 同避免洩密

- Broad `except Exception` 只可以喺清理、provider boundary 或 best-effort notification 等有明確 fallback 嘅位置；至少 server log 可診斷 context，但唔 log secret／token／signed URL／raw audio。
- 唔好 `raise HTTPException(..., str(exc))` 將 SQL、SDK、filesystem、provider 或 secret detail 直接回傳 browser。將 known validation error 映射 4xx，其餘用穩定 5xx message。
- Cleanup failure 唔可以被 `pass` 吞咗之後仍回成功，尤其 privacy delete、transaction settle、reservation release、final score write。
- Fail-open 只可以係明確低風險 optional display；auth、schema readiness、privacy、resource gate、media validation、score integrity 必須 fail closed。
- Log 要帶 operation/request/record ID 同 stage，避免靠 PII 或完整 payload 搵問題。

## 22. Appliance 係 cloud client，唔好將佢變成另一套 backend

`appliance/` 假設 Supabase/Render 可用，負責 backup、health、Chromium kiosk 同 projector。

- 唔好加入 local PostgreSQL、雙向 sync、offline writes 或 Wi-Fi hotspot，除非使用者明確決定新 tier 同 conflict resolution。
- `pg_dump` 要 direct/session connection，transaction pooler 6543 唔適合 backup。
- Shell scripts 用 `set -euo pipefail`、安全 quote、atomic status file，同時兼顧 Ubuntu package／Chromium path 差異。
- systemd user、file permission、secret path、backup retention 同 restore rehearsal 係部署 contract；local macOS 成功唔代表 appliance 成功。

## 23. Dependencies 同 runtime 唔好靠本機偶然狀態

- Production Docker 係 Python 3.11 slim；本機 `venv` 可能係另一 minor version。避免只喺本機新語法／dependency transitive version work。
- `requirements.txt` 目前未完全 pin；新增或升級 library 前要問清楚相容性／lock 策略，並喺乾淨 Python 3.11 環境驗證。
- ffmpeg、字型等 OS dependency 由 `packages.txt`／Docker 安裝；Python import 成功唔代表 external binary 存在。
- 唔好提交 `venv/`、cache、temporary media、generated dataset、local secrets 或 IDE state。
- Tools 可能處理 10GB archive／production catalog；預設 dry-run、有界 scan、清晰 confirmation，同 runtime code 分離。

## 24. Tests 係事故 contract，唔好為通過而削弱

- 修 bug 要先寫能夠喺舊 code fail 嘅 regression；mock 應保留真正 failure ordering，而唔係 mock 走成個 invariant。
- 唔好刪 assertion、放寬 status、加入 arbitrary sleep 或將 race test skip，除非 production 證據證明 contract 本身改咗，而且使用者已決定新行為。
- 改 scoring、auth、resource、R2、Kiosk、Live、cache、migration 時，先睇對應 test file，因為好多防線係由歷史 bug 固化。
- Offline test 無 database、network、真 browser/media；通過唔代表 production smoke、真機 WebRTC、R2 lifecycle、migration replay 或 provider privacy 已驗證。
- 最後至少跑 compile、diff check、migration lint 同完整 pytest；JS/HTML 另跑相應 syntax/contract/browser gate。

## 25. 文件同 release snapshot 都可能過時

- `version.py` 先係 code release version；migration head 以實際 catalog/ledger 同 `migrations/` 為準，唔好信 README 入面寫死嘅舊 head。
- 分清 production 現況、repo ready 同未授權 deploy；「code 已有」唔等於 production 已部署。
- Model、價格、供應商政策、production table count、R2 bytes、Render plan 都係時間敏感資料。要引用時重新核對，並加核實日期。
- 完成功能後只更新真實受影響嘅 user manual／rules／service docs；唔好開散落嘅 migration diary 或複製一份 source of truth。
- 任何 release bump、tag、push、deploy、migration apply 同 production smoke 都要有明確授權，唔好因 code/tests ready 自動執行。

## 提交前快速檢查

- [ ] Bug 已有 production 根源證據；或者功能取捨已由使用者決定。
- [ ] 權限、owner、token、rate limit、resource、privacy 同 failure path 已逐項核對。
- [ ] 多 write 有 transaction／idempotency／concurrency protection；external call 無長佔 transaction。
- [ ] SQL values parameterized；dynamic identifier 來自 allowlist；list/export 有 bounds。
- [ ] Schema change 有新 up/down migration、bootstrap/readiness/permission/rollback parity。
- [ ] Binary 保持 R2-only；media 實際 probe；failure/orphan/delete lifecycle 完整。
- [ ] AI attempt、operation stage、費用、bandwidth/storage accounting 無重複或 phantom record。
- [ ] 香港時區 boundary、DataFrame null/JSON、frontend stale response/cache-buster 已核對。
- [ ] 相關 regression 同完整 release gates 已跑；未跑嘅真實環境驗證有清楚列明。
- [ ] 無 deploy、production write、secret/data mutation 或通知越權執行。
