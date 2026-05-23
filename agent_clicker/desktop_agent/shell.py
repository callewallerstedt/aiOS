"""PowerShell executor for the agent.

Runs commands via powershell.exe -NonInteractive -NoProfile, captures stdout +
stderr + exit code, enforces a timeout, and truncates output so the model
doesn't drown in megabytes from `Get-ChildItem -Recurse C:\\`.
"""
from __future__ import annotations
import os
import subprocess
import tempfile
from dataclasses import dataclass


PS_EXE = r"powershell.exe"          # Windows PowerShell 5.1 (always present)
DEFAULT_TIMEOUT = 30.0
MAX_OUTPUT_CHARS = 4000             # per stream, then truncate


@dataclass
class ShellResult:
    command: str
    exit_code: int
    stdout: str
    stderr: str
    timed_out: bool
    elapsed_ms: int

    def to_text(self, max_chars: int = MAX_OUTPUT_CHARS) -> str:
        def clip(s: str) -> str:
            s = s.rstrip()
            if len(s) > max_chars:
                return s[:max_chars] + f"\n…[truncated, {len(s) - max_chars} more chars]"
            return s
        parts = [f"$ {self.command}"]
        if self.timed_out:
            parts.append(f"(TIMED OUT after {self.elapsed_ms} ms)")
        else:
            parts.append(f"(exit={self.exit_code}, {self.elapsed_ms} ms)")
        if self.stdout.strip():
            parts.append("--- stdout ---\n" + clip(self.stdout))
        if self.stderr.strip():
            parts.append("--- stderr ---\n" + clip(self.stderr))
        if not self.stdout.strip() and not self.stderr.strip():
            parts.append("(no output)")
        return "\n".join(parts)


def run(command: str, cwd: str | None = None, timeout: float = DEFAULT_TIMEOUT, cancel_event=None) -> ShellResult:
    import time
    t0 = time.time()
    args = [PS_EXE, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
            "-Command", command]
    try:
        proc = subprocess.Popen(
            args,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, cwd=cwd, encoding="utf-8", errors="replace",
        )
        timed_out = False
        while proc.poll() is None:
            elapsed = time.time() - t0
            if cancel_event is not None and cancel_event.is_set():
                proc.kill()
                stdout, stderr = proc.communicate()
                return ShellResult(
                    command=command, exit_code=-3,
                    stdout=stdout or "", stderr=(stderr or "") + "\nSTOP requested; shell command killed.",
                    timed_out=False, elapsed_ms=int((time.time() - t0) * 1000),
                )
            if elapsed >= timeout:
                timed_out = True
                proc.kill()
                break
            time.sleep(0.05)
        stdout, stderr = proc.communicate()
        return ShellResult(
            command=command, exit_code=proc.returncode,
            stdout=stdout or "", stderr=stderr or "",
            timed_out=timed_out, elapsed_ms=int((time.time() - t0) * 1000),
        )
    except Exception as e:
        return ShellResult(
            command=command, exit_code=-2,
            stdout="", stderr=f"executor error: {type(e).__name__}: {e}",
            timed_out=False, elapsed_ms=int((time.time() - t0) * 1000),
        )


def run_script(script: str, cwd: str | None = None,
               timeout: float = DEFAULT_TIMEOUT, cancel_event=None) -> ShellResult:
    """Run a multi-line PowerShell SCRIPT.

    The script body is written to a temp .ps1 with a UTF-8 BOM, then executed
    via `powershell.exe -File`. This avoids ALL one-liner pitfalls — most
    importantly: here-strings (@'...'@) work, because the markers are on their
    own physical lines in the file, not crammed into a -Command argument.

    Prefer this over `run(...)` for anything multi-line, anything containing
    here-strings, function defs, or scripts that write source files.
    """
    import time
    t0 = time.time()
    fd, path = tempfile.mkstemp(prefix="agent_ps_", suffix=".ps1")
    try:
        # PowerShell 5.1 needs a UTF-8 BOM to treat the file as UTF-8 — without
        # it, non-ASCII chars (åäö, em-dashes, etc.) decode as Latin-1.
        with os.fdopen(fd, "wb") as f:
            f.write(b"\xef\xbb\xbf")
            f.write(script.encode("utf-8", errors="replace"))
        args = [PS_EXE, "-NoProfile", "-NonInteractive",
                "-ExecutionPolicy", "Bypass", "-File", path]
        try:
            proc = subprocess.Popen(
                args, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, cwd=cwd, encoding="utf-8", errors="replace",
            )
            timed_out = False
            while proc.poll() is None:
                elapsed = time.time() - t0
                if cancel_event is not None and cancel_event.is_set():
                    proc.kill()
                    stdout, stderr = proc.communicate()
                    return ShellResult(
                        command=f"(script: {script.count(chr(10)) + 1} lines)",
                        exit_code=-3,
                        stdout=stdout or "",
                        stderr=(stderr or "") + "\nSTOP requested; script killed.",
                        timed_out=False,
                        elapsed_ms=int((time.time() - t0) * 1000),
                    )
                if elapsed >= timeout:
                    timed_out = True
                    proc.kill()
                    break
                time.sleep(0.05)
            stdout, stderr = proc.communicate()
            return ShellResult(
                command=f"(script: {script.count(chr(10)) + 1} lines)",
                exit_code=proc.returncode,
                stdout=stdout or "", stderr=stderr or "",
                timed_out=timed_out,
                elapsed_ms=int((time.time() - t0) * 1000),
            )
        except Exception as e:
            return ShellResult(
                command=f"(script: {script.count(chr(10)) + 1} lines)",
                exit_code=-2, stdout="",
                stderr=f"executor error: {type(e).__name__}: {e}",
                timed_out=False, elapsed_ms=int((time.time() - t0) * 1000),
            )
    finally:
        try: os.unlink(path)
        except Exception: pass
