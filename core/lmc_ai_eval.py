"""Provider-neutral contracts for the fixed local-AI blind evaluation."""

from __future__ import annotations

from collections import defaultdict
import hashlib
import json
import math
from statistics import median

from ai_model_config import LMC_AI_MODE_OPTIONS
from core.ai_eval_defaults import EVAL_SUITE_ID, suite_hash


EVAL_PROMPT_VERSION = 1
EVAL_MODES = ("daily", "complex", "deep")
EVAL_PAIRS = (("daily", "complex"), ("daily", "deep"), ("complex", "deep"))
REVIEW_DIMENSIONS = (
    "overall", "cantonese", "reasoning", "usefulness", "factual", "privacy",
)
REVIEW_CHOICES = frozenset({"left", "right", "tie", "both_bad"})
_MODE_ORDERS = (
    ("daily", "complex", "deep"), ("daily", "deep", "complex"),
    ("complex", "daily", "deep"), ("complex", "deep", "daily"),
    ("deep", "daily", "complex"), ("deep", "complex", "daily"),
)


def _canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def prompt_fingerprint() -> str:
    material = {
        "suite_id": EVAL_SUITE_ID,
        "suite_hash": suite_hash(),
        "prompt_version": EVAL_PROMPT_VERSION,
        "task_types": [
            "speech_review", "strategy", "attack_defence", "mock_judgement",
            "cantonese_style",
        ],
    }
    return hashlib.sha256(_canonical(material).encode("utf-8")).hexdigest()


def build_eval_prompt(task_type: str, input_data: dict) -> str:
    """Build the only model input; reference/rubric are intentionally absent."""
    data = input_data if isinstance(input_data, dict) else {}
    if task_type == "speech_review":
        return (
            f"辯題：{data.get('topic', '')}\n立場：{data.get('side', '')}\n"
            f"發言內容：{data.get('text', '')}\n\n"
            "請用自然香港粵語，指出最核心嘅論證問題，解釋點解會影響說服力，"
            "再畀具體、可以直接改稿或練習嘅改善方法。唔好虛構資料。"
        )
    if task_type == "strategy":
        return (
            f"辯題：{data.get('topic', '')}\n立場：{data.get('side', '')}\n\n"
            "請用自然香港粵語建立一套完整辯論策略，包括主線同判準、論證機制、"
            "主要攻防、對方最強反駁及賽前準備重點。唔好虛構事實或數據。"
        )
    if task_type == "attack_defence":
        if str(data.get("opponent") or "").strip():
            return (
                f"對方講法：{data.get('opponent')}\n\n"
                "請用自然香港粵語設計精準追問，指出追問要迫對方交代嘅漏洞，"
                "並提供對方回應後嘅下一步攻防。"
            )
        return (
            f"對方問題：{data.get('question', '')}\n\n"
            "請用自然香港粵語先直接回應，再拆解問題前設，最後提供後續攻防。"
        )
    if task_type == "mock_judgement":
        return (
            f"比賽片段：{data.get('transcript', '')}\n\n"
            "只可以根據以上片段，用自然香港粵語比較雙方論證、攻防同勝負關鍵。"
            "如果資料不足，必須清楚講明，唔可以補作未提供嘅發言或宣布無根據勝方。"
        )
    if task_type == "cantonese_style":
        value = str(data.get("text") or "").strip()
        if not value:
            raise ValueError("cantonese_style text is required")
        return value
    raise ValueError("unsupported eval task type")


def generation_order(case_hash: str) -> tuple[str, str, str]:
    value = str(case_hash or "")
    if len(value) != 64:
        raise ValueError("invalid case hash")
    # The fixed suite gets an exact 10/10/10 first-position balance while the
    # content hash still makes the assignment stable across bootstrap paths.
    from core.ai_eval_defaults import load_eval_cases
    suite_hashes = sorted(case["content_hash"] for case in load_eval_cases())
    try:
        position = suite_hashes.index(value)
    except ValueError:
        position = int(value[:8], 16)
    return _MODE_ORDERS[position % len(_MODE_ORDERS)]


def validate_review_payload(payload: dict) -> dict:
    clean = {}
    for dimension in REVIEW_DIMENSIONS:
        value = str(payload.get(dimension) or "")
        if value not in REVIEW_CHOICES:
            raise ValueError(f"invalid {dimension} review choice")
        clean[dimension] = value
    return clean


def _score(choice: str, left_mode: str, right_mode: str) -> dict[str, float]:
    if choice == "left":
        return {left_mode: 1.0, right_mode: 0.0}
    if choice == "right":
        return {left_mode: 0.0, right_mode: 1.0}
    if choice == "tie":
        return {left_mode: 0.5, right_mode: 0.5}
    return {left_mode: 0.0, right_mode: 0.0}


def _percentile(values: list[int], quantile: float) -> int:
    if not values:
        return 0
    ordered = sorted(values)
    return int(ordered[math.ceil(quantile * len(ordered)) - 1])


def aggregate_campaign(reviews: list[dict], outputs: list[dict], cases: dict[str, dict]) -> dict:
    """Create a deterministic, identity-free immutable campaign summary."""
    mode_scores = {mode: {dimension: 0.0 for dimension in REVIEW_DIMENSIONS} for mode in EVAL_MODES}
    mode_denominators = {mode: {dimension: 0 for dimension in REVIEW_DIMENSIONS} for mode in EVAL_MODES}
    pair_matrix = defaultdict(lambda: {"mode_a_wins": 0, "mode_b_wins": 0, "ties": 0, "both_bad": 0})
    task_scores = defaultdict(lambda: {mode: {dimension: 0.0 for dimension in REVIEW_DIMENSIONS} for mode in EVAL_MODES})
    task_denominators = defaultdict(lambda: {mode: {dimension: 0 for dimension in REVIEW_DIMENSIONS} for mode in EVAL_MODES})
    both_bad_cases = set()
    safety_failure_cases = set()
    for review in reviews:
        left, right = review["left_mode"], review["right_mode"]
        mode_a, mode_b = sorted((left, right))
        pair_key = "_vs_".join((mode_a, mode_b))
        task = cases[review["case_id"]]["task_type"]
        for dimension in REVIEW_DIMENSIONS:
            choice = review[dimension]
            points = _score(choice, left, right)
            for mode in (left, right):
                mode_scores[mode][dimension] += points[mode]
                mode_denominators[mode][dimension] += 1
                task_scores[task][mode][dimension] += points[mode]
                task_denominators[task][mode][dimension] += 1
            if dimension == "privacy" and choice == "both_bad":
                safety_failure_cases.add(review["case_id"])
        overall = review["overall"]
        pair_matrix[pair_key]["mode_a"] = mode_a
        pair_matrix[pair_key]["mode_b"] = mode_b
        if overall == "left": pair_matrix[pair_key]["mode_a_wins" if left == mode_a else "mode_b_wins"] += 1
        elif overall == "right": pair_matrix[pair_key]["mode_a_wins" if right == mode_a else "mode_b_wins"] += 1
        elif overall == "tie": pair_matrix[pair_key]["ties"] += 1
        else:
            pair_matrix[pair_key]["both_bad"] += 1
            both_bad_cases.add(review["case_id"])
    durations = [max(0, int(item.get("duration_ms") or 0)) for item in outputs if item.get("status") == "succeeded"]
    failed = [item for item in outputs if item.get("status") == "failed"]
    def ratios(scores, denominators):
        return {mode: {dimension: (scores[mode][dimension] / denominators[mode][dimension] if denominators[mode][dimension] else 0.0) for dimension in REVIEW_DIMENSIONS} for mode in EVAL_MODES}
    return {
        "scoring": "selected=1,tie=0.5,both_bad=0; denominator includes both_bad",
        "mode_scores": ratios(mode_scores, mode_denominators),
        "head_to_head": dict(pair_matrix),
        "task_type_scores": {task: ratios(values, task_denominators[task]) for task, values in task_scores.items()},
        "both_bad_cases": sorted(both_bad_cases),
        "safety_failure_cases": sorted(safety_failure_cases),
        "generation": {
            "succeeded": sum(item.get("status") == "succeeded" for item in outputs),
            "failed": len(failed),
            "attempts": sum(int(item.get("attempt_count") or 0) for item in outputs),
            "input_tokens": sum(int(item.get("input_tokens") or 0) for item in outputs),
            "output_tokens": sum(int(item.get("output_tokens") or 0) for item in outputs),
            "median_latency_ms": int(median(durations)) if durations else 0,
            "p95_latency_ms": _percentile(durations, 0.95),
        },
    }
