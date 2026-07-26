"""The Codex stream must not be able to run forever.

httpx's timeout is per read, so it resets on every chunk. A stream that keeps
dribbling bytes without ever sending `response.completed` therefore never times
out — which is how a run froze mid-step with no error at all.
"""

import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "agent_clicker"))

from agent import codex_backend  # noqa: E402


def _dribble():
    """A stream that is always busy and never finishes."""
    while True:
        yield b": keep-alive\n\n"
        time.sleep(0.01)


def test_a_stream_that_never_completes_is_cut_off():
    deadline = time.monotonic() + 0.3
    with pytest.raises(TimeoutError) as caught:
        codex_backend._parse_sse(_dribble(), deadline=deadline)
    assert "without completing" in str(caught.value)


def test_a_stream_that_completes_in_time_is_returned():
    events = [
        b'data: {"type":"response.output_text.delta","delta":"{\\"ok\\""}\n\n',
        b'data: {"type":"response.output_text.delta","delta":":true}"}\n\n',
        b'data: {"type":"response.completed","response":{"usage":'
        b'{"input_tokens":10,"output_tokens":4,"total_tokens":14}}}\n\n',
    ]
    text, usage = codex_backend._parse_sse(iter(events),
                                           deadline=time.monotonic() + 30)
    assert text == '{"ok":true}'
    assert usage["input_tokens"] == 10
    assert usage["output_tokens"] == 4


def test_no_deadline_still_works():
    """Callers that pass nothing keep the old behaviour."""
    events = [b'data: {"type":"response.output_text.delta","delta":"hi"}\n\n']
    text, _ = codex_backend._parse_sse(iter(events))
    assert text == "hi"


def test_the_model_timeout_env_var_reaches_this_backend(monkeypatch):
    """It only governed the API path before, so Codex calls ignored it."""
    monkeypatch.setenv("AIOS_MODEL_TIMEOUT", "42")
    assert codex_backend._env_float("AIOS_MODEL_TIMEOUT", 150.0) == 42.0
    monkeypatch.setenv("AIOS_MODEL_TIMEOUT", "nonsense")
    assert codex_backend._env_float("AIOS_MODEL_TIMEOUT", 150.0) == 150.0
    monkeypatch.setenv("AIOS_MODEL_TIMEOUT", "0")
    assert codex_backend._env_float("AIOS_MODEL_TIMEOUT", 150.0) == 150.0
