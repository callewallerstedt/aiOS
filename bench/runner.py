"""The process that actually runs a benchmark.

Started by `bench.runs.create_run` with `AIOS_CODE_JOBS_DIR` pointed at the
run's own `jobs/` folder, which is what keeps benchmark sessions out of the
CODE tab. It is a separate process for exactly that reason -- `code_jobs` binds
its store at import time -- and the isolation is asserted below rather than
assumed, because getting it wrong would silently dump fifty throwaway sessions
into your real session list.

    python -m bench.runner --run bench/runs/<id>

Tasks run concurrently, each as an ordinary CODE session: same providers, same
tools, same reviewer. That is the point. We are measuring the harness you use,
not a special benchmark mode of it.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

if __package__ in (None, ""):  # tolerate `python bench/runner.py`
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bench import adapters, efficiency, runs, scoring, suites

VERIFY_TIMEOUT = 120.0
POLL_SECONDS = 2.0
HEARTBEAT_SECONDS = 20.0
CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0

_LOCK = threading.Lock()


def log(message: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {message}", flush=True)


# ------------------------------------------------------------------ run state


class Run:
    """The run document, with the only writer in the system behind a lock."""

    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self.path = directory / "run.json"
        self.stop_path = directory / "STOP"
        self.data = json.loads(self.path.read_text(encoding="utf-8"))

    @property
    def stopping(self) -> bool:
        return self.stop_path.exists()

    @property
    def max_cost_usd(self) -> float:
        try:
            return max(0.0, float((self.data.get("config") or {}).get("max_cost_usd") or 0.0))
        except (TypeError, ValueError):
            return 0.0

    def spent_usd(self) -> float:
        summary = scoring.summarise(self.data.get("tasks") or [])
        try:
            return max(0.0, float(summary.get("cost_usd") or 0.0))
        except (TypeError, ValueError):
            return 0.0

    def remaining_usd(self) -> float:
        return max(0.0, self.max_cost_usd - self.spent_usd()) if self.max_cost_usd else 0.0

    def budget_exhausted(self) -> bool:
        return bool(self.max_cost_usd and self.spent_usd() >= self.max_cost_usd)

    def save(self) -> None:
        with _LOCK:
            self.data["summary"] = scoring.summarise(self.data.get("tasks") or [])
            self.data["budget"] = runs._budget_snapshot(self.data, self.data["summary"])
            runs.write_run(self.data)

    def task(self, task_id: str) -> dict:
        for row in self.data["tasks"]:
            if row["id"] == task_id:
                return row
        raise KeyError(task_id)

    def update(self, task_id: str, **fields) -> None:
        with _LOCK:
            self.task(task_id).update(fields)
        self.save()


# ---------------------------------------------------------------- verification


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def protected_checks(task: suites.Task, workspace: Path) -> list[dict]:
    """Frozen files must come back byte-identical.

    A run that edits the test it was asked to satisfy has not solved anything,
    however green the result looks, so this is a check like any other and its
    failure fails the task.
    """
    results = []
    for name in task.protected:
        expected = task.files.get(name, "")
        target = workspace / name
        try:
            actual = target.read_text(encoding="utf-8")
        except OSError:
            results.append({"name": f"{name} still exists", "passed": False,
                            "detail": "the file was deleted or renamed"})
            continue
        same = _sha256(actual) == _sha256(expected)
        results.append({
            "name": f"{name} left untouched",
            "passed": same,
            "detail": "" if same else "the brief said not to edit this file",
        })
    return results


def verify(task: suites.Task, workspace: Path, verify_dir: Path) -> dict:
    """Run the hidden checks in a fresh process against what is on disk."""
    verify_dir.mkdir(parents=True, exist_ok=True)
    script = verify_dir / f"{task.id.replace('/', '-')}.py"
    script.write_text(task.verifier, encoding="utf-8")
    try:
        verify_env = os.environ.copy()
        # A fast same-size edit can retain the source file's timestamp and make
        # Python reuse a stale workspace __pycache__. Give every external grade
        # a fresh cache root so the verdict always reflects bytes now on disk.
        verify_env["PYTHONPYCACHEPREFIX"] = str(verify_dir / f"pycache-{time.time_ns()}")
        result = subprocess.run(
            [sys.executable, str(script), str(workspace)],
            capture_output=True, text=True, timeout=VERIFY_TIMEOUT, cwd=str(workspace),
            creationflags=CREATE_NO_WINDOW, env=verify_env,
        )
    except subprocess.TimeoutExpired:
        return {"passed": False, "checks": [], "error": "verification timed out"}
    except OSError as exc:
        return {"passed": False, "checks": [], "error": f"could not verify: {exc}"}

    for line in reversed((result.stdout or "").splitlines()):
        if line.startswith("__BENCH__"):
            try:
                payload = json.loads(line[len("__BENCH__"):])
            except json.JSONDecodeError:
                break
            return {"passed": bool(payload.get("passed")), "checks": payload.get("checks") or [],
                    "error": ""}
    # No verdict line means the checks could not even import the module.
    detail = ((result.stderr or "").strip() or (result.stdout or "").strip())[-400:]
    return {"passed": False, "checks": [], "error": detail or "the checks produced no verdict"}


def tool_call_count(events: list[dict]) -> int:
    """Real tool calls, not activity rows.

    A thinking block emits started/update/completed, so counting rows made
    three tools out of one thought and reported ~20 calls for a task that made
    three.
    """
    return int(efficiency.build_efficiency_trace(events).get("total_calls") or 0)


# ------------------------------------------------------------------- one task


def run_task(run: Run, code_jobs, task: suites.Task) -> None:
    """One task, and a promise: it always ends with a verdict written down.

    A task that raised used to be left saying "running" with `passed` still
    null, and scoring skips anything unfinished -- so a run where half the
    tasks crashed reported 100/100 over the half that survived. A crash is a
    failure of that task, and it is recorded as one.
    """
    try:
        _run_task(run, code_jobs, task)
    except Exception as exc:
        log(f"{task.id}: crashed · {type(exc).__name__}: {exc}")
        run.update(
            task.id,
            status="failed",
            passed=False,
            error=f"the runner crashed on this task: {type(exc).__name__}: {exc}"[:400],
            finished_at=round(time.time(), 3),
        )


def _run_task(run: Run, code_jobs, task: suites.Task) -> None:
    config = run.data["config"]
    engine = str(config.get("engine") or "aios").strip().casefold()
    workspace = runs.workspace_of(run.data["id"], task.id)
    provider = task.provider or config["provider"]
    model = task.model or config["model"]
    reasoning = task.reasoning or config["reasoning"]
    fast = bool(task.fast if task.provider else config.get("fast"))
    agent_id = int(run.data.get("agent_id") or 0)
    preview_port = int(run.data.get("preview_port") or 0)
    identity = ""
    runtime_env: dict[str, str] = {}
    if agent_id and preview_port:
        preview_url = f"http://127.0.0.1:{preview_port}"
        identity = (
            f"You are Agent #{agent_id:03d} in a concurrent benchmark. "
            f"Your exclusive preview port is {preview_port}; its last three digits match your ID. "
            f"Use only {preview_url} for preview servers and HTTP checks. "
            "Read AIOS_PREVIEW_PORT or AIOS_PREVIEW_URL in scripts instead of hard-coding a port. "
            "Never use ports 3000, 4173, 5000, 8000, or 8080 because other agents run concurrently."
        )
        runtime_env = {"AIOS_PREVIEW_PORT": str(preview_port), "AIOS_PREVIEW_URL": preview_url}

    if run.stopping:
        run.update(task.id, status="skipped")
        return
    if run.budget_exhausted():
        run.update(
            task.id,
            status="budget_stopped",
            passed=False,
            error=f"reported-cost ceiling of ${run.max_cost_usd:.2f} was already reached",
            finished_at=round(time.time(), 3),
        )
        return

    started = time.monotonic()
    # The workspace path travels with the task so the UI can open the repository
    # the agent actually worked in, and you can read the diff yourself.
    run.update(task.id, status="running", started_at=round(time.time(), 3),
               workspace=str(workspace), provider=provider, model=model,
               reasoning=reasoning, fast=fast, agent_id=agent_id, preview_port=preview_port)
    try:
        task.build(workspace)
    except Exception as exc:
        run.update(task.id, status="failed", passed=False, error=f"could not build the task repo: {exc}",
                   finished_at=round(time.time(), 3))
        return

    brief = task.brief if not identity else f"{identity}\n\n{task.brief}"
    timeout = float(config["timeout"])
    task_budget = run.remaining_usd()
    timed_out = budget_stopped = False
    meta: dict = {}
    job_error = ""

    if engine == "aios":
        job_options = {
            "title": f"{task.suite} · {task.title}",
            "strategy": str(config.get("strategy") or "auto"),
        }
        if identity:
            job_options.update(runtime_env=runtime_env, system_context=identity)
        created = code_jobs.create_job(
            provider, str(workspace), brief, model,
            reasoning, fast, **job_options,
        )
        if not created.get("ok") or not (created.get("job") or {}).get("id"):
            run.update(task.id, status="failed", passed=False,
                       error=str(created.get("error") or "the harness refused the job"),
                       seconds=round(time.monotonic() - started, 1),
                       finished_at=round(time.time(), 3))
            return

        job_id = str(created["job"]["id"])
        run.update(task.id, job_id=job_id)
        log(f"{task.id}: session {job_id} · {provider}/{model}")

        while True:
            meta = code_jobs.get_job(job_id) or {}
            if str(meta.get("status")) in code_jobs.TERMINAL_STATES:
                break
            if run.stopping:
                code_jobs.stop_job(job_id)
                meta = code_jobs.get_job(job_id) or {}
                break
            if time.monotonic() - started > timeout:
                timed_out = True
                code_jobs.stop_job(job_id)
                meta = code_jobs.get_job(job_id) or {}
                break
            if task_budget:
                live_usage = meta.get("usage") if isinstance(meta.get("usage"), dict) else {}
                try:
                    live_cost = float(live_usage.get("cost_usd") or meta.get("estimated_cost_usd") or 0.0)
                except (TypeError, ValueError):
                    live_cost = 0.0
                if live_cost >= task_budget:
                    budget_stopped = True
                    code_jobs.stop_job(job_id)
                    meta = code_jobs.get_job(job_id) or {}
                    break
            # A waiting session will never move on its own: nobody is going to
            # answer its question, so treat the question as the end of the attempt.
            if str(meta.get("status")) == "waiting_user":
                code_jobs.stop_job(job_id)
                meta = code_jobs.get_job(job_id) or {}
                break
            time.sleep(POLL_SECONDS)
    else:
        safe_task_id = "".join(character if character.isalnum() else "-" for character in task.id).strip("-")
        job_id = f"native-{engine}-{safe_task_id}"[:120]
        run.update(task.id, job_id=job_id)
        log(f"{task.id}: raw {engine} session {job_id} · {model or 'native default'}")
        native = adapters.run_native(
            engine,
            workspace,
            brief,
            model,
            reasoning,
            run.directory / "jobs" / job_id,
            timeout,
            max_cost_usd=task_budget,
            should_stop=lambda: run.stopping,
        )
        timed_out = bool(native.get("timed_out"))
        job_error = str(native.get("error") or "")
        native_usage = native.get("usage") if isinstance(native.get("usage"), dict) else {}
        try:
            reported_cost = float(native_usage.get("cost_usd") or 0.0)
        except (TypeError, ValueError):
            reported_cost = 0.0
        budget_stopped = bool(
            task_budget
            and (reported_cost >= task_budget or "budget" in job_error.casefold())
        )
        meta = {
            "status": native.get("status") or "failed",
            "model": native.get("model") or model,
            "native_primary_model": native.get("primary_model") or "",
            "native_models_used": list(native.get("models_used") or []),
            "usage": dict(native_usage),
            "estimated_cost_usd": reported_cost,
            "cost_provenance": native.get("cost_provenance") or config.get("cost_provenance") or "unavailable",
            "tool_calls": native.get("tool_calls") or 0,
            "files_edited": native.get("files_edited") or 0,
            "lines_added": native.get("lines_added") or 0,
            "lines_deleted": native.get("lines_deleted") or 0,
            "role_usage": {},
            "pipeline_stages": {},
            "model_request_count": native.get("model_request_count"),
            "model_request_count_source": native.get("model_request_count_source") or "unavailable",
            "model_request_rounds": list(native.get("model_request_rounds") or []),
            "model_request_rounds_omitted": int(native.get("model_request_rounds_omitted") or 0),
        }

    events = (runs.read_task_events(run.data["id"], task.id, 0) or {}).get("events") or []
    task_started_at = (run.task(task.id) or {}).get("started_at") or 0.0
    efficiency_trace = efficiency.build_efficiency_trace(
        events,
        task_started_at=task_started_at,
        default_role="native" if engine != "aios" else "",
    )
    usage = meta.get("usage") if isinstance(meta.get("usage"), dict) else {}
    usage = dict(usage)
    # record_usage() copies the provider's reported cost into both fields; take
    # whichever one this provider populated. Nothing here is estimated.
    if not usage.get("cost_usd"):
        usage["cost_usd"] = meta.get("estimated_cost_usd") or 0.0

    job_status = str(meta.get("status") or "")
    if task.suite in {"custom", "project"}:
        # No hidden checks. Finished cleanly means the attempt completed; you
        # judge custom work yourself, while project lanes also persist an exact
        # byte-level diff against their common immutable snapshot.
        run.update(task.id, status="verifying")
        passed = (not timed_out) and (not budget_stopped) and job_status == "completed" and not run.stopping
        checks: list[dict] = []
        error = ""
        if timed_out:
            error = f"timed out after {int(timeout)}s"
        elif budget_stopped:
            error = f"reported-cost ceiling reached after ${float(usage.get('cost_usd') or 0):.4f}"
        elif run.stopping:
            error = "stopped"
        elif job_status != "completed":
            error = job_error or f"agent ended as {job_status or 'unknown'}"
        status = "passed" if passed else ("timeout" if timed_out else (
            "budget_stopped" if budget_stopped else ("stopped" if run.stopping else "failed")
        ))
    else:
        run.update(task.id, status="verifying")
        checked = verify(task, workspace, run.directory / "verify")
        checks = protected_checks(task, workspace) + (checked.get("checks") or [])
        native_failed = engine != "aios" and job_status != "completed"
        passed = (
            bool(checked.get("passed"))
            and all(row["passed"] for row in checks)
            and not native_failed
        )
        error = checked.get("error") or ""
        failed = [row["name"] for row in checks if not row["passed"]]
        grader_error = error or (
            f"failed {len(failed)} of {len(checks)} checks: {', '.join(failed[:3])}"
            if failed else ""
        )
        if timed_out:
            error = f"timed out after {int(timeout)}s" + (f" · {grader_error}" if grader_error else "")
        elif budget_stopped and not passed:
            error = "reported-cost ceiling reached" + (f" · {grader_error}" if grader_error else "")
        elif native_failed:
            detail = job_error or f"process ended as {job_status or 'unknown'}"
            error = f"native adapter failed: {detail}"
            if grader_error:
                error += f" · hidden grader: {grader_error}"
        elif not passed:
            error = grader_error
        status = "passed" if passed else ("timeout" if timed_out else (
            "budget_stopped" if budget_stopped else "failed"
        ))

    project_result = {}
    if task.suite == "project":
        try:
            from . import project_campaigns
            project_result = project_campaigns.workspace_diff(
                str(config.get("project_campaign_id") or ""), workspace,
            )
        except Exception as exc:
            project_result = {"error": f"could not compare project result: {exc}"}

    run.update(
        task.id,
        status=status,
        passed=passed,
        error=error[:400],
        seconds=round(time.monotonic() - started, 1),
        usage=usage,
        model=str(meta.get("model") or model),
        native_primary_model=str(meta.get("native_primary_model") or ""),
        native_models_used=list(meta.get("native_models_used") or []),
        cost_provenance=str(meta.get("cost_provenance") or config.get("cost_provenance") or "provider_reported"),
        role_usage=dict(meta.get("role_usage") or {}),
        pipeline_stages=dict(meta.get("pipeline_stages") or {}),
        tool_calls=int(efficiency_trace.get("total_calls") or meta.get("tool_calls") or 0),
        efficiency_trace=efficiency_trace,
        model_request_count=meta.get("model_request_count"),
        model_request_count_source=str(meta.get("model_request_count_source") or "unavailable"),
        model_request_rounds=list(meta.get("model_request_rounds") or [])[:128],
        model_request_rounds_omitted=int(meta.get("model_request_rounds_omitted") or 0),
        events=len(events),
        checks=checks,
        review=str((meta.get("review") or {}).get("verdict") or ""),
        job_status=job_status,
        agent_error=job_error,
        files_edited=int(meta.get("files_edited") or 0),
        lines_added=int(meta.get("lines_added") or 0),
        lines_deleted=int(meta.get("lines_deleted") or 0),
        project_result=project_result,
        finished_at=round(time.time(), 3),
    )
    log(f"{task.id}: {'PASS' if passed else 'FAIL'} "
        f"{usage.get('total_tokens') or 0} tok · {run.task(task.id)['seconds']}s")


# ------------------------------------------------------------------- the run


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Execute one aiOS harness benchmark run.")
    parser.add_argument("--run", required=True, help="the run directory")
    args = parser.parse_args(argv)

    run = Run(Path(args.run).resolve())
    import code_jobs

    # The isolation is load-bearing, so prove it rather than trust the caller.
    expected = (run.directory / "jobs").resolve()
    if code_jobs.JOBS_DIR.resolve() != expected:
        run.data["status"] = "failed"
        run.data["error"] = (
            f"refusing to run: sessions would land in {code_jobs.JOBS_DIR}, not {expected}"
        )
        run.save()
        log(run.data["error"])
        return 2

    # A benchmark should not pay an extra model call per task to invent a title
    # it will never read. The titles come from the task ids instead.
    code_jobs._generate_title = lambda job_id: None

    run.data["status"] = "running"
    # The pid the UI checks when a run has gone quiet for too long.
    run.data["pid"] = os.getpid()
    run.save()

    selected = runs.select_tasks(run.data["config"])
    harness = run.data["config"].get("model") or "?"
    if run.data["config"].get("kind") == "custom":
        harness = f"custom · {len(selected)} model{'s' if len(selected) != 1 else ''}"
    else:
        harness = f"{run.data['config']['provider']}/{run.data['config']['model']}"
    log(f"{len(selected)} tasks · {harness} "
        f"· concurrency {run.data['config']['concurrency']}")

    beating = threading.Event()

    def heartbeat() -> None:
        # Proof of life. Without it a task that thinks for two minutes looks
        # like a runner that died, and the UI would call the run interrupted.
        while not beating.wait(HEARTBEAT_SECONDS):
            run.save()

    pulse = threading.Thread(target=heartbeat, daemon=True, name="bench-heartbeat")
    pulse.start()

    try:
        with ThreadPoolExecutor(max_workers=int(run.data["config"]["concurrency"]),
                                thread_name_prefix="bench-task") as pool:
            futures = [pool.submit(run_task, run, code_jobs, task) for task in selected]
            for future in futures:
                # run_task records its own crashes; this only catches a failure
                # of the pool itself, which must not take the run down with it.
                try:
                    future.result()
                except Exception as exc:
                    log(f"the task pool failed: {exc}")
    finally:
        beating.set()

    run.data["status"] = "stopped" if run.stopping else "completed"
    run.data["finished_at"] = round(time.time(), 3)
    run.save()
    summary = run.data["summary"]
    cost_label = f"${summary['cost_usd']:.4f}" if summary.get("cost_usd") is not None else "cost n/a"
    log(f"done · score {summary['score']} ({summary['grade']}) · "
        f"{summary['passed']}/{summary['finished']} passed · "
        f"{summary['usage']['total_tokens']} tokens · {cost_label}")
    print(json.dumps(summary, indent=2), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
