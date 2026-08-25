from __future__ import annotations

from pathlib import Path
import json
import io

import pytest

import code_intelligence
import code_jobs


@pytest.fixture(autouse=True)
def isolated_clients(monkeypatch):
    code_intelligence.close_all()
    code_intelligence._DISCOVERY_CACHE.clear()
    for spec in code_intelligence.LANGUAGES:
        monkeypatch.delenv(spec.env_name, raising=False)
    yield
    code_intelligence.close_all()


def test_no_server_uses_bounded_lexical_definition_and_reports_limit(tmp_path, monkeypatch):
    monkeypatch.setattr(code_intelligence, "_command_for_language", lambda _spec: None)
    (tmp_path / "one.py").write_text("def target():\n    return 1\n", encoding="utf-8")
    (tmp_path / "two.py").write_text(
        "from one import target\n\nvalue = target()\nother = target()\n",
        encoding="utf-8",
    )

    definition = code_intelligence.query(tmp_path, {
        "operation": "definition", "relative_path": "two.py", "symbol": "target",
    })
    references = code_intelligence.query(tmp_path, {
        "operation": "references", "relative_path": "two.py", "symbol": "target",
        "max_results": 2,
    })

    assert definition["engine"] == "lexical"
    assert definition["server"]["available"] is False
    assert "No installed python language server" in definition["server"]["reason"]
    assert definition["locations"][0]["path"] == "one.py"
    assert definition["locations"][0]["line"] == 1
    assert len(references["locations"]) == 2
    assert references["truncated"] is True


def test_lexical_symbols_are_file_scoped_and_do_not_repeat_named_groups(tmp_path, monkeypatch):
    monkeypatch.setattr(code_intelligence, "_command_for_language", lambda _spec: None)
    (tmp_path / "app.py").write_text(
        "VALUE = 1\n\nclass Store:\n    pass\n\ndef load():\n    return VALUE\n",
        encoding="utf-8",
    )
    (tmp_path / "other.py").write_text("def outside():\n    pass\n", encoding="utf-8")

    result = code_intelligence.query(tmp_path, {
        "operation": "symbols", "relative_path": "app.py",
    })

    assert [row["name"] for row in result["symbols"]] == ["VALUE", "Store", "load"]
    assert {row["path"] for row in result["symbols"]} == {"app.py"}


def test_query_accepts_cross_project_paths_and_rejects_unknown_operations(tmp_path):
    outside = tmp_path.parent / "outside.py"
    outside.write_text("pass\n", encoding="utf-8")
    (tmp_path / "inside.py").write_text("pass\n", encoding="utf-8")

    cross_project = code_intelligence.query(tmp_path, {
        "operation": "symbols", "relative_path": "../outside.py",
    })
    unknown = code_intelligence.query(tmp_path, {
        "operation": "complete", "relative_path": "inside.py",
    })

    assert cross_project["path"] == "outside.py"
    assert cross_project["symbols"] == []
    assert "operation must be" in unknown["error"]


def test_installed_server_result_is_provider_neutral_and_symbol_selects_position(tmp_path, monkeypatch):
    source = "# emoji U0001f642\nvalue = target()\n"
    target = tmp_path / "app.py"
    target.write_text(source, encoding="utf-8")

    class FakeClient:
        server_name = "fake-lsp"

        def query(self, path, operation, line, character, max_results, timeout):
            assert path == target
            assert (operation, line, character) == ("definition", 2, 9)
            assert max_results == code_intelligence.MAX_RESULTS
            assert timeout == code_intelligence.REQUEST_TIMEOUT_SECONDS
            return {"locations": [{"path": "defs.py", "line": 4, "character": 1}]}

    monkeypatch.setattr(
        code_intelligence, "_get_client", lambda _root, _spec: (FakeClient(), "")
    )

    result = code_intelligence.query(tmp_path, {
        "operation": "definition", "relative_path": "app.py", "symbol": "target",
        "max_results": 999, "timeout_seconds": 999,
    })

    assert result["engine"] == "lsp"
    assert result["server"] == {"available": True, "name": "fake-lsp"}
    assert result["locations"][0]["path"] == "defs.py"


def test_utf16_position_counts_non_bmp_characters_as_two_units():
    assert code_intelligence._utf16_position(chr(0x1F642) + "target", 1, 2) == {
        "line": 0, "character": 2,
    }


def test_edit_notification_reuses_only_existing_matching_client(tmp_path):
    target = (tmp_path / "app.py").resolve()
    target.write_text("value = 1\n", encoding="utf-8")
    calls = []

    class FakeClient:
        def refresh_if_open(self, path):
            calls.append(path)

        def close(self):
            pass

    key = (str(tmp_path.resolve()).casefold(), "python")
    code_intelligence._CLIENTS[key] = FakeClient()

    code_intelligence.notify_path_changed(tmp_path, target)
    code_intelligence.notify_path_changed(tmp_path, tmp_path / "notes.txt")

    assert calls == [target]


def test_server_discovery_never_invokes_a_package_manager(tmp_path, monkeypatch):
    spec = next(item for item in code_intelligence.LANGUAGES if item.key == "typescript")
    looked_up = []

    def fake_which(name):
        looked_up.append(name)
        return None

    monkeypatch.setattr(code_intelligence, "HARNESS_LSP_BIN", tmp_path / "empty")
    monkeypatch.setattr(code_intelligence.shutil, "which", fake_which)

    assert code_intelligence._command_for_language(spec) is None
    assert looked_up == ["typescript-language-server"]
    assert "npx" not in looked_up


def test_harness_owned_server_precedes_path_but_explicit_override_wins(tmp_path, monkeypatch):
    spec = next(item for item in code_intelligence.LANGUAGES if item.key == "typescript")
    harness_bin = tmp_path / ".tools" / "lsp" / "node_modules" / ".bin"
    harness_bin.mkdir(parents=True)
    harness_wrapper = harness_bin / "typescript-language-server.cmd"
    harness_wrapper.write_text("@echo off\n", encoding="utf-8")
    path_lookups = []
    monkeypatch.setattr(code_intelligence, "HARNESS_LSP_BIN", harness_bin)
    monkeypatch.setattr(
        code_intelligence.shutil,
        "which",
        lambda name: path_lookups.append(name) or str(tmp_path / "path-server.exe"),
    )

    harness_command = code_intelligence._command_for_language(spec)

    assert harness_command == (str(harness_wrapper.resolve()), "--stdio")
    assert path_lookups == []

    override = tmp_path / "operator-server.exe"
    override.write_bytes(b"")
    monkeypatch.setenv(spec.env_name, f'"{override}" --stdio')
    code_intelligence._DISCOVERY_CACHE.clear()

    explicit_command = code_intelligence._command_for_language(spec)

    assert explicit_command == (str(override.resolve()), "--stdio")
    assert path_lookups == []


def test_harness_wrapper_resolution_supports_windows_and_posix_names(tmp_path, monkeypatch):
    monkeypatch.setattr(code_intelligence, "HARNESS_LSP_BIN", tmp_path)
    plain = tmp_path / "server"
    executable = tmp_path / "server.exe"
    command = tmp_path / "server.cmd"
    for path in (plain, executable, command):
        path.write_bytes(b"")

    assert code_intelligence._harness_executable("server", windows=True) == str(command.resolve())
    assert code_intelligence._harness_executable("server", windows=False) == str(plain.resolve())


def test_owned_typescript_server_receives_documented_tsserver_path(tmp_path, monkeypatch):
    node_modules = tmp_path / "node_modules"
    harness_bin = node_modules / ".bin"
    harness_bin.mkdir(parents=True)
    wrapper = harness_bin / "typescript-language-server.cmd"
    wrapper.write_text("@echo off\n", encoding="utf-8")
    tsserver = node_modules / "typescript" / "lib" / "tsserver.js"
    tsserver.parent.mkdir(parents=True)
    tsserver.write_text("// owned test entrypoint\n", encoding="utf-8")
    monkeypatch.setattr(code_intelligence, "HARNESS_LSP_BIN", harness_bin)
    spec = next(item for item in code_intelligence.LANGUAGES if item.key == "typescript")
    observed = {}

    class FakeProcess:
        stdin = io.BytesIO()
        stdout = io.BytesIO()
        stderr = io.BytesIO()
        returncode = None

        def poll(self):
            return self.returncode

        def terminate(self):
            self.returncode = 0

        def wait(self, timeout=None):
            return self.returncode

        def kill(self):
            self.returncode = 1

    client = code_intelligence.LspClient(tmp_path, spec, (str(wrapper), "--stdio"))
    monkeypatch.setattr(code_intelligence.subprocess, "Popen", lambda *args, **kwargs: FakeProcess())

    def fake_request(method, params, timeout):
        observed.update(method=method, params=params, timeout=timeout)
        return {"capabilities": {"definitionProvider": True}}

    monkeypatch.setattr(client, "_request", fake_request)
    monkeypatch.setattr(client, "_notify", lambda *_args, **_kwargs: None)

    client.start()
    client.close()

    assert observed["method"] == "initialize"
    assert observed["params"]["initializationOptions"] == {
        "tsserver": {"path": str(tsserver.resolve())},
    }
    assert observed["params"]["capabilities"]["textDocument"]["publishDiagnostics"]


def test_external_typescript_server_never_gets_harness_tsserver_path(tmp_path, monkeypatch):
    harness_bin = tmp_path / "owned" / "node_modules" / ".bin"
    tsserver = harness_bin.parent / "typescript" / "lib" / "tsserver.js"
    tsserver.parent.mkdir(parents=True)
    tsserver.write_text("// owned\n", encoding="utf-8")
    external = tmp_path / "external" / "typescript-language-server.exe"
    external.parent.mkdir()
    external.write_bytes(b"")
    monkeypatch.setattr(code_intelligence, "HARNESS_LSP_BIN", harness_bin)
    spec = next(item for item in code_intelligence.LANGUAGES if item.key == "typescript")

    assert code_intelligence._harness_tsserver_path(spec, (str(external), "--stdio")) is None


def test_server_diagnostic_uri_is_canonicalized_to_open_document_key(tmp_path):
    target = (tmp_path / "app.ts").resolve()
    target.write_text("const value = 1;\n", encoding="utf-8")
    emitted = target.as_uri().replace("app.ts", "app%2Ets")
    if code_intelligence.os.name == "nt":
        drive = target.drive
        emitted = emitted.replace(
            f"file:///{drive}", f"file:///{drive[0].lower()}%3A",
        )

    assert code_intelligence._canonical_file_uri(emitted) == target.as_uri()


def test_code_job_exposes_one_compact_schema_only_for_large_source_tasks(tmp_path):
    job = code_jobs.CodeJob("lsp-profile", tmp_path / "job")

    direct = {
        tool["function"]["name"]
        for tool in job._ollama_tools("Make the settings button slightly darker")
    }
    planned_tools = job._ollama_tools(
        "Refactor the authentication state machine across modules and update all call sites"
    )
    planned = {tool["function"]["name"] for tool in planned_tools}

    assert "code_intelligence" not in direct
    assert "code_intelligence" in planned
    assert sum(tool["function"]["name"] == "code_intelligence" for tool in planned_tools) == 1
    schema = next(
        tool["function"] for tool in planned_tools
        if tool["function"]["name"] == "code_intelligence"
    )
    assert set(schema["parameters"]["properties"]) == {
        "operation", "relative_path", "line", "character", "symbol", "max_results",
    }


def test_code_job_dispatches_provider_neutral_tool_and_refreshes_after_edit(tmp_path, monkeypatch):
    target = tmp_path / "app.py"
    target.write_text("value = 1\n", encoding="utf-8")
    observed = []
    job = code_jobs.CodeJob("lsp-dispatch", tmp_path / "job")
    monkeypatch.setattr(job, "_persist_harness_state", lambda: None)
    monkeypatch.setattr(
        code_jobs.code_intelligence,
        "query",
        lambda root, args: {"ok": True, "root": str(root), "operation": args["operation"]},
    )
    monkeypatch.setattr(
        code_jobs.code_intelligence,
        "notify_path_changed",
        lambda root, path: observed.append((root, path)),
    )

    result = json.loads(job._ollama_run_tool(tmp_path, "lsp", {
        "action": "definition", "path": "app.py", "name": "value",
    }))
    job._record_mutation_state(tmp_path, target)

    assert result["ok"] is True
    assert result["operation"] == "definition"
    assert observed == [(tmp_path, target)]
