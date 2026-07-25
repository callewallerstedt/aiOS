import sys
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


AGENT_ROOT = Path(__file__).resolve().parents[1] / "agent_clicker"
if str(AGENT_ROOT) not in sys.path:
    sys.path.insert(0, str(AGENT_ROOT))

from agent import codex_backend, config, vlm


def test_operator_codex_fallback_uses_api_and_records_it():
    usage = SimpleNamespace(
        prompt_tokens=12,
        completion_tokens=4,
        total_tokens=16,
        prompt_tokens_details=SimpleNamespace(cached_tokens=0),
    )
    response = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content='{"done":true}'))],
        usage=usage,
    )
    fake_client = SimpleNamespace(
        chat=SimpleNamespace(
            completions=SimpleNamespace(create=mock.Mock(return_value=response)),
        )
    )

    with (
        mock.patch.object(config, "OPENAI_API_KEY", "sk-test-key-with-enough-characters"),
        mock.patch.object(codex_backend, "chat_with_usage", side_effect=RuntimeError("Codex offline")),
        mock.patch.object(vlm, "client", return_value=fake_client),
    ):
        text, result_usage = vlm.chat_with_usage(
            "Return JSON.",
            [{"role": "user", "content": "Continue."}],
            model="gpt-test",
            backend="codex_fallback",
        )

    assert text == '{"done":true}'
    assert result_usage["backend"] == "api"
    assert result_usage["fallback_from"] == "codex"
    assert result_usage["total_tokens"] == 16
