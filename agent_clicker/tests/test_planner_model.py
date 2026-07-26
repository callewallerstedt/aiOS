import tempfile
import unittest
from unittest import mock

from PIL import Image

from desktop_agent import loop as loop_module
from desktop_agent.loop import AgentLoop
from desktop_agent.screen import Monitor


class PlannerModelTests(unittest.TestCase):
    def test_planner_runs_once_and_hands_plan_to_clicking_model(self):
        events = []
        calls = []

        def fake_chat(system, messages, model=None, **kwargs):
            calls.append({"system": system, "messages": messages, "model": model, **kwargs})
            if len(calls) == 1:
                return '{"plan":"1. Open Settings. 2. Verify the result."}'
            return '{"thought":"Ready","message":"Done","status":"done","actions":[]}'

        monitor = Monitor(index=1, left=0, top=0, width=64, height=48, label="Test")
        image = Image.new("RGB", (64, 48), "white")
        agent = AgentLoop(events.append)

        with tempfile.TemporaryDirectory() as folder, \
                mock.patch.object(loop_module, "DEBUG_ROOT", folder), \
                mock.patch.object(loop_module, "capture", return_value=image), \
                mock.patch.object(loop_module.vlm, "chat_raw", side_effect=fake_chat):
            agent._run(
                "Change one setting",
                monitor,
                "gpt-5.6-luna",
                1,
                0,
                0,
                backend="codex",
                reasoning_effort="low",
                planner_model="gpt-5.6-sol",
            )

        # plan, then execute, then the completion check — which reuses the
        # planner model for its second opinion.
        self.assertEqual([call["model"] for call in calls],
                         ["gpt-5.6-sol", "gpt-5.6-luna", "gpt-5.6-sol"])
        self.assertEqual(calls[0]["reasoning_effort"], "high")
        execution_text = str(calls[1]["messages"])
        self.assertIn("PLAN from the planning model", execution_text)
        self.assertTrue(any(event.get("type") == "plan" for event in events))


if __name__ == "__main__":
    unittest.main()
