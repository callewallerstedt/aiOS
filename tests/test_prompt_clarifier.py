import json
from unittest import mock

import pytest

import prompt_clarifier


def test_question_limit_grows_without_exceeding_ten():
    assert prompt_clarifier.question_limit("Open Paint.") == 2
    large = "\n".join(f"- change independent setting {index}" for index in range(20))
    assert prompt_clarifier.question_limit(large) == 10


def test_api_provider_requires_a_key():
    with pytest.raises(RuntimeError, match="API key"):
        prompt_clarifier.clarify_prompt_for_provider(
            "Update the app.",
            provider_mode="api",
            api_key="",
        )


def test_codex_api_fallback_uses_api_after_codex_failure():
    api_result = {
        "questions": [{"id": "target", "question": "Which target?", "answered": False}],
        "provider": "api",
    }
    with (
        mock.patch.object(prompt_clarifier, "clarify_prompt_codex", side_effect=RuntimeError("offline")),
        mock.patch.object(prompt_clarifier, "clarify_prompt", return_value=api_result) as api_call,
    ):
        result = prompt_clarifier.clarify_prompt_for_provider(
            "Update it.",
            provider_mode="codex_api_fallback",
            api_key="sk-test-key-with-enough-characters",
        )

    assert result["provider"] == "api"
    assert result["fallback_from"] == "codex"
    api_call.assert_called_once()


def test_previous_questions_keep_identity_when_answered():
    previous = [{"id": "target_path", "question": "Which folder?", "answered": False}]
    payload, limit = prompt_clarifier._request_payload(
        r"Use C:\aiOS\phone_site.",
        previous,
    )
    assert limit >= 2
    assert payload["previous_questions"] == previous


def test_codex_intent_check_uses_the_fast_nano_model():
    assert prompt_clarifier.CODEX_MODEL == prompt_clarifier.MODEL
    assert "nano" in prompt_clarifier.CODEX_MODEL


def test_api_payload_caps_output_tokens():
    with mock.patch("urllib.request.urlopen") as urlopen:
        urlopen.return_value.__enter__.return_value.read.return_value = json.dumps({
            "choices": [{"message": {"content": '{"questions":[]}'}}],
        }).encode("utf-8")
        prompt_clarifier.clarify_prompt("sk-test", "Open Notepad and type hello.")
    payload = json.loads(urlopen.call_args[0][0].data.decode("utf-8"))
    assert payload["model"] == prompt_clarifier.MODEL
    assert payload["max_completion_tokens"] == prompt_clarifier.MAX_OUTPUT_TOKENS
    assert "reasoning_effort" not in payload
