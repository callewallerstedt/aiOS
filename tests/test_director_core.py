"""Offline tests for the Director runtime: store, pairing, history rebuilding,
operator reply parsing and the model item translation."""
import json
import os

import pytest


@pytest.fixture()
def director(tmp_path, monkeypatch):
    """A Director package pointed at a throwaway home directory."""
    monkeypatch.setenv("AIOS_DIRECTOR_HOME", str(tmp_path / "home"))
    import director.config as config
    import director.store as store
    store.close()
    config.load_settings(refresh=True)
    yield store
    store.close()


def test_pairing_code_is_single_use(director):
    from director import auth

    code = auth.new_pairing_code()["code"]
    first = auth.redeem_pairing_code(code, name="Phone")
    assert first and first["token"]
    assert auth.redeem_pairing_code(code) is None


def test_pairing_code_is_not_stored_in_the_clear(director):
    from director import auth

    code = auth.new_pairing_code()["code"]
    rows = director.list_pairing_codes()
    assert rows and code not in json.dumps(rows)


def test_device_lookup_round_trips(director):
    from director import auth

    token = auth.redeem_pairing_code(auth.new_pairing_code()["code"])["token"]
    device = auth.device_for_token(token)
    assert device and device["kind"] == "phone"
    assert auth.device_for_token("not-a-token") is None


def test_wrong_token_never_matches_a_machine(director):
    from director import auth

    auth.enroll_machine(name="calle-windows", platform="windows", caps={"code": True})
    assert auth.machine_for_token("bogus") is None


def test_agents_seed_once(director):
    from director import agents

    first = agents.ensure_seeded()
    second = agents.ensure_seeded()
    assert [a["id"] for a in first] == [a["id"] for a in second]
    assert {"agt_director", "agt_operator", "agt_coder"} <= {a["id"] for a in second}


def test_seeding_refreshes_builtin_tool_lists(director):
    """Regression: a built-in seeded before a tool existed kept the old list
    forever. Asked to schedule something it could not, Director went and edited
    the database by hand instead."""
    from director import agents

    agents.ensure_seeded()
    director.update_agent("agt_director", {"tools": ["recall"]})
    assert director.get_agent("agt_director")["tools"] == ["recall"]

    agents.ensure_seeded()
    tools = director.get_agent("agt_director")["tools"]
    assert "schedule" in tools
    assert sorted(tools) == sorted(agents.DIRECTOR_TOOLS)


def test_seeding_leaves_custom_agents_alone(director):
    from director import agents

    agents.ensure_seeded()
    mine = director.create_agent(name="Mine", kind="custom", tools=["recall"])
    agents.ensure_seeded()
    assert director.get_agent(mine["id"])["tools"] == ["recall"]


def test_the_schedule_tools_are_on_the_default_lineup():
    from director import agents

    for spec in agents.DEFAULT_AGENTS:
        assert "schedule" in spec["tools"], f"{spec['name']} cannot schedule anything"


def test_history_rebuilds_tool_calls(director):
    from director import runtime

    agent = director.create_agent(name="T", agent_id="agt_t")
    thread = director.create_thread(agent["id"])
    director.add_message(thread["id"], "user", "check the disk")
    director.add_message(thread["id"], "tool_call", "", {
        "call_id": "call_1", "name": "shell", "arguments": '{"command":"df -h"}'})
    director.add_message(thread["id"], "tool_result", "", {
        "call_id": "call_1", "name": "shell", "output": "exit 0\n/dev/nvme0n1p2 48%"})
    director.add_message(thread["id"], "assistant", "You are at 48%.")

    items = runtime.build_items(thread["id"])
    assert [item["type"] for item in items] == [
        "message", "tool_call", "tool_result", "message"]
    assert items[1]["call_id"] == "call_1"
    assert "48%" in items[2]["output"]


def test_system_messages_become_user_items(director):
    """Job results are injected as system rows; the backends only take
    user/assistant, so they must be translated rather than dropped."""
    from director import runtime

    agent = director.create_agent(name="T", agent_id="agt_t2")
    thread = director.create_thread(agent["id"])
    director.add_message(thread["id"], "system", "[operator job finished: done] found it")

    items = runtime.build_items(thread["id"])
    assert items[0]["role"] == "user"
    assert "found it" in items[0]["content"][0]["text"]


def test_memory_survives_and_renders(director):
    from director.tools import memory as memory_tools

    director.remember("printer", "The 3D printer is on the Linux box at :5000")
    block = memory_tools.memory_block()
    assert "printer" in block and ":5000" in block


# ---------------- model item translation ----------------

def test_codex_input_translation():
    from director.models import codex, tool_call, tool_result, user_message

    items = [
        user_message("hello"),
        tool_call("call_9", "shell", '{"command":"ls"}'),
        tool_result("call_9", "exit 0"),
    ]
    converted = codex.to_input(items)
    assert converted[0]["content"][0]["type"] == "input_text"
    assert converted[1] == {"type": "function_call", "call_id": "call_9",
                            "name": "shell", "arguments": '{"command":"ls"}'}
    assert converted[2] == {"type": "function_call_output", "call_id": "call_9",
                            "output": "exit 0"}


def test_codex_assistant_text_uses_output_text():
    from director.models import assistant_message, codex

    converted = codex.to_input([assistant_message("done")])
    assert converted[0]["content"][0]["type"] == "output_text"


def test_openrouter_message_translation():
    from director.models import openrouter, tool_call, tool_result, user_message

    messages = openrouter.to_messages("be brief", [
        user_message("hi"),
        tool_call("call_3", "shell", "{}"),
        tool_result("call_3", "ok"),
    ])
    assert messages[0]["role"] == "system"
    assert messages[2]["tool_calls"][0]["function"]["name"] == "shell"
    assert messages[3] == {"role": "tool", "tool_call_id": "call_3", "content": "ok"}


def test_reasoning_none_maps_to_minimal_for_codex():
    """The aiOS UI calls the lowest level "none"; the endpoint calls it
    "minimal". Sending the wrong word silently costs reasoning tokens."""
    from director.models import codex

    assert codex.reasoning_block("none")["effort"] == "minimal"
    assert codex.reasoning_block("low")["effort"] == "low"
    assert codex.reasoning_block("xhigh")["effort"] == "high"
    assert "effort" not in codex.reasoning_block("")


def test_codex_token_expiry_reads_the_jwt():
    from director.models import codex

    assert codex.token_expiry("not.a.jwt") is None
    assert codex.token_client_id("not.a.jwt") == ""


# ---------------- operator ----------------

def test_operator_reply_parses_fenced_json():
    from director.operator import loop

    parsed = loop.parse_reply('```json\n{"thought":"t","status":"done","actions":[]}\n```')
    assert parsed["status"] == "done" and parsed["thought"] == "t"


def test_operator_reply_survives_chatter_around_json():
    from director.operator import loop

    parsed = loop.parse_reply('Sure!\n{"thought":"t","status":"continue",'
                              '"actions":[{"type":"click","x":10,"y":20}]}\nDone.')
    assert parsed["actions"][0]["x"] == 10


def test_operator_reply_failure_is_reported_not_raised():
    from director.operator import loop

    parsed = loop.parse_reply("I cannot do that")
    assert parsed["status"] == "fail" and parsed["actions"] == []


def test_action_coordinates_scale_back_to_real_pixels():
    """The model sees a shrunk screenshot; clicking where it says means scaling
    up first, or every click lands in the wrong place."""
    from director.operator import loop

    actions = [
        {"type": "click", "x": 100, "y": 50},
        {"type": "drag", "from": [10, 10], "to": [20, 20]},
        {"type": "path", "points": [[1, 2], [3, 4]]},
    ]
    scaled = loop.scale_actions(actions, 2.0)
    assert scaled[0] == {"type": "click", "x": 200, "y": 100}
    assert scaled[1]["from"] == [20, 20] and scaled[1]["to"] == [40, 40]
    assert scaled[2]["points"] == [[2, 4], [6, 8]]


def test_scaling_is_a_no_op_at_full_size():
    from director.operator import loop

    actions = [{"type": "click", "x": 7, "y": 9}]
    assert loop.scale_actions(actions, 1.0) == actions


def test_keysym_aliases_cover_what_the_prompt_promises():
    from director.operator import x11

    assert x11.keysym("enter") == "Return"
    assert x11.keysym("ctrl") == "ctrl"
    assert x11.keysym("f5") == "F5"
    assert x11.keysym("a") == "a"


# ---------------- tools ----------------

def test_destructive_tools_are_marked():
    """The approval gate keys off this flag, so a mislabelled tool is a hole."""
    from director import tools

    tools.load_all()
    by_name = {tool.name: tool for tool in tools.all_tools()}
    assert by_name["shell"].destructive
    assert by_name["write_file"].destructive
    assert not by_name["read_file"].destructive
    assert not by_name["web_fetch"].destructive


def test_every_agent_only_lists_real_tools():
    from director import agents, tools

    tools.load_all()
    for spec in agents.DEFAULT_AGENTS:
        missing = tools.missing(spec["tools"])
        assert not missing, f"{spec['name']} lists unknown tools: {missing}"


def test_registry_loads_fully_even_when_one_module_was_imported_first(tmp_path):
    """Regression: agents.py imports tools.memory for the memory block, which
    used to make the lazy loader think the registry was already populated. The
    coordinator then got three tools instead of nineteen and told the user it
    could not run anything."""
    import subprocess
    import sys

    script = (
        "import director.agents\n"                       # imports tools.memory
        "from director import tools\n"
        "names = sorted(t.name for t in tools.all_tools())\n"
        "print(len(names)); print(' '.join(names))\n"
    )
    env = {**os.environ, "AIOS_DIRECTOR_HOME": str(tmp_path / "home"),
           "PYTHONPATH": os.getcwd()}
    out = subprocess.run([sys.executable, "-c", script], capture_output=True,
                         text=True, env=env, cwd=os.getcwd())
    assert out.returncode == 0, out.stderr
    count, names = out.stdout.strip().splitlines()
    assert int(count) >= 15, f"only {count} tools registered: {names}"
    for expected in ("shell", "operator", "code_session", "web_fetch", "remember"):
        assert expected in names.split()


def test_shell_destructive_hints_catch_the_obvious():
    from director.tools import system

    assert system.looks_destructive("rm -rf /tmp/x")
    assert system.looks_destructive("sudo apt install nginx")
    assert not system.looks_destructive("ls -la ~/aios-director")


def test_readable_text_strips_scripts_and_keeps_words():
    from director.tools import web

    html = ("<html><head><title>Hi</title><script>var x=1;</script></head>"
            "<body><p>First line</p><p>Second &amp; last</p></body></html>")
    text = web.readable_text(html)
    assert "var x" not in text
    assert "First line" in text and "Second & last" in text
    assert web.page_title(html) == "Hi"


def test_every_agent_gets_the_slack_coworker_base_prompt(director):
    from director import agents

    agents.ensure_seeded()
    prompt = agents.system_prompt(director.get_agent("agt_director"), {})
    assert "sharp coworker in Slack" in prompt
    assert "operator_screenshot" in prompt
    operator = agents.system_prompt(director.get_agent("agt_operator"), {})
    assert "sharp coworker in Slack" in operator
    assert "You are part of aiOS Director" in operator


def test_takeover_path_is_the_chrome_free_viewer():
    from director.operator import display as display_mod

    assert display_mod.takeover_path() == "/vnc/view"


def test_appearance_defaults_cover_both_bubbles():
    from director import config

    look = config.DEFAULT_SETTINGS["appearance"]
    assert look["user_bubble"].startswith("#")
    assert look["agent_bubble"].startswith("#")
    assert look["user_text"].startswith("#")
    assert look["agent_text"].startswith("#")


def test_user_image_attachments_become_model_parts(director):
    from director import runtime

    agent = director.create_agent(name="T", agent_id="agt_img")
    thread = director.create_thread(agent["id"])
    director.add_message(thread["id"], "user", "what is this", {
        "attachments": [{"type": "image/jpeg", "url": "data:image/jpeg;base64,QQ=="}],
    })
    items = runtime.build_items(thread["id"])
    parts = items[0]["content"]
    assert parts[0]["type"] == "text"
    assert parts[1] == {"type": "image", "url": "data:image/jpeg;base64,QQ=="}


def test_operator_screenshot_reaches_the_model_as_an_image(director):
    from director import runtime

    agent = director.create_agent(name="T", agent_id="agt_shot")
    thread = director.create_thread(agent["id"])
    director.add_message(thread["id"], "user", "look")
    director.add_message(thread["id"], "tool_call", "", {
        "call_id": "c1", "name": "operator_screenshot", "arguments": "{}"})
    director.add_message(thread["id"], "tool_result", "", {
        "call_id": "c1", "name": "operator_screenshot",
        "output": "Screenshot taken (800x600).",
        "image": "data:image/jpeg;base64,QQ=="})
    items = runtime.build_items(thread["id"])
    kinds = [(item["type"], item.get("role"), item.get("name")) for item in items]
    assert kinds[0][0] == "message"
    assert kinds[1][0] == "tool_call"
    assert kinds[2][0] == "tool_result"
    assert items[3]["type"] == "message" and items[3]["role"] == "user"
    assert any(part.get("type") == "image" for part in items[3]["content"])


def test_a_follow_up_is_only_started_when_the_last_row_is_still_the_user(director):
    """Mid-run steering is already in the transcript. After the turn, start
    another only if Calle's last message has not been answered yet."""
    agent = director.create_agent(name="T", agent_id="agt_q")
    thread = director.create_thread(agent["id"])
    director.add_message(thread["id"], "user", "one")
    director.add_message(thread["id"], "assistant", "answered")
    messages = director.list_messages(thread["id"])
    assert messages[-1]["role"] == "assistant"
    director.add_message(thread["id"], "user", "two")
    messages = director.list_messages(thread["id"])
    assert messages[-1]["role"] == "user"
