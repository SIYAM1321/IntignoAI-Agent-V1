"""Offline tests for the NVIDIA OpenAI-compatible streaming adapter."""
import json
import os
import sys
import unittest
from unittest.mock import patch
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agent import engine
from agent.memory import Session


class FakeResponse:
    def __init__(self, records):
        self.records = [(("data: " + json.dumps(r) + "\n\n") if isinstance(r, dict) else str(r)).encode() for r in records]
        self.closed = False
    def __iter__(self):
        return iter(self.records)
    def close(self):
        self.closed = True


class NvidiaAdapterTests(unittest.TestCase):
    def setUp(self):
        self.old_key = os.environ.get("NVIDIA_API_KEY")
        os.environ["NVIDIA_API_KEY"] = "nvapi-test-key-value-123456789"
        engine.rate_limiter.configure(10)

    def tearDown(self):
        if self.old_key is None:
            os.environ.pop("NVIDIA_API_KEY", None)
        else:
            os.environ["NVIDIA_API_KEY"] = self.old_key

    def test_streamed_text_is_forwarded_and_persisted_in_history(self):
        response = FakeResponse([
            {"choices": [{"index": 0, "delta": {"role": "assistant", "content": "Hello "}, "finish_reason": None}]},
            {"choices": [{"index": 0, "delta": {"content": "there."}, "finish_reason": "stop"}]},
            "data: [DONE]\n\n",
        ])
        with patch.object(engine.urllib.request, "urlopen", return_value=response) as urlopen:
            session = Session()
            session.active_model = "z-ai/glm-5.3-flash"
            emitted = []
            run = engine.stream_interaction({
                "model": "z-ai/glm-5.3-flash", "system_instruction": "system prompt",
                "tools": [], "input": "say hello", "stream": True, "thinking_enabled": False,
            }, emitted.append, session=session)
        self.assertEqual("".join(emitted), "Hello there.")
        self.assertEqual(run.output_text, "Hello there.")
        self.assertFalse(run.calls)
        self.assertEqual(session._nim_histories["z-ai/glm-5.3-flash"][-1]["content"], "Hello there.")
        sent = json.loads(urlopen.call_args.args[0].data.decode())
        self.assertEqual(sent["chat_template_kwargs"]["reasoning_effort"], "low")
        self.assertTrue(response.closed)

    def test_native_tool_call_is_converted_to_agent_tool_schema(self):
        response = FakeResponse([
            {"choices": [{"index": 0, "delta": {"tool_calls": [{"index": 0, "id": "call123", "type": "function", "function": {"name": "check_port", "arguments": json.dumps({"port": 8000})}}]}, "finish_reason": None}]},
            {"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]},
            "data: [DONE]\n\n",
        ])
        with patch.object(engine.urllib.request, "urlopen", return_value=response):
            session = Session()
            session.active_model = "z-ai/glm-5.3-flash"
            run = engine.stream_interaction({
                "model": "z-ai/glm-5.3-flash", "system_instruction": "system prompt",
                "tools": [{"type": "function", "name": "check_port", "description": "check", "parameters": {"type": "object", "properties": {"port": {"type": "integer"}}}}],
                "input": "check port 8000", "stream": True,
            }, lambda _text: None, session=session)
        self.assertEqual(len(run.calls), 1)
        self.assertEqual(run.calls[0]["name"], "check_port")
        self.assertEqual(run.calls[0]["arguments"], {"port": 8000})
        self.assertEqual(run.calls[0]["call_id"], "call123")


if __name__ == "__main__":
    unittest.main()
