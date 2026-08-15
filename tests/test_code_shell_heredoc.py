import json
import shutil
import sys
from pathlib import Path

import pytest

import code_jobs


def _make_job(tmp_path: Path, name: str = "heredoc") -> tuple[code_jobs.CodeJob, Path]:
    project = tmp_path / "project"
    project.mkdir()
    job = code_jobs.CodeJob(name, directory=tmp_path / "sessions" / name)
    job.save(id=name, cwd=str(project), status="running")
    return job, project


@pytest.mark.parametrize("marker_token", ["PY", "'PY'", '"PY"'])
def test_strict_python_heredoc_parser_accepts_quoted_markers(marker_token):
    command = f"python - <<{marker_token}\r\nprint('ready')\r\nPY\r\n"

    parsed = code_jobs._standalone_python_heredoc(command)

    assert parsed == ("python", "print('ready')\r\n")


@pytest.mark.parametrize(
    "command",
    [
        "python - <<'PY'\nprint('missing close')\n",
        "python - <<'PY' && echo chained\nprint('nope')\nPY",
        "echo prefix; python - <<'PY'\nprint('nope')\nPY",
        "python - <<'PY'\nprint('nope')\nPY; echo suffix",
        "python - <<'PY'\nprint('first')\nPY\necho compound\nPY",
        "python - <<'$(whoami)'\nprint('nope')\n$(whoami)",
        "python -m pytest <<'PY'\nprint('nope')\nPY",
        "node - <<'JS'\nconsole.log('nope')\nJS",
    ],
)
def test_python_heredoc_parser_never_transforms_malformed_or_compound_commands(command):
    assert code_jobs._standalone_python_heredoc(command) is None


def test_run_shell_executes_python_heredoc_with_project_cwd_env_and_no_scratch(tmp_path):
    job, project = _make_job(tmp_path)
    job.save(runtime_env={"AIOS_HEREDOC_VALUE": "from-runtime-env"})
    executable = str(Path(sys.executable).resolve()).replace('"', '')
    command = (
        f'"{executable}" - <<\'CHECK\'\n'
        "import os\n"
        "from pathlib import Path\n"
        "print(os.environ['AIOS_HEREDOC_VALUE'])\n"
        "print(Path.cwd().resolve())\n"
        "CHECK\n"
    )

    result = json.loads(job._ollama_run_tool(
        project,
        "run_shell",
        {"command": command, "timeout_seconds": 30},
    ))

    assert result["exit_code"] == 0
    assert result["normalization"] == "python_heredoc_stdin"
    assert "from-runtime-env" in result["output"]
    assert str(project.resolve()) in result["output"]
    assert list(project.iterdir()) == []
    assert not list(job.directory.glob(".shell-*"))
    verification = job.load()["verification"]
    assert verification["generation"] == 0
    assert verification["changed_path_hashes"] == {}
    assert verification["evidence"][-1]["command"] == command.strip()


def test_run_shell_python_heredoc_tracks_only_real_project_mutation(tmp_path):
    job, project = _make_job(tmp_path, "heredoc-mutation")
    command = (
        "python - <<'PY'\n"
        "from pathlib import Path\n"
        "Path('created.txt').write_text('real change\\n', encoding='utf-8')\n"
        "PY"
    )

    result = json.loads(job._ollama_run_tool(project, "run_shell", {"command": command}))

    assert result["exit_code"] == 0
    assert result["mutated_paths"] == ["created.txt"]
    assert (project / "created.txt").read_text(encoding="utf-8") == "real change\n"
    assert not list(job.directory.glob(".shell-*"))
    verification = job.load()["verification"]
    assert verification["generation"] == 1
    assert list(verification["changed_path_hashes"]) == ["created.txt"]


@pytest.mark.skipif(shutil.which("git") is None, reason="git is unavailable")
def test_run_shell_does_not_promote_gitignored_build_output_to_source(tmp_path):
    job, project = _make_job(tmp_path, "ignored-build-output")
    (project / ".gitignore").write_text("public/\ndist/\n", encoding="utf-8")
    assert code_jobs.subprocess.run(
        ["git", "init", "--quiet"], cwd=project, check=False,
    ).returncode == 0
    command = (
        "python - <<'PY'\n"
        "from pathlib import Path\n"
        "Path('public').mkdir()\n"
        "Path('public/bundle.js').write_text('generated\\n', encoding='utf-8')\n"
        "Path('source.py').write_text('value = 1\\n', encoding='utf-8')\n"
        "PY"
    )

    result = json.loads(job._ollama_run_tool(project, "run_shell", {"command": command}))

    assert result["exit_code"] == 0
    assert result["mutated_paths"] == ["source.py"]
    assert list(job.load()["verification"]["changed_path_hashes"]) == ["source.py"]


def test_run_shell_explicit_stdin_is_cleaned_after_failure(tmp_path):
    job, project = _make_job(tmp_path, "explicit-stdin")

    result = json.loads(job._ollama_run_tool(
        project,
        "run_shell",
        {"command": "python -", "stdin": "raise SystemExit(7)\n"},
    ))

    assert result["exit_code"] == 7
    assert list(project.iterdir()) == []
    assert not list(job.directory.glob(".shell-*"))


def test_run_shell_relative_path_selects_the_real_working_directory(tmp_path):
    job, project = _make_job(tmp_path, "relative-cwd")
    package = project / "phone_site"
    package.mkdir()

    result = json.loads(job._ollama_run_tool(
        project,
        "run_shell",
        {
            "command": "python -c \"from pathlib import Path; print(Path.cwd().name)\"",
            "relative_path": "phone_site",
        },
    ))

    assert result["exit_code"] == 0
    assert result["output"].strip() == "phone_site"
    assert Path(result["cwd"]) == package.resolve()


def test_run_shell_uses_reported_native_exit_code_for_verification(tmp_path):
    job, project = _make_job(tmp_path, "reported-exit")
    command = 'python -m py_compile missing.py 2>&1; echo "CHECK_EXIT=$LASTEXITCODE"'

    result = json.loads(job._ollama_run_tool(
        project,
        "run_shell",
        {"command": command},
    ))

    assert result["exit_code"] != 0
    assert result["verification"] == {
        "kind": "syntax",
        "status": "failed",
        "generation": 0,
        "exit_code": result["exit_code"],
    }


def test_run_shell_rejects_a_missing_working_directory(tmp_path):
    job, project = _make_job(tmp_path, "missing-cwd")

    result = json.loads(job._ollama_run_tool(
        project,
        "run_shell",
        {"command": "Write-Output never", "relative_path": "missing"},
    ))

    assert result["code"] == "working_directory_missing"
    assert not (project / "missing").exists()


def test_run_shell_rejects_two_stdin_sources_without_executing(tmp_path):
    job, project = _make_job(tmp_path, "conflicting-stdin")
    command = "python - <<'PY'\nprint('never runs')\nPY"

    result = json.loads(job._ollama_run_tool(
        project,
        "run_shell",
        {"command": command, "stdin": "print('also never runs')\n"},
    ))

    assert result["code"] == "conflicting_stdin"
    assert list(project.iterdir()) == []
    assert not list(job.directory.glob(".shell-*"))


def test_run_shell_schema_exposes_scratch_free_stdin_contract(tmp_path):
    job, _project = _make_job(tmp_path, "stdin-contract")

    run_shell = next(
        item["function"] for item in job._ollama_tools("edit app.py")
        if item["function"]["name"] == "run_shell"
    )

    assert "stdin" in run_shell["parameters"]["properties"]
    assert "relative_path" in run_shell["parameters"]["properties"]
    assert "scratch file" in run_shell["description"]
    assert "multiple run_shell tool calls" in run_shell["parameters"]["properties"]["command"]["description"]
