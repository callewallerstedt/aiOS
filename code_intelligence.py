"""Lazy, bounded Language Server Protocol navigation for the CODE harness.

The harness never installs or downloads a language server.  When a compatible
server is configured explicitly, present in aiOS's owned tool directory, or
already exists on PATH, one persistent stdio client is shared by project root
and language.  If no server is available, cheap lexical navigation remains
useful and the result says plainly that semantic code intelligence was
unavailable.
"""
from __future__ import annotations

import atexit
import hashlib
import json
import os
import queue
import re
import shlex
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse


CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0
HARNESS_LSP_BIN = Path(__file__).resolve().parent / ".tools" / "lsp" / "node_modules" / ".bin"
MAX_DOCUMENT_BYTES = 2_000_000
MAX_MESSAGE_BYTES = 2_000_000
MAX_RESULTS = 100
MAX_CLIENTS = 8
REQUEST_TIMEOUT_SECONDS = 8.0
LEXICAL_TIMEOUT_SECONDS = 3.0
IGNORED_DIRECTORIES = frozenset({
    ".git", ".hg", ".svn", ".venv", "venv", "node_modules", "dist",
    "build", "target", "vendor", "__pycache__", ".next", ".nuxt",
    ".mypy_cache", ".pytest_cache", ".ruff_cache", ".gradle", ".tools",
})


@dataclass(frozen=True)
class LanguageSpec:
    key: str
    extensions: frozenset[str]
    commands: tuple[tuple[str, ...], ...]
    env_name: str


LANGUAGES: tuple[LanguageSpec, ...] = (
    LanguageSpec(
        "python", frozenset({".py", ".pyi"}),
        (("basedpyright-langserver", "--stdio"), ("pyright-langserver", "--stdio"), ("pylsp",)),
        "AIOS_LSP_PYTHON_COMMAND",
    ),
    LanguageSpec(
        "typescript", frozenset({".ts", ".tsx", ".mts", ".cts"}),
        (("typescript-language-server", "--stdio"),), "AIOS_LSP_TYPESCRIPT_COMMAND",
    ),
    LanguageSpec(
        "javascript", frozenset({".js", ".jsx", ".mjs", ".cjs"}),
        (("typescript-language-server", "--stdio"),), "AIOS_LSP_JAVASCRIPT_COMMAND",
    ),
    LanguageSpec("rust", frozenset({".rs"}), (("rust-analyzer",),), "AIOS_LSP_RUST_COMMAND"),
    LanguageSpec("go", frozenset({".go"}), (("gopls", "serve"),), "AIOS_LSP_GO_COMMAND"),
    LanguageSpec(
        "cpp", frozenset({".c", ".cc", ".cpp", ".cxx", ".h", ".hh", ".hpp", ".hxx"}),
        (("clangd",),), "AIOS_LSP_CPP_COMMAND",
    ),
    LanguageSpec("csharp", frozenset({".cs"}), (("omnisharp", "-lsp"),), "AIOS_LSP_CSHARP_COMMAND"),
    LanguageSpec("lua", frozenset({".lua"}), (("lua-language-server",),), "AIOS_LSP_LUA_COMMAND"),
    LanguageSpec("java", frozenset({".java"}), (("jdtls",),), "AIOS_LSP_JAVA_COMMAND"),
    LanguageSpec("css", frozenset({".css"}), (("vscode-css-language-server", "--stdio"),), "AIOS_LSP_CSS_COMMAND"),
    LanguageSpec("html", frozenset({".html", ".htm"}), (("vscode-html-language-server", "--stdio"),), "AIOS_LSP_HTML_COMMAND"),
    LanguageSpec("json", frozenset({".json", ".jsonc"}), (("vscode-json-language-server", "--stdio"),), "AIOS_LSP_JSON_COMMAND"),
)
_SPEC_BY_EXTENSION = {
    extension: spec for spec in LANGUAGES for extension in spec.extensions
}


class LspFailure(RuntimeError):
    """A bounded, user-safe LSP startup or request failure."""


def _short(value: Any, limit: int = 600) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    return text if len(text) <= limit else text[:limit] + "..."


def _language_for_path(path: Path) -> LanguageSpec | None:
    return _SPEC_BY_EXTENSION.get(path.suffix.casefold())


def _configured_command(spec: LanguageSpec) -> tuple[str, ...] | None:
    raw = str(os.environ.get(spec.env_name) or "").strip()
    if not raw:
        return None
    try:
        parsed = shlex.split(raw, posix=os.name != "nt")
    except ValueError:
        return None
    parts = tuple(
        part[1:-1] if len(part) >= 2 and part[0] == part[-1] and part[0] in {'"', "'"} else part
        for part in parsed
    )
    return parts or None


_DISCOVERY_LOCK = threading.Lock()
_DISCOVERY_CACHE: dict[str, tuple[float, tuple[str, ...] | None]] = {}


def _resolve_explicit_executable(command: tuple[str, ...] | None) -> tuple[str, ...] | None:
    if not command:
        return None
    executable = Path(command[0]).expanduser()
    found = str(executable.resolve()) if executable.is_file() else shutil.which(command[0])
    return (found, *command[1:]) if found else None


def _harness_executable(program: str, *, windows: bool | None = None) -> str | None:
    """Resolve npm/POSIX wrappers owned by aiOS without consulting PATH."""
    use_windows = os.name == "nt" if windows is None else bool(windows)
    suffixes = (".cmd", ".exe", "") if use_windows else ("", ".cmd", ".exe")
    name = Path(str(program or "")).name
    if not name or name != str(program):
        return None
    for suffix in suffixes:
        candidate = HARNESS_LSP_BIN / f"{name}{suffix}"
        if candidate.is_file():
            return str(candidate.resolve())
    return None


def _harness_tsserver_path(spec: LanguageSpec, command: tuple[str, ...]) -> Path | None:
    """Return the owned tsserver entrypoint only for the owned JS/TS LSP."""
    if spec.key not in {"typescript", "javascript"} or not command:
        return None
    try:
        executable = Path(command[0]).resolve()
        node_modules = HARNESS_LSP_BIN.parent.resolve()
        owned_roots = (
            HARNESS_LSP_BIN.resolve(),
            (node_modules / "typescript-language-server").resolve(),
        )
        if not any(executable.is_relative_to(root) for root in owned_roots):
            return None
    except OSError:
        return None
    candidate = node_modules / "typescript" / "lib" / "tsserver.js"
    return candidate.resolve() if candidate.is_file() else None


def _command_for_language(spec: LanguageSpec) -> tuple[str, ...] | None:
    """Resolve an already-installed server without invoking a package manager."""
    configured = _configured_command(spec)
    cache_key = f"{spec.key}:{configured!r}:{HARNESS_LSP_BIN}"
    now = time.monotonic()
    with _DISCOVERY_LOCK:
        cached = _DISCOVERY_CACHE.get(cache_key)
        if cached and now - cached[0] < 30.0:
            return cached[1]
    resolved = _resolve_explicit_executable(configured)
    if not resolved:
        for candidate in spec.commands:
            found = _harness_executable(candidate[0])
            if found:
                resolved = (found, *candidate[1:])
                break
    if not resolved:
        for candidate in spec.commands:
            found = shutil.which(candidate[0])
            if found:
                resolved = (found, *candidate[1:])
                break
    with _DISCOVERY_LOCK:
        _DISCOVERY_CACHE[cache_key] = (now, resolved)
    return resolved


def _document_language_id(spec: LanguageSpec, path: Path) -> str:
    suffix = path.suffix.casefold()
    if suffix in {".tsx", ".jsx"}:
        return "typescriptreact" if suffix == ".tsx" else "javascriptreact"
    if suffix == ".jsonc":
        return "jsonc"
    return spec.key


def _read_text(path: Path) -> str:
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise LspFailure(f"could not read {path.name}: {_short(exc)}") from exc
    if len(raw) > MAX_DOCUMENT_BYTES:
        raise LspFailure(f"{path.name} exceeds the {MAX_DOCUMENT_BYTES:,}-byte code-intelligence limit")
    try:
        return raw.decode("utf-8-sig", errors="strict")
    except UnicodeError as exc:
        raise LspFailure(f"{path.name} is not UTF-8 text") from exc


def _utf16_position(text: str, line: int, character: int) -> dict[str, int]:
    lines = text.splitlines()
    line_index = max(0, min(int(line or 1) - 1, max(0, len(lines) - 1)))
    current = lines[line_index] if lines else ""
    codepoint_index = max(0, min(int(character or 1) - 1, len(current)))
    utf16_units = len(current[:codepoint_index].encode("utf-16-le")) // 2
    return {"line": line_index, "character": utf16_units}


def _identifier_at(text: str, line: int, character: int, supplied: str = "") -> str:
    explicit = str(supplied or "").strip()
    if re.fullmatch(r"[A-Za-z_$][\w$]*", explicit):
        return explicit
    rows = text.splitlines()
    if not rows:
        return ""
    row = rows[max(0, min(int(line or 1) - 1, len(rows) - 1))]
    column = max(0, min(int(character or 1) - 1, len(row)))
    for match in re.finditer(r"[A-Za-z_$][\w$]*", row):
        if match.start() <= column <= match.end():
            return match.group(0)
    return ""


class LspClient:
    """Minimal persistent JSON-RPC/LSP stdio client with bounded I/O."""

    def __init__(self, root: Path, spec: LanguageSpec, command: tuple[str, ...]):
        self.root = root.resolve()
        self.spec = spec
        self.command = command
        self.process: subprocess.Popen[bytes] | None = None
        self.capabilities: dict[str, Any] = {}
        self.server_name = Path(command[0]).name
        self._next_id = 0
        self._pending: dict[int, queue.Queue[dict[str, Any]]] = {}
        self._pending_lock = threading.Lock()
        self._write_lock = threading.Lock()
        self._start_lock = threading.Lock()
        self._documents_lock = threading.Lock()
        self._documents: dict[str, dict[str, Any]] = {}
        self._diagnostics: dict[str, list[dict[str, Any]]] = {}
        self._diagnostic_events: dict[str, threading.Event] = {}
        self._stderr_parts: list[str] = []
        self.last_used = time.monotonic()

    @property
    def alive(self) -> bool:
        return bool(self.process and self.process.poll() is None)

    def _send(self, payload: dict[str, Any]) -> None:
        process = self.process
        if not process or not process.stdin or process.poll() is not None:
            raise LspFailure("language server stopped")
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        if len(body) > MAX_MESSAGE_BYTES:
            raise LspFailure("language-server request exceeded the bounded message size")
        packet = f"Content-Length: {len(body)}\r\n\r\n".encode("ascii") + body
        try:
            with self._write_lock:
                process.stdin.write(packet)
                process.stdin.flush()
        except OSError as exc:
            raise LspFailure(f"language-server write failed: {_short(exc)}") from exc

    def _request(self, method: str, params: dict[str, Any], timeout: float) -> Any:
        self._next_id += 1
        request_id = self._next_id
        response_queue: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=1)
        with self._pending_lock:
            self._pending[request_id] = response_queue
        try:
            self._send({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
            try:
                response = response_queue.get(timeout=max(0.1, timeout))
            except queue.Empty as exc:
                raise LspFailure(f"{method} timed out after {timeout:.1f}s") from exc
        finally:
            with self._pending_lock:
                self._pending.pop(request_id, None)
        if response.get("error"):
            error = response.get("error") or {}
            raise LspFailure(f"{method} failed: {_short(error.get('message') or error)}")
        return response.get("result")

    def _notify(self, method: str, params: dict[str, Any]) -> None:
        self._send({"jsonrpc": "2.0", "method": method, "params": params})

    def _read_loop(self) -> None:
        process = self.process
        stream = process.stdout if process else None
        if stream is None:
            return
        try:
            while process and process.poll() is None:
                content_length: int | None = None
                header_bytes = 0
                while True:
                    line = stream.readline()
                    if not line:
                        return
                    header_bytes += len(line)
                    if header_bytes > 16_384:
                        raise LspFailure("language-server response headers were too large")
                    if line in {b"\r\n", b"\n"}:
                        break
                    name, separator, value = line.decode("ascii", errors="replace").partition(":")
                    if separator and name.strip().casefold() == "content-length":
                        content_length = int(value.strip())
                if content_length is None or not 0 <= content_length <= MAX_MESSAGE_BYTES:
                    raise LspFailure("language-server response exceeded the bounded message size")
                raw = stream.read(content_length)
                if len(raw) != content_length:
                    return
                message = json.loads(raw.decode("utf-8", errors="strict"))
                if not isinstance(message, dict):
                    continue
                if "id" in message and "method" not in message:
                    try:
                        request_id = int(message["id"])
                    except (TypeError, ValueError):
                        continue
                    with self._pending_lock:
                        pending = self._pending.get(request_id)
                    if pending:
                        try:
                            pending.put_nowait(message)
                        except queue.Full:
                            pass
                    continue
                method = str(message.get("method") or "")
                if method == "textDocument/publishDiagnostics":
                    params = message.get("params") or {}
                    uri = str(params.get("uri") or "")
                    diagnostics = params.get("diagnostics") or []
                    if uri:
                        uri = _canonical_file_uri(uri)
                        self._diagnostics[uri] = diagnostics if isinstance(diagnostics, list) else []
                        self._diagnostic_events.setdefault(uri, threading.Event()).set()
                elif "id" in message:
                    self._send({
                        "jsonrpc": "2.0", "id": message.get("id"),
                        "error": {"code": -32601, "message": "Method not supported by aiOS CODE"},
                    })
        except Exception as exc:
            self._stderr_parts.append(_short(exc))

    def _stderr_loop(self) -> None:
        process = self.process
        stream = process.stderr if process else None
        if stream is None:
            return
        try:
            while sum(len(part) for part in self._stderr_parts) < 4_000:
                line = stream.readline()
                if not line:
                    return
                self._stderr_parts.append(line.decode("utf-8", errors="replace")[:500])
        except OSError:
            return

    def start(self, timeout: float = REQUEST_TIMEOUT_SECONDS) -> None:
        if self.alive:
            return
        with self._start_lock:
            if self.alive:
                return
            try:
                self.process = subprocess.Popen(
                    list(self.command), cwd=str(self.root), stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0,
                    creationflags=CREATE_NO_WINDOW,
                )
            except OSError as exc:
                raise LspFailure(f"could not start {self.server_name}: {_short(exc)}") from exc
            threading.Thread(target=self._read_loop, name=f"aios-lsp-{self.spec.key}", daemon=True).start()
            threading.Thread(target=self._stderr_loop, name=f"aios-lsp-{self.spec.key}-stderr", daemon=True).start()
            try:
                initialize_params: dict[str, Any] = {
                    "processId": os.getpid(),
                    "clientInfo": {"name": "aiOS CODE", "version": "1"},
                    "rootUri": self.root.as_uri(),
                    "capabilities": {
                        "general": {"positionEncodings": ["utf-16"]},
                        "textDocument": {
                            "synchronization": {"didSave": True},
                            "definition": {}, "references": {}, "documentSymbol": {},
                            "hover": {}, "implementation": {}, "diagnostic": {},
                            "publishDiagnostics": {
                                "relatedInformation": True,
                                "tagSupport": {"valueSet": [1, 2]},
                            },
                        },
                        "workspace": {"workspaceFolders": True},
                    },
                    "workspaceFolders": [{"uri": self.root.as_uri(), "name": self.root.name}],
                }
                tsserver_path = _harness_tsserver_path(self.spec, self.command)
                if tsserver_path:
                    initialize_params["initializationOptions"] = {
                        "tsserver": {"path": str(tsserver_path)},
                    }
                result = self._request("initialize", initialize_params, timeout)
                self.capabilities = dict((result or {}).get("capabilities") or {})
                self._notify("initialized", {})
            except Exception:
                self.close()
                raise

    def close(self) -> None:
        process, self.process = self.process, None
        if not process:
            return
        try:
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=1)
        except Exception:
            try:
                process.kill()
            except OSError:
                pass

    def _supports(self, operation: str) -> bool:
        capability = {
            "definition": "definitionProvider",
            "references": "referencesProvider",
            "symbols": "documentSymbolProvider",
            "hover": "hoverProvider",
            "implementations": "implementationProvider",
            "diagnostics": "diagnosticProvider",
        }[operation]
        if operation == "diagnostics":
            # Most servers publish diagnostics and therefore omit the newer
            # pull-diagnostics capability.
            return True
        return bool(self.capabilities.get(capability))

    def _sync_document(self, path: Path) -> tuple[str, str]:
        text = _read_text(path)
        uri = path.as_uri()
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        with self._documents_lock:
            existing = self._documents.get(uri)
            if not existing:
                self._documents[uri] = {"digest": digest, "version": 1, "text": text}
                self._diagnostic_events.setdefault(uri, threading.Event()).clear()
                self._notify("textDocument/didOpen", {"textDocument": {
                    "uri": uri, "languageId": _document_language_id(self.spec, path),
                    "version": 1, "text": text,
                }})
            elif existing.get("digest") != digest:
                version = int(existing.get("version") or 1) + 1
                existing.update(digest=digest, version=version, text=text)
                self._diagnostics.pop(uri, None)
                self._diagnostic_events.setdefault(uri, threading.Event()).clear()
                self._notify("textDocument/didChange", {
                    "textDocument": {"uri": uri, "version": version},
                    "contentChanges": [{"text": text}],
                })
        return uri, text

    def refresh_if_open(self, path: Path) -> None:
        """Notify an existing client after an edit; never starts a new client."""
        uri = path.as_uri()
        with self._documents_lock:
            opened = uri in self._documents
        if not opened or not self.alive:
            return
        if not path.is_file():
            with self._documents_lock:
                self._documents.pop(uri, None)
                self._diagnostics.pop(uri, None)
            try:
                self._notify("textDocument/didClose", {"textDocument": {"uri": uri}})
            except LspFailure:
                pass
            return
        try:
            self._sync_document(path)
        except LspFailure:
            return

    def query(
        self, path: Path, operation: str, line: int, character: int,
        max_results: int, timeout: float,
    ) -> dict[str, Any]:
        self.start(timeout)
        self.last_used = time.monotonic()
        if not self._supports(operation):
            raise LspFailure(f"{self.server_name} does not advertise {operation} support")
        uri, text = self._sync_document(path)
        position = _utf16_position(text, line, character)
        document = {"uri": uri}
        if operation == "diagnostics":
            if self.capabilities.get("diagnosticProvider"):
                result = self._request("textDocument/diagnostic", {"textDocument": document}, timeout)
                raw = (result or {}).get("items") if isinstance(result, dict) else []
            else:
                event = self._diagnostic_events.setdefault(uri, threading.Event())
                # Push-diagnostic servers can spend more than a second loading
                # a project before their first publication.  Keep the same
                # strict request timeout instead of returning a false clean bill.
                event.wait(timeout=timeout)
                raw = self._diagnostics.get(uri, [])
            return {"diagnostics": _normalise_diagnostics(raw, max_results)}
        method = {
            "definition": "textDocument/definition",
            "references": "textDocument/references",
            "symbols": "textDocument/documentSymbol",
            "hover": "textDocument/hover",
            "implementations": "textDocument/implementation",
        }[operation]
        params: dict[str, Any] = {"textDocument": document}
        if operation != "symbols":
            params["position"] = position
        if operation == "references":
            params["context"] = {"includeDeclaration": True}
        result = self._request(method, params, timeout)
        if operation in {"definition", "references", "implementations"}:
            return {"locations": _normalise_locations(self.root, result, max_results)}
        if operation == "symbols":
            return {"symbols": _normalise_symbols(self.root, path, result, max_results)}
        return {"hover": _normalise_hover(result)}


def _path_from_uri(uri: str) -> Path | None:
    parsed = urlparse(str(uri or ""))
    if parsed.scheme != "file":
        return None
    raw = unquote(parsed.path)
    if os.name == "nt" and re.match(r"^/[A-Za-z]:/", raw):
        raw = raw[1:]
    return Path(raw).resolve()


def _canonical_file_uri(uri: str) -> str:
    """Match server-emitted URI spelling to the Path.as_uri client key."""
    path = _path_from_uri(uri)
    return path.as_uri() if path else str(uri or "")


def _range(raw: Any) -> dict[str, int]:
    value = raw if isinstance(raw, dict) else {}
    start = value.get("start") if isinstance(value.get("start"), dict) else {}
    end = value.get("end") if isinstance(value.get("end"), dict) else {}
    return {
        "line": int(start.get("line") or 0) + 1,
        "character": int(start.get("character") or 0) + 1,
        "end_line": int(end.get("line") or 0) + 1,
        "end_character": int(end.get("character") or 0) + 1,
    }


def _relative(root: Path, path: Path) -> str | None:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except (OSError, ValueError):
        return None


def _normalise_locations(root: Path, raw: Any, limit: int) -> list[dict[str, Any]]:
    values = raw if isinstance(raw, list) else ([raw] if isinstance(raw, dict) else [])
    rows: list[dict[str, Any]] = []
    for value in values:
        uri = str(value.get("uri") or value.get("targetUri") or "")
        path = _path_from_uri(uri)
        relative = _relative(root, path) if path else None
        if relative is None:
            continue
        location_range = value.get("range") or value.get("targetSelectionRange") or value.get("targetRange")
        rows.append({"path": relative, **_range(location_range)})
        if len(rows) >= limit:
            break
    return rows


def _normalise_diagnostics(raw: Any, limit: int) -> list[dict[str, Any]]:
    values = raw if isinstance(raw, list) else []
    rows: list[dict[str, Any]] = []
    for value in values[:limit]:
        if not isinstance(value, dict):
            continue
        row: dict[str, Any] = {
            **_range(value.get("range")),
            "message": _short(value.get("message"), 500),
        }
        for key in ("severity", "source", "code"):
            if value.get(key) is not None:
                row[key] = value[key]
        rows.append(row)
    return rows


def _normalise_symbols(root: Path, path: Path, raw: Any, limit: int) -> list[dict[str, Any]]:
    values = raw if isinstance(raw, list) else []
    rows: list[dict[str, Any]] = []

    def visit(items: list[Any], depth: int = 0) -> None:
        for value in items:
            if len(rows) >= limit:
                return
            if not isinstance(value, dict):
                continue
            location = value.get("location") if isinstance(value.get("location"), dict) else {}
            symbol_range = value.get("selectionRange") or value.get("range") or location.get("range")
            rows.append({
                "name": _short(value.get("name"), 160),
                "kind": value.get("kind"),
                "path": _relative(root, path) or path.name,
                "depth": depth,
                **_range(symbol_range),
            })
            children = value.get("children")
            if isinstance(children, list):
                visit(children, depth + 1)

    visit(values)
    return rows


def _normalise_hover(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    contents = raw.get("contents")
    if isinstance(contents, dict):
        text = str(contents.get("value") or "")
        kind = str(contents.get("kind") or "plaintext")
    elif isinstance(contents, list):
        text = "\n".join(
            str(item.get("value") if isinstance(item, dict) else item) for item in contents
        )
        kind = "plaintext"
    else:
        text, kind = str(contents or ""), "plaintext"
    return {"contents": text[:2_000], "kind": kind, **_range(raw.get("range"))}


_CLIENTS_LOCK = threading.Lock()
_CLIENTS: dict[tuple[str, str], LspClient] = {}


def _get_client(root: Path, spec: LanguageSpec) -> tuple[LspClient | None, str]:
    command = _command_for_language(spec)
    if not command:
        return None, f"No installed {spec.key} language server was found"
    key = (str(root.resolve()).casefold(), spec.key)
    with _CLIENTS_LOCK:
        existing = _CLIENTS.get(key)
        if existing and existing.alive:
            return existing, ""
        if existing:
            existing.close()
            _CLIENTS.pop(key, None)
        while len(_CLIENTS) >= MAX_CLIENTS:
            oldest_key = min(_CLIENTS, key=lambda item: _CLIENTS[item].last_used)
            _CLIENTS.pop(oldest_key).close()
        client = LspClient(root, spec, command)
        _CLIENTS[key] = client
        return client, ""


def notify_path_changed(root: Path, path: Path) -> None:
    """Refresh matching open documents without creating a server or blocking edits."""
    root = root.resolve()
    path = path.resolve()
    spec = _language_for_path(path)
    if not spec:
        return
    key = (str(root).casefold(), spec.key)
    with _CLIENTS_LOCK:
        client = _CLIENTS.get(key)
    if client:
        client.refresh_if_open(path)


def close_all() -> None:
    with _CLIENTS_LOCK:
        clients = list(_CLIENTS.values())
        _CLIENTS.clear()
    for client in clients:
        client.close()


atexit.register(close_all)


_DEFINITION_RE = (
    r"^\s*(?:export\s+|public\s+|private\s+|protected\s+|static\s+|final\s+|async\s+|pub\s+)*"
    r"(?:def|class|function|fn|func|struct|trait|interface|type|enum|impl|const|let|var)\s+{name}\b"
    r"|^\s*{name}\s*(?:=|:)"
)
_OUTLINE_RE = re.compile(
    r"^\s*(?:export\s+|public\s+|private\s+|protected\s+|static\s+|final\s+|async\s+|pub\s+)*"
    r"(?:def|class|function|fn|func|struct|trait|interface|type|enum|impl|const|let|var)\s+"
    r"(?P<declared>[A-Za-z_$][\w$]*)\b"
    r"|^\s*(?P<assigned>[A-Za-z_$][\w$]*)\s*(?:=|:)"
)


def _iter_lexical_files(root: Path, target: Path, spec: LanguageSpec):
    yielded: set[Path] = set()
    if target.is_file():
        yielded.add(target)
        yield target
    count = 0
    for base, directories, files in os.walk(root):
        directories[:] = [
            name for name in directories
            if name not in IGNORED_DIRECTORIES and not (Path(base) / name).is_symlink()
        ]
        for name in files:
            candidate = (Path(base) / name).resolve()
            if candidate in yielded or candidate.suffix.casefold() not in spec.extensions:
                continue
            if _relative(root, candidate) is None:
                continue
            yielded.add(candidate)
            yield candidate
            count += 1
            if count >= 400:
                return


def _lexical_query(
    root: Path, path: Path, spec: LanguageSpec, operation: str,
    symbol: str, line: int, character: int, max_results: int, reason: str,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "ok": True,
        "engine": "lexical",
        "language": spec.key,
        "server": {"available": False, "reason": _short(reason)},
    }
    if operation == "diagnostics":
        result.update(diagnostics=[], note="Diagnostics require an installed language server; edit diagnostics remain available separately.")
        return result
    text = _read_text(path)
    identifier = _identifier_at(text, line, character, symbol)
    if operation in {"hover", "implementations"}:
        result.update(
            **({"hover": None} if operation == "hover" else {"locations": []}),
            note=f"{operation} requires semantic language-server support.",
        )
        return result
    if operation in {"definition", "references"} and not identifier:
        result.update(locations=[], note="Pass symbol, or line and character on an identifier.")
        return result
    deadline = time.monotonic() + LEXICAL_TIMEOUT_SECONDS
    rows: list[dict[str, Any]] = []
    if operation == "symbols":
        pattern = _OUTLINE_RE
        candidates = (path,)
    else:
        pattern = re.compile(
            _DEFINITION_RE.format(name=re.escape(identifier))
            if operation == "definition" else rf"\b{re.escape(identifier)}\b"
        )
        candidates = _iter_lexical_files(root, path, spec)
    truncated = False
    for candidate in candidates:
        if time.monotonic() >= deadline:
            truncated = True
            break
        try:
            if candidate.stat().st_size > MAX_DOCUMENT_BYTES:
                continue
            candidate_text = candidate.read_text(encoding="utf-8-sig", errors="strict")
        except (OSError, UnicodeError):
            continue
        for index, row_text in enumerate(candidate_text.splitlines(), start=1):
            match = pattern.search(row_text)
            if not match:
                continue
            name = (
                match.groupdict().get("declared") or match.groupdict().get("assigned")
                if operation == "symbols" else identifier
            )
            rows.append({
                "path": _relative(root, candidate), "line": index,
                "character": match.start() + 1, "name": name,
                "text": row_text.strip()[:240],
            })
            if len(rows) >= max_results:
                truncated = True
                break
        if truncated:
            break
    result["symbols" if operation == "symbols" else "locations"] = rows
    result["symbol"] = identifier or None
    result["truncated"] = truncated
    result["note"] = "Lexical fallback can miss dynamic or aliased relationships."
    return result


def query(root: Path, args: dict[str, Any]) -> dict[str, Any]:
    """Run one bounded semantic operation from a project-relative or absolute path."""
    root = root.expanduser().resolve()
    raw_path = str(args.get("relative_path") or "").strip()
    if not raw_path:
        return {"error": "relative_path is required"}
    requested = Path(raw_path).expanduser()
    path = requested.resolve() if requested.is_absolute() else (root / requested).resolve()
    try:
        path.relative_to(root)
    except ValueError:
        # A file outside the session's starting project is still a valid target.
        # Scope its language server and lexical fallback to the containing folder
        # so cross-project access does not turn into an unbounded drive scan.
        root = path.parent
    if not path.is_file():
        return {"error": f"File not found: {raw_path}"}
    spec = _language_for_path(path)
    if not spec:
        return {"error": f"No code-intelligence language mapping for {path.suffix or 'this file type'}"}
    operation = str(args.get("operation") or "").strip().casefold()
    if operation not in {"diagnostics", "definition", "references", "symbols", "hover", "implementations"}:
        return {"error": "operation must be diagnostics, definition, references, symbols, hover, or implementations"}
    line = max(1, int(args.get("line") or 1))
    character = max(1, int(args.get("character") or 1))
    max_results = max(1, min(int(args.get("max_results") or 40), MAX_RESULTS))
    timeout = max(0.5, min(float(args.get("timeout_seconds") or REQUEST_TIMEOUT_SECONDS), REQUEST_TIMEOUT_SECONDS))
    symbol = str(args.get("symbol") or "").strip()
    if symbol and args.get("line") is None and args.get("character") is None:
        try:
            source = _read_text(path)
        except LspFailure as exc:
            return {"error": str(exc), "language": spec.key}
        match = re.search(rf"\b{re.escape(symbol)}\b", source)
        if match:
            line = source.count("\n", 0, match.start()) + 1
            line_start = source.rfind("\n", 0, match.start()) + 1
            character = match.start() - line_start + 1
    client, unavailable = _get_client(root, spec)
    if client:
        try:
            payload = client.query(path, operation, line, character, max_results, timeout)
            return {
                "ok": True, "engine": "lsp", "language": spec.key,
                "server": {"available": True, "name": client.server_name},
                "path": path.relative_to(root).as_posix(), "operation": operation,
                **payload,
            }
        except LspFailure as exc:
            unavailable = str(exc)
    try:
        payload = _lexical_query(
            root, path, spec, operation, symbol, line, character, max_results, unavailable,
        )
        payload.update(path=path.relative_to(root).as_posix(), operation=operation)
        return payload
    except LspFailure as exc:
        return {"error": str(exc), "engine": "lexical", "language": spec.key}
