"""Fast checks for model selection and the unchanged Claude SDK configuration path."""

import asyncio
import unittest

from friday import config
from friday.brain import Brain
from friday.gate import Gate
from friday.routing import choose, requires_claude
from friday.settings import _validate


async def _deny(_spoken, _detail):
    return False, "no"


class RoutingTests(unittest.TestCase):
    def test_claude_auto_routes_by_difficulty(self):
        settings = {"provider": "claude", "model_mode": "auto"}
        self.assertEqual((choose("Hi", settings).model, choose("Hi", settings).effort),
                         ("claude-haiku-5-5", "low"))
        self.assertEqual((choose("Summarize the meeting notes", settings).model,
                          choose("Summarize the meeting notes", settings).effort),
                         ("claude-sonnet-5-5", "medium"))
        self.assertEqual((choose("Debug and refactor this codebase", settings).model,
                          choose("Debug and refactor this codebase", settings).effort),
                         (config.MODEL, "max"))

    def test_manual_preserves_existing_claude_settings(self):
        route = choose("Hi", {"provider": "claude", "model_mode": "manual", "effort": "high"})
        self.assertEqual((route.model, route.effort), (config.MODEL, "high"))

    def test_codex_route_and_native_tool_fallback(self):
        settings = {"provider": "codex", "model_mode": "auto", "codex_model": "gpt-5.6-sol"}
        self.assertEqual(choose("Open Safari", settings).model, "gpt-5.6-luna")
        self.assertEqual((choose("Build a dashboard", settings).model,
                          choose("Build a dashboard", settings).effort), ("gpt-5.6-sol", "max"))
        self.assertTrue(requires_claude("Read my email"))
        self.assertTrue(requires_claude("What's on my calendar?"))
        self.assertFalse(requires_claude("Build a dashboard"))

    def test_settings_validation(self):
        self.assertEqual(_validate("provider", "claude"), "claude")
        self.assertEqual(_validate("model_mode", "auto"), "auto")
        with self.assertRaises(ValueError):
            _validate("codex_model", "bad model; rm -rf ~")

    def test_claude_options_use_selected_route(self):
        brain = Brain(Gate(_deny), browser=False)
        asyncio.run(brain.configure("claude-sonnet-5-5", "medium"))
        opts = brain._options()
        self.assertEqual((opts.model, opts.effort), ("claude-sonnet-5-5", "medium"))


if __name__ == "__main__":
    unittest.main()
