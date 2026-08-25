"""Run a harness benchmark from the terminal.

The same run the BENCH page starts, without the window. Both go through
`bench.runs`, so a score printed here and a score shown there can never mean
two different things.

    python -m bench.run_bench
    python -m bench.run_bench --tasks humaneval=6,bugfix=2 --label cheap
    python -m bench.run_bench --provider openrouter --model qwen/qwen3-coder

Results live in `bench/runs/<id>/run.json` and stay there; the page reads the
same directory, so a run started here shows up in the UI and vice versa.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

if __package__ in (None, ""):  # tolerate `python bench/run_bench.py`
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bench import runs, suites


def parse_counts(raw: str) -> dict:
    """`humaneval=6,bugfix=2` -> {"humaneval": 6, "bugfix": 2}."""
    if not raw:
        return dict(suites.DEFAULT_COUNTS)
    counts = {}
    for chunk in raw.split(","):
        name, _, value = chunk.partition("=")
        name = name.strip()
        if name not in suites.SUITES:
            raise SystemExit(f"unknown suite {name!r}; pick from {', '.join(suites.SUITES)}")
        counts[name] = int(value or 1)
    return counts


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tasks", default="", help="suite=count pairs, comma separated")
    parser.add_argument("--provider", default="openrouter")
    parser.add_argument("--model", default="deepseek/deepseek-v4-flash")
    parser.add_argument("--reasoning", default="off")
    parser.add_argument("--fast", action="store_true")
    parser.add_argument("--review-fix", action="store_true",
                        help="hand the reviewer's findings back for one fix pass")
    parser.add_argument("--concurrency", type=int, default=3)
    parser.add_argument("--timeout", type=float, default=600.0)
    parser.add_argument("--profile", default="lean", choices=["lean", "legacy"],
                        help="legacy restores the pre-2026-08 prompt and verification rules")
    parser.add_argument("--label", default="")
    args = parser.parse_args(argv)

    created = runs.create_run({
        "provider": args.provider,
        "model": args.model,
        "reasoning": args.reasoning,
        "fast": args.fast,
        "review_fix": args.review_fix,
        "profile": args.profile,
        "counts": parse_counts(args.tasks),
        "concurrency": args.concurrency,
        "timeout": args.timeout,
    }, label=args.label)
    if not created.get("ok"):
        print(f"could not start: {created.get('error')}", file=sys.stderr)
        return 1

    run_id = created["run"]["id"]
    print(f"run {run_id} · {args.provider}/{args.model} · {len(created['run']['tasks'])} tasks\n")

    reported: set[str] = set()
    while True:
        time.sleep(2.0)
        run = runs.get_run(run_id) or {}
        for task in run.get("tasks") or []:
            if task.get("passed") is None or task["id"] in reported:
                continue
            reported.add(task["id"])
            usage = task.get("usage") or {}
            print(f"{'PASS' if task['passed'] else 'FAIL'}  {task['id']:<34} "
                  f"{usage.get('total_tokens') or 0:>7} tok  {task.get('seconds', 0):>6}s  "
                  f"{task.get('tool_calls') or 0:>3} tools"
                  + (f"  ({task['error'][:70]})" if task.get("error") else ""))
        if str(run.get("status")) not in {"running", "starting"}:
            break

    summary = (runs.get_run(run_id) or {}).get("summary") or {}
    print(f"\nscore {summary.get('score')} ({summary.get('grade')})"
          f" · {summary.get('passed')}/{summary.get('finished')} passed"
          f" · {(summary.get('usage') or {}).get('total_tokens')} tokens"
          f" · ${summary.get('cost_usd', 0):.4f}"
          f" · {summary.get('tokens_per_pass')} tok/pass"
          f" · {summary.get('seconds_per_pass')}s/pass")
    print(f"\n{runs.run_dir(run_id) / 'run.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
