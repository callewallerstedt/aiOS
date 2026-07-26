"""The operator's shell: on by default, and able to reach cmd as well as ps."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "agent_clicker"))

from desktop_agent import shell as ps  # noqa: E402


def test_the_timeout_is_generous_but_bounded():
    assert ps.clamp_timeout(None) == ps.DEFAULT_TIMEOUT
    assert ps.clamp_timeout("nonsense") == ps.DEFAULT_TIMEOUT
    assert ps.clamp_timeout(0) == ps.DEFAULT_TIMEOUT
    assert ps.clamp_timeout(-5) == ps.DEFAULT_TIMEOUT
    assert ps.clamp_timeout(60) == 60
    # A model asking for an hour must not be able to wedge the run.
    assert ps.clamp_timeout(99999) == ps.MAX_TIMEOUT


def test_shell_is_on_by_default_for_new_installs():
    root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(root))
    import re

    # Read the default rather than importing the Tk app, which needs a display.
    source = (root / "helper_overlay.py").read_text(encoding="utf-8", errors="replace")
    block = re.search(r'"ai_operator":\s*\{(.+?)\n    \},', source, re.DOTALL)
    assert block, "could not find the ai_operator defaults"
    assert re.search(r'"shell":\s*True', block.group(1)), \
        "the operator should ship with shell access enabled"


def test_powershell_still_runs_a_one_liner():
    result = ps.run("Write-Output 'hello-ps'", timeout=60)
    assert result.exit_code == 0, result.stderr
    assert "hello-ps" in result.stdout


def test_cmd_is_reachable_for_bat_style_commands():
    result = ps.run("echo hello-cmd", timeout=60, interpreter="cmd")
    assert result.exit_code == 0, result.stderr
    assert "hello-cmd" in result.stdout


def test_an_unknown_interpreter_falls_back_to_powershell():
    result = ps.run("Write-Output $PSVersionTable.PSVersion.Major",
                    timeout=60, interpreter="fish")
    assert result.exit_code == 0, result.stderr
    assert result.stdout.strip().isdigit()
