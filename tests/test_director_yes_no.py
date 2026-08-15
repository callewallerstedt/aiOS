"""Yes/no confirmation box for Director agents.

The agent calls `ask_yes_no`; the phone renders an action card with a green
check (yes) and a red X (no); the chosen value flows back to the awaiting
agent turn. These tests pin the tool contract, the rendering, and the
result propagation.
"""

import asyncio
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
JS = (ROOT / "phone_site" / "director.js").read_text(encoding="utf-8")
CSS = (ROOT / "phone_site" / "director.css").read_text(encoding="utf-8")


def _block(source: str, start: str, end: str) -> str:
    i = source.index(start)
    j = source.index(end, i + len(start))
    return source[i:j]


def _css_rule(selector: str) -> str:
    i = CSS.index(selector)
    j = CSS.index("}", i)
    return CSS[i:j]


def _ctx(answer="No"):
    calls = {}

    async def ask_user(question="", options=None, **kw):
        calls["question"] = question
        calls["options"] = list(options or [])
        calls["kind"] = kw.get("kind")
        return answer

    class Context:
        agent = {"id": "agt_x"}
        thread_id = "thr_x"
        settings = {}
        request_approval = None
        emit = None
        cancel = None

    ctx = Context()
    ctx.ask_user = ask_user
    return ctx


async def _run(question="Proceed?", answer="Yes"):
    from director.tools import interaction

    return await interaction.ask_yes_no(_ctx(answer), question)


def test_tool_is_registered_and_exposed_to_agents():
    from director import agents, tools

    tools.load_all()
    names = {tool.name for tool in tools.all_tools()}
    assert "ask_yes_no" in names
    assert "ask_yes_no" in agents.CORE_TOOLS
    assert "ask_yes_no" in agents.DIRECTOR_TOOLS
    for spec in agents.DEFAULT_AGENTS:
        assert not tools.missing(spec["tools"])


def test_invoking_yes_answers_yes():
    res = asyncio.run(_run(answer="Yes"))
    assert res.output == "yes"


def test_yes_no_tool_routes_through_ask_user_with_two_options_and_returns_yes():
    res = asyncio.run(_run(answer="Yes"))
    assert res.error == ""
    assert res.output == "yes"
    assert res.card["meta"] == "yes"
    assert res.card["tone"] == "ok"


def test_no_answer_propagates_as_no():
    res = asyncio.run(_run(answer="No"))
    assert res.output == "no"
    assert res.card["meta"] == "no"
    assert res.card["tone"] == "danger"


def test_blank_question_is_rejected():
    res = asyncio.run(_run(question="  "))
    assert res.error == "no question given"


def test_yes_no_uses_kind_yes_no_and_two_options():
    seen = {}

    async def ask_user(question="", options=None, **kw):
        seen["kind"] = kw.get("kind")
        seen["options"] = list(options or [])
        return "No"

    from director.tools import interaction

    ctx = _ctx()
    ctx.ask_user = ask_user
    asyncio.run(interaction.ask_yes_no(ctx, "Shall I?"))
    assert seen["kind"] == "yes_no"
    assert seen["options"] == ["Yes", "No"]


def test_phone_renders_a_green_check_yes_button():
    block = _block(JS, "function questionCard", "async function answer")
    assert "yes_no" in block
    assert '"btn decision " + (yes ? "yes" : "no")' in block
    assert r'<path d="M4.5 12.5l4.5 4.5 10.5-10.5"/>' in block  # green check
    assert r'<path d="M6 6l12 12M18 6L6 18"/>' in block  # red X
    assert "aria-label" in block


def test_answer_posts_and_propagates():
    block = _block(JS, "function questionCard", "function answer")
    assert 'answer(payload.id, yes ? "Yes" : "No", wrap)' in block
    answer_block = _block(JS, "async function answer(",
                          "function shotCard")
    assert "api(`/api/questions/${id}`" in answer_block
    assert "body: JSON.stringify({ answer: text })" in answer_block


def test_yes_no_buttons_are_green_and_red_in_css():
    base = _css_rule(".action-card .btn.decision {")
    assert "min-height: 48px" in base
    assert "align-items: center" in base
    yes = _css_rule(".action-card .btn.decision.yes {")
    assert "var(--green)" in yes
    no = _css_rule(".action-card .btn.decision.no {")
    assert "var(--red)" in no