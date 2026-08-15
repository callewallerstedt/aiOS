"""Generation-bound verification state for one aiOS CODE turn.

The ledger is deliberately small and stdlib-only.  It records workspace
mutations, automatic per-file syntax diagnostics, and explicit verification
commands.  Completion policy stays deterministic: evidence only applies to
the workspace generation on which it ran.
"""

from __future__ import annotations

import copy
import fnmatch
import os
import posixpath
import re
import shlex
import threading
from typing import Any


_STATE_CLEAN = "clean"
_STATE_UNVERIFIED = "unverified"
_STATE_STALE = "stale"
_STATE_PASSED = "passed"
_STATE_FAILED = "failed"

_VERIFICATION_KINDS = frozenset(
    {"test", "lint", "typecheck", "build", "syntax", "ad_hoc"}
)

# Only explicit documentation and binary/design asset types are exempt.  An
# unknown extension is treated as source/configuration so a new language or a
# build file cannot silently bypass verification.
_DOC_EXTENSIONS = frozenset(
    {
        ".adoc",
        ".asc",
        ".markdown",
        ".md",
        ".mdx",
        ".rst",
        ".rtf",
        ".txt",
    }
)
_DOC_BASENAMES = frozenset(
    {
        "authors",
        "changelog",
        "code_of_conduct",
        "contributing",
        "copying",
        "license",
        "notice",
        "readme",
        "security",
    }
)
_ASSET_EXTENSIONS = frozenset(
    {
        ".avif",
        ".bmp",
        ".eot",
        ".flac",
        ".gif",
        ".ico",
        ".jpeg",
        ".jpg",
        ".m4a",
        ".mov",
        ".mp3",
        ".mp4",
        ".ogg",
        ".otf",
        ".pdf",
        ".png",
        ".pot",
        ".potm",
        ".potx",
        ".ppt",
        ".pptm",
        ".pptx",
        ".pps",
        ".ppsm",
        ".ppsx",
        ".odp",
        ".key",
        ".svg",
        ".tif",
        ".tiff",
        ".ttf",
        ".wav",
        ".webm",
        ".webp",
        ".woff",
        ".woff2",
    }
)

# These text/UI formats have no dependable stdlib syntax check, and the agent
# prompt explicitly says a localized presentation-only edit does not warrant a
# test run.  Keep them source-like for planned/distributed work, where a build
# may still be the proportionate proof, but exempt them on the direct path.
_DIRECT_EXEMPT_EXTENSIONS = frozenset(
    {
        ".css",
        ".htm",
        ".html",
        ".less",
        ".sass",
        ".scss",
        ".xhtml",
    }
)

_PASS_STATUSES = frozenset({"clean", "ok", "pass", "passed", "success", "succeeded"})
_FAIL_STATUSES = frozenset({"error", "errors", "fail", "failed", "invalid"})

_OUTPUT_LIMIT = 4096
_SHELL_CONTROL_RE = re.compile(r"(?:&&|\|\||[;\r\n]|(?<!\|)\|(?!\|))")
_POWERSHELL_EXIT_STATUS_TRAILER_RE = re.compile(
    r"""
    ^(?P<command>.+?)\s*;\s*
    (?:echo|write-output)\s+
    (?P<quote>["']?)
    (?P<label>[A-Za-z_][A-Za-z0-9_-]*(?:exit|status|code)[A-Za-z0-9_-]*)
    \s*=\s*\$(?:LASTEXITCODE|\{LASTEXITCODE\})
    (?P=quote)\s*$
    """,
    re.IGNORECASE | re.DOTALL | re.VERBOSE,
)
_NO_EXECUTION_FLAGS = frozenset(
    {"--collect-only", "--dry-run", "--help", "--version", "-h"}
)
_COVERAGE_BOUND_KINDS = frozenset({"syntax", "lint", "typecheck"})
_CLEAN_MAKE_TARGETS = frozenset({"clean", "clobber", "distclean", "mostlyclean", "mrproper"})

# Values for these checker options are configuration, reports, or selectors;
# they are not source paths covered by the check.  This is intentionally a
# shared conservative subset rather than a complete parser for every CLI.
_CHECKER_OPTIONS_WITH_VALUES = frozenset(
    {
        "--config",
        "--config-file",
        "--config-path",
        "--exclude",
        "--extend-exclude",
        "--extend-ignore",
        "--extend-select",
        "--format",
        "--ignore",
        "--output",
        "--output-file",
        "--output-format",
        "-p",
        "--project",
        "--python-version",
        "--select",
        "--target-version",
        "--tsconfig",
    }
)


def _normalize_path(path: Any) -> str:
    raw = os.fspath(path).strip() if path is not None else ""
    if not raw:
        raise ValueError("path must be non-empty")
    # Normalize both slash styles so persisted snapshots are portable between
    # Windows workers and provider-side tooling.
    normalized = posixpath.normpath(raw.replace("\\", "/"))
    return normalized


def _path_key(path: str) -> str:
    # aiOS is Windows-first, but case-folding also makes replayed Windows paths
    # stable if tests or workers happen to run on another host.
    return path.casefold()


def _classify_path(path: str) -> str:
    basename = posixpath.basename(path).casefold()
    stem, extension = posixpath.splitext(basename)
    if extension in _DOC_EXTENSIONS or basename in _DOC_BASENAMES or stem in _DOC_BASENAMES:
        return "docs"
    if extension in _ASSET_EXTENSIONS:
        return "asset"
    return "source"


def _normalize_diagnostic_status(status: Any) -> str:
    value = str(status or "unavailable").strip().casefold().replace("-", "_")
    if value in _PASS_STATUSES:
        return "passed"
    if value in _FAIL_STATUSES:
        return "failed"
    return "unavailable"


def _bounded_output(output: Any) -> str:
    text = str(output or "")
    if len(text) <= _OUTPUT_LIMIT:
        return text
    head = _OUTPUT_LIMIT // 2
    tail = _OUTPUT_LIMIT - head
    return f"{text[:head]}\n... output truncated ...\n{text[-tail:]}"


def _tokenize(command: str) -> list[str]:
    try:
        tokens = shlex.split(command, posix=False)
    except ValueError:
        return []
    cleaned: list[str] = []
    for token in tokens:
        token = token.strip().strip("\"'")
        if token:
            cleaned.append(token)
    return cleaned


def _program_name(token: str) -> str:
    token = token.strip().strip("\"'").replace("\\", "/")
    return posixpath.basename(token).casefold()


def _split_exit_status_trailer(command: str) -> tuple[str, str]:
    """Separate a read-only PowerShell native-exit report from one command.

    Models commonly print ``$LASTEXITCODE`` because PowerShell itself exits 0
    after ``echo``.  Treat only that narrow, terminal form as transparent; all
    other compound commands remain non-verification.
    """

    raw = str(command or "").strip()
    match = _POWERSHELL_EXIT_STATUS_TRAILER_RE.fullmatch(raw)
    if not match:
        return raw, ""
    core = str(match.group("command") or "").strip()
    if not core or _SHELL_CONTROL_RE.search(core):
        return raw, ""
    return core, str(match.group("label") or "")


def _reported_exit_code(output: Any, label: str) -> int | None:
    if not label:
        return None
    matches = re.findall(
        rf"(?im)^\s*{re.escape(label)}\s*=\s*(-?\d+)\s*$",
        str(output or ""),
    )
    return int(matches[-1]) if matches else None


def _unwrap_command(tokens: list[str]) -> list[str]:
    result = list(tokens)
    if result and result[0] == "&":
        result.pop(0)

    # Environment runners preserve the wrapped command's exit code.
    while len(result) >= 2:
        name = _program_name(result[0])
        if name in {"uv", "uv.exe", "poetry", "poetry.exe", "pipenv", "pipenv.exe"} and result[1].casefold() == "run":
            result = result[2:]
            continue
        if name in {"npx", "npx.cmd", "pnpx", "pnpx.cmd"}:
            result = result[1:]
            continue
        break
    return result


def _npm_script_kind(tokens: list[str]) -> str | None:
    if not tokens:
        return None
    program = _program_name(tokens[0])
    if program not in {
        "npm",
        "npm.cmd",
        "pnpm",
        "pnpm.cmd",
        "yarn",
        "yarn.cmd",
        "bun",
        "bun.exe",
    }:
        return None
    args = [token.casefold() for token in tokens[1:]]
    if args and args[0] == "run":
        args = args[1:]
    if not args:
        return None
    script = args[0].split(":", 1)[0]
    return {
        "test": "test",
        "tests": "test",
        "lint": "lint",
        "typecheck": "typecheck",
        "type-check": "typecheck",
        "check-types": "typecheck",
        "build": "build",
    }.get(script)


def _artifact_is_executed(tokens: list[str], artifact_path: str) -> bool:
    if not artifact_path or len(tokens) < 2:
        return False
    normalized = _normalize_path(artifact_path).casefold()
    basename = posixpath.basename(normalized)
    token_paths = {
        token.strip().strip("\"'").replace("\\", "/").casefold() for token in tokens[1:]
    }
    if normalized not in token_paths and basename not in {
        posixpath.basename(token) for token in token_paths
    }:
        return False
    program = _program_name(tokens[0])
    return program in {
        "bash",
        "bash.exe",
        "cmd",
        "cmd.exe",
        "node",
        "node.exe",
        "php",
        "php.exe",
        "powershell",
        "powershell.exe",
        "pwsh",
        "pwsh.exe",
        "py",
        "py.exe",
        "python",
        "python.exe",
        "python3",
        "python3.exe",
        "ruby",
        "ruby.exe",
        "sh",
        "sh.exe",
    }


def _looks_like_test_script(value: Any) -> bool:
    """Recognize conventional standalone test entrypoints, never arbitrary scripts."""

    path = str(value or "").strip().strip("\"'").replace("\\", "/").casefold()
    if not path or path.startswith("-"):
        return False
    basename = posixpath.basename(path)
    return (
        basename in {"test.py", "test.js", "test.ts"}
        or bool(re.match(r"^test_.+\.(?:py|js|ts)$", basename))
        or bool(re.search(r"(?:_test\.py|\.(?:test|spec)\.(?:js|ts))$", basename))
    )


def _coverage_tokens(command: str, kind: str) -> list[str]:
    """Return explicit checker target arguments, excluding CLI structure."""

    if kind not in _COVERAGE_BOUND_KINDS:
        return []
    tokens = _unwrap_command(_tokenize(command))
    if not tokens:
        return []
    program = _program_name(tokens[0])
    args = list(tokens[1:])

    if program in {"python", "python.exe", "python3", "python3.exe", "py", "py.exe"}:
        if len(args) >= 2 and args[0].casefold() == "-m":
            module = args[1].casefold()
            args = args[2:]
            if module == "ruff" and args and args[0].casefold() in {"check", "analyze"}:
                args = args[1:]
    elif program in {"ruff", "ruff.exe"} and args and args[0].casefold() in {"check", "analyze"}:
        args = args[1:]
    elif program in {"pre-commit", "pre-commit.exe"}:
        # Hook names are not paths.  A pre-commit run is repository-scoped
        # unless a future integration supplies its resolved file list.
        return []
    elif program in {
        "npm", "npm.cmd", "pnpm", "pnpm.cmd", "yarn", "yarn.cmd", "bun", "bun.exe"
    }:
        if args and args[0].casefold() == "run":
            args = args[1:]
        # The script name describes the checker, not a filesystem target.
        args = args[1:] if args else []
    elif program in {"cargo", "cargo.exe"} and args and args[0].casefold() == "check":
        args = args[1:]
    elif program in {"go", "go.exe"} and args and args[0].casefold() == "vet":
        args = args[1:]

    result: list[str] = []
    skip_value = False
    after_separator = False
    for raw in args:
        token = raw.strip().strip("\"'")
        lowered = token.casefold()
        if not token:
            continue
        if skip_value:
            skip_value = False
            continue
        if not after_separator and lowered == "--":
            after_separator = True
            continue
        if not after_separator and lowered in _CHECKER_OPTIONS_WITH_VALUES:
            skip_value = True
            continue
        if not after_separator and lowered.startswith("-"):
            continue
        if lowered in {"check", "analyze"} and program in {"ruff", "ruff.exe"}:
            continue
        result.append(token)
    return result


def _normalize_coverage_target(value: Any) -> str:
    raw = str(value or "").strip().strip("\"'").replace("\\", "/")
    if not raw or raw == "-":
        return ""
    # Pytest-style selectors occasionally reach generic wrappers.  Only the
    # filesystem portion is meaningful for path coverage.
    raw = raw.split("::", 1)[0]
    if raw == "./...":
        return "."
    if raw.endswith("/..."):
        raw = raw[:-4] or "."
    normalized = posixpath.normpath(raw)
    while normalized.startswith("./"):
        normalized = normalized[2:]
    return normalized or "."


def _command_coverage(command: str, kind: str) -> tuple[str, list[str]]:
    """Describe whether path-bound evidence is broad or explicitly targeted."""

    if kind not in _COVERAGE_BOUND_KINDS:
        return "unbounded", []
    command, _status_label = _split_exit_status_trailer(command)
    targets = list(
        dict.fromkeys(
            target
            for target in (_normalize_coverage_target(item) for item in _coverage_tokens(command, kind))
            if target
        )
    )
    if not targets or any(target in {".", "**", "**/*"} for target in targets):
        return "all", []
    return "targets", targets


def _target_covers_path(target: str, path: str) -> bool:
    target_value = _normalize_coverage_target(target).casefold()
    path_value = _normalize_path(path).casefold()
    if target_value in {".", "**", "**/*"}:
        return True
    if any(character in target_value for character in "*?["):
        if "/" not in target_value:
            return "/" not in path_value and fnmatch.fnmatchcase(path_value, target_value)
        return fnmatch.fnmatchcase(path_value, target_value)
    target_value = target_value.rstrip("/")
    if path_value == target_value or path_value.startswith(target_value + "/"):
        return True
    # A checker invoked with an absolute path can cover a ledger path persisted
    # relative to the project (and vice versa).
    target_absolute = bool(re.match(r"^(?:[a-z]:/|/)", target_value))
    path_absolute = bool(re.match(r"^(?:[a-z]:/|/)", path_value))
    if target_absolute != path_absolute and (
        path_value.endswith("/" + target_value) or target_value.endswith("/" + path_value)
    ):
        return True
    return False


def _classify_command(command: str, artifact_path: str = "") -> str:
    command, _status_label = _split_exit_status_trailer(str(command or ""))
    if not command or _SHELL_CONTROL_RE.search(command):
        return "non_verification"
    tokens = _unwrap_command(_tokenize(command))
    if not tokens:
        return "non_verification"

    lowered = [token.casefold() for token in tokens]
    # Metadata/dry-run invocations are not proof that code executed.
    if any(flag in _NO_EXECUTION_FLAGS for flag in lowered[1:]):
        return "non_verification"

    npm_kind = _npm_script_kind(tokens)
    if npm_kind:
        return npm_kind

    program = _program_name(tokens[0])
    args = lowered[1:]

    if program in {"python", "python.exe", "python3", "python3.exe", "py", "py.exe"}:
        if len(args) >= 2 and args[0] == "-m":
            module = args[1]
            if module in {"pytest", "unittest", "nose", "nose2"}:
                return "test"
            if module in {"py_compile", "compileall"}:
                return "syntax"
            if module in {"mypy", "pyright", "basedpyright", "pyre"}:
                return "typecheck"
            if module == "build":
                return "build"
            if module == "ruff" and len(args) >= 3 and args[2] in {"check", "analyze"}:
                return "lint"
        elif args and _looks_like_test_script(tokens[1]):
            return "test"
    if program in {"pytest", "pytest.exe", "py.test", "py.test.exe", "nose", "nose2"}:
        return "test"
    if program in {"jest", "jest.cmd", "vitest", "vitest.cmd", "mocha", "mocha.cmd"}:
        return "test"
    if program in {"cargo", "cargo.exe"} and args:
        if args[0] == "test":
            return "test"
        if args[0] == "check":
            return "typecheck"
        if args[0] == "build":
            return "build"
    if program in {"go", "go.exe"} and args:
        if args[0] == "test":
            return "test"
        if args[0] == "vet":
            return "typecheck"
        if args[0] == "build":
            return "build"
    if program in {"dotnet", "dotnet.exe"} and args:
        if args[0] == "test":
            return "test"
        if args[0] == "build":
            return "build"
    if program in {"mvn", "mvn.cmd", "mvnw", "mvnw.cmd"} and args:
        if any(arg == "test" for arg in args):
            return "test"
        if any(arg in {"package", "verify", "install"} for arg in args):
            return "build"
    if program in {"gradle", "gradle.bat", "gradlew", "gradlew.bat"} and args:
        if any(arg.casefold() == "test" for arg in args):
            return "test"
        if any(arg.casefold() in {"build", "assemble"} for arg in args):
            return "build"
    if program in {"make", "make.exe", "nmake", "nmake.exe"}:
        if any(arg in {"test", "tests", "check"} for arg in args):
            return "test"
        if any(arg in _CLEAN_MAKE_TARGETS for arg in args) and not any(
            arg in {"all", "build", "install", "package", "release"} for arg in args
        ):
            return "non_verification"
        return "build"
    if program in {"cmake", "cmake.exe"} and "--build" in args:
        return "build"
    if program in {"ninja", "ninja.exe", "msbuild", "msbuild.exe"}:
        return "build"
    if program in {"vite", "vite.cmd", "next", "next.cmd", "webpack", "webpack.cmd"} and args and args[0] == "build":
        return "build"

    if program in {
        "eslint",
        "eslint.cmd",
        "flake8",
        "flake8.exe",
        "golangci-lint",
        "golangci-lint.exe",
        "hadolint",
        "markdownlint",
        "pylint",
        "pylint.exe",
        "shellcheck",
        "stylelint",
        "stylelint.cmd",
        "yamllint",
    }:
        return "lint"
    if program in {"ruff", "ruff.exe"} and (not args or args[0] in {"check", "analyze"}):
        return "lint"
    if program in {"pre-commit", "pre-commit.exe"} and args and args[0] == "run":
        return "lint"
    if program in {
        "basedpyright",
        "basedpyright.exe",
        "mypy",
        "mypy.exe",
        "pyre",
        "pyre.exe",
        "pyright",
        "pyright.exe",
        "tsc",
        "tsc.cmd",
    }:
        return "typecheck"

    if program in {"node", "node.exe"} and "--check" in args:
        return "syntax"
    if program in {"node", "node.exe"} and args and _looks_like_test_script(tokens[1]):
        return "test"
    if program in {"ruby", "ruby.exe"} and "-c" in args:
        return "syntax"
    if program in {"php", "php.exe"} and "-l" in args:
        return "syntax"
    if program in {"bash", "bash.exe", "sh", "sh.exe"} and "-n" in args:
        return "syntax"

    if _artifact_is_executed(tokens, artifact_path):
        return "ad_hoc"
    return "non_verification"


def classify_command(command: Any, artifact_path: Any = "") -> str:
    """Public, side-effect-free command classification for harness policy."""
    return _classify_command(str(command or ""), str(artifact_path or ""))


def _normalize_strategy(strategy: Any) -> tuple[str, bool]:
    value = str(strategy or "").strip().casefold().replace("-", "_")
    if value in {"direct", "direct_edit", "single", "small", "small_edit"}:
        return "direct", False
    if value in {"distributed", "delegated", "multi_agent", "multiagent"}:
        return "distributed", True
    if value in {"planned", "plan", "planner", "large", "large_task", "coder_led"}:
        return "planned", True
    # An unknown/typoed strategy must not weaken the completion gate.
    return value or "unknown", True


class VerificationLedger:
    """Track verification evidence for a single CODE turn.

    The object is thread-safe because terminal callbacks and file mutation
    callbacks may arrive from different worker threads.
    """

    schema_version = 4

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._generation = 0
        self._paths: dict[str, dict[str, Any]] = {}
        self._evidence: list[dict[str, Any]] = []
        self._completion_blocks = 0
        self._had_verification_signal = False

    @property
    def generation(self) -> int:
        with self._lock:
            return self._generation

    def begin_turn(self) -> None:
        """Reset bounded completion retries while preserving workspace evidence."""

        with self._lock:
            self._completion_blocks = 0

    def mark_mutation(
        self,
        path: Any,
        content_hash: Any,
        diagnostic_status: str = "unavailable",
        diagnostic_checker: str = "",
        previous_hash: Any = None,
    ) -> dict[str, Any]:
        """Record the latest net content identity and diagnostics for ``path``.

        ``previous_hash`` is the content identity immediately before the first
        mutation observed this turn.  When it is known, returning a file to
        that identity removes it from the changed-path set.  This keeps
        temporary verification files and checkpoint reversions from making a
        clean final workspace look perpetually stale.
        """

        normalized = _normalize_path(path)
        key = _path_key(normalized)
        status = _normalize_diagnostic_status(diagnostic_status)
        checker = str(diagnostic_checker or "").strip()
        current_hash = str(content_hash or "")
        with self._lock:
            previous = self._paths.get(key)
            initial_known = bool(previous and previous.get("initial_known"))
            initial_hash = str(previous.get("initial_hash") or "") if previous else ""
            if not previous and previous_hash is not None:
                initial_known = True
                initial_hash = str(previous_hash or "")

            # A persisted session change becomes context, not a mutation made
            # by the next operator instruction.  Keep both identities: the
            # original session baseline drives the net session diff, while the
            # turn baseline lets a temporary follow-up edit return to the
            # carried state without erasing that earlier change.
            turn_baseline_known = bool(previous and previous.get("turn_baseline_known"))
            turn_baseline_hash = (
                str(previous.get("turn_baseline_hash") or "") if previous else ""
            )
            turn_baseline_carried = bool(
                previous and previous.get("turn_baseline_carried")
            )
            if previous and previous.get("carried"):
                turn_baseline_known = True
                turn_baseline_hash = str(previous.get("content_hash") or "")
                turn_baseline_carried = True
            elif not previous and previous_hash is not None:
                turn_baseline_known = True
                turn_baseline_hash = str(previous_hash or "")
                turn_baseline_carried = False

            if previous and current_hash == str(previous.get("content_hash") or ""):
                unchanged = dict(previous)
                unchanged.update({
                    "diagnostic_status": status,
                    "diagnostic_checker": checker,
                    "diagnostic_fresh": status in {"passed", "failed"} and bool(checker),
                    "mutation_count": int(previous.get("mutation_count") or 0) + 1,
                    "unchanged": True,
                })
                self._paths[key] = unchanged
                if unchanged["diagnostic_fresh"]:
                    self._had_verification_signal = True
                return copy.deepcopy(unchanged)

            if initial_known and current_hash == initial_hash:
                self._paths.pop(key, None)
                return {
                    "path": normalized,
                    "content_hash": current_hash,
                    "category": _classify_path(normalized),
                    "generation": self._generation,
                    "first_generation": (
                        int(previous["first_generation"]) if previous else self._generation
                    ),
                    "mutation_count": (int(previous["mutation_count"]) + 1 if previous else 1),
                    "diagnostic_status": status,
                    "diagnostic_checker": checker,
                    "diagnostic_fresh": status in {"passed", "failed"} and bool(checker),
                    "initial_known": True,
                    "initial_hash": initial_hash,
                    "carried": False,
                    "reverted": True,
                }

            if turn_baseline_known and current_hash == turn_baseline_hash:
                if turn_baseline_carried:
                    restored = {
                        "path": normalized,
                        "content_hash": current_hash,
                        "category": _classify_path(normalized),
                        "generation": int(previous["generation"]) if previous else self._generation,
                        "first_generation": (
                            int(previous["first_generation"]) if previous else self._generation
                        ),
                        "mutation_count": (int(previous["mutation_count"]) + 1 if previous else 1),
                        "diagnostic_status": status,
                        "diagnostic_checker": checker,
                        "diagnostic_fresh": status in {"passed", "failed"} and bool(checker),
                        "initial_known": initial_known,
                        "initial_hash": initial_hash,
                        "carried": True,
                        "turn_baseline_known": True,
                        "turn_baseline_hash": current_hash,
                        "turn_baseline_carried": True,
                    }
                    self._paths[key] = restored
                    return copy.deepcopy(restored)
                self._paths.pop(key, None)
                return {
                    "path": normalized,
                    "content_hash": current_hash,
                    "category": _classify_path(normalized),
                    "generation": self._generation,
                    "first_generation": (
                        int(previous["first_generation"]) if previous else self._generation
                    ),
                    "mutation_count": (int(previous["mutation_count"]) + 1 if previous else 1),
                    "diagnostic_status": status,
                    "diagnostic_checker": checker,
                    "diagnostic_fresh": status in {"passed", "failed"} and bool(checker),
                    "initial_known": initial_known,
                    "initial_hash": initial_hash,
                    "carried": False,
                    "reverted": True,
                }

            self._generation += 1
            record = {
                "path": normalized,
                "content_hash": current_hash,
                "category": _classify_path(normalized),
                "generation": self._generation,
                "first_generation": (
                    int(previous["first_generation"]) if previous else self._generation
                ),
                "mutation_count": (int(previous["mutation_count"]) + 1 if previous else 1),
                "diagnostic_status": status,
                "diagnostic_checker": checker,
                "diagnostic_fresh": status in {"passed", "failed"} and bool(checker),
                "initial_known": initial_known,
                "initial_hash": initial_hash,
                "carried": False,
                "turn_baseline_known": turn_baseline_known,
                "turn_baseline_hash": turn_baseline_hash,
                "turn_baseline_carried": turn_baseline_carried,
            }
            self._paths[key] = record
            if record["diagnostic_fresh"]:
                self._had_verification_signal = True
            return copy.deepcopy(record)

    def record_command(
        self,
        command: Any,
        exit_code: Any,
        output: Any = "",
        elapsed_seconds: Any = 0.0,
        artifact_path: Any = "",
        *,
        explicit_verification: bool = False,
    ) -> dict[str, Any]:
        """Record one terminal result, conservatively classifying verification."""

        command_text = str(command or "").strip()
        artifact = str(artifact_path or "").strip()
        kind = _classify_command(command_text, artifact)
        prompt_command = bool(explicit_verification)
        classification_command, status_label = _split_exit_status_trailer(command_text)
        tokens = _unwrap_command(_tokenize(classification_command))
        if (
            kind == "non_verification"
            and prompt_command
            and classification_command
            and not _SHELL_CONTROL_RE.search(classification_command)
            and tokens
            and not any(flag in _NO_EXECUTION_FLAGS for flag in (token.casefold() for token in tokens[1:]))
        ):
            # A project-specific executable can be the strongest available
            # acceptance check even when its name is not pytest/build/lint.
            # The caller may set this flag only for an exact safe command
            # quoted in the user's current instruction.
            kind = "ad_hoc"
        coverage_mode, coverage_targets = _command_coverage(command_text, kind)
        try:
            code = int(exit_code)
        except (TypeError, ValueError):
            code = -1
        if status_label:
            reported_code = _reported_exit_code(output, status_label)
            # The trailing echo makes PowerShell itself exit successfully. The
            # printed native status is authoritative; a missing marker is not
            # allowed to turn an unknown verifier result into a pass.
            code = reported_code if reported_code is not None else -1
        try:
            elapsed = max(0.0, float(elapsed_seconds))
        except (TypeError, ValueError):
            elapsed = 0.0
        with self._lock:
            record = {
                "sequence": len(self._evidence) + 1,
                "generation": self._generation,
                "command": command_text,
                "command_key": " ".join(command_text.casefold().split()),
                "kind": kind,
                "verification": kind in _VERIFICATION_KINDS,
                "status": (
                    "passed"
                    if kind in _VERIFICATION_KINDS and code == 0
                    else "failed"
                    if kind in _VERIFICATION_KINDS
                    else "ignored"
                ),
                "exit_code": code,
                "output": _bounded_output(output),
                "elapsed_seconds": elapsed,
                "artifact_path": artifact,
                "explicit_prompt_command": prompt_command and kind == "ad_hoc",
                "coverage_mode": coverage_mode,
                "coverage_targets": coverage_targets,
                "carried": False,
            }
            self._evidence.append(record)
            if record["verification"]:
                self._had_verification_signal = True
            return copy.deepcopy(record)

    def _path_sets(
        self,
        *,
        carried: bool | None = None,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        records = sorted(self._paths.values(), key=lambda item: item["path"].casefold())
        if carried is not None:
            records = [item for item in records if bool(item.get("carried")) is carried]
        source = [item for item in records if item["category"] == "source"]
        non_code = [item for item in records if item["category"] != "source"]
        return source, non_code

    def _current_evidence(self) -> list[dict[str, Any]]:
        return [
            item
            for item in self._evidence
            if (
                item["verification"]
                and not item.get("carried")
                and item["generation"] == self._generation
            )
        ]

    @staticmethod
    def _unresolved_evidence(current: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        # A failed targeted command is cleared only by rerunning that command
        # successfully.  Passing an unrelated lint command must not hide a
        # still-failing test command from the same generation.
        latest_by_command: dict[tuple[str, str], dict[str, Any]] = {}
        for item in current:
            latest_by_command[(item["kind"], item["command_key"])] = item
        latest = list(latest_by_command.values())
        failed = [item for item in latest if item["status"] == "failed"]
        passed = [item for item in latest if item["status"] == "passed"]
        return failed, passed

    @staticmethod
    def _passing_evidence_for_paths(
        passed: list[dict[str, Any]],
        required_paths: list[dict[str, Any]],
    ) -> tuple[list[dict[str, Any]], set[str]]:
        """Return passing evidence only when its union covers every source path."""

        required = {_path_key(item["path"]): item["path"] for item in required_paths}
        if not required:
            return list(passed), set()
        accepted: list[dict[str, Any]] = []
        covered: set[str] = set()
        broad = False
        for item in passed:
            kind = str(item.get("kind") or "")
            mode = str(item.get("coverage_mode") or "")
            targets = [str(target) for target in item.get("coverage_targets") or []]
            if not mode:
                mode, targets = _command_coverage(str(item.get("command") or ""), kind)
            if kind not in _COVERAGE_BOUND_KINDS or mode in {"all", "unbounded"}:
                broad = True
                accepted.append(item)
                continue
            if mode != "targets" or not targets:
                continue
            matched = {
                key
                for key, path in required.items()
                if any(_target_covers_path(target, path) for target in targets)
            }
            if matched:
                covered.update(matched)
                accepted.append(item)
        if broad:
            covered = set(required)
        if covered != set(required):
            return [], covered
        return accepted, covered

    def decision(self, strategy: Any) -> dict[str, Any]:
        """Return the deterministic completion decision without mutating attempts."""

        strategy_name, explicit_required = _normalize_strategy(strategy)
        with self._lock:
            source_paths, non_code_paths = self._path_sets(carried=False)
            carried_source_paths, carried_non_code_paths = self._path_sets(carried=True)
            all_paths = source_paths + non_code_paths
            direct_exempt_paths = [
                item
                for item in source_paths
                if posixpath.splitext(item["path"].casefold())[1] in _DIRECT_EXEMPT_EXTENSIONS
            ] if strategy_name == "direct" else []
            required_source_paths = [
                item for item in source_paths if item not in direct_exempt_paths
            ]
            current = self._current_evidence()
            failed_evidence, all_passed_evidence = self._unresolved_evidence(current)
            passed_evidence, covered_keys = self._passing_evidence_for_paths(
                all_passed_evidence,
                required_source_paths,
            )
            failed_diagnostics = [
                item
                for item in all_paths
                if item["diagnostic_fresh"] and item["diagnostic_status"] == "failed"
            ]
            automatic_pass = bool(required_source_paths) and all(
                item["diagnostic_fresh"] and item["diagnostic_status"] == "passed"
                for item in required_source_paths
            )
            coverage_incomplete = bool(all_passed_evidence) and not passed_evidence

            if failed_diagnostics:
                state = _STATE_FAILED
                allowed = False
                reason = "Fresh automatic diagnostics failed for changed files."
            elif failed_evidence:
                state = _STATE_FAILED
                allowed = False
                reason = "Explicit verification failed for the current workspace generation."
            elif not all_paths:
                state = _STATE_CLEAN
                allowed = True
                reason = "No workspace mutations were recorded in this turn."
            elif not required_source_paths:
                state = _STATE_PASSED
                allowed = True
                reason = (
                    "Only direct presentation/content files changed; verification is exempt."
                    if source_paths
                    else "Only documentation or asset files changed; verification is exempt."
                )
            elif passed_evidence:
                state = _STATE_PASSED
                allowed = True
                reason = "Explicit verification passed for the current workspace generation."
            elif not explicit_required and automatic_pass:
                state = _STATE_PASSED
                allowed = True
                reason = "Fresh automatic syntax diagnostics passed for every changed source file."
            elif explicit_required and automatic_pass:
                state = _STATE_UNVERIFIED
                allowed = False
                reason = (
                    "Fresh syntax diagnostics passed, but a planned source change still needs one "
                    "focused behavior, test, lint, typecheck, or build check."
                )
            elif coverage_incomplete:
                state = _STATE_UNVERIFIED
                allowed = False
                reason = "Passing targeted verification does not cover every changed source file."
            elif self._had_verification_signal:
                state = _STATE_STALE
                allowed = False
                reason = "Verification evidence exists, but none applies to the current workspace generation."
            else:
                state = _STATE_UNVERIFIED
                allowed = False
                reason = "Changed source files have no passing verification evidence."

            return {
                "allowed": allowed,
                "state": state,
                "reason": reason,
                "strategy": strategy_name,
                "requires_explicit_verification": explicit_required,
                "generation": self._generation,
                "source_paths": [item["path"] for item in source_paths],
                "non_code_paths": [item["path"] for item in non_code_paths],
                "carried_source_paths": [item["path"] for item in carried_source_paths],
                "carried_non_code_paths": [item["path"] for item in carried_non_code_paths],
                "carried_change_count": len(carried_source_paths) + len(carried_non_code_paths),
                "direct_exempt_paths": [item["path"] for item in direct_exempt_paths],
                "automatic_diagnostics_passed": automatic_pass,
                "failed_diagnostic_paths": [item["path"] for item in failed_diagnostics],
                "passing_evidence_count": len(passed_evidence),
                "ignored_passing_evidence_count": len(all_passed_evidence) - len(passed_evidence),
                "verification_covered_paths": [
                    item["path"]
                    for item in required_source_paths
                    if _path_key(item["path"]) in covered_keys
                ],
                "failing_evidence_count": len(failed_evidence),
            }

    def block_completion(self, strategy: Any, max_attempts: int = 2) -> dict[str, Any]:
        """Apply the bounded completion gate.

        At most ``max_attempts`` calls request another continuation.  A later
        call remains disallowed but is marked exhausted so the caller can end
        the job as incomplete instead of looping.
        """

        try:
            maximum = max(0, int(max_attempts))
        except (TypeError, ValueError):
            maximum = 2
        with self._lock:
            result = self.decision(strategy)
            if result["allowed"]:
                result.update(
                    {
                        "blocked": False,
                        "continuation": False,
                        "exhausted": False,
                        "attempt": self._completion_blocks,
                        "max_attempts": maximum,
                        "remaining_attempts": max(0, maximum - self._completion_blocks),
                    }
                )
                return result
            if self._completion_blocks >= maximum:
                result.update(
                    {
                        "blocked": False,
                        "continuation": False,
                        "exhausted": True,
                        "attempt": self._completion_blocks,
                        "max_attempts": maximum,
                        "remaining_attempts": 0,
                    }
                )
                return result

            self._completion_blocks += 1
            result.update(
                {
                    "blocked": True,
                    "continuation": True,
                    "exhausted": False,
                    "attempt": self._completion_blocks,
                    "max_attempts": maximum,
                    "remaining_attempts": max(0, maximum - self._completion_blocks),
                }
            )
            return result

    def block_requirement(
        self,
        strategy: Any,
        reason: Any,
        max_attempts: int = 2,
        **details: Any,
    ) -> dict[str, Any]:
        """Apply the bounded gate for a requirement outside path coverage.

        CODE uses this for exact operator-requested commands.  The ledger still
        owns retry accounting, so an external requirement cannot accidentally
        create an unbounded second completion loop.
        """

        try:
            maximum = max(0, int(max_attempts))
        except (TypeError, ValueError):
            maximum = 2
        with self._lock:
            result = self.decision(strategy)
            result.update({
                "allowed": False,
                "state": _STATE_UNVERIFIED,
                "reason": str(reason or "A required verification step is outstanding."),
                **copy.deepcopy(details),
            })
            if self._completion_blocks >= maximum:
                result.update({
                    "blocked": False,
                    "continuation": False,
                    "exhausted": True,
                    "attempt": self._completion_blocks,
                    "max_attempts": maximum,
                    "remaining_attempts": 0,
                })
                return result
            self._completion_blocks += 1
            result.update({
                "blocked": True,
                "continuation": True,
                "exhausted": False,
                "attempt": self._completion_blocks,
                "max_attempts": maximum,
                "remaining_attempts": max(0, maximum - self._completion_blocks),
            })
            return result

    def restore(self, snapshot: Any, *, new_turn: bool = False) -> VerificationLedger:
        """Restore a persisted snapshot, optionally starting fresh retry accounting.

        Stored categories, verification kinds, and coverage claims are derived
        again instead of trusted.  That upgrades schema-v1 snapshots safely and
        prevents stale persisted metadata from weakening current policy.
        """

        if not isinstance(snapshot, dict):
            raise ValueError("verification snapshot must be an object")

        def safe_int(value: Any, default: int = 0) -> int:
            try:
                return max(0, int(value))
            except (TypeError, ValueError):
                return default

        def safe_float(value: Any) -> float:
            try:
                return max(0.0, float(value))
            except (TypeError, ValueError):
                return 0.0

        restored_paths: dict[str, dict[str, Any]] = {}
        raw_paths = snapshot.get("paths")
        path_rows = list(raw_paths) if isinstance(raw_paths, list) else []
        if not path_rows and isinstance(snapshot.get("changed_path_hashes"), dict):
            path_rows = [
                {"path": path, "content_hash": content_hash}
                for path, content_hash in snapshot["changed_path_hashes"].items()
            ]

        snapshot_schema = safe_int(snapshot.get("schema_version"))
        maximum_generation = safe_int(snapshot.get("generation"))
        for raw in path_rows:
            if not isinstance(raw, dict):
                continue
            try:
                path = _normalize_path(raw.get("path"))
            except (TypeError, ValueError):
                continue
            generation = safe_int(raw.get("generation"), maximum_generation)
            first_generation = safe_int(raw.get("first_generation"), generation)
            mutation_count = max(1, safe_int(raw.get("mutation_count"), 1))
            diagnostic_status = _normalize_diagnostic_status(raw.get("diagnostic_status"))
            diagnostic_checker = str(raw.get("diagnostic_checker") or "").strip()
            initial_known = snapshot_schema >= 3 and raw.get("initial_known") is True
            carried = snapshot_schema >= 4 and raw.get("carried") is True
            if new_turn:
                carried = True
            turn_baseline_known = snapshot_schema >= 4 and raw.get("turn_baseline_known") is True
            turn_baseline_hash = (
                str(raw.get("turn_baseline_hash") or "") if turn_baseline_known else ""
            )
            turn_baseline_carried = (
                snapshot_schema >= 4 and raw.get("turn_baseline_carried") is True
            )
            if new_turn:
                turn_baseline_known = True
                turn_baseline_hash = str(raw.get("content_hash") or "")
                turn_baseline_carried = True
            restored_paths[_path_key(path)] = {
                "path": path,
                "content_hash": str(raw.get("content_hash") or ""),
                "category": _classify_path(path),
                "generation": generation,
                "first_generation": min(first_generation, generation),
                "mutation_count": mutation_count,
                "diagnostic_status": diagnostic_status,
                "diagnostic_checker": diagnostic_checker,
                "diagnostic_fresh": (
                    diagnostic_status in {"passed", "failed"} and bool(diagnostic_checker)
                ),
                "initial_known": initial_known,
                "initial_hash": str(raw.get("initial_hash") or "") if initial_known else "",
                "carried": carried,
                "turn_baseline_known": turn_baseline_known,
                "turn_baseline_hash": turn_baseline_hash,
                "turn_baseline_carried": turn_baseline_carried,
            }
            maximum_generation = max(maximum_generation, generation)

        restored_evidence: list[dict[str, Any]] = []
        raw_evidence = snapshot.get("evidence")
        for raw in (raw_evidence if isinstance(raw_evidence, list) else []):
            if not isinstance(raw, dict):
                continue
            command = str(raw.get("command") or "").strip()
            artifact = str(raw.get("artifact_path") or "").strip()
            kind = _classify_command(command, artifact)
            coverage_mode, coverage_targets = _command_coverage(command, kind)
            try:
                exit_code = int(raw.get("exit_code"))
            except (TypeError, ValueError):
                exit_code = -1
            generation = safe_int(raw.get("generation"), maximum_generation)
            maximum_generation = max(maximum_generation, generation)
            restored_evidence.append(
                {
                    "sequence": len(restored_evidence) + 1,
                    "generation": generation,
                    "command": command,
                    "command_key": " ".join(command.casefold().split()),
                    "kind": kind,
                    "verification": kind in _VERIFICATION_KINDS,
                    "status": (
                        "passed"
                        if kind in _VERIFICATION_KINDS and exit_code == 0
                        else "failed"
                        if kind in _VERIFICATION_KINDS
                        else "ignored"
                    ),
                    "exit_code": exit_code,
                    "output": _bounded_output(raw.get("output")),
                    "elapsed_seconds": safe_float(raw.get("elapsed_seconds")),
                    "artifact_path": artifact,
                    # Prompt authority belongs to one live turn.  Never trust
                    # a persisted flag after a continuation or process reload.
                    "explicit_prompt_command": False,
                    "coverage_mode": coverage_mode,
                    "coverage_targets": coverage_targets,
                    "carried": bool(new_turn or (snapshot_schema >= 4 and raw.get("carried") is True)),
                }
            )

        with self._lock:
            self._generation = maximum_generation
            self._paths = restored_paths
            self._evidence = restored_evidence
            self._completion_blocks = (
                0 if new_turn else safe_int(snapshot.get("completion_blocks"))
            )
            self._had_verification_signal = (not new_turn) and (
                bool(snapshot.get("had_verification_signal")) or any(
                    item["diagnostic_fresh"] and not item.get("carried")
                    for item in restored_paths.values()
                ) or any(
                    item["verification"] and not item.get("carried")
                    for item in restored_evidence
                )
            )
        return self

    @classmethod
    def from_snapshot(
        cls,
        snapshot: Any,
        *,
        new_turn: bool = False,
    ) -> VerificationLedger:
        """Construct a ledger from persisted state."""

        return cls().restore(snapshot, new_turn=new_turn)

    def snapshot(self) -> dict[str, Any]:
        """Return a JSON-serializable ledger snapshot."""

        with self._lock:
            # Snapshot state is intentionally strategy-neutral and therefore
            # uses the strict planned policy.  Call decision("direct") when
            # automatic syntax diagnostics are sufficient for a small edit.
            strict = self.decision("planned")
            paths = sorted(self._paths.values(), key=lambda item: item["path"].casefold())
            return {
                "schema_version": self.schema_version,
                "generation": self._generation,
                "state": strict["state"],
                "changed_path_hashes": {
                    item["path"]: item["content_hash"] for item in paths
                },
                "current_changed_path_hashes": {
                    item["path"]: item["content_hash"]
                    for item in paths
                    if not item.get("carried")
                },
                "carried_path_hashes": {
                    item["path"]: item["content_hash"]
                    for item in paths
                    if item.get("carried")
                },
                "paths": copy.deepcopy(paths),
                "evidence": copy.deepcopy(self._evidence),
                "completion_blocks": self._completion_blocks,
                "had_verification_signal": self._had_verification_signal,
            }


__all__ = ["VerificationLedger"]
