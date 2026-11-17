"""Codex event adapter checks without network or a real agent turn."""

import asyncio
import json
import unittest
from unittest.mock import patch

from friday.brain import TASK_PERSONA
from friday.codex_brain import CodexBrain


class _Input:
    def __init__(self):
        self.data = b""

    def write(self, data):
        self.data += data

    async def drain(self):
        pass

    def close(self):
        pass


class _Process:
    def __init__(self):
        self.stdin = _Input()
        self.stdout = asyncio.StreamReader()
        self.stderr = asyncio.StreamReader()
        for event in (
            {"type": "thread.started", "thread_id": "session-123"},
            {"type": "item.completed", "item": {"type": "agent_message", "text": "Done."}},
            {"type": "turn.completed"},
        ):
            self.stdout.feed_data((json.dumps(event) + "\n").encode())
        self.stdout.feed_eof()
        self.stderr.feed_eof()
        self.returncode = 0

    async def wait(self):
        return self.returncode


class CodexAdapterTests(unittest.IsolatedAsyncioTestCase):
    async def test_streams_reply_and_resumes_own_session(self):
        calls = []
        processes = []

        async def spawn(*args, **kwargs):
            calls.append(args)
            proc = _Process()
            processes.append(proc)
            return proc

        brain = CodexBrain(TASK_PERSONA)
        await brain.configure("gpt-5.6-sol", "high")
        with patch("asyncio.create_subprocess_exec", side_effect=spawn):
            first = [event async for event in brain.ask("Build a small app")]
            second = [event async for event in brain.ask("Now explain it")]
        self.assertEqual([event.kind for event in first], ["say", "done"])
        self.assertEqual(brain.session_id, "session-123")
        self.assertIn("--model", calls[0])
        self.assertNotIn("resume", calls[0])
        self.assertIn("resume", calls[1])
        self.assertIn("Build a small app", processes[0].stdin.data.decode())
        self.assertEqual(processes[1].stdin.data.decode(), "Now explain it")


if __name__ == "__main__":
    unittest.main()
