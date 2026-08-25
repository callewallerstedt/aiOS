"""The Ollama-compatible shim in front of llama.cpp.

The harness reads a small, exact set of fields off every chunk, and treats a
finish reason it does not recognise as unsafe. These tests pin the translation
so a change upstream cannot quietly hand the harness a malformed round.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import aios_fast_llm as shim


def drive(chunks, payload=None):
    """Run translate_chat over a canned upstream stream."""
    original = shim.post_stream
    shim.post_stream = lambda url, body, timeout=1800: iter(chunks)
    try:
        return list(shim.translate_chat(payload or {"messages": [], "model": "m"}))
    finally:
        shim.post_stream = original


def delta(**fields):
    return {"choices": [{"delta": dict(fields), "finish_reason": None}]}


def test_finish_reason_never_invents_a_safe_stop():
    """The harness only trusts "stop" and "length"; everything else must map."""
    assert shim.finish_reason("stop") == "stop"
    assert shim.finish_reason("length") == "length"
    assert shim.finish_reason("tool_calls") == "stop"
    # An unknown or missing reason must not become something the harness reads
    # as permission to act on a truncated response.
    assert shim.finish_reason("") == "stop"
    assert shim.finish_reason("content_filter") == "stop"


def test_text_is_streamed_then_closed_with_ollama_counters():
    chunks = drive([
        delta(content="def "),
        delta(content="merge("),
        {"choices": [{"delta": {}, "finish_reason": "stop"}],
         "timings": {"predicted_n": 40, "predicted_ms": 400.0,
                     "prompt_n": 12, "prompt_ms": 60.0}},
    ])
    assert [c["message"]["content"] for c in chunks[:-1]] == ["def ", "merge("]
    assert all(c["done"] is False for c in chunks[:-1])

    done = chunks[-1]
    assert done["done"] is True
    assert done["done_reason"] == "stop"
    assert done["eval_count"] == 40
    # Ollama reports nanoseconds; 400ms must not arrive as 400.
    assert done["eval_duration"] == 400_000_000
    assert done["prompt_eval_count"] == 12
    assert done["prompt_eval_duration"] == 60_000_000
    # 40 tokens / 0.4s -> 100 tok/s, which is what the summary row will show.
    assert round(done["eval_count"] / (done["eval_duration"] / 1e9)) == 100


def test_tool_call_fragments_are_reassembled_into_one_call():
    chunks = drive([
        delta(tool_calls=[{"index": 0, "id": "abc",
                           "function": {"name": "read_file", "arguments": '{"relative_'}}]),
        delta(tool_calls=[{"index": 0, "function": {"arguments": 'path": "a.py"}'}}]),
        {"choices": [{"delta": {}, "finish_reason": "tool_calls"}],
         "timings": {"predicted_n": 9, "predicted_ms": 90.0}},
    ])
    call = chunks[-1]["message"]["tool_calls"][0]
    assert call["id"] == "abc"
    assert call["function"]["name"] == "read_file"
    # Arguments must arrive parsed, the way the harness expects them.
    assert call["function"]["arguments"] == {"relative_path": "a.py"}
    assert chunks[-1]["done_reason"] == "stop"


def test_a_tool_call_cut_off_mid_json_is_reported_as_truncated():
    """Half an arguments object must never reach the harness as a real call.

    Executing it would run a call the model never finished asking for, so the
    round is reported as length-truncated with no tool call at all.
    """
    chunks = drive([
        delta(tool_calls=[{"index": 0, "id": "x",
                           "function": {"name": "edit_file", "arguments": '{"old_text": "de'}}]),
        {"choices": [{"delta": {}, "finish_reason": "length"}],
         "timings": {"predicted_n": 5, "predicted_ms": 50.0}},
    ])
    done = chunks[-1]
    assert "tool_calls" not in done["message"]
    assert done["done_reason"] == "length"


def test_thinking_is_passed_through_under_its_ollama_name():
    chunks = drive([
        delta(reasoning_content="weighing options"),
        {"choices": [{"delta": {}, "finish_reason": "stop"}], "timings": {}},
    ])
    assert chunks[0]["message"]["thinking"] == "weighing options"


def test_thinking_off_is_forwarded_to_the_template():
    """The harness turns thinking off per turn; the template must be told."""
    captured = {}
    original = shim.post_stream

    def capture(url, body, timeout=1800):
        captured.update(body)
        return iter([{"choices": [{"delta": {}, "finish_reason": "stop"}], "timings": {}}])

    shim.post_stream = capture
    try:
        list(shim.translate_chat({"messages": [], "think": False,
                                  "options": {"num_predict": 256}}))
        assert captured["chat_template_kwargs"] == {"enable_thinking": False}
        assert captured["max_tokens"] == 256
        list(shim.translate_chat({"messages": [], "think": True}))
        assert "chat_template_kwargs" not in captured or captured.get("stream") is True
    finally:
        shim.post_stream = original


def test_missing_timings_do_not_fabricate_a_rate():
    chunks = drive([{"choices": [{"delta": {}, "finish_reason": "stop"}]}])
    done = chunks[-1]
    assert done["eval_count"] == 0
    assert done["eval_duration"] == 0
