"""Integrity and external-grading checks for the pinned Aider refactor subset."""

from __future__ import annotations

import ast
import hashlib
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from aios_ui import bench_api  # noqa: E402
from bench import aider_refactor, reporting, runner, runs, suites  # noqa: E402


OFFICIAL_COMMIT = "c90dfb67d829f4da2759955a69111fc5f3b0e0fd"
OFFICIAL_TASK_IDS = [
    "operations_DatabaseOperations_check_expression_support",
    "grpc_debug_server_EventListenerBaseServicer__process_tensor_event_in_chunks",
    "concat__Concatenator__clean_keys_and_objs",
    "doc_DocCLI_display_plugin_list",
    "triton_TritonScheduling_define_kernel",
]
OFFICIAL_SOURCE_BYTES = [17015, 18854, 27974, 63439, 120534]
OFFICIAL_SHA256 = {
    "refactor-benchmark/operations_DatabaseOperations_check_expression_support/.docs/instructions.md":
        "51e1902854f40ffcd1301e5a70c773a8e822c2c92ad9a7ff75a7851b884fe247",
    "refactor-benchmark/operations_DatabaseOperations_check_expression_support/operations.py":
        "93d7c5cf7d24d07ec4e933743d4f25ee9442faee2abf12073740e40cb1b18e04",
    "refactor-benchmark/operations_DatabaseOperations_check_expression_support/operations_test.py":
        "3b1d2a588c81c2ca9355807e4b96f740b1c8d5df8fdc77ace727f4ac7aefaf06",
    "refactor-benchmark/grpc_debug_server_EventListenerBaseServicer__process_tensor_event_in_chunks/.docs/instructions.md":
        "4df2dc3323cb1ad0183ac9df2bbb20e90360a5b92a080a59a96a057674f2f5cc",
    "refactor-benchmark/grpc_debug_server_EventListenerBaseServicer__process_tensor_event_in_chunks/grpc_debug_server.py":
        "7c4ba50b3e24a681c431860c07beac99d5facbc95c57ec93b0d5fd688bd24e27",
    "refactor-benchmark/grpc_debug_server_EventListenerBaseServicer__process_tensor_event_in_chunks/grpc_debug_server_test.py":
        "f3f8ae4727993d19e674f9692e2c8b063cbf32f3cbf7f7ded9bf4daba2cdd4e5",
    "refactor-benchmark/concat__Concatenator__clean_keys_and_objs/.docs/instructions.md":
        "0f242d323c8d9df6cba888afb323d22d3fa2acb21f808e75b3b26dc4a26eac00",
    "refactor-benchmark/concat__Concatenator__clean_keys_and_objs/concat.py":
        "e21a6a4d77d51345052ab097dc13c83e0f3dc50dd4f85a47f2294adc7c0a3788",
    "refactor-benchmark/concat__Concatenator__clean_keys_and_objs/concat_test.py":
        "7c341bfad055ee02f1c4dde816e940df775dfcb4f3a26c7a7c980f4dc2e08cb9",
    "refactor-benchmark/doc_DocCLI_display_plugin_list/.docs/instructions.md":
        "660eda7ceb76ed9fbf713c235fab631634a4f19c5774176b60625420bbb1108c",
    "refactor-benchmark/doc_DocCLI_display_plugin_list/doc.py":
        "1b3423cdd2f330009d99b363d4ca6a444d48b15a79a5884f2b425a2baa348903",
    "refactor-benchmark/doc_DocCLI_display_plugin_list/doc_test.py":
        "68d4cad19f9a6a0627eba5d81c6cd097f54161e217bb73e6652b81baa703ec15",
    "refactor-benchmark/triton_TritonScheduling_define_kernel/.docs/instructions.md":
        "962d1d4d8e304e859055584d649c36ae96bd4a389809855cd61d8269e2e5551e",
    "refactor-benchmark/triton_TritonScheduling_define_kernel/triton.py":
        "9316307442480602a6759cf92dab46f7d40a51d748d245a3c6edcc4d7185a222",
    "refactor-benchmark/triton_TritonScheduling_define_kernel/triton_test.py":
        "16b5a9e137918a94f637903077f55b003367738111ffb20b4030ba2798ae1d36",
}


def _node_counts(source: str, method: str, class_name: str) -> tuple[int, int]:
    tree = ast.parse(source)
    function = next(
        node for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == method
    )
    old_class = next(
        node for node in ast.walk(tree)
        if isinstance(node, ast.ClassDef) and node.name == class_name
    )
    return sum(1 for _ in ast.walk(function)), sum(1 for _ in ast.walk(old_class))


def _fake_exercise() -> tuple[aider_refactor.Exercise, str, str]:
    method = "extract_me"
    class_name = "Example"
    body = '''\
    def extract_me(self, value):
        total = value + 1
        if total > 2:
            total *= 2
        return total
'''
    stable_members = "".join(f"    marker_{index} = {index}\n" for index in range(20))
    original = f"class {class_name}:\n{stable_members}{body}"
    extracted = f"{body[4:]}\n\nclass {class_name}:\n{stable_members}"
    method_children, class_children = _node_counts(original, method, class_name)
    exercise = aider_refactor.Exercise(
        "sample_Example_extract_me",
        "Example.extract_me",
        "sample.py",
        "sample_test.py",
        method,
        method_children,
        class_name,
        class_children,
        len(original.encode("utf-8")),
    )
    return exercise, original, extracted


def _upstream_test(exercise: aider_refactor.Exercise) -> str:
    return f'''\
import unittest
from benchmark.refactor_tools import verify_refactor
from pathlib import Path

class TheTest(unittest.TestCase):
    def test_{exercise.method}(self):
        fname = Path(__file__).parent / "{exercise.module}"
        method = "{exercise.method}"
        method_children = {exercise.method_children}

        class_name = "{exercise.class_name}"
        class_children = {exercise.class_children}

        verify_refactor(fname, method, method_children, class_name, class_children)

if __name__ == "__main__":
    unittest.main()
'''


def _install_fake_cache(tmp_path, monkeypatch, *, corrupt: str = ""):
    exercise, original, extracted = _fake_exercise()
    manifest = aider_refactor.file_manifest(exercise)
    payloads = {
        manifest[".docs/instructions.md"]: b"# Refactor Example.extract_me\n",
        manifest[exercise.module]: original.encode("utf-8"),
        manifest[exercise.test_file]: _upstream_test(exercise).encode("utf-8"),
    }
    monkeypatch.setattr(aider_refactor, "EXERCISES", (exercise,))
    monkeypatch.setattr(aider_refactor, "CACHE_DIR", tmp_path / "aider-refactor-cache")
    monkeypatch.setattr(
        aider_refactor,
        "UPSTREAM_SHA256",
        {path: hashlib.sha256(payload).hexdigest() for path, payload in payloads.items()},
    )
    for upstream_path, payload in payloads.items():
        target = aider_refactor.cache_path(upstream_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"damaged\n" if upstream_path == corrupt else payload)
    monkeypatch.setattr(
        aider_refactor.urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("network used")),
    )
    return exercise, original, extracted, payloads


def test_official_subset_pin_ids_sizes_and_every_artifact_hash_are_exact():
    assert aider_refactor.UPSTREAM_COMMIT == OFFICIAL_COMMIT
    assert aider_refactor.UPSTREAM_RAW.endswith(f"/{OFFICIAL_COMMIT}")
    assert [exercise.task_id for exercise in aider_refactor.EXERCISES] == OFFICIAL_TASK_IDS
    assert [exercise.source_bytes for exercise in aider_refactor.EXERCISES] == OFFICIAL_SOURCE_BYTES
    assert aider_refactor.UPSTREAM_SHA256 == OFFICIAL_SHA256

    manifest_paths = {
        upstream_path
        for exercise in aider_refactor.EXERCISES
        for upstream_path in aider_refactor.file_manifest(exercise).values()
    }
    assert manifest_paths == set(OFFICIAL_SHA256)
    assert len(manifest_paths) == 15
    assert aider_refactor.SUITE_PROVENANCE["upstream_task_count"] == 89
    assert aider_refactor.SUITE_PROVENANCE["leaderboard_comparable"] is False
    assert suites.SUITES["aider_refactor"]["max"] == 5
    assert reporting._suite("aider_refactor") == "aider-refactor"
    assert "aider-refactor" in reporting.SUITE_ORDER
    assert "aider-refactor" in reporting.SUITE_GUIDANCE


def test_subset_catalogue_selection_task_ids_and_ui_are_generic(tmp_path, monkeypatch):
    exercise, _original, _extracted, payloads = _install_fake_cache(tmp_path, monkeypatch)

    tasks = suites.select({"aider_refactor": 99})
    assert [task.id for task in tasks] == [f"aider_refactor/{exercise.task_id}"]
    task = tasks[0]
    manifest = aider_refactor.file_manifest(exercise)
    assert task.files[exercise.module].encode("utf-8") == payloads[manifest[exercise.module]]
    assert set(task.protected) == {
        ".docs/instructions.md",
        exercise.test_file,
        "benchmark/__init__.py",
        "benchmark/refactor_tools.py",
    }

    filtered = runs.select_tasks({
        "kind": "suite",
        "counts": {"aider_refactor": 5},
        "task_ids": [task.id],
    })
    assert [row.id for row in filtered] == [task.id]

    entry = next(row for row in suites.suite_catalogue() if row["id"] == "aider_refactor")
    assert entry["official"] is True
    assert entry["leaderboard_comparable"] is False
    assert "Public benchmark subset" in entry["comparability_note"]

    monkeypatch.setattr(bench_api.adapters, "catalogue", lambda: [])
    meta = bench_api.dispatch("/api/bench/meta", "GET", {}, {})
    assert any(row["id"] == "aider_refactor" for row in meta["suites"])
    bench_js = (ROOT / "aios_ui" / "web" / "js" / "bench.js").read_text(encoding="utf-8")
    assert "${suites.map((suite) =>" in bench_js
    assert "data-suite=\"${escapeHtml(suite.id)}\"" in bench_js


def test_external_ast_verifier_rejects_original_and_accepts_complete_extraction(
    tmp_path, monkeypatch,
):
    exercise, _original, extracted, _payloads = _install_fake_cache(tmp_path, monkeypatch)
    task = aider_refactor.tasks(1)[0]
    workspace = tmp_path / "workspace"
    task.build(workspace)

    untouched = runner.verify(task, workspace, tmp_path / "verifiers" / "untouched")
    assert untouched["passed"] is False
    assert untouched["checks"][0]["name"] == "upstream AST refactor criteria pass"

    (workspace / exercise.module).write_text(extracted, encoding="utf-8")
    solved = runner.verify(task, workspace, tmp_path / "verifiers" / "solved")
    assert solved["passed"] is True
    verifier = tmp_path / "verifiers" / "solved" / f"{task.id.replace('/', '-')}.py"
    assert verifier.exists()
    assert workspace.resolve() not in verifier.resolve().parents

    local = subprocess.run(
        [sys.executable, exercise.test_file],
        cwd=workspace,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert local.returncode == 0, local.stdout + local.stderr


def test_corrupt_cache_aborts_before_run_creation(tmp_path, monkeypatch):
    exercise, _original, _extracted, _payloads = _install_fake_cache(tmp_path, monkeypatch)
    corrupt = aider_refactor.file_manifest(exercise)[exercise.module]
    aider_refactor.cache_path(corrupt).write_bytes(b"damaged\n")
    monkeypatch.setattr(runs, "RUNS_DIR", tmp_path / "runs")

    result = runs.create_run({
        "provider": "openrouter",
        "model": "test/model",
        "reasoning": "off",
        "counts": {"aider_refactor": 1},
    })

    assert result["ok"] is False
    assert "integrity check failed" in result["error"]
    assert not (tmp_path / "runs").exists()


def test_download_uses_only_commit_pinned_raw_url_then_verified_cache(tmp_path, monkeypatch):
    exercise, _original, _extracted = _fake_exercise()
    upstream_path = aider_refactor.file_manifest(exercise)[".docs/instructions.md"]
    payload = b"# exact pinned instruction\n"
    monkeypatch.setattr(aider_refactor, "CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(
        aider_refactor,
        "UPSTREAM_SHA256",
        {upstream_path: hashlib.sha256(payload).hexdigest()},
    )
    requested = []

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return payload

    def fake_urlopen(request, timeout):
        requested.append((request.full_url, timeout))
        return Response()

    monkeypatch.setattr(aider_refactor.urllib.request, "urlopen", fake_urlopen)
    assert aider_refactor.load_pinned_text(upstream_path) == payload.decode("utf-8")
    assert requested == [(f"{aider_refactor.UPSTREAM_RAW}/{upstream_path}", 60)]
    assert aider_refactor.cache_path(upstream_path).read_bytes() == payload

    monkeypatch.setattr(
        aider_refactor.urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("cache miss")),
    )
    assert aider_refactor.load_pinned_text(upstream_path) == payload.decode("utf-8")


def test_preflight_rejects_metadata_that_does_not_match_the_pinned_test(tmp_path, monkeypatch):
    exercise, _original, _extracted, _payloads = _install_fake_cache(tmp_path, monkeypatch)
    mismatched = aider_refactor.Exercise(
        exercise.task_id,
        exercise.title,
        exercise.module,
        exercise.test_file,
        exercise.method,
        exercise.method_children + 1,
        exercise.class_name,
        exercise.class_children,
        exercise.source_bytes,
    )
    with pytest.raises(RuntimeError, match="test contract mismatch"):
        aider_refactor.preflight((mismatched,))
