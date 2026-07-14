# 系統服務、成本及用量限制

更新日期：2026-07-14

目前基線：Render production 為 **4.2.1**，實際版本仍以[`version.py`](../version.py)
為準。Production migration head為`20260714_0001`；兩個legacy BYTEA columns已由
versioned migration永久移除。45張相片、45張縮圖及148段錄音共238個R2 objects
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
| Render | FastAPI、HTML、WebSocket、Gemini relay | Starter | US$7，約 HK$55 | 512MB RAM、0.5 CPU；目前帳戶顯示 5GB outbound/月 |
| Supabase | PostgreSQL database | Free | US$0 | 500MB database、500MB RAM、5GB egress、5GB cached egress、1GB Storage |
| Cloudflare R2 | Private 相片、縮圖及 TTS 錄音 | Standard Free Tier | 預期 US$0 | 10GB-month、1M Class A、10M Class B；Internet egress 免費 |
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

相片／錄音（R2 migration 後）
Browser ⇄ Render：登入、metadata、短期 presigned URL
Browser ⇄ Cloudflare R2：binary PUT／GET
Render ⇄ Supabase：只傳 metadata

Gemini Live
Browser ⇄ Render WebSocket ⇄ Google Gemini Live
```

Cloudflare R2 不會降低 Gemini Live bandwidth；Gemini audio 仍然經 Render relay。
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

### 聯機練習

- 每位委員每日一次聯機 Free De。
- 每位委員每日一次聯機完整 Mock。
- Free De 及 Mock 分開計算。
- 以香港日期計算。
- 重連同一房間不會重複扣次數。
- 新房間預設最多同時存在兩個（`MAX_ROOMS=2`）。
- 全系統每月最多20個聯機Free De房及10個聯機Mock房。
- 聯機Free De時間由server強制限制為最多10分鐘，不能只靠前端欄位繞過。
- Gemini upstream WebSocket 單一 message 上限4MB；browser WebSocket message 上限2MB。
- Server-side TTS native fallback 每個 turn 最多保留8MB。
- Uvicorn 同時連線／request 上限預設20；壓力測試後才考慮調高。
- 單一Gemini relay最多轉發96MB，超額會中止連線。
- HTTP request body預設最多5MB；middleware會逐個ASGI chunk累計實際bytes，
  `Content-Length`只作早期拒絕，chunked request不能繞過。AI Coach臨時分析錄音
  最多2MB及60秒。
- AI Coach文字／錄音分析最多同時3個request；TTS音質檢查維持最多同時2個。

超額訊息：

> 由於系統每月可用的網絡傳輸量有限，為控制營運預算並確保所有委員均能使用服務，每位委員每日只可進行一次聯機自由辯論及一次聯機完整模擬練習。你今日已使用此類別的練習限額，請於翌日再試。

### 相片

- 每次最多五張。
- Browser 上載前縮至最長邊 2000px。
- 原圖壓縮後最多2MB。
- 另建最長邊480px、最多300KB thumbnail。
- 每位委員每日最多20張，全系統每月最多500張。
- 新檔案直接上載 private R2，不經 Render request body。
- Gallery 每頁20筆，圖片使用 lazy loading。

### TTS 錄音

- 每段1至60秒。
- 每段預設最多2MB（可用 `MAX_AUDIO_BYTES`調整）。
- Browser先直接上載private R2，音質檢查request不再包含base64錄音。
- 音質檢查最多同時兩個，每次只由R2讀取一段已驗證錄音。
- Presigned PUT簽署時綁定確實`Content-Length`；完成上載後再核對R2大小及SHA256。
- 所有R2 PUT intent均寫入`r2_upload_intents`：相片每人每日最多20個、全系統
  每月最多500個；錄音每人每日最多30個、全系統每月最多1,000個。即使申請者
  上載後不完成登記，亦會計入intent限額，避免孤兒檔洪水。
- 錄音技術metadata由server重新下載並probe；AI預檢結果使用短期HMAC token綁定
  使用者、句子、R2 key及SHA256，browser不能自行修改後入庫。
- Render錄音ZIP endpoint已移除，改為metadata及一小時R2直連下載清單。
- Dataset snapshot只使用R2 key及SHA256，不讀取任何BYTEA。
- PostgreSQL connection pool 預設3個、overflow最多2個。
- 新檔案先上載至`pending/photos/...`或`pending/audio/tts/...`，server驗證後用R2
  內部copy升格到正式key；中斷上載由48小時lifecycle及orphan cleaner清理。
- R2總量7GB開始警告、8GB停止簽發新PUT，保留至少2GB free-tier緩衝。

### 單人Gemini Live

- 單人Free De每人每日一次，全系統每月20次，每次最多10分鐘。
- 單人完整Mock每人每星期一次，全系統每月10次。
- 限額以香港時間計算；usage log以UTC儲存及查詢，香港午夜、星期及月份邊界
  均先轉換成UTC，Render重啟不會重設或造成八小時繞過窗口。
- Relay簽名綁定`user_id`、練習類型、`practice_id`及`max_seconds`；Google upstream
  WebSocket成功建立後才原子扣限額，未使用token自然過期而不會產生usage row。
- Server在`max_seconds`主動關閉browser及Google WebSocket，不能只靠client timer。
- `prepare-live`每人每小時最多1次、每日最多3次，失敗嘗試同樣計算，防止重覆
  賽前研究燒AI tokens。

## 單人 Gemini Live bandwidth 評估

單人 Free De／Mock 同樣消耗大量 Render bandwidth，因 browser audio 及 Gemini
audio 均需經 Render relay：

- Browser 16kHz PCM base64：約154MB／連續發言小時。
- Gemini 24kHz PCM base64：約230MB／連續回覆小時。
- 雙向理論上限約384MB／Live小時，未計 WebSocket／JSON overhead。
- 10分鐘 session 最壞約64MB；完整 Mock 可達約190–380MB，視長度及實際發言比例。

Render 每月只有5GB outbound，扣除一般網站及安全預留後，不適合無限制使用
Gemini Live。

## 目前production用量上限

| 功能 | 每人限制 | 全系統限制 | 時間上限 |
|---|---:|---:|---:|
| 單人 Free De | 每日1次 | 每月20次 | 每次最多10分鐘 |
| 單人完整 Mock | 每週1次 | 每月10次 | 按一場正式賽制 |
| 聯機 Free De | 每日建立1房 | 每月20房 | 每房最多10分鐘 |
| 聯機完整 Mock | 每日建立1房 | 每月10房 | 每房一場正式賽制 |
| 錄音bulk download | 不經Render | R2免費egress範圍內 | 清單內URL一小時有效 |

系統亦會每30秒checkpoint單人 Gemini relay 及聯機房的實際轉發 bytes，結束時只
補寫餘額；Render crash最多遺失最後一個checkpoint interval。同一練習／房間的
checkpoint會在當月同一行累加，不會每30秒新增一行；log預設只保留62日。記錄會
加上Render dashboard既有用量基線，執行全系統月度預算：

- 3.0GB：向全部委員發一次push notification，並在log及typed `app_config`寫入
  developer warning。
- 3.5GB：停止新 Gemini Live／聯機房間；已開始的Live會在下一個30秒checkpoint
  由server強制結束，避免單一長session繼續穿透hard gate。
- 4.0GB：只保留一般 HTML、JSON、R2 media 及管理功能。

Render沒有向app提供即時billing meter API。更新dashboard baseline時必須同時
保存以下三個同一時刻snapshot，避免重覆計算更新前已tracked的bytes：

```text
BANDWIDTH_MONTH_BASE_BYTES=<dashboard當時累計bytes>
BANDWIDTH_BASELINE_AS_OF=<ISO timestamp，例如2026-07-13T12:00:00+08:00>
BANDWIDTH_BASELINE_TRACKED_BYTES=<該刻bandwidth_usage_logs本月累計bytes>
```

計算為`baseline + max(0, tracked_now - tracked_snapshot)`。缺少snapshot時會沿用
保守舊算法，可能高估但不會低估；`baseline_as_of`不是當月時會忽略舊baseline。

AI Coach／TTS送往provider的錄音bytes、Render回傳的TTS audio，以及所有CSV／JSONL export bytes亦會寫入同一
bandwidth tracker。其他保護限制：HTTP request body 5MB、AI Coach分析錄音2MB／60秒、同時最多三個
AI Coach request、同時最多兩個
聯機房、單條Gemini relay 96MB、相片每次5張／每人每日20張／全系統每月500張、
錄音每段2MB／60秒、TTS音質檢查同時最多2個。

## Repo-wide RAM／storage保護

目前release不只限制Live及媒體；所有production FastAPI路徑均加入一致邊界：

| 範圍 | 預設保護 |
|---|---|
| HTTP | request body實際stream累計5MB；同時最多4個body buffer；1KB以上文字回應gzip |
| 管理員SQL | statement timeout 10秒；最多500行／1MB；binary cell不回傳；禁止maintenance DDL |
| CSV／JSONL | 最多5,000行及5MB；超額明確413，不會產生看似完整的截斷backup |
| LLM training | Active收集：每筆20,000字、每人每日10筆、全庫最多5,000筆，export先在DB計算bytes；future snapshot registry未provision，計劃上限為每個500筆／共200個 |
| RAG | 目前fail-closed且embedding前先做cached schema gate；正式provision後才啟用每次10份文件／100 chunks、最多1,000 active文件、3個embedding並行及單一pgvector storage |
| AI／TTS | AI分析最多3個並行；TTS最多2個並行及4MB response；prompt／response token均有上限 |
| 資料庫inventory | 辯題2,000、場次500、每場評判50、影片2,000、AI training項目2,000、帳戶1,000 |
| 長期log | bandwidth 62日；login／notification read／AI usage及一般AI training audit 400日；consent grant/withdraw及AI基金交易永久保留；R2 intents完成／孤兒狀態90日 |
| Push／互動 | 每人最多5個active push devices；舊inactive subscription 90日後刪除；影片view每人每片24小時只記一次 |
| 其他輸入 | 抽籤最多128隊；投票理由每項500字；影片章節最多30段；評分JSON只保留schema需要欄位 |

以上預設值全部由[`system_limits.py`](../system_limits.py)提供；同名environment
variable只係有記錄的部署override。增加前要一併檢查Render 512MB RAM、5GB outbound
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

Render environment同名值會override repo default；低於安全minimum會被提升，格式或
threshold次序錯誤會令worker fail fast。固定值應盡量留在`system_limits.py`，只有有記錄
的短期調整先用environment。Bandwidth dashboard snapshot三個值則必須留在Render：

```text
BANDWIDTH_MONTH_BASE_BYTES
BANDWIDTH_BASELINE_AS_OF
BANDWIDTH_BASELINE_TRACKED_BYTES
```

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
7. 被 daily／monthly limit 拒絕的用戶及次數。
8. R2 snapshot、`pending/`孤兒、7GB／8GB gate及lifecycle最近執行情況。
