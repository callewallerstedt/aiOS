"""Isolated benchmarks against a real user project.

The real project is read exactly once when a campaign is created.  Every lane
works from that immutable snapshot, and the only route back to the real project
is a two-step preview/apply operation with drift detection and a rollback
checkpoint.  Benchmark runners never receive the real project path as their
workspace.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from . import BENCH_DIR


CAMPAIGNS_DIR = BENCH_DIR / "project_campaigns"
SNAPSHOT_SCHEMA = 1
MAX_FILES = 100_000
MAX_BYTES = 2 * 1024 * 1024 * 1024
PREVIEW_TTL_SECONDS = 15 * 60
CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0
_FALLBACK_IGNORED_DIRS = {
    ".git", ".hg", ".svn", ".idea", ".vs", ".vscode", "__pycache__",
    ".pytest_cache", ".mypy_cache", ".ruff_cache", ".tox", ".venv", "venv",
    "node_modules", "dist", "build", "coverage", ".next", ".turbo",
}


def _is_linklike(path: Path) -> bool:
    """Reject symlinks and Windows junction/reparse points at every boundary."""
    try:
        if path.is_symlink():
            return True
        is_junction = getattr(path, "is_junction", None)
        if callable(is_junction) and is_junction():
            return True
        attributes = int(getattr(path.lstat(), "st_file_attributes", 0) or 0)
        reparse = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400) or 0x400)
        return bool(attributes & reparse)
    except OSError:
        return False


def _atomic_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(f".{os.getpid()}-{uuid.uuid4().hex[:8]}.tmp")
    try:
        temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        temp.replace(path)
    finally:
        temp.unlink(missing_ok=True)


def _read_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def _safe_relative(raw: str) -> str:
    value = str(raw or "").replace("\\", "/").strip("/")
    parts = [part for part in value.split("/") if part]
    if not parts or any(part in {".", ".."} for part in parts):
        raise ValueError("unsafe project-relative path")
    if parts[0].casefold() == ".git":
        raise ValueError("git internals are not project source")
    return "/".join(parts)


def _within(path: Path, root: Path) -> bool:
    try:
        path.resolve(strict=False).relative_to(root.resolve(strict=False))
        return True
    except ValueError:
        return False


def _source_path(raw: Any) -> Path:
    text = str(raw or "").strip().strip('"')
    if not text:
        raise ValueError("choose a project folder")
    candidate = Path(text)
    if not candidate.is_absolute():
        raise ValueError("project folder must be an absolute path")
    if _is_linklike(candidate):
        raise ValueError("project folder cannot be a symlink, junction, or reparse point")
    try:
        resolved = candidate.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise ValueError(f"project folder is unavailable: {exc}") from exc
    if not resolved.is_dir():
        raise ValueError("project folder is not a directory")
    if _within(resolved, CAMPAIGNS_DIR):
        raise ValueError("benchmark storage cannot be used as the source project")
    return resolved


def _git_files(source: Path) -> list[str] | None:
    try:
        probe = subprocess.run(
            ["git", "-C", str(source), "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, timeout=20, creationflags=CREATE_NO_WINDOW,
        )
        if probe.returncode != 0 or Path(probe.stdout.strip()).resolve() != source:
            return None
        listed = subprocess.run(
            ["git", "-C", str(source), "ls-files", "-co", "--exclude-standard", "-z"],
            capture_output=True, timeout=60, creationflags=CREATE_NO_WINDOW,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if listed.returncode != 0:
        return None
    return [chunk.decode("utf-8", "surrogateescape") for chunk in listed.stdout.split(b"\0") if chunk]


def _walk_files(source: Path) -> Iterable[str]:
    for root, directories, files in os.walk(source, followlinks=False):
        base = Path(root)
        safe_directories = []
        for name in directories:
            path = base / name
            if _is_linklike(path):
                raise ValueError(f"project contains a symlink or junction: {path.relative_to(source)}")
            if name not in _FALLBACK_IGNORED_DIRS:
                safe_directories.append(name)
        directories[:] = safe_directories
        for name in files:
            path = base / name
            if _is_linklike(path):
                raise ValueError(f"project contains a symlink or reparse file: {path.relative_to(source)}")
            if path.is_file():
                yield path.relative_to(source).as_posix()


def _manifest(root: Path, paths: Iterable[str] | None = None) -> tuple[dict[str, dict], int]:
    rows: dict[str, dict] = {}
    total = 0
    candidates = paths if paths is not None else _walk_files(root)
    for raw in candidates:
        relative = _safe_relative(raw)
        target = root / Path(relative)
        if _is_linklike(target):
            raise ValueError(f"project contains a symlink or reparse point: {relative}")
        # Generated BENCH storage can live inside aiOS itself. It is never part
        # of a source snapshot, which permits safely benchmarking aiOS without
        # recursively copying earlier runs or campaigns.
        campaign_storage = CAMPAIGNS_DIR
        run_storage = BENCH_DIR / "runs"
        if ((not _within(root, campaign_storage) and _within(target, campaign_storage))
                or (not _within(root, run_storage) and _within(target, run_storage))):
            continue
        if not target.is_file() or not _within(target, root):
            continue
        size = target.stat().st_size
        total += size
        if len(rows) >= MAX_FILES:
            raise ValueError(f"project has more than {MAX_FILES:,} source files")
        if total > MAX_BYTES:
            raise ValueError("project source snapshot exceeds 2 GB")
        rows[relative] = {
            "sha256": _digest(target),
            "size": size,
            "mode": stat.S_IMODE(target.stat().st_mode),
        }
    return dict(sorted(rows.items())), total


def _manifest_hash(files: dict[str, dict]) -> str:
    encoded = json.dumps(
        {path: {"sha256": row["sha256"], "size": row["size"]} for path, row in sorted(files.items())},
        sort_keys=True, separators=(",", ":"), ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def campaign_dir(campaign_id: str) -> Path:
    value = str(campaign_id or "")
    if not re.fullmatch(r"project-[A-Za-z0-9._-]+", value):
        raise ValueError("bad project campaign id")
    return CAMPAIGNS_DIR / value


def create_snapshot(source_raw: Any, prompt: Any) -> dict:
    try:
        source = _source_path(source_raw)
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    brief = str(prompt or "").strip()
    if not brief:
        return {"ok": False, "error": "write what every harness should do"}
    if len(brief) > 32_000:
        return {"ok": False, "error": "project benchmark prompt is too long"}
    campaign_id = f"project-{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:8]}"
    directory = campaign_dir(campaign_id)
    snapshot = directory / "source"
    try:
        paths = _git_files(source)
        files, total = _manifest(source, paths)
        if not files:
            return {"ok": False, "error": "the project has no source files to benchmark"}
        snapshot.mkdir(parents=True, exist_ok=False)
        for relative, row in files.items():
            source_file = source / Path(relative)
            target = snapshot / Path(relative)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source_file, target)
            if _digest(target) != row["sha256"]:
                raise OSError(f"source changed while snapshotting: {relative}")
        digest = _manifest_hash(files)
        metadata = {
            "schema": SNAPSHOT_SCHEMA,
            "id": campaign_id,
            "created_at": round(time.time(), 3),
            "source_path": str(source),
            "source_name": source.name,
            "prompt": brief,
            "snapshot_hash": digest,
            "file_count": len(files),
            "total_bytes": total,
            "selection": "git-source" if paths is not None else "source-with-generated-folders-excluded",
            "files": files,
        }
        _atomic_json(directory / "campaign.json", metadata)
    except Exception as exc:
        shutil.rmtree(directory, ignore_errors=True)
        return {"ok": False, "error": f"could not snapshot project: {exc}"}
    return {"ok": True, "campaign": metadata}


def create_campaign(raw: dict, configurations: list[dict], label: str = "") -> dict:
    """Freeze one source tree, then start every selected harness from it."""
    data = raw if isinstance(raw, dict) else {}
    snapped = create_snapshot(data.get("source_path"), data.get("prompt"))
    if not snapped.get("ok"):
        return snapped
    campaign = snapped["campaign"]
    base = dict(data)
    base.update({
        "kind": "project",
        "project_campaign_id": campaign["id"],
        "project_snapshot_hash": campaign["snapshot_hash"],
        "project_source_path": campaign["source_path"],
        "project_source_name": campaign["source_name"],
        "prompt": campaign["prompt"],
        "concurrency": 1,
    })
    from . import runs

    result = runs.create_run_group(base, configurations, label=label)
    if not result.get("ok"):
        shutil.rmtree(campaign_dir(str(campaign["id"])), ignore_errors=True)
        return result
    campaign["group_id"] = str((result.get("group") or {}).get("id") or "")
    _atomic_json(campaign_dir(str(campaign["id"])) / "campaign.json", campaign)
    result["campaign"] = {
        key: campaign[key] for key in (
            "id", "source_path", "source_name", "snapshot_hash", "file_count",
            "total_bytes", "selection", "group_id",
        )
    }
    return result


def get_campaign(campaign_id: str) -> dict | None:
    try:
        value = _read_json(campaign_dir(campaign_id) / "campaign.json")
    except ValueError:
        return None
    return value if value.get("id") == campaign_id else None


def _validated_campaign(config: dict) -> tuple[dict | None, str]:
    campaign_id = str(config.get("project_campaign_id") or "").strip()
    campaign = get_campaign(campaign_id)
    if not campaign:
        return None, "project source snapshot is unavailable"
    if str(config.get("project_snapshot_hash") or "") != str(campaign.get("snapshot_hash") or ""):
        return None, "project source snapshot fingerprint changed"
    return campaign, ""


def normalise_config(data: dict) -> tuple[dict, str]:
    campaign, error = _validated_campaign(data)
    if error:
        return {}, error
    prompt = str(data.get("prompt") or campaign.get("prompt") or "").strip()
    if not prompt:
        return {}, "write what every harness should do"
    return {
        "project_campaign_id": campaign["id"],
        "project_snapshot_hash": campaign["snapshot_hash"],
        "project_source_path": campaign["source_path"],
        "project_source_name": campaign["source_name"],
        "prompt": prompt[:32_000],
    }, ""


@dataclass(frozen=True)
class ProjectTask:
    id: str
    title: str
    brief: str
    campaign_id: str
    snapshot_hash: str
    suite: str = "project"
    files: dict[str, str] = field(default_factory=dict)
    protected: tuple[str, ...] = field(default_factory=tuple)
    provider: str = ""
    model: str = ""
    reasoning: str = ""
    fast: bool = False
    source: str = ""
    provenance: dict[str, object] = field(default_factory=dict)

    @property
    def verifier(self) -> str:
        return ""

    def build(self, workspace: Path) -> None:
        campaign = get_campaign(self.campaign_id)
        if not campaign or campaign.get("snapshot_hash") != self.snapshot_hash:
            raise ValueError("project snapshot changed or disappeared")
        source = campaign_dir(self.campaign_id) / "source"
        expected_files = campaign.get("files") if isinstance(campaign.get("files"), dict) else {}
        frozen, _total = _manifest(source, expected_files.keys())
        if _manifest_hash(frozen) != self.snapshot_hash:
            raise ValueError("project snapshot bytes changed or disappeared")
        if workspace.exists():
            raise FileExistsError("benchmark workspace already exists")
        shutil.copytree(source, workspace, copy_function=shutil.copy2)
        copied, _total = _manifest(workspace, expected_files.keys())
        if _manifest_hash(copied) != self.snapshot_hash:
            shutil.rmtree(workspace, ignore_errors=True)
            raise OSError("isolated project copy failed byte-for-byte verification")
        for args in (
            ["init", "-q"], ["add", "-A"],
            ["-c", "user.email=bench@aios", "-c", "user.name=aiOS bench", "commit", "-qm", "project snapshot"],
        ):
            result = subprocess.run(
                ["git", "-C", str(workspace), *args], capture_output=True,
                timeout=120, creationflags=CREATE_NO_WINDOW,
            )
            if result.returncode != 0:
                detail = result.stderr.decode("utf-8", "replace")[-300:]
                raise OSError(detail or "could not initialise isolated project workspace")


def task_for_config(config: dict) -> ProjectTask:
    campaign, error = _validated_campaign(config)
    if error:
        raise ValueError(error)
    return ProjectTask(
        id="project/workspace",
        title=f"{campaign['source_name']} project task",
        brief=str(config.get("prompt") or campaign.get("prompt") or ""),
        campaign_id=str(campaign["id"]),
        snapshot_hash=str(campaign["snapshot_hash"]),
        files={"snapshot.manifest": str(campaign["snapshot_hash"])},
        source=str(campaign["source_path"]),
        provenance={
            "benchmark": "user-project-snapshot",
            "snapshot_hash": str(campaign["snapshot_hash"]),
            "file_count": int(campaign.get("file_count") or 0),
            "total_bytes": int(campaign.get("total_bytes") or 0),
        },
    )


def workspace_diff(campaign_id: str, workspace: Path) -> dict:
    campaign = get_campaign(campaign_id)
    if not campaign:
        raise ValueError("project campaign is unavailable")
    baseline = campaign.get("files") if isinstance(campaign.get("files"), dict) else {}
    current, total = _manifest(workspace, _git_files(workspace))
    changes = []
    for relative in sorted(set(baseline) | set(current)):
        before = baseline.get(relative)
        after = current.get(relative)
        if before and after and before.get("sha256") == after.get("sha256"):
            continue
        status = "added" if before is None else "deleted" if after is None else "modified"
        changes.append({
            "path": relative,
            "status": status,
            "before_sha256": str((before or {}).get("sha256") or ""),
            "after_sha256": str((after or {}).get("sha256") or ""),
            "before_size": int((before or {}).get("size") or 0),
            "after_size": int((after or {}).get("size") or 0),
        })
    signature = hashlib.sha256(json.dumps(changes, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
    return {
        "schema": SNAPSHOT_SCHEMA,
        "snapshot_hash": campaign["snapshot_hash"],
        "result_hash": _manifest_hash(current),
        "diff_hash": signature,
        "file_count": len(current),
        "total_bytes": total,
        "changed_files": len(changes),
        "added": sum(row["status"] == "added" for row in changes),
        "modified": sum(row["status"] == "modified" for row in changes),
        "deleted": sum(row["status"] == "deleted" for row in changes),
        "changes": changes,
    }


def _current_file(root: Path, relative: str) -> dict:
    target = root / Path(_safe_relative(relative))
    if not _within(target, root) or _is_linklike(target):
        return {"kind": "unsafe"}
    if not target.exists():
        return {"kind": "absent"}
    if not target.is_file():
        return {"kind": "non_file"}
    return {"kind": "file", "sha256": _digest(target), "size": target.stat().st_size}


def preview_apply(run_id: str, task_id: str = "") -> dict:
    from . import runs

    run = runs.get_run(run_id)
    if not run or str((run.get("config") or {}).get("kind")) != "project":
        return {"ok": False, "error": "choose a completed project benchmark lane"}
    if str(run.get("status")) in {"starting", "running", "stopping"}:
        return {"ok": False, "error": "wait for that benchmark lane to finish"}
    task = runs.task_of(run, task_id) if task_id else ((run.get("tasks") or [None])[0])
    if not task or not task.get("workspace"):
        return {"ok": False, "error": "that lane has no result workspace"}
    config = run.get("config") or {}
    campaign = get_campaign(str(config.get("project_campaign_id") or ""))
    if not campaign:
        return {"ok": False, "error": "source snapshot is unavailable"}
    try:
        root = _source_path(campaign.get("source_path"))
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    try:
        result = workspace_diff(str(campaign["id"]), Path(str(task["workspace"])))
    except Exception as exc:
        return {"ok": False, "error": f"could not inspect lane result: {exc}"}
    rows = []
    conflicts = 0
    already = 0
    for change in result["changes"]:
        current = _current_file(root, change["path"])
        current_hash = str(current.get("sha256") or "")
        before_hash = str(change.get("before_sha256") or "")
        desired_hash = str(change.get("after_sha256") or "")
        if ((desired_hash and current["kind"] == "file" and current_hash == desired_hash)
                or (not desired_hash and current["kind"] == "absent")):
            disposition = "already_applied"
            already += 1
        elif ((before_hash and current["kind"] == "file" and current_hash == before_hash)
              or (not before_hash and current["kind"] == "absent")):
            disposition = "ready"
        else:
            disposition = "conflict"
            conflicts += 1
        rows.append({**change, "real": current, "disposition": disposition})
    preview_id = f"preview-{uuid.uuid4().hex}"
    preview = {
        "schema": SNAPSHOT_SCHEMA,
        "id": preview_id,
        "created_at": round(time.time(), 3),
        "expires_at": round(time.time() + PREVIEW_TTL_SECONDS, 3),
        "campaign_id": campaign["id"],
        "run_id": str(run["id"]),
        "task_id": str(task.get("id") or ""),
        "workspace": str(task["workspace"]),
        "source_path": str(root),
        "snapshot_hash": campaign["snapshot_hash"],
        "diff_hash": result["diff_hash"],
        "changes": rows,
        "conflicts": conflicts,
        "already_applied": already,
        "ready": sum(row["disposition"] == "ready" for row in rows),
        "deletions": sum(row["status"] == "deleted" and row["disposition"] == "ready" for row in rows),
    }
    _atomic_json(campaign_dir(str(campaign["id"])) / "previews" / f"{preview_id}.json", preview)
    return {"ok": True, "preview": preview}


def _restore_checkpoint(root: Path, checkpoint: Path, manifest: dict, paths: list[str]) -> list[str]:
    errors = []
    states = manifest.get("states") if isinstance(manifest.get("states"), dict) else {}
    for relative in reversed(paths):
        target = root / Path(relative)
        state = states.get(relative) or {}
        try:
            if state.get("kind") == "file":
                source = checkpoint / "files" / Path(relative)
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, target)
            elif target.is_file() or target.is_symlink():
                target.unlink()
        except OSError as exc:
            errors.append(f"{relative}: {exc}")
    return errors


def confirm_apply(preview_id: str, *, allow_deletions: bool = False) -> dict:
    value = str(preview_id or "")
    if not re.fullmatch(r"preview-[0-9a-f]{32}", value):
        return {"ok": False, "error": "bad or expired apply preview"}
    matches = list(CAMPAIGNS_DIR.glob(f"project-*/previews/{value}.json"))
    if len(matches) != 1:
        return {"ok": False, "error": "bad or expired apply preview"}
    preview_path = matches[0]
    preview = _read_json(preview_path)
    if not preview or float(preview.get("expires_at") or 0) < time.time():
        preview_path.unlink(missing_ok=True)
        return {"ok": False, "error": "apply preview expired; preview the result again"}
    campaign = get_campaign(str(preview.get("campaign_id") or ""))
    if not campaign or campaign.get("snapshot_hash") != preview.get("snapshot_hash"):
        return {"ok": False, "error": "source snapshot changed; refusing to apply"}
    try:
        root = _source_path(preview.get("source_path"))
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    try:
        result = workspace_diff(str(campaign["id"]), Path(str(preview.get("workspace") or "")))
    except Exception as exc:
        return {"ok": False, "error": f"lane result changed after preview: {exc}"}
    if result.get("diff_hash") != preview.get("diff_hash"):
        return {"ok": False, "error": "lane result changed after preview; preview it again"}

    ready = []
    conflicts = []
    preview_rows = {str(row.get("path")): row for row in (preview.get("changes") or [])}
    for change in result["changes"]:
        expected = preview_rows.get(change["path"]) or {}
        current = _current_file(root, change["path"])
        if current != expected.get("real"):
            conflicts.append(change["path"])
        elif expected.get("disposition") == "ready":
            ready.append(change)
        elif expected.get("disposition") != "already_applied":
            conflicts.append(change["path"])
    if conflicts:
        return {"ok": False, "error": "real project changed after the snapshot", "conflicts": conflicts}
    deletions = [row["path"] for row in ready if row["status"] == "deleted"]
    if deletions and not allow_deletions:
        return {"ok": False, "error": "confirm deletions explicitly", "deletions": deletions}
    if not ready:
        return {"ok": False, "error": "nothing remains to copy"}

    checkpoint_id = f"checkpoint-{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:8]}"
    checkpoint = campaign_dir(str(campaign["id"])) / "backups" / checkpoint_id
    states = {}
    try:
        for change in ready:
            relative = change["path"]
            state = _current_file(root, relative)
            states[relative] = state
            if state.get("kind") == "file":
                target = checkpoint / "files" / Path(relative)
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(root / Path(relative), target)
        checkpoint_manifest = {
            "schema": SNAPSHOT_SCHEMA, "id": checkpoint_id,
            "created_at": round(time.time(), 3), "source_path": str(root),
            "campaign_id": campaign["id"], "run_id": preview.get("run_id"),
            "preview_id": value, "states": states,
        }
        _atomic_json(checkpoint / "checkpoint.json", checkpoint_manifest)
    except OSError as exc:
        shutil.rmtree(checkpoint, ignore_errors=True)
        return {"ok": False, "error": f"could not create rollback checkpoint: {exc}"}

    applied: list[str] = []
    lane = Path(str(preview["workspace"]))
    try:
        for change in ready:
            relative = change["path"]
            target = root / Path(relative)
            if change["status"] == "deleted":
                target.unlink()
            else:
                source = lane / Path(relative)
                target.parent.mkdir(parents=True, exist_ok=True)
                temp = target.with_name(f".{target.name}.aios-{uuid.uuid4().hex[:8]}.tmp")
                try:
                    shutil.copy2(source, temp)
                    temp.replace(target)
                finally:
                    temp.unlink(missing_ok=True)
            applied.append(relative)
    except OSError as exc:
        rollback_errors = _restore_checkpoint(root, checkpoint, checkpoint_manifest, applied)
        return {
            "ok": False,
            "error": f"apply failed and was rolled back: {exc}",
            "rollback_errors": rollback_errors,
            "checkpoint": str(checkpoint),
        }

    preview_path.unlink(missing_ok=True)
    receipt = {
        "schema": SNAPSHOT_SCHEMA, "applied_at": round(time.time(), 3),
        "checkpoint": str(checkpoint), "applied": applied,
        "skipped_already_applied": int(preview.get("already_applied") or 0),
    }
    _atomic_json(checkpoint / "receipt.json", receipt)
    return {"ok": True, **receipt}
