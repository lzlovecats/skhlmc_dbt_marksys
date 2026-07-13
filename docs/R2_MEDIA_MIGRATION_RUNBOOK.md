# Cloudflare R2 媒體遷移 Runbook

更新日期：2026-07-13

## 目標

將 `match_photos.image_data` 及 `tts_voice_recordings.audio_data` 從 Supabase
PostgreSQL `BYTEA` 搬到 private Cloudflare R2。新上載會由瀏覽器直接傳送到
R2，Render 只簽發短期 URL 及儲存 metadata。

2026-07-13 migration 前只讀盤點：

| 類別 | 筆數 | BYTEA 總量 | 最大單檔 |
|---|---:|---:|---:|
| TTS 錄音 | 148 | 110,926,192 bytes（約105.8MiB） | 1,957,932 bytes |
| 相片 | 44 | 7,965,281 bytes（約7.6MiB） | 529,807 bytes |

TTS 錄音佔 media BYTEA 約93%，migration 工具會先搬錄音，再搬相片。

2026-07-13 第一轉完成狀態：

- TTS 錄音148／148段已有並已驗證 `r2_key`；148段Supabase BYTEA仍保留。
- 相片45／45張已有並已驗證原圖及縮圖R2 key；45張Supabase BYTEA仍保留。
- 相片遷移時比初次盤點新增一張，工具已一併處理；目前相片metadata總大小為
  10,890,918 bytes。
- 尚未執行第7節永久清除BYTEA；必須先部署及驗收`version.py`所列資源保護版本。

## 1. 建立 bucket API token

在 Cloudflare R2 建立只限目標 bucket 的 `Object Read & Write` token，記下：

- Account ID
- Access Key ID
- Secret Access Key
- Bucket name

不要把 Secret Access Key 放入前端、Git 或任何公開 URL。

## 2. 設定 bucket CORS

在 R2 bucket 設定：

```json
[
  {
    "AllowedOrigins": [
      "https://skhlmc-dbt-marksys.onrender.com"
    ],
    "AllowedMethods": ["GET", "HEAD", "PUT"],
    "AllowedHeaders": [
      "content-type",
      "content-length",
      "cache-control",
      "x-amz-meta-sha256"
    ],
    "ExposeHeaders": ["etag"],
    "MaxAgeSeconds": 3600
  }
]
```

如正式網址另有 custom domain，亦要加入 `AllowedOrigins`。參考
[Cloudflare R2 CORS](https://developers.cloudflare.com/r2/buckets/cors/)。

## 3. 設定 Render secrets

```text
R2_ACCOUNT_ID=<Cloudflare Account ID>
R2_ACCESS_KEY_ID=<bucket token Access Key ID>
R2_SECRET_ACCESS_KEY=<bucket token Secret Access Key>
R2_BUCKET=<bucket name>
R2_ENDPOINT=https://<account-id>.r2.cloudflarestorage.com
BANDWIDTH_MONTH_BASE_BYTES=<本月Render dashboard既有用量bytes>
BANDWIDTH_BASELINE_AS_OF=<baseline snapshot ISO timestamp>
BANDWIDTH_BASELINE_TRACKED_BYTES=<該刻app本月已tracked bytes>
```

所有固定limit（包括房間、錄音、AI並發、Render bandwidth、R2容量及Uvicorn）
均由repo根目錄[`system_limits.py`](../system_limits.py)集中管理，不應在呢份runbook
複製數值。Render同名environment variable只用作有記錄的臨時override；完整規則及
merge後操作見[system limits](SYSTEM_LIMITS.md)。上面三個bandwidth值係每月dashboard
同app tracked bytes的同時點runtime snapshot，並非固定limit，所以仍需在Render保存。

保持 bucket private。上載、播放及bulk下載各自的presigned URL有效期由
`system_limits.py`集中設定；詳見
[R2 Presigned URLs](https://developers.cloudflare.com/r2/api/s3/presigned-urls/)。

## 4. 部署目前資源保護版本

目前release不再提供base64／BYTEA上載、播放fallback或Render錄音ZIP。未設定R2時
媒體功能會明確暫停，避免靜默退回會消耗Supabase egress的舊路徑。
部署後檢查：

1. `/match-photos` 可以登入及載入舊相片。
2. `/ai-training` 可以播放舊錄音。
3. `/api/match-photos/data` 的 `storage` 為 `r2`。
4. 新相片及新錄音在Supabase只寫metadata及R2 key。
5. 錄音音質檢查先由browser直傳R2，Render每次只讀取一段最多2MB錄音。
6. 新上載先出現於`pending/`，完成驗證後才升格至`photos/`或`audio/tts/`。
7. `/api/match-photos/data`及`/api/ai-training/data`顯示R2用量；7GB警告，8GB
   拒絕新upload intent。

## 5. 第一轉：複製及驗證，不刪除 Supabase binary

在安全環境提供與 Render 相同的 R2 secrets 及 database secrets，執行：

```bash
./venv/bin/python tools/migrate_media_to_r2.py --media audio
./venv/bin/python tools/migrate_media_to_r2.py --media photos
```

工具逐筆執行：

1. 只讀取一個 BYTEA record。
2. 計算 SHA256。
3. 相片另建 480px WebP thumbnail。
4. 上載 R2。
5. 以 R2 `HEAD` 核對 bytes 及 SHA256 metadata。
6. 寫回 `r2_key`／`thumbnail_r2_key`。

此步不會清除原本 BYTEA，失敗可安全重跑。

## 6. 驗收

至少檢查：

- 五張不同格式／大小的相片縮圖及原圖下載。
- 五段不同使用者的 TTS 錄音播放。
- R2 object count、總大小及 object metadata。
- Render log 沒有 R2 403、CORS 或 signature mismatch。
- Supabase media rows 全部已有 `r2_key`。

SQL：

```sql
SELECT COUNT(*) FILTER (WHERE r2_key IS NULL) AS photos_not_migrated,
       COUNT(*) FILTER (WHERE r2_key IS NOT NULL) AS photos_migrated
FROM match_photos;

SELECT COUNT(*) FILTER (WHERE r2_key IS NULL) AS audio_not_migrated,
       COUNT(*) FILTER (WHERE r2_key IS NOT NULL) AS audio_migrated
FROM tts_voice_recordings;
```

## 7. 第二轉：以versioned migration永久移除Supabase BYTEA columns

只有完成browser驗收及可還原backup／restore演練後，先執行唯讀驗證：

```bash
./venv/bin/python tools/finalize_r2_media.py --json
```

第一條命令只對全部R2 object做HEAD，不下載媒體：original／audio會把size及SHA-256
metadata與database逐項比對，thumbnail亦必須有有效SHA-256 metadata；全部object同時核對
durable prefix、MIME及private cache-control。工具用keyset分批讀metadata，每批關閉
Supabase connection後才查R2，避免長時間佔用database connection或把媒體bytes經Render。
`--json`只輸出aggregate row／object／byte數及legacy columns狀態，不輸出object key、user id
或secret，可保存作dry-run摘要。

`finalize_r2_media.py`現時只做驗證；legacy `--apply`會在任何R2／database存取前
fail closed。不要恢復舊版直接drop行為，因為它會繞過migration ledger。
保存dry-run摘要及明確irreversible approval後，另建一個正式versioned migration移除
`match_photos.image_data`及`tts_voice_recordings.audio_data`，在staging驗證lock timeout、
forward及失敗回復，再於maintenance window套用。Down SQL不能重建已刪binary；真正rollback
係事前已演練的媒體backup restore。目前schema及程式均不再建立或引用這兩個columns。

PostgreSQL 已刪除的 TOAST 空間未必即時反映於 database size。日常 autovacuum
會重用空間；如要立即縮小實體檔案，需要另行評估 `VACUUM FULL` 的鎖表影響。

## 8. 清理R2孤兒檔

Direct upload在metadata完成前中斷時可能留下孤兒object。所有新PUT先落在
`pending/`；在R2 bucket建立只匹配`pending/`prefix、兩日後刪除的object lifecycle
rule。Lifecycle刪除可能稍有延遲，因此同時每日執行以下48小時dry-run：

```bash
./venv/bin/python tools/cleanup_r2_orphans.py --older-than-hours 48
```

核對清單後才執行：

```bash
./venv/bin/python tools/cleanup_r2_orphans.py --older-than-hours 48 \
  --apply --confirm DELETE-R2-ORPHANS
```

Cleaner亦會把相應`r2_upload_intents`標記為`orphan_deleted`，令8GB保守容量ledger
釋放已清除intent。Lifecycle設定及實際刪除時間應在R2 dashboard每月核對；不要把
rule套到`photos/`或`audio/tts/`正式prefix。

## 9. 故障處理

- R2暫時故障：相片／錄音功能會回傳503並停止新上載；一般系統繼續運作。
- 修復R2 credentials／CORS後功能自動恢復；系統不會fallback Supabase BYTEA。
- Presigned URL 403：核對 browser 發送的 `Content-Type`、`Cache-Control` 及
  `Content-Length`、`x-amz-meta-sha256` 是否與簽署時完全一致，並確認R2 CORS
  已容許上述headers。
