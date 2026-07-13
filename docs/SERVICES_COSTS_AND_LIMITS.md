# 系統服務、成本及用量限制

更新日期：2026-07-13

部署狀態：Render production 及此 workspace 均為 **4.1.2**。R2 第一轉已完成，
148 段錄音及 45 張相片均已驗證。production 驗收通過後，會以 finalizer 永久
移除兩個舊 BYTEA columns。

此文件記錄 production architecture、固定月費、免費額度及系統內的保護限制。
Provider 價格可隨時調整；付款前應以各 provider dashboard 及官方 pricing page
為準。

## 每月固定成本摘要

| 服務 | 用途 | 目前方案 | 固定月費 | 主要限制 |
|---|---|---:|---:|---|
| Render | FastAPI、HTML、WebSocket、Gemini relay | Starter | US$7，約 HK$55 | 512MB RAM、0.5 CPU；目前帳戶顯示 5GB outbound/月 |
| Supabase | PostgreSQL database | Free | US$0 | 500MB database、500MB RAM、5GB egress、5GB cached egress、1GB Storage |
| Cloudflare R2 | Private 相片、縮圖及 TTS 錄音 | Standard Free Tier | 預期 US$0 | 10GB-month、1M Class A、10M Class B；Internet egress 免費 |
| Google Gemini API | AI Coach、審核、RAG embedding、Gemini Live | Free／paid usage | US$0 起，按使用量 | 模型 token、Search grounding、Live API rate limits |
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

## 2026-07 bandwidth 事故摘要

Supabase database 約137.4MB；其中 TTS 錄音 BYTEA 約110.9MB，相片約8.0MB。
7月9至10日約11GB uncached egress，與當時 AI Training 開發及舊管理頁整批讀取
`audio_data` 的時間吻合。約100次完整錄音 dataset 讀取已可產生11GB egress。
舊相片頁每次 rerun 讀取全部 `image_data` 亦會增加流量，但按實際 table size
只屬次要來源。

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
- Uvicorn 同時連線／request 上限預設30。
- 單一Gemini relay最多轉發96MB，超額會中止連線。
- HTTP request body預設最多5MB；AI Coach臨時分析錄音最多2MB及60秒。

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

### 單人Gemini Live

- 單人Free De每人每日一次，全系統每月20次，每次最多10分鐘。
- 單人完整Mock每人每星期一次，全系統每月10次。
- 限額以香港時間計算；usage log以UTC儲存及查詢，香港午夜、星期及月份邊界
  均先轉換成UTC，Render重啟不會重設或造成八小時繞過窗口。

## 單人 Gemini Live bandwidth 評估

單人 Free De／Mock 同樣消耗大量 Render bandwidth，因 browser audio 及 Gemini
audio 均需經 Render relay：

- Browser 16kHz PCM base64：約154MB／連續發言小時。
- Gemini 24kHz PCM base64：約230MB／連續回覆小時。
- 雙向理論上限約384MB／Live小時，未計 WebSocket／JSON overhead。
- 10分鐘 session 最壞約64MB；完整 Mock 可達約190–380MB，視長度及實際發言比例。

Render 每月只有5GB outbound，扣除一般網站及安全預留後，不適合無限制使用
Gemini Live。

## 4.1.2已啟用用量上限

| 功能 | 每人限制 | 全系統限制 | 時間上限 |
|---|---:|---:|---:|
| 單人 Free De | 每日1次 | 每月20次 | 每次最多10分鐘 |
| 單人完整 Mock | 每週1次 | 每月10次 | 按一場正式賽制 |
| 聯機 Free De | 每日建立1房 | 每月20房 | 每房最多10分鐘 |
| 聯機完整 Mock | 每日建立1房 | 每月10房 | 每房一場正式賽制 |
| 錄音bulk download | 不經Render | R2免費egress範圍內 | 清單內URL一小時有效 |

系統亦會記錄每條單人 Gemini relay 及聯機房的實際轉發 bytes，並加上 Render
dashboard 的月初既有用量基線，執行全系統月度預算：

- 3.0GB：向全部委員發一次push notification，並在log及`system_config`寫入
  developer warning。
- 3.5GB：停止新 Gemini Live／聯機房間。
- 4.0GB：只保留一般 HTML、JSON、R2 media 及管理功能。

Render沒有向app提供即時billing meter API，因此每個billing month應把dashboard
當時已使用bytes填入`BANDWIDTH_MONTH_BASE_BYTES`。之後程式會把新記錄的高流量
session加上基線；如沒有填寫，保護器只會看見app部署後自行記錄的流量。

其他保護限制：HTTP request body 5MB、AI Coach分析錄音2MB／60秒、同時最多兩個
聯機房、單條Gemini relay 96MB、相片每次5張／每人每日20張／全系統每月500張、
錄音每段2MB／60秒、TTS音質檢查同時最多2個。

保留至少1GB予一般 API、deploy health check、external AI HTTP requests及突發流量。

## 每月檢查清單

1. Render bandwidth、最大及平均 RAM。
2. Supabase egress、cached egress、database size。
3. `match_photos`／`tts_voice_recordings` 是否仍有新 BYTEA。
4. R2 storage、Class A／B operations、失敗 PUT／GET。
5. Gemini及OpenRouter實際帳單與 `ai_fund_usage_logs`。
6. 各類 Live session 次數、分鐘及估算 bytes。
7. 被 daily／monthly limit 拒絕的用戶及次數。
