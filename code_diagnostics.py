"""Fast, side-effect-free diagnostics for aiOS file mutations.

These checks are deliberately cheap enough to run after every local edit.  They
are not a substitute for the project's test suite or an LSP; they are the first
line of feedback while a language server is unavailable or still warming up.
"""

from __future__ import annotations

import ast
import json
import os
import re
import shutil
import subprocess
import tomllib
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass
from pathlib import Path


CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0
_HARNESS_TOOLS = Path(__file__).resolve().parent / ".tools" / "lsp" / "node_modules"
_BINARY_SUFFIXES = frozenset({
    ".avif", ".bmp", ".eot", ".flac", ".gif", ".ico", ".jpeg", ".jpg",
    ".m4a", ".mov", ".mp3", ".mp4", ".ogg", ".otf", ".pdf", ".png",
    ".tif", ".tiff", ".ttf", ".wav", ".webm", ".webp", ".woff", ".woff2",
})


@dataclass(frozen=True)
class DiagnosticResult:
    status: str
    checker: str
    message: str = ""
    line: int = 0
    column: int = 0

    def as_dict(self) -> dict:
        return asdict(self)


def _passed(checker: str, message: str = "Syntax OK") -> DiagnosticResult:
    return DiagnosticResult("passed", checker, message)


def _coordinate(value: object) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError, OverflowError):
        return 0


def _exception_location(exc: BaseException, text: str = "") -> tuple[int, int]:
    """Normalize the location shapes exposed by the stdlib parsers.

    SyntaxError uses ``lineno``/``offset``, JSON and newer tomllib use
    ``lineno``/``colno``, while ElementTree exposes a ``(line, column)``
    ``position`` tuple. Older tomllib versions only expose a character ``pos``.
    """
    line = _coordinate(getattr(exc, "lineno", 0))
    column = _coordinate(getattr(exc, "colno", 0))
    if not column:
        column = _coordinate(getattr(exc, "offset", 0))

    position = getattr(exc, "position", None)
    if isinstance(position, (tuple, list)) and len(position) >= 2:
        if not line:
            line = _coordinate(position[0])
        if not column:
            column = _coordinate(position[1])

    # Some Python versions put the location only in the exception text.
    match = re.search(r"\bline\s+(\d+)(?:\s*,?\s*column\s+(\d+))?", str(exc), re.IGNORECASE)
    if match:
        if not line:
            line = _coordinate(match.group(1))
        if not column and match.group(2) is not None:
            column = _coordinate(match.group(2))

    # TOMLDecodeError historically exposed only a zero-based character offset.
    raw_pos = getattr(exc, "pos", None)
    if text and raw_pos is not None and (not line or not column):
        try:
            pos = min(len(text), max(0, int(raw_pos)))
        except (TypeError, ValueError, OverflowError):
            pos = -1
        if pos >= 0:
            prefix = text[:pos]
            if not line:
                line = prefix.count("\n") + 1
            if not column:
                column = pos - prefix.rfind("\n")

    return line, column


def _failed(checker: str, exc: BaseException, text: str = "") -> DiagnosticResult:
    line, column = _exception_location(exc, text)
    return DiagnosticResult(
        "failed",
        checker,
        str(exc).strip()[:1200],
        line,
        column,
    )


def _node_location(output: str) -> tuple[int, int]:
    """Extract Node's ``path:line`` header and caret column."""
    line_match = re.search(r"(?m)^.*:(\d+)\s*$", output)
    caret_match = re.search(r"(?m)^([ \t]*)\^", output)
    line = _coordinate(line_match.group(1)) if line_match else 0
    column = len(caret_match.group(1).expandtabs(8)) + 1 if caret_match else 0
    return line, column


def _lua_location(output: str) -> tuple[int, int]:
    """Extract luaparse's stable ``[line:column]`` syntax location."""

    match = re.search(r"\[(\d+):(\d+)\]", str(output or ""))
    if not match:
        return 0, 0
    return _coordinate(match.group(1)), _coordinate(match.group(2))


def _luaparse_path() -> Path | None:
    """Return the pinned harness parser; never install packages at runtime."""

    parser = _HARNESS_TOOLS / "luaparse" / "luaparse.js"
    return parser if parser.is_file() else None


def diagnose_path(path: str | Path, *, timeout_seconds: float = 12.0) -> DiagnosticResult:
    """Return a fresh syntax verdict for one file, without creating artifacts."""
    target = Path(path)
    if not target.is_file():
        return DiagnosticResult("unavailable", "none", "File is not available for diagnostics")
    suffix = target.suffix.casefold()
    if suffix in _BINARY_SUFFIXES:
        return DiagnosticResult("not_applicable", "none", "Binary asset has no text syntax check")
    try:
        text = target.read_text(encoding="utf-8-sig", errors="strict")
    except (OSError, UnicodeError) as exc:
        return _failed("utf-8", exc)

    checker = {
        ".py": "python-ast",
        ".json": "json",
        ".toml": "tomllib",
        ".xml": "xml",
        ".svg": "xml",
        ".csproj": "xml",
        ".props": "xml",
        ".targets": "xml",
        ".js": "node --check",
        ".mjs": "node --check",
        ".cjs": "node --check",
        ".lua": "luaparse (LuaJIT)",
    }.get(suffix, "syntax")

    try:
        if suffix == ".py":
            ast.parse(text, filename=str(target))
            return _passed("python-ast")
        if suffix == ".json":
            json.loads(text)
            return _passed("json")
        if suffix == ".toml":
            tomllib.loads(text)
            return _passed("tomllib")
        if suffix in {".xml", ".svg", ".csproj", ".props", ".targets"}:
            ET.fromstring(text)
            return _passed("xml")
        if suffix in {".js", ".mjs", ".cjs"}:
            node = shutil.which("node")
            if not node:
                return DiagnosticResult("unavailable", "node --check", "Node.js is not installed")
            # Node 24 can return success for ``--check path/to/file.js`` when
            # syntax detection reclassifies an ambiguous .js file as ESM. Feed
            # the source over stdin with an explicit grammar so a broken browser
            # module cannot be recorded as a passing automatic diagnostic.
            input_types = {
                ".mjs": ("module",),
                ".cjs": ("commonjs",),
                ".js": ("module", "commonjs"),
            }[suffix]
            failures: list[str] = []
            for input_type in input_types:
                result = subprocess.run(
                    [node, f"--input-type={input_type}", "--check"],
                    cwd=str(target.parent),
                    input=text,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=max(1.0, float(timeout_seconds)),
                    creationflags=CREATE_NO_WINDOW,
                )
                output = (result.stderr or result.stdout or "").strip()
                if not result.returncode:
                    return _passed("node --check")
                failures.append(output)
            output = failures[0] if failures else "JavaScript syntax check failed"
            line, column = _node_location(output)
            return DiagnosticResult("failed", "node --check", output[-1200:], line, column)
        if suffix == ".lua":
            node = shutil.which("node")
            parser = _luaparse_path()
            if not node or parser is None:
                return DiagnosticResult(
                    "unavailable",
                    "luaparse (LuaJIT)",
                    "Pinned aiOS luaparse or Node.js is not installed",
                )
            script = (
                "const fs=require('fs');const lua=require(process.argv[1]);"
                "lua.parse(fs.readFileSync(process.argv[2],'utf8'),{luaVersion:'LuaJIT'});"
            )
            result = subprocess.run(
                [node, "-e", script, str(parser), str(target)],
                cwd=str(target.parent),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=max(1.0, float(timeout_seconds)),
                creationflags=CREATE_NO_WINDOW,
            )
            output = (result.stderr or result.stdout or "").strip()
            if result.returncode:
                line, column = _lua_location(output)
                return DiagnosticResult(
                    "failed", "luaparse (LuaJIT)", output[-1200:], line, column
                )
            return _passed("luaparse (LuaJIT)")
    except (SyntaxError, ValueError, ET.ParseError, subprocess.SubprocessError, OSError) as exc:
        return _failed(checker, exc, text)

    return DiagnosticResult("not_applicable", "none", "No fast syntax checker for this file type")
