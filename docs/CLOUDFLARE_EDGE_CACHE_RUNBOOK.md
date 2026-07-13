# Cloudflare custom domain及edge cache runbook

更新日期：2026-07-13

## 目的

以Cloudflare proxied custom domain代理Render，將圖示、CSS、JavaScript及其他
版本化靜態資源留在edge cache，減少一般頁面對Render outbound bandwidth的消耗。
Gemini Live WebSocket、API、HTML及私人R2 media不可套用同一cache rule。

## Repo已準備的origin headers

- `/shared/*`：`public, max-age=86400, stale-while-revalidate=604800`。部分檔名未帶
  版本，因此不設`immutable`。
- `/app-icon-*.png`、`/favicon.ico`、`/api/practice/bell`及帶版本query的專用JS：
  一年`immutable`。
- HTML：最多5分鐘及stale revalidation。
- `/sw.js`：`no-cache`，避免舊service worker長期留存。
- R2 presigned media：private，不經網站edge cache。

## Cloudflare設定

1. 在Render加入custom domain，取得Render要求的DNS target。
2. 在Cloudflare DNS建立相應CNAME並開啟Proxy（橙雲）。Cache Rules只會套用到
   proxied DNS record。
3. 在Caching → Cache Rules建立「public static only」rule，hostname只匹配正式
   custom domain，URI path只匹配以下公開路徑：

   ```text
   /shared/*
   /app-icon-*.png
   /favicon.ico
   /api/practice/bell
   /ai-training/app.js
   /dev-settings/lateness-managers.js
   ```

4. Cache eligibility設為Eligible for cache；Edge TTL使用origin `Cache-Control`。
   保留query string於cache key，令`?v=...`指向不同版本。
5. 不要建立全站「Cache Everything」。明確排除`/api/*`（上列bell例外）、
   `/gemini-live`、`/room/*`、HTML頁面、登入／cookie response及R2 presigned URL。
6. 部署後用瀏覽器或`curl -I`連續請求同一靜態URL，核對：

   - `Cache-Control`符合上列origin設定；
   - `CF-Cache-Status`由首次`MISS`轉為`HIT`；
   - API response保持`DYNAMIC`或`BYPASS`；
   - WebSocket練習、登入及service worker更新正常。

## 發版及回復

- 修改immutable或長TTL資源時同步更新query version或檔名；不要用同一URL覆蓋後
  期望edge即時更新。
- 緊急回復可先停用Cache Rule或purge指定URL，再回復origin版本。
- 每月在Cache Analytics核對static HIT ratio及Render outbound；如HIT率低，先查
  DNS是否仍為proxied及response有沒有`Set-Cookie`／private cache header。

參考：

- [Cloudflare Cache Rules](https://developers.cloudflare.com/cache/how-to/cache-rules/)
- [Cloudflare default cache behavior](https://developers.cloudflare.com/cache/concepts/default-cache-behavior/)
- [Cloudflare origin Cache-Control](https://developers.cloudflare.com/cache/concepts/cache-control/)
