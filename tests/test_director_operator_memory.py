"""The operator remembers.

Every run used to begin from nothing: the same login route was re-discovered,
the same dead end walked into the next day. Three Spotify runs on 2026-08-14
each rediscovered that the artist switcher was the blocker, and none of them
left anything behind.

Two halves. What it chose to keep (`remember`), and how its recent runs went
(automatic, because "that did not work" is the lesson worth having).
"""
import asyncio

import pytest


@pytest.fixture()
def director(tmp_path, monkeypatch):
    monkeypatch.setenv("AIOS_DIRECTOR_HOME", str(tmp_path / "home"))
    import director.config as config
    import director.store as store
    store.close()
    config.load_settings(refresh=True)
    yield store
    store.close()


# ---------------- keeping a lesson ----------------

def test_remember_is_an_action_the_operator_can_call():
    from director.operator import loop, prompts

    assert "remember" in {tool["name"] for tool in prompts.ACTION_TOOLS}
    assert loop.TOOL_ACTIONS["remember"] == "remember"


def test_a_remembered_lesson_is_stored_in_the_operator_scope(director):
    from director.operator import loop

    said = asyncio.run(loop.execute(
        {"type": "remember", "key": "spotify-artist-switcher",
         "value": "The artist switcher is behind the avatar, top right."}, {}))
    assert "spotify-artist-switcher" in said
    rows = director.list_memory(scope=loop.MEMORY_SCOPE)
    assert rows and rows[0]["value"].startswith("The artist switcher")


def test_operator_memory_is_kept_out_of_the_coordinator_prompt(director):
    """The chat prompt injects the global set every turn; screen trivia there
    would cost tokens in every conversation."""
    from director.operator import loop
    from director.tools import memory as memory_tools

    asyncio.run(loop.execute({"type": "remember", "key": "k", "value": "screen trivia"}, {}))
    assert "screen trivia" not in memory_tools.memory_block()


def test_a_half_written_lesson_is_refused(director):
    from director.operator import loop

    said = asyncio.run(loop.execute({"type": "remember", "key": "k", "value": ""}, {}))
    assert "needs both" in said
    assert director.list_memory(scope=loop.MEMORY_SCOPE) == []


# ---------------- carrying it into the next run ----------------

def test_the_background_carries_what_it_learned(director):
    from director.operator import loop

    director.remember("gmail-code", "Codes land in the newest thread, not the top one.",
                      scope=loop.MEMORY_SCOPE)
    text = loop.background()
    assert "WHAT YOU LEARNED ON THIS SCREEN BEFORE" in text
    assert "newest thread" in text


def test_the_background_carries_how_recent_runs_went(director):
    from director.operator import loop

    job = director.create_job(kind="operator", request={"task": "open Spotify for Artists"},
                              thread_id="t", agent_id="a", status="running")
    director.update_job(job["id"], status="stopped",
                        result={"summary": "stuck on the login wall"})
    text = loop.background()
    assert "YOUR RECENT RUNS" in text
    assert "open Spotify for Artists" in text
    assert "stuck on the login wall" in text


def test_other_kinds_of_job_are_not_operator_history(director):
    from director.operator import loop

    job = director.create_job(kind="code", request={"task": "fix the header"},
                              thread_id="t", agent_id="a", status="done")
    director.update_job(job["id"], result={"summary": "shipped"})
    assert "fix the header" not in loop.background()


def test_a_first_run_on_a_clean_box_has_no_background(director):
    from director.operator import loop

    assert loop.background() == ""


def test_the_background_is_only_sent_on_the_first_step():
    """It belongs in the history after that, and repeating it every step pushes
    the screenshot out of the model's attention."""
    from director.operator import prompts

    first = prompts.task_message("t", 1280, 720, "", background="WHAT YOU LEARNED: x")
    later = prompts.task_message("t", 1280, 720, "history", background="")
    assert "WHAT YOU LEARNED" in first
    assert "WHAT YOU LEARNED" not in later


def test_the_operator_is_told_to_read_and_write_its_memory():
    from director.operator import prompts

    assert "You have been here before" in prompts.SYSTEM_PROMPT
    assert "`remember`" in prompts.SYSTEM_PROMPT
    assert "instruction to yourself" in prompts.SYSTEM_PROMPT
