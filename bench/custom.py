"""Saved custom benchmarks: a prompt you re-run against chosen models.

Unlike the fixed suites, a custom benchmark has no hidden checks. The point is
to give the same brief to one or more harnesses, keep every attempt in its own
run folder, and compare tokens, time, cost and what landed on disk.

    bench/custom/<id>/
        definition.json   name, prompt, notes -- the saved test

Runs still live under `bench/runs/<run id>/`, tagged with `kind: "custom"` and
the definition id, so the existing isolation story is unchanged.
"""

from __future__ import annotations

import json
import re
import time
import uuid
from pathlib import Path
from typing import Any

from . import CUSTOM_DIR
from .suites import Task

_EMPTY_README = """\
# Custom task

Empty repository. Build whatever the brief asks for here.
"""

# A no-op check set so Task.verifier is still valid if something asks for it.
# The runner does not run these for custom tasks; completion is the verdict.
_CUSTOM_CHECKS = """
@case("workspace exists")
def _workspace_exists():
    from pathlib import Path
    assert Path(WORKSPACE).is_dir()
"""


def _atomic_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(f".{uuid.uuid4().hex[:8]}.tmp")
    try:
        temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        temp.replace(path)
    finally:
        temp.unlink(missing_ok=True)


def _read(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _safe_id(value: str) -> str:
    safe = re.sub(r"[^a-zA-Z0-9._-]+", "-", str(value or "").strip()).strip("-._").lower()
    return safe[:48]


def definition_dir(custom_id: str) -> Path:
    safe = _safe_id(custom_id)
    if not safe or safe != str(custom_id):
        raise ValueError("bad custom id")
    return CUSTOM_DIR / safe


def list_definitions(limit: int = 100) -> list[dict]:
    CUSTOM_DIR.mkdir(parents=True, exist_ok=True)
    rows = []
    for directory in CUSTOM_DIR.iterdir():
        if not directory.is_dir():
            continue
        payload = _read(directory / "definition.json")
        if payload.get("id"):
            rows.append(summarise(payload))
    rows.sort(key=lambda row: float(row.get("updated_at") or 0), reverse=True)
    return rows[: max(1, min(int(limit or 100), 200))]


def summarise(definition: dict) -> dict:
    prompt = str(definition.get("prompt") or "")
    return {
        "id": definition.get("id"),
        "name": definition.get("name") or definition.get("id"),
        "title": definition.get("title") or "",
        "prompt": prompt,
        "notes": definition.get("notes") or "",
        "info": definition.get("info") or "",
        "tasks": definition.get("tasks") or [],
        "created_at": definition.get("created_at"),
        "updated_at": definition.get("updated_at"),
        "prompt_chars": len(prompt),
        "prompt_preview": prompt.strip().splitlines()[0][:120] if prompt.strip() else "",
    }


def _normalise_tasks(raw: Any) -> list[dict[str, str]]:
    """Clean the reusable task list while preserving the user's order."""
    rows = raw if isinstance(raw, list) else []
    tasks: list[dict[str, str]] = []
    for index, row in enumerate(rows[:24]):
        if not isinstance(row, dict):
            continue
        prompt = str(row.get("prompt") or "").strip()
        if not prompt:
            continue
        if len(prompt) > 40_000:
            raise ValueError("a task prompt is too long")
        tasks.append({
            "id": _safe_id(str(row.get("id") or "")) or f"task-{index + 1}",
            "title": str(row.get("title") or f"Task {index + 1}").strip()[:120],
            "info": str(row.get("info") or "").strip()[:1000],
            "prompt": prompt,
        })
    return tasks


def get_definition(custom_id: str) -> dict | None:
    try:
        payload = _read(definition_dir(custom_id) / "definition.json")
    except ValueError:
        return None
    return payload if payload.get("id") else None


def create_definition(raw: dict) -> dict:
    data = raw if isinstance(raw, dict) else {}
    name = str(data.get("name") or "").strip()[:80]
    prompt = str(data.get("prompt") or "").strip()
    notes = str(data.get("notes") or "").strip()[:500]
    title = str(data.get("title") or "").strip()[:120]
    info = str(data.get("info") or "").strip()[:2000]
    try:
        tasks = _normalise_tasks(data.get("tasks"))
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    if not name:
        return {"ok": False, "error": "give the test a name"}
    if not prompt and not tasks:
        return {"ok": False, "error": "write a prompt or add a task"}
    if len(prompt) > 40_000:
        return {"ok": False, "error": "prompt is too long"}

    base = _safe_id(name) or "custom"
    custom_id = base
    for attempt in range(1, 20):
        try:
            directory = definition_dir(custom_id)
        except ValueError:
            custom_id = f"{base}-{attempt}"
            continue
        if not (directory / "definition.json").exists():
            break
        custom_id = f"{base}-{attempt}"
    else:
        custom_id = f"{base}-{uuid.uuid4().hex[:6]}"
        directory = definition_dir(custom_id)

    now = round(time.time(), 3)
    definition = {
        "id": custom_id,
        "name": name,
        "prompt": prompt,
        "notes": notes,
        "title": title or name,
        "info": info,
        "tasks": tasks,
        "created_at": now,
        "updated_at": now,
    }
    _atomic_json(directory / "definition.json", definition)
    return {"ok": True, "definition": definition}


def update_definition(custom_id: str, raw: dict) -> dict:
    current = get_definition(custom_id)
    if not current:
        return {"ok": False, "error": "unknown custom test"}
    data = raw if isinstance(raw, dict) else {}
    name = str(data.get("name") if "name" in data else current.get("name") or "").strip()[:80]
    prompt = str(data.get("prompt") if "prompt" in data else current.get("prompt") or "").strip()
    notes = str(data.get("notes") if "notes" in data else current.get("notes") or "").strip()[:500]
    title = str(data.get("title") if "title" in data else current.get("title") or "").strip()[:120]
    info = str(data.get("info") if "info" in data else current.get("info") or "").strip()[:2000]
    tasks_raw = data.get("tasks") if "tasks" in data else current.get("tasks")
    try:
        tasks = _normalise_tasks(tasks_raw)
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    if not name:
        return {"ok": False, "error": "give the test a name"}
    if not prompt and not tasks:
        return {"ok": False, "error": "write a prompt or add a task"}
    if len(prompt) > 40_000:
        return {"ok": False, "error": "prompt is too long"}

    definition = {
        **current,
        "name": name,
        "prompt": prompt,
        "notes": notes,
        "title": title or name,
        "info": info,
        "tasks": tasks,
        "updated_at": round(time.time(), 3),
    }
    _atomic_json(definition_dir(custom_id) / "definition.json", definition)
    return {"ok": True, "definition": definition}


def delete_definition(custom_id: str) -> dict:
    try:
        directory = definition_dir(custom_id)
    except ValueError:
        return {"ok": False, "error": "unknown custom test"}
    if not (directory / "definition.json").exists():
        return {"ok": False, "error": "unknown custom test"}
    import shutil

    try:
        shutil.rmtree(directory)
    except OSError as exc:
        return {"ok": False, "error": str(exc)}
    return {"ok": True}


def _model_slug(provider: str, model: str) -> str:
    raw = f"{provider}-{model}"
    slug = re.sub(r"[^a-zA-Z0-9._-]+", "-", raw).strip("-._").lower()
    return (slug or "model")[:40]


def tasks_for_config(config: dict) -> list[Task]:
    """Build every selected saved task against every selected model."""
    prompt = str(config.get("prompt") or "").strip()
    name = str(config.get("custom_name") or config.get("custom_id") or "custom")
    task_defs = _normalise_tasks(config.get("custom_tasks"))
    if not task_defs and prompt:
        task_defs = [{"id": "prompt", "title": name, "info": "", "prompt": prompt}]
    models = config.get("models") if isinstance(config.get("models"), list) else []
    tasks: list[Task] = []
    for model_index, row in enumerate(models):
        if not isinstance(row, dict):
            continue
        provider = str(row.get("provider") or "").strip().lower()
        model = str(row.get("model") or "").strip()
        reasoning = str(row.get("reasoning") or "").strip().lower()
        if not provider or not model or not reasoning:
            continue
        slug = _model_slug(provider, model)
        for task_index, task_def in enumerate(task_defs):
            task_slug = _safe_id(task_def["id"]) or f"task-{task_index + 1}"
            tasks.append(Task(
                id=f"custom/{task_slug}-{slug}-{model_index + 1}",
                suite="custom",
                title=f"{task_def['title']} · {model}",
                brief=task_def["prompt"],
                files={"README.md": _EMPTY_README},
                checks=_CUSTOM_CHECKS,
                provider=provider,
                model=model,
                reasoning=reasoning,
                fast=bool(row.get("fast")),
            ))
    return tasks


def normalise_models(raw: Any) -> tuple[list[dict], str]:
    """Validate the multi-model picker. Returns (models, error)."""
    rows = raw if isinstance(raw, list) else []
    models: list[dict] = []
    seen: set[tuple[str, str, str, bool]] = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        provider = str(row.get("provider") or "").strip().lower()
        model = str(row.get("model") or "").strip()
        reasoning = str(row.get("reasoning") or "").strip().lower()
        fast = bool(row.get("fast"))
        if not provider or not model or not reasoning:
            continue
        key = (provider, model, reasoning, fast)
        if key in seen:
            continue
        seen.add(key)
        models.append({
            "provider": provider,
            "model": model,
            "reasoning": reasoning,
            "fast": fast,
        })
        if len(models) >= 8:
            break
    if not models:
        return [], "pick at least one model"
    return models, ""
