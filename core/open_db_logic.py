"""Pure query, filtering and statistics logic for the public topic bank."""

import pandas as pd

from schema import TABLE_TOPICS, TABLE_TOPIC_VOTES
from core.vote_logic import _resolve_db
from system_limits import OPEN_DB_MAX_TOPICS

DIFFICULTY_OPTIONS = {
    1: "Lv1 — 概念日常",
    2: "Lv2 — 一般議題",
    3: "Lv3 — 進階專業",
}


def fetch_open_db_data(db=None):
    """Fetch the topic rows and resolved-motion aggregates used by the API."""
    db = _resolve_db(db)
    topics = db.query(
        f"SELECT topic_text, author, category, difficulty FROM {TABLE_TOPICS} "
        "ORDER BY topic_text LIMIT :limit",
        {"limit": OPEN_DB_MAX_TOPICS},
    )
    vote_stats = db.query(
        f"""SELECT category,status,COUNT(*) AS motion_count FROM {TABLE_TOPIC_VOTES}
        WHERE status IN ('passed','rejected') GROUP BY category,status"""
    )
    return topics, vote_stats


def with_difficulty_label(topics_df):
    topics = topics_df.copy()
    if "difficulty" in topics.columns:
        topics["difficulty_label"] = topics["difficulty"].map(DIFFICULTY_OPTIONS)
    return topics


def filter_topics(topics_df, search_term="", author="全部", category="全部", difficulty="全部"):
    """Apply author/category/difficulty filters and literal case-insensitive search."""
    filtered = topics_df.copy()
    if search_term:
        filtered = filtered[
            filtered["topic_text"].str.contains(search_term, case=False, na=False, regex=False)
        ]
    if author != "全部":
        filtered = filtered[filtered["author"] == author]
    if category != "全部":
        filtered = filtered[filtered["category"] == category]
    if difficulty != "全部" and "difficulty_label" in filtered.columns:
        filtered = filtered[filtered["difficulty_label"] == difficulty]
    return filtered


def display_topics(topics_df):
    """Return the public table shape with stable Chinese field labels."""
    display = topics_df.copy()
    if "difficulty_label" in display.columns:
        display = display.drop(columns=["difficulty"]).rename(columns={"difficulty_label": "difficulty"})
    columns = [column for column in ["topic_text", "author", "category", "difficulty"] if column in display.columns]
    return display[columns].rename(columns={
        "topic_text": "辯題",
        "author": "作者",
        "category": "類別",
        "difficulty": "難度",
    })


def filter_options(topics_df):
    """Build selectbox options in the exact existing order."""
    authors = ["全部"] + sorted(topics_df["author"].dropna().unique().tolist())
    categories = ["全部"] + sorted(topics_df["category"].dropna().unique().tolist())
    difficulties = (["全部"] + sorted(topics_df["difficulty_label"].dropna().unique().tolist())
                    if "difficulty_label" in topics_df.columns else ["全部"])
    return {"authors": authors, "categories": categories, "difficulties": difficulties}


def category_distribution(topics_df):
    if "category" not in topics_df.columns:
        return pd.DataFrame(columns=["類別", "辯題數量", "佔比"])
    counts = topics_df["category"].value_counts().reset_index()
    counts.columns = ["類別", "辯題數量"]
    total = len(topics_df)
    counts["佔比"] = counts["辯題數量"].apply(lambda value: f"{value / total * 100:.1f}%")
    return counts


def difficulty_distribution(topics_df):
    if "difficulty_label" not in topics_df.columns:
        return pd.DataFrame(columns=["難度", "辯題數量", "佔比"])
    counts = topics_df["difficulty_label"].fillna("未分類").value_counts().reset_index()
    counts.columns = ["難度", "辯題數量"]
    total = len(topics_df)
    counts["佔比"] = counts["辯題數量"].apply(lambda value: f"{value / total * 100:.1f}%")
    return counts


def category_vote_pass_rate(topic_vote_stats_df):
    """Return completed topic-motion pass rates in product display order."""
    required = {"category", "status"}
    if topic_vote_stats_df.empty or not required.issubset(topic_vote_stats_df.columns):
        return pd.DataFrame(columns=["類別", "動議數量", "通過數", "投票通過率"])
    resolved = topic_vote_stats_df[topic_vote_stats_df["status"].isin(["passed", "rejected"])].copy()
    if resolved.empty:
        return pd.DataFrame(columns=["類別", "動議數量", "通過數", "投票通過率"])
    resolved["category"] = resolved["category"].fillna("未分類")
    if "motion_count" in resolved.columns:
        resolved["motion_count"] = pd.to_numeric(resolved["motion_count"], errors="coerce").fillna(0)
        resolved["passed_count"] = resolved["motion_count"].where(resolved["status"] == "passed", 0)
        stats = resolved.groupby("category", as_index=False).agg(
            動議數量=("motion_count", "sum"), 通過數=("passed_count", "sum"),
        )
    else:
        stats = resolved.groupby("category").agg(
            動議數量=("status", "count"),
            通過數=("status", lambda value: (value == "passed").sum()),
        ).reset_index()
    stats["投票通過率"] = stats["通過數"] / stats["動議數量"]
    stats = stats.sort_values(by=["投票通過率", "動議數量"], ascending=[False, False])
    display = stats.rename(columns={"category": "類別"}).copy()
    display["投票通過率"] = display["投票通過率"].apply(lambda value: f"{value:.1%}")
    return display


def dataframe_records(dataframe):
    """JSON-safe records without changing displayed column values."""
    if dataframe.empty:
        return []
    return dataframe.where(pd.notna(dataframe), None).to_dict(orient="records")
