"""aiOS auto-updater.

Pulls the latest source from GitHub (git if the repo is a clone, otherwise via
the GitHub tarball API), reinstalls Python requirements, and relaunches the
helper. All work happens off the Tk thread — callers pass a `progress` callback
that receives short status strings.

Public API:
    get_current_sha()        -> short SHA or "" if unknown
    check_for_update()       -> dict with current/latest SHAs and a behind flag
    perform_update(progress) -> dict with ok/message
    restart_aios()           -> never returns; relaunches the complete stack
    spawn_relaunch(pid, args)-> detach the shared full-stack coordinator
"""
from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
HELPER_CONFIG_PATH = BASE_DIR / "helper_config.json"
DEFAULT_OWNER = "callewallerstedt"
DEFAULT_REPO = "aiOS"
DEFAULT_BRANCH = "main"
REQUIREMENTS_PATH = BASE_DIR / "requirements.txt"
USER_AGENT = "aiOS-updater"

# Where the tarball is unpacked before being applied. We can't overwrite
# the running helper's own files (Windows holds them open), so we stage
# everything here and let a separate apply process swap them in after the
# helper exits.
STAGING_DIR = BASE_DIR / ".aios_update_staging"
PENDING_SHA_FILE = BASE_DIR / ".aios_update_pending_sha"


def load_source(owner: str | None = None, repo: str | None = None,
                branch: str | None = None) -> dict:
    """Resolve owner/repo/branch from explicit args > helper_config > defaults."""
    cfg: dict = {}
    if HELPER_CONFIG_PATH.exists():
        try:
            with HELPER_CONFIG_PATH.open("r", encoding="utf-8") as fh:
                raw = json.load(fh) or {}
            cfg = raw.get("update_source") or {}
        except (OSError, json.JSONDecodeError):
            cfg = {}
    token = (cfg.get("token") or os.environ.get("AIOS_UPDATE_TOKEN")
             or os.environ.get("GITHUB_TOKEN") or "").strip()
    return {
        "owner": (owner or cfg.get("owner") or DEFAULT_OWNER).strip(),
        "repo":  (repo  or cfg.get("repo")  or DEFAULT_REPO).strip(),
        "branch": (branch or cfg.get("branch") or DEFAULT_BRANCH).strip(),
        "token": token,
    }


def save_source(owner: str, repo: str, branch: str, token: str | None = None) -> bool:
    """Persist update_source into helper_config.json. Returns True on success."""
    owner = (owner or "").strip()
    repo = (repo or "").strip()
    branch = (branch or "").strip() or DEFAULT_BRANCH
    if not owner or not repo:
        return False
    data: dict = {}
    if HELPER_CONFIG_PATH.exists():
        try:
            with HELPER_CONFIG_PATH.open("r", encoding="utf-8") as fh:
                data = json.load(fh) or {}
        except (OSError, json.JSONDecodeError):
            data = {}
    payload = {"owner": owner, "repo": repo, "branch": branch}
    # Preserve existing token unless explicitly replaced.
    existing = (data.get("update_source") or {}).get("token", "")
    if token is None:
        if existing:
            payload["token"] = existing
    else:
        token = token.strip()
        if token:
            payload["token"] = token
    data["update_source"] = payload
    try:
        tmp = HELPER_CONFIG_PATH.with_suffix(".json.tmp")
        with tmp.open("w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, ensure_ascii=False)
        tmp.replace(HELPER_CONFIG_PATH)
        return True
    except OSError:
        return False


def _api_url(src: dict) -> str:
    return f"https://api.github.com/repos/{src['owner']}/{src['repo']}"


def _tarball_url(src: dict) -> str:
    return (f"https://codeload.github.com/{src['owner']}/{src['repo']}"
            f"/tar.gz/refs/heads/{src['branch']}")

# Files / dirs we never overwrite from upstream — user data + secrets + caches.
PRESERVE = {
    ".git",
    ".venv",
    "venv",
    "helper_config.json",
    "helper_config.local.json",
    "phone_operator_events",
    "debug_runs",
    "agent_clicker/.env",
    "agent_clicker/.env.local",
    "agent_clicker/local.json",
    "startup-splash.log",
}


def _run(cmd: list[str], cwd: Path | None = None, timeout: int = 120) -> tuple[int, str]:
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(cwd or BASE_DIR),
            capture_output=True,
            text=True,
            timeout=timeout,
            creationflags=(subprocess.CREATE_NO_WINDOW if os.name == "nt" and hasattr(subprocess, "CREATE_NO_WINDOW") else 0),
        )
        out = (proc.stdout or "") + (proc.stderr or "")
        return proc.returncode, out.strip()
    except FileNotFoundError as exc:
        return 127, f"not found: {exc}"
    except subprocess.TimeoutExpired:
        return 124, "timed out"


def _is_git_repo() -> bool:
    return (BASE_DIR / ".git").exists()


def _http_json(url: str, timeout: float = 8.0, token: str = "") -> tuple[dict | None, str]:
    """Returns (data, error). On success error is ''. On failure data is None."""
    headers = {"User-Agent": USER_AGENT, "Accept": "application/vnd.github+json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", "replace")
        try:
            return json.loads(raw), ""
        except json.JSONDecodeError as exc:
            return None, f"bad JSON from {url}: {exc}"
    except urllib.error.HTTPError as exc:
        body = ""
        try:
            body = exc.read().decode("utf-8", "replace")[:200]
        except Exception:
            pass
        if exc.code == 404:
            return None, "HTTP 404 from GitHub — repo or branch not found"
        if exc.code == 403:
            return None, f"HTTP 403 from GitHub — rate limited: {body or exc.reason}"
        return None, f"HTTP {exc.code} from GitHub: {body or exc.reason}"
    except urllib.error.URLError as exc:
        return None, f"network error reaching {url}: {exc.reason}"
    except Exception as exc:
        return None, f"unexpected error reaching {url}: {exc}"


def _http_bytes(url: str, timeout: float = 60.0, progress=None, token: str = "") -> bytes | None:
    headers = {"User-Agent": USER_AGENT}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            total = int(resp.headers.get("Content-Length") or 0)
            chunks = []
            seen = 0
            last_report = 0.0
            while True:
                buf = resp.read(64 * 1024)
                if not buf:
                    break
                chunks.append(buf)
                seen += len(buf)
                now = time.time()
                if progress and now - last_report > 0.25:
                    last_report = now
                    if total:
                        pct = int(100 * seen / total)
                        progress(f"download {pct}% ({seen // 1024} / {total // 1024} KiB)")
                    else:
                        progress(f"download {seen // 1024} KiB")
            return b"".join(chunks)
    except Exception:
        return None


def get_current_sha() -> str:
    """Return the short commit SHA of the installed copy, or '' if unknown."""
    if _is_git_repo():
        rc, out = _run(["git", "rev-parse", "--short", "HEAD"])
        if rc == 0:
            return out.strip()
    sha_file = BASE_DIR / ".aios_sha"
    if sha_file.exists():
        try:
            return sha_file.read_text("utf-8").strip()[:12]
        except OSError:
            pass
    return ""


def get_current_branch() -> str:
    if _is_git_repo():
        rc, out = _run(["git", "rev-parse", "--abbrev-ref", "HEAD"])
        if rc == 0:
            return out.strip() or DEFAULT_BRANCH
    return DEFAULT_BRANCH


def _remote_latest(src: dict) -> tuple[dict | None, str]:
    url = f"{_api_url(src)}/commits/{src['branch']}"
    data, err = _http_json(url, token=src.get("token", ""))
    if not data:
        return None, err or "no response"
    sha = (data.get("sha") or "")[:12]
    commit = data.get("commit") or {}
    msg = ((commit.get("message") or "").splitlines() or [""])[0]
    author = (commit.get("author") or {}).get("name") or ""
    date = (commit.get("author") or {}).get("date") or ""
    return {"sha": sha, "message": msg, "author": author, "date": date}, ""


def check_for_update(owner: str | None = None, repo: str | None = None,
                     branch: str | None = None) -> dict:
    """Compare installed SHA with the latest commit on the configured branch."""
    src = load_source(owner, repo, branch)
    current = get_current_sha()
    latest, err = _remote_latest(src)
    if not latest:
        return {"ok": False, "error": err or "could not reach GitHub",
                "current": current, "latest": "", "behind": False,
                "source": {k: v for k, v in src.items() if k != "token"},
                "token_present": bool(src.get("token"))}
    latest_sha = latest["sha"]
    # Compare on the shorter prefix so 7-char vs 12-char SHAs aren't false positives.
    n = min(len(current), len(latest_sha)) if current and latest_sha else 0
    same = bool(n) and current[:n].lower() == latest_sha[:n].lower()
    behind = bool(current) and not same
    unknown = not current
    return {
        "ok": True,
        "current": current,
        "latest": latest_sha,
        "behind": behind or unknown,
        "message": latest["message"],
        "author": latest["author"],
        "date": latest["date"],
        "branch": src["branch"],
        "via_git": _is_git_repo(),
        "source": {k: v for k, v in src.items() if k != "token"},
        "token_present": bool(src.get("token")),
    }


def _git_update(src: dict, progress) -> tuple[bool, str]:
    rc, out = _run(["git", "status", "--porcelain", "--untracked-files=no"])
    if rc != 0:
        return False, f"could not inspect worktree: {out}"
    if out.strip():
        return False, "update paused: tracked local changes are present"
    branch = get_current_branch()
    if branch != src["branch"]:
        return False, f"update paused: current branch is {branch}, expected {src['branch']}"
    progress(f"git fetch origin (branch {src['branch']})…")
    rc, out = _run(["git", "fetch", "origin", src["branch"], "--prune"], timeout=180)
    if rc != 0:
        return False, f"git fetch failed: {out}"
    rc, out = _run(["git", "merge-base", "--is-ancestor", "HEAD", f"origin/{src['branch']}"])
    if rc != 0:
        return False, "update paused: local history has diverged from origin"
    progress(f"fast-forwarding to origin/{src['branch']}")
    rc, out = _run(["git", "merge", "--ff-only", f"origin/{src['branch']}"], timeout=120)
    if rc != 0:
        return False, f"fast-forward failed: {out}"
    return True, "git updated"


def _tarball_update(src: dict, progress) -> tuple[bool, str]:
    """Download + extract to a STAGING dir. The actual file swap is deferred
    to `_spawn_apply_script()` which runs after the helper exits, because
    Windows won't let us overwrite files held open by the running process."""
    progress(f"downloading tarball ({src['owner']}/{src['repo']}@{src['branch']})…")
    blob = _http_bytes(_tarball_url(src), timeout=180, progress=progress,
                       token=src.get("token", ""))
    if not blob:
        return False, "tarball download failed"
    progress("clearing staging dir…")
    if STAGING_DIR.exists():
        try:
            shutil.rmtree(STAGING_DIR, ignore_errors=True)
        except Exception:
            pass
    STAGING_DIR.mkdir(parents=True, exist_ok=True)
    progress("extracting to staging…")
    try:
        with tarfile.open(fileobj=io.BytesIO(blob), mode="r:gz") as tf:
            members = tf.getmembers()
            if not members:
                return False, "empty tarball"
            # Tarball has a single top-level "{owner}-{repo}-{sha}/" directory.
            top = members[0].name.split("/", 1)[0] + "/"
            preserve_norm = {p.replace("\\", "/").rstrip("/") for p in PRESERVE}
            written = 0
            for m in members:
                if not m.name.startswith(top):
                    continue
                rel = m.name[len(top):].replace("\\", "/")
                if not rel:
                    continue
                # Skip preserved trees so we don't even stage them.
                head = rel.split("/", 1)[0]
                if head in preserve_norm or rel in preserve_norm:
                    continue
                target = STAGING_DIR / rel
                if m.isdir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                if not m.isreg():
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                fh = tf.extractfile(m)
                if fh is None:
                    continue
                data = fh.read()
                target.write_bytes(data)
                written += 1
            progress(f"staged {written} file(s) — will apply on restart")
    except Exception as exc:
        return False, f"extract failed: {exc}"
    # Remember which SHA was downloaded so we can stamp .aios_sha after apply.
    latest, _err = _remote_latest(src)
    if latest and latest.get("sha"):
        try:
            PENDING_SHA_FILE.write_text(latest["sha"], encoding="utf-8")
        except OSError:
            pass
    return True, "tarball staged"


def _install_deps(progress) -> tuple[bool, str]:
    if not REQUIREMENTS_PATH.exists():
        return True, "no requirements.txt"
    progress("pip install -r requirements.txt …")
    rc, out = _run([sys.executable, "-m", "pip", "install", "--disable-pip-version-check",
                    "--quiet", "-r", str(REQUIREMENTS_PATH)], timeout=600)
    if rc != 0:
        # Don't fail the whole update over deps — surface it but continue.
        return False, f"pip install warning: {out[-400:]}"
    return True, "deps ok"


def perform_update(progress=None, owner: str | None = None,
                   repo: str | None = None, branch: str | None = None) -> dict:
    """For git installs: pull + install deps in-place.
    For tarball installs: stage files for the apply-after-restart flow;
    deps will be installed by the apply script once files are swapped."""
    progress = progress or (lambda _msg: None)
    src = load_source(owner, repo, branch)
    via_git = _is_git_repo()
    progress(f"updating via {'git' if via_git else 'tarball'} from "
             f"{src['owner']}/{src['repo']}@{src['branch']}…")
    if via_git:
        ok, msg = _git_update(src, progress)
        if not ok:
            return {"ok": False, "message": msg, "restart_needed": False}
        deps_ok, deps_msg = _install_deps(progress)
        progress(deps_msg)
        return {
            "ok": True,
            "message": "updated; ready to restart",
            "deps_message": deps_msg,
            "deps_ok": deps_ok,
            "restart_needed": True,
            "via": "git",
            "current": get_current_sha(),
        }
    # Tarball: stage now, apply on restart.
    ok, msg = _tarball_update(src, progress)
    if not ok:
        return {"ok": False, "message": msg, "restart_needed": False}
    return {
        "ok": True,
        "message": "files staged · close aiOS to apply the update",
        "restart_needed": True,
        "via": "tarball",
        "staged": True,
    }


# ---------------------------------------------------------------------------
# Apply-after-restart helper. This script is dropped into TEMP, run detached,
# waits for the running helper to release its files, swaps the staged tree in,
# reinstalls deps, and then relaunches the complete stack.
# ---------------------------------------------------------------------------

APPLY_SCRIPT_TEMPLATE = r'''#!/usr/bin/env python3
"""aiOS update applier — runs from TEMP after the helper exits."""
import os
import sys
import shutil
import subprocess
import time
import traceback
from pathlib import Path

BASE_DIR = Path(r"__BASE_DIR__")
STAGING_DIR = BASE_DIR / ".aios_update_staging"
LOG_PATH = BASE_DIR / "update-apply.log"
PENDING_SHA_FILE = BASE_DIR / ".aios_update_pending_sha"
SHA_FILE = BASE_DIR / ".aios_sha"
LAUNCHER = BASE_DIR / "launch_aios.py"
REQUIREMENTS = BASE_DIR / "requirements.txt"
PARENT_PID = __PARENT_PID__


def log(msg):
    try:
        with LOG_PATH.open("a", encoding="utf-8") as fh:
            fh.write(time.strftime("[%Y-%m-%d %H:%M:%S] ") + str(msg) + "\n")
    except Exception:
        pass


def wait_for_parent():
    if PARENT_PID <= 0:
        time.sleep(2.0)
        return
    deadline = time.time() + 20.0
    while time.time() < deadline:
        try:
            os.kill(PARENT_PID, 0)
        except OSError:
            return  # parent gone
        time.sleep(0.25)
    log(f"timed out waiting for parent pid {PARENT_PID}")


def copy_with_retry(src_path, dst_path, attempts=20):
    last_exc = None
    for i in range(attempts):
        try:
            dst_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src_path, dst_path)
            return
        except PermissionError as exc:
            last_exc = exc
            time.sleep(0.5)
        except OSError as exc:
            last_exc = exc
            time.sleep(0.5)
    raise last_exc if last_exc else RuntimeError("copy failed")


def apply_staging():
    if not STAGING_DIR.exists():
        log("no staging dir, nothing to apply")
        return False
    # Sweep any leftover *.aios-new temp files from earlier broken update
    # attempts — they confuse the next install.
    for stale in BASE_DIR.rglob("*.aios-new"):
        try:
            stale.unlink()
        except OSError:
            pass
    copied = 0
    failed = []
    for src_path in STAGING_DIR.rglob("*"):
        if src_path.is_dir():
            continue
        rel = src_path.relative_to(STAGING_DIR)
        dst = BASE_DIR / rel
        try:
            copy_with_retry(src_path, dst)
            copied += 1
        except Exception as exc:
            failed.append(f"{rel}: {exc}")
            log(f"copy failed: {rel}: {exc}")
    log(f"copied {copied} file(s); {len(failed)} failures")
    if failed:
        # Surface a brief summary in a sentinel file so the next launch can
        # show it.
        try:
            (BASE_DIR / "update-failures.log").write_text(
                "\n".join(failed), encoding="utf-8")
        except OSError:
            pass
    # Promote pending SHA → current SHA marker.
    if PENDING_SHA_FILE.exists():
        try:
            SHA_FILE.write_text(PENDING_SHA_FILE.read_text(encoding="utf-8").strip(),
                                encoding="utf-8")
            PENDING_SHA_FILE.unlink()
        except OSError:
            pass
    # Clean up staging.
    shutil.rmtree(STAGING_DIR, ignore_errors=True)
    return not failed


def install_deps():
    if not REQUIREMENTS.exists():
        return
    log("pip install -r requirements.txt …")
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "pip", "install", "--disable-pip-version-check",
             "--quiet", "-r", str(REQUIREMENTS)],
            capture_output=True, text=True, timeout=600,
        )
        log(f"pip rc={proc.returncode}")
        if proc.returncode != 0:
            log(proc.stdout[-2000:] + "\n" + proc.stderr[-2000:])
    except Exception as exc:
        log(f"pip install error: {exc}")


def relaunch_helper():
    if not LAUNCHER.exists():
        log("launch_aios.py missing — cannot relaunch")
        return
    log("relaunching complete aiOS stack")
    kwargs = {"cwd": str(BASE_DIR), "close_fds": True}
    if os.name == "nt":
        DETACHED = 0x00000008
        NEW_GROUP = 0x00000200
        kwargs["creationflags"] = DETACHED | NEW_GROUP
    try:
        subprocess.Popen([sys.executable, str(LAUNCHER)], **kwargs)
    except Exception as exc:
        log(f"relaunch failed: {exc}")


def main():
    log("== apply start ==")
    try:
        wait_for_parent()
        ok = apply_staging()
        if ok:
            install_deps()
        relaunch_helper()
        log("== apply done ==")
    except Exception:
        log("apply crashed:\n" + traceback.format_exc())


if __name__ == "__main__":
    main()
'''


def _write_apply_script(parent_pid: int) -> Path:
    import tempfile
    body = (APPLY_SCRIPT_TEMPLATE
            .replace("__BASE_DIR__", str(BASE_DIR))
            .replace("__PARENT_PID__", str(int(parent_pid))))
    fd, path = tempfile.mkstemp(prefix="aios_apply_", suffix=".py")
    os.close(fd)
    Path(path).write_text(body, encoding="utf-8")
    return Path(path)


def spawn_staged_apply(parent_pid: int | None = None) -> bool:
    """Launch the detached tarball applier without exposing update secrets."""
    try:
        script = _write_apply_script(os.getpid() if parent_pid is None else parent_pid)
        kwargs = {"close_fds": True}
        if os.name == "nt":
            DETACHED_PROCESS = 0x00000008
            CREATE_NEW_PROCESS_GROUP = 0x00000200
            kwargs["creationflags"] = DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
        subprocess.Popen([sys.executable, str(script)], **kwargs)
        return True
    except Exception:
        return False


def _spawn_apply_script() -> bool:
    return spawn_staged_apply()


def spawn_relaunch(parent_pid: int | None = None, extra_args: list[str] | None = None) -> bool:
    """Detach the shared coordinator so it restarts all aiOS services."""
    relaunch = BASE_DIR / "aios_relaunch.py"
    shell = BASE_DIR / "aios_shell.py"
    if not relaunch.exists() or not shell.exists():
        return False
    pid = int(parent_pid if parent_pid is not None else os.getpid())
    args = [sys.executable, str(relaunch), str(pid), *(extra_args or ["--fast-start"])]
    kwargs: dict = {"cwd": str(BASE_DIR), "close_fds": True}
    if os.name == "nt":
        DETACHED_PROCESS = 0x00000008
        CREATE_NEW_PROCESS_GROUP = 0x00000200
        CREATE_NO_WINDOW = 0x08000000
        kwargs["creationflags"] = DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW
        kwargs["stdin"] = subprocess.DEVNULL
        kwargs["stdout"] = subprocess.DEVNULL
        kwargs["stderr"] = subprocess.DEVNULL
    try:
        subprocess.Popen(args, **kwargs)
        return True
    except Exception:
        return False


def restart_aios(extra_args: list[str] | None = None) -> None:
    """Restart the complete aiOS desktop stack.

    If there's a staged update in `.aios_update_staging`, spawn the apply
    script (which waits for us to exit, swaps files, installs dependencies,
    and invokes the full launcher). Otherwise the coordinator waits for this
    process to exit, tears down every managed tree, and fast-starts the stack.

    Safe to call from the Tk thread — we launch detached then os._exit.
    """
    extra_args = list(extra_args or [])
    if "--fast-start" not in extra_args:
        extra_args.append("--fast-start")
    shell_path = BASE_DIR / "aios_shell.py"
    if not shell_path.exists():
        return
    # If files were staged, hand off to the apply script and exit.
    if STAGING_DIR.exists():
        if _spawn_apply_script():
            threading.Timer(0.5, lambda: os._exit(0)).start()
            return
        # If we couldn't spawn the apply script for some reason, fall through
        # to a plain relaunch and surface a notice via the failures log.
        try:
            (BASE_DIR / "update-failures.log").write_text(
                "could not spawn apply script — staged files remain in "
                ".aios_update_staging\n", encoding="utf-8")
        except OSError:
            pass
    if not spawn_relaunch(os.getpid(), extra_args):
        # Last resort: direct spawn (may still race the mutex briefly).
        cmd = [sys.executable, str(shell_path)]
        kwargs = {"cwd": str(BASE_DIR), "close_fds": True}
        if os.name == "nt":
            DETACHED_PROCESS = 0x00000008
            CREATE_NEW_PROCESS_GROUP = 0x00000200
            kwargs["creationflags"] = DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
        try:
            subprocess.Popen(cmd, **kwargs)
        except Exception:
            return
    threading.Timer(0.2, lambda: os._exit(0)).start()


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["check", "update", "restart"])
    args = parser.parse_args()
    if args.action == "check":
        print(json.dumps(check_for_update(), indent=2))
    elif args.action == "update":
        result = perform_update(progress=lambda m: print(f"[update] {m}"))
        print(json.dumps(result, indent=2))
    elif args.action == "restart":
        restart_aios()
