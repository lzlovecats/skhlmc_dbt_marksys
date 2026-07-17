# 系統服務、成本及用量限制

更新日期：2026-07-16

目前已核實的Render production為 **4.5.5**；repo release版本為 **4.6.0**，尚未
部署，程式碼版本唯一來源仍是[`version.py`](../version.py)。Production migration
head為`20260717_0003`；兩個legacy BYTEA columns及
legacy `system_config`已由versioned migrations退役。45張相片、45張縮圖及148段錄音共238個R2 objects
（122,687,464 bytes）已再次完成size、SHA、MIME及metadata驗證。

此文件記錄 production architecture、固定月費、免費額度及系統內的保護限制。
Provider 價格可隨時調整；付款前應以各 provider dashboard 及官方 pricing page
為準。

本文件同時係唯一營運參考。Limit數字只係摘要；repo內唯一可執行來源係
[`system_limits.py`](../system_limits.py)。修改數字時只改該file，避免文件、Render
及程式各自漂移。

## 每月固定成本摘要

| 服務 | 用途 | 目前方案 | 固定月費 | 主要限制 |
|---|---|---:|---:|---|
| Render | FastAPI、HTML、普通AI／TTS、Mode A control／signaling | Starter | US$7，約 HK$55 | 512MB RAM、0.5 CPU；app以3／3.5／4GB system-wide門檻保護 |
| Supabase | PostgreSQL database | Free | US$0 | 500MB database、500MB RAM、5GB egress、5GB cached egress、1GB Storage |
| Cloudflare R2 | Private 相片、縮圖、TTS 錄音及 AI評判易短暫錄音 | Standard Free Tier | 預期 US$0 | 10GB-month、1M Class A、10M Class B；Internet egress 免費 |
| Google Gemini API | AI Coach、審核、Gemini Live；未來RAG embedding | Free／paid usage | US$0 起，按使用量 | 模型 token、Search grounding、Live API rate limits；RAG schema未provision時會在provider call前停止 |
| OpenRouter | DeepSeek、Claude、GPT 等可選模型 | Prepaid／usage | 無固定月費 | 按模型 token 及搜尋用量扣 credits |
| Azure Speech | 可選廣東話 TTS | 按 Azure subscription | 未能由 repo 確定 | 按合成字元／subscription quota |
| YouTube | 比賽影片 embed | 外部服務 | 系統無固定月費 | 影片 bytes 直接由 YouTube 傳送，不經 Render |
| Web Push | 投票及系統通知 | Browser push endpoints + VAPID | 系統無固定月費 | 受 browser／push vendor 政策限制 |

目前可確認的固定基線約為 **US$7／月（約 HK$55）**，另加 Gemini、
OpenRouter、Azure 或其他 AI provider 的實際變動費用。

來源：

- [Render pricing](https://render.com/pricing/)：Starter US$7、512MB、0.5 CPU。
- [Supabase pricing](https://supabase.com/pricing)：Free 500MB database、500MB RAM、
  5GB egress、5GB cached egress、1GB Storage。
- [Cloudflare R2 pricing](https://developers.cloudflare.com/r2/pricing/)：Standard
  Free Tier 10GB-month、1M Class A、10M Class B，R2 Internet egress 免費。
- [Gemini Developer API pricing](https://ai.google.dev/gemini-api/docs/pricing)：
  免費及 paid tier、模型 token 及 Google Search 收費。
- [OpenRouter pricing](https://openrouter.ai/pricing)：實際價格按選用模型及 provider route。
- [Azure Speech pricing](https://azure.microsoft.com/pricing/details/cognitive-services/speech-services/)：
  Speech/TTS subscription pricing。

## 資料流

```text
一般 HTML／JSON
Browser ⇄ Render ⇄ Supabase PostgreSQL

相片／TTS錄音／AI Coach暫存錄音
Browser ⇄ Render：登入、metadata、短期 presigned URL
Browser ⇄ Cloudflare R2：binary PUT／GET
Render ⇄ Supabase：只傳 metadata

普通AI／搵料／Fact Check
Browser ⇄ Render ⇄ AI provider

AI Coach錄音分析
Browser → R2：32kbps Opus direct PUT
R2 → Render → Google Files API：bounded raw stream（沒有base64膨脹）
Render → Gemini：temporary file URI分析；finally刪Google file及R2 object

Solo Gemini Live
Browser ⇄ Render：登入、地區提示、prompt、一次性ephemeral token
Browser ⇄ Google Gemini Live：WebSocket audio及session（直連）

Mode A真人聯機
Browser ⇄ Browser：STUN-only WebRTC Opus audio
Browser ⇄ Render WebSocket：SDP／ICE、roster、turn、timer、逐字稿
Browser ⇄ Render HTTPS：完場結果及由主持手動觸發一次文字AI評判
```

Solo Gemini Live只有短小HTML、prompt及token response經Render，Live audio直接在
browser與Google之間傳送；長期Gemini／OpenRouter key永遠只留在server。Mode A只在Render
傳送低流量control／signaling／逐字稿，沒有TURN、SFU或Render audio fallback。Mode B已移除。
未deploy前production資料流仍以實際production版本為準；真機及cutover gate見
[`ROADMAP.md` P0.1](ROADMAP.md)。
網站custom domain及靜態edge cache屬optional；只可cache公開CSS、JS、manifest及圖示，
不可對HTML、API、登入、WebSocket或private R2 URL使用`Cache Everything`。

## 2026-07 bandwidth 事故摘要

事故發生時Supabase database 約137.4MB；其中 TTS 錄音 BYTEA 約110.9MB，相片約8.0MB。
7月9至10日約11GB uncached egress，與當時 AI Training 開發及舊管理頁整批讀取
`audio_data` 的時間吻合。約100次完整錄音 dataset 讀取已可產生11GB egress。
舊相片頁每次 rerun 讀取全部 `image_data` 亦會增加流量，但按實際 table size
只屬次要來源。2026-07-14兩個BYTEA columns已刪；PostgreSQL可能繼續保留可重用
TOAST空間，未經maintenance評估不為追求dashboard數字而跑`VACUUM FULL`。

## 已實施限制

系統不再設AI／media每日、每週或每月使用次數quota。以下只保留system-wide資源gate、
資料完整性及技術安全邊界；`monthly_resource_limits`係每月Render、R2、AI基金及provider
預算的DB authoritative source，process只在DB暫時失敗時使用last-known cache／安全預設。

### Mode A真人聯機

- 只接受兩位真人的Mode A；`mode=B`明確400。Free De及完整Mock保留。
- 音訊為Cloudflare public STUN-only WebRTC Opus mono P2P；不設TURN、SFU或Render fallback。
- Render只轉發authenticated SDP／ICE及低流量control；訊息受member、roster generation、
  byte、rate、room capacity、lobby 10分鐘TTL、開始後流程時限及兩房並行上限保護，ICE不寫log／database。
- 每部裝置由使用者按鍵做咪／播放、ICE、data-channel及remote packets／energy測試；
  兩位通過才開始聯機；測試後停傳，正式練習只在輪到本人並按開始發言後啟用音軌。
- Free De每方有完整設定時間，整體另有15分鐘安全寬限；由正方開始並在每次停咪後嚴格正反交替，
  一方用完時間先跳過；完整Mock以預定流程加15分鐘為硬上限。
- 每次發言用server簽發`turn_id`及ordered final chunks；可用的逐字稿會逐段保存。系統截停時
  逾時未收到browser final會保留server已收到內容並標示可能不完整。
- 房間完結後只可由主持手動要求一次AI評價；前端及server會先確認正反雙方各有至少一段
  逐字稿，未齊不呼叫provider，亦不消耗該次機會。評判呼叫不另設output token上限，
  由provider／模型預設控制長度，但回應仍受2MiB byte上限、45秒timeout及一次性要求保護。
- 完場逐字稿及結果只在process記憶體保留15分鐘；最多保留8個完場房，AI評判workflow同時最多2個。
- P2P中斷先暫停timer，只做一次10秒ICE restart；失敗安全完場，永不改經Render。

### AI Coach錄音分析

- Browser以32kbps Opus錄音，沒有6分鐘／2MB app上限；正式賽制計時器保留。
- 每次初錄及retake建立獨立user／operation-bound R2 intent，直接PUT並綁size、MIME、SHA256。
- Render在分析前重做owner、HEAD、SHA、MIME及ffprobe；原子預留3.5GB前剩餘bandwidth。
- R2原檔以bounded temporary-file stream送Google Files API，ACTIVE後用file URI分析，避免
  base64約33%膨脹；按實際raw uploaded bytes結算。
- 成功、provider error、timeout及取消均在`finally`刪Google file、R2 object及Render暫存檔。
- 唯一大小／時間邊界係Google 2GB／9.5小時，以及R2／Render system-wide剩餘容量。

### Solo Live、研究、Kiosk及AI Training

- Solo Free／Mock、賽前研究、Kiosk全場分析及LLM training沒有使用次數quota。
- Solo仍保留三秒重複mint防護、single in-flight token、operation idempotency、正式賽制／
  overall deadline、provider token／response／timeout及地區技術gate。
- Kiosk仍只接受專用kiosk account及正式場次，保留10秒至90分鐘、12MiB、media probe、
  paid-project確認、單一全場分析並行及兩小時加密結果TTL；這些是技術／私隱邊界。
- TTS training每段仍為1至60秒及2MiB，並保留consent、inventory、probe、single-use review
  claim及兩個並行；LLM training保留每筆20,000字、duplicate及全庫5,000筆inventory。

### R2 upload intents及相片

- 相片、TTS錄音及其他R2 PUT intent沒有每日／每月次數quota。
- Intent仍用於ownership、declared bytes、SHA、object lifecycle、single-use及孤兒清理；
  它不是quota counter。所有binary都browser直傳private R2，不經Render request body。
- 相片每批最多5張、原圖2MiB／2000px、thumbnail 300KiB／480px，屬request及media技術界限。
- R2當月DB門檻預設7GB warning、8GB stop／hard；48小時pending lifecycle及orphan cleaner
  保留。Durable相片／TTS prefixes不受pending lifecycle誤刪。

## 單人 Gemini Live bandwidth 評估

舊relay架構下，單人 Free De／Mock的browser及Gemini audio都經Render，理論流量為：

- Browser 16kHz PCM base64：約154MB／連續發言小時。
- Gemini 24kHz PCM base64：約230MB／連續回覆小時。
- 雙向理論上限約384MB／Live小時，未計 WebSocket／JSON overhead。
- 10分鐘 session 最壞約64MB；完整 Mock 可達約190–380MB，視長度及實際發言比例。

改為browser直連Google後，Solo audio不再進出Render；只剩登入、prompt、
HTML及ephemeral token等短小response。按上述audio量級與控制流量相比，預計每場可節省
約 **95–99% Solo Render bandwidth**。這不會減少Gemini provider用量；普通AI、server TTS、
AI Coach向Google Files上載及Mode A控制流量仍會消耗Render outbound，所以system-wide門檻保留。

## System-wide月度資源及AI基金預算

`monthly_resource_limits`以`period_month + limit_key`保存Render、R2、AI基金可用額及
`provider:{name}`分配；數值非負，Render必須`warning ≤ stop ≤ hard`。只保留62日內已完結
月份；current／future月份不會被retention刪除。PUBLIC、`anon`及`authenticated`沒有直接權限。

Render web process在設定`RENDER_API_KEY`及`RENDER_SERVICE_ID`後每小時讀官方
`/v1/metrics/bandwidth`作每小時total真值，並以`/v1/metrics/bandwidth-sources`補充
HTTP／WebSocket／NAT／PrivateLink分類；單位統一換算成bytes後idempotent寫入62日ledger。
分類只供audit，唔會同total重複相加。月度計算為
最新官方完整bucket累計，加該bucket後本地即時tracker；rollout首月官方retention不足時才加
manual baseline，完整月份後自動停止依賴baseline。Developer另有手動sync endpoint。

預設門檻：

- 3GB：一次性全體委員push及developer warning。
- 3.5GB：停止新AI Coach錄音分析傳輸及server TTS；Mode A P2P真人練習及文字AI可用。
- 4GB：停止一般AI、Mode A AI評判及該房Web Speech逐字稿；Mode A仍可作無逐字稿／AI評判真人練習。
- R2 7GB warning、8GB stop／hard；停止新PUT intent但不影響讀取。

AI基金每期香港時間上月25日00:00（包括）至本月25日00:00（不包括），只加總
`member_deposit`且`status='confirmed'`的`confirmed_at`，供下一budget month使用。管理員在
25號後手動設定月度HKD/USD匯率及Google／OpenRouter／Azure／其他分配；總額不得超過捐款。
Google外部cap最高為`allocated_hkd ÷ fx × 90%`，其他provider為100%。正數分配必須確認已在
provider後台更新cap。手動「結算並通知」以advisory lock及single-flight claim發送一次Web Push，
tag為`ai-fund-budget-YYYY-MM`；零成功delivery不標記完成，可安全重試。相同內容以負數
`-YYYYMM`通知ID成為登入公告，沒有push的active非kiosk委員下次登入仍會看到。

## Repo-wide RAM／storage保護

目前release不只限制Live及媒體；所有production FastAPI路徑均加入一致邊界：

| 範圍 | 預設保護 |
|---|---|
| HTTP | request body實際stream累計5MB；同時最多4個body buffer；1KB以上文字回應gzip |
| 管理員SQL | statement timeout 10秒；最多500行／1MB；binary cell不回傳；禁止maintenance DDL |
| CSV／JSONL | 最多5,000行及5MB；超額明確413，不會產生看似完整的截斷backup |
| LLM training | Active收集：每筆20,000字、duplicate protection、全庫最多5,000筆，export先在DB計算bytes；future snapshot registry未provision，計劃上限為每個500筆／共200個 |
| RAG | 目前fail-closed且embedding前先做cached schema gate；正式provision後才啟用每次10份文件／100 chunks、最多1,000 active文件、3個embedding並行及單一pgvector storage |
| AI／TTS | AI分析最多3個並行；TTS最多2個並行及4MB response；prompt／response token均有上限 |
| 資料庫inventory | 辯題2,000、場次500、每場評判50、影片2,000、AI training項目2,000、帳戶1,000 |
| 長期log | bandwidth 62日；login／notification read／AI usage及一般AI training audit 400日；consent grant/withdraw及AI基金交易永久保留；R2 intents完成／孤兒狀態90日 |
| Push／互動 | 每人最多5個active push devices；舊inactive subscription 90日後刪除；影片view每人每片24小時只記一次 |
| 其他輸入 | 抽籤最多128隊；投票理由每項500字；影片章節最多30段；評分JSON只保留schema需要欄位 |

技術安全邊界預設值由[`system_limits.py`](../system_limits.py)提供；Render、R2及provider
月度數值則以`monthly_resource_limits`當月DB row為準，DB暫時失敗才fallback到process cache／
安全預設。增加前要一併檢查Render 512MB RAM、account outbound
及Supabase 500MB database，而不是只提高單一endpoint限制。目前Render static回應仍是
`CF-Cache-Status: DYNAMIC`；日後有Cloudflare custom domain時可只cache公開CSS、JS、
manifest及圖示。HTML、API、登入、WebSocket及private R2 URL不可套`Cache Everything`；
R2 media及YouTube影片保持browser直連，不能恢復經Render proxy binary。

保留至少1GB予一般 API、deploy health check、external AI HTTP requests及突發流量。

## 設定、部署及Cloudflare操作

查看當前process會採用的limits：

```bash
./venv/bin/python system_limits.py --json
```

固定技術值由`system_limits.py`或短期有audit的environment設定；Render／R2月度門檻由
`monthly_resource_limits`管理。格式或threshold次序錯誤必須fail fast。以下dashboard
snapshot只供官方metrics rollout當月補回API retention未覆蓋的月初流量：

```text
BANDWIDTH_MONTH_BASE_BYTES
BANDWIDTH_BASELINE_AS_OF
BANDWIDTH_BASELINE_TRACKED_BYTES
```

完成一個完整官方月份後便停止依賴baseline；日常同步以`RENDER_API_KEY`及
`RENDER_SERVICE_ID`每小時讀取官方bandwidth buckets。

Production以`main`auto-deploy。先在`develop`完成review及驗證，再於低流量窗口合併／push
一次到`main`；部署後核對版本、health、登入、media、Live、RAM、5xx及WebSocket close。

R2 bucket必須保持private；browser CORS只加入正式origins及實際需要的`GET`、`HEAD`、
`PUT`／headers。Object Lifecycle rule只匹配`pending/`，2日後delete；不可匹配
`photos/`或`audio/tts/`。每日orphan檢查預設dry-run：

```bash
./venv/bin/python tools/cleanup_r2_orphans.py --older-than-hours 48
```

核對清單後先可加`--apply --confirm DELETE-R2-ORPHANS`。每月確認bucket、CORS、lifecycle、
Class A/B、object count、R2 backup可還原及最近一次orphan結果。Runtime R2 token只需目標
bucket Object Read & Write；不要為讀bucket admin設定而擴權。

## 每月檢查清單

1. Render bandwidth、最大及平均 RAM。
2. Supabase egress、cached egress、database size。
3. `match_photos.image_data`／`tts_voice_recordings.audio_data`保持不存在；media rows均有R2 metadata。
4. R2 storage、Class A／B operations、失敗 PUT／GET。
5. Gemini及OpenRouter實際帳單與 `ai_fund_usage_logs`。
6. 各類 Live session 次數、分鐘及估算 bytes。
7. 被Render／R2／provider system-wide gate或技術安全邊界拒絕的操作及原因。
8. R2 snapshot、`pending/`孤兒、7GB／8GB gate及lifecycle最近執行情況。
