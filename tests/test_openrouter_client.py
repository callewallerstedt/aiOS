import json

import pytest

import openrouter_client


def test_credit_balance_is_remaining_account_credit_and_cached(monkeypatch):
    calls = []

    def request(path, payload, api_key, timeout):
        calls.append((path, payload, api_key, timeout))
        return {"data": {"total_credits": 40, "total_usage": 28.375}}

    monkeypatch.setattr(openrouter_client, "get_api_key", lambda **_kwargs: "test-key")
    monkeypatch.setattr(openrouter_client, "_request_json", request)
    openrouter_client.invalidate_cache()

    first = openrouter_client.credit_balance()
    second = openrouter_client.credit_balance()

    assert first == second == {
        "ok": True,
        "currency": "USD",
        "balance": 11.625,
        "total_credits": 40.0,
        "total_usage": 28.375,
    }
    assert calls == [("/credits", None, "test-key", 12)]


def test_credit_balance_reports_missing_key(monkeypatch):
    monkeypatch.setattr(openrouter_client, "get_api_key", lambda **_kwargs: "")
    openrouter_client.invalidate_cache()
    result = openrouter_client.credit_balance()
    assert result["ok"] is False
    assert "API key" in result["error"]


def test_catalog_includes_deepseek_v4_flash():
    ids = [row["id"] for row in openrouter_client.catalog_models()]
    assert "deepseek/deepseek-v4-flash" in ids


def test_enabled_models_default_to_catalog_defaults(tmp_path, monkeypatch):
    config_path = tmp_path / "helper_config.json"
    config_path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(openrouter_client, "CONFIG_PATH", config_path)
    openrouter_client.invalidate_cache()
    assert openrouter_client.enabled_model_ids() == ["deepseek/deepseek-v4-flash"]
    models = openrouter_client.list_enabled_models()
    assert models and models[0]["id"] == "deepseek/deepseek-v4-flash"
    assert models[0]["default"] is True


def test_enabled_models_respect_config(tmp_path, monkeypatch):
    config_path = tmp_path / "helper_config.json"
    config_path.write_text(
        '{"openrouter_enabled_models": ["deepseek/deepseek-v4-flash"], "openrouter_api_key": "sk-or-test"}',
        encoding="utf-8",
    )
    monkeypatch.setattr(openrouter_client, "CONFIG_PATH", config_path)
    openrouter_client.invalidate_cache()
    ready, message = openrouter_client.provider_status(use_cache=False)
    assert ready is True
    assert "1 model" in message
    caps = openrouter_client.capabilities()
    assert caps["provider"] == "openrouter"
    assert caps["ready"] is True
    assert caps["models"][0]["id"] == "deepseek/deepseek-v4-flash"


def test_configured_custom_model_id_is_not_discarded(tmp_path, monkeypatch):
    config_path = tmp_path / "helper_config.json"
    config_path.write_text(
        '{"openrouter_enabled_models": ["vendor/custom-coder"], "openrouter_api_key": "sk-or-test"}',
        encoding="utf-8",
    )
    monkeypatch.setattr(openrouter_client, "CONFIG_PATH", config_path)
    monkeypatch.setattr(openrouter_client, "MODEL_CACHE_PATH", tmp_path / "models.json")
    openrouter_client.invalidate_cache()

    assert openrouter_client.enabled_model_ids() == ["vendor/custom-coder"]
    assert openrouter_client.list_enabled_models()[0]["id"] == "vendor/custom-coder"


def test_dynamic_catalog_keeps_only_tool_capable_models(tmp_path, monkeypatch):
    monkeypatch.setattr(openrouter_client, "MODEL_CACHE_PATH", tmp_path / "models.json")
    monkeypatch.setattr(openrouter_client, "_request_json", lambda *_args, **_kwargs: {"data": [
        {
            "id": "vendor/tool-coder",
            "name": "Tool Coder",
            "context_length": 200000,
            "supported_parameters": ["tools", "reasoning"],
            "architecture": {"input_modalities": ["text"]},
        },
        {"id": "vendor/chat-only", "name": "Chat Only", "supported_parameters": ["temperature"]},
    ]})
    openrouter_client.invalidate_cache()

    ids = [row["id"] for row in openrouter_client.catalog_models(refresh=True)]

    assert "vendor/tool-coder" in ids
    assert "vendor/chat-only" not in ids
    assert (tmp_path / "models.json").is_file()


def test_dynamic_catalog_honors_mandatory_reasoning_efforts(tmp_path, monkeypatch):
    monkeypatch.setattr(openrouter_client, "MODEL_CACHE_PATH", tmp_path / "models.json")
    monkeypatch.setattr(openrouter_client, "_request_json", lambda *_args, **_kwargs: {"data": [{
        "id": "vendor/mandatory-reasoner",
        "name": "Mandatory Reasoner",
        "supported_parameters": ["tools", "reasoning_effort"],
        "reasoning": {
            "mandatory": True,
            "supported_efforts": ["max", "high", "low"],
            "default_effort": "max",
        },
    }]})
    openrouter_client.invalidate_cache()

    model = next(
        row for row in openrouter_client.catalog_models(refresh=True)
        if row["id"] == "vendor/mandatory-reasoner"
    )

    assert model["reasoning"] == ["max", "high", "low"]
    assert model["default_reasoning"] == "max"


def test_generic_reasoning_capability_is_a_toggle_not_invented_efforts(monkeypatch):
    model = openrouter_client._normalize_model({
        "id": "vendor/toggle-reasoner",
        "name": "Toggle Reasoner",
        "supported_parameters": ["tools", "reasoning"],
    })

    assert model["reasoning"] == ["off", "on"]
    assert model["default_reasoning"] == "off"
    monkeypatch.setattr(openrouter_client, "_MODEL_CACHE", [model])
    assert openrouter_client.reasoning_payload(model["id"], "off") == {"enabled": False}
    assert openrouter_client.reasoning_payload(model["id"], "high") == {"enabled": True}
    assert openrouter_client.reasoning_payload(model["id"], "on") == {"enabled": True}


def test_model_without_reasoning_capability_omits_reasoning_payload(monkeypatch):
    model = openrouter_client._normalize_model({
        "id": "vendor/plain-coder",
        "name": "Plain Coder",
        "supported_parameters": ["tools"],
    })
    monkeypatch.setattr(openrouter_client, "_MODEL_CACHE", [model])

    assert model["reasoning"] == ["off"]
    assert openrouter_client.reasoning_payload(model["id"], "off") is None


def test_missing_key_is_not_ready(tmp_path, monkeypatch):
    config_path = tmp_path / "helper_config.json"
    config_path.write_text('{"openrouter_enabled_models": ["deepseek/deepseek-v4-flash"]}', encoding="utf-8")
    monkeypatch.setattr(openrouter_client, "CONFIG_PATH", config_path)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    openrouter_client.invalidate_cache()
    ready, message = openrouter_client.provider_status(use_cache=False)
    assert ready is False
    assert "API key" in message


def test_reasoning_payload_maps_levels():
    assert openrouter_client.reasoning_payload("deepseek/deepseek-v4-flash", "off") == {"effort": "none"}
    assert openrouter_client.reasoning_payload("deepseek/deepseek-v4-flash", False) == {"effort": "none"}
    assert openrouter_client.reasoning_payload("deepseek/deepseek-v4-flash", "minimal") == {"effort": "minimal"}
    assert openrouter_client.reasoning_payload("deepseek/deepseek-v4-flash", "high") == {"effort": "high"}
    assert openrouter_client.reasoning_payload("deepseek/deepseek-v4-flash", "xhigh") == {"effort": "xhigh"}
    assert openrouter_client.reasoning_payload("deepseek/deepseek-v4-flash", "max") == {"effort": "max"}
    assert openrouter_client.reasoning_payload("deepseek/deepseek-v4-flash", "ultra") == {"effort": "ultra"}


def test_provider_none_effort_is_normalized_to_one_off_choice():
    model = openrouter_client._normalize_model({
        "id": "vendor/reasoner",
        "name": "Reasoner",
        "supported_parameters": ["tools", "reasoning_effort"],
        "reasoning": {
            "supported_efforts": ["max", "high", "none"],
            "default_effort": "none",
        },
    })

    assert model["reasoning"] == ["max", "high", "off"]
    assert model["default_reasoning"] == "off"


def test_reasoning_off_is_sent_explicitly(monkeypatch):
    captured = {}

    class Response:
        def __enter__(self): return self
        def __exit__(self, *_args): return False
        def __iter__(self): return iter((b"data: [DONE]\n",))

    def open_(request, **_kwargs):
        captured.update(json.loads(request.data.decode("utf-8")))
        return Response()

    monkeypatch.setattr(openrouter_client, "urlopen", open_)
    list(openrouter_client.stream_chat([], "vendor/model", api_key="test", reasoning="off"))
    assert captured["reasoning"] == {"effort": "none"}
    assert "max_completion_tokens" not in captured


def test_stream_chat_sends_an_explicit_completion_ceiling(monkeypatch):
    captured = {}

    class Response:
        def __enter__(self): return self
        def __exit__(self, *_args): return False
        def __iter__(self): return iter((b"data: [DONE]\n",))

    def open_(request, **_kwargs):
        captured.update(json.loads(request.data.decode("utf-8")))
        return Response()

    monkeypatch.setattr(openrouter_client, "urlopen", open_)
    list(openrouter_client.stream_chat(
        [],
        "vendor/model",
        api_key="test",
        max_completion_tokens=1152,
    ))

    assert captured["max_completion_tokens"] == 1152


def test_nonpositive_completion_ceiling_is_rejected_before_network_io(monkeypatch):
    monkeypatch.setattr(
        openrouter_client,
        "urlopen",
        lambda *_args, **_kwargs: pytest.fail("invalid budget reached the provider"),
    )

    with pytest.raises(ValueError, match="positive"):
        list(openrouter_client.stream_chat(
            [],
            "vendor/model",
            api_key="test",
            max_completion_tokens=0,
        ))


def test_stream_chat_preserves_final_provider_usage(monkeypatch):
    chunks = [
        {"id": "gen-1", "choices": [{"delta": {"content": "done"}}]},
        {
            "id": "gen-1",
            "choices": [{"delta": {}, "finish_reason": "length"}],
            "usage": {"prompt_tokens": 12, "completion_tokens": 3, "total_tokens": 15, "cost": 0.002},
        },
    ]

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def __iter__(self):
            for chunk in chunks:
                yield f"data: {json.dumps(chunk)}\n".encode()
            yield b"data: [DONE]\n"

    monkeypatch.setattr(openrouter_client, "urlopen", lambda *_args, **_kwargs: Response())
    events = list(openrouter_client.stream_chat(
        [{"role": "user", "content": "go"}],
        "deepseek/deepseek-v4-flash",
        api_key="test-key",
    ))

    assert events[-1]["done"] is True
    assert events[-1]["message"]["content"] == "done"
    assert events[-1]["message"]["finish_reason"] == "length"
    assert events[-1]["finish_reason"] == "length"
    assert events[-1]["usage"]["total_tokens"] == 15
    assert events[-1]["usage"]["cost"] == 0.002


def test_stream_chat_preserves_structured_reasoning_details_in_order(monkeypatch):
    details = [
        {
            "type": "reasoning.summary",
            "summary": "Checked the target before calling a tool.",
            "id": "summary-1",
            "format": "anthropic-claude-v1",
            "index": 0,
        },
        {
            "type": "reasoning.encrypted",
            "data": "opaque-provider-payload",
            "id": "encrypted-1",
            "format": "anthropic-claude-v1",
            "index": 1,
        },
    ]
    chunks = [
        {"choices": [{"delta": {"reasoning_details": [details[0]]}}]},
        {"choices": [{"delta": {"reasoning_details": [details[1]]}, "finish_reason": "stop"}]},
    ]

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def __iter__(self):
            for chunk in chunks:
                yield f"data: {json.dumps(chunk)}\n".encode()
            yield b"data: [DONE]\n"

    monkeypatch.setattr(openrouter_client, "urlopen", lambda *_args, **_kwargs: Response())
    events = list(openrouter_client.stream_chat(
        [{"role": "user", "content": "go"}],
        "vendor/reasoning-model",
        api_key="test-key",
    ))

    assert events[0]["delta"]["reasoning_details"] == [details[0]]
    assert events[1]["delta"]["reasoning_details"] == [details[1]]
    assert events[-1]["message"]["reasoning_details"] == details


def test_fast_stream_uses_openrouter_throughput_routing(monkeypatch):
    captured = {}

    class Response:
        def __enter__(self): return self
        def __exit__(self, *_args): return False
        def __iter__(self): return iter((b"data: [DONE]\n",))

    def open_(request, **_kwargs):
        captured.update(json.loads(request.data.decode("utf-8")))
        return Response()

    monkeypatch.setattr(openrouter_client, "urlopen", open_)
    list(openrouter_client.stream_chat([], "vendor/model", api_key="test", fast=True))
    assert captured["provider"] == {"sort": "throughput"}


def test_stream_chat_sends_a_bounded_sticky_session_id(monkeypatch):
    captured = {}

    class Response:
        def __enter__(self): return self
        def __exit__(self, *_args): return False
        def __iter__(self): return iter((b"data: [DONE]\n",))

    def open_(request, **_kwargs):
        captured.update(json.loads(request.data.decode("utf-8")))
        return Response()

    monkeypatch.setattr(openrouter_client, "urlopen", open_)
    list(openrouter_client.stream_chat(
        [], "vendor/model", api_key="test", session_id="x" * 400,
    ))

    assert captured["session_id"] == "x" * 256


def test_endpoint_tps_is_average_of_provider_p50(monkeypatch):
    monkeypatch.setattr(openrouter_client, "get_api_key", lambda: "test")
    monkeypatch.setattr(openrouter_client, "_request_json", lambda *_args, **_kwargs: {"data": {"endpoints": [
        {"status": "online", "throughput_last_30m": {"p50": 20}},
        {"status": "online", "throughput_last_30m": {"p50": 40}},
        {"status": "offline", "throughput_last_30m": {"p50": 100}},
    ]}})
    assert openrouter_client._endpoint_average_tps("vendor/model") == 30.0
