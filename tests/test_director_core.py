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


def test_every_default_agent_has_web_search_and_fetch():
    from director import agents

    for spec in agents.DEFAULT_AGENTS:
        names = spec["tools"]
        assert "web_search" in names, f"{spec['name']} cannot search the web"
        assert "web_fetch" in names, f"{spec['name']} cannot fetch a page"


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


def test_compacted_context_replaces_only_the_model_facing_past(director):
    from director import runtime

    agent = director.create_agent(name="T", agent_id="agt_compact")
    thread = director.create_thread(agent["id"])
    first = director.add_message(thread["id"], "user", "the old request")
    director.add_message(thread["id"], "assistant", "the old answer")
    through = director.latest_message_sequence(thread["id"])
    director.save_compaction(thread["id"], "The user made an old request and it was answered.", through)
    director.add_message(thread["id"], "user", "the new request")

    # Full history remains available to the disclosure UI.
    assert [row["content"] for row in director.list_messages(thread["id"])] == [
        "the old request", "the old answer", "the new request"]
    items = runtime.build_items(thread["id"])
    text = [part["text"] for item in items for part in item.get("content", [])
            if part.get("type") == "text"]
    assert any("Compacted conversation context" in value for value in text)
    assert "the new request" in text
    assert "the old request" not in text
    assert first["id"]


def test_idle_compaction_is_due_once_until_new_activity(director):
    import time

    agent = director.create_agent(name="T", agent_id="agt_due")
    thread = director.create_thread(agent["id"])
    director.add_message(thread["id"], "user", "hello")
    director._exec("UPDATE threads SET updated_at = ? WHERE id = ?",
                   (time.time() - 3700, thread["id"]))
    due = director.threads_due_for_compaction(before=time.time() - 3600)
    assert [row["id"] for row in due] == [thread["id"]]

    through = director.latest_message_sequence(thread["id"])
    director.save_compaction(thread["id"], "hello", through)
    assert director.threads_due_for_compaction(before=time.time() - 3600) == []

    director.add_message(thread["id"], "assistant", "new reply")
    director.touch_thread(thread["id"])
    director._exec("UPDATE threads SET updated_at = ? WHERE id = ?",
                   (time.time() - 3700, thread["id"]))
    assert [row["id"] for row in director.threads_due_for_compaction(
        before=time.time() - 3600)] == [thread["id"]]


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
    for expected in ("shell", "operator", "code_session", "web_fetch", "web_search", "remember"):
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


def test_search_parser_unwraps_duckduckgo_redirects():
    from director.tools import web

    markup = """
    <a href="https://duckduckgo.com/lite/">Home</a>
    <a href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fdocs">Example docs</a>
    <a href="https://news.ycombinator.com/item?id=1">HN</a>
    """
    rows = web.parse_search_results(markup, limit=5)
    assert [row["url"] for row in rows] == [
        "https://example.com/docs",
        "https://news.ycombinator.com/item?id=1",
    ]
    assert rows[0]["title"] == "Example docs"
    text = web.format_search_results("example", rows)
    assert "https://example.com/docs" in text


def test_every_agent_gets_the_slack_coworker_base_prompt(director):
    from director import agents

    agents.ensure_seeded()
    prompt = agents.system_prompt(director.get_agent("agt_director"), {})
    assert "sharp coworker in Slack" in prompt
    assert "operator_screenshot" in prompt
    operator = agents.system_prompt(director.get_agent("agt_operator"), {})
    assert "sharp coworker in Slack" in operator
    assert "You are part of aiOS Director" in operator


def test_house_instructions_reach_every_agent(director):
    from director import agents, config

    assert config.DEFAULT_SETTINGS["instructions"] == ""
    agents.ensure_seeded()
    settings = {"instructions": "Always reply in Swedish. Never buy anything."}
    for agent_id in ("agt_director", "agt_operator", "agt_coder"):
        prompt = agents.system_prompt(director.get_agent(agent_id), settings)
        assert "Always reply in Swedish. Never buy anything." in prompt
        assert "standing instructions for every agent" in prompt
    empty = agents.system_prompt(director.get_agent("agt_director"), {})
    assert "standing instructions for every agent" not in empty
    custom = director.create_agent(name="Shop", kind="custom", agent_id="agt_shop")
    assert "Always reply in Swedish" in agents.system_prompt(custom, settings)
    members = [director.get_agent("agt_director"), director.get_agent("agt_operator")]
    group_prompt = agents.group_system_prompt(
        members[0], {"name": "Kitchen", "rules": ""}, members, settings)
    assert "Always reply in Swedish" in group_prompt
    config.update_settings({"instructions": "Speak like a pirate."})
    loaded = agents.system_prompt(director.get_agent("agt_director"))
    assert "Speak like a pirate." in loaded
    assert len(agents.house_instructions({"instructions": "x" * 9000})) == 8000


def test_funnel_prefix_is_stripped_so_vnc_routes_match():
    from director.server import strip_funnel_prefix

    assert strip_funnel_prefix("/director/vnc/view") == "/vnc/view"
    assert strip_funnel_prefix("/director/vnc/ws") == "/vnc/ws"
    assert strip_funnel_prefix("/director") == "/"
    assert strip_funnel_prefix("/api/health") == "/api/health"


def test_takeover_path_is_the_chrome_free_viewer():
    from director.operator import display as display_mod

    assert display_mod.takeover_path() == "/vnc/view"


def test_chrome_flags_paint_on_a_virtual_screen(director, monkeypatch):
    from director.operator import display as display_mod

    monkeypatch.setattr(display_mod, "chrome_binary", lambda: "/usr/bin/google-chrome-stable")
    argv = display_mod.chrome_argv("https://example.com")
    assert "--ozone-platform=x11" in argv
    assert "--use-angle=swiftshader" in argv
    assert "--no-sandbox" in argv
    assert "--restore-last-session" in argv
    assert argv[-1] == "https://example.com"


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


def test_ensure_running_skips_probes_when_the_display_is_already_ready(monkeypatch):
    """Screenshot polls used to shell out to systemctl/xsetroot/pgrep every
    time. Once the display is up, a short cache must skip that work so the
    operator screen can open without waiting on four systemd round-trips."""
    import asyncio

    from director.operator import display as display_mod

    display_mod.reset_ready_cache()
    calls = {"active": 0, "run": 0, "chrome": 0, "launch": 0}

    async def fake_active(unit):
        calls["active"] += 1
        return True

    async def fake_run(argv, timeout=10, env=None):
        calls["run"] += 1
        return 0, "active"

    async def fake_chrome(settings=None):
        calls["chrome"] += 1
        return True

    async def fake_launch(url="", settings=None):
        calls["launch"] += 1
        return "opened"

    monkeypatch.setattr(display_mod, "unit_active", fake_active)
    monkeypatch.setattr(display_mod, "_run", fake_run)
    monkeypatch.setattr(display_mod, "chrome_running", fake_chrome)
    monkeypatch.setattr(display_mod, "launch_chrome", fake_launch)

    async def fake_alive(settings=None):
        return True

    monkeypatch.setattr(display_mod, "display_alive", fake_alive)

    first = asyncio.run(display_mod.ensure_running({"operator": {}}))
    second = asyncio.run(display_mod.ensure_running({"operator": {}}))
    display_mod.reset_ready_cache()
    assert first["ready"] is True
    assert second is first
    assert calls["active"] == 2  # wm + vnc; xvfb skipped because the display is up
    assert calls["chrome"] == 0
    assert calls["run"] == 0
    assert calls["launch"] == 0


def test_ensure_running_starts_chrome_when_asked(monkeypatch):
    """Opening the operator screen must bring Chrome up on that display.
    Screenshot polls must not, or they relaunch it over the live session."""
    import asyncio

    from director.operator import display as display_mod

    display_mod.reset_ready_cache()
    calls = {"chrome": 0, "launch": 0}

    async def fake_alive(settings=None):
        return True

    async def fake_active(unit):
        return True

    async def fake_chrome(settings=None):
        calls["chrome"] += 1
        return False

    async def fake_launch(url="", settings=None):
        calls["launch"] += 1
        return "opened"

    monkeypatch.setattr(display_mod, "display_alive", fake_alive)
    monkeypatch.setattr(display_mod, "unit_active", fake_active)
    monkeypatch.setattr(display_mod, "chrome_running", fake_chrome)
    monkeypatch.setattr(display_mod, "launch_chrome", fake_launch)

    asyncio.run(display_mod.ensure_running({"operator": {}}, with_chrome=True))
    display_mod.reset_ready_cache()
    assert calls["chrome"] == 1
    assert calls["launch"] == 1


def test_group_agents_store_members_and_rules(director):
    from director import agents

    agents.ensure_seeded()
    group = director.create_agent(
        name="Ops", kind="group",
        members=["agt_coder", "agt_operator", "grp_nope"],
        rules="Keep it short.")
    assert group["id"].startswith("grp_")
    assert director.is_group(group)
    assert group["members"] == ["agt_coder", "agt_operator", "grp_nope"]
    resolved = agents.resolve_member_ids(group["members"] + ["agt_coder", "missing"])
    assert resolved == ["agt_coder", "agt_operator"]
    prompt = agents.group_system_prompt(
        director.get_agent("agt_coder"), group, agents.group_members(group), {})
    assert "Keep it short." in prompt
    assert "You are **Coder**" in prompt
    assert "SILENT" in prompt
    assert "start_work" in prompt
    assert "`react`" in prompt or "react" in prompt
    assert "@Coder" in prompt
    assert "@Operator" in prompt


def test_group_prompt_includes_private_thread_and_mentions(director):
    from director import agents

    agents.ensure_seeded()
    private = director.latest_thread("agt_coder") or director.create_thread("agt_coder")
    director.add_message(private["id"], "user", "Please tidy the login form.")
    director.add_message(private["id"], "assistant", "I restyled the login form.")
    group = director.create_agent(
        name="Ops", kind="group", members=["agt_coder", "agt_operator"])
    prompt = agents.group_system_prompt(
        director.get_agent("agt_coder"), group, agents.group_members(group), {})
    assert "restyled the login form" in prompt
    assert "Please tidy the login form." in prompt
    tagged = agents.mentions_in("@Operator take this", agents.group_members(group))
    assert [row["id"] for row in tagged] == ["agt_operator"]
    named = agents.mentions_in("yo Coder plan this with Operator", agents.group_members(group))
    assert {row["id"] for row in named} == {"agt_coder", "agt_operator"}
    assert agents.mentions_in("Codering around", agents.group_members(group)) == []


def test_react_normalizes_heart_and_aliases():
    from director.tools.group import normalize_react

    assert normalize_react("❤️") == "❤️"
    assert normalize_react("heart") == "❤️"
    assert normalize_react("love") == "❤️"
    assert normalize_react("+1") == "👍"


def test_group_round_speaks_or_stays_quiet(director, monkeypatch):
    import asyncio
    import json

    from director import agents, models, runtime

    agents.ensure_seeded()
    group = director.create_agent(
        name="Ops", kind="group",
        members=["agt_coder", "agt_operator"],
        rules="Be brief.")
    thread = director.create_thread(group["id"])
    director.add_message(thread["id"], "user", "Coder, say hi. Operator stay quiet.")

    async def fake_complete(**kwargs):
        instructions = str(kwargs.get("instructions") or "")
        tools = {item.get("name") for item in (kwargs.get("tools") or [])}
        if "start_work" in tools and "You are **Coder**" in instructions:
            items = kwargs.get("items") or []
            blob = json.dumps(items)
            if "Continue as yourself" in blob:
                return {"text": "", "tool_calls": [], "usage": {},
                        "backend": "openrouter", "model": "x"}
            return {"text": "Hi — Coder here.", "tool_calls": [], "usage": {},
                    "backend": "openrouter", "model": "x"}
        if "start_work" in tools:
            return {"text": "SILENT", "tool_calls": [], "usage": {},
                    "backend": "openrouter", "model": "x"}
        return {"text": "private done", "tool_calls": [], "usage": {},
                "backend": "openrouter", "model": "x"}

    monkeypatch.setattr(models, "complete", fake_complete)
    hub = runtime.Runtime()

    async def run():
        await hub.run_group_round(thread["id"])
        pending = [task for (tid, _), task in list(hub._group_speaks.items())
                   if tid == thread["id"]]
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

    asyncio.run(run())
    rows = director.list_messages(thread["id"])
    assistants = [row for row in rows if row["role"] == "assistant"]
    assert len(assistants) == 1
    assert assistants[0]["content"] == "Hi — Coder here."
    assert assistants[0]["meta"]["speaker_id"] == "agt_coder"


def test_group_start_work_runs_in_the_private_thread(director, monkeypatch):
    import asyncio
    import json

    from director import agents, models, runtime

    agents.ensure_seeded()
    group = director.create_agent(
        name="Ops", kind="group", members=["agt_coder", "agt_operator"])
    group_thread = director.create_thread(group["id"])
    private = director.latest_thread("agt_coder") or director.create_thread("agt_coder")
    director.add_message(group_thread["id"], "user", "Coder, please fix the login bug.")

    async def fake_complete(**kwargs):
        instructions = str(kwargs.get("instructions") or "")
        tools = {item.get("name") for item in (kwargs.get("tools") or [])}
        if "start_work" in tools and "You are **Coder**" in instructions:
            items = kwargs.get("items") or []
            blob = json.dumps(items)
            if "Continue as yourself" in blob:
                return {"text": "", "tool_calls": [], "usage": {},
                        "backend": "openrouter", "model": "x"}
            return {
                "text": "On it.",
                "tool_calls": [{
                    "call_id": "c1",
                    "name": "start_work",
                    "arguments": json.dumps({"task": "fix the login bug"}),
                }],
                "usage": {}, "backend": "openrouter", "model": "x",
            }
        if "start_work" in tools:
            return {"text": "SILENT", "tool_calls": [], "usage": {},
                    "backend": "openrouter", "model": "x"}
        return {"text": "Fixed the login bug.", "tool_calls": [], "usage": {},
                "backend": "openrouter", "model": "x"}

    monkeypatch.setattr(models, "complete", fake_complete)
    hub = runtime.Runtime()

    async def run():
        await hub.run_group_round(group_thread["id"])
        for _ in range(80):
            speaks = [task for task in hub._group_speaks.values() if not task.done()]
            turns = [task for task in hub._turn_tasks.values() if not task.done()]
            if not speaks and not turns:
                break
            await asyncio.sleep(0.02)
        leftover = [task for task in list(hub._group_speaks.values()) + list(hub._turn_tasks.values())
                    if not task.done()]
        if leftover:
            await asyncio.gather(*leftover, return_exceptions=True)

    asyncio.run(run())
    group_rows = director.list_messages(group_thread["id"])
    spoken = [row for row in group_rows if row["role"] == "assistant"
              and row["meta"].get("kind") != "work_done"]
    assert any("On it" in row["content"] for row in spoken)
    assert not any(row["role"] == "tool_call" and row["meta"].get("name") == "shell"
                   for row in group_rows)
    private_rows = director.list_messages(private["id"])
    assert any("login bug" in row["content"] for row in private_rows if row["role"] == "user")
    assert any("Fixed the login bug." in row["content"]
               for row in private_rows if row["role"] == "assistant")
    done = [row for row in group_rows if row["meta"].get("kind") == "work_done"]
    assert done
    assert done[0]["meta"]["speaker_id"] == "agt_coder"
    assert not hub.working_from_group(group_thread["id"])


def test_group_react_posts_a_reaction(director, monkeypatch):
    import asyncio
    import json

    from director import agents, models, runtime

    agents.ensure_seeded()
    group = director.create_agent(
        name="Ops", kind="group", members=["agt_coder", "agt_operator"])
    thread = director.create_thread(group["id"])
    director.add_message(thread["id"], "user", "sounds good?")

    async def fake_complete(**kwargs):
        instructions = str(kwargs.get("instructions") or "")
        tools = {item.get("name") for item in (kwargs.get("tools") or [])}
        items = kwargs.get("items") or []
        blob = json.dumps(items)
        if "start_work" in tools and "You are **Coder**" in instructions:
            if "Continue as yourself" in blob:
                return {"text": "", "tool_calls": [], "usage": {},
                        "backend": "openrouter", "model": "x"}
            return {
                "text": "",
                "tool_calls": [{
                    "call_id": "r1",
                    "name": "react",
                    "arguments": json.dumps({"emoji": "👍"}),
                }],
                "usage": {}, "backend": "openrouter", "model": "x",
            }
        if "start_work" in tools:
            return {"text": "SILENT", "tool_calls": [], "usage": {},
                    "backend": "openrouter", "model": "x"}
        return {"text": "private", "tool_calls": [], "usage": {},
                "backend": "openrouter", "model": "x"}

    monkeypatch.setattr(models, "complete", fake_complete)
    hub = runtime.Runtime()

    async def run():
        await hub.run_group_round(thread["id"])
        pending = [task for (tid, _), task in list(hub._group_speaks.items())
                   if tid == thread["id"]]
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

    asyncio.run(run())
    rows = director.list_messages(thread["id"])
    reacts = [row for row in rows if row["role"] == "reaction"]
    assert len(reacts) == 1
    assert reacts[0]["content"] == "👍"
    assert reacts[0]["meta"]["speaker_id"] == "agt_coder"
    assert reacts[0]["meta"].get("target_id")


def test_group_tag_wakes_the_named_agent(director, monkeypatch):
    import asyncio
    import json

    from director import agents, models, runtime

    agents.ensure_seeded()
    group = director.create_agent(
        name="Ops", kind="group", members=["agt_coder", "agt_operator"])
    thread = director.create_thread(group["id"])
    director.add_message(thread["id"], "user", "Coder, plan this with Operator.")
    operator_turns = {"n": 0}

    async def fake_complete(**kwargs):
        instructions = str(kwargs.get("instructions") or "")
        tools = {item.get("name") for item in (kwargs.get("tools") or [])}
        items = kwargs.get("items") or []
        blob = json.dumps(items)
        if "start_work" in tools and "You are **Coder**" in instructions:
            if "Continue as yourself" in blob:
                return {"text": "", "tool_calls": [], "usage": {},
                        "backend": "openrouter", "model": "x"}
            return {"text": "@Operator take the screen side.", "tool_calls": [],
                    "usage": {}, "backend": "openrouter", "model": "x"}
        if "start_work" in tools:
            operator_turns["n"] += 1
            if "@Operator take the screen side." in blob:
                return {"text": "On it.", "tool_calls": [], "usage": {},
                        "backend": "openrouter", "model": "x"}
            return {"text": "SILENT", "tool_calls": [], "usage": {},
                    "backend": "openrouter", "model": "x"}
        return {"text": "private", "tool_calls": [], "usage": {},
                "backend": "openrouter", "model": "x"}

    monkeypatch.setattr(models, "complete", fake_complete)
    hub = runtime.Runtime()

    async def run():
        await hub.run_group_round(thread["id"])
        for _ in range(80):
            pending = [task for task in hub._group_speaks.values() if not task.done()]
            if not pending:
                break
            await asyncio.sleep(0.02)
        leftover = [task for task in hub._group_speaks.values() if not task.done()]
        assert leftover == []

    asyncio.run(run())
    rows = director.list_messages(thread["id"])
    assistants = [row for row in rows if row["role"] == "assistant"]
    texts = [row["content"] for row in assistants]
    assert any("On it." in (text or "") for text in texts)
    assert operator_turns["n"] >= 1


def test_wake_packet_is_six_ff_then_the_mac_sixteen_times():
    from director import wake

    mac = wake.parse_mac("30-C5-99-D0-0D-4A")
    packet = wake.magic_packet(mac)
    assert packet[:6] == b"\xff" * 6
    assert packet[6:12] == mac
    assert len(packet) == 102
    assert packet[6:] == mac * 16
    assert wake.format_mac(mac) == "30:C5:99:D0:0D:4A"


def test_wake_status_hides_the_button_while_windows_is_online(director):
    from director import config, wake

    assert config.DEFAULT_SETTINGS["wake"]["mac"] == "30:C5:99:D0:0D:4A"
    asleep = wake.status(machines=[{
        "name": "calle-windows", "platform": "windows", "online": False}])
    assert asleep["available"] is True
    assert asleep["online"] is False
    awake = wake.status(machines=[{
        "name": "calle-windows", "platform": "windows", "online": True}])
    assert awake["online"] is True
    assert awake["can_power_off"] is True


def test_wake_status_probes_a_pc_without_a_director_client(director, monkeypatch):
    import asyncio

    from director import wake

    async def reachable(ip):
        assert ip == "192.168.50.22"
        return True

    monkeypatch.setattr(wake, "_probe_host", reachable)
    wake._probe_cache.update(ip="", at=0.0, online=False)
    result = asyncio.run(wake.status_with_probe(
        machines=[{"name": "calle-windows", "platform": "windows", "online": False}],
        settings={"wake": {"mac": "30:C5:99:D0:0D:4A", "ip": "192.168.50.22"}}))
    assert result["online"] is True
    assert result["reachable"] is True
    assert result["connected"] is False
    assert result["can_power_off"] is False


def test_stale_machine_socket_cannot_detach_a_reconnected_pc(director):
    from director import runtime, store

    machine = store.upsert_machine(
        name="calle-windows", platform="windows", token_hash="token", caps={})
    old_link = object()
    new_link = object()
    hub = runtime.Runtime()
    hub.attach_machine(machine["id"], old_link)
    hub.attach_machine(machine["id"], new_link)

    assert hub.detach_machine(machine["id"], old_link) is False
    assert hub.machine_link(machine["id"]) is new_link
    assert hub.online_machines()[0]["online"] is True
    assert hub.detach_machine(machine["id"], new_link) is True
    assert hub.online_machines()[0]["online"] is False


def test_wake_send_broadcasts_to_the_house_lan(director, monkeypatch):
    from director import config, wake

    sent = []

    class FakeSock:
        def setsockopt(self, *args):
            return None
        def settimeout(self, *args):
            return None
        def sendto(self, packet, addr):
            sent.append((packet, addr))
        def close(self):
            return None

    monkeypatch.setattr(wake.socket, "socket", lambda *a, **k: FakeSock())
    result = wake.send(config.DEFAULT_SETTINGS)
    assert result["ok"] is True
    hosts = {addr[0] for _, addr in sent}
    ports = {addr[1] for _, addr in sent}
    assert "255.255.255.255" in hosts
    assert "192.168.0.255" in hosts
    assert "192.168.0.83" in hosts
    assert ports == {9, 7}
    assert sent[0][0][:6] == b"\xff" * 6


def test_wake_remember_keeps_the_last_reported_nic(director):
    from director import config, wake

    wake.remember({"mac": "30-c5-99-d0-0d-4a", "ip": "192.168.0.99",
                   "broadcast": "192.168.0.255"})
    stored = config.load_settings(refresh=True)["wake"]
    assert stored["mac"] == "30:C5:99:D0:0D:4A"
    assert stored["ip"] == "192.168.0.99"
