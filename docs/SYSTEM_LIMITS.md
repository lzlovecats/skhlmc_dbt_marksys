# 系統 Limits 集中管理

所有會影響 Render RAM／bandwidth、Supabase database growth、Cloudflare R2容量、
AI provider用量、並發、quota及retention的設定，以repo根目錄
[`system_limits.py`](../system_limits.py)作唯一程式碼來源。

API欄位格式驗證（例如電話號碼、密碼及日期字串長度）仍留在相應Pydantic model；
呢啲屬於API contract，唔係營運資源limit。

## 優先次序

每項limit在`system_limits.py`包含預設值、最低值、分類及說明。啟動時：

1. 如Render有同名environment variable，使用environment value。
2. 否則使用`system_limits.py`內的default。
3. 數值低於安全minimum會被提升至minimum；錯誤格式或threshold次序錯誤會令worker
   fail fast，避免以不安全設定啟動。

因此，要以repo數值為準，Render不應保留過時的同名limit overrides；如確實需要臨時
調整，environment variable仍可使用，但必須同步記錄及在下次部署前核對。

## 查看實際生效值

```bash
./venv/bin/python system_limits.py --json
```

Render container啟動時，`deploy/start.sh`亦會由同一file讀取Uvicorn concurrency、
WebSocket frame及malloc limits。登入開發者設定後，可讀取：

```text
GET /api/dev-settings/system-limits
```

回傳的是該worker啟動時已resolve的值，不包含任何secret。

## Merge到main後令設定生效

`deploy/render.yaml`目前是`autoDeploy: false`，所以merge本身不會更新production：

1. 確認main branch CI／全套tests通過。
2. 到Render為`skhlmc-dbt-marksys`執行 **Manual Deploy → Deploy latest commit**。
3. 在Render Environment檢查舊limit overrides：刪除佢哋以採用repo defaults，或者改成
   與`system_limits.py`一致。儲存environment變更會觸發restart。
4. 同一時間更新當月bandwidth snapshot三個runtime state值：
   `BANDWIDTH_MONTH_BASE_BYTES`、`BANDWIDTH_BASELINE_AS_OF`、
   `BANDWIDTH_BASELINE_TRACKED_BYTES`。佢哋係當月meter snapshot，唔係固定limit，
   所以唔會寫死在repo。
5. 部署成功後登入開發者設定，讀取`/api/dev-settings/system-limits`，核對3.0／3.5／
   4.0GB bandwidth gate、7／8GB R2 gate、Uvicorn 20及其他關鍵值。
6. 測試一個一般API、相片direct R2 upload、TTS錄音、單人Free De及聯機房；查看Render
   logs確認無startup limit error。
7. Cloudflare dashboard的外部設定不會由git deploy自動建立：按edge-cache runbook設定
   靜態資源Cache Rule，並在R2 bucket設定只針對`pending/`、48小時後刪除的lifecycle。
8. 觀察Render RAM／bandwidth及R2用量至少一個實際練習週期，之後先執行R2 finalizer
   dry-run；確認production播放正常後才永久drop舊BYTEA columns。

修改任何limit後必須restart/redeploy，因為所有值在Python process import時只讀一次。
