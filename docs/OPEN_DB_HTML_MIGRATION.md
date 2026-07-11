# `open_db.py` HTML Migration Baseline

狀態：HTML 版已直接接管 `/open_db`；Streamlit 原版保留於 `legacy_streamlit/open_db.py`，但不再註冊路由。

## 不變原則

今次遷移只改 rendering 技術：Streamlit 改為 HTML/CSS/JS + JSON API。不得改 UI/UX、文案、資料範圍、排序、篩選語意、圖表內容或空狀態。`open_db.py` 係唯一產品基準；HTML 必須逐項對照現行頁面。

## 現行頁面契約

- 公開頁面，不需登入。
- 標題：`查閱辯題庫`。
- 資料查詢：
  - `topics`：`topic_text, author, category, difficulty`，cache TTL 60 秒。
  - `topic_votes`：`category, status`，cache TTL 60 秒。
- 搜尋：label `🔍 搜尋辯題`；placeholder `輸入關鍵字搜尋...`；topic 文字不分大小寫、純文字 substring（不可當 regex）。
- 三個並排篩選器，次序固定：
  - `👤 作者篩選`
  - `🏷️ 類別篩選`
  - `⭐ 難度篩選`
- 每個篩選器第一項均為 `全部`；其餘選項按現行 `sorted()` 排序。
- 難度顯示沿用 `DIFFICULTY_OPTIONS`：
  - `Lv1 — 概念日常`
  - `Lv2 — 一般議題`
  - `Lv3 — 進階專業`
- 結果 caption：`共找到 N 條符合條件的辯題`。
- 結果 table 欄位及次序固定：`辯題`、`作者`、`類別`、`難度`；不顯示 index。
- 無任何 topics：`辯題庫目前為空。`，其後內容不 render。
- 篩選結果為空：`沒有符合條件的辯題。請調整搜尋關鍵字或篩選條件後再試。`
- DB 錯誤：`連線錯誤: {error}`。

## 統計區契約

1. `📊 類別分佈 (所有辯題)`
   - bar chart：類別 → 辯題數量。
   - table：`類別`、`辯題數量`、`佔比`（一位小數百分比）。
2. `📈 難度分佈 (所有辯題)`
   - 只在 difficulty 欄存在時顯示。
   - null difficulty 顯示為 `未分類`。
   - bar chart：難度 → 辯題數量。
   - table：`難度`、`辯題數量`、`佔比`（一位小數百分比）。
3. `🗳️ 類別投票通過率`
   - 只計 `passed` + `rejected`，兩者都沒有則整區不顯示。
   - caption：`只計已完成表決的辯題動議（已通過 + 已否決）。`
   - null category 顯示為 `未分類`。
   - 排序：投票通過率 descending，再動議數量 descending。
   - bar chart：類別 → 通過率百分點。
   - table：`類別`、`動議數量`、`通過數`、`投票通過率`（一位小數百分比）。

## 實作切分

1. `core/open_db_logic.py`
   - 只負責兩個查詢、difficulty mapping、篩選和三組統計資料。
   - `db=None` executor 注入；禁止 import Streamlit。
   - Streamlit `open_db.py` 先改用同一 core，輸出必須不變。
2. `api/open_db_api.py`
   - 公開唯讀 `GET /api/open-db/data`；只回現行頁面需要的欄位。
   - server cache 60 秒，對齊現有 `ttl=60`。
   - 不接受任意 SQL、table 名或 column 名。
3. `frontend/open_db/index.html`
   - 原生 HTML/CSS/JS；dark theme 跟現行 Streamlit。
   - 首屏順序、label、table、divider、chart 全部按上述契約。
   - desktop 三欄篩選；窄屏只做必要 reflow，不改操作流程。
4. 雙軌
   - 保留現行 public route 及首頁入口語意。
   - HTML 路由接管前，先把 Streamlit 版移到 classic route；不得令首頁連結中斷。

## 實作結果（2026-07-11）

- shared core：`core/open_db_logic.py`；`open_db.py` 已改為呼叫同一套查詢、篩選及統計邏輯。
- public API：`GET /api/open-db/data`，server cache 60 秒。
- HTML：`frontend/open_db/index.html`，由 proxy 的 `/open_db` 直接提供；`/open-db` 保留為兼容 alias。
- 驗證：live 資料回傳 78 條辯題；類別篩選及「安樂死」literal search 均已驗證；`py_compile`、core import isolation、core parity check、JavaScript syntax 及 `git diff --check` 通過。

## 驗收

- 用同一份 DB snapshot 比較 Streamlit 與 HTML：全部 topics、每種單一篩選、組合篩選、搜尋、零結果。
- 三個統計 table 每個數值完全相同；chart 使用同一組資料及順序。
- desktop + mobile 無水平頁面 overflow，table 本身可橫向 scroll。
- `py_compile`、JavaScript syntax、core import isolation、API read smoke、瀏覽器視覺驗證全部通過。
- 不加入排序、分頁、卡片 view、匯出、URL query state 或其他現行 Streamlit 沒有的 UX。
