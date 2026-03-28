# SKH LMC 辯電子評分系統 | SKH LMC Debate Marking System

> 聖呂中辯電子分紙系統

一個為校園辯論比賽而設計的全功能電子評分與管理平台，涵蓋辯題徵集投票、場次管理、評判電子分紙及成績統計。

A full-featured electronic scoring and management platform for school debate competitions, covering topic voting, match management, live judge scoring, and automated result aggregation.

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
- Telegram 推送通知（新辯題、罷免動議、投票結果、24 小時截止提醒、活躍度警告）

**English:**
- Committee members submit topics for collective vote
- Dynamic entry threshold: `max(5, ⌈active_members × 40%⌉)` votes with majority
- Dynamic deposition threshold: `max(6, ⌈active_members × 50%⌉)` votes with majority
- 7-day voting deadline per topic, auto-rejected on expiry
- Active member system: ≥ 40% overall participation rate AND ≥ 3 of last 10 votes
- AI topic review suggestions (Gemini / ChatGPT integration)
- Telegram push notifications (new topics, depositions, vote results, 24h deadline reminders, activity warnings)

---

### 📋 場次管理 | Match Management (`match_info.py`, `db_mgmt.py`)
**中文：**
- 建立及編輯比賽場次（日期、時間、辯題、隊伍、辯員）
- 從辯題庫隨機抽取辯題
- 隨機抽籤決定正反方站位
- 設定評判入場密碼（Access Code）
- 刪除場次（連帶刪除所有相關評分紀錄）

**English:**
- Create and edit debate matches (date, time, motion, teams, debaters)
- Draw random topics from the topic bank
- Randomly assign pro/con sides via draw
- Set judge access codes per match
- Delete matches (cascades to all related score records)

---

### ⚖️ 電子評判分紙 | Live Judging Interface (`judging.py`)
**中文：**
- 實時電子分紙，適合平板及手提電腦使用
- 雲端自動暫存（PostgreSQL `temp_scores`），防止頁面刷新導致資料遺失
- 細項評分：甲部（台上發言）× 4 辯員、乙部（自由辯論）、丙部（扣分及連貫性）
- 提交前確認對話框，防止誤操作

**English:**
- Real-time digital score sheets optimised for tablets and laptops
- Cloud auto-save to PostgreSQL `temp_scores`, preventing data loss on refresh
- Granular scoring: Part A (Speeches) × 4 debaters, Part B (Free Debate), Part C (Deductions & Coherence)
- Submission confirmation dialog to prevent accidental submissions

---

### 📊 賽果統計與查分 | Results & Score Review (`management.py`, `review.py`)
**中文：**
- 即時統計多位評判的投票及得分
- 自動計算最佳辯論員（依名次總和優先，次以平均分決定）
- 隊伍查閱分紙（按評判逐張查看完整評分詳情）

**English:**
- Real-time aggregation of votes and scores across multiple judges
- Automatic best debater calculation (rank-sum primary, average score secondary)
- Teams can review detailed per-judge score breakdowns

---

## 👥 用戶角色與權限 | User Roles & Access

| 角色 / Role | 頁面 / Pages | 認證方式 / Auth |
|---|---|---|
| 評判 / Judge | 電子分紙 | 賽會提供入場密碼 |
| 賽會人員 / Organiser | 場次管理、賽果統計、辯題庫管理 | 管理員密碼 |
| 比賽隊伍 / Teams | 查閱分紙 | 查卷密碼 |
| 一般人員 / Public | 查閱辯題庫 | 無需登入 |
| 委員會成員 / Committee | 辯題投票系統 | 個人帳戶 (ID + 密碼) |

---

## 🛠️ 技術架構 | Technology Stack

| 組件 / Component | 技術 / Technology |
|---|---|
| 前端框架 / Frontend | [Streamlit](https://streamlit.io/) |
| 數據庫 / Database | PostgreSQL (via `st.connection` + SQLAlchemy) |
| 數據處理 / Data | Pandas, NumPy |
| 身份管理 / Auth | Cookie-based sessions (`extra-streamlit-components`) |
| Telegram Bot / 通知 | Cloudflare Worker (TypeScript) + Hyperdrive |
| 部署 / Deployment | Streamlit Community Cloud + Cloudflare Workers |

---

## 🚀 快速開始 | Getting Started

### 環境要求 | Prerequisites
- Python 3.12+
- 一個運行中的 PostgreSQL 實例 / A running PostgreSQL instance

### 安裝步驟 | Installation

**1. 安裝依賴 / Install dependencies**
```bash
pip install -r requirements.txt
```

**2. 設定資料庫憑證 / Configure database credentials**

在專案根目錄建立 `.streamlit/secrets.toml` / Create `.streamlit/secrets.toml`:
```toml
[connections.postgresql]
dialect = "postgresql"
host = "your_host"
port = "5432"
database = "your_db"
username = "your_user"
password = "your_password"

[passwords]
admin = "your_admin_password"
score = "your_score_review_password"
```

**3. 啟動應用 / Run the app**
```bash
.streamlit run main.py
```

**4. （可選）部署 Telegram Bot / (Optional) Deploy Telegram Bot**

詳見 `worker/README.md`。需要 Cloudflare 帳戶、Hyperdrive 及 Telegram Bot Token。

See `worker/README.md` for full setup. Requires a Cloudflare account, Hyperdrive, and a Telegram Bot Token.

---

## 🗄️ 資料庫結構 | Database Schema

| 資料表 / Table | 內容 / Contents |
|---|---|
| `matches` | 場次資料（隊伍、辯題、密碼等）|
| `scores` | 正式提交的評判評分 |
| `temp_scores` | 評判評分暫存（JSON 格式）|
| `topics` | 辯題庫 |
| `topic_votes` | 待表決辯題投票紀錄 |
| `topic_vote_ballots` | 辯題投票選票 |
| `topic_depose_votes` | 辯題罷免投票紀錄 |
| `depose_vote_ballots` | 罷免投票選票 |
| `accounts` | 委員會成員帳戶（含 Telegram 連結欄位）|
| `login_record` | 成員登入紀錄 |
| `noti` | 站內通知 |
| `tg_notification_queue` | Telegram 推送通知佇列（由 Cloudflare Worker 處理）|

---

## 📁 檔案結構 | File Structure

```
├── main.py                   # 主入口 / Entry point, navigation
├── judging.py                # 電子分紙 / Judge scoring interface
├── match_info.py             # 場次管理 / Match management
├── management.py             # 賽果統計 / Results dashboard
├── review.py                 # 查閱分紙 / Score review
├── vote.py                   # 辯題投票系統 / Topic voting system
├── open_db.py                # 公開辯題庫 / Public topic viewer
├── db_mgmt.py                # 辯題庫管理 / Topic bank management (admin)
├── draw_match_schedule.py    # 抽籤賽程 / Draw schedule
├── functions.py              # 核心工具函數 / Core utilities
├── scoring.py                # 評分常數及欄位 / Scoring constants
├── schema.py                 # 資料庫建表語句 / DB schema definitions
├── assets/
│   ├── user_manual.md        # 使用手冊 / User manual
│   ├── rules.md              # 賽規 / Competition rules
│   └── *_reminder.md        # AI 審題建議 / AI topic review guides
└── worker/                   # Cloudflare Worker — Telegram bot
    ├── src/index.ts          # Bot logic (commands + scheduled jobs)
    ├── wrangler.jsonc        # Cloudflare deployment config
    └── README.md             # Worker setup & deployment guide
```

---

## 🔒 安全性 | Security

- 所有資料庫操作均使用參數化查詢，防範 SQL Injection
- All database operations use parameterized queries to prevent SQL injection
- 密碼以 session state 及 cookie 管理，不以明文傳輸
- Passwords managed via session state and cookies, not transmitted in plaintext

---

*Developed by lzlovecats @ 2026*
