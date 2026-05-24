"""aiOS auto-updater.

Pulls the latest source from GitHub (git if the repo is a clone, otherwise via
the GitHub tarball API), reinstalls Python requirements, and relaunches the
helper. All work happens off the Tk thread — callers pass a `progress` callback
that receives short status strings.

Public API:
    get_current_sha()        -> short SHA or "" if unknown
    check_for_update()       -> dict with current/latest SHAs and a behind flag
    perform_update(progress) -> dict with ok/message
    restart_aios()           -> never returns; launches a new helper and exits
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
            return out.strip() or GITHUB_BRANCH
    return GITHUB_BRANCH


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
    progress(f"git fetch origin (branch {src['branch']})…")
    rc, out = _run(["git", "fetch", "--all", "--prune"], timeout=180)
    if rc != 0:
        return False, f"git fetch failed: {out}"
    progress(f"git reset --hard origin/{src['branch']}")
    rc, out = _run(["git", "reset", "--hard", f"origin/{src['branch']}"], timeout=120)
    if rc != 0:
        return False, f"git reset failed: {out}"
    return True, "git updated"


def _tarball_update(src: dict, progress) -> tuple[bool, str]:
    progress(f"downloading tarball ({src['owner']}/{src['repo']}@{src['branch']})…")
    blob = _http_bytes(_tarball_url(src), timeout=180, progress=progress,
                       token=src.get("token", ""))
    if not blob:
        return False, "tarball download failed"
    progress("extracting…")
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
                # Skip preserved trees.
                head = rel.split("/", 1)[0]
                if head in preserve_norm or rel in preserve_norm:
                    continue
                target = BASE_DIR / rel
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
                tmp = target.with_suffix(target.suffix + ".aios-new")
                tmp.write_bytes(data)
                tmp.replace(target)
                written += 1
            progress(f"extracted {written} file(s)")
    except Exception as exc:
        return False, f"extract failed: {exc}"
    # Persist a SHA marker so future checks know what we last installed.
    latest, _err = _remote_latest(src)
    if latest and latest.get("sha"):
        try:
            (BASE_DIR / ".aios_sha").write_text(latest["sha"], encoding="utf-8")
        except OSError:
            pass
    return True, "tarball applied"


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
    """Pull latest, install deps. Returns dict with ok/message/restart_needed."""
    progress = progress or (lambda _msg: None)
    src = load_source(owner, repo, branch)
    via_git = _is_git_repo()
    progress(f"updating via {'git' if via_git else 'tarball'} from "
             f"{src['owner']}/{src['repo']}@{src['branch']}…")
    if via_git:
        ok, msg = _git_update(src, progress)
    else:
        ok, msg = _tarball_update(src, progress)
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
        "current": get_current_sha(),
    }


def restart_aios(extra_args: list[str] | None = None) -> None:
    """Spawn a fresh helper process and exit the current one.

    Safe to call from the Tk thread — we launch detached then os._exit.
    """
    extra_args = extra_args or []
    helper_path = BASE_DIR / "helper_overlay.py"
    if not helper_path.exists():
        return
    cmd = [sys.executable, str(helper_path), *extra_args]
    kwargs = {"cwd": str(BASE_DIR), "close_fds": True}
    if os.name == "nt":
        DETACHED_PROCESS = 0x00000008
        CREATE_NEW_PROCESS_GROUP = 0x00000200
        kwargs["creationflags"] = DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    try:
        subprocess.Popen(cmd, **kwargs)
    except Exception:
        return
    # Give the new process a beat to claim resources, then bail.
    threading.Timer(0.6, lambda: os._exit(0)).start()


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
