"""Immutable Phase-2 evaluation suite shared by bootstrap and runtime."""

from __future__ import annotations

from functools import lru_cache
import hashlib
import json
from pathlib import Path

from sqlalchemy import text

from schema import TABLE_AI_EVAL_CASES


EVAL_SUITE_ID = "lmc_ai_fixed_v1"
EVAL_SUITE_VERSION = 1
EVAL_ASSET = Path(__file__).resolve().parents[1] / "assets" / "ai_eval_cases_v0.json"
EVAL_TASK_TYPES = frozenset({
    "speech_review", "strategy", "attack_defence", "mock_judgement",
    "cantonese_style",
})


def _canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def case_content_hash(case: dict) -> str:
    material = {
        "case_id": case["case_id"],
        "task_type": case["task_type"],
        "title": case["title"],
        "input": case["input"],
        "rubric": case["rubric"],
        "reference_text": case["reference_text"],
    }
    return hashlib.sha256(_canonical(material).encode("utf-8")).hexdigest()


@lru_cache(maxsize=1)
def load_eval_cases() -> tuple[dict, ...]:
    raw = json.loads(EVAL_ASSET.read_text(encoding="utf-8"))
    if not isinstance(raw, list) or len(raw) != 30:
        raise RuntimeError("Phase 2 eval suite must contain exactly 30 cases")
    clean = []
    seen = set()
    for source in raw:
        if not isinstance(source, dict):
            raise RuntimeError("invalid eval case")
        case_id = str(source.get("case_id") or "")
        task_type = str(source.get("task_type") or "")
        if not case_id or case_id in seen or task_type not in EVAL_TASK_TYPES:
            raise RuntimeError("invalid or duplicate eval case")
        if not isinstance(source.get("input"), dict) or not isinstance(source.get("rubric"), dict):
            raise RuntimeError("eval input and rubric must be objects")
        case = {
            "case_id": case_id,
            "task_type": task_type,
            "title": str(source.get("title") or ""),
            "input": source["input"],
            "rubric": source["rubric"],
            "reference_text": str(source.get("reference_text") or ""),
        }
        case["content_hash"] = case_content_hash(case)
        clean.append(case)
        seen.add(case_id)
    return tuple(clean)


def suite_hash() -> str:
    material = [
        {key: value for key, value in case.items() if key != "content_hash"}
        for case in load_eval_cases()
    ]
    return hashlib.sha256(_canonical(material).encode("utf-8")).hexdigest()


def seed_default_eval_cases(conn) -> None:
    """Seed only during explicit empty-database bootstrap."""
    conn.execute(
        text(f"""INSERT INTO {TABLE_AI_EVAL_CASES}(
            case_id,suite_id,suite_version,task_type,title,input_json,rubric_json,
            reference_text,content_hash,is_active
        ) VALUES(
            :case_id,:suite_id,:suite_version,:task_type,:title,
            CAST(:input_json AS JSONB),CAST(:rubric_json AS JSONB),
            :reference_text,:content_hash,TRUE
        ) ON CONFLICT(case_id) DO NOTHING"""),
        [
            {
                "case_id": case["case_id"], "suite_id": EVAL_SUITE_ID,
                "suite_version": EVAL_SUITE_VERSION,
                "task_type": case["task_type"], "title": case["title"],
                "input_json": _canonical(case["input"]),
                "rubric_json": _canonical(case["rubric"]),
                "reference_text": case["reference_text"],
                "content_hash": case["content_hash"],
            }
            for case in load_eval_cases()
        ],
    )
