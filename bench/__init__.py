"""Benchmarks for the aiOS CODE harness.

The question this package answers is not "which model is best on a leaderboard".
It is the narrower, more useful one:

    For work this harness should find easy, what does it cost us in tokens,
    money and wall clock -- and does the change actually work?

Every task is a real git repository the agent has to navigate, edit and finish.
Verification runs hidden tests in a separate process against whatever ended up
on disk; the agent's own claim of success is never consulted.

Layout:
    suites.py   the tasks, and how each one is checked
    custom.py   saved prompt tests you re-run against chosen models
    scoring.py  the 0-100 score and what each part of it means
    runs.py     run storage -- one isolated directory per run
    runner.py   the subprocess that actually executes a run
"""

from pathlib import Path

BENCH_DIR = Path(__file__).resolve().parent
ROOT = BENCH_DIR.parent
RUNS_DIR = BENCH_DIR / "runs"
DATA_DIR = BENCH_DIR / "data"
CUSTOM_DIR = BENCH_DIR / "custom"
PROJECT_CAMPAIGNS_DIR = BENCH_DIR / "project_campaigns"

__all__ = ["BENCH_DIR", "ROOT", "RUNS_DIR", "DATA_DIR", "CUSTOM_DIR", "PROJECT_CAMPAIGNS_DIR"]
