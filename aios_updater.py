"""aiOS auto-updater.

Pulls the latest source from GitHub (git if the repo is a clone, otherwise via
the GitHub tarball API), reinstalls Python requirements, and relaunches the
helper. All work happens off the Tk thread — callers pass a `progress` callback
that receives short status strings.

Public API:
    get_current_sha()        -> short SHA or "" if unknown
    check_for_update()       -> dict with current/latest SHAs and a behind flag
    update_now(progress)     -> check + pull + install in one call
    perform_update(progress) -> dict with ok/message
    restart_aios()           -> never returns; launches a new helper and exits

CLI:
    python aios_updater.py auto [--force]   check, install, relaunch
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
import urllib.parse
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


def _tarball_urls(src: dict) -> list[str]:
    """Candidate tarball URLs, best first.

    codeload works for public repos without auth; the API endpoint is the one
    that honours a Bearer token, so private repos need it.
    """
    urls = [f"https://codeload.github.com/{src['owner']}/{src['repo']}"
            f"/tar.gz/refs/heads/{src['branch']}"]
    api = f"{_api_url(src)}/tarball/{src['branch']}"
    if src.get("token"):
        urls.insert(0, api)
    else:
        urls.append(api)
    return urls


def _git_remote_url(src: dict, *, with_token: bool = False) -> str:
    """HTTPS clone URL for the configured source.

    The token variant is only ever passed to git as a command-line argument so
    it never gets persisted into .git/config.
    """
    host = f"github.com/{src['owner']}/{src['repo']}.git"
    token = (src.get("token") or "").strip()
    if with_token and token:
        return f"https://x-access-token:{token}@{host}"
    return f"https://{host}"

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


class _StripAuthRedirect(urllib.request.HTTPRedirectHandler):
    """Drop the Authorization header when a redirect crosses hosts.

    api.github.com/…/tarball redirects to a pre-signed codeload URL that
    rejects the inherited Bearer header with a 400.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new is not None and urllib.parse.urlsplit(newurl).hostname != req.host:
            new.headers = {k: v for k, v in new.headers.items()
                           if k.lower() != "authorization"}
        return new


def _http_bytes(url: str, timeout: float = 60.0, progress=None, token: str = "") -> bytes | None:
    headers = {"User-Agent": USER_AGENT}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, headers=headers)
    opener = urllib.request.build_opener(_StripAuthRedirect)
    try:
        with opener.open(req, timeout=timeout) as resp:
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


def _marker_sha() -> str:
    """SHA the updater last installed from a tarball, or ''."""
    sha_file = BASE_DIR / ".aios_sha"
    if sha_file.exists():
        try:
            return sha_file.read_text("utf-8").strip()[:12]
        except OSError:
            pass
    return ""


def get_current_sha() -> str:
    """Return the short commit SHA of the installed copy, or '' if unknown."""
    # `.aios_sha` inside a git clone means the last install came from the
    # tarball fallback, which overwrites the files without moving HEAD — so it
    # describes what's on disk and HEAD does not. A successful git update
    # clears the marker and hands authority back to git.
    marker = _marker_sha()
    if marker:
        return marker
    if _is_git_repo():
        rc, out = _run(["git", "rev-parse", "--short", "HEAD"])
        if rc == 0:
            return out.strip()
    return ""


def get_current_branch() -> str:
    """Branch of the local checkout, falling back to the configured branch."""
    configured = load_source()["branch"]
    if _is_git_repo():
        rc, out = _run(["git", "rev-parse", "--abbrev-ref", "HEAD"])
        if rc == 0:
            name = out.strip()
            # Detached HEAD reports "HEAD" — that's not a branch name.
            if name and name != "HEAD":
                return name
    return configured


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


def _git_available() -> bool:
    rc, _out = _run(["git", "--version"], timeout=20)
    return rc == 0


def _scrub(text: str, src: dict) -> str:
    """Never let a token leak into a status line or log."""
    token = (src.get("token") or "").strip()
    return text.replace(token, "***") if token else text


def _git_fetch(src: dict, progress) -> tuple[bool, str]:
    """Fetch the configured branch straight from the configured remote URL.

    We fetch by URL rather than by remote name so the update always follows
    owner/repo/branch from the config, even when `origin` points somewhere
    else (a fork, an old clone, or a path that no longer exists).
    """
    url = _git_remote_url(src, with_token=True)
    cmd = ["git", "fetch", "--no-tags", "--force", url,
           f"refs/heads/{src['branch']}"]
    last = ""
    for attempt in range(3):
        if attempt:
            progress(f"fetch failed, retrying ({attempt + 1}/3)…")
            time.sleep(2 ** attempt)
        rc, out = _run(cmd, timeout=300)
        if rc == 0:
            return True, "fetched"
        last = _scrub(out, src)
    return False, f"git fetch failed: {last}"


def _git_update(src: dict, progress) -> tuple[bool, str]:
    """Hard-sync the working tree onto the fetched tip. Untracked files
    (helper_config.json, logs, downloaded models) are left alone."""
    if not _git_available():
        return False, "git is not installed or not on PATH"

    progress(f"git fetch {src['owner']}/{src['repo']} {src['branch']}…")
    ok, msg = _git_fetch(src, progress)
    if not ok:
        return False, msg

    rc, target = _run(["git", "rev-parse", "FETCH_HEAD"], timeout=60)
    if rc != 0 or not target:
        return False, f"could not resolve FETCH_HEAD: {_scrub(target, src)}"
    target = target.strip().split()[0]

    branch = get_current_branch() or src["branch"]
    progress(f"git reset --hard {target[:12]} (branch {branch})")
    # `reset --hard` rather than `checkout -B`: it moves the current branch and
    # overwrites the tree without refusing when an untracked file collides with
    # a newly added upstream one.
    rc, out = _run(["git", "reset", "--hard", target], timeout=180)
    if rc != 0:
        return False, f"git reset failed: {_scrub(out, src)}"

    rc, head_ref = _run(["git", "rev-parse", "--abbrev-ref", "HEAD"], timeout=60)
    if rc == 0 and head_ref.strip() == "HEAD":
        # Detached (a previous update left us there) — reattach to the branch.
        _run(["git", "checkout", "-B", branch, target], timeout=180)

    rc, head = _run(["git", "rev-parse", "HEAD"], timeout=60)
    if rc == 0 and head.strip() != target:
        return False, "working tree did not move to the fetched commit"

    # Keep `origin` pointing at the configured source (without the token) so
    # manual git commands agree with the updater.
    rc, current_remote = _run(["git", "remote", "get-url", "origin"], timeout=30)
    plain = _git_remote_url(src)
    if rc != 0:
        _run(["git", "remote", "add", "origin", plain], timeout=30)
    elif current_remote.strip() != plain and "github.com" in current_remote:
        _run(["git", "remote", "set-url", "origin", plain], timeout=30)

    # git is authoritative again — drop any marker left by a tarball fallback.
    try:
        (BASE_DIR / ".aios_sha").unlink()
    except OSError:
        pass

    return True, f"git updated to {target[:12]}"


def _tarball_update(src: dict, progress) -> tuple[bool, str]:
    """Download + extract to a STAGING dir. The actual file swap is deferred
    to `_spawn_apply_script()` which runs after the helper exits, because
    Windows won't let us overwrite files held open by the running process."""
    progress(f"downloading tarball ({src['owner']}/{src['repo']}@{src['branch']})…")
    blob = None
    for attempt, url in enumerate(_tarball_urls(src)):
        if attempt:
            progress("retrying download from the GitHub API…")
        blob = _http_bytes(url, timeout=300, progress=progress,
                           token=src.get("token", ""))
        if blob:
            break
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
            if not written:
                shutil.rmtree(STAGING_DIR, ignore_errors=True)
                return False, "tarball contained no files to apply"
            progress(f"staged {written} file(s) — will apply on restart")
    except Exception as exc:
        shutil.rmtree(STAGING_DIR, ignore_errors=True)
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
    """Pull the latest source and install it.

    Git installs are updated in place (fetch + hard reset + pip install). If
    git is missing or the fetch/reset fails for any reason, we fall back to the
    tarball path automatically rather than leaving the user stuck — tarball
    files are staged now and swapped in by the apply script after the helper
    exits (Windows holds the running files open).
    """
    progress = progress or (lambda _msg: None)
    src = load_source(owner, repo, branch)
    via_git = _is_git_repo()
    progress(f"updating via {'git' if via_git else 'tarball'} from "
             f"{src['owner']}/{src['repo']}@{src['branch']}…")
    git_error = ""
    if via_git:
        ok, msg = _git_update(src, progress)
        if ok:
            deps_ok, deps_msg = _install_deps(progress)
            progress(deps_msg)
            return {
                "ok": True,
                "message": "updated; restarting",
                "deps_message": deps_msg,
                "deps_ok": deps_ok,
                "restart_needed": True,
                "via": "git",
                "current": get_current_sha(),
            }
        git_error = msg
        progress(f"{msg} — falling back to a direct download")
    # Tarball: stage now, apply on restart.
    ok, msg = _tarball_update(src, progress)
    if not ok:
        return {"ok": False, "restart_needed": False,
                "message": f"{git_error}; {msg}" if git_error else msg}
    return {
        "ok": True,
        "message": "files staged · aiOS will close and reopen updated",
        "restart_needed": True,
        "via": "tarball",
        "staged": True,
        "git_error": git_error,
    }


def update_now(progress=None, owner: str | None = None, repo: str | None = None,
               branch: str | None = None, force: bool = False) -> dict:
    """Check GitHub and, if there's anything new, pull and install it.

    This is the one call the UI needs: no separate check step, no state to
    thread through. Returns the `perform_update` result plus the check result;
    `updated` is False when we were already on the latest commit.
    """
    progress = progress or (lambda _msg: None)
    progress("checking GitHub for the latest commit…")
    check = check_for_update(owner, repo, branch)
    if not check.get("ok"):
        return {"ok": False, "updated": False, "restart_needed": False,
                "check": check,
                "message": check.get("error") or "could not reach GitHub"}
    if not check.get("behind") and not force:
        return {"ok": True, "updated": False, "restart_needed": False,
                "check": check,
                "message": f"already up to date ({check.get('current') or '?'})"}
    latest = check.get("latest") or ""
    if latest:
        progress(f"latest is {latest} — installing")
    result = perform_update(progress, owner, repo, branch)
    result["check"] = check
    result["updated"] = bool(result.get("ok"))
    return result


# ---------------------------------------------------------------------------
# Apply-after-restart helper. This script is dropped into TEMP, run detached,
# waits for the running helper to release its files, swaps the staged tree in,
# reinstalls deps, and then relaunches the helper.
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
HELPER = BASE_DIR / "helper_overlay.py"
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
        return None
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
    # Clean up staging so a later restart doesn't re-apply a spent update.
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
    if not HELPER.exists():
        log("helper_overlay.py missing — cannot relaunch")
        return
    log("relaunching helper")
    kwargs = {"cwd": str(BASE_DIR), "close_fds": True}
    if os.name == "nt":
        DETACHED = 0x00000008
        NEW_GROUP = 0x00000200
        kwargs["creationflags"] = DETACHED | NEW_GROUP
    try:
        subprocess.Popen([sys.executable, str(HELPER)], **kwargs)
    except Exception as exc:
        log(f"relaunch failed: {exc}")


def main():
    log("== apply start ==")
    try:
        wait_for_parent()
        applied = apply_staging()
        # Deps are installed even when a few files refused to copy — a partial
        # update still needs whatever new packages requirements.txt asks for.
        if applied is not None:
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


def _has_staged_files() -> bool:
    if not STAGING_DIR.exists():
        return False
    try:
        return any(p.is_file() for p in STAGING_DIR.rglob("*"))
    except OSError:
        return False


def _spawn_apply_script() -> bool:
    try:
        script = _write_apply_script(os.getpid())
        kwargs = {"close_fds": True}
        if os.name == "nt":
            DETACHED_PROCESS = 0x00000008
            CREATE_NEW_PROCESS_GROUP = 0x00000200
            kwargs["creationflags"] = DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
        subprocess.Popen([sys.executable, str(script)], **kwargs)
        return True
    except Exception:
        return False


def restart_aios(extra_args: list[str] | None = None) -> None:
    """Restart the helper.

    If there's a staged update in `.aios_update_staging`, spawn the apply
    script (which will wait for us to exit, swap files in, install deps,
    and relaunch the helper). Otherwise just relaunch the helper directly.

    Safe to call from the Tk thread — we launch detached then os._exit.
    """
    extra_args = extra_args or []
    helper_path = BASE_DIR / "helper_overlay.py"
    if not helper_path.exists():
        return
    # If files were staged, hand off to the apply script and exit. An empty
    # staging dir (a download that died mid-flight) is not an update.
    if _has_staged_files():
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
    threading.Timer(0.6, lambda: os._exit(0)).start()


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["check", "update", "auto", "restart"])
    parser.add_argument("--force", action="store_true",
                        help="reinstall even when already on the latest commit")
    parser.add_argument("--no-restart", action="store_true",
                        help="with `auto`, skip relaunching the helper")
    args = parser.parse_args()
    emit = lambda m: print(f"[update] {m}", flush=True)
    if args.action == "check":
        print(json.dumps(check_for_update(), indent=2))
    elif args.action == "update":
        print(json.dumps(perform_update(progress=emit), indent=2))
    elif args.action == "auto":
        result = update_now(progress=emit, force=args.force)
        print(json.dumps({k: v for k, v in result.items() if k != "check"}, indent=2))
        if result.get("restart_needed") and not args.no_restart:
            emit("restarting aiOS…")
            restart_aios()
        sys.exit(0 if result.get("ok") else 1)
    elif args.action == "restart":
        restart_aios()
