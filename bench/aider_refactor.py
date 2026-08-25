"""Pinned size-stratified Aider refactoring tasks.

This is a deterministic five-task harness regression subset of the official
89-task Aider refactoring benchmark.  Every upstream artifact is fetched from
one immutable commit and SHA-256 verified before a Task can be constructed.
The grader is external to the task workspace and reproduces the upstream AST
criteria without importing or executing the edited source file.
"""

from __future__ import annotations

import ast
import hashlib
import os
import tempfile
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Iterable

from . import DATA_DIR

if TYPE_CHECKING:
    from .suites import Task


UPSTREAM_REPOSITORY = "https://github.com/Aider-AI/refactor-benchmark"
UPSTREAM_COMMIT = "c90dfb67d829f4da2759955a69111fc5f3b0e0fd"
UPSTREAM_RAW = f"https://raw.githubusercontent.com/Aider-AI/refactor-benchmark/{UPSTREAM_COMMIT}"
CACHE_DIR = DATA_DIR / "aider-refactor" / UPSTREAM_COMMIT
SUBSET_NOTE = (
    "Fixed five-task, size-stratified subset of Aider's 89-task refactoring "
    "benchmark for aiOS harness regression; it is not leaderboard-comparable."
)


@dataclass(frozen=True)
class Exercise:
    task_id: str
    title: str
    module: str
    test_file: str
    method: str
    method_children: int
    class_name: str
    class_children: int
    source_bytes: int

    @property
    def upstream_root(self) -> str:
        return f"refactor-benchmark/{self.task_id}"


# Ascending raw source size gives a stable spread from a medium one-file
# refactor to a 120 kB stress case without paying for all 89 upstream tasks.
EXERCISES = (
    Exercise(
        "operations_DatabaseOperations_check_expression_support",
        "DatabaseOperations.check_expression_support",
        "operations.py",
        "operations_test.py",
        "check_expression_support",
        118,
        "DatabaseOperations",
        1942,
        17015,
    ),
    Exercise(
        "grpc_debug_server_EventListenerBaseServicer__process_tensor_event_in_chunks",
        "EventListenerBaseServicer._process_tensor_event_in_chunks",
        "grpc_debug_server.py",
        "grpc_debug_server_test.py",
        "_process_tensor_event_in_chunks",
        279,
        "EventListenerBaseServicer",
        1416,
        18854,
    ),
    Exercise(
        "concat__Concatenator__clean_keys_and_objs",
        "_Concatenator._clean_keys_and_objs",
        "concat.py",
        "concat_test.py",
        "_clean_keys_and_objs",
        305,
        "_Concatenator",
        2038,
        27974,
    ),
    Exercise(
        "doc_DocCLI_display_plugin_list",
        "DocCLI.display_plugin_list",
        "doc.py",
        "doc_test.py",
        "display_plugin_list",
        389,
        "DocCLI",
        7038,
        63439,
    ),
    Exercise(
        "triton_TritonScheduling_define_kernel",
        "TritonScheduling.define_kernel",
        "triton.py",
        "triton_test.py",
        "define_kernel",
        267,
        "TritonScheduling",
        4346,
        120534,
    ),
)


def file_manifest(exercise: Exercise) -> dict[str, str]:
    """Map task workspace paths to the exact pinned upstream artifacts."""
    root = exercise.upstream_root
    return {
        ".docs/instructions.md": f"{root}/.docs/instructions.md",
        exercise.module: f"{root}/{exercise.module}",
        exercise.test_file: f"{root}/{exercise.test_file}",
    }


# SHA-256 of raw GitHub bytes at UPSTREAM_COMMIT.  The sizes in EXERCISES and
# these hashes are intentionally based on the raw LF blobs, not a Windows
# checkout where core.autocrlf may alter every digest.
UPSTREAM_SHA256 = {
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


SUITE_PROVENANCE = {
    "benchmark": "Aider Refactoring",
    "repository": UPSTREAM_REPOSITORY,
    "commit": UPSTREAM_COMMIT,
    "language": "python",
    "tasks": [exercise.task_id for exercise in EXERCISES],
    "selection": "fixed-size-stratified-5",
    "upstream_task_count": 89,
    "subset": True,
    "leaderboard_comparable": False,
}


def cache_path(upstream_path: str) -> Path:
    """Return the cache target for a known artifact, rejecting arbitrary paths."""
    if upstream_path not in UPSTREAM_SHA256:
        raise ValueError(f"unknown Aider Refactoring file: {upstream_path}")
    return CACHE_DIR.joinpath(*upstream_path.split("/"))


def _verified(payload: bytes, upstream_path: str) -> bytes:
    digest = hashlib.sha256(payload).hexdigest()
    expected = UPSTREAM_SHA256[upstream_path]
    if digest != expected:
        raise RuntimeError(
            f"Aider Refactoring integrity check failed for {upstream_path}: "
            f"expected {expected}, got {digest}"
        )
    return payload


def _cache_atomically(target: Path, payload: bytes) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = ""
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            prefix=f".{target.name}.",
            suffix=".tmp",
            dir=target.parent,
            delete=False,
        ) as handle:
            temporary = handle.name
            handle.write(payload)
        os.replace(temporary, target)
    finally:
        if temporary:
            Path(temporary).unlink(missing_ok=True)


def load_pinned_text(upstream_path: str) -> str:
    """Read one exact artifact, downloading only a missing cache entry."""
    target = cache_path(upstream_path)
    try:
        payload = target.read_bytes()
    except FileNotFoundError:
        request = urllib.request.Request(
            f"{UPSTREAM_RAW}/{upstream_path}",
            headers={"User-Agent": "aiOS-benchmark/1"},
        )
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                payload = response.read()
        except Exception as exc:
            raise RuntimeError(
                f"could not download pinned Aider Refactoring file {upstream_path}: {exc}"
            ) from exc
        _verified(payload, upstream_path)
        _cache_atomically(target, payload)
    return _verified(payload, upstream_path).decode("utf-8")


def _validate_upstream_test(exercise: Exercise, source: str) -> None:
    """Confirm that pinned test metadata still describes our external grader."""
    try:
        tree = ast.parse(source, filename=exercise.test_file)
    except SyntaxError as exc:
        raise RuntimeError(f"invalid pinned Aider Refactoring test {exercise.test_file}: {exc}") from exc

    assignments: dict[str, object] = {}
    source_filename = ""
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        target = node.targets[0]
        if not isinstance(target, ast.Name):
            continue
        if isinstance(node.value, ast.Constant):
            assignments[target.id] = node.value.value
        elif target.id == "fname" and isinstance(node.value, ast.BinOp):
            if isinstance(node.value.right, ast.Constant) and isinstance(node.value.right.value, str):
                source_filename = node.value.right.value

    expected = {
        "method": exercise.method,
        "method_children": exercise.method_children,
        "class_name": exercise.class_name,
        "class_children": exercise.class_children,
    }
    for name, value in expected.items():
        if assignments.get(name) != value:
            raise RuntimeError(
                f"Aider Refactoring test contract mismatch for {exercise.task_id}: "
                f"{name} is {assignments.get(name)!r}, expected {value!r}"
            )
    if source_filename != exercise.module:
        raise RuntimeError(
            f"Aider Refactoring test contract mismatch for {exercise.task_id}: "
            f"source is {source_filename!r}, expected {exercise.module!r}"
        )
    calls = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "verify_refactor"
    ]
    names = [arg.id for arg in calls[0].args if isinstance(arg, ast.Name)] if len(calls) == 1 else []
    if names != ["fname", "method", "method_children", "class_name", "class_children"]:
        raise RuntimeError(f"Aider Refactoring test contract mismatch for {exercise.task_id}: verifier call")


def preflight(exercises: Iterable[Exercise]) -> dict[str, str]:
    """Verify every selected artifact before any benchmark run is created."""
    payloads: dict[str, str] = {}
    selected = tuple(exercises)
    for exercise in selected:
        manifest = file_manifest(exercise)
        for upstream_path in manifest.values():
            payloads[upstream_path] = load_pinned_text(upstream_path)
        _validate_upstream_test(exercise, payloads[manifest[exercise.test_file]])
        source_size = len(payloads[manifest[exercise.module]].encode("utf-8"))
        if source_size != exercise.source_bytes:
            raise RuntimeError(
                f"Aider Refactoring source size mismatch for {exercise.task_id}: "
                f"expected {exercise.source_bytes}, got {source_size}"
            )
    return payloads


# The upstream test imports benchmark.refactor_tools from Aider itself.  This
# protected compatibility module makes the exact upstream test runnable inside
# each isolated task repository.  The authoritative grader below is still
# generated outside the workspace and does not trust this helper.
_SELF_CHECK_HELPER = '''\
import ast


class ParentNodeTransformer(ast.NodeTransformer):
    def generic_visit(self, node):
        for child in ast.iter_child_nodes(node):
            child.parent = node
        return super().generic_visit(node)


def verify_refactor(fname, func, func_children, old_class, old_class_children):
    tree = ast.parse(fname.read_text(encoding="utf-8"))
    ParentNodeTransformer().visit(tree)
    functions = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == func
    ]
    for node in functions:
        if not isinstance(node.parent, ast.Module):
            continue
        count = sum(1 for _ in ast.walk(node))
        assert abs(count - func_children) * 100 / func_children < 10, (
            f"Old method had {func_children} children, new method has {count}"
        )
        break
    else:
        raise AssertionError(f"{func} is not a full top level function")

    node = next((
        node for node in ast.walk(tree)
        if isinstance(node, ast.ClassDef) and node.name == old_class
    ), None)
    assert node is not None, f"Old class {old_class} not found"
    expected = old_class_children - func_children
    count = sum(1 for _ in ast.walk(node))
    assert abs(count - expected) * 100 / expected < 10, (
        f"Old class had {expected} children after extraction, new class has {count}"
    )
'''


def _brief(exercise: Exercise) -> str:
    return f"""\
Complete the pinned upstream Aider refactoring task in `{exercise.module}`.

Read `.docs/instructions.md`; it is the exact upstream specification. Run
`python {exercise.test_file}` while you work. Do not edit the instruction,
`{exercise.test_file}`, or anything under `benchmark/`.

This is {SUBSET_NOTE[0].lower() + SUBSET_NOTE[1:]}
"""


def _checks(exercise: Exercise) -> str:
    return f'''\
import ast
from pathlib import Path


class _ParentNodeTransformer(ast.NodeTransformer):
    def generic_visit(self, node):
        for child in ast.iter_child_nodes(node):
            child.parent = node
        return super().generic_visit(node)


@case("upstream AST refactor criteria pass")
def upstream_ast_refactor_criteria_pass():
    source = Path(WORKSPACE) / {exercise.module!r}
    tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
    _ParentNodeTransformer().visit(tree)

    functions = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == {exercise.method!r}
    ]
    for node in functions:
        if not isinstance(node.parent, ast.Module):
            continue
        count = sum(1 for _ in ast.walk(node))
        difference = abs(count - {exercise.method_children}) * 100 / {exercise.method_children}
        assert difference < 10, (
            f"Old method had {exercise.method_children} children, new method has {{count}}"
        )
        break
    else:
        raise AssertionError({exercise.method!r} + " is not a full top level function")

    old_class = next((
        node for node in ast.walk(tree)
        if isinstance(node, ast.ClassDef) and node.name == {exercise.class_name!r}
    ), None)
    assert old_class is not None, "Old class " + {exercise.class_name!r} + " not found"
    expected = {exercise.class_children} - {exercise.method_children}
    count = sum(1 for _ in ast.walk(old_class))
    difference = abs(count - expected) * 100 / expected
    assert difference < 10, f"Old class had {{expected}} children, new class has {{count}}"
'''


def _readme(exercise: Exercise) -> str:
    source = f"{UPSTREAM_REPOSITORY}/tree/{UPSTREAM_COMMIT}/{exercise.upstream_root}"
    return f"""\
# Aider Refactoring: {exercise.title}

Source: {source}

The instruction, {exercise.source_bytes:,}-byte starter module, and upstream
AST test are byte-pinned to the commit above. Only `{exercise.module}` is part
of the solution.

{SUBSET_NOTE}
"""


def tasks(limit: int | None = None) -> tuple["Task", ...]:
    """Materialise the deterministic size-stratified prefix requested."""
    from .suites import Task

    count = len(EXERCISES) if limit is None else max(0, min(len(EXERCISES), int(limit)))
    chosen = EXERCISES[:count]
    payloads = preflight(chosen)
    selected: list[Task] = []
    for exercise in chosen:
        manifest = file_manifest(exercise)
        files = {name: payloads[path] for name, path in manifest.items()}
        files.update({
            "README.md": _readme(exercise),
            "benchmark/__init__.py": "",
            "benchmark/refactor_tools.py": _SELF_CHECK_HELPER,
        })
        source = f"{UPSTREAM_REPOSITORY}/tree/{UPSTREAM_COMMIT}/{exercise.upstream_root}"
        selected.append(Task(
            id=f"aider_refactor/{exercise.task_id}",
            suite="aider_refactor",
            title=exercise.title,
            brief=_brief(exercise),
            files=files,
            checks=_checks(exercise),
            protected=(
                ".docs/instructions.md",
                exercise.test_file,
                "benchmark/__init__.py",
                "benchmark/refactor_tools.py",
            ),
            source=source,
            provenance={
                **SUITE_PROVENANCE,
                "task_id": exercise.task_id,
                "source_bytes": exercise.source_bytes,
            },
        ))
    return tuple(selected)


__all__ = [
    "CACHE_DIR",
    "EXERCISES",
    "Exercise",
    "SUBSET_NOTE",
    "SUITE_PROVENANCE",
    "UPSTREAM_COMMIT",
    "UPSTREAM_RAW",
    "UPSTREAM_REPOSITORY",
    "UPSTREAM_SHA256",
    "cache_path",
    "file_manifest",
    "load_pinned_text",
    "preflight",
    "tasks",
]
