"""One number for a run, and an honest account of where it came from.

A single score is only useful if you can say what it measures, so this is
deliberately simple arithmetic you can check by hand:

    score = 60 x correctness + 20 x efficiency + 20 x speed

* **correctness** is the share of tasks whose hidden checks all passed. It is
  worth three times either other component because a cheap wrong answer is not
  a good answer.
* **efficiency** compares tokens spent *per solved task* against a reference
  budget. Per solved task, not per task: a run that burns tokens failing is
  penalised twice, which is correct.
* **speed** does the same for wall clock, using each task's own elapsed time.
  Not the run's total, because that would reward turning up the concurrency
  rather than making the harness faster.

Both budget components saturate at 1.0 -- there is no credit above the
reference -- so the raw ratios are reported alongside them. A harness twice as
cheap as the reference shows up as `tokens_per_pass`, not as a score above 100.

The references are a calibration point, not a law. They were set from a
measured baseline (deepseek-v4-flash on the HumanEval tasks: ~17k tokens and
~45s per solved task) and then tightened, so a good run lands in the nineties
and a great one has somewhere left to go.
"""

from __future__ import annotations

# Tokens we are willing to spend to solve one benchmark task.
TOKEN_REFERENCE = 25_000
# Seconds one task should take, measured per task rather than per run.
SECONDS_REFERENCE = 90.0

WEIGHTS = {"correctness": 60.0, "efficiency": 20.0, "speed": 20.0}


def _ratio(reference: float, actual: float) -> float:
    """Reference over actual, clamped to [0, 1]. Zero actual means no data."""
    if actual <= 0:
        return 0.0
    return max(0.0, min(1.0, reference / actual))


def _int(value) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _float(value) -> float:
    try:
        return max(0.0, float(value or 0.0))
    except (TypeError, ValueError):
        return 0.0


def grade(score: float) -> str:
    if score >= 90:
        return "excellent"
    if score >= 75:
        return "strong"
    if score >= 60:
        return "fair"
    if score >= 40:
        return "weak"
    return "poor"


def summarise(tasks: list[dict]) -> dict:
    """Roll a run's task records into totals, a score, and its workings.

    Only tasks that actually finished are scored. A run you stopped halfway
    would otherwise report a pass rate against tasks that never ran.
    """
    finished = [task for task in tasks if task.get("passed") is not None]
    passed = [task for task in finished if task.get("passed")]

    usage_keys = ("input_tokens", "cached_input_tokens", "output_tokens",
                  "reasoning_tokens", "total_tokens")
    usage = {key: 0 for key in usage_keys}
    canonical_prompt_tokens = 0
    cost = 0.0
    cost_available = True
    for task in tasks:
        row = task.get("usage") if isinstance(task.get("usage"), dict) else {}
        for key in usage_keys:
            usage[key] += _int(row.get(key))
        canonical_prompt_tokens += _int(
            row.get("canonical_prompt_tokens")
            if "canonical_prompt_tokens" in row
            else row.get("input_tokens")
        )
        if str(task.get("cost_provenance") or "provider_reported") == "unavailable":
            cost_available = False
        else:
            cost += _float(row.get("cost_usd"))

    seconds = sum(_float(task.get("seconds")) for task in tasks)
    tool_calls = sum(_int(task.get("tool_calls")) for task in tasks)
    count = len(finished)
    solved = len(passed)

    tokens_per_pass = round(usage["total_tokens"] / solved) if solved else None
    seconds_per_pass = round(sum(_float(t.get("seconds")) for t in passed) / solved, 1) if solved else None
    cost_per_pass = round(cost / solved, 5) if solved and cost_available else None

    correctness = (solved / count) if count else 0.0
    # No credit for being fast and cheap at getting it wrong.
    efficiency = _ratio(TOKEN_REFERENCE, tokens_per_pass or 0) if solved else 0.0
    speed = _ratio(SECONDS_REFERENCE, seconds_per_pass or 0) if solved else 0.0

    parts = {
        "correctness": round(correctness * WEIGHTS["correctness"], 1),
        "efficiency": round(efficiency * WEIGHTS["efficiency"], 1),
        "speed": round(speed * WEIGHTS["speed"], 1),
    }
    score = round(sum(parts.values()), 1)

    by_suite: dict[str, dict] = {}
    for task in finished:
        bucket = by_suite.setdefault(str(task.get("suite") or "other"), {"tasks": 0, "passed": 0})
        bucket["tasks"] += 1
        bucket["passed"] += 1 if task.get("passed") else 0

    return {
        "score": score,
        "grade": grade(score),
        "parts": parts,
        "weights": dict(WEIGHTS),
        "components": {
            "correctness": round(correctness, 3),
            "efficiency": round(efficiency, 3),
            "speed": round(speed, 3),
        },
        "reference": {"tokens_per_pass": TOKEN_REFERENCE, "seconds_per_pass": SECONDS_REFERENCE},
        "tasks": len(tasks),
        "finished": count,
        "passed": solved,
        "failed": count - solved,
        "pass_rate": round(correctness, 3),
        "usage": usage,
        "cost_usd": round(cost, 6) if cost_available else None,
        "cost_available": cost_available,
        "tokens_per_pass": tokens_per_pass,
        "seconds_per_pass": seconds_per_pass,
        "cost_per_pass": cost_per_pass,
        "total_seconds": round(seconds, 1),
        "tool_calls": tool_calls,
        "canonical_prompt_tokens": canonical_prompt_tokens,
        "cache_hit_rate": (
            round(usage["cached_input_tokens"] / canonical_prompt_tokens, 3)
            if canonical_prompt_tokens else None
        ),
        "by_suite": by_suite,
    }
