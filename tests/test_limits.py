"""Checks for the safety limits on agent runs: CLI buffer size, subagent cap and task budget.

Run with: .venv/bin/python -m unittest tests.test_limits -v
"""

import asyncio
import unittest

from claude_agent_sdk import ResultMessage

from friday import config
from friday.brain import Brain, compose_persona, TASK_PERSONA
from friday.gate import Gate


async def _deny(_spoken, _detail):
    return False, "no"


def _decision(out: dict) -> str | None:
    return (out.get("hookSpecificOutput") or {}).get("permissionDecision")


class LimitTests(unittest.TestCase):
    def test_buffer_is_raised_above_sdk_default(self):
        opts = Brain(Gate(_deny), browser=False)._options()
        self.assertGreater(opts.max_buffer_size, 1024 * 1024)

    def test_budget_only_on_background_tasks(self):
        self.assertIsNone(Brain(Gate(_deny), browser=False)._options().max_budget_usd)
        task = Brain(Gate(_deny), persona=TASK_PERSONA, browser=False, budget_usd=7.5)
        self.assertEqual(task._options().max_budget_usd, 7.5)

    def test_subagent_hook_is_registered_for_task_tools(self):
        matchers = Brain(Gate(_deny), browser=False)._options().hooks["PreToolUse"]
        self.assertEqual(matchers[0].matcher, "Task|Agent")

    def test_subagents_refused_after_cap_and_reset_per_request(self):
        brain = Brain(Gate(_deny), browser=False)
        hook = brain._limit_subagents
        inp = {"hook_event_name": "PreToolUse", "tool_name": "Task", "tool_input": {}}
        results = [asyncio.run(hook(inp, f"t{i}", None)) for i in range(config.MAX_SUBAGENTS + 3)]
        allowed = [r for r in results if _decision(r) != "deny"]
        self.assertEqual(len(allowed), config.MAX_SUBAGENTS)
        self.assertTrue(all(_decision(r) == "deny" for r in results[config.MAX_SUBAGENTS:]))
        brain.subagents = 0  # what ask() does at the start of each request
        self.assertNotEqual(_decision(asyncio.run(hook(inp, "next", None))), "deny")

    def test_budget_stop_is_reported_as_failure_with_reason(self):
        brain = Brain(Gate(_deny), browser=False, budget_usd=10)
        msg = ResultMessage(subtype="error_max_budget_usd", duration_ms=1, duration_api_ms=1,
                            is_error=True, num_turns=3, session_id="s", total_cost_usd=10.2)
        ev = brain._track(msg)[-1]
        self.assertEqual(ev.kind, "error")
        self.assertIn("$10 task budget", ev.text)

    def test_persona_states_the_limit(self):
        self.assertIn(f"({config.MAX_SUBAGENTS},", compose_persona(TASK_PERSONA))


if __name__ == "__main__":
    unittest.main()
