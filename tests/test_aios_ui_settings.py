"""The WebView2 Settings backend.

The web UI has no widget callbacks to hold the clamping rules, so every rule the
Tk build kept next to a tk.Scale now lives in aios_ui.settings_api. These tests
pin the ones that would silently corrupt a config if they drifted: the theme
ranges, the operator provider_mode pairing, and the "off" thinking level that
voice_settings.merge_voice_dictation does not know about.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from aios_ui import settings_api  # noqa: E402


@pytest.fixture
def config(monkeypatch):
    """An in-memory helper_config, so tests never touch the real one."""
    store = {
        "theme": {"accent": "#61dafb", "opacity": 0.94, "font_size": 10, "radius": 28},
        "ai_operator": {"model": "gpt-5.6-luna", "steps": "25", "delay": "0.20", "provider_mode": "api"},
        "voice_dictation": {},
        "phone_relay": {},
        "openrouter_enabled_models": ["deepseek/deepseek-v4-flash"],
    }
    monkeypatch.setattr(settings_api, "load_config", lambda: store)
    monkeypatch.setattr(settings_api, "save_config", lambda cfg: store.update(cfg))
    monkeypatch.setattr(settings_api, "nudge_voice", lambda: None)
    return store


def test_theme_numbers_are_clamped_to_the_tk_ranges(config):
    theme = settings_api.save_theme(
        {"opacity": 2.0, "font_size": 99, "radius": 1, "thinking_base_opacity": 400}
    )["theme"]
    assert theme["opacity"] == 1.0
    assert theme["font_size"] == 15
    assert theme["radius"] == 1
    assert theme["thinking_base_opacity"] == 100

    exact = settings_api.save_theme({"radius": 0.375})["theme"]
    assert exact["radius"] == 0.375
    square = settings_api.save_theme({"radius": -5})["theme"]
    assert square["radius"] == 0


def test_theme_colors_pass_through_untouched(config):
    theme = settings_api.save_theme({
        "accent": "#00ff88",
        "app_background": "#111111",
        "code_chat_background": "#222222",
        "code_sidebar_background": "#333333",
        "chat_link": "#ff9de2",
    })["theme"]
    assert theme["accent"] == "#00ff88"
    assert theme["app_background"] == "#111111"
    assert theme["code_chat_background"] == "#222222"
    assert theme["code_sidebar_background"] == "#333333"
    assert theme["chat_link"] == "#ff9de2"
    # Unrelated keys survive a partial patch.
    assert theme["radius"] == 28


def test_operator_steps_and_delay_keep_their_string_shape(config):
    operator = settings_api.save_operator({"steps": 40.0, "delay": 1.5})["ai_operator"]
    # The OPERATOR loop parses these from strings; a float would break it.
    assert operator["steps"] == "40"
    assert operator["delay"] == "1.50"


def test_codex_auth_moves_provider_mode_with_it(config):
    assert settings_api.save_operator({"codex_auth": True})["ai_operator"]["provider_mode"] == "codex"
    assert settings_api.save_operator({"codex_auth": False})["ai_operator"]["provider_mode"] == "api"


def test_voice_thinking_off_survives_the_merge(config):
    """merge_voice_dictation only knows minimal/low/medium/high.

    "off" is a real choice for local Ollama models (it skips the thinking pass
    entirely), so it has to be re-applied after the merge or the UI would show a
    setting that silently reverts to "low".
    """
    voice = settings_api.save_voice({"agent_reasoning": "off"})["voice_dictation"]
    assert voice["agent_reasoning"] == "off"
    assert settings_api.save_voice({"agent_reasoning": "high"})["voice_dictation"]["agent_reasoning"] == "high"


def test_voice_patch_is_clamped_by_the_shared_merge(config):
    voice = settings_api.save_voice({"hold_ms": 5000, "agent_max_rounds": 99})["voice_dictation"]
    assert voice["hold_ms"] == 800
    assert voice["agent_max_rounds"] == 12


def test_openrouter_never_saves_an_empty_model_list(config):
    saved = settings_api.save_openrouter_models([])["openrouter_enabled_models"]
    assert saved, "CODE's model picker must never be left with nothing to select"


def test_dispatch_returns_none_for_routes_it_does_not_own():
    assert settings_api.dispatch("/api/code/jobs", "GET", {}, {}) is None
    assert settings_api.dispatch("/api/settings/nope", "POST", {}, {}) is None


def test_dispatch_routes_a_theme_patch(config):
    result = settings_api.dispatch("/api/settings/theme", "POST", {}, {"patch": {"font_size": 12}})
    assert result["ok"] is True
    assert result["theme"]["font_size"] == 12


def test_unknown_quick_tool_is_reported_not_raised():
    result = settings_api.run_tool("nonsense", {})
    assert result["ok"] is False
    assert "nonsense" in result["error"]


def test_settings_meta_does_not_start_ollama(monkeypatch):
    """Opening Settings used to block for seconds while ensure_ollama ran."""
    calls = []

    def fake_list(*, use_cache=True, ensure=True, **_kwargs):
        calls.append({"use_cache": use_cache, "ensure": ensure})
        return [{"id": "llama", "description": "local"}]

    import ollama_client

    monkeypatch.setattr(ollama_client, "_MODELS_CACHE", None)
    monkeypatch.setattr(ollama_client, "list_installed_models", fake_list)

    models = settings_api._ollama_models(wait_s=1.0)
    assert calls and calls[0]["ensure"] is False
    assert models == [{"id": "ollama:llama", "label": "ollama:llama", "hint": "local"}]


def test_settings_ollama_probe_gives_up_quickly(monkeypatch):
    """A hung Ollama must not keep /api/settings/meta waiting."""
    import ollama_client

    def hang(*, use_cache=True, ensure=True, **_kwargs):
        time.sleep(2.0)
        return [{"id": "late"}]

    monkeypatch.setattr(ollama_client, "_MODELS_CACHE", None)
    monkeypatch.setattr(ollama_client, "list_installed_models", hang)
    started = time.perf_counter()
    assert settings_api._ollama_models(wait_s=0.2) == []
    assert time.perf_counter() - started < 0.8
