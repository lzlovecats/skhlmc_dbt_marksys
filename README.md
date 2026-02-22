# SKH LMC Debate Marking System (電子評分系統)

A robust, real-time debate marking and management system built with Streamlit and PostgreSQL. Designed to streamline the entire debate competition lifecycle, from soliciting and voting on topics to live judge scoring and automated result calculation.

## 🌟 Key Features

### 1. 🗳️ Topic Voting System (`vote.py`)
- Allows committee members to submit new debate topics.
- Built-in voting mechanism (Agree/Disagree) with automatic thresholds.
- Topics hitting the +5 agree & majority thresholds are automatically promoted to the central Topic Bank.
- Defensive list parsing prevents data corruption and ensures atomic voting accuracy.

### 2. 📝 Match Management (`match_info.py` & `db_mgmt.py`)
- **Match Setup:** Create detailed debate matches with assigned motions, pro/con teams, and custom passwords.
- **Topic Engine:** Draw random topics natively from the database bank.
- **Side Drawing:** Randomly assign Pro/Con sides to teams.
- **Topics Management:** Direct admin access to upload, view, or delete topics from the bank.

### 3. ⚖️ Live Judging Interface (`judging.py`)
- Real-time digital score sheet optimized for tablets and laptops.
- **Cloud Auto-Save:** Automatically drafts scores to the PostgreSQL `temp_scores` table as judges type, preventing data loss on disconnects.
- Detailed granular scoring (Part A: Speeches, Part B: Free Debate, Deductions, and Coherence markings).
- Safe, parameterized score parsing directly into database repositories.

### 4. 📊 Results & Review (`management.py` & `review.py`)
- **Dashboard:** Automatically aggregates scores across multiple judges, calculates averages, applies penalty deductions, and outputs final victorious teams.
- **Score Review:** Provides an interface to retrieve and display granular score breakdowns for transparency and post-match reviews.

## 🛠️ Technology Stack
- **Frontend & Framework:** [Streamlit](https://streamlit.io/)
- **Data Manipulation:** [Pandas](https://pandas.pydata.org/)
- **Database Engine:** PostgreSQL via `st.connection("postgresql")` + SQLAlchemy
- **Security:** Full SQL Injection hardening via Parameterized Execution (`functions.py`).

## 🚀 Getting Started

### Prerequisites
- Python 3.12+ 
- A running PostgreSQL instance.

### Installation

1. Clone the repository and install dependencies:
   ```bash
   pip install -r requirements.txt
   ```

2. Configure Database Credentials:
   Create a `.streamlit/secrets.toml` file in the project root:
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
   ```

3. Run the application:
   ```bash
   streamlit run main.py
   ```

## 🔒 System Security
All database interactions are routed through heavily sanitized, parameterized `execute_query` blocks wrapping core SQLAlchemy transactions. The system naturally accommodates dynamic lengths, PostgreSQL array typings (`text[]`), JSONB blob casting, and native cache invalidations using `ttl=0` to assure absolute data synchronicity.

---
*Developed by lzlovecats @ 2026*
