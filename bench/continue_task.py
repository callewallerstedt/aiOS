"""Resume a timed-out / stopped custom benchmark task in its existing workspace.

The agent is told to continue — same job id, same files on disk — rather than
starting a fresh empty repo. Used when a custom prompt run hit the wall clock
before the agent was done.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bench import efficiency, runs, scoring
from bench.runner import HEARTBEAT_SECONDS, POLL_SECONDS, Run, log, tool_call_count

CONTINUE_PROMPT = """\
Continue from where you left off in this repository. Do NOT start over or wipe files.

Inspect what is already on disk, compare it to the original brief, finish anything
incomplete (geometry correctness, UI, animation, SVG export, README), verify the
app opens via index.html, then stop when it works.
"""


def continue_task(run: Run, code_jobs, task_id: str, extra_seconds: float,
                  instruction: str = "") -> None:
    task = run.task(task_id)
    job_id = str(task.get("job_id") or "")
    if not job_id:
        run.update(task_id, status="failed", passed=False, error="no job to continue")
        return

    meta = code_jobs.get_job(job_id) or {}
    if not meta:
        run.update(task_id, status="failed", passed=False, error="the old session is gone")
        return

    # Keep cumulative wall time honest: previous attempt + this continuation.
    prior_seconds = float(task.get("seconds") or 0.0)
    started = time.monotonic()
    run.update(
        task_id,
        status="running",
        passed=None,
        error="",
        finished_at=0.0,
        continued_at=round(time.time(), 3),
    )

    # This is a harness-written continuation of an already scouted and planned
    # task. Re-running those stages wastes money and can trap a weak scout in a
    # second exploration loop before the coder sees the preserved workspace.
    sent = code_jobs.send_message(job_id, instruction.strip() or CONTINUE_PROMPT, planned=False)
    if not sent.get("ok"):
        run.update(
            task_id,
            status="failed",
            passed=False,
            error=str(sent.get("error") or "could not continue the session"),
            finished_at=round(time.time(), 3),
        )
        return

    log(f"{task_id}: continuing session {job_id} · +{int(extra_seconds)}s")
    timed_out = False
    while True:
        meta = code_jobs.get_job(job_id) or {}
        if str(meta.get("status")) in code_jobs.TERMINAL_STATES:
            break
        if run.stopping:
            code_jobs.stop_job(job_id)
            meta = code_jobs.get_job(job_id) or {}
            break
        if time.monotonic() - started > extra_seconds:
            timed_out = True
            code_jobs.stop_job(job_id)
            meta = code_jobs.get_job(job_id) or {}
            break
        if str(meta.get("status")) == "waiting_user":
            code_jobs.stop_job(job_id)
            meta = code_jobs.get_job(job_id) or {}
            break
        time.sleep(POLL_SECONDS)

    events = (runs.read_task_events(run.data["id"], task_id, 0) or {}).get("events") or []
    efficiency_trace = efficiency.build_efficiency_trace(
        events,
        task_started_at=task.get("started_at") or 0.0,
    )
    usage = meta.get("usage") if isinstance(meta.get("usage"), dict) else {}
    usage = dict(usage)
    if not usage.get("cost_usd"):
        usage["cost_usd"] = meta.get("estimated_cost_usd") or 0.0

    job_status = str(meta.get("status") or "")
    elapsed = prior_seconds + (time.monotonic() - started)
    passed = (not timed_out) and job_status == "completed" and not run.stopping
    error = ""
    if timed_out:
        error = f"timed out after extra {int(extra_seconds)}s"
    elif run.stopping:
        error = "stopped"
    elif job_status != "completed":
        error = f"agent ended as {job_status or 'unknown'}"
    status = "passed" if passed else ("timeout" if timed_out else ("stopped" if run.stopping else "failed"))

    run.update(
        task_id,
        status=status,
        passed=passed,
        error=error[:400],
        seconds=round(elapsed, 1),
        usage=usage,
        role_usage=dict(meta.get("role_usage") or {}),
        pipeline_stages=dict(meta.get("pipeline_stages") or {}),
        tool_calls=int(efficiency_trace.get("total_calls") or tool_call_count(events)),
        efficiency_trace=efficiency_trace,
        events=len(events),
        checks=[],
        review=str((meta.get("review") or {}).get("verdict") or ""),
        job_status=job_status,
        files_edited=int(meta.get("files_edited") or 0),
        lines_added=int(meta.get("lines_added") or 0),
        lines_deleted=int(meta.get("lines_deleted") or 0),
        finished_at=round(time.time(), 3),
    )
    log(f"{task_id}: {'PASS' if passed else 'FAIL'} "
        f"{usage.get('total_tokens') or 0} tok · {run.task(task_id)['seconds']}s")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Continue one timed-out custom bench task.")
    parser.add_argument("--run", required=True, help="the run directory")
    parser.add_argument("--task", required=True, help="task id to continue")
    parser.add_argument("--extra-seconds", type=float, default=5400.0)
    parser.add_argument("--instruction-file", default="")
    args = parser.parse_args(argv)

    run = Run(Path(args.run).resolve())
    import code_jobs

    expected = (run.directory / "jobs").resolve()
    if code_jobs.JOBS_DIR.resolve() != expected:
        run.data["status"] = "failed"
        run.data["error"] = (
            f"refusing to continue: sessions would land in {code_jobs.JOBS_DIR}, not {expected}"
        )
        run.save()
        log(run.data["error"])
        return 2

    code_jobs._generate_title = lambda job_id: None
    run.data["status"] = "running"
    run.data["pid"] = os.getpid()
    run.data["error"] = ""
    run.save()

    beating = threading.Event()

    def heartbeat() -> None:
        while not beating.wait(HEARTBEAT_SECONDS):
            run.save()

    pulse = threading.Thread(target=heartbeat, daemon=True, name="bench-continue-heartbeat")
    pulse.start()
    instruction = ""
    instruction_path = Path(args.instruction_file).resolve() if args.instruction_file else None
    if instruction_path is not None:
        try:
            instruction = instruction_path.read_text(encoding="utf-8")[:16_000]
        except OSError:
            instruction = ""
        finally:
            instruction_path.unlink(missing_ok=True)
    try:
        continue_task(run, code_jobs, args.task, float(args.extra_seconds), instruction)
    finally:
        beating.set()

    run.data["status"] = "stopped" if run.stopping else "completed"
    run.data["finished_at"] = round(time.time(), 3)
    run.data["summary"] = scoring.summarise(run.data.get("tasks") or [])
    run.save()
    summary = run.data["summary"]
    log(f"done · continued · {summary['usage']['total_tokens']} tokens · ${summary['cost_usd']:.4f}")
    print(json.dumps(summary, indent=2), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
