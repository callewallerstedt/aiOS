"""Two things Director got wrong in the same session on 2026-08-15.

*Waking.* A CODE job finished while the agent was still mid-turn. The result
was written into the transcript, `run_turn` dropped the wake because the thread
was busy, and nobody ever read it — so Director kept telling Calle a job was
running that had already failed.

*Seeing the prompt.* "What are the agents actually told?" was unanswerable from
the phone: the prompt lived partly in code and partly in settings. It is now
one assembled list, readable and editable from Settings.
"""
import asyncio
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
JS = (ROOT / "phone_site" / "director.js").read_text(encoding="utf-8")
CSS = (ROOT / "phone_site" / "director.css").read_text(encoding="utf-8")


@pytest.fixture()
def director(tmp_path, monkeypatch):
    monkeypatch.setenv("AIOS_DIRECTOR_HOME", str(tmp_path / "home"))
    import director.config as config
    import director.store as store
    store.close()
    config.load_settings(refresh=True)
    yield store
    store.close()


def _thread(store):
    agent = store.create_agent(name="Björn", emoji="🧔", kind="custom",
                               system_prompt="Be blunt.", tools=[])
    return agent, store.create_thread(agent["id"])


# ---------------- waking ----------------

def test_a_job_that_lands_mid_turn_still_gets_a_turn(director):
    from director import runtime

    agent, thread = _thread(director)
    hub = runtime.Runtime()
    ran = []

    async def scenario():
        hub.run_turn = lambda thread_id, trigger="user": ran.append(trigger)
        # A turn is in flight: `busy` is true for the whole of this block.
        hub._turn_tasks[thread["id"]] = asyncio.create_task(asyncio.sleep(0.2))
        await hub._report_job({"id": "job_1", "kind": "code",
                               "thread_id": thread["id"], "agent_id": agent["id"]},
                              {"status": "fail", "summary": "no such project directory"})
        assert not ran, "a busy thread should not be interrupted"
        assert thread["id"] in hub._owed_turns
        hub._turn_tasks[thread["id"]].cancel()

    asyncio.run(scenario())
    notes = [m for m in director.list_messages(thread["id"]) if m["role"] == "system"]
    assert notes and "no such project directory" in notes[-1]["content"]


def test_an_idle_thread_is_woken_straight_away(director):
    from director import runtime

    agent, thread = _thread(director)
    hub = runtime.Runtime()
    ran = []

    async def scenario():
        async def fake_run_turn(thread_id, trigger="user"):
            ran.append(trigger)

        hub.run_turn = fake_run_turn
        await hub.wake(thread["id"], trigger="job")

    asyncio.run(scenario())
    assert ran == ["job"]


def test_a_note_the_running_turn_already_answered_needs_no_second_turn(director):
    from director import runtime

    agent, thread = _thread(director)
    hub = runtime.Runtime()
    note = director.add_message(thread["id"], "system", "[job finished]", {})
    hub._owed_turns[thread["id"]] = note["id"]
    director.add_message(thread["id"], "assistant", "That job failed — bad path.", {})
    assert hub._still_owed(thread["id"], director.list_messages(thread["id"])) is False


def test_a_note_nobody_replied_to_still_owes_a_turn(director):
    from director import runtime

    agent, thread = _thread(director)
    hub = runtime.Runtime()
    director.add_message(thread["id"], "assistant", "Dispatched.", {})
    note = director.add_message(thread["id"], "system", "[job failed]", {})
    hub._owed_turns[thread["id"]] = note["id"]
    assert hub._still_owed(thread["id"], director.list_messages(thread["id"])) is True


def test_a_waiting_code_session_lands_in_the_chat_and_wakes_the_agent(director):
    from director import runtime

    agent, thread = _thread(director)
    hub = runtime.Runtime()
    woken = []

    async def scenario():
        async def fake_run_turn(thread_id, trigger="user"):
            woken.append(trigger)

        hub.run_turn = fake_run_turn
        await hub.code_question(
            {"id": "job_9", "thread_id": thread["id"], "agent_id": agent["id"]},
            {"question": "How should I scope the commit before pushing to main?"})

    asyncio.run(scenario())
    assert woken == ["code.question"]
    notes = [m for m in director.list_messages(thread["id"]) if m["role"] == "system"]
    assert "scope the commit" in notes[-1]["content"]
    assert notes[-1]["meta"]["kind"] == "code.question"
    assert notes[-1]["meta"]["job_id"] == "job_9"


def test_a_question_without_a_thread_is_ignored(director):
    from director import runtime

    hub = runtime.Runtime()
    asyncio.run(hub.code_question({"id": "job_9", "thread_id": ""}, {"question": "?"}))


# ---------------- a question that outlives a reload ----------------

def test_an_unanswered_question_is_still_there_when_the_phone_opens(director):
    """The card only ever existed as a live event, so closing the app mid-ask
    left the agent waiting an hour on an answer nobody was shown."""
    from director import runtime

    agent, thread = _thread(director)
    hub = runtime.Runtime()
    seen = {}

    async def scenario():
        asking = asyncio.create_task(
            hub.ask_user(thread["id"], agent["id"], "Ship it?",
                         options=["Yes", "No"], kind="yes_no"))
        await asyncio.sleep(0)
        seen["pending"] = hub.pending_questions(thread["id"])
        hub.answer_question(seen["pending"][0]["id"], "Yes")
        await asking
        seen["after"] = hub.pending_questions(thread["id"])

    asyncio.run(scenario())
    assert seen["pending"][0]["question"] == "Ship it?"
    assert seen["pending"][0]["kind"] == "yes_no"
    assert seen["after"] == []


def test_pending_questions_stay_in_their_own_thread(director):
    from director import runtime

    agent, thread = _thread(director)
    hub = runtime.Runtime()

    async def scenario():
        asking = asyncio.create_task(
            hub.ask_user(thread["id"], agent["id"], "Ship it?", kind="yes_no"))
        await asyncio.sleep(0)
        assert hub.pending_questions("thr_somewhere_else") == []
        hub.answer_question(hub.pending_questions(thread["id"])[0]["id"], "Yes")
        await asking

    asyncio.run(scenario())


def test_a_restart_closes_out_jobs_whose_waiter_died(director):
    """The task that polls a CODE job lives in memory. Live proof: one job sat
    at "running" long after its session had stopped."""
    from director import server

    job = director.create_job(kind="code", request={}, thread_id="thr_x",
                              agent_id="agt_x", machine_id="mch_1", status="running")
    server._release_orphaned_jobs()
    row = director.get_job(job["id"])
    assert row["status"] == "stopped"
    assert "Director restarted" in row["result"]["summary"]


def test_a_finished_job_is_left_alone_by_a_restart(director):
    from director import server

    job = director.create_job(kind="code", request={}, thread_id="thr_x",
                              agent_id="agt_x", machine_id="mch_1", status="done")
    director.update_job(job["id"], result={"summary": "shipped"})
    server._release_orphaned_jobs()
    row = director.get_job(job["id"])
    assert row["status"] == "done" and row["result"]["summary"] == "shipped"


def test_the_phone_redraws_a_question_it_missed():
    assert "thread?.questions" in JS
    assert "data.thread.questions = data.questions || [];" in JS


# ---------------- seeing and editing the prompt ----------------

def test_the_assembled_prompt_is_exactly_the_sections(director):
    from director import agents

    agent, _ = _thread(director)
    sections = agents.prompt_sections(agent)
    joined = "\n\n".join(s["text"].strip() for s in sections)
    assert joined == agents.system_prompt(agent)


def test_the_sections_name_what_calle_can_change(director):
    from director import agents

    agent, _ = _thread(director)
    by_key = {s["key"]: s for s in agents.prompt_sections(agent)}
    assert by_key["base"]["editable"] == "settings"
    assert by_key["custom"]["editable"] == "agent"
    assert by_key["environment"]["editable"] == "live"
    assert "Be blunt." in by_key["custom"]["text"]


def test_an_edited_block_replaces_the_shipped_one(director):
    from director import agents, config

    agent, _ = _thread(director)
    config.update_settings({"prompts": {"base": "Answer only in Swedish."}})
    prompt = agents.system_prompt(agent, config.load_settings(refresh=True))
    assert "Answer only in Swedish." in prompt
    assert "sharp coworker in Slack" not in prompt


def test_clearing_a_block_restores_the_shipped_one(director):
    from director import agents, config

    agent, _ = _thread(director)
    config.update_settings({"prompts": {"base": "Temporary."}})
    config.update_settings({"prompts": {"base": ""}})
    prompt = agents.system_prompt(agent, config.load_settings(refresh=True))
    assert "sharp coworker in Slack" in prompt


def test_group_prompts_use_the_same_editable_blocks(director):
    from director import agents, config

    member, _ = _thread(director)
    config.update_settings({"prompts": {"group": "Say nothing but yes."}})
    prompt = agents.group_system_prompt(
        member, {"name": "Group1"}, [member], config.load_settings(refresh=True))
    assert "Say nothing but yes." in prompt


def test_prompt_defaults_expose_every_editable_block():
    from director import agents

    blocks = agents.prompt_defaults()
    assert set(blocks) == {"base", "coordinator", "group"}
    assert all(block["default"] and block["label"] for block in blocks.values())


# ---------------- what the phone shows ----------------

def test_the_phone_renders_a_waiting_code_session():
    assert "function codeQuestionCard(" in JS
    assert 'case "code.question":' in JS
    assert "waiting on you" in JS


def test_answering_a_code_question_goes_through_the_agent():
    """Straight to the session would leave the agent not knowing what was
    decided, and the answer out of the transcript."""
    block = JS[JS.index("function codeQuestionCard("):JS.index("async function answer(")]
    assert "sendToThread(" in block
    assert "Answer for CODE job" in block


def test_the_settings_screen_shows_the_whole_prompt():
    assert "async function promptGroup(" in JS
    assert '"/api/prompt"' in JS
    assert "System prompt" in JS
    assert "promptGroup()" in JS[JS.index("async function openSettings("):]


def test_prompt_blocks_are_editable_and_resettable():
    block = JS[JS.index("async function editPromptBlock("):JS.index("async function openSettings(")]
    assert "prompts:" in block
    assert "Use the built-in text" in block


def test_the_prompt_view_is_collapsed_until_tapped():
    """The whole prompt is thousands of characters; a settings screen that
    opens as a wall of text is not readable on a phone."""
    assert ".prompt-text {" in CSS
    assert ".prompt-block.open .prompt-text {" in CSS
