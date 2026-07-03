# SKH LMC 辯電子分紙系統 | SKH LMC Debate Marking System

> 聖呂中辯電子分紙系統

一個為校園辯論比賽而設計的全功能電子評分與管理平台，涵蓋辯題徵集投票、場次管理、隊伍自助提交名單、評判電子分紙、比賽片段重溫及成績統計。

A full-featured electronic scoring and management platform for school debate competitions, covering topic voting, match management, team roster self-submission, live judge scoring, match video replay, and automated result aggregation.

---

## 🌟 主要功能 | Key Features

### 🗳️ 辯題徵集與投票系統 | Topic Voting System (`vote.py`)
**中文：**
- 委員會成員可提出新辯題，由所有成員投票表決
- 動態入庫門檻：`max(5, ⌈活躍成員數 × 40%⌉)` 票，且同意多於不同意
- 動態罷免門檻：`max(6, ⌈活躍成員數 × 50%⌉)` 票，且同意多於不同意
- 每個投票設有 7 日截止期限，逾期自動否決
- 活躍成員制度：整體投票率 ≥ 40% 且最近10次至少參與3次
- AI 審題建議（Gemini / ChatGPT 整合）

**English:**
- Committee members submit topics for collective vote
- Dynamic entry threshold: `max(5, ⌈active_members × 40%⌉)` votes with majority
- Dynamic deposition threshold: `max(6, ⌈active_members × 50%⌉)` votes with majority
- 7-day voting deadline per topic, auto-rejected on expiry
- Active member system: ≥ 40% overall participation rate AND ≥ 3 of last 10 votes
- AI topic review suggestions (Gemini / ChatGPT integration)

---

### ✨ AI 辯論易 | AI Debate Coach (`ai_coach.py`)
**中文：**
- 內部委員會成員專用的 AI 辯論教練，可選擇 Gemini 或 OpenAI 模型
- **發言檢查**：輸入文字稿或粵語錄音（錄音分析需選 Gemini 模型），AI 根據正式評分標準（內容、辭鋒、組織、風度）提供詳細反饋及預估分數
- **主線策劃**：根據辯題及立場生成完整比賽策略（論點、反駁、自由辯論策略、辯員分工）
- 支援 Gemini 3.5 Flash、Gemini 2.5 Flash、Gemini 2.5 Pro、Gemini 3.1 Pro Preview、GPT-5.4 mini 及 GPT-5.4
- 會標示模型收費狀態，並提醒委員節約使用高級或收費模型
- 可從系統場次載入比賽資料，或手動輸入外部比賽辯題
- 策略建議可下載為 Markdown 文件

**English:**
- Committee-only AI debate coach with selectable Gemini or OpenAI models
- **Speech Review**: submit text or Cantonese audio recordings (audio review requires a Gemini model); AI provides detailed feedback and estimated scores based on the official scoring rubric (Content, Eloquence, Organisation, Manner)
- **Strategy Planning**: generates full match strategy (arguments, counter-arguments, free debate tactics, role assignments) from a given motion and side
- Supports Gemini 3.5 Flash, Gemini 2.5 Flash, Gemini 2.5 Pro, Gemini 3.1 Pro Preview, GPT-5.4 mini, and GPT-5.4
- Shows model cost status and reminds committee members to conserve premium or paid model usage
- Can load match data from the system or accept manually entered external match topics
- Strategy output downloadable as Markdown

---

### 🏠 主頁導航 | Home Page (`home.py`)
**中文：**
- 以身份分區（評判、賽會人員、參賽隊伍、一般人員、內部委員會成員）展示所有入口
- 每個區塊提供一鍵跳轉對應頁面的快捷連結
- 報名時間開放時，主頁標題下方會顯示下一屆比賽報名提示及報名連結

**English:**
- Identity-based card layout (Judge, Organiser, Teams, Public, Committee) with direct page links
- One-click navigation to all system functions from a single landing page
- When registration is open, a homepage banner appears below the title with the signup steps and registration link

---

### 📝 比賽報名 | Competition Registration (`registration.py`, `registration_admin.py`)
**中文：**
- 報名時間內開放公開報名頁，收集隊名、四位辯員姓名及聯絡人資料
- 同一屆比賽不可重覆提交相同隊名
- 賽會人員可設定比賽屆數、報名開始／截止時間、查看報名紀錄、更新狀態及匯出 CSV

**English:**
- Public signup page opens only during the configured registration window
- Captures team name, four debater names, and contact details
- Prevents duplicate team names within the same competition edition
- Organisers can set the edition/window, review submissions, update status, and export CSV

---

### 📋 場次管理 | Match Management (`match_info.py`)
**中文：**
- 建立及編輯比賽場次（日期、時間、辯題、隊伍、辯員）
- 為正反方產生隨機專屬名單提交連結，讓隊伍自行填寫隊名及辯員姓名
- 可查看各方提交狀態，並按需要重開填寫或重新生成連結
- 從辯題庫隨機抽取辯題
- 隨機抽籤決定正反方站位
- 設定評判入場密碼（Access Code）
- 刪除場次（連帶刪除所有相關評分紀錄）

**English:**
- Create and edit debate matches (date, time, motion, teams, debaters)
- Generate random per-side roster links so teams can submit their own team and debater names
- View each side's submission status, reopen a submitted link, or regenerate leaked links
- Draw random topics from the topic bank
- Randomly assign pro/con sides via draw
- Set judge access codes per match
- Delete matches (cascades to all related score records)

---

### 🎬 比賽片段重溫 | Match Video Replay (`video_replay.py`, `video_admin.py`)
**中文：**
- 賽會人員可為現有場次新增多條 YouTube 比賽片段連結
- 未使用電子分紙系統的舊比賽，可手動輸入比賽名稱、辯題及正反方隊名
- 支援 `https://youtube.com/watch?v=...`、`youtube.com/watch?v=...`、`www.youtube.com/watch?v=...` 及 `youtu.be/...` 格式
- 公開重溫頁無須登入，可按場次、隊伍、辯題或片段標題搜尋

**English:**
- Organisers can add multiple YouTube replay links for each existing match
- Legacy matches that were not scored in the system can be added with manually entered metadata
- Supports common YouTube URL formats, including links without an explicit `https://` prefix
- The public replay page is login-free and searchable by match, team, motion, or video title

---

### 🖥️ 數據庫管理控制台 | Database Management Console (`db_mgmt.py`)
**中文：**
- 賽會人員專用 SQL 控制台，直接查詢及操作生產數據庫
- SELECT / INSERT / UPDATE / DELETE 均支援，結果以表格呈現
- 安全保護：`system_config` 表不可在此修改；無 WHERE 條件的 UPDATE / DELETE 需二次確認

**English:**
- Organiser-only SQL console for direct production database access
- Supports SELECT, INSERT, UPDATE, DELETE; results displayed as tables
- Safety guards: `system_config` table is blocked from modification; UPDATE/DELETE without WHERE requires explicit re-confirmation

---

### ⚖️ 電子評判分紙 | Live Judging Interface (`judging.py`)
**中文：**
- 實時電子分紙，適合平板及手提電腦使用
- 雲端自動暫存（PostgreSQL `score_drafts`），防止頁面刷新導致資料遺失
- 細項評分：甲部（台上發言）× 4 辯員、乙部（自由辯論）、丙部（扣分及連貫性）
- 提交前確認對話框，防止誤操作

**English:**
- Real-time digital score sheets optimised for tablets and laptops
- Cloud auto-save to PostgreSQL `score_drafts`, preventing data loss on refresh
- Granular scoring: Part A (Speeches) × 4 debaters, Part B (Free Debate), Part C (Deductions & Coherence)
- Submission confirmation dialog to prevent accidental submissions

---

### 📊 賽果統計與查分 | Results & Score Review (`management.py`, `review.py`)
**中文：**
- 即時統計多位評判的投票及得分
- 評判提交後可選擇手動排名最佳辯論員（亦可自動根據發言分數填入或略過）
- 隊伍查閱分紙（按評判逐張查看完整評分詳情）
- 匯出指定評判的完整評分表 PDF（依 PDF template 填入資料）

**English:**
- Real-time aggregation of votes and scores across multiple judges
- Judges can optionally rank best debater after submission (auto-fill from scores or skip)
- Teams can review detailed per-judge score breakdowns
- Export a selected judge's complete score sheet using the PDF template

---

## 👥 用戶角色與權限 | User Roles & Access

| 角色 / Role | 頁面 / Pages | 認證方式 / Auth |
|---|---|---|
| 評判 / Judge | 電子分紙 | 賽會提供入場密碼 |
| 賽會人員 / Organiser | 報名管理、場次管理、比賽片段管理、賽果統計、數據庫控制台、抽取賽程 | 賽會人員密碼（存於 DB） |
| 準參賽隊伍 / Prospective Teams | 比賽報名 | 無需登入（只限報名時間內） |
| 參賽隊伍 / Teams | 提交比賽名單、查閱分紙 | 隨機專屬名單連結、查閱分紙密碼 |
| 一般人員 / Public | 比賽片段重溫、查閱辯題庫 | 無需登入 |
| 委員會成員 / Committee | 辯題徵集、投票及罷免、✨AI 辯論易 | 個人帳戶（用戶名稱 + 密碼） |
| Developer | 開發者設定 | 開發者密碼（存於 DB） |

---

## 🛠️ 技術架構 | Technology Stack

| 組件 / Component | 技術 / Technology |
|---|---|
| 前端框架 / Frontend | [Streamlit](https://streamlit.io/) |
| 數據庫 / Database | PostgreSQL (via `st.connection` + SQLAlchemy) |
| 數據處理 / Data | Pandas, NumPy |
| 文件輸出 / Document Export | ReportLab + pypdf PDF template overlay |
| AI 整合 / AI | Google Gemini (`google-genai`) + OpenAI GPT (`openai`) |
| 身份管理 / Auth | Cookie-based sessions (`extra-streamlit-components`) |
| 部署 / Deployment | Streamlit Community Cloud |

---

## 🚀 快速開始 | Getting Started

### 環境要求 | Prerequisites
- Python 3.12+
- 一個運行中的 PostgreSQL 實例 / A running PostgreSQL instance

### 安裝步驟 | Installation

**1. 安裝 Python 依賴 / Install Python dependencies**
```bash
pip install -r requirements.txt
```

如部署至 Streamlit Community Cloud，`packages.txt` 只會安裝 CJK 字型，不再安裝 LibreOffice。

On Streamlit Community Cloud, `packages.txt` only installs CJK fonts and no longer installs LibreOffice.

**2. 設定資料庫憑證 / Configure database credentials**

在專案根目錄建立 `.streamlit/secrets.toml` / Create `.streamlit/secrets.toml`:
```toml
GEMINI_API_KEY = "your_gemini_api_key"
OPENAI_API_KEY = "your_openai_api_key"

[connections.postgresql]
dialect = "postgresql"
host = "your_host"
port = "5432"
database = "your_db"
username = "your_user"
password = "your_password"
```

`GEMINI_API_KEY` 用於 Gemini 模型；`OPENAI_API_KEY` 用於 GPT-5.4 mini / GPT-5.4。

**3. 初始化系統密碼 / Seed initial passwords**

首次部署時，需直接在資料庫插入初始密碼（可以明文，登入後再改為加密版本）：

On first deploy, seed initial passwords directly in the database (plaintext is accepted initially; change them via 開發者設定 after first login):

```sql
INSERT INTO system_config (key, value, updated_at) VALUES
  ('admin_password',      '<賽會人員密碼>', NOW()::TEXT),
  ('developer_password',  '<開發者密碼>',   NOW()::TEXT);
```

**4. 啟動應用 / Run the app**
```bash
streamlit run main.py
```

## 🗄️ 資料庫結構 | Database Structure

| 資料表 / Table | 內容 / Contents |
|---|---|
| `matches` | 場次資料（隊伍、辯題、密碼等）|
| `match_videos` | 比賽片段連結（可連結現有場次，亦可記錄舊比賽手動資料）|
| `match_roster_links` | 正反方自助提交名單的隨機 token、提交狀態及建立時間 |
| `scores` | 正式提交的評判評分 |
| `score_drafts` | 評判評分暫存（JSON 格式）|
| `best_debater_rankings` | 評判手動提交的最佳辯論員排名 |
| `topics` | 辯題庫 |
| `topic_votes` | 待表決辯題投票紀錄 |
| `topic_vote_ballots` | 辯題投票選票 |
| `topic_removal_votes` | 辯題罷免投票紀錄 |
| `topic_removal_vote_ballots` | 罷免投票選票 |
| `accounts` | 委員會成員帳戶 |
| `login_records` | 成員登入紀錄 |
| `notification_reads` | 站內通知已讀紀錄 |
| `competition_registration_settings` | 下一屆比賽報名設定（屆數、開始及截止時間）|
| `competition_registrations` | 比賽報名紀錄（隊伍、辯員、聯絡人及狀態）|
| `system_config` | 系統設定（賽會人員密碼、開發者密碼等，以 bcrypt 加密存放）|

---

## 📁 檔案結構 | File Structure

```
├── main.py                   # 主入口 / Entry point, navigation
├── home.py                   # 主頁 / Landing page with role-based navigation
├── judging.py                # 電子分紙 / Judge scoring interface
├── match_info.py             # 場次管理 / Match management
├── team_roster.py            # 隱藏隊伍名單提交頁 / Hidden team roster submission page
├── video_admin.py            # 比賽片段管理 / Match video management
├── video_replay.py           # 比賽片段重溫 / Public match video replay
├── management.py             # 賽果統計 / Results dashboard
├── registration.py           # 公開比賽報名 / Public competition registration
├── registration_admin.py     # 比賽報名管理 / Registration management
├── review.py                 # 查閱分紙 / Score review
├── vote.py                   # 辯題投票系統 / Topic voting system
├── open_db.py                # 公開辯題庫 / Public topic viewer
├── db_mgmt.py                # 數據庫管理控制台 / SQL console (admin)
├── dev_settings.py           # 開發者設定 / Developer settings (password management)
├── draw_match_schedule.py    # 抽籤賽程 / Draw schedule
├── ai_coach.py               # ✨AI 辯論易 / AI debate coach page
├── ai_coach_helpers.py       # AI API 調用及 prompt 組裝 / AI API helpers
├── functions.py              # 核心工具函數 / Core utilities
├── scoring.py                # 評分常數及欄位 / Scoring constants
├── score_sheet_pdf.py        # PDF template 填寫及匯出 / PDF template export
├── schema.py                 # 資料庫建表語句 + Python DB table constants / DB schema + Python DB identifiers
├── packages.txt              # CJK 字型 / CJK fonts
└── assets/
    ├── user_manual.md        # 使用手冊 / User manual
    ├── rules.md              # 賽規 / Competition rules
    └── *_reminder.md         # AI 審題建議 / AI topic review guides
```

---

## 🔒 安全性 | Security

- 所有資料庫操作均使用參數化查詢，防範 SQL Injection
- All database operations use parameterized queries to prevent SQL injection
- 密碼以 session state 及 cookie 管理，不以明文傳輸
- Session state and cookies handle credentials, avoiding plaintext transmission
- 賽會人員密碼及開發者密碼以 bcrypt 加密存放於資料庫 `system_config` 表，不寫入設定檔
- Organiser and developer passwords are bcrypt-hashed and stored in the `system_config` DB table, not in config files
- 隊伍名單提交頁不會顯示於公開導航，只能透過資料庫儲存的隨機 token 連結進入；重新生成連結後舊 token 會失效
- Team roster submission pages are hidden from public navigation and require random DB-backed token links; regenerated links invalidate old tokens
- 數據庫管理控制台設有保護：禁止修改 `system_config` 表，無 WHERE 條件的危險操作須二次確認
- The SQL console blocks modifications to `system_config` and requires explicit re-confirmation for UPDATE/DELETE without WHERE

---

*Developed & Maintained by lzlovecats @ 2026*
