from __future__ import annotations

import shutil
from pathlib import Path

import code_diagnostics
import pytest


def test_python_diagnostics_are_fresh_and_side_effect_free(tmp_path: Path):
    source = tmp_path / "demo.py"
    source.write_text("def answer():\n    return 42\n", encoding="utf-8")

    result = code_diagnostics.diagnose_path(source)

    assert result.status == "passed"
    assert result.checker == "python-ast"
    assert not (tmp_path / "__pycache__").exists()


def test_python_syntax_failure_reports_location(tmp_path: Path):
    source = tmp_path / "broken.py"
    source.write_text("def nope(:\n    pass\n", encoding="utf-8")

    result = code_diagnostics.diagnose_path(source)

    assert result.status == "failed"
    assert result.line == 1
    assert result.column > 0


def test_json_and_toml_failures_preserve_line_and_column(tmp_path: Path):
    good_json = tmp_path / "good.json"
    bad_json = tmp_path / "bad.json"
    bad_toml = tmp_path / "bad.toml"
    good_json.write_text('{"ok": true}', encoding="utf-8")
    bad_json.write_text('{\n  "ok": ]\n}', encoding="utf-8")
    bad_toml.write_text('name = "ok"\nvalue = @\n', encoding="utf-8")

    assert code_diagnostics.diagnose_path(good_json).status == "passed"
    json_result = code_diagnostics.diagnose_path(bad_json)
    toml_result = code_diagnostics.diagnose_path(bad_toml)

    assert (json_result.status, json_result.checker) == ("failed", "json")
    assert (json_result.line, json_result.column) == (2, 9)
    assert (toml_result.status, toml_result.checker) == ("failed", "tomllib")
    assert (toml_result.line, toml_result.column) == (2, 9)


@pytest.mark.parametrize("suffix", [".xml", ".svg"])
def test_malformed_xml_and_svg_report_xml_position(tmp_path: Path, suffix: str):
    source = tmp_path / f"broken{suffix}"
    source.write_text('<svg>\n  <g id="x">\n</svg>\n', encoding="utf-8")

    result = code_diagnostics.diagnose_path(source)

    assert (result.status, result.checker) == ("failed", "xml")
    assert (result.line, result.column) == (3, 2)
    assert "mismatched tag" in result.message


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is not installed")
def test_javascript_failure_reports_node_line_and_caret_column(tmp_path: Path):
    source = tmp_path / "broken.js"
    source.write_text("const ready = true;\nconst broken = ;\n", encoding="utf-8")

    result = code_diagnostics.diagnose_path(source)

    assert (result.status, result.checker) == ("failed", "node --check")
    assert (result.line, result.column) == (2, 16)


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is not installed")
def test_javascript_module_failure_is_not_skipped_by_node_syntax_detection(tmp_path: Path):
    source = tmp_path / "broken-module.js"
    source.write_text(
        'import { ready } from "./dependency.js";\n'
        'const action = "refresh";\n'
        'else if (action === "refresh") ready();\n',
        encoding="utf-8",
    )

    result = code_diagnostics.diagnose_path(source)

    assert (result.status, result.checker) == ("failed", "node --check")
    assert (result.line, result.column) == (3, 1)


@pytest.mark.skipif(
    shutil.which("node") is None or code_diagnostics._luaparse_path() is None,
    reason="The pinned aiOS Lua parser is not installed",
)
def test_lua_diagnostics_use_pinned_parser_and_report_location(tmp_path: Path):
    valid = tmp_path / "valid.lua"
    broken = tmp_path / "broken.lua"
    valid.write_text("local ready = true\nreturn ready\n", encoding="utf-8")
    broken.write_text("local ready = true\nif ready then\n  return )\nend\n", encoding="utf-8")

    passed = code_diagnostics.diagnose_path(valid)
    failed = code_diagnostics.diagnose_path(broken)

    assert (passed.status, passed.checker) == ("passed", "luaparse (LuaJIT)")
    assert (failed.status, failed.checker) == ("failed", "luaparse (LuaJIT)")
    assert failed.line == 3
    assert failed.column > 0
    assert not list(tmp_path.glob("*.json"))


def test_non_code_files_are_explicitly_not_applicable(tmp_path: Path):
    note = tmp_path / "README.md"
    note.write_text("hello", encoding="utf-8")

    result = code_diagnostics.diagnose_path(note)

    assert result.status == "not_applicable"
    assert result.checker == "none"


@pytest.mark.parametrize("suffix", [".png", ".ttf", ".ico"])
def test_binary_assets_are_not_misreported_as_utf8_failures(tmp_path: Path, suffix: str):
    asset = tmp_path / f"asset{suffix}"
    asset.write_bytes(b"\x89\xff\x00binary")

    result = code_diagnostics.diagnose_path(asset)

    assert result.status == "not_applicable"
    assert result.checker == "none"
